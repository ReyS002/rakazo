"""journals: paper fills + decision journals (today and the previous weekday, ET) for Bull Mentor's rule check.

Sources (/opt/arena/state/exports, written by the box's own GET-only exporters):
  paper-fills-<YYYYMMDD>.json      schema 2.0, one block per paper account (fills, stop_orders, open_positions,
                                   bars_2m). Written ~16:23 ET on weekdays.
  <bot>-decisions-<YYYYMMDD>.json  per-bot decision journals (today: unchained-decisions, written 04:10 ET the
                                   next day). Any other `<bot>-decisions-*.json` with the same shape is included.
  bot SQLite `decisions` tables    Velez Trading (TYWQ), Bull Pilot (JI68), Velez swing (RH5Y): DB_SOURCES. Opened
                                   in place, file:...?mode=ro + query_only, busy timeout BUSY_TIMEOUT_S, a progress
                                   handler stops each query after QUERY_BUDGET_S. Never copied. A WAL-mode db is only
                                   opened while the writer's own -wal and -shm exist (a mode=ro open would create
                                   them); otherwise ok:false. Only the small columns in DB_COLS are selected, never
                                   order_payload_json / broker_response_json / snapshot_json. No db or no table ->
                                   `missing` entry; locked / timed out / malformed / bad columns -> ok:false block.
                                   Skipped for a bot whose `<bot>-decisions-*.json` export exists that day.
Dropped: bars_2m / bars_2m_meta entirely, account keys/ids/numbers, order ids, client order ids.
Bots are keyed by the export's own label ("Unchained", "Bull Pilot", ...); each entry also carries the stable
4-char bot `code` from BOTS (the one mapping table; unknown labels -> code null).
Every list keeps up to its cap (decisions MAX_DECISIONS, others MAX_ROWS); `lists.<name>` records
total_rows / kept_rows / truncated. data.catalog has per day and bot the kept-row counts and the decision time
range (ET) so a reader (the relay's /observe/journals slicer) can plan slices.
"""

import re
import sqlite3
import time as clock
import urllib.parse
from datetime import datetime, time, timedelta, timezone

import scrub
from sections import ET, SourceMissing, fnum, iso, iso_str, read_json

EXPORTS = "/opt/arena/state/exports"
MAX_READ = 16 << 20
MAX_ROWS = 1000           # fills, stop_orders, open_positions, closed_trades per bot-day
MAX_DECISIONS = 3000      # decisions per bot-day (a full Unchained day is ~2000)
TEXT_MAX = 160
AFTER_CLOSE = time(16, 30)
DECISIONS_RE = re.compile(r"^([a-z][a-z0-9-]{0,40})-decisions-(\d{8})\.json$")
LABEL_RE = re.compile(r"[A-Za-z0-9 _.-]{1,40}")
TAKEN = ("taken", "approval_needed", "error", "errors", "rejected")
CATALOG_LISTS = ("fills", "stop_orders", "open_positions", "closed_trades", "decisions")

# (decisions key, bot code, db path). Same `decisions` schema in all three (journal_store.py record_decision).
DB_SOURCES = (
    ("velez-trading", "TYWQ", "/opt/stacks/velez-trading-bot/data/trading_bull_desk.sqlite3"),
    ("bull-pilot", "JI68", "/opt/stacks/bull-pilot/data/trading_bull_desk.sqlite3"),
    ("velez-swing", "RH5Y", "/opt/stacks/velez-swing-bot/data/trading_bull_desk.sqlite3"),
)
DB_COLS = ("id", "timestamp", "status", "reason", "symbol", "side", "play", "qty", "order_type", "entry_price",
           "stop_price", "take_profit_price", "timeframe", "location", "max_dollar_risk")
DB_QUERY = (f"select {', '.join(DB_COLS)} from decisions where timestamp >= ? and timestamp < ? "
            "order by timestamp")
BUSY_TIMEOUT_S = 3.0
QUERY_BUDGET_S = 8.0
DB_REASON_MAX = 300
SQLITE_MAGIC = b"SQLite format 3\x00"

# The only bot-identity table: (label, code, aliases). Matched on the lowercase alphanumerics of a
# paper-fills label or a `<bot>-decisions-*.json` prefix.
BOTS = (
    ("Unchained", "PYPV", ("unchained",)),
    ("Bull Pilot", "JI68", ()),
    ("Velez Trading", "TYWQ", ()),
    ("Swing", "RH5Y", ("velez swing",)),
    ("BullMatrix", "7E3U", ("bull-swarm",)),
)


