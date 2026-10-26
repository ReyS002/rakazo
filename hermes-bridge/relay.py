"""Bull Desk -> Hermes one-way notification relay.

Checks a Bearer key, validates a small JSON body, signs it (HMAC-SHA256 V2 over
"<timestamp>.<body>") and forwards it to the Hermes webhook. Delivery only:
bot text is untrusted and is never interpreted here. Python stdlib only.

Optional POST /task (enabled only when TASK_KEYS, TASK_HMAC_SECRET and
HERMES_TASK_URL are all set) forwards a build-task request with its own keys,
secret, rate limit and kill switch. Hermes only queues it for owner approval.
GET /results (same TASK_KEYS) serves the sanitized status files Hermes publishes
into RESULTS_DIR (read-only mount); it never touches anything else.
GET /observe[/<name>] (enabled only when OBS_KEYS is set) serves the oversight
snapshots a host-side collector writes into OBSERVE_DIR (read-only mount); only
the six allowlisted file names are ever opened.
"""

import collections
import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "0.0.0.0"
PORT = 8080
KILL_FILE = "/run/bridge/KILL"
KILL_TASK_FILE = "/run/bridge/KILL_TASK"
MAX_BODY = 4096
MAX_TASK_BODY = 6144
MIN_KEY_LEN = 32
UPSTREAM_TIMEOUT = 10
PER_MINUTE = 10
PER_DAY = 100
TASK_PER_HOUR = 6
TASK_PER_DAY = 20
FIELDS = {"title": 120, "message": 2000, "from": 40}
TASK_FIELDS = {"title": 120, "task": 4000, "from": 40}
TASK_MODELS = ("claude", "codex")
RESULTS_DIR = os.environ.get("RESULTS_DIR", "/run/bridge/results")
RESULTS_PER_MINUTE = 30
RESULTS_LIST_MAX = 10
RESULTS_SCAN_MAX = 30       # files read at most per list request
MAX_RESULT_FILE = 8192
RESULT_ID_RE = re.compile(r"bd-[0-9]{8}-[0-9]{6}-[0-9a-f]{4}")
RESULT_FILE_RE = re.compile(r"(bd-[0-9]{8}-[0-9]{6}-[0-9a-f]{4})\.json")
KILL_OBSERVE_FILE = "/run/bridge/KILL_OBSERVE"
OBSERVE_DIR = os.environ.get("OBSERVE_DIR", "/run/bridge/observe")
OBSERVE_NAMES = ("health", "scanner", "paper", "projects", "vault", "logs")
OBSERVE_FILES = {name: name + ".json" for name in OBSERVE_NAMES}  # the only paths ever opened
OBSERVE_ENVELOPE = ("name", "generated_at", "ok", "error", "data")
OBSERVE_PER_MINUTE = 60
OBSERVE_STALE_S = 600
MAX_SNAPSHOT_FILE = 256 * 1024

# C0/C1 controls except "\n", plus bidi overrides that can spoof rendered text.
_STRIP = {c for c in range(0x20) if c != 0x0A} | {0x7F} | set(range(0x80, 0xA0))
_STRIP |= set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A))
_STRIP_TABLE = dict.fromkeys(_STRIP)


class ConfigError(Exception):
    pass


def split_keys(env, name):
    return [k.strip() for k in env.get(name, "").split(",") if k.strip()]


def load_config(env):
    keys = split_keys(env, "BRIDGE_KEYS")
    secret = env.get("BRIDGE_HMAC_SECRET", "").strip()
    url = env.get("HERMES_WEBHOOK_URL", "").strip()
    if not keys:
        raise ConfigError("BRIDGE_KEYS missing or empty")
    if any(len(k) < MIN_KEY_LEN for k in keys):
        raise ConfigError(f"every BRIDGE_KEYS entry must be at least {MIN_KEY_LEN} chars")
    if not secret:
        raise ConfigError("BRIDGE_HMAC_SECRET missing or empty")
    if not url:
        raise ConfigError("HERMES_WEBHOOK_URL missing or empty")
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise ConfigError("HERMES_WEBHOOK_URL must be http(s)")
    return {
        "keys": [k.encode() for k in keys],
        "secret": secret.encode(),
        "url": url,
        "task": load_task_config(env, keys),
        "observe": load_observe_config(env, keys),
    }


