"""phasec: Phase C progress counters (TB-VSL-009 elephant swing paper-forward, TB-VSL-014 Fab4 mega Phase C').

Count rules come from files on this box; anything that cannot be reproduced exactly is ok:false with a reason
and the list of what was found. Numbers are never estimated.

TB-VSL-009 (rule text: Bull Pen "TB-VSL-009 Phase C weekday digest" routine, /root/grok-bot-instructions/
bull-pen-strategies.md; the Lab md files TB-VSL-009_PHASE_C_ARMED.md etc. live on Class B /workspace, not here):
  primary count source = Bull Pilot JI68 closed-trade export (/opt/stacks/ops-status/bullpilot-closed-trades-latest.json,
  reconciled from Alpaca paper fills/orders + the bull-pilot journal). Count a trade when: complete close (exit time,
  exit price, pnl present), setup == elephant_bar, entry date (ET) after 2026-09-21, symbol in A1 watchlist_all 25
  (universe_a1_watchlist_equity_buckets_v1.yaml), SPCX fail-closed (excluded: short-history check not reproducible
  here), timeframe 1Day when the export states one. Bucket = mega / mid_smaller from the same yaml.
  Gate: n >= 50 OR 90 calendar days from 2026-09-22. Alerts at n 10/25/40. Fail: net <= 0 at n >= 30,
  DD > 8 %, PF < 1 at n >= 40.
TB-VSL-014: setup fab4_trap_breakout, entry after 2026-09-21 23:40 ET, mega 13 from tb_vsl_014_candidate01_manifest.json.
  No count md and no reconciled closed-trade export for the account the Bull Pen notes name (TYWQ) exist on this
  box, while the Bull Pilot swing lane (JI68) also allowlists fab4 -> source account ambiguous -> ok:false.
"""

import re
from datetime import date, datetime, timedelta

import scrub
from sections import ET, SourceMissing, epoch_of, fnum, iso, iso_str, read_json, stale_weekday

OPS_DIR = "/opt/stacks/ops-status"
CLOSED = f"{OPS_DIR}/bullpilot-closed-trades-latest.json"
EVAL_DIR = "/opt/stacks/bull-pilot/bot/reports/evaluation"
BUCKETS = f"{EVAL_DIR}/universe_a1_watchlist_equity_buckets_v1.yaml"
MANIFEST_014 = f"{EVAL_DIR}/tb_vsl_014_candidate01_manifest.json"
RULE_DOC = "/root/grok-bot-instructions/bull-pen-strategies.md"
EXPORTS = "/opt/arena/state/exports"
LAB_DIRS = ("/opt/stacks/tba-brain", "/root/tba-brain", "/opt/repos", "/opt/stacks/bull-pilot-futures-lab")
MAX_READ = 4 << 20
MAX_ROWS = 60

START_009 = date(2026, 9, 22)          # entries after 2026-09-21
GATE_N, GATE_DAYS = 50, 90
ALERTS = (10, 25, 40)
DAILY_TF = {"1day", "1d", "d", "day", "daily", "swing"}
SPCX = "SPCX"
START_014 = datetime(2026, 9, 21, 23, 40, tzinfo=ET)
SYM_RE = re.compile(r"[A-Z][A-Z0-9.]{0,9}")


# ── rule inputs ──────────────────────────────────────────────────────────

def parse_buckets(text):
    """{'watchlist_all': [...], 'mega': [...], 'mid_smaller': [...]} from the frozen A1 yaml (block lists only)."""
    want = {"primary_sleeve_equity": "watchlist_all", "mega": "mega", "mid_smaller": "mid_smaller"}
    out, cur, cur_indent = {}, None, 0
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        s = line.strip()
        if cur and s.startswith("- ") and indent >= cur_indent:
            sym = s[2:].strip().strip("'\"")
            if SYM_RE.fullmatch(sym):
                out[cur].append(sym)
            continue
        cur = None
        m = re.fullmatch(r"([a-z_]+):\s*", s)
        if m and m.group(1) in want:
            cur, cur_indent = want[m.group(1)], indent
            out[cur] = []
    return out


def load_universe(reader):
    text, _ = reader.read_text(BUCKETS, 65536)
    b = parse_buckets(text)
    sizes = {k: len(set(b.get(k, ()))) for k in ("watchlist_all", "mega", "mid_smaller")}
    if sizes != {"watchlist_all": 25, "mega": 13, "mid_smaller": 12} \
            or set(b["mega"]) | set(b["mid_smaller"]) != set(b["watchlist_all"]):
        raise ValueError(f"A1 bucket yaml does not give 25 = 13 mega + 12 mid_smaller (got {sizes})")
    return b


def lab_md_found(reader):
    found = []
    for d in LAB_DIRS:
        for depth in ("", "/*", "/*/*", "/*/*/*"):
            for tag in ("TB-VSL-009", "TB-VSL-014"):
                found += reader.glob(f"{d}{depth}/{tag}*.md")
    return sorted(set(found))[:20]


# ── metrics ──────────────────────────────────────────────────────────────

