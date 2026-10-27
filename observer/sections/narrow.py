"""narrow: Bull Pilot narrow-gap ORB shadow lane (r2 v2) for Narrow State Warden. Read-only SQLite.

Opens the lane DB with `file:...?mode=ro&immutable=0` (never writes). If that fails (locked, read-only -shm,
ProtectSystem), copies db + -wal into a private temp dir and reads the copy. Schema is checked first: a missing
DB, missing table or missing column -> ok:false with an explicit `reason`; fields are never guessed.

Meaning (bot/shadow/narrow_gap_long_v2.py): `signals` = per-symbol narrow-gap ORB signals (trigger = opening-range
high, stop = entry - 0.1 ATR, outcome in R), `blocked` = signals refused by the three-finger guardrail,
`heartbeats` = lane state once a minute. The NARROW/WIDE StateMachine (bot/core/state_machine.py, spread of
SMA20/SMA200 over close < 0.004 for narrow_bars = NARROW, > 0.008 = WIDE) is in-memory only and is not in this DB;
`per_symbol_narrow_wide_in_source` says so instead of inventing a state.
"""

import os
import shutil
import sqlite3
import tempfile
import urllib.parse
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import scrub
from sections import fnum, iso_str
from sections.scanner import clean

ET = ZoneInfo("America/New_York")
LANE = "long_narrow_gap_orb_r2_v2"
DB_PATH = f"/opt/stacks/bull-pilot/data/shadow-v2/{LANE}.sqlite3"
MAX_DB_BYTES = 64 << 20
MAX_RECENT = 30
MAX_SYMBOLS = 150
REASON_MAX = 200
BUSY_TIMEOUT_S = 2.0

SIGNAL_COLS = ("setup_id", "symbol", "session_date", "detected_at", "breakout_time", "entry_time", "raw_entry",
               "opening_high", "opening_low", "gap_pct", "opening_range_atr", "relative_volume", "atr14",
               "extension_atr", "extension_side", "guardrail_status", "guardrail_reason", "stop_price", "status",
               "exit_time", "exit_reason", "raw_exit", "normal_net_r", "harsh_net_r")
BLOCKED_COLS = ("setup_id", "symbol", "session_date", "entry_time", "raw_entry", "extension_atr", "reason")
HEARTBEAT_COLS = ("generated_at", "session_date", "state", "market_regime", "eligible_symbols", "new_signals",
                  "new_blocked", "total_signals", "total_closed", "error")
REQUIRED = {"signals": SIGNAL_COLS, "heartbeats": HEARTBEAT_COLS}
OPTIONAL = {"blocked": BLOCKED_COLS}
STATE_COLS = ("narrow_wide", "narrow_state", "narrow_wide_state")

TIME_FIELDS = ("detected_at", "breakout_time", "entry_time", "exit_time", "generated_at")
NUM_FIELDS = ("raw_entry", "opening_high", "opening_low", "gap_pct", "opening_range_atr", "relative_volume", "atr14",
              "extension_atr", "stop_price", "raw_exit", "normal_net_r", "harsh_net_r")
INT_FIELDS = ("eligible_symbols", "new_signals", "new_blocked", "total_signals", "total_closed")
FREE_TEXT = ("guardrail_reason", "reason", "error")


class SourceError(Exception):
    """Reason the source could not be read (message is the snapshot `reason`)."""


# ── reading ────────────────────────────────────────────────────────────────

def _connect_ro(host_path, timeout):
    uri = "file:" + urllib.parse.quote(host_path) + "?mode=ro&immutable=0"
    con = sqlite3.connect(uri, uri=True, timeout=timeout, isolation_level=None)
    con.execute("PRAGMA query_only=1")
    return con


def _copy_source(reader, path, tmp_dir):
    """Copy db + -wal (not -shm) into tmp_dir via the rooted reader; returns the copy's path."""
    dst = os.path.join(tmp_dir, "lane.sqlite3")
    for suffix in ("", "-wal"):
        try:
            data, truncated = reader.read_bytes(path + suffix, MAX_DB_BYTES)
        except FileNotFoundError:
            if suffix:
                continue
            raise
        if truncated:
            raise SourceError("source db too large to copy")
        with open(dst + suffix, "wb") as fh:
            fh.write(data)
    return dst