def load_observe_config(env, bridge_keys):
    """Return the /observe config, or None (route disabled) if OBS_KEYS is unset."""
    keys = split_keys(env, "OBS_KEYS")
    if not keys:
        return None
    if any(len(k) < MIN_KEY_LEN for k in keys):
        raise ConfigError(f"every OBS_KEYS entry must be at least {MIN_KEY_LEN} chars")
    if set(keys) & (set(bridge_keys) | set(split_keys(env, "TASK_KEYS"))):
        raise ConfigError("OBS_KEYS must not share a key with BRIDGE_KEYS or TASK_KEYS")
    return {"keys": [k.encode() for k in keys]}


def load_task_config(env, bridge_keys):
    """Return the /task config, or None (route disabled) if any of its variables is unset."""
    keys = split_keys(env, "TASK_KEYS")
    secret = env.get("TASK_HMAC_SECRET", "").strip()
    url = env.get("HERMES_TASK_URL", "").strip()
    if not (keys and secret and url):
        return None
    if any(len(k) < MIN_KEY_LEN for k in keys):
        raise ConfigError(f"every TASK_KEYS entry must be at least {MIN_KEY_LEN} chars")
    if set(keys) & set(bridge_keys):
        raise ConfigError("TASK_KEYS and BRIDGE_KEYS must not share a key")
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise ConfigError("HERMES_TASK_URL must be http(s)")
    return {"keys": [k.encode() for k in keys], "secret": secret.encode(), "url": url}


class RateLimiter:
    """Global sliding windows [(seconds, limit), ...]; default PER_MINUTE per 60s and PER_DAY per 86400s."""

    def __init__(self, per_minute=PER_MINUTE, per_day=PER_DAY, clock=time.monotonic, windows=None):
        if windows is None:
            windows = [(60.0, per_minute), (86400.0, per_day)]
        self.windows = [(float(span), limit, collections.deque()) for span, limit in windows]
        self.clock = clock
        self.lock = threading.Lock()

    def acquire(self):
        """Return 0 if allowed (and record it), else seconds until retry."""
        with self.lock:
            now = self.clock()
            wait = 0.0
            for span, limit, hits in self.windows:
                while hits and hits[0] <= now - span:
                    hits.popleft()
                if len(hits) >= limit:
                    wait = max(wait, hits[0] + span - now)
            if wait:
                return max(1, int(wait + 0.999))
            for _, _, hits in self.windows:
                hits.append(now)
            return 0


def clean(value):
    return value.translate(_STRIP_TABLE)


def parse_body(raw):
    """Return the normalized payload dict, or None if invalid."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    out = {}
    for name, limit in FIELDS.items():
        value = data.get(name)
        if value is None:
            if name == "message":
                return None
            value = ""
        if not isinstance(value, str):
            return None
        value = clean(value).strip()
        if len(value) > limit:
            return None
        out[name] = value
    if not out["message"]:
        return None
    out["from"] = out["from"] or "bull-desk"
    return out


def parse_task(raw):
    """Return the normalized /task payload dict (without id), or None if invalid."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    out = {}
    for name, limit in TASK_FIELDS.items():
        value = data.get(name)
        if value is None:
            if name != "from":
                return None
            value = ""
        if not isinstance(value, str):
            return None
        value = clean(value).strip()
        if len(value) > limit:
            return None
        out[name] = value
    if not out["title"] or not out["task"]:
        return None
    out["from"] = out["from"] or "bull-desk"
    model = data.get("model")
    if model is None:
        model = "claude"
    if model not in TASK_MODELS:
        return None
    out["model"] = model
    return out


def new_task_id():
    return time.strftime("bd-%Y%m%d-%H%M%S-", time.gmtime()) + secrets.token_hex(2)


def read_result(task_id, results_dir=None):
    """Return the stored status dict for task_id, or None (missing, symlink, not a file, too big, bad JSON)."""
    if not isinstance(task_id, str) or RESULT_ID_RE.fullmatch(task_id) is None:
        return None
    base = os.path.realpath(results_dir or RESULTS_DIR)
    path = os.path.join(base, task_id + ".json")
    if os.path.dirname(os.path.realpath(path)) != base:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_RESULT_FILE:
                return None
            raw = fh.read(MAX_RESULT_FILE + 1)
    except OSError:
        return None
    if len(raw) > MAX_RESULT_FILE:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("id") != task_id:
        return None
    return data


