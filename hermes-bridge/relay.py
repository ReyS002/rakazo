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
the allowlisted file names are ever opened. /observe/journals, /observe/nrs and /observe/bars are sliced: no query
-> a compact index, query -> one bounded page (journals: one bot's list; nrs: one market's section, ?full=1 the
whole ~40 KB snapshot; bars: one symbol/timeframe/day).
/market proxies only allowlisted gateway reads and drill/quiz answer submissions,
with separate keys, limits and kill switch.
"""

import collections
import datetime
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import socket
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
KILL_MARKET_FILE = "/run/bridge/KILL_MARKET"
MAX_MARKET_BODY = 2 * 1024
MAX_MARKET_RESPONSE = 12 * 1024
MARKET_TIMEOUT = 10
MARKET_PER_MINUTE = 30
MARKET_PER_DAY = 600
MARKET_SYMBOL_RE = re.compile(r"[A-Z]{1,5}([.-][A-Z]{1,2})?")
MARKET_SECTOR_RE = re.compile(r"[a-z]+(?:-[a-z]+)*")
MARKET_PARAMS = {
    "quote": {}, "profile": {}, "sector": {},
    "history": {"bars": range(1, 261)},
    "filings": {"forms": "forms", "limit": range(1, 21)},
    "edu/drill": {"symbol": "symbol", "seed": range(0, 2 ** 31), "kind": ("identify", "reclaim"),
                  "difficulty": ("easy", "normal", "hard"), "bars": range(20, 121), "compact": ("1",)},
    "edu/quiz": {"symbol": "symbol", "seed": range(0, 2 ** 31), "kind": ("candle", "ma_context", "next5"),
                 "difficulty": ("easy", "normal", "hard"), "bars": range(20, 121), "compact": ("1",)},
    "edu/brief": {"date": "date", "compact": ("1",)},
    "edu/patterns": {"mark": ("reclaim",), "limit": range(1, 21), "bars": range(20, 121), "compact": ("1",)},
}
# The chat bot must never have to remember these: a 12 KB relay cap cannot carry a verbose drill.
MARKET_EDU_DEFAULTS = {
    "edu/drill": (("bars", "40"), ("compact", "1")),
    "edu/quiz": (("bars", "40"), ("compact", "1")),
    "edu/brief": (("compact", "1"),),
    "edu/patterns": (("compact", "1"),),
}
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
OBSERVE_NAMES = ("health", "scanner", "paper", "projects", "vault", "logs", "narrow", "nrs", "journals", "phasec",
                 "robinhood", "bars")
OBSERVE_FILES = {name: name + ".json" for name in OBSERVE_NAMES}  # the only paths ever opened
OBSERVE_ENVELOPE = ("name", "generated_at", "ok", "error", "data")
OBSERVE_PER_MINUTE = 60
OBSERVE_STALE_S = 600
MAX_SNAPSHOT_FILE = 256 * 1024
# journals and bars are never served whole: see journals_index/journals_slice, bars_index/bars_slice
SNAPSHOT_MAX_BYTES = {"journals": 8 << 20, "bars": 2 << 20}  # bars: parsed ~7x its size; container has 64 MB
JOURNALS_PER_MINUTE = 120                   # own limiter for /observe/journals (index + slices)
JOURNALS_INDEX_MAX = 20 * 1024
JOURNALS_SLICE_MAX = 40 * 1024
JOURNALS_SECTIONS = ("fills", "stop_orders", "open_positions", "closed_trades", "decisions")
JOURNALS_PARAMS = ("day", "bot", "section", "offset", "limit", "before", "after", "status")
JOURNALS_DECISION_PARAMS = ("before", "after", "status")
JOURNALS_DAY_KEYS = ("today", "prev_weekday")
JOURNALS_LIMIT_DEFAULT = 200
JOURNALS_LIMIT_MAX = 500
JOURNALS_STATUS = ("passed", "not_passed", "all")
# Paper-fills lists come from data.days.<day>.paper_fills.bots.<label>, the others from .decisions.<file prefix>.
_PAPER_LISTS = {"fills": "fill_count", "stop_orders": "stop_order_count", "open_positions": "open_position_count"}
_DECISION_LISTS = {"decisions": "decisions_total", "closed_trades": "closed_trades_count"}
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_BOT_RE = re.compile(r"[A-Za-z0-9 _.-]{1,40}")
_SECTION_RE = re.compile(r"[a-z0-9_]{1,20}")
_INT_RE = re.compile(r"[0-9]{1,7}")
_HHMM_RE = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")
_ROW_TIME_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z")
# Defense in depth on top of the collector's scrub: never pass on id/account/key-like fields or nested values.
_ROW_DENY_RE = re.compile(r"(?i)(?:^|_)(?:ids?|account\w*|keys?|tokens?|secrets?|password\w*)(?:$|_)")
JOURNALS_USAGE = (
    "GET /observe/journals?day=<today|prev_weekday|YYYY-MM-DD>&bot=<label or code>&section=<section>"
    "[&offset=N][&limit=N][&before=HH:MM][&after=HH:MM][&status=passed|not_passed|all]. "
    "day, bot and section are required. section: fills, stop_orders, open_positions, closed_trades, decisions. "
    "bot: label (case-insensitive) or code (PYPV Unchained, JI68 Bull Pilot, TYWQ Velez Trading, RH5Y Swing, "
    "7E3U BullMatrix). offset >= 0 (default 0), limit 1..500 (default 200); a reply is capped at 40 KB, so it "
    "may return fewer rows than limit: keep calling with offset=next_offset until next_offset is null. "
    "decisions only: after=HH:MM (ET, inclusive) and before=HH:MM (ET, exclusive) filter by time of day, "
    "status filters passed vs every other decision. Rows are in time order; `time` is UTC, `time_et` the ET clock. "
    "truncated_at_source=true means the "
    "collector kept fewer rows than the export had."
)
SLICE_MAX = 12 * 1024                       # /observe/nrs and /observe/bars replies (index and slices)
_SYMBOL_RE = re.compile(r"[A-Za-z0-9._/-]{1,20}")
NRS_MARKETS = ("stock", "forex", "crypto")
NRS_SECTIONS = ("summary", "recent_triggers", "watchlist", "heartbeats", "new_since_prev")
NRS_LISTS = ("recent_triggers", "watchlist")
NRS_ALIASES = {"triggers": "recent_triggers"}
NRS_PARAMS = ("market", "section", "offset", "limit", "symbol", "since", "full")
NRS_LIMIT_DEFAULT = 50
NRS_LIMIT_MAX = 200
NRS_USAGE = (
    "GET /observe/nrs?market=<stock|forex|crypto>[&section=<section>][&offset=N][&limit=N][&symbol=SYM]"
    "[&since=HH:MM]. section: summary (default), recent_triggers (alias triggers), watchlist, heartbeats, "
    "new_since_prev. offset >= 0, limit 1..200 (default 50); a reply is capped at 12 KB, so keep calling with "
    "offset=next_offset until next_offset is null. symbol= (exact, any case) filters recent_triggers and watchlist; "
    "since=HH:MM keeps recent_triggers fired today (ET) at or after that ET time. Triggers are newest first; "
    "`triggered_at` is UTC, `triggered_at_et` the ET date and clock. ?full=1 returns the whole snapshot (~40 KB)."
)
BARS_TFS = ("2m", "5m")
BARS_DAY_KEYS = ("today", "prev_weekday")
BARS_PARAMS = ("symbol", "tf", "day", "from", "to", "offset", "limit")
BARS_LIMIT_DEFAULT = 200
BARS_LIMIT_MAX = 400
BARS_USAGE = (
    "GET /observe/bars?symbol=SYM[&tf=2m|5m][&day=today|prev_weekday|YYYY-MM-DD][&from=HH:MM][&to=HH:MM]"
    "[&offset=N][&limit=N]. Defaults: tf=5m, day=today. Regular session only (09:30-16:00 ET). Each row is "
    "[t_et, open, high, low, close, volume]; t_et is the ET bar start. from/to are inclusive ET bar starts. "
    "offset >= 0, limit 1..400 (default 200); a reply is capped at 12 KB, so keep calling with "
    "offset=next_offset until next_offset is null."
)

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
        "market": load_market_config(env, keys),
    }


def load_market_config(env, bridge_keys):
    """Return the /market config, or None (route disabled) if MARKET_KEYS is unset."""
    keys = split_keys(env, "MARKET_KEYS")
    if not keys:
        return None
    if any(len(k) < MIN_KEY_LEN for k in keys):
        raise ConfigError(f"every MARKET_KEYS entry must be at least {MIN_KEY_LEN} chars")
    if set(keys) & (set(bridge_keys) | set(split_keys(env, "TASK_KEYS")) | set(split_keys(env, "OBS_KEYS"))):
        raise ConfigError("MARKET_KEYS must not share a key with BRIDGE_KEYS, TASK_KEYS or OBS_KEYS")
    key = env.get("MARKET_UPSTREAM_KEY", "").strip()
    if not key or any(ord(c) < 33 or ord(c) > 126 for c in key) or key in keys:
        raise ConfigError("MARKET_UPSTREAM_KEY must be a separate, nonempty ASCII token")
    url = env.get("MARKET_UPSTREAM_URL", "http://host.docker.internal:8790")
    try:
        parsed = urllib.parse.urlsplit(url)
        valid = (parsed.scheme in ("http", "https") and parsed.hostname and parsed.port != 0
                 and not parsed.username and not parsed.password and parsed.path in ("", "/")
                 and not parsed.query and not parsed.fragment
                 and all(33 <= ord(c) <= 126 for c in url) and "\\" not in url)
    except ValueError:
        valid = False
    if not valid:
        raise ConfigError("MARKET_UPSTREAM_URL must be an http(s) origin without credentials, query or fragment")
    return {"keys": [k.encode() for k in keys], "key": key, "url": url.rstrip("/")}


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
    max_bytes = SNAPSHOT_MAX_BYTES.get(name, MAX_SNAPSHOT_FILE)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise SnapshotMissing() from None
    except OSError:
        raise SnapshotBad() from None
    try:
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
                raise SnapshotBad()
            raw = fh.read(max_bytes + 1)
    except OSError:
        raise SnapshotBad() from None
    if len(raw) > max_bytes:
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


def _head(name, env):
    return {"name": name, "generated_at": env["generated_at"], "age_s": env["age_s"], "stale": env["stale"]}


# ── /observe/journals: compact index + bounded slices ────────────────────

def _nth_sunday(year, month, n):
    first = datetime.date(year, month, 1)
    return first + datetime.timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def utc_to_et(when):
    """Aware UTC datetime -> US Eastern wall clock (rules since 2007: 2nd Sun Mar 07:00Z .. 1st Sun Nov 06:00Z).
    Stdlib only, no tzdata needed in the slim image."""
    start = datetime.datetime.combine(_nth_sunday(when.year, 3, 2), datetime.time(7), datetime.timezone.utc)
    end = datetime.datetime.combine(_nth_sunday(when.year, 11, 1), datetime.time(6), datetime.timezone.utc)
    hours = -4 if start <= when < end else -5
    return when.astimezone(datetime.timezone(datetime.timedelta(hours=hours)))


def _row_et(row):
    """ET datetime of a row's collector "time" ("YYYY-MM-DDTHH:MM:SSZ"), else None."""
    value = row.get("time")
    m = _ROW_TIME_RE.fullmatch(value) if isinstance(value, str) else None
    if m is None:
        return None
    try:
        when = datetime.datetime(*map(int, m.groups()), tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    return utc_to_et(when)


def _clean_row(row):
    return {k: v for k, v in row.items() if isinstance(k, str) and not _ROW_DENY_RE.search(k)
            and (v is None or isinstance(v, (str, int, float, bool)))}


def _out_row(row):
    """A row as served: cleaned, plus "time_et" (HH:MM:SS US Eastern) when it has a collector "time"."""
    out = _clean_row(row)
    when = _row_et(row)
    if when is not None:
        out["time_et"] = when.strftime("%H:%M:%S")
    return out


def _list(value):
    return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else None


def _total(block, section, legacy):
    """Rows the export had for that list (collector `lists` meta, else the legacy count field), or None."""
    meta = block.get("lists") if isinstance(block.get("lists"), dict) else {}
    meta = meta.get(section) if isinstance(meta.get(section), dict) else {}
    for value in (meta.get("total_rows"), block.get(legacy)):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def journal_days(data):
    """[(key, date, day block)] for the snapshot's days (today, prev_weekday) with a valid date."""
    days = data.get("days") if isinstance(data, dict) else None
    out = []
    if isinstance(days, dict):
        for key in JOURNALS_DAY_KEYS:
            day = days.get(key)
            if isinstance(day, dict) and isinstance(day.get("date"), str) and _DATE_RE.fullmatch(day["date"]):
                out.append((key, day["date"], day))
    return out


def journal_bots(day):
    """Bots of one day: paper-fills and decisions entries merged by code (else by lowercase label).

    Each: {"label", "code", "names" (lowercase match keys), "lists": {section: rows or None (no source)},
    "totals": {section: export row count or None}}.
    """
    bots = {}

    def entry(label, code):
        code = code if isinstance(code, str) and _BOT_RE.fullmatch(code) else None
        key = code or label.lower()
        if key not in bots:
            bots[key] = {"label": label, "code": code, "names": set(), "lists": dict.fromkeys(JOURNALS_SECTIONS),
                         "totals": dict.fromkeys(JOURNALS_SECTIONS)}
        bots[key]["names"].add(label.lower())
        if code:
            bots[key]["names"].add(code.lower())
        return bots[key]

    def fill(e, block, sections):
        for section, legacy in sections.items():
            rows = _list(block.get(section))
            if rows is not None:
                e["lists"][section] = rows
                e["totals"][section] = _total(block, section, legacy)

    pf = day.get("paper_fills")
    if isinstance(pf, dict) and pf.get("ok") is True and isinstance(pf.get("bots"), dict):
        for label, block in pf["bots"].items():
            if isinstance(label, str) and _BOT_RE.fullmatch(label) and isinstance(block, dict):
                fill(entry(label, block.get("code")), block, _PAPER_LISTS)
    decisions = day.get("decisions")
    if isinstance(decisions, dict):
        for slug, block in decisions.items():
            if not (isinstance(slug, str) and isinstance(block, dict) and block.get("ok") is True):
                continue
            label = block.get("label") if isinstance(block.get("label"), str) else slug
            if not _BOT_RE.fullmatch(label):
                continue
            e = entry(label, block.get("code"))
            if _BOT_RE.fullmatch(slug):
                e["names"].add(slug.lower())
            fill(e, block, _DECISION_LISTS)
    return list(bots.values())


def _truncated(bot, section):
    rows, total = bot["lists"][section], bot["totals"][section]
    return rows is not None and total is not None and len(rows) < total


def _catalog_bot(bot):
    out = {"label": bot["label"], "code": bot["code"]}
    out.update({s: (len(rows) if rows is not None else None) for s, rows in bot["lists"].items()})
    out["truncated_at_source"] = [s for s in JOURNALS_SECTIONS if _truncated(bot, s)]
    times = [t for t in map(_row_et, bot["lists"]["decisions"] or ()) if t is not None]
    out["first_time_et"] = min(times).isoformat() if times else None
    out["last_time_et"] = max(times).isoformat() if times else None
    return out


def journals_index(env):
    """Compact (< JOURNALS_INDEX_MAX) map of the journals snapshot: what exists, how many rows, how to slice."""
    data = env["data"] if isinstance(env["data"], dict) else {}
    catalog, missing = {}, []
    for key, date, day in journal_days(data):
        catalog[key] = {"date": date, "bots": [_catalog_bot(b) for b in journal_bots(day)]}
        if isinstance(day.get("missing"), list):
            missing += [m[:200] for m in day["missing"][:10] if isinstance(m, str)]
    body = {"ok": env["ok"], **_head("journals", env), "error": env["error"],
            "exports_stale": data.get("stale") if isinstance(data.get("stale"), bool) else None,
            "missing": missing, "catalog": catalog, "usage": JOURNALS_USAGE}
    while len(json.dumps(body)) > JOURNALS_INDEX_MAX:
        biggest = max(catalog.values(), key=lambda d: len(d["bots"]), default=None)
        if biggest and biggest["bots"]:
            biggest["bots"].pop()
            body["catalog_truncated"] = True
        elif body["missing"]:
            body["missing"].pop()
        else:
            break
    return body


class SliceError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


JournalsError = SliceError


def _query_params(query, allowed):
    """{name: value} from a raw query string (each allowed name at most once); raises SliceError(400).
    Never echoes input back in the message."""
    try:
        params = urllib.parse.parse_qs(query, keep_blank_values=True, max_num_fields=len(allowed))
    except ValueError:
        raise SliceError(400, "too many query parameters") from None
    if any(name not in allowed for name in params):
        raise SliceError(400, "unknown query parameter; allowed: " + ", ".join(allowed))
    if any(len(values) != 1 for values in params.values()):
        raise SliceError(400, "each query parameter may appear only once")
    return {name: values[0] for name, values in params.items()}


def _paging(p, out, limit_max):
    """offset/limit from p into out (defaults already in out); raises SliceError(400)."""
    for name, low, high in (("offset", 0, None), ("limit", 1, limit_max)):
        if name in p:
            value = p[name]
            if not _INT_RE.fullmatch(value) or int(value) < low or (high is not None and int(value) > high):
                raise SliceError(400, f"{name} must be an integer >= {low}" + (f" and <= {high}" if high else ""))
            out[name] = int(value)


def _hhmm(p, name):
    m = _HHMM_RE.fullmatch(p[name])
    if m is None:
        raise SliceError(400, f"{name} must be HH:MM (24h, ET)")
    return int(m.group(1)), int(m.group(2))


def _page(body, rows, offset, limit, max_bytes, encode=lambda r: r):
    """Fill body with one page of rows (total/offset/limit/returned/next_offset/rows) whose JSON stays <= max_bytes.
    Always returns at least one row when one exists at offset."""
    total = len(rows)
    body.update(total=total, offset=offset, limit=limit, returned=0, next_offset=10 ** 9, rows=[])
    budget = max_bytes - len(json.dumps(body))
    page = []
    for row in rows[offset:offset + limit]:
        row = encode(row)
        cost = len(json.dumps(row)) + 2
        if page and cost > budget:
            break
        page.append(row)
        budget -= cost
    body.update(rows=page, returned=len(page),
                next_offset=offset + len(page) if offset + len(page) < total and page else None)
    while len(json.dumps(body)) > max_bytes and len(page) > 1:  # belt and braces
        page.pop()
        body.update(returned=len(page), next_offset=offset + len(page))
    return body


def parse_journals_query(query):
    """Validated slice parameters from a raw query string; raises JournalsError(400) on any bad input.
    Never echoes input back in the message."""
    p = _query_params(query, JOURNALS_PARAMS)
    out = {"offset": 0, "limit": JOURNALS_LIMIT_DEFAULT, "before": None, "after": None, "status": "all"}
    day = p.get("day")
    if day is not None and day not in JOURNALS_DAY_KEYS and not _DATE_RE.fullmatch(day):
        raise JournalsError(400, "day must be today, prev_weekday or YYYY-MM-DD")
    bot = p.get("bot")
    if bot is not None and not _BOT_RE.fullmatch(bot):
        raise JournalsError(400, "bot must be a label or code (letters, digits, space . _ -; max 40)")
    section = p.get("section")
    if section is not None and not _SECTION_RE.fullmatch(section):
        raise JournalsError(400, "section must be one of: " + ", ".join(JOURNALS_SECTIONS))
    out.update(day=day, bot=bot, section=section)
    _paging(p, out, JOURNALS_LIMIT_MAX)
    for name in ("before", "after"):
        if name in p:
            out[name] = _hhmm(p, name)
    if "status" in p:
        if p["status"] not in JOURNALS_STATUS:
            raise JournalsError(400, "status must be one of: " + ", ".join(JOURNALS_STATUS))
        out["status"] = p["status"]
    if section is not None and section != "decisions" and any(n in p for n in JOURNALS_DECISION_PARAMS):
        raise JournalsError(400, "before, after and status apply only to section=decisions")
    return out


def _select_rows(rows, q):
    if q["status"] != "all":
        rows = [r for r in rows if (r.get("decision") == "passed") == (q["status"] == "passed")]
    if q["before"] or q["after"]:
        kept = []
        for r in rows:
            t = _row_et(r)
            if t is None:
                continue
            hm = (t.hour, t.minute, t.second)
            if q["after"] and hm < q["after"] + (0,):
                continue
            if q["before"] and hm >= q["before"] + (0,):
                continue
            kept.append(r)
        rows = kept
    return rows


def journals_slice(env, q):
    """One page of one bot's list for one day; the encoded reply stays <= JOURNALS_SLICE_MAX.

    Raises JournalsError(404) (with the valid values) for an absent/unknown day, bot or section.
    """
    days = journal_days(env["data"] if isinstance(env["data"], dict) else {})
    if not days:
        raise JournalsError(404, "no journal days in the snapshot (see GET /observe/journals)")
    valid_days = ", ".join([k for k, _, _ in days] + [d for _, d, _ in days])
    match = [(k, d, day) for k, d, day in days if q["day"] in (k, d)] if q["day"] else []
    if not match:
        raise JournalsError(404, ("unknown day" if q["day"] else "day is required") + "; valid: " + valid_days)
    key, date, day = match[0]
    bots = journal_bots(day)
    names = ", ".join(f"{b['label']} ({b['code']})" if b["code"] else b["label"] for b in bots) or "none"
    found = [b for b in bots if q["bot"] and q["bot"].lower() in b["names"]]
    if not found:
        raise JournalsError(404, ("unknown bot" if q["bot"] else "bot is required")
                            + f" for {date}; valid: {names}")
    bot = found[0]
    if q["section"] not in JOURNALS_SECTIONS:
        raise JournalsError(404, ("unknown section" if q["section"] else "section is required")
                            + "; valid: " + ", ".join(JOURNALS_SECTIONS))
    rows = bot["lists"][q["section"]]
    if rows is None:
        source = "paper-fills export" if q["section"] in _PAPER_LISTS else "decisions export"
        raise JournalsError(404, f"no {source} with {q['section']} for {bot['label']} on {date}")
    body = {"ok": True, **_head("journals", env), "day": date, "day_key": key,
            "bot": {"label": bot["label"], "code": bot["code"]}, "section": q["section"],
            "filters": {"before": "%02d:%02d" % q["before"] if q["before"] else None,
                        "after": "%02d:%02d" % q["after"] if q["after"] else None, "status": q["status"]},
            "truncated_at_source": _truncated(bot, q["section"])}
    return _page(body, _select_rows(rows, q), q["offset"], q["limit"], JOURNALS_SLICE_MAX, _out_row)


# ── /observe/nrs: compact index + bounded slices ─────────────────────────

def _et_stamp(value):
    """ET datetime of a collector timestamp ("YYYY-MM-DDTHH:MM:SSZ"), else None."""
    return _row_et({"time": value})


def _now_et(now=None):
    return utc_to_et(datetime.datetime.fromtimestamp(time.time() if now is None else now, datetime.timezone.utc))


def _scalar(v):
    return v is None or isinstance(v, (str, int, float, bool))


def _nrs_row(row):
    """A watchlist/trigger row as served: scalars and short scalar lists only, no id/account/key-like fields,
    plus triggered_at_et ("YYYY-MM-DD HH:MM" ET) for triggers."""
    out = {k: v for k, v in row.items() if isinstance(k, str) and not _ROW_DENY_RE.search(k)
           and (_scalar(v) or (isinstance(v, list) and all(_scalar(x) for x in v)))}
    when = _et_stamp(row.get("triggered_at"))
    if when is not None:
        out["triggered_at_et"] = when.strftime("%Y-%m-%d %H:%M")
    return out


def _nrs_markets(env):
    data = env["data"] if isinstance(env["data"], dict) else {}
    markets = data.get("markets") if isinstance(data.get("markets"), dict) else {}
    return {m: markets[m] for m in NRS_MARKETS if isinstance(markets.get(m), dict)}


def _nrs_today(rows, today):
    return [r for r in rows if (when := _et_stamp(r.get("triggered_at"))) is not None and when.date() == today]


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def nrs_index(env, now=None):
    """Compact (<= SLICE_MAX) map of the nrs snapshot: markets present, counts, freshness, section sizes."""
    now = time.time() if now is None else now
    today = _now_et(now).date()
    data = env["data"] if isinstance(env["data"], dict) else {}
    markets = {}
    for m, v in _nrs_markets(env).items():
        lists = {s: _list(v.get(s)) or [] for s in NRS_LISTS}
        as_of = v.get("as_of")
        try:
            age = max(0, int(now - _parse_utc(as_of))) if as_of else None
        except SnapshotBad:
            age = None
        nsp = v.get("new_since_prev") if isinstance(v.get("new_since_prev"), dict) else None
        counts = v.get("counts") if isinstance(v.get("counts"), dict) else {}
        markets[m] = {
            "ok": v.get("ok") is True, "reason": v.get("reason") if isinstance(v.get("reason"), str) else None,
            "as_of": as_of if isinstance(as_of, str) else None, "age_s": age,
            "stale": v.get("stale") if isinstance(v.get("stale"), bool) else True,
            "export_error": v.get("export_error") if isinstance(v.get("export_error"), str) else None,
            "watchlist_scan_date": v.get("watchlist_scan_date") if isinstance(v.get("watchlist_scan_date"), str)
            else None,
            "recent_triggers": len(lists["recent_triggers"]), "triggers_today": len(_nrs_today(lists["recent_triggers"],
                                                                                              today)),
            "watchlist": len(lists["watchlist"]),
            "export_counts": {k[:60]: _int(c) for k, c in list(counts.items())[:10] if isinstance(k, str)},
            "new_since_prev": {s: len(nsp[s]) if isinstance(nsp.get(s), list) else None for s in NRS_LISTS}
            if nsp else None,
            "bytes": {s: len(json.dumps(v.get(s))) for s in NRS_SECTIONS if s != "summary"},
        }
    body = {"ok": env["ok"], **_head("nrs", env), "error": env["error"],
            "exports_stale": data.get("stale") if isinstance(data.get("stale"), bool) else None,
            "today_et": today.isoformat(), "markets": markets, "sections": list(NRS_SECTIONS),
            "slice_max_bytes": SLICE_MAX, "usage": NRS_USAGE}
    while len(json.dumps(body)) > SLICE_MAX and any(m["export_counts"] for m in markets.values()):
        for m in markets.values():
            m["export_counts"] = {}
    return body


def parse_nrs_query(query):
    """Validated nrs slice parameters; raises SliceError(400). {"full": True} for ?full=1."""
    p = _query_params(query, NRS_PARAMS)
    if "full" in p:
        if p["full"] != "1" or len(p) != 1:
            raise SliceError(400, "full=1 takes no other parameter")
        return {"full": True}
    out = {"full": False, "offset": 0, "limit": NRS_LIMIT_DEFAULT, "symbol": None, "since": None}
    market = p.get("market")
    if market is not None and market not in NRS_MARKETS:
        raise SliceError(400, "market must be one of: " + ", ".join(NRS_MARKETS))
    section = NRS_ALIASES.get(p.get("section"), p.get("section") or "summary")
    if section not in NRS_SECTIONS:
        raise SliceError(400, "section must be one of: " + ", ".join(NRS_SECTIONS) + " (triggers = recent_triggers)")
    out.update(market=market, section=section)
    _paging(p, out, NRS_LIMIT_MAX)
    if "symbol" in p:
        if not _SYMBOL_RE.fullmatch(p["symbol"]):
            raise SliceError(400, "symbol must be 1-20 of letters, digits . _ / -")
        out["symbol"] = p["symbol"].upper()
    if "since" in p:
        out["since"] = _hhmm(p, "since")
    if section not in NRS_LISTS and any(n in p for n in ("offset", "limit", "symbol")):
        raise SliceError(400, "offset, limit and symbol apply only to section=recent_triggers or watchlist")
    if section != "recent_triggers" and "since" in p:
        raise SliceError(400, "since applies only to section=recent_triggers")
    return out


def nrs_slice(env, q, now=None):
    """One market's section; lists are paged and the encoded reply stays <= SLICE_MAX."""
    markets = _nrs_markets(env)
    if q["market"] not in markets:
        raise SliceError(404, ("unknown market" if q["market"] else "market is required")
                         + "; valid: " + (", ".join(markets) or "none"))
    m, section = markets[q["market"]], q["section"]
    body = {"ok": True, **_head("nrs", env), "market": q["market"], "section": section,
            "as_of": m.get("as_of"), "market_stale": m.get("stale") if isinstance(m.get("stale"), bool) else True}
    if section == "summary":
        body["summary"] = {k: v for k, v in m.items() if k not in NRS_SECTIONS
                           and (_scalar(v) or k == "counts" and isinstance(v, dict))}
        return body
    if section in ("heartbeats", "new_since_prev"):
        value = m.get(section) if isinstance(m.get(section), dict) else None
        body[section] = value
        if value and len(json.dumps(body)) > SLICE_MAX:  # bounded at the collector; trim anyway, never overflow
            body[section] = {k: v[:20] if isinstance(v, list) else v for k, v in value.items()}
            body["truncated"] = True
        return body
    rows = _list(m.get(section)) or []
    if q["symbol"]:
        rows = [r for r in rows if isinstance(r.get("symbol"), str) and r["symbol"].upper() == q["symbol"]]
    if q["since"]:
        today, since = _now_et(now).date(), q["since"] + (0,)
        kept = []
        for r in rows:
            when = _et_stamp(r.get("triggered_at"))
            if when is not None and when.date() == today and (when.hour, when.minute, when.second) >= since:
                kept.append(r)
        rows = kept
    body["filters"] = {"symbol": q["symbol"], "since": "%02d:%02d" % q["since"] if q["since"] else None}
    return _page(body, rows, q["offset"], q["limit"], SLICE_MAX, _nrs_row)


# ── /observe/bars: compact index + bounded slices ────────────────────────

def _bars_days(data):
    days = data.get("days") if isinstance(data.get("days"), dict) else {}
    out = {}
    for key in BARS_DAY_KEYS:
        d = days.get(key)
        if isinstance(d, dict) and isinstance(d.get("date"), str) and _DATE_RE.fullmatch(d["date"]):
            out[key] = d
    return out


def _bars_symbols(data):
    bars = data.get("bars") if isinstance(data.get("bars"), dict) else {}
    return {s: v for s, v in bars.items() if isinstance(s, str) and _SYMBOL_RE.fullmatch(s) and isinstance(v, dict)}


def _bar_rows(value):
    return [r for r in value if isinstance(r, list) and len(r) == 6 and isinstance(r[0], str)
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in r[1:])] \
        if isinstance(value, list) else []