def metrics(trades):
    """n, net, PF, expR, max DD ($, trade-PnL path). PF None without losses; expR over trades with an R."""
    n = len(trades)
    pnl = [t["pnl_usd"] for t in trades]
    wins = sum(p for p in pnl if p > 0)
    losses = -sum(p for p in pnl if p < 0)
    rs = [t["r_multiple"] for t in trades if t["r_multiple"] is not None]
    peak = cum = dd = 0.0
    for p in pnl:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return {"n": n, "net_usd": round(sum(pnl), 2), "profit_factor": round(wins / losses, 3) if losses > 0 else None,
            "pf_note": None if losses > 0 else ("no losing trades" if n else "no trades"),
            "exp_r": round(sum(rs) / len(rs), 3) if rs else None, "exp_r_n": len(rs),
            "max_dd_usd": round(dd, 2), "win_rate": round(sum(1 for p in pnl if p > 0) / n, 3) if n else None}


def _entry_date(t):
    ep = epoch_of(t.get("entry_time_et"))
    return datetime.fromtimestamp(ep, ET).date() if ep is not None else None


def complete_close(t):
    return (iso_str(t.get("exit_time_et")) is not None and fnum(t.get("exit_price")) is not None
            and fnum(t.get("pnl_usd")) is not None and _entry_date(t) is not None)


def filter_009(trades, buckets):
    """(counted rows, exclusion tallies)."""
    allowed = set(buckets["watchlist_all"])
    mega = set(buckets["mega"])
    rows, excl, tf_unverified = [], {}, 0

    def skip(why):
        excl[why] = excl.get(why, 0) + 1

    for t in trades:
        if not isinstance(t, dict):
            skip("not an object")
            continue
        if t.get("setup") != "elephant_bar":
            skip("setup not elephant_bar")
            continue
        if not complete_close(t):
            skip("incomplete close")
            continue
        if _entry_date(t) < START_009:
            skip("entry on/before 2026-09-21")
            continue
        sym = t.get("symbol")
        if sym == SPCX:
            skip("SPCX fail-closed")
            continue
        if sym not in allowed:
            skip("symbol not in A1 watchlist_all 25")
            continue
        tf = t.get("timeframe")
        if tf is not None and str(tf).strip().lower() not in DAILY_TF:
            skip("timeframe not daily")
            continue
        if tf is None:
            tf_unverified += 1
        rows.append({"symbol": sym, "bucket": "mega" if sym in mega else "mid_smaller",
                     "side": t.get("side") if isinstance(t.get("side"), str) else None,
                     "entry_time": iso_str(t.get("entry_time_et")), "exit_time": iso_str(t.get("exit_time_et")),
                     "exit_reason": scrub.clip(t.get("exit_reason"), 40) if isinstance(t.get("exit_reason"), str)
                     else None,
                     "pnl_usd": fnum(t.get("pnl_usd")), "r_multiple": fnum(t.get("r_multiple"))})
    rows.sort(key=lambda r: r["exit_time"])
    return rows, excl, tf_unverified


def coverage_check(reader, export):
    """Compare the export's fill_count with the newest paper-fills fill_history_count for the bull-pilot stack."""
    files = [p for p in reader.glob(f"{EXPORTS}/paper-fills-*.json") if re.search(r"-\d{8}\.json$", p)]
    if not files:
        return {"checked": False, "note": "no paper-fills export to cross-check"}
    try:
        doc, _ = read_json(reader, files[-1], 16 << 20)
        acct = next(a for a in (doc.get("accounts") or {}).values()
                    if isinstance(a, dict) and a.get("env_stack") == "bull-pilot")
    except (SourceMissing, ValueError, StopIteration, AttributeError, OSError, UnicodeDecodeError):
        return {"checked": False, "note": "bull-pilot block not found in newest paper-fills"}
    hist, fc = acct.get("fill_history_count"), export.get("fill_count")
    ok = isinstance(hist, int) and isinstance(fc, int) and hist == fc and acct.get("fill_history_complete") is True
    return {"checked": True, "paper_fills_file": files[-1], "fill_history_count": hist,
            "export_fill_count": fc, "complete": ok}


# ── lanes ────────────────────────────────────────────────────────────────

def _fail(lane, reason, found):
    return {"lane": lane, "ok": False, "reason": scrub.clip(reason, 300), "found": found, "n": None}


