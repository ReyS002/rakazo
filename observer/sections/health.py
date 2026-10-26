"""health: containers, selected systemd units, load/mem/swap/disk/uptime, cron log freshness."""

import deps as d
import scrub
from sections import age, iso_str, newest_logs

FIXED_UNITS = ("hermes-gateway.service", "hermes-robinhood-bridge.service", "docker.service", "ssh.service",
               "cron.service")
UNIT_PATTERNS = ("bull-*", "tba-*")
MAX_CONTAINERS = 100
MAX_CRON_FILES = 20


def container_names(runner):
    rc, out, _ = runner(d.DOCKER_PS, timeout=10)
    if rc != 0:
        raise RuntimeError("docker ps failed")
    return [n for n in out.split() if d.CONTAINER_RE.fullmatch(n)][:MAX_CONTAINERS]


def containers(runner):
    names = container_names(runner)
    if not names:
        return []
    rc, out, _ = runner(d.DOCKER_INSPECT + tuple(names), timeout=10)
    if rc != 0 and not out.strip():
        raise RuntimeError("docker inspect failed")
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 6:
            continue
        name, image, status, health, started, restarts = parts
        rows.append({
            "name": name.lstrip("/"),
            "image": image,
            "status": status,
            "health": health or None,
            "started_at": iso_str(started),
            "restart_count": int(restarts) if restarts.isdigit() else None,
        })
    return rows


def units(runner):
    rc, out, _ = runner(d.SYSTEMCTL_SHOW + FIXED_UNITS + UNIT_PATTERNS, timeout=5)
    rows = []
    for block in out.split("\n\n"):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        uid = props.get("Id")
        if not uid:
            continue
        if props.get("LoadState") == "not-found" and uid not in FIXED_UNITS:
            continue
        name = uid[:-8] if uid.endswith(".service") else uid
        rows.append({"name": name, "active": props.get("ActiveState"), "sub": props.get("SubState")})
    return rows


def _pct(used, total):
    return round(100.0 * used / total, 1) if total else 0.0


def resources(runner, reader):
    res = {}
    try:
        load = reader.read_text("/proc/loadavg", 4096)[0].split()
        res.update(load1=float(load[0]), load5=float(load[1]), load15=float(load[2]))
    except (OSError, ValueError, IndexError):
        res.update(load1=None, load5=None, load15=None)
    try:
        mem = {}
        for line in reader.read_text("/proc/meminfo", 65536)[0].splitlines():
            k, _, v = line.partition(":")
            if v.split():
                mem[k.strip()] = int(v.split()[0])
        total, avail = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
        res["mem_used_pct"] = _pct(total - avail, total)
        stotal, sfree = mem.get("SwapTotal", 0), mem.get("SwapFree", 0)
        res["swap_used_pct"] = _pct(stotal - sfree, stotal)
    except (OSError, ValueError):
        res.update(mem_used_pct=None, swap_used_pct=None)
    disks = []
    rc, out, _ = runner(d.DF, timeout=5)
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 6 and parts[4].endswith("%") and parts[4][:-1].isdigit():
            disks.append({"mount": " ".join(parts[5:]), "used_pct": int(parts[4][:-1])})
    res["disk"] = disks
    try:
        res["uptime_s"] = int(float(reader.read_text("/proc/uptime", 4096)[0].split()[0]))
    except (OSError, ValueError, IndexError):
        res["uptime_s"] = None
    return res


def cron_last(reader, now):
    rows = []
    for path, mtime in newest_logs(reader, MAX_CRON_FILES):
        try:
            lines = reader.tail_lines(reader.realpath(path), 1, 16384)
        except OSError:
            lines = []
        rows.append({
            "job": path.rsplit("/", 1)[1][:-4],
            "last_log_age_s": age(now, mtime),
            "last_line": scrub.clip(lines[-1], 300) if lines else None,
        })
    return rows


def collect(deps):
    now = deps.now()
    errors = []
    data = {}
    for key, fn in (("containers", lambda: containers(deps.runner)),
                    ("units", lambda: units(deps.runner)),
                    ("resources", lambda: resources(deps.runner, deps.reader)),
                    ("cron_last", lambda: cron_last(deps.reader, now))):
        try:
            data[key] = fn()
        except Exception as e:
            data[key] = [] if key != "resources" else {}
            errors.append(f"{key}: {type(e).__name__}")
    if errors:
        return data, len(errors) < 4, "; ".join(errors)
    return data