def bars_index(env):
    """Compact (<= SLICE_MAX) map of the bars snapshot: symbols, timeframes, days, per-symbol bar counts."""
    data = env["data"] if isinstance(env["data"], dict) else {}
    days = _bars_days(data)
    counts = {s: {k: {tf: len(_bar_rows((v.get(k) or {}).get(tf))) for tf in BARS_TFS} for k in days}
              for s, v in _bars_symbols(data).items()}

    def strs(key, n=60):
        v = data.get(key)
        return [x for x in v if isinstance(x, str)][:n] if isinstance(v, list) else []

    body = {"ok": env["ok"], **_head("bars", env), "error": env["error"],
            "source": data.get("source") if isinstance(data.get("source"), str) else None,
            "feed": data.get("feed") if isinstance(data.get("feed"), str) else None,
            "as_of": data.get("as_of") if isinstance(data.get("as_of"), str) else None,
            "data_stale": data.get("stale") if isinstance(data.get("stale"), bool) else True,
            "latest_bar_et": data.get("latest_bar_et") if isinstance(data.get("latest_bar_et"), str) else None,
            "timeframes": list(BARS_TFS),
            "days": {k: {"date": d["date"], "session": d.get("session") if isinstance(d.get("session"), str)
                         else None} for k, d in days.items()},
            "symbols": list(counts), "counts": counts,
            "dropped_for_cap": strs("dropped_for_cap"), "dropped_for_size": strs("dropped_for_size"),
            "bar_format": ["t_et", "o", "h", "l", "c", "v"], "slice_max_bytes": SLICE_MAX, "usage": BARS_USAGE}
    while len(json.dumps(body)) > SLICE_MAX and body["counts"]:
        body["counts"] = {}  # symbols list still says what exists; a slice reports its own total
        body["counts_omitted"] = True
    return body


