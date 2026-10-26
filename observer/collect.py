#!/usr/bin/env python3
"""Oversight feed collector: writes sanitized JSON snapshots for the read-only relay.

collect.py [--only health,scanner,...] [--out DIR] [--once]

Read-only: fixed git/docker/systemctl/df argv shapes, GET-only HTTP to paper/practice hosts, file reads.
Every snapshot is scrubbed, re-checked for leaks (dropped to ok:false if any rule still matches),
capped at 200 KB and written atomically (tmp + os.replace, 0644). One lock per section prevents overlap.
"""

import argparse
import fcntl
import json
import os
import signal
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import deps as d  # noqa: E402
import scrub  # noqa: E402
from sections import health, iso, logs, paper, projects, scanner, vault  # noqa: E402

DEFAULT_OUT = "/opt/stacks/bull-desk/hermes-bridge-run/observe"
SECTIONS = {"health": health.collect, "scanner": scanner.collect, "paper": paper.collect,
            "projects": projects.collect, "vault": vault.collect, "logs": logs.collect}
BUDGET_S = 60.0
HARD_STOP_S = 75
MAX_BYTES = 200 * 1024
ERROR_MAX = 200


def short_error(e):
    msg = scrub.clip(str(e).splitlines()[0] if str(e) else "", 120)
    return scrub.clip(type(e).__name__ + (": " + msg if msg else ""), ERROR_MAX)


def _largest_list(obj):
    """(list, len) of the longest list anywhere in obj."""
    best = (obj, len(obj)) if isinstance(obj, list) else (None, 0)
    children = obj.values() if isinstance(obj, dict) else obj if isinstance(obj, list) else ()
    for v in children:
        cand = _largest_list(v)
        if cand[1] > best[1]:
            best = cand
    return best


def encode(env):
    return (json.dumps(env, ensure_ascii=False, separators=(",", ":"), sort_keys=False) + "\n").encode("utf-8")


def fit(env):
    """Halve the longest list until the snapshot fits MAX_BYTES; give up -> ok:false."""
    raw = encode(env)
    for _ in range(64):
        if len(raw) <= MAX_BYTES:
            return raw
        lst, n = _largest_list(env["data"])
        if not lst or n <= 1:
            break
        del lst[max(1, n // 2):]
        raw = encode(env)
    if len(raw) <= MAX_BYTES:
        return raw
    return encode({"name": env["name"], "generated_at": env["generated_at"], "ok": False,
                   "error": "snapshot too large", "data": {}})


def build(name, fn, deps):
    """Envelope bytes for one section. BudgetExceeded propagates (caller skips the write)."""
    generated_at = iso(deps.now())
    try:
        res = fn(deps)
        if isinstance(res, tuple):
            data, ok, error = res
        else:
            data, ok, error = res, True, None
    except Exception as e:
        data, ok, error = {}, False, short_error(e)
    env = {"name": name, "generated_at": generated_at, "ok": bool(ok),
           "error": scrub.clip(error, ERROR_MAX) if error else None,
           "data": scrub.scrub_tree(data)}
    leaks = scrub.find_leaks(env)
    if leaks:
        env = {"name": name, "generated_at": generated_at, "ok": False,
               "error": "scrub check failed: " + ",".join(leaks), "data": {}}
    return fit(env)


def write_atomic(out_dir, name, raw):
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, os.path.join(out_dir, name + ".json"))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(out_dir, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def clean_stale_tmp(out_dir, max_age_s=600):
    """Remove temp files a killed run may have left (relay only serves the six fixed names anyway)."""
    now = time.time()
    for n in os.listdir(out_dir):
        if n.startswith(".") and n.endswith(".tmp"):
            p = os.path.join(out_dir, n)
            try:
                if now - os.lstat(p).st_mtime > max_age_s:
                    os.unlink(p)
            except OSError:
                pass


def lock_section(out_dir, name):
    """Open fd holding an exclusive non-blocking lock, or None if that section is already running."""
    fd = os.open(os.path.join(out_dir, f".{name}.lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def prepare_out(out_dir):
    os.makedirs(out_dir, mode=0o755, exist_ok=True)
    os.chmod(out_dir, 0o755)


def run(names, out_dir, deps, log=print):
    """Collect names into out_dir. Returns {name: "ok"|"error"|"busy"|"skipped"}."""
    prepare_out(out_dir)
    clean_stale_tmp(out_dir)
    result = {}
    for name in names:
        if deps.deadline.expired():
            result[name] = "skipped"
            continue
        fd = lock_section(out_dir, name)
        if fd is None:
            result[name] = "busy"
            continue
        t0 = time.monotonic()
        try:
            raw = build(name, SECTIONS[name], deps)
            write_atomic(out_dir, name, raw)
            result[name] = "ok" if json.loads(raw)["ok"] else "error"
        except d.BudgetExceeded:
            result[name] = "skipped"  # previous snapshot stays; its generated_at shows the age
        finally:
            os.close(fd)
        log(f"observe {name}: {result[name]} {int((time.monotonic() - t0) * 1000)}ms")
    return result


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Oversight feed collector (read-only).")
    ap.add_argument("--only", default=",".join(SECTIONS), help="comma list of: " + ",".join(SECTIONS))
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--once", action="store_true", help="single pass (the only mode; scheduling is systemd's job)")
    args = ap.parse_args(argv)
    names = [n.strip() for n in args.only.split(",") if n.strip()]
    bad = [n for n in names if n not in SECTIONS]
    if bad or not names:
        ap.error("unknown section(s): " + ",".join(bad) if bad else "no sections")
    args.names = list(dict.fromkeys(names))
    return args


def _hard_stop(signum, frame):
    raise d.BudgetExceeded()


def main(argv=None):
    args = parse_args(argv)
    os.umask(0o022)
    signal.signal(signal.SIGALRM, _hard_stop)
    signal.alarm(HARD_STOP_S)
    try:
        run(args.names, args.out, d.real_deps(BUDGET_S))
    except d.BudgetExceeded:
        print("observe: hard stop", file=sys.stderr)
    finally:
        signal.alarm(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
