"""Injected dependencies: allowlisted subprocess runner, GET-only HTTP, rooted file reader, run deadline.

Sections never import subprocess/urllib/open themselves; tests swap these for fakes.
"""

import glob
import json
import os
import re
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

# ── deadline ────────────────────────────────────────────────────────────────


class BudgetExceeded(BaseException):
    """BaseException so a section's `except Exception` cannot swallow it."""


class Deadline:
    def __init__(self, budget_s=60.0, clock=time.monotonic):
        self.clock = clock
        self.end = clock() + budget_s

    def remaining(self):
        return self.end - self.clock()

    def expired(self):
        return self.remaining() <= 0

    def clip(self, timeout):
        left = self.remaining()
        if left <= 0:
            raise BudgetExceeded()
        return max(0.1, min(float(timeout), left))


# ── subprocess allowlist ───────────────────────────────────────────────────

GIT_PREFIX = ("git", "-c", "core.fsmonitor=false", "-c", "safe.directory=*", "--no-optional-locks")
GIT_STATUS = GIT_PREFIX + ("status", "--porcelain")
GIT_BRANCH = GIT_PREFIX + ("rev-parse", "--abbrev-ref", "HEAD")
GIT_HEAD = GIT_PREFIX + ("rev-parse", "--short=12", "HEAD")
GIT_LOG = GIT_PREFIX + ("log", "-1", "--format=%ct%x00%s")
GIT_AHEAD = GIT_PREFIX + ("rev-list", "--left-right", "--count", "@{u}...HEAD")
GIT_ARGVS = frozenset({GIT_STATUS, GIT_BRANCH, GIT_HEAD, GIT_LOG, GIT_AHEAD})

DOCKER_PS = ("docker", "ps", "-a", "--format", "{{.Names}}")
DOCKER_INSPECT_TPL = ("{{.Name}}\t{{.Config.Image}}\t{{.State.Status}}\t"
                      "{{if .State.Health}}{{.State.Health.Status}}{{end}}\t{{.State.StartedAt}}\t{{.RestartCount}}")
DOCKER_INSPECT = ("docker", "inspect", "--type", "container", "--format", DOCKER_INSPECT_TPL)
DOCKER_LOGS = ("docker", "logs", "--tail", "40")
CONTAINER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")

SYSTEMCTL_SHOW = ("systemctl", "show", "--no-pager", "--property=Id,LoadState,ActiveState,SubState")
SYSTEMCTL_IS_ACTIVE = ("systemctl", "is-active")
UNIT_RE = re.compile(r"[A-Za-z0-9@_.*:-]{1,128}")

DF = ("df", "-P", "-k", "-x", "tmpfs", "-x", "devtmpfs", "-x", "overlay", "-x", "squashfs", "-x", "efivarfs")

ALLOWED_BINARIES = frozenset({"git", "docker", "systemctl", "df"})


class CommandRefused(Exception):
    pass


def _args_ok(args, pattern):
    return bool(args) and all(isinstance(a, str) and not a.startswith("-") and pattern.fullmatch(a) for a in args)


def check_argv(argv):
    """Raise CommandRefused unless argv is exactly one of the fixed read-only shapes."""
    argv = tuple(argv)
    if not argv or argv[0] not in ALLOWED_BINARIES:
        raise CommandRefused("binary not allowed")
    if argv in GIT_ARGVS or argv == DOCKER_PS or argv == DF:
        return
    n = len(DOCKER_INSPECT)
    if argv[:n] == DOCKER_INSPECT and _args_ok(argv[n:], CONTAINER_RE):
        return
    n = len(DOCKER_LOGS)
    if argv[:n] == DOCKER_LOGS and len(argv) == n + 1 and _args_ok(argv[n:], CONTAINER_RE):
        return
    n = len(SYSTEMCTL_SHOW)
    if argv[:n] == SYSTEMCTL_SHOW and _args_ok(argv[n:], UNIT_RE):
        return
    n = len(SYSTEMCTL_IS_ACTIVE)
    if argv[:n] == SYSTEMCTL_IS_ACTIVE and len(argv) == n + 1 and _args_ok(argv[n:], UNIT_RE):
        return
    raise CommandRefused("argv not allowed")


SAFE_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LC_ALL": "C",
    "HOME": "/root",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "SYSTEMD_PAGER": "",
}
MAX_OUTPUT = 1 << 20


