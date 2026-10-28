#!/usr/bin/env python3
"""Load Grok Bull Mentor's knowledge pack into the Bull Desk bot's folder.

Reads two exported streams (`=== FILE: <rel/path> ===` markers), validates
paths, runs a secret guard, and (with --apply) writes an atomic mirror plus
INDEX.md and LOAD_LOG.json. Default is a dry run. Stdlib only.

Env overrides (tests): BD_LOAD_CRED_GLOB = os.pathsep-separated globs of
env files whose values are treated as secrets.
"""
import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import re
import sys

STREAM1 = "/root/grok-bot-instructions/bull-mentor-pack.md"
STREAM2 = "/root/grok-bot-instructions/bull-mentor-pack-extra.md"
DEFAULT_DEST = ("/opt/stacks/bull-desk/data/homes/team-9ad1e3dcb977a710a9d2cdacac6273ec"
                "/bots/cmvojss7oc4d4ttu7i6frmoq5/bull-mentor")
DEFAULT_CRED_GLOBS = ["/root/.hermes/credentials/*.env", "/opt/stacks/bull-desk/.env"]

MARKER_RE = re.compile(r"^=== FILE: (.*) ===$")
PATH_CHARS_RE = re.compile(r"^[A-Za-z0-9._ /-]+$")
PRIVKEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")

MAX_FILES = 2000
MAX_TOTAL_BYTES = 6 * 1024 * 1024
MAX_SEG = 100
MAX_DEPTH = 8
MAX_PATH = 300
MIN_SECRET_LEN = 20
PREAMBLE = "_preamble.md"
RESERVED = {"INDEX.md", "LOAD_LOG.json"}
LISTED_FOLDERS = ("knowledge", "knowledge-drafts", "features")
ENC = ("utf-8", "surrogateescape")  # byte-exact round trip


class Abort(Exception):
    def __init__(self, msg, code=2):
        super().__init__(msg)
        self.code = code


# ---------------------------------------------------------------- parsing

def parse_stream(text):
    """Return list of (path, content_str). Text before first marker -> _preamble.md."""
    out = []
    cur_path, buf = None, []
    for line in text.splitlines(keepends=True):
        m = MARKER_RE.match(line.rstrip("\r\n"))
        if m:
            if cur_path is not None:
                out.append((cur_path, "".join(buf)))
            elif "".join(buf).strip():
                out.append((PREAMBLE, "".join(buf)))
            cur_path, buf = m.group(1), []
        else:
            buf.append(line)
    if cur_path is not None:
        out.append((cur_path, "".join(buf)))
    elif "".join(buf).strip():
        out.append((PREAMBLE, "".join(buf)))
    return out


def check_path(p):
    """Return None if safe, else a reason string."""
    if not p:
        return "empty path"
    if len(p) > MAX_PATH:
        return "path > %d chars" % MAX_PATH
    if not PATH_CHARS_RE.match(p):
        return "bad chars"
    if p.startswith("/"):
        return "absolute path"
    segs = p.split("/")
    for s in segs:
        if s == "":
            return "empty segment"
        if s == ".":
            return "'.' segment"
        if s == "..":
            return "'..' segment"
        if len(s) > MAX_SEG:
            return "segment > %d chars" % MAX_SEG
    if len(segs) > MAX_DEPTH:
        return "depth %d > %d" % (len(segs), MAX_DEPTH)
    base = segs[-1].lower()
    if not any(base.endswith(e) and len(base) > len(e) for e in (".md", ".txt")):
        return "extension not .md/.txt"
    if p in RESERVED:
        return "reserved name"
    return None