def parse_bars_query(query):
    """Validated bars slice parameters; raises SliceError(400)."""
    p = _query_params(query, BARS_PARAMS)
    out = {"offset": 0, "limit": BARS_LIMIT_DEFAULT, "from": None, "to": None}
    symbol = p.get("symbol")
    if symbol is not None and not _SYMBOL_RE.fullmatch(symbol):
        raise SliceError(400, "symbol must be 1-20 of letters, digits . _ / -")
    tf = p.get("tf", "5m").lower()
    if tf not in BARS_TFS:
        raise SliceError(400, "tf must be one of: " + ", ".join(BARS_TFS))
    day = p.get("day", "today")
    if day not in BARS_DAY_KEYS and not _DATE_RE.fullmatch(day):
        raise SliceError(400, "day must be today, prev_weekday or YYYY-MM-DD")
    out.update(symbol=symbol.upper() if symbol else None, tf=tf, day=day)
    _paging(p, out, BARS_LIMIT_MAX)
    for name in ("from", "to"):
        if name in p:
            out[name] = "%02d:%02d" % _hhmm(p, name)
    if out["from"] and out["to"] and out["from"] > out["to"]:
        raise SliceError(400, "from must be <= to")
    return out


def bars_slice(env, q):
    """One symbol/timeframe/day page of [t_et, o, h, l, c, v] rows; the encoded reply stays <= SLICE_MAX."""
    data = env["data"] if isinstance(env["data"], dict) else {}
    symbols = _bars_symbols(data)
    if q["symbol"] not in symbols:
        raise SliceError(404, ("unknown symbol" if q["symbol"] else "symbol is required")
                         + "; valid: " + (", ".join(list(symbols)[:60]) or "none"))
    days = _bars_days(data)
    match = [(k, d["date"]) for k, d in days.items() if q["day"] in (k, d["date"])]
    if not match:
        raise SliceError(404, "unknown day; valid: " + (", ".join(list(days) + [d["date"] for d in days.values()])
                                                        or "none"))
    key, date = match[0]
    rows = _bar_rows((symbols[q["symbol"]].get(key) or {}).get(q["tf"]))
    rows = [r for r in rows if (not q["from"] or r[0] >= q["from"]) and (not q["to"] or r[0] <= q["to"])]
    body = {"ok": True, **_head("bars", env), "feed": data.get("feed") if isinstance(data.get("feed"), str) else None,
            "data_stale": data.get("stale") if isinstance(data.get("stale"), bool) else True,
            "symbol": q["symbol"], "tf": q["tf"], "day": date, "day_key": key,
            "session": days[key].get("session") if isinstance(days[key].get("session"), str) else None,
            "filters": {"from": q["from"], "to": q["to"]}, "bar_format": ["t_et", "o", "h", "l", "c", "v"]}
    return _page(body, rows, q["offset"], q["limit"], SLICE_MAX)


