"""bars: regular-session 2Min/5Min stock bars (today + previous weekday) for symbols the desk just looked at.

Symbols (deduped, newest reference first, capped at MAX_SYMBOLS) come from the snapshots already in the output dir:
today's NRS stock triggers (nrs.json), stock scanner fires of today/the previous weekday (scanner.json), and
symbols of fills, closed trades and decisions with an entry price on those two days (journals.json).
Bars: GET https://data.alpaca.markets/v2/stocks/bars (multi-symbol, paged), feed sip with iex fallback (same
order as /opt/scripts/tba/export_paper_fills.py), adjustment raw, 09:30-16:00 ET only. The paper key pair is
found exactly like the paper section (paper.alpaca_accounts), used only as request headers, never stored.
Rows are [t_et "HH:MM", o, h, l, c, v]. Bars are never invented: a failed request leaves that list empty and
names the failure in `error`.
"""

import json
import re
import urllib.parse
from datetime import datetime, time as dtime, timedelta, timezone

import deps as d
from sections import ET, epoch_of, fnum, iso, paper

DATA = "https://data.alpaca.markets"
SOURCE = "alpaca market data GET /v2/stocks/bars (regular session, adjustment=raw)"
TIMEFRAMES = {"2m": "2Min", "5m": "5Min"}
STEP_S = {"2m": 120, "5m": 300}
FEEDS = ("sip", "iex")
FEED_REFUSED = ("HTTP 403", "HTTP 422")  # plan cannot query that feed (e.g. recent SIP) -> try the next one
OPEN, CLOSE = dtime(9, 30), dtime(16, 0)
MAX_SYMBOLS = 40
MAX_PAGES = 10
PAGE_LIMIT = 10000
TIMEOUT = 15
MAX_BYTES = 1900 * 1024  # collect.SECTION_MAX_BYTES["bars"] is 2 MB; trim here so nothing is cut blindly
STALE_OPEN_S = 20 * 60
STOCK_RE = re.compile(r"[A-Z]{1,5}(?:\.[A-Z]{1,2})?")  # forex pairs (6 letters) and crypto (with /) never match


def prev_weekday(d):
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _utc(day, t):
    return datetime.combine(day, t, ET).astimezone(timezone.utc)


def _z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── symbol selection ──────────────────────────────────────────────────────

def _snapshot_data(deps, name):
    try:
        env = deps.read_prev(name)
    except Exception:
        return None
    return env.get("data") if isinstance(env, dict) and isinstance(env.get("data"), dict) else None


def _et_day(epoch):
    return datetime.fromtimestamp(epoch, ET).date() if epoch is not None else None


def pick_symbols(deps, days):
    """([symbol, ...] newest reference first, {symbol: [sources]}, {input: "ok"|"missing"})."""
    seen = {}  # symbol -> [latest epoch, sources]
    inputs = {}

    def add(symbol, epoch, source):
        if not isinstance(symbol, str) or not STOCK_RE.fullmatch(symbol) or _et_day(epoch) not in days:
            return
        e = seen.setdefault(symbol, [epoch, []])
        e[0] = max(e[0], epoch)
        if source not in e[1]:
            e[1].append(source)

    nrs = _snapshot_data(deps, "nrs")
    inputs["nrs"] = "ok" if nrs else "missing"
    stock = ((nrs or {}).get("markets") or {}).get("stock") or {}
    for r in stock.get("recent_triggers") or ():
        if isinstance(r, dict):
            when = epoch_of(r.get("triggered_at"))
            if _et_day(when) == days[0]:  # today's triggers only
                add(r.get("symbol"), when, "nrs")

    scanner = _snapshot_data(deps, "scanner")
    inputs["scanner"] = "ok" if scanner else "missing"
    for f in (scanner or {}).get("recent_fires") or ():
        if isinstance(f, dict) and f.get("kind") == "stock":
            add(f.get("symbol"), epoch_of(f.get("time")), "scanner")

    journals = _snapshot_data(deps, "journals")
    inputs["journals"] = "ok" if journals else "missing"
    for day in ((journals or {}).get("days") or {}).values():
        if not isinstance(day, dict):
            continue
        pf = day.get("paper_fills") if isinstance(day.get("paper_fills"), dict) else {}
        for block in (pf.get("bots") or {}).values():
            for r in (block.get("fills") or ()) if isinstance(block, dict) else ():
                if isinstance(r, dict):
                    add(r.get("symbol"), epoch_of(r.get("time")), "journals")
        for block in (day.get("decisions") or {}).values():
            if not isinstance(block, dict):
                continue
            for r in block.get("closed_trades") or ():
                if isinstance(r, dict):
                    add(r.get("symbol"), epoch_of(r.get("exit_time")) or epoch_of(r.get("entry_time")), "journals")
            for r in block.get("decisions") or ():
                if isinstance(r, dict) and fnum(r.get("entry")) is not None:
                    add(r.get("symbol"), epoch_of(r.get("time")), "journals")

    order = sorted(seen, key=lambda s: (-seen[s][0], s))
    return order, {s: seen[s][1] for s in order}, inputs


