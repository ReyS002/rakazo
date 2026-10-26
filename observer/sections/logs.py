"""logs: scrubbed tails of allowlisted logs only (/opt/logs/tba/*.log + docker logs of allowlisted containers)."""

import deps as d
import scrub
from sections import age, newest_logs
from sections.health import container_names

MAX_FILES = 15
TAIL = 40
LINE_MAX = 300
CONTAINER_PREFIXES = ("bull-desk", "tba", "velez", "narrow-range", "oanda")


def _tail(lines):
    # PEM blocks span lines: scrub the joined text, then split and cut each line
    text = scrub.scrub_text("\n".join(lines))
    return [ln[:LINE_MAX] for ln in text.split("\n") if ln.strip()][-TAIL:]


def collect(deps):
    now = deps.now()
    files, errors = [], []
    for path, mtime in newest_logs(deps.reader, MAX_FILES):
        try:
            lines = deps.reader.tail_lines(deps.reader.realpath(path), TAIL)
        except OSError as e:
            errors.append(f"{path.rsplit('/', 1)[1]}: {type(e).__name__}")
            continue
        files.append({"name": "tba/" + path.rsplit("/", 1)[1], "mtime_age_s": age(now, mtime), "tail": _tail(lines)})
    try:
        names = [n for n in container_names(deps.runner) if n.startswith(CONTAINER_PREFIXES)]
    except Exception as e:
        names = []
        errors.append(f"docker: {type(e).__name__}")
    for name in names:
        try:
            rc, out, err = deps.runner(d.DOCKER_LOGS + (name,), timeout=10)
        except Exception as e:
            errors.append(f"docker {name}: {type(e).__name__}")
            continue
        lines = [ln for ln in (out.splitlines() + err.splitlines()) if ln.strip()][-TAIL:]
        files.append({"name": "docker/" + name, "mtime_age_s": None, "tail": _tail(lines)})
    data = {"files": files}
    return (data, True, "; ".join(errors)) if errors else data