def open_source(reader, deadline, path=DB_PATH, scratch=None):
    """(connection, read_mode, cleanup). read_mode is "ro" or "copy"."""
    if not reader.exists(path):
        raise SourceError("source db not found")
    if not reader.isfile_nolink(path):
        raise SourceError("source db is not a regular file")
    timeout = deadline.clip(BUSY_TIMEOUT_S)
    # A mode=ro open of a WAL db creates -wal/-shm next to it when they are missing (writer idle):
    # only open in place while the writer's own -wal/-shm exist, otherwise read a copy.
    first = "no -wal/-shm beside db"
    if reader.isfile_nolink(path + "-wal") and reader.isfile_nolink(path + "-shm"):
        con = None
        try:
            con = _connect_ro(reader.host_path(path), timeout)
            con.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return con, "ro", lambda: None
        except sqlite3.DatabaseError as e:
            if con is not None:
                con.close()
            if not isinstance(e, sqlite3.OperationalError):
                raise SourceError("source is not a readable sqlite db") from None
            first = scrub.clip(str(e), 80)
    tmp_dir = tempfile.mkdtemp(prefix="narrow-", dir=scratch)
    cleanup = lambda: shutil.rmtree(tmp_dir, ignore_errors=True)  # noqa: E731
    con = None
    try:
        copy = _copy_source(reader, path, tmp_dir)
        con = sqlite3.connect(copy, timeout=deadline.clip(BUSY_TIMEOUT_S), isolation_level=None)
        con.execute("PRAGMA query_only=1")
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return con, "copy", cleanup
    except BaseException as e:
        if con is not None:
            con.close()
        cleanup()
        if isinstance(e, sqlite3.DatabaseError) and not isinstance(e, sqlite3.OperationalError):
            raise SourceError("source is not a readable sqlite db") from None
        if isinstance(e, (sqlite3.DatabaseError, OSError)):
            raise SourceError(f"source db locked/unreadable ({first}); copy failed: "
                              + scrub.clip(f"{type(e).__name__}: {e}", 80)) from None
        raise


def describe_schema(con):
    """{table: [columns]} for user tables only."""
    out = {}
    rows = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                       "ORDER BY name").fetchall()
    for (name,) in rows:
        cols = con.execute("SELECT name FROM pragma_table_info(?)", (name,)).fetchall()
        out[name] = [c[0] for c in cols]
    return out


def schema_problems(schema):
    problems = []
    for table, cols in REQUIRED.items():
        if table not in schema:
            problems.append(f"missing table {table}")
            continue
        missing = [c for c in cols if c not in schema[table]]
        if missing:
            problems.append(f"{table} missing " + ",".join(missing))
    for table, cols in OPTIONAL.items():
        if table in schema:
            missing = [c for c in cols if c not in schema[table]]
            if missing:
                problems.append(f"{table} missing " + ",".join(missing))
    return problems


# ── shaping ────────────────────────────────────────────────────────────────

def _row(keys, values):
    out = {}
    for k, v in zip(keys, values):
        if k in TIME_FIELDS:
            out[k] = iso_str(v)
        elif k in NUM_FIELDS:
            out[k] = fnum(v)
        elif k in INT_FIELDS:
            out[k] = int(v) if isinstance(v, int) and not isinstance(v, bool) else None
        elif k in FREE_TEXT:
            out[k] = scrub.clip(v, REASON_MAX) if isinstance(v, str) else None
        elif k == "session_date":
            out[k] = v if isinstance(v, str) and _is_date(v) else None
        else:
            out[k] = clean(v)
    return out


def _is_date(s):
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


def signal_view(r):
    """Per-signal fields for the Warden: trigger = opening-range high (breakout level)."""
    return {"symbol": r["symbol"], "as_of": r["detected_at"], "session_date": r["session_date"],
            "setup_id": r["setup_id"], "status": r["status"], "gap_pct": r["gap_pct"],
            "opening_range_atr": r["opening_range_atr"], "relative_volume": r["relative_volume"], "atr14": r["atr14"],
            "trigger": r["opening_high"], "opening_low": r["opening_low"], "breakout_time": r["breakout_time"],
            "entry_time": r["entry_time"], "entry": r["raw_entry"], "stop": r["stop_price"],
            "extension_atr": r["extension_atr"], "extension_side": r["extension_side"],
            "guardrail_status": r["guardrail_status"], "guardrail_reason": r["guardrail_reason"],
            "exit_time": r["exit_time"], "exit_reason": r["exit_reason"], "exit": r["raw_exit"],
            "normal_net_r": r["normal_net_r"], "harsh_net_r": r["harsh_net_r"]}


def blocked_view(r):
    return {"symbol": r["symbol"], "session_date": r["session_date"], "setup_id": r["setup_id"],
            "entry_time": r["entry_time"], "entry": r["raw_entry"], "extension_atr": r["extension_atr"],
            "reason": r["reason"]}