def plan(streams):
    """streams: list of (label, text). Return plan dict."""
    merged = {}  # path -> (content_bytes, label)
    collisions, skipped = [], []
    for label, text in streams:
        for path, content in parse_stream(text):
            reason = check_path(path)
            if reason:
                skipped.append({"path": path, "stream": label, "reason": reason})
                continue
            if path in merged:
                collisions.append({"path": path, "winner": label, "loser": merged[path][1]})
                del merged[path]  # re-insert so winner order reflects its stream
            merged[path] = (content.encode(*ENC), label)
    files, total = {}, 0
    for path, (data, label) in merged.items():
        if len(files) >= MAX_FILES:
            skipped.append({"path": path, "stream": label, "reason": "file cap %d" % MAX_FILES})
            continue
        if total + len(data) > MAX_TOTAL_BYTES:
            skipped.append({"path": path, "stream": label, "reason": "size cap %d bytes" % MAX_TOTAL_BYTES})
            continue
        files[path] = (data, label)
        total += len(data)
    return {"files": files, "collisions": collisions, "skipped": skipped, "total_bytes": total}


# ---------------------------------------------------------------- secret guard

def load_secrets(globs):
    """Return (list of (name, value, file), list of unreadable files)."""
    secrets, unreadable, seen = [], [], set()
    for g in globs:
        for f in sorted(glob.glob(g)):
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().splitlines()
            except OSError:
                unreadable.append(f)
                continue
            for line in lines:
                m = ENV_LINE_RE.match(line)
                if not m or line.lstrip().startswith("#"):
                    continue
                name, val = m.group(1), m.group(2).strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1]
                for piece in val.split(","):
                    piece = piece.strip()
                    if len(piece) < MIN_SECRET_LEN or piece.startswith(("http", "/")):
                        continue
                    if piece in seen:
                        continue
                    seen.add(piece)
                    secrets.append((name, piece, f))
    return secrets, unreadable


def secret_hits(files, secrets):
    """files: {path: (bytes, label)}. Return list of (var_name, path) - never values."""
    hits = []
    enc = [(n, v.encode(*ENC)) for n, v, _ in secrets]
    for path, (data, _) in files.items():
        for name, v in enc:
            if v in data:
                hits.append((name, path))
        if PRIVKEY_RE.search(data.decode(*ENC)):
            hits.append(("PRIVATE_KEY_HEADER", path))
    return hits


# ---------------------------------------------------------------- index / log

def top_folder(path):
    return path.split("/", 1)[0] if "/" in path else "(root)"


def first_heading(data):
    text = data.decode("utf-8", "replace")
    first = None
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            h = s.lstrip("#").strip()
            if h:
                return h[:100]
        if first is None:
            first = s
    return (first or "")[:100]


def folder_stats(files):
    stats = {}
    for path, (data, _) in files.items():
        c = stats.setdefault(top_folder(path), [0, 0])
        c[0] += 1
        c[1] += len(data)
    return dict(sorted(stats.items()))


def build_index(files, loaded_at):
    stats = folder_stats(files)
    out = [
        "# Bull Mentor knowledge pack (mirror)",
        "",
        "Mirror of Grok Bull Mentor's `/workspace/bull-mentor`, loaded %s." % loaded_at,
        "Read-only reference for the Bull Desk bot. Source of truth stays with Grok Bull Mentor.",
        "",
        "Redaction note: content was secret-redacted on export. Long file names or links",
        "inside notes may show `[REDACTED-TOKEN]` / `[REDACTED-URL]` - the real file is",
        "usually the one in the same folder with a similar topic.",
        "",
        "`knowledge-drafts/` is DRAFT (not approved doctrine). `_manifest.md` rows show",
        "redacted names; left as is.",
        "",
        "## Folders",
        "",
    ]
    for folder, (n, b) in stats.items():
        tag = " - DRAFT (not approved doctrine)" if folder == "knowledge-drafts" else ""
        out.append("- %s: %d files, %d bytes%s" % (folder, n, b, tag))
    for folder in LISTED_FOLDERS:
        rows = sorted(p for p in files if top_folder(p) == folder)
        if not rows:
            continue
        title = folder + "/"
        if folder == "knowledge-drafts":
            title += " (DRAFT - not approved doctrine)"
        out += ["", "## " + title, ""]
        for p in rows:
            data = files[p][0]
            out.append("- %s - %s - %d bytes" % (p, first_heading(data), len(data)))
    out.append("")
    return "\n".join(out).encode("utf-8")