# name -> (parse_query, index, slice) for the routes that are never served whole by default
SLICED = {"journals": (parse_journals_query, journals_index, journals_slice),
          "nrs": (parse_nrs_query, nrs_index, nrs_slice),
          "bars": (parse_bars_query, bars_index, bars_slice)}


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


def market_target(target, method):
    """Exact allowlisted path and canonical validated query; never use caller input as an origin."""
    if len(target) > 2048 or any(ord(c) < 33 or ord(c) > 126 for c in target) or "#" in target:
        raise SliceError(404, "not_found")
    path, _, query = target.partition("?")
    route = path.removeprefix("/market")
    if not path.startswith("/market/"):
        raise SliceError(404, "not_found")
    if method == "POST":
        if route not in ("/edu/drill/answer", "/edu/quiz/answer"):
            raise SliceError(404, "not_found")
        allowed = {}
    elif method == "GET":
        parts = route.split("/")
        if len(parts) == 3 and parts[1] in ("quote", "history", "profile", "filings"):
            if not MARKET_SYMBOL_RE.fullmatch(parts[2]):
                raise SliceError(404, "not_found")
            endpoint = parts[1]
        elif (len(parts) == 3 and parts[1] == "sector" and len(parts[2]) <= 64
              and MARKET_SECTOR_RE.fullmatch(parts[2])):
            endpoint = "sector"
        elif route in ("/edu/drill", "/edu/quiz", "/edu/brief", "/edu/patterns"):
            endpoint = route[1:]
        else:
            raise SliceError(404, "not_found")
        allowed = MARKET_PARAMS.get(endpoint, {})
    else:
        raise SliceError(404, "not_found")
    try:
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True, strict_parsing=True,
                                      max_num_fields=len(allowed), errors="strict")
    except (ValueError, UnicodeError):
        raise SliceError(400, "invalid_query") from None
    if len(dict(pairs)) != len(pairs):
        raise SliceError(400, "invalid_query")
    for name, value in pairs:
        rule = allowed.get(name)
        if rule is None:
            raise SliceError(400, "invalid_query")
        if rule == "symbol":
            valid = MARKET_SYMBOL_RE.fullmatch(value)
        elif rule == "forms":
            forms = value.split(",")
            valid = len(forms) == len(set(forms)) and set(forms) <= {"8-K", "10-Q", "10-K", "4"}
        elif rule == "date":
            valid = value == "today"
            if not valid and _DATE_RE.fullmatch(value):
                try:
                    today = datetime.datetime.now(datetime.timezone.utc).date()
                    valid = datetime.date(1970, 1, 1) <= datetime.date.fromisoformat(value) <= today
                except ValueError:
                    pass
        elif isinstance(rule, range):
            valid = re.fullmatch(r"[0-9]{1,10}", value) and int(value) in rule
        else:
            valid = value in rule
        if not valid:
            raise SliceError(400, "invalid_query")
    if method == "GET":
        supplied = {name for name, _ in pairs}
        pairs += [(name, value) for name, value in MARKET_EDU_DEFAULTS.get(endpoint, ())
                  if name not in supplied]
    return "/v1" + route + ("?" + urllib.parse.urlencode(pairs) if pairs else "")