# ── fetch ─────────────────────────────────────────────────────────────────

def alpaca_key(reader):
    """(kid, secret) of the first paper account the paper section would use, else None."""
    for _label, kid, sec, base in paper.alpaca_accounts(reader):
        if paper.alpaca_is_paper(kid, base):
            return kid, sec
    return None


def bar_row(b, day, end, step_s):
    """[HH:MM, o, h, l, c, v] for a complete regular-session bar of `day` (ET) that closed by `end` (epoch)."""
    when = epoch_of(b.get("t")) if isinstance(b, dict) else None
    if when is None or when + step_s > end:
        return None
    t = datetime.fromtimestamp(when, ET)
    if t.date() != day or not OPEN <= t.time() < CLOSE:
        return None
    vals = [fnum(b.get(k)) for k in ("o", "h", "l", "c", "v")]
    if any(v is None for v in vals):
        return None
    return [t.strftime("%H:%M"), *vals[:4], int(vals[4])]


class Fetcher:
    """Multi-symbol paged GET; keeps the first feed the plan accepts for the whole run."""

    def __init__(self, http_get, key):
        self.http_get = http_get
        self.hdr = {"APCA-API-KEY-ID": key[0], "APCA-API-SECRET-KEY": key[1], "Accept": "application/json"}
        self.feeds = list(FEEDS)
        self.feed = None

    def _pages(self, params):
        out, token = {}, None
        for _ in range(MAX_PAGES):
            p = dict(params, **({"page_token": token} if token else {}))
            r = self.http_get(DATA + "/v2/stocks/bars?" + urllib.parse.urlencode(p), self.hdr, TIMEOUT)
            if not isinstance(r, dict):
                raise ValueError("unexpected response shape")
            for sym, bars in (r.get("bars") or {}).items():
                if isinstance(sym, str) and isinstance(bars, list):
                    out.setdefault(sym, []).extend(bars)
            token = r.get("next_page_token")
            if not isinstance(token, str) or not token:
                return out
        return out

    def bars(self, symbols, tf, start, end):
        params = {"symbols": ",".join(symbols), "timeframe": TIMEFRAMES[tf], "start": _z(start), "end": _z(end),
                  "limit": PAGE_LIMIT, "adjustment": "raw", "sort": "asc"}
        while True:
            feed = self.feed or self.feeds[0]
            try:
                res = self._pages(dict(params, feed=feed))
            except d.HttpError as e:
                if self.feed is None and str(e) in FEED_REFUSED and len(self.feeds) > 1:
                    self.feeds.pop(0)
                    continue
                raise
            self.feed = feed
            return res


# ── collect ───────────────────────────────────────────────────────────────

