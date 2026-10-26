"""Collector sections. Each module exposes collect(deps) -> data dict (or (data, ok, error) for partial results)."""

import math
import re
from datetime import datetime, timezone

LOG_DIR = "/opt/logs/tba"
_RFC3339 = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)?$")


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_str(value):
    """RFC3339 string (any fraction/offset) -> "YYYY-MM-DDTHH:MM:SSZ"; None if unparseable.
    Drops nanosecond fractions that the 9+ digit scrub rule would otherwise mangle."""
    if not isinstance(value, str):
        return None
    m = _RFC3339.match(value.strip())
    if not m:
        return None
    try:
        dt = datetime.fromisoformat(m.group(1) + (m.group(2) or "Z").replace("Z", "+00:00"))
    except ValueError:
        return None
    return iso(dt.timestamp())


def age(now, epoch):
    return max(0, int(now - epoch))


def fnum(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, str):
        try:
            v = float(v)
        except ValueError:
            return None
    if not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def newest_logs(reader, limit):
    """[(path, mtime)] of regular *.log files directly in LOG_DIR, newest first; symlinks escaping the dir refused."""
    base = reader.realpath(LOG_DIR)
    out = []
    for path in reader.glob(LOG_DIR + "/*.log"):
        real = reader.realpath(path)
        if real.rsplit("/", 1)[0] != base or not reader.isfile_nolink(real):
            continue
        try:
            out.append((path, reader.lstat(real).st_mtime))
        except OSError:
            continue
    out.sort(key=lambda x: -x[1])
    return out[:limit]