def lane_009(reader, now, found):
    lane = "TB-VSL-009"
    try:
        buckets = load_universe(reader)
    except (OSError, ValueError) as e:
        return _fail(lane, f"A1 bucket yaml unusable: {type(e).__name__}: {e}", found)
    try:
        export, mtime = read_json(reader, CLOSED, MAX_READ, link_dir=OPS_DIR)
    except SourceMissing as e:
        return _fail(lane, f"closed-trade export {e}", found)
    except (ValueError, UnicodeDecodeError):
        return _fail(lane, "closed-trade export is not valid JSON", found)
    if not isinstance(export, dict) or export.get("export_type") != "bullpilot_closed_trades" \
            or not isinstance(export.get("trades"), list):
        return _fail(lane, "closed-trade export has unexpected shape (need export_type bullpilot_closed_trades "
                           "+ trades list)", found)
    since = epoch_of(export.get("since"))
    rows, excl, tf_unverified = filter_009(export["trades"], buckets)
    m = metrics(rows)
    today = datetime.fromtimestamp(now, ET).date()
    days = (today - START_009).days
    gate_hit = m["n"] >= GATE_N or days >= GATE_DAYS
    triggers = [
        {"rule": "net <= 0 at n >= 30", "hit": m["n"] >= 30 and m["net_usd"] <= 0},
        {"rule": "max DD > 8 %", "hit": None,
         "note": "not evaluable: export has no equity base for a % drawdown (max_dd_usd is the trade-PnL path)"},
        {"rule": "PF < 1 at n >= 40", "hit": m["n"] >= 40 and m["profit_factor"] is not None
         and m["profit_factor"] < 1},
    ]
    gen = epoch_of(export.get("generated_utc")) or mtime
    cov = coverage_check(reader, export)
    window_gap = since is not None and datetime.fromtimestamp(since, ET).date() > START_009
    return {
        "lane": lane, "ok": True, "reason": None,
        "rule": {"setup": "elephant_bar", "timeframe": "1Day (swing)", "entry_after_et": "2026-09-21",
                 "universe": "A1 watchlist_all 25", "spcx": "excluded (fail-closed)",
                 "complete_close": "exit time + exit price + pnl present"},
        **m,
        "max_dd_pct": None,
        "buckets": {b: metrics([r for r in rows if r["bucket"] == b]) for b in ("mega", "mid_smaller")},
        "days_elapsed": days, "gate": {"rule": f"n >= {GATE_N} OR {GATE_DAYS} calendar days from 2026-09-22",
                                       "deadline": (START_009 + timedelta(days=GATE_DAYS)).isoformat(),
                                       "status": "hit" if gate_hit else "open",
                                       "n_remaining": max(0, GATE_N - m["n"]),
                                       "days_remaining": max(0, GATE_DAYS - days)},
        "alerts_crossed": [a for a in ALERTS if m["n"] >= a],
        "fail_triggers": triggers,
        "fail_triggers_hit": [t["rule"] for t in triggers if t["hit"]],
        "timeframe_unverified_n": tf_unverified,
        "excluded": excl,
        "closes": rows[-MAX_ROWS:],
        "last_close": rows[-1]["exit_time"] if rows else None,
        "last_updated": iso(gen), "stale": stale_weekday(gen, now),
        "export_window_since": iso(since) if since is not None else None,
        "export_window_gap": ("export starts after 2026-09-22; entries before its `since` are only covered "
                              "if coverage.complete is true") if window_gap else None,
        "coverage": cov,
        "sources": [CLOSED, BUCKETS, RULE_DOC],
        "found": found,
    }


def lane_014(reader, found):
    lane = "TB-VSL-014"
    info = {"setup": "fab4_trap_breakout", "entry_after_et": START_014.isoformat()}
    try:
        man, _ = read_json(reader, MANIFEST_014, MAX_READ)
        uni = man.get("confirmation_universe") if isinstance(man, dict) else None
        if isinstance(uni, list) and len(uni) == 13 and all(isinstance(s, str) and SYM_RE.fullmatch(s) for s in uni):
            info["mega13"] = uni
            info["mega13_source"] = MANIFEST_014
    except (SourceMissing, ValueError, UnicodeDecodeError, OSError):
        pass
    reason = ("count rule not reproducible here: no TB-VSL-014*.md on this box, and the source account is "
              "ambiguous (Bull Pilot swing lane allowlists fab4_trap_breakout, but Bull Pen notes say Fab4 Phase C' "
              "closes are on the Velez Trading account, which has no reconciled closed-trade export)")
    out = _fail(lane, reason, found)
    out["rule_known"] = info
    return out


def collect(deps):
    now = deps.now()
    found = {"lab_md": lab_md_found(deps.reader),
             "rule_doc": RULE_DOC if deps.reader.exists(RULE_DOC) else None,
             "closed_trades_export": CLOSED if deps.reader.exists(CLOSED) else None,
             "a1_buckets": BUCKETS if deps.reader.exists(BUCKETS) else None,
             "manifest_014": MANIFEST_014 if deps.reader.exists(MANIFEST_014) else None}
    lanes = {"tb_vsl_009": lane_009(deps.reader, now, found), "tb_vsl_014": lane_014(deps.reader, found)}
    ok_lanes = [v for v in lanes.values() if v["ok"]]
    data = {"today_et": datetime.fromtimestamp(now, ET).date().isoformat(), "lanes": lanes,
            "stale": not ok_lanes or any(v["stale"] for v in ok_lanes)}
    errors = [f"{k}: {v['reason']}" for k, v in lanes.items() if not v["ok"]]
    if not errors:
        return data
    return data, any(v["ok"] for v in lanes.values()), "; ".join(errors)