def list_results(results_dir=None):
    """Newest first (ids sort by creation time), at most RESULTS_LIST_MAX, without "files"."""
    try:
        names = os.listdir(results_dir or RESULTS_DIR)
    except OSError:
        return []
    ids = sorted((m.group(1) for m in map(RESULT_FILE_RE.fullmatch, names) if m), reverse=True)
    out = []
    for task_id in ids[:RESULTS_SCAN_MAX]:
        data = read_result(task_id, results_dir)
        if data is not None:
            data.pop("files", None)
            out.append(data)
            if len(out) >= RESULTS_LIST_MAX:
                break
    return out


class SnapshotMissing(Exception):
    pass


class SnapshotBad(Exception):
    pass


def _reject_constant(value):
    raise ValueError("non-finite number")


def _parse_utc(value):
    """Epoch seconds for an ISO-8601 timestamp (naive = UTC); raise SnapshotBad if unparseable."""
    if not isinstance(value, str) or len(value) > 40:
        raise SnapshotBad()
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        when = datetime.datetime.fromisoformat(text)
    except ValueError:
        raise SnapshotBad() from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when.timestamp()


def read_snapshot(name, observe_dir=None, now=None):
    """Return the envelope for an allowlisted name plus "age_s"/"stale".

    Raises SnapshotMissing (unknown name, no file) or SnapshotBad (symlink, not a regular file,
    too big, bad JSON or envelope). The path is built only from OBSERVE_FILES, never from input.
    """
    filename = OBSERVE_FILES.get(name) if isinstance(name, str) else None
    if filename is None:
        raise SnapshotMissing()
    path = os.path.join(observe_dir or OBSERVE_DIR, filename)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise SnapshotMissing() from None
    except OSError:
        raise SnapshotBad() from None
    try:
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_SNAPSHOT_FILE:
                raise SnapshotBad()
            raw = fh.read(MAX_SNAPSHOT_FILE + 1)
    except OSError:
        raise SnapshotBad() from None
    if len(raw) > MAX_SNAPSHOT_FILE:
        raise SnapshotBad()
    try:
        data = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise SnapshotBad() from None
    if not isinstance(data, dict) or any(key not in data for key in OBSERVE_ENVELOPE):
        raise SnapshotBad()
    if (data["name"] != name or not isinstance(data["ok"], bool)
            or not (data["error"] is None or isinstance(data["error"], str))
            or not (data["data"] is None or isinstance(data["data"], dict))):
        raise SnapshotBad()
    age = (time.time() if now is None else now) - _parse_utc(data["generated_at"])
    data["age_s"] = max(0, int(age))
    data["stale"] = data["age_s"] > OBSERVE_STALE_S
    return data


def observe_index(observe_dir=None, now=None):
    """Summary of every allowlisted snapshot that exists; unreadable ones are listed as bad."""
    out = []
    for name in OBSERVE_NAMES:
        try:
            data = read_snapshot(name, observe_dir, now)
        except SnapshotMissing:
            continue
        except SnapshotBad:
            out.append({"name": name, "generated_at": None, "age_s": None, "ok": False, "stale": True,
                        "error": "bad snapshot"})
            continue
        out.append({key: data[key] for key in ("name", "generated_at", "age_s", "ok", "stale")})
    return out


def sign(secret, timestamp, body):
    return hmac.new(secret, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def forward(config, payload):
    body = json.dumps({"title": payload["title"], "message": payload["message"], "from": payload["from"]}).encode()
    return post_signed(config, body)


def forward_task(task_config, task_id, payload):
    body = json.dumps(
        {
            "id": task_id,
            "title": payload["title"],
            "task": payload["task"],
            "model": payload["model"],
            "from": payload["from"],
        }
    ).encode()
    return post_signed(task_config, body)


def post_signed(config, body):
    timestamp = str(int(time.time()))
    req = urllib.request.Request(
        config["url"],
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": timestamp,
            "X-Webhook-Signature-V2": sign(config["secret"], timestamp, body),
        },
    )
    try:
        with _opener.open(req, timeout=UPSTREAM_TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as err:
        err.close()
        return False
    except (urllib.error.URLError, OSError, ValueError):
        return False


def log(status, nbytes, label, extra=""):
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"{ts} status={status} bytes={nbytes} from={label}{extra}", flush=True)