def _norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower()) if isinstance(name, str) else ""


_BOT_INDEX = {_norm(a): (label, code) for label, code, aliases in BOTS for a in (label, *aliases)}


def bot_identity(name):
    """(canonical label, code) for a paper-fills label or decisions file prefix; (None, None) if unknown."""
    return _BOT_INDEX.get(_norm(name), (None, None))


def _t(v):
    return scrub.clip(v, TEXT_MAX) if isinstance(v, str) else None


def prev_weekday(d):
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


# ── paper fills ───────────────────────────────────────────────────────────

def fill_row(f):
    return {"time": iso_str(f.get("time_et")), "symbol": _t(f.get("symbol")), "side": _t(f.get("side")),
            "qty": fnum(f.get("qty")), "price": fnum(f.get("price")), "fill_type": _t(f.get("fill_type")),
            "position_after": fnum(f.get("position_after"))}


def stop_row(o):
    return {"symbol": _t(o.get("symbol")), "side": _t(o.get("side")), "type": _t(o.get("type")),
            "qty": fnum(o.get("qty")), "stop_price": fnum(o.get("stop_price")),
            "limit_price": fnum(o.get("limit_price")), "trail_price": fnum(o.get("trail_price")),
            "trail_percent": fnum(o.get("trail_percent")), "status": _t(o.get("status")),
            "time_in_force": _t(o.get("time_in_force")), "submitted": iso_str(o.get("submitted_et")),
            "filled": iso_str(o.get("filled_et")), "cancelled": iso_str(o.get("cancelled_et")),
            "filled_avg_price": fnum(o.get("filled_avg_price"))}


def position_row(p):
    return {"symbol": _t(p.get("symbol")), "side": _t(p.get("side")), "qty": fnum(p.get("qty")),
            "avg_entry_price": fnum(p.get("avg_entry_price")), "current_price": fnum(p.get("current_price")),
            "unrealized_pl": fnum(p.get("unrealized_pl")), "opened_at": iso_str(p.get("opened_at_et"))}


def _meta(total, kept):
    return {"total_rows": total, "kept_rows": kept, "truncated": kept < total}


