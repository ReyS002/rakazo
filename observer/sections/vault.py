"""vault: markdown page index of the tba-brain vault (title + short scrubbed excerpt, never full contents)."""

import re

import scrub
from sections import iso

VAULT = "/opt/stacks/tba-brain"
MAX_PAGES = 400
MAX_FILE = 300 * 1024
EXCERPT = 200
HEAD_BYTES = 4096
SKIP_RE = re.compile(r"(?i)secret|credential|token|password|\.env")
H1_RE = re.compile(r"^#\s+(.+?)\s*#*\s*$")


def _title_excerpt(text, fallback):
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":  # front matter
        for i, ln in enumerate(lines[1:], 1):
            if ln.strip() == "---":
                lines = lines[i + 1:]
                break
    title, body = None, []
    for ln in lines:
        m = H1_RE.match(ln)
        if title is None and m:
            title = m.group(1)
            continue
        body.append(ln)
    excerpt = " ".join(" ".join(body).split())
    return scrub.clip(title or fallback, 200), scrub.clip(excerpt, EXCERPT)


def collect(deps):
    reader = deps.reader
    if not reader.isdir(VAULT):
        raise FileNotFoundError("vault missing")
    found = []
    for top, dirs, files in reader.walk(VAULT):
        dirs[:] = sorted(x for x in dirs if not x.startswith(".") and not SKIP_RE.search(x))
        for name in files:
            path = f"{top}/{name}"
            rel = path[len(VAULT) + 1:]
            if not name.lower().endswith(".md") or name.startswith(".") or SKIP_RE.search(rel):
                continue
            try:
                st = reader.lstat(path)
            except OSError:
                continue
            if not reader.isfile_nolink(path) or st.st_size > MAX_FILE:
                continue
            found.append((st.st_mtime, path, rel))
    found.sort(key=lambda x: -x[0])
    pages = []
    for mtime, path, rel in found[:MAX_PAGES]:
        try:
            text, _ = reader.read_text(path, HEAD_BYTES)
        except OSError:
            continue
        title, excerpt = _title_excerpt(text, rel.rsplit("/", 1)[-1][:-3])
        pages.append({"path": rel, "title": title, "mtime": iso(mtime), "excerpt": excerpt})
    return {"pages": pages}