def make_handler(config, limiter, task_limiter, results_limiter, observe_limiter):
    task_config = config.get("task")
    observe_config = config.get("observe")
    post_paths = {"/notify", "/task"} if task_config else {"/notify"}
    get_paths = {"/health", "/results"} if task_config else {"/health"}
    if observe_config:
        get_paths.add("/observe")

    def is_observe(path):
        return bool(observe_config) and (path == "/observe" or path.startswith("/observe/"))

    class Handler(BaseHTTPRequestHandler):
        server_version = "hermes-bridge"
        sys_version = ""
        timeout = 15

        def log_message(self, format, *args):
            pass

        def _send(self, status, body, nbytes=0, label="-", headers=None, extra=""):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)
            log(status, nbytes, label, extra)

        def _path(self):
            return urllib.parse.urlsplit(self.path).path

        def do_GET(self):
            path = self._path()
            if path == "/health":
                self._send(200, {"ok": True})
            elif path == "/results" and task_config:
                self._results()
            elif is_observe(path):
                self._observe(path)
            elif path in post_paths:
                self._send(405, {"ok": False}, headers={"Allow": "POST"})
            else:
                self._send(404, {"ok": False})

        def _authorized(self, keys, extra="", headers=None):
            """True if the Bearer token is one of keys; otherwise send 401/403 and return False."""
            auth = self.headers.get("Authorization", "")
            if not auth:
                self._send(401, {"ok": False}, headers={**(headers or {}), "WWW-Authenticate": "Bearer"}, extra=extra)
                return False
            scheme, _, token = auth.partition(" ")
            token = token.strip().encode()
            matched = False
            for key in keys:
                matched |= hmac.compare_digest(token, key)
            if scheme.lower() != "bearer" or not matched:
                self._send(403, {"ok": False}, headers=headers, extra=extra)
                return False
            return True

        def _read_body(self, max_body, extra=""):
            """Return the raw body, or None after sending 400/411/413."""
            if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                return self._send(411, {"ok": False}, extra=extra)
            length = self.headers.get("Content-Length")
            if length is None:
                return self._send(411, {"ok": False}, extra=extra)
            try:
                length = int(length)
            except ValueError:
                return self._send(400, {"ok": False}, extra=extra)
            if length < 0:
                return self._send(400, {"ok": False}, extra=extra)
            if length > max_body:
                self.close_connection = True
                return self._send(413, {"ok": False}, nbytes=length, extra=extra)
            return self.rfile.read(length)

        def do_POST(self):
            path = self._path()
            if path in get_paths or is_observe(path):
                return self._send(405, {"ok": False}, headers={"Allow": "GET"})
            if path == "/task" and task_config:
                return self._task()
            if path != "/notify":
                return self._send(404, {"ok": False})
            if os.path.exists(KILL_FILE):
                return self._send(503, {"ok": False})
            if not self._authorized(config["keys"]):
                return

            wait = limiter.acquire()
            if wait:
                return self._send(429, {"ok": False}, headers={"Retry-After": str(wait)})

            raw = self._read_body(MAX_BODY)
            if raw is None:
                return
            payload = parse_body(raw)
            if payload is None:
                return self._send(422, {"ok": False}, nbytes=len(raw))
            if forward(config, payload):
                return self._send(202, {"ok": True}, nbytes=len(raw), label=payload["from"])
            return self._send(502, {"ok": False}, nbytes=len(raw), label=payload["from"])

        def _task(self):
            # Task text is never logged: only status, size, label and id.
            extra = " route=task"
            if os.path.exists(KILL_FILE) or os.path.exists(KILL_TASK_FILE):
                return self._send(503, {"ok": False}, extra=extra)
            if not self._authorized(task_config["keys"], extra):
                return

            wait = task_limiter.acquire()
            if wait:
                return self._send(429, {"ok": False}, headers={"Retry-After": str(wait)}, extra=extra)

            raw = self._read_body(MAX_TASK_BODY, extra)
            if raw is None:
                return
            payload = parse_task(raw)
            if payload is None:
                return self._send(422, {"ok": False}, nbytes=len(raw), extra=extra)
            task_id = new_task_id()
            extra += f" id={task_id}"
            if forward_task(task_config, task_id, payload):
                return self._send(202, {"ok": True, "id": task_id}, nbytes=len(raw), label=payload["from"], extra=extra)
            return self._send(502, {"ok": False}, nbytes=len(raw), label=payload["from"], extra=extra)

        def _results(self):
            # Contents are never logged: only status (and the id when it is well-formed).
            extra = " route=results"
            nostore = {"Cache-Control": "no-store"}
            if os.path.exists(KILL_FILE) or os.path.exists(KILL_TASK_FILE):
                return self._send(503, {"ok": False}, headers=nostore, extra=extra)
            if not self._authorized(task_config["keys"], extra):
                return

            wait = results_limiter.acquire()
            if wait:
                return self._send(429, {"ok": False}, headers={**nostore, "Retry-After": str(wait)}, extra=extra)

            query = urllib.parse.urlsplit(self.path).query
            if not query:
                return self._send(200, {"ok": True, "results": list_results()}, headers=nostore, extra=extra)
            params = urllib.parse.parse_qs(query, keep_blank_values=True)
            ids = params.get("id", [])
            if set(params) != {"id"} or len(ids) != 1 or RESULT_ID_RE.fullmatch(ids[0]) is None:
                return self._send(422, {"ok": False}, headers=nostore, extra=extra)
            extra += f" id={ids[0]}"
            data = read_result(ids[0])
            if data is None:
                return self._send(404, {"ok": False}, headers=nostore, extra=extra)
            return self._send(200, {"ok": True, "result": data}, headers=nostore, extra=extra)

        def _observe(self, path):
            # Snapshot contents are never logged: only status and the name when it is allowlisted.
            # Kill switches and auth come before the name check so unauthenticated callers learn nothing.
            extra = " route=observe"
            nostore = {"Cache-Control": "no-store"}
            if os.path.exists(KILL_FILE) or os.path.exists(KILL_OBSERVE_FILE):
                return self._send(503, {"ok": False}, headers=nostore, extra=extra)
            if not self._authorized(observe_config["keys"], extra, headers=nostore):
                return

            wait = observe_limiter.acquire()
            if wait:
                return self._send(429, {"ok": False}, headers={**nostore, "Retry-After": str(wait)}, extra=extra)

            if path == "/observe":
                return self._send(200, {"ok": True, "snapshots": observe_index()}, headers=nostore, extra=extra)
            name = path[len("/observe/"):]
            if name not in OBSERVE_FILES:
                return self._send(404, {"ok": False}, headers=nostore, extra=extra)
            extra += f" name={name}"
            try:
                data = read_snapshot(name)
            except SnapshotMissing:
                return self._send(404, {"ok": False, "error": "no snapshot yet"}, headers=nostore, extra=extra)
            except SnapshotBad:
                return self._send(502, {"ok": False, "error": "bad snapshot"}, headers=nostore, extra=extra)
            return self._send(200, data, headers=nostore, extra=extra)

        def _not_allowed(self):
            path = self._path()
            if path in get_paths or is_observe(path):
                self._send(405, {"ok": False}, headers={"Allow": "GET"})
            elif path in post_paths:
                self._send(405, {"ok": False}, headers={"Allow": "POST"})
            else:
                self._send(404, {"ok": False})

        do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _not_allowed

    return Handler


def make_task_limiter(clock=time.monotonic):
    return RateLimiter(clock=clock, windows=[(3600.0, TASK_PER_HOUR), (86400.0, TASK_PER_DAY)])


def make_results_limiter(clock=time.monotonic):
    return RateLimiter(clock=clock, windows=[(60.0, RESULTS_PER_MINUTE)])


def make_observe_limiter(clock=time.monotonic):
    return RateLimiter(clock=clock, windows=[(60.0, OBSERVE_PER_MINUTE)])


def make_server(config, host=HOST, port=PORT, limiter=None, task_limiter=None, results_limiter=None,
                observe_limiter=None):
    handler = make_handler(config, limiter or RateLimiter(), task_limiter or make_task_limiter(),
                           results_limiter or make_results_limiter(), observe_limiter or make_observe_limiter())
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main():
    try:
        config = load_config(os.environ)
    except ConfigError as exc:
        print(f"hermes-bridge: refusing to start: {exc}", file=sys.stderr, flush=True)
        return 2
    server = make_server(config)
    log("start", 0, "-", " task=" + ("on" if config["task"] else "off")
        + " observe=" + ("on" if config["observe"] else "off"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