def _unique_object(pairs):
    if len(dict(pairs)) != len(pairs):
        raise ValueError("duplicate field")
    return dict(pairs)


def parse_market_answer(raw, route):
    """Validate without rewriting the JSON bytes sent to the gateway."""
    try:
        data = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, RecursionError):
        return False
    if not isinstance(data, dict):
        return False
    ids = ("id", "drill_id") if route == "/v1/edu/drill/answer" else ("id", "drill_id", "quiz_id")
    if not any(set(data) == {name, "answer"} for name in ids):
        return False
    identifier = next(data[name] for name in ids if name in data)
    return (isinstance(identifier, str) and re.fullmatch(r"[A-Za-z0-9_.=-]{1,1536}", identifier) is not None
            and isinstance(data["answer"], str) and 1 <= len(data["answer"].strip()) <= 1000
            and not any(ord(c) < 32 and c not in "\n\t" for c in data["answer"]))


# Bound abandoned DNS/connect workers too; a slow upstream cannot create unbounded threads.
_market_slots = threading.BoundedSemaphore(MARKET_PER_MINUTE)


def forward_market(config, target, raw=None):
    """A total ten-second deadline, capped JSON, no redirects, proxies or upstream error bodies."""
    failure = (502, {"ok": False, "error": "upstream_unavailable"})
    if not _market_slots.acquire(blocking=False):
        return failure
    done = threading.Event()
    cancelled = threading.Event()
    sockets = []
    result = [failure]
    parsed = urllib.parse.urlsplit(config["url"])
    cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    conn = cls(parsed.hostname, parsed.port, timeout=MARKET_TIMEOUT)

    def stop():
        cancelled.set()
        sock = sockets[0] if sockets else conn.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        conn.close()

    def fetch():
        try:
            headers = {"Authorization": "Bearer " + config["key"], "Accept": "application/json"}
            if raw is not None:
                headers["Content-Type"] = "application/json"
            conn.auto_open = 0
            conn.connect()
            sockets.append(conn.sock)  # HTTP/1.0 getresponse() detaches the connection's socket.
            if cancelled.is_set():
                return
            conn.request("POST" if raw is not None else "GET", target, body=raw, headers=headers)
            with conn.getresponse() as response:
                if response.status != 200 or response.getheader("Content-Encoding", "identity") != "identity":
                    return
                body = response.read(MAX_MARKET_RESPONSE + 1)
            if len(body) > MAX_MARKET_RESPONSE:
                result[0] = (413, {"ok": False, "error": "response_too_large"})
                return
            data = json.loads(body.decode("utf-8"), parse_constant=_reject_constant,
                              object_pairs_hook=_unique_object)
            # Treat error envelopes as failures even when upstream incorrectly returns HTTP 200.
            if not isinstance(data, dict) or data.get("ok") is False or data.get("error"):
                return
            encoded = json.dumps(data, allow_nan=False)
            if any(json.dumps(k)[1:-1] in encoded for k in
                   [config["key"], *(key.decode() for key in config["keys"])]):
                return
            if len(encoded.encode()) > MAX_MARKET_RESPONSE:
                result[0] = (413, {"ok": False, "error": "response_too_large"})
                return
            result[0] = (200, data)
        except (OSError, ValueError, UnicodeError, RecursionError, http.client.HTTPException):
            pass
        finally:
            conn.close()
            _market_slots.release()
            done.set()

    threading.Thread(target=fetch, daemon=True).start()
    if not done.wait(MARKET_TIMEOUT):
        stop()
        return failure
    return result[0]


