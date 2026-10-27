"""journals: paper fills + decision journals (today and the previous weekday, ET) for Bull Mentor's rule check.

Sources (/opt/arena/state/exports, written by the box's own GET-only exporters):
  paper-fills-<YYYYMMDD>.json      schema 2.0, one block per paper account (fills, stop_orders, open_positions,
                                   bars_2m). Written ~16:23 ET on weekdays.
  <bot>-decisions-<YYYYMMDD>.json  per-bot decision journals (today: unchained-decisions, written 04:10 ET the
                                   next day). Any other `<bot>-decisions-*.json` with the same shape is included.
Dropped: bars_2m / bars_2m_meta entirely, account keys/ids/numbers, order ids, client order ids.
Bots are keyed by the export's own label ("Unchained", "Bull Pilot", ...), never by account.
"""

import re
from datetime import datetime, time, timedelta

import scrub
from sections import ET, SourceMissing, fnum, iso, iso_str, read_json

EXPORTS = "/opt/arena/state/exports"
MAX_READ = 16 << 20
MAX_ROWS = 150
TEXT_MAX = 160
AFTER_CLOSE = time(16, 30)
DECISIONS_RE = re.compile(r"^([a-z][a-z0-9-]{0,40})-decisions-(\d{8})\.json$")
LABEL_RE = re.compile(r"[A-Za-z0-9 _.-]{1,40}")
TAKEN = ("taken", "approval_needed", "error", "errors", "rejected")


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
        bots[label] = {"env_stack": _t(acct.get("env_stack")), "fill_count": nf, "fills": fills,
                       "stop_order_count": ns, "stop_orders": stops, "open_position_count": npos,
                       "open_positions": pos, "fill_history_complete": acct.get("fill_history_complete")
                       if isinstance(acct.get("fill_history_complete"), bool) else None,
                       "truncated": nf > MAX_ROWS or ns > MAX_ROWS or npos > MAX_ROWS}
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


def pick_decisions(rows):
    """<= MAX_ROWS rows: every non-pass decision first (taken/approval/error...), then the newest passes."""
    rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    keep = [r for r in rows if str(r.get("decision")) != "passed"][:MAX_ROWS]
    passed = [r for r in rows if str(r.get("decision")) == "passed"]
    room = MAX_ROWS - len(keep)
    keep += passed[-room:] if room > 0 else []
    out = [decision_row(r) for r in keep]
    out.sort(key=lambda r: r["time"] or "")
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
    return {"schema_version": _t(doc.get("schema_version")), "generated_at": iso_str(doc.get("generated_et")),
            "final": doc.get("final") if isinstance(doc.get("final"), bool) else None,
            "summary": _summary(doc.get("summary")), "decisions_total": total, "decisions_shown": len(rows),
            "decisions": rows, "closed_trades_count": nclosed, "closed_trades": closed,
            "errors_count": len(doc["errors"]) if isinstance(doc.get("errors"), list) else None}


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


def read_day(reader, day):
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
        out["decisions"][bot], _ = _load(reader, p, decisions)
    return out


def collect(deps):
    now = deps.now()
    now_et = datetime.fromtimestamp(now, ET)
    today = now_et.date()
    days = {"today": read_day(deps.reader, today), "prev_weekday": read_day(deps.reader, prev_weekday(today))}
    errors = []
    for key, d in days.items():
        blocks = [d["paper_fills"]] + list(d["decisions"].values())
        for b in blocks:
            if b and not b["ok"]:
                errors.append(f"{key} {b['path'].rsplit('/', 1)[-1]}: {b['reason']}")
    after_close = today.weekday() < 5 and now_et.time() >= AFTER_CLOSE
    prev_fills_missing = days["prev_weekday"]["paper_fills"] is None
    today_fills_missing = days["today"]["paper_fills"] is None
    stale = prev_fills_missing or (after_close and today_fills_missing)
    data = {"today_et": today.isoformat(), "after_close": after_close, "stale": stale,
            "stale_rule": "prev weekday paper-fills missing, or today's missing after 16:30 ET on a weekday",
            "days": days}
    any_file = any(d["paper_fills"] is not None or d["decisions"] for d in days.values())
    if not any_file:
        return data, False, "no paper-fills or decisions exports for today or the previous weekday"
    if errors:
        return data, True, "; ".join(errors)
    return data