class Runner:
    """runner(argv, timeout, cwd=None) -> (returncode, stdout, stderr). No shell, fixed argv shapes only."""

    def __init__(self, deadline, run=None):
        self.deadline = deadline
        self._run = run or subprocess.run

    def __call__(self, argv, timeout=5.0, cwd=None):
        check_argv(argv)
        try:
            res = self._run(list(argv), shell=False, capture_output=True, stdin=subprocess.DEVNULL,
                            timeout=self.deadline.clip(timeout), cwd=cwd, env=dict(SAFE_ENV))
        except subprocess.TimeoutExpired:
            return 124, "", "timeout"
        except OSError as e:
            return 127, "", type(e).__name__
        out = (res.stdout or b"")[:MAX_OUTPUT]
        err = (res.stderr or b"")[:MAX_OUTPUT]
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return res.returncode, out, err


# ── GET-only HTTP ───────────────────────────────────────────────────────────

ALLOWED_HOSTS = frozenset({"paper-api.alpaca.markets", "api-fxpractice.oanda.com"})
MAX_BODY = 4 << 20


class HttpError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class HttpGet:
    """http_get(url, headers, timeout=10) -> parsed JSON. GET only, https only, allowlisted hosts, no redirects."""

    METHOD = "GET"

    def __init__(self, deadline, opener=None):
        self.deadline = deadline
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def __call__(self, url, headers=None, timeout=10.0):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS or parts.username or parts.port:
            raise HttpError("host not allowed")
        req = urllib.request.Request(url, method=self.METHOD, headers=dict(headers or {}))
        if req.get_method() != "GET":
            raise HttpError("method not allowed")
        try:
            with self.opener.open(req, timeout=self.deadline.clip(timeout)) as resp:
                raw = resp.read(MAX_BODY + 1)
        except urllib.error.HTTPError as e:
            e.close()
            raise HttpError(f"HTTP {e.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise HttpError(type(e).__name__) from None
        if len(raw) > MAX_BODY:
            raise HttpError("response too large")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise HttpError("bad JSON") from None


# ── rooted read-only filesystem ─────────────────────────────────────────────


class Reader:
    """All paths are absolute box paths; root lets tests point the whole box at a temp dir."""

    def __init__(self, root="/"):
        self.root = os.path.realpath(root)

    def _p(self, path):
        return os.path.join(self.root, path.lstrip("/")) if self.root != "/" else path

    def _unp(self, real):
        if self.root == "/":
            return real
        rel = os.path.relpath(real, self.root)
        return "/" if rel == "." else "/" + rel

    def exists(self, path):
        return os.path.lexists(self._p(path))

    def isdir(self, path):
        return os.path.isdir(self._p(path))

    def islink(self, path):
        return os.path.islink(self._p(path))

    def lstat(self, path):
        return os.lstat(self._p(path))

    def isfile_nolink(self, path):
        try:
            return stat.S_ISREG(os.lstat(self._p(path)).st_mode)
        except OSError:
            return False

    def realpath(self, path):
        return self._unp(os.path.realpath(self._p(path)))

    def glob(self, pattern):
        return sorted(self._unp(p) for p in glob.glob(self._p(pattern)))

    def listdir(self, path):
        return sorted(os.listdir(self._p(path)))

    def walk(self, top):
        for d, dirs, files in os.walk(self._p(top), followlinks=False):
            yield self._unp(d), dirs, files

    def host_path(self, path):
        """Real filesystem path (for subprocess cwd)."""
        return self._p(path)

    def read_bytes(self, path, max_bytes=65536, tail=False):
        """(data, truncated). Refuses symlinks and non-regular files."""
        fd = os.open(self._p(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise OSError("not a regular file")
            truncated = st.st_size > max_bytes
            if tail and truncated:
                fh.seek(st.st_size - max_bytes)
            return fh.read(max_bytes), truncated

    def read_text(self, path, max_bytes=65536, tail=False):
        data, truncated = self.read_bytes(path, max_bytes, tail)
        return data.decode("utf-8", "replace"), truncated

    def tail_lines(self, path, n, max_bytes=65536):
        text, truncated = self.read_text(path, max_bytes, tail=True)
        lines = text.splitlines()
        if truncated and lines:
            lines = lines[1:]  # first line may be partial
        return [ln for ln in lines if ln.strip()][-n:]


def default_push_status():
    """Capture `grokbot_push.py --status` output by calling its own status() (no subprocess)."""
    import contextlib
    import io
    import sys
    from datetime import datetime, timezone

    tba = "/opt/scripts/tba"
    if tba not in sys.path:
        sys.path.insert(0, tba)
    import grokbot_push as gp

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        gp.status(gp.parse_args(["--status"]), datetime.now(timezone.utc))
    return buf.getvalue()


@dataclass
class Deps:
    runner: Callable
    http_get: Callable
    reader: Reader
    deadline: Deadline
    now: Callable = time.time
    push_status: Callable = default_push_status


def real_deps(budget_s=60.0):
    deadline = Deadline(budget_s)
    return Deps(runner=Runner(deadline), http_get=HttpGet(deadline), reader=Reader("/"), deadline=deadline)