def trading_days_after(start, end):
    """Weekdays d with start < d <= end (no holiday calendar)."""
    n, d = 0, start + timedelta(days=1)
    while d <= end and n < 30:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def is_stale(as_of, now):
    """True when as_of is missing or more than one trading day behind today (ET)."""
    if not as_of:
        return True
    then = datetime.fromisoformat(as_of.replace("Z", "+00:00")).astimezone(ET).date()
    today = datetime.fromtimestamp(now, ET).date()
    return trading_days_after(then, today) > 1


def _counts(con, table, today, week_start):
    q = f'SELECT COUNT(*), SUM(session_date = ?), SUM(session_date >= ?) FROM "{table}"'
    total, t, w = con.execute(q, (today, week_start)).fetchone()
    return {"total": int(total or 0), "today": int(t or 0), "last_7d": int(w or 0)}


def read_lane(con, schema, now):
    today_d = datetime.fromtimestamp(now, ET).date()
    today, week_start = today_d.isoformat(), (today_d - timedelta(days=6)).isoformat()
    has_blocked = "blocked" in schema

    sel = ",".join(SIGNAL_COLS)
    latest = con.execute(
        f"SELECT {sel} FROM signals s WHERE id = (SELECT id FROM signals s2 WHERE s2.symbol = s.symbol "
        f"ORDER BY session_date DESC, detected_at DESC, id DESC LIMIT 1) ORDER BY symbol LIMIT ?",
        (MAX_SYMBOLS,)).fetchall()
    latest_by_symbol = [signal_view(_row(SIGNAL_COLS, r)) for r in latest]

    recent = []
    for r in con.execute(f"SELECT {sel} FROM signals ORDER BY detected_at DESC, id DESC LIMIT ?", (MAX_RECENT,)):
        recent.append(dict(kind="signal", **signal_view(_row(SIGNAL_COLS, r))))
    if has_blocked:
        bsel = ",".join(BLOCKED_COLS)
        for r in con.execute(f"SELECT {bsel} FROM blocked ORDER BY entry_time DESC LIMIT ?", (MAX_RECENT,)):
            recent.append(dict(kind="blocked", **blocked_view(_row(BLOCKED_COLS, r))))
    recent.sort(key=lambda x: x.get("as_of") or x.get("entry_time") or "", reverse=True)

    hb = con.execute(f"SELECT {','.join(HEARTBEAT_COLS)} FROM heartbeats ORDER BY generated_at DESC LIMIT 1"
                     ).fetchone()
    lane_state = _row(HEARTBEAT_COLS, hb) if hb else None

    counts = {"signals": _counts(con, "signals", today, week_start),
              "heartbeats": _counts(con, "heartbeats", today, week_start)}
    if has_blocked:
        counts["blocked"] = _counts(con, "blocked", today, week_start)

    last_signal_at = iso_str(con.execute("SELECT MAX(detected_at) FROM signals").fetchone()[0])
    as_of = (lane_state or {}).get("generated_at") or last_signal_at
    mode = None
    if "value" in schema.get("metadata", ()) and "key" in schema["metadata"]:
        row = con.execute("SELECT value FROM metadata WHERE key = 'execution_mode'").fetchone()
        mode = clean(row[0]) if row else None
    return {"as_of": as_of, "stale": is_stale(as_of, now), "today_et": today,
            "execution_mode": mode, "lane_state": lane_state, "last_signal_at": last_signal_at, "counts": counts,
            "latest_by_symbol": latest_by_symbol, "recent": recent[:MAX_RECENT]}


# ── section entry ──────────────────────────────────────────────────────────

def _fail(source, reason, schema=None):
    reason = scrub.clip(reason, REASON_MAX)
    data = {"ok": False, "reason": reason, "source": source, "as_of": None, "stale": True}
    if schema is not None:
        data["schema"] = schema
    return data, False, reason


def collect(deps, path=DB_PATH, scratch=None):
    source = {"kind": "sqlite", "path": path, "lane": LANE, "read_mode": None}
    try:
        con, mode, cleanup = open_source(deps.reader, deps.deadline, path, scratch)
    except SourceError as e:
        return _fail(source, str(e))
    source["read_mode"] = mode
    try:
        try:
            schema = describe_schema(con)
            problems = schema_problems(schema)
            if problems:
                return _fail(source, "unexpected schema: " + "; ".join(problems), schema)
            body = read_lane(con, schema, deps.now())
        except sqlite3.DatabaseError as e:
            return _fail(source, "query failed: " + scrub.clip(f"{type(e).__name__}: {e}", 120))
    finally:
        con.close()
        cleanup()
    data = {"ok": True, "reason": None, "source": source, "as_of": body.pop("as_of"), "stale": body.pop("stale"),
            "schema": schema,
            "per_symbol_narrow_wide_in_source": any(c in cols for cols in schema.values() for c in STATE_COLS)}
    data.update(body)
    return data