def _rows(items, fn):
    items = [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []
    return [fn(x) for x in items[:MAX_ROWS]], len(items)


def paper_fills(doc):
    if not isinstance(doc, dict) or not isinstance(doc.get("accounts"), dict):
        raise ValueError("unexpected paper-fills shape (need object with accounts)")
    bots = {}
    for i, acct in enumerate(doc["accounts"].values()):
        if not isinstance(acct, dict):
            continue
        label = acct.get("label") if isinstance(acct.get("label"), str) and LABEL_RE.fullmatch(acct["label"]) \
            else f"bot{i + 1}"
        fills, nf = _rows(acct.get("fills"), fill_row)
        stops, ns = _rows(acct.get("stop_orders"), stop_row)
        pos, npos = _rows(acct.get("open_positions"), position_row)
        bots[label] = {"code": bot_identity(label)[1], "env_stack": _t(acct.get("env_stack")),
                       "fill_count": nf, "fills": fills,
                       "stop_order_count": ns, "stop_orders": stops, "open_position_count": npos,
                       "open_positions": pos, "fill_history_complete": acct.get("fill_history_complete")
                       if isinstance(acct.get("fill_history_complete"), bool) else None,
                       "truncated": nf > MAX_ROWS or ns > MAX_ROWS or npos > MAX_ROWS,
                       "lists": {"fills": _meta(nf, len(fills)), "stop_orders": _meta(ns, len(stops)),
                                 "open_positions": _meta(npos, len(pos))}}
    missing = doc.get("missing_accounts")
    return {"schema_version": _t(doc.get("schema_version")), "generated_at": iso_str(doc.get("generated_et")),
            "positions_as_of": iso_str(doc.get("positions_as_of_et")),
            "missing_accounts_count": len(missing) if isinstance(missing, list) else None, "bots": bots}


# ── decision journals ─────────────────────────────────────────────────────

def decision_row(r):
    return {"time": iso_str(r.get("time_et")), "symbol": _t(r.get("symbol")), "setup": _t(r.get("setup")),
            "timeframe": _t(r.get("timeframe")), "side": _t(r.get("side")), "decision": _t(r.get("decision")),
            "reason": _t(r.get("reason")), "entry": fnum(r.get("entry")), "stop": fnum(r.get("stop")),
            "qty": fnum(r.get("qty")), "vol_ratio": fnum(r.get("vol_ratio")), "location": _t(r.get("location"))}


def closed_row(t):
    return {"symbol": _t(t.get("symbol")), "side": _t(t.get("side")), "qty": fnum(t.get("qty")),
            "setup": _t(t.get("setup")), "timeframe": _t(t.get("timeframe")),
            "entry_time": iso_str(t.get("entry_time_et")), "entry_price": fnum(t.get("entry_price")),
            "exit_time": iso_str(t.get("exit_time_et")), "exit_price": fnum(t.get("exit_price")),
            "exit_reason": _t(t.get("exit_reason")), "stop_price": fnum(t.get("stop_price")),
            "pnl_usd": fnum(t.get("pnl_usd")), "r_multiple": fnum(t.get("r_multiple"))}


def _time_key(r):
    return (r["time"] is None, r["time"] or "")


def pick_decisions(rows):
    """Chronological (time ascending, untimed last), all rows up to MAX_DECISIONS. Over the cap every
    non-pass row (taken/approval/error...) is kept and the oldest `passed` rows are dropped first."""
    rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    out = sorted((decision_row(r) for r in rows), key=_time_key)
    if len(out) > MAX_DECISIONS:
        keep = [r for r in out if r["decision"] != "passed"][:MAX_DECISIONS]
        passed = [r for r in out if r["decision"] == "passed"]
        room = MAX_DECISIONS - len(keep)
        out = sorted(keep + (passed[-room:] if room > 0 else []), key=_time_key)
    return out, len(rows)


def _summary(s):
    if not isinstance(s, dict):
        return None
    out = {}
    for k, v in s.items():
        if not isinstance(k, str) or len(k) > 40:
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = fnum(v)
        elif isinstance(v, dict):
            out[k] = {scrub.clip(kk, 60): (fnum(vv) if not isinstance(vv, dict) else
                                           {scrub.clip(a, 40): fnum(b) for a, b in list(vv.items())[:10]})
                      for kk, vv in list(v.items())[:40] if isinstance(kk, str)}
    return out


def decisions(doc):
    if not isinstance(doc, dict) or not isinstance(doc.get("decisions"), list):
        raise ValueError("unexpected decisions shape (need object with decisions list)")
    rows, total = pick_decisions(doc["decisions"])
    closed, nclosed = _rows(doc.get("closed_trades"), closed_row)
    return {"lists": {"decisions": _meta(total, len(rows)), "closed_trades": _meta(nclosed, len(closed))},
            "schema_version": _t(doc.get("schema_version")), "generated_at": iso_str(doc.get("generated_et")),
            "final": doc.get("final") if isinstance(doc.get("final"), bool) else None,
            "summary": _summary(doc.get("summary")), "decisions_total": total, "decisions_shown": len(rows),
            "decisions": rows, "closed_trades_count": nclosed, "closed_trades": closed,
            "errors_count": len(doc["errors"]) if isinstance(doc.get("errors"), list) else None}


# ── bot SQLite decision tables ────────────────────────────────────────────

class DbMissing(Exception):
    """No db / no decisions table: recorded in the day's `missing`, not an error."""


class DbError(Exception):
    """db present but not readable as expected: ok:false block with this reason."""


def et_day_utc(day):
    """[start, end) of the ET calendar day as aware UTC datetimes (23 h / 25 h on DST change days)."""
    start = datetime.combine(day, time(0), ET)
    end = datetime.combine(day + timedelta(days=1), time(0), ET)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _parse_ts(value):
    """Row timestamp (any ISO offset; naive = UTC, journal_store writes utc isoformat) -> aware UTC, else None."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _txt(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(v)
    return _t(v)


def db_row(values, when):
    v = dict(zip(DB_COLS, values))
    return {"time": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "time_et": when.astimezone(ET).strftime("%H:%M:%S"),
            "symbol": _txt(v["symbol"]), "decision": _txt(v["status"]),
            "reason": scrub.clip(v["reason"], DB_REASON_MAX) if isinstance(v["reason"], str) else None,
            "side": _txt(v["side"]), "play": _txt(v["play"]), "qty": fnum(v["qty"]),
            "order_type": _txt(v["order_type"]), "entry": fnum(v["entry_price"]), "stop": fnum(v["stop_price"]),
            "target": fnum(v["take_profit_price"]), "timeframe": _txt(v["timeframe"]),
            "location": _txt(v["location"]), "max_dollar_risk": fnum(v["max_dollar_risk"])}


def open_db(reader, deadline, path):
    """Read-only connection to the db in place. Never copies, never creates -wal/-shm."""
    if not reader.exists(path):
        raise DbMissing("source db not found")
    if not reader.isfile_nolink(path):
        raise DbError("source db is not a regular file")
    try:
        head = reader.read_bytes(path, 100)[0]
    except OSError as e:
        raise DbError(f"source db unreadable: {type(e).__name__}") from None
    if not head:
        raise DbMissing("source db is empty (no decisions table)")
    if len(head) < 100 or not head.startswith(SQLITE_MAGIC):
        raise DbError("source is not a sqlite db")
    if 2 in head[18:20] and not (reader.isfile_nolink(path + "-wal") and reader.isfile_nolink(path + "-shm")):
        raise DbError("WAL db without the writer's -wal/-shm: not opened (a read-only open would create them)")
    uri = "file:" + urllib.parse.quote(reader.host_path(path)) + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=deadline.clip(BUSY_TIMEOUT_S), isolation_level=None)
        con.execute("PRAGMA query_only=1")
    except sqlite3.Error as e:
        raise DbError("open failed: " + scrub.clip(f"{type(e).__name__}: {e}", 120)) from None
    return con


def _budget(con, deadline):
    stop = clock.monotonic() + min(QUERY_BUDGET_S, max(0.1, deadline.remaining()))
    con.set_progress_handler(lambda: 1 if clock.monotonic() > stop else 0, 1000)


def db_decisions(reader, deadline, path, day):
    """Decision rows of one ET day from a bot db, chronological, first MAX_DECISIONS kept.

    The indexed string range is widened by a day each side so rows written with any UTC offset are fetched;
    the exact ET-day cut is then made on the parsed timestamp. Unparseable timestamps are dropped (counted)."""
    start, end = et_day_utc(day)
    lo, hi = (start - timedelta(days=1)).strftime("%Y-%m-%d"), (end + timedelta(days=1)).strftime("%Y-%m-%d")
    con = open_db(reader, deadline, path)
    try:
        try:
            _budget(con, deadline)
            cols = [r[0] for r in con.execute("select name from pragma_table_info('decisions')")]
            if not cols:
                raise DbMissing("no decisions table")
            absent = [c for c in DB_COLS if c not in cols]
            if absent:
                raise DbError("decisions table missing columns: " + ",".join(absent))
            _budget(con, deadline)
            raw = con.execute(DB_QUERY, (lo, hi)).fetchall()
        except sqlite3.OperationalError as e:
            if "interrupt" in str(e):
                raise DbError(f"query exceeded the {QUERY_BUDGET_S:g} s budget") from None
            raise DbError("query failed: " + scrub.clip(f"{type(e).__name__}: {e}", 120)) from None
        except sqlite3.DatabaseError as e:
            raise DbError("source is not a readable sqlite db: " + scrub.clip(str(e), 80)) from None
    finally:
        con.close()
    rows, bad = [], 0
    for r in raw:
        when = _parse_ts(r[1])
        if when is None:
            bad += 1
        elif start <= when < end:
            rows.append((when, r[0] if isinstance(r[0], int) else 0, r))
    rows.sort(key=lambda x: x[:2])
    kept = [db_row(r, when) for when, _, r in rows[:MAX_DECISIONS]]
    return {"source": "sqlite", "lists": {"decisions": _meta(len(rows), len(kept))},
            "day_utc": [start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")],
            "decisions_total": len(rows), "decisions_shown": len(kept), "decisions": kept,
            "unparsed_timestamps": bad}


def _label(code):
    return next(label for label, c, _ in BOTS if c == code)


def read_db_day(reader, deadline, day, out, skip_codes):
    for key, code, path in DB_SOURCES:
        if code in skip_codes:
            continue
        head = {"label": _label(code), "code": code, "path": path}
        try:
            body = db_decisions(reader, deadline, path, day)
        except DbMissing as e:
            out["missing"].append(f"{path}: {e}")
            continue
        except DbError as e:
            out["decisions"][key] = dict(head, ok=False, reason=scrub.clip(str(e), 200), source="sqlite")
            continue
        out["decisions"][key] = dict(head, ok=True, reason=None, **body)


# ── per day ───────────────────────────────────────────────────────────────

def _load(reader, path, shape):
    """(block, missing). Missing file -> (None, True); unreadable/malformed -> ok:false block."""
    try:
        doc, mtime = read_json(reader, path, MAX_READ)
    except SourceMissing as e:
        if str(e) == "file not found":
            return None, True
        return {"ok": False, "reason": f"source {e}", "path": path}, False
    except (ValueError, UnicodeDecodeError):
        return {"ok": False, "reason": "source is not valid JSON", "path": path}, False
    try:
        body = shape(doc)
    except ValueError as e:
        return {"ok": False, "reason": str(e), "path": path, "file_mtime": iso(mtime)}, False
    return dict({"ok": True, "reason": None, "path": path, "file_mtime": iso(mtime)}, **body), False


def read_day(reader, day, deadline):
    ymd = day.strftime("%Y%m%d")
    out = {"date": day.isoformat(), "paper_fills": None, "decisions": {}, "missing": []}
    path = f"{EXPORTS}/paper-fills-{ymd}.json"
    out["paper_fills"], missing = _load(reader, path, paper_fills)
    if missing:
        out["missing"].append(path)
    names = []
    for p in reader.glob(f"{EXPORTS}/*-decisions-{ymd}.json"):
        m = DECISIONS_RE.match(p.rsplit("/", 1)[-1])
        if m and m.group(2) == ymd:
            names.append((m.group(1), p))
    if not any(n == "unchained" for n, _ in names):
        out["missing"].append(f"{EXPORTS}/unchained-decisions-{ymd}.json")
    for bot, p in names:
        block = _load(reader, p, decisions)[0] or {"ok": False, "reason": "source file not found", "path": p}
        label, code = bot_identity(bot)
        out["decisions"][bot] = dict({"label": label or bot, "code": code}, **block)
    read_db_day(reader, deadline, day, out, {bot_identity(n)[1] for n, _ in names})
    return out


# ── catalog ───────────────────────────────────────────────────────────────

def _et(stamp):
    try:
        when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return when.astimezone(ET).isoformat()


def catalog_day(day):
    """Per bot (paper-fills and decisions entries merged by code, else by label): kept-row counts per list,
    the lists truncated at the source, and the first/last decision time in ET."""
    bots = {}

    def entry(label, code):
        key = code or _norm(label)
        if key not in bots:
            bots[key] = {"label": label, "code": code, **dict.fromkeys(CATALOG_LISTS, 0),
                         "truncated": [], "first_time_et": None, "last_time_et": None}
        return bots[key]

    def count(e, block, names):
        for n in names:
            e[n] = len(block[n])
            if block["lists"][n]["truncated"]:
                e["truncated"].append(n)

    pf = day["paper_fills"]
    if pf and pf["ok"]:
        for label, b in pf["bots"].items():
            count(entry(label, b["code"]), b, ("fills", "stop_orders", "open_positions"))
    for b in day["decisions"].values():
        if b["ok"]:
            e = entry(b["label"], b["code"])
            count(e, b, [n for n in ("decisions", "closed_trades") if n in b["lists"]])
            times = [r["time"] for r in b["decisions"] if r["time"]]
            if times:
                e["first_time_et"], e["last_time_et"] = _et(min(times)), _et(max(times))
    return {"date": day["date"], "bots": list(bots.values())}


def collect(deps):
    now = deps.now()
    now_et = datetime.fromtimestamp(now, ET)
    today = now_et.date()
    days = {"today": read_day(deps.reader, today, deps.deadline),
            "prev_weekday": read_day(deps.reader, prev_weekday(today), deps.deadline)}
    errors = []
    for key, d in days.items():
        blocks = [d["paper_fills"]] + list(d["decisions"].values())
        for b in blocks:
            if b and not b["ok"]:
                name = f"{b['code']} decisions db" if b.get("source") == "sqlite" else b["path"].rsplit("/", 1)[-1]
                errors.append(f"{key} {name}: {b['reason']}")
    after_close = today.weekday() < 5 and now_et.time() >= AFTER_CLOSE
    prev_fills_missing = days["prev_weekday"]["paper_fills"] is None
    today_fills_missing = days["today"]["paper_fills"] is None
    stale = prev_fills_missing or (after_close and today_fills_missing)
    data = {"catalog": {k: catalog_day(d) for k, d in days.items()}, "today_et": today.isoformat(), "after_close": after_close, "stale": stale,
            "stale_rule": "prev weekday paper-fills missing, or today's missing after 16:30 ET on a weekday",
            "days": days}
    any_file = any(d["paper_fills"] is not None or d["decisions"] for d in days.values())
    if not any_file:
        return data, False, "no paper-fills or decisions exports for today or the previous weekday"
    if errors:
        return data, True, "; ".join(errors)
    return data