def build_log(pl, loaded_at, sources):
    return (json.dumps({
        "loaded_at": loaded_at,
        "sources": sources,
        "counts": {
            "files": len(pl["files"]),
            "bytes": pl["total_bytes"],
            "skipped": len(pl["skipped"]),
            "collisions": len(pl["collisions"]),
            "per_folder": {k: {"files": v[0], "bytes": v[1]}
                           for k, v in folder_stats(pl["files"]).items()},
        },
        "skipped": pl["skipped"],
        "collisions": pl["collisions"],
        "files": {p: {"bytes": len(d), "sha256": hashlib.sha256(d).hexdigest(), "stream": lbl}
                  for p, (d, lbl) in sorted(pl["files"].items())},
    }, indent=2) + "\n").encode("utf-8")


# ---------------------------------------------------------------- apply

def _mkdirs_nofollow(root, rel_dir):
    cur = root
    for seg in [s for s in rel_dir.split("/") if s]:
        cur = os.path.join(cur, seg)
        try:
            os.mkdir(cur, 0o755)
        except FileExistsError:
            pass
        if os.path.islink(cur) or not os.path.isdir(cur):
            raise Abort("refusing non-directory/symlink at %s" % cur)
        os.chmod(cur, 0o755)


def _write_file(root, root_real, rel, data):
    _mkdirs_nofollow(root, os.path.dirname(rel))
    full = os.path.join(root, rel)
    real = os.path.realpath(full)
    if os.path.commonpath([root_real, real]) != root_real or real == root_real:
        raise Abort("path escapes dest: %s" % rel)
    fd = os.open(full, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        os.fchmod(fd, 0o644)
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]
    finally:
        os.close(fd)


def _chown_tree(root, uid, gid):
    os.lchown(root, uid, gid)
    for dp, dns, fns in os.walk(root, followlinks=False):
        for n in dns + fns:
            os.lchown(os.path.join(dp, n), uid, gid)


def apply(pl, dest, index_bytes, log_bytes, now):
    dest = os.path.abspath(dest)
    parent = os.path.dirname(dest)
    if not os.path.isdir(parent) or os.path.islink(parent):
        raise Abort("dest parent missing or symlink: %s" % parent)
    if os.path.islink(dest):
        raise Abort("dest is a symlink, refusing: %s" % dest)
    tmp = "%s.tmp.%d" % (dest, os.getpid())
    os.mkdir(tmp, 0o755)
    os.chmod(tmp, 0o755)
    tmp_real = os.path.realpath(tmp)
    all_files = dict((p, d) for p, (d, _) in pl["files"].items())
    all_files["INDEX.md"] = index_bytes
    all_files["LOAD_LOG.json"] = log_bytes
    for rel, data in sorted(all_files.items()):
        _write_file(tmp, tmp_real, rel, data)
    if os.geteuid() == 0:
        st = os.stat(parent)
        _chown_tree(tmp, st.st_uid, st.st_gid)
    old = None
    if os.path.lexists(dest):
        base = "%s.old.%s" % (dest, now.strftime("%Y%m%dT%H%M%SZ"))
        old, i = base, 1
        while os.path.lexists(old):
            old, i = "%s-%d" % (base, i), i + 1
        os.rename(dest, old)
    os.rename(tmp, dest)
    return dest, old


def tree_summary(root):
    nfiles = nbytes = ndirs = 0
    for dp, dns, fns in os.walk(root, followlinks=False):
        ndirs += len(dns)
        for n in fns:
            nfiles += 1
            nbytes += os.lstat(os.path.join(dp, n)).st_size
    return nfiles, ndirs, nbytes