def _session(day, now_et):
    if day.weekday() >= 5:
        return "no_session"
    if day < now_et.date():
        return "closed"
    if now_et.time() < OPEN:
        return "not_started"
    return "open" if now_et.time() < CLOSE else "closed"


def collect(deps):
    now = deps.now()
    now_et = datetime.fromtimestamp(now, ET)
    today, prev = now_et.date(), prev_weekday(now_et.date())
    days = {"today": {"date": today.isoformat(), "session": _session(today, now_et)},
            "prev_weekday": {"date": prev.isoformat(), "session": "closed"}}
    symbols, sources, inputs = pick_symbols(deps, (today, prev))
    dropped_cap = symbols[MAX_SYMBOLS:]
    symbols = symbols[:MAX_SYMBOLS]
    data = {"source": SOURCE, "feed": None, "as_of": iso(now), "stale": True, "error": None, "inputs": inputs,
            "timeframes": list(TIMEFRAMES), "days": days, "symbols": symbols, "symbol_sources": sources,
            "dropped_for_cap": dropped_cap, "dropped_for_size": [], "latest_bar_et": None,
            "bar_format": ["t_et", "o", "h", "l", "c", "v"], "counts": {}, "bars": {}}
    if not symbols:
        data["stale"] = False
        return data
    key = alpaca_key(deps.reader)
    if key is None:
        data["error"] = "no paper Alpaca key in the stack env files"
        return data, False, data["error"]

    fetch = Fetcher(deps.http_get, key)
    bars = {s: {k: {tf: [] for tf in TIMEFRAMES} for k in days} for s in symbols}
    errors, tried, latest = [], 0, None
    for dkey, day in (("today", today), ("prev_weekday", prev)):
        if days[dkey]["session"] in ("no_session", "not_started"):
            continue
        start, end = _utc(day, OPEN), min(_utc(day, CLOSE), datetime.fromtimestamp(now, timezone.utc))
        for tf in TIMEFRAMES:
            tried += 1
            try:
                got = fetch.bars(symbols, tf, start, end)
            except Exception as e:
                errors.append(f"{dkey} {tf}: {type(e).__name__}" + (f" ({e})" if str(e).startswith("HTTP ") else ""))
                continue
            for sym, raw in got.items():
                if sym not in bars:
                    continue
                rows = [r for r in (bar_row(b, day, end.timestamp(), STEP_S[tf]) for b in raw) if r]
                rows.sort(key=lambda r: r[0])
                bars[sym][dkey][tf] = rows
                if dkey == "today" and rows and (latest is None or rows[-1][0] > latest):
                    latest = rows[-1][0]

    data["feed"] = fetch.feed
    data["latest_bar_et"] = f"{today.isoformat()} {latest}" if latest else None
    for s in symbols:
        data["counts"][s] = {k: {tf: len(bars[s][k][tf]) for tf in TIMEFRAMES} for k in days}
    data["bars"] = bars
    _fit(data)

    if days["today"]["session"] == "open":
        if latest is None:
            behind = True
        else:
            hh, mm = map(int, latest.split(":"))
            behind = (now_et - datetime.combine(today, dtime(hh, mm), ET)).total_seconds() > STALE_OPEN_S
    else:
        behind = False
    data["stale"] = bool(errors) or behind
    if not errors:
        return data
    data["error"] = "; ".join(errors)[:200]
    return data, len(errors) < tried, data["error"]


def _fit(data):
    """Drop the symbols with the oldest reference (end of the list) until the JSON fits MAX_BYTES."""
    size = len(json.dumps(data, separators=(",", ":")))
    while size > MAX_BYTES and data["symbols"]:
        sym = data["symbols"].pop()
        size -= len(json.dumps({sym: data["bars"].pop(sym)}, separators=(",", ":")))
        data["counts"].pop(sym, None)
        data["dropped_for_size"].append(sym)
