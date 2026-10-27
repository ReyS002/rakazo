"""nrs: narrow-range scanner lists (stock, forex, crypto) from the scanner's own redacted exports.

Source: /opt/stacks/ops-status/nrs-<market>-latest.json (symlink to the newest nrs-<market>-<ts>.json in the same
dir, written every 15 min by the nrs-<market>-export jobs; schema 1.0, export_type nrs_<market>_redacted).
Per market: watchlist (daily scan) + recent_triggers (15m breakouts), each capped, URL fields dropped.
`new_since_prev` = entries not in the previous nrs.json snapshot (null when there is none for that market).
"""

import re

import scrub
from sections import SourceMissing, epoch_of, fnum, iso, iso_str, read_json, stale_weekday

OPS_DIR = "/opt/stacks/ops-status"
MARKETS = ("stock", "forex", "crypto")
MAX_READ = 4 << 20
MAX_ENTRIES = 60
MAX_FIELDS = 40
MAX_LIST = 12
TEXT_MAX = 200
REASON_MAX = 200
KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,47}")
DROP_KEY = re.compile(r"(?i)url|chart|link|href|account|host")
URLISH = re.compile(r"(?i)\b(?:https?|wss?)://")


def count_key(k):
    """`..._24h` would read as a token to the scrubber (digit+letter segment): spell the unit out."""
    return scrub.clip(re.sub(r"(?<![A-Za-z0-9])(\d+)h\b", r"\1_hours", k), 60)


def market_path(market):
    return f"{OPS_DIR}/nrs-{market}-latest.json"


def _value(v):
    """Scalar (or short list of scalars) from an export entry; None to drop it."""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return fnum(v)
    if isinstance(v, str):
        if URLISH.search(v):
            return None
        return iso_str(v) or scrub.clip(v, TEXT_MAX)
    if isinstance(v, list):
        out = [x for x in (_value(x) for x in v[:MAX_LIST] if not isinstance(x, (list, dict))) if x is not None]
        return out
    return None


def clean_entry(e):
    """Every field the export has (whatever they are), minus URLs/chart links/account/host and nested objects."""
    if not isinstance(e, dict):
        return None
    out = {}
    for k, v in list(e.items())[:MAX_FIELDS]:
        if not isinstance(k, str) or not KEY_RE.fullmatch(k) or DROP_KEY.search(k) or isinstance(v, dict):
            continue
        val = _value(v)
        if val is not None or v is None:
            out[count_key(k)] = val
    return out if out.get("symbol") else None


def watch_key(e):
    return scrub.scrub_text(str(e.get("symbol")))


def trigger_key(e):
    return "|".join(scrub.scrub_text(str(e.get(k))) for k in ("symbol", "triggered_at", "direction"))


def read_market(reader, market, now):
    path = market_path(market)
    out = {"ok": False, "reason": None, "source": path, "file_mtime": None, "generated_at": None, "age_s": None,
           "stale": True}
    try:
        doc, mtime = read_json(reader, path, MAX_READ, link_dir=OPS_DIR)
    except SourceMissing as e:
        out["reason"] = f"source {e}"
        return out
    except (ValueError, UnicodeDecodeError):
        out["reason"] = "source is not valid JSON"
        return out
    out["file_mtime"] = iso(mtime)
    if not isinstance(doc, dict) or not isinstance(doc.get("watchlist"), list) \
            or not isinstance(doc.get("recent_triggers"), list):
        out["reason"] = "unexpected export shape (need object with watchlist + recent_triggers lists)"
        return out
    gen = epoch_of(doc.get("ts_utc"))
    ref = gen if gen is not None else mtime
    watch = [r for r in (clean_entry(e) for e in doc["watchlist"]) if r]
    trig = [r for r in (clean_entry(e) for e in doc["recent_triggers"]) if r]
    trig.sort(key=lambda r: str(r.get("triggered_at") or ""), reverse=True)
    counts = doc.get("counts") if isinstance(doc.get("counts"), dict) else {}
    heart = doc.get("heartbeats") if isinstance(doc.get("heartbeats"), dict) else {}
    out.update({
        "ok": True,
        "generated_at": iso(gen) if gen is not None else None,
        "as_of": iso(ref),
        "age_s": max(0, int(now - ref)),
        "stale": stale_weekday(ref, now),
        "export_type": scrub.clip(doc.get("export_type"), 40) if isinstance(doc.get("export_type"), str) else None,
        "trading_mode": scrub.clip(doc.get("trading_mode"), 40) if isinstance(doc.get("trading_mode"), str) else None,
        "export_error": scrub.clip(doc.get("error"), REASON_MAX) if isinstance(doc.get("error"), str) else None,
        "watchlist_scan_date": doc.get("watchlist_scan_date") if isinstance(doc.get("watchlist_scan_date"), str)
        and len(doc["watchlist_scan_date"]) == 10 else None,
        "counts": {count_key(k): int(v) for k, v in counts.items()
                   if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)},
        "heartbeats": {scrub.clip(k, 60): {"last_success_at": iso_str(v.get("last_success_at")),
                                           "detail": scrub.clip(v.get("detail"), 80)
                                           if isinstance(v.get("detail"), str) else None}
                       for k, v in list(heart.items())[:10] if isinstance(k, str) and isinstance(v, dict)},
        "watchlist_count": len(watch),
        "recent_triggers_count": len(trig),
        "watchlist": watch[:MAX_ENTRIES],
        "recent_triggers": trig[:MAX_ENTRIES],
    })
    return out


def new_since_prev(cur, prev_market):
    """Keys present now but not in the previous snapshot; None when there is no comparable previous market."""
    if not cur.get("ok") or not isinstance(prev_market, dict) or not prev_market.get("ok"):
        return None
    pw = {watch_key(e) for e in prev_market.get("watchlist") or () if isinstance(e, dict)}
    pt = {trigger_key(e) for e in prev_market.get("recent_triggers") or () if isinstance(e, dict)}
    return {"watchlist": [e["symbol"] for e in cur["watchlist"] if watch_key(e) not in pw],
            "recent_triggers": [{"symbol": e.get("symbol"), "triggered_at": e.get("triggered_at"),
                                 "direction": e.get("direction")}
                                for e in cur["recent_triggers"] if trigger_key(e) not in pt]}


def _prev_markets(deps):
    try:
        prev = deps.read_prev("nrs")
    except Exception:
        return None
    if not isinstance(prev, dict) or not isinstance(prev.get("data"), dict):
        return None
    m = prev["data"].get("markets")
    return m if isinstance(m, dict) else None


def collect(deps):
    now = deps.now()
    prev = _prev_markets(deps)
    markets, errors = {}, []
    for m in MARKETS:
        try:
            cur = read_market(deps.reader, m, now)
        except OSError as e:
            cur = {"ok": False, "reason": f"read failed: {type(e).__name__}", "source": market_path(m), "stale": True}
        cur["new_since_prev"] = new_since_prev(cur, (prev or {}).get(m))
        markets[m] = cur
        if not cur["ok"]:
            errors.append(f"{m}: {cur['reason']}")
    data = {"markets": markets, "stale": any(v.get("stale") for v in markets.values()),
            "prev_snapshot": prev is not None}
    if not errors:
        return data
    return data, any(v["ok"] for v in markets.values()), "; ".join(errors)
