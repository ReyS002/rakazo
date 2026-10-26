"""projects: read-only git summary per repo (fixed argv shapes from deps, 5s timeout each, errors ignored)."""

import deps as d
from sections import age

FIXED_REPOS = ("/opt/scripts/tba", "/root/dendrix-ai", "/root/hermes-ai-router", "/root/rakazo", "/root/trading-bull-ai")
STACKS_GLOB = "/opt/stacks/*"
MAX_DIRTY = 20
TIMEOUT = 5


def repo_paths(reader):
    paths = [FIXED_REPOS[0]] + [p for p in reader.glob(STACKS_GLOB) if reader.isdir(p)] + list(FIXED_REPOS[1:])
    seen, out = set(), []
    for p in paths:
        if p not in seen and reader.exists(p + "/.git") and not reader.islink(p):
            seen.add(p)
            out.append(p)
    return out


def _dirty_name(line):
    name = line[3:]
    if " -> " in name:
        name = name.split(" -> ", 1)[1]
    return name.strip().strip('"')


def repo(runner, reader, path, now):
    cwd = reader.host_path(path)

    def git(argv):
        rc, out, _ = runner(argv, timeout=TIMEOUT, cwd=cwd)
        return out if rc == 0 else None

    row = {"path": path, "branch": None, "head": None, "subject": None, "dirty_count": None, "dirty_files": [],
           "ahead": None, "behind": None, "last_commit_age_s": None}
    out = git(d.GIT_BRANCH)
    if out:
        row["branch"] = out.strip()
    out = git(d.GIT_HEAD)
    if out:
        row["head"] = out.strip()
    out = git(d.GIT_LOG)
    if out and "\x00" in out:
        ct, subject = out.rstrip("\n").split("\x00", 1)
        row["subject"] = subject[:200]
        if ct.isdigit():
            row["last_commit_age_s"] = age(now, int(ct))
    out = git(d.GIT_STATUS)
    if out is not None:
        lines = [ln for ln in out.splitlines() if len(ln) > 3]
        row["dirty_count"] = len(lines)
        row["dirty_files"] = [_dirty_name(ln)[:200] for ln in lines[:MAX_DIRTY]]
    out = git(d.GIT_AHEAD)
    if out:
        parts = out.split()
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            row["behind"], row["ahead"] = int(parts[0]), int(parts[1])
    return row


def collect(deps):
    now = deps.now()
    return {"repos": [repo(deps.runner, deps.reader, p, now) for p in repo_paths(deps.reader)]}