# ---------------------------------------------------------------- main

def read_stream(path):
    if os.path.islink(path):
        raise Abort("input is a symlink, refusing: %s" % path)
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as fh:
        return fh.read()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stream1", default=STREAM1)
    ap.add_argument("--stream2", default=STREAM2)
    ap.add_argument("--dest", default=DEFAULT_DEST)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--allow-var", action="append", default=[], metavar="NAME",
                    help="env var NAME known to be non-secret (e.g. BRAND_NAME); repeatable")
    a = ap.parse_args(argv)
    try:
        return run(a)
    except Abort as e:
        print("ABORT: %s" % e, file=sys.stderr)
        return e.code


def run(a):
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    loaded_at = now.isoformat().replace("+00:00", "Z")
    texts, sources = [], []
    for label, path in (("stream1", a.stream1), ("stream2", a.stream2)):
        t = read_stream(path)
        texts.append((label, t))
        sources.append({"stream": label, "path": path,
                        "sha256": hashlib.sha256(t.encode(*ENC)).hexdigest()})
    pl = plan(texts)

    mode = "APPLY" if a.apply else "DRY RUN"
    print("== load-mentor-pack: %s ==" % mode)
    print("dest: %s" % a.dest)
    for s in sources:
        print("%s: %s" % (s["stream"], s["path"]))
    print("\nper top-level folder:")
    for folder, (n, b) in folder_stats(pl["files"]).items():
        print("  %-28s %5d files %10d bytes" % (folder, n, b))
    print("total: %d files, %d bytes (caps %d files / %d bytes)"
          % (len(pl["files"]), pl["total_bytes"], MAX_FILES, MAX_TOTAL_BYTES))
    print("\ncollisions (%d):" % len(pl["collisions"]))
    for c in pl["collisions"]:
        print("  %s  (%s wins over %s)" % (c["path"], c["winner"], c["loser"]))
    print("\nskipped (%d):" % len(pl["skipped"]))
    for s in pl["skipped"]:
        print("  [%s] %s  -- %s" % (s["stream"], s["path"], s["reason"]))

    globs = os.environ.get("BD_LOAD_CRED_GLOB")
    globs = globs.split(os.pathsep) if globs else DEFAULT_CRED_GLOBS
    secrets, unreadable = load_secrets(globs)
    allowed = set(a.allow_var)
    secrets = [s for s in secrets if s[0] not in allowed]
    cred_files = sorted({f for _, _, f in secrets})
    print("\nsecret guard: %d candidate values from %d env files" % (len(secrets), len(cred_files)))
    if allowed:
        print("  allow-listed (not checked): %s" % ", ".join(sorted(allowed)))
    for f in unreadable:
        print("  WARN unreadable cred file: %s" % f)
    if unreadable and a.apply:
        raise Abort("cannot read cred files; refusing to apply", 3)
    hits = secret_hits(pl["files"], secrets)
    if hits:
        for name, path in hits:
            print("  SECRET MATCH: %s in %s" % (name, path))
        raise Abort("secret guard tripped (%d hits); nothing written" % len(hits), 3)
    print("  clean")

    index_bytes = build_index(pl["files"], loaded_at)
    log_bytes = build_log(pl, loaded_at, sources)
    print("\nINDEX.md preview: %d bytes, %d lines; LOAD_LOG.json: %d bytes"
          % (len(index_bytes), index_bytes.count(b"\n"), len(log_bytes)))

    if not a.apply:
        print("\ndry run - nothing written. re-run with --apply to write.")
        return 0
    dest, old = apply(pl, a.dest, index_bytes, log_bytes, now)
    if old:
        print("\nprevious dest kept at: %s" % old)
    nf, nd, nb = tree_summary(dest)
    print("written: %s -> %d files, %d dirs, %d bytes" % (dest, nf, nd, nb))
    return 0


if __name__ == "__main__":
    sys.exit(main())
