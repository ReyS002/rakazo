"""Collector sections. Each module exposes collect(deps) -> data dict (or (data, ok, error) for partial results)."""

import json
import math
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
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


STALE_WEEKDAY_S = 26 * 3600


def stale_weekday(epoch, now):
    """True when epoch is missing, or now (ET) is a weekday and epoch is more than 26 h old."""
    if epoch is None:
        return True
    return datetime.fromtimestamp(now, ET).weekday() < 5 and now - epoch > STALE_WEEKDAY_S


def epoch_of(value):
    """ISO string (any offset) -> epoch seconds, else None."""
    s = iso_str(value)
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None


class SourceMissing(Exception):
    """File source absent or refused (message is the snapshot `reason`)."""


def read_json(reader, path, max_bytes, link_dir=None):
    """(obj, mtime) of a JSON file. A symlink is followed only when it resolves to a regular file directly
    inside link_dir (the `-latest.json` pointers). Missing/refused -> SourceMissing; bad JSON -> ValueError."""
    real = path
    if reader.islink(path):
        real = reader.realpath(path)
        if link_dir is None or real.rsplit("/", 1)[0] != reader.realpath(link_dir):
            raise SourceMissing("symlink leaves source dir")
    if not reader.exists(real):
        raise SourceMissing("file not found")
    if not reader.isfile_nolink(real):
        raise SourceMissing("not a regular file")
    data, truncated = reader.read_bytes(real, max_bytes)
    if truncated:
        raise SourceMissing(f"file larger than {max_bytes} bytes")
    return json.loads(data.decode("utf-8")), reader.lstat(real).st_mtime


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