def log(status, nbytes, label, extra=""):
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"{ts} status={status} bytes={nbytes} from={label}{extra}", flush=True)


def make_handler(config, limiter, task_limiter, results_limiter, observe_limiter, journals_limiter, market_limiter):
    task_config = config.get("task")
    observe_config = config.get("observe")
    market_config = config.get("market")
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
            if getattr(self, "market_request", False):
                headers = {**(headers or {}), "Cache-Control": "no-store"}
                extra = " route=market"
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
            if getattr(self, "market_request", False):
                print(f"route=market status={status}", flush=True)
            else:
                log(status, nbytes, label, extra)

        def parse_request(self):
            if not super().parse_request():
                return False
            target = self.requestline.split()[1]
            raw_path = target.partition("?")[0]
            self.market_request = raw_path.startswith("/market") or "/market/" in raw_path
            if self.market_request:
                self.path = target
                if self.command not in ("GET", "POST") or any(ord(c) < 32 for c in self.requestline):
                    self._send(404, {"ok": False})
                    return False
            return True

        def _path(self):
            return urllib.parse.urlsplit(self.path).path

        def do_GET(self):
            if self.market_request:
                return self._market()
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
            if self.market_request:
                return self._market()
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

        def _market(self):
            extra = " route=market"
            if not market_config:
                return self._send(404, {"ok": False}, extra=extra)
            if os.path.exists(KILL_FILE) or os.path.exists(KILL_MARKET_FILE):
                return self._send(503, {"ok": False}, extra=extra)
            if len(self.headers.get_all("Authorization", [])) > 1:
                return self._send(403, {"ok": False}, extra=extra)
            if not self._authorized(market_config["keys"], extra):
                return
            wait = market_limiter.acquire()
            if wait:
                return self._send(429, {"ok": False}, headers={"Retry-After": str(wait)}, extra=extra)
            try:
                target = market_target(self.path, self.command)
            except SliceError as exc:
                return self._send(exc.status, {"ok": False, "error": exc.message}, extra=extra)
            raw = None
            if self.command == "POST":
                if (self.headers.get_content_type() != "application/json"
                        or len(self.headers.get_all("Content-Length", [])) != 1
                        or self.headers.get("Transfer-Encoding") is not None):
                    return self._send(400, {"ok": False}, extra=extra)
                raw = self._read_body(MAX_MARKET_BODY, extra)
                if raw is None:
                    return
                if not parse_market_answer(raw, target):
                    return self._send(422, {"ok": False}, extra=extra)
            status, body = forward_market(market_config, target, raw)
            return self._send(status, body, extra=extra)

        def _observe(self, path):
            # Snapshot contents are never logged: only status and the name when it is allowlisted.
            # Kill switches and auth come before the name check so unauthenticated callers learn nothing.
            extra = " route=observe"
            nostore = {"Cache-Control": "no-store"}
            if os.path.exists(KILL_FILE) or os.path.exists(KILL_OBSERVE_FILE):
                return self._send(503, {"ok": False}, headers=nostore, extra=extra)
            if not self._authorized(observe_config["keys"], extra, headers=nostore):
                return

            wait = (journals_limiter if path == "/observe/journals" else observe_limiter).acquire()
            if wait:
                return self._send(429, {"ok": False}, headers={**nostore, "Retry-After": str(wait)}, extra=extra)

            if path == "/observe":
                return self._send(200, {"ok": True, "snapshots": observe_index()}, headers=nostore, extra=extra)
            name = path[len("/observe/"):]
            if name not in OBSERVE_FILES:
                return self._send(404, {"ok": False}, headers=nostore, extra=extra)
            extra += f" name={name}"
            parse, index, slice_ = SLICED.get(name, (None, None, None))
            query = urllib.parse.urlsplit(self.path).query if parse else ""
            if query:
                try:
                    q = parse(query)
                except SliceError as exc:
                    return self._send(exc.status, {"ok": False, "error": exc.message}, headers=nostore, extra=extra)
            try:
                data = read_snapshot(name)
            except SnapshotMissing:
                return self._send(404, {"ok": False, "error": "no snapshot yet"}, headers=nostore, extra=extra)
            except SnapshotBad:
                return self._send(502, {"ok": False, "error": "bad snapshot"}, headers=nostore, extra=extra)
            if not parse or (query and q.get("full")):
                return self._send(200, data, headers=nostore, extra=extra + (" view=full" if parse else ""))
            if not query:
                return self._send(200, index(data), headers=nostore, extra=extra + " view=index")
            extra += " view=slice"
            try:
                body = slice_(data, q)
            except SliceError as exc:
                return self._send(exc.status, {"ok": False, "error": exc.message}, headers=nostore, extra=extra)
            return self._send(200, body, headers=nostore,
                              extra=extra + (f" section={body['section']}" if "section" in body else f" tf={body['tf']}"))

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


def make_journals_limiter(clock=time.monotonic):
    return RateLimiter(clock=clock, windows=[(60.0, JOURNALS_PER_MINUTE)])


def make_market_limiter(clock=time.monotonic):
    return RateLimiter(per_minute=MARKET_PER_MINUTE, per_day=MARKET_PER_DAY, clock=clock)


def make_server(config, host=HOST, port=PORT, limiter=None, task_limiter=None, results_limiter=None,
                observe_limiter=None, journals_limiter=None, market_limiter=None):
    handler = make_handler(config, limiter or RateLimiter(), task_limiter or make_task_limiter(),
                           results_limiter or make_results_limiter(), observe_limiter or make_observe_limiter(),
                           journals_limiter or make_journals_limiter(), market_limiter or make_market_limiter())
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
        + " observe=" + ("on" if config["observe"] else "off")
        + " market=" + ("on" if config["market"] else "off"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
