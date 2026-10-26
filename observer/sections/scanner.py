"""scanner: recent fires (same feed + parsing as `tba-ro-status fires`), push status, scanner cron freshness."""

import json
import math
import re
from datetime import datetime, timezone

from sections import LOG_DIR, age, iso

FIRES_FILE = "/opt/stacks/ops-status/scanner-fires-latest.jsonl"
MAX_READ = 4 * 1024 * 1024
MAX_FIRES = 50
KINDS = ("stock", "forex", "crypto")
BAD = re.compile(r"[^A-Za-z0-9._:+/ -]")  # tba-ro-status clean()
SCANNER_LOGS = ("stock-alerts", "forex-alerts", "crypto-alerts", "options-alerts", "nrs-stock-export",
                "nrs-forex-export", "nrs-crypto-export", "narrow-gap-shadow-export", "narrow-gap-v2-shadow-export",
                "fires-export", "grokbot-push")
STATUS_RE = re.compile(r"^(\S+)\s+configured=(yes|no)\s+last_success=(\S+)\s+consec_fail=(\d+)"
                       r"\s+sent_today=(\d+)\s+retries=(\d+)\s+dead=(\d+)\s*$")
BOT_RE = re.compile(r"[A-Z0-9_]{1,24}")


def clean(v):
    if not isinstance(v, str):
        return None
    return BAD.sub("", v)[:40] or None


def to_epoch(ts):
    if isinstance(ts, bool):
        return None
    if isinstance(ts, (int, float)):
        ep = float(ts) / 1000.0 if ts > 1e12 else float(ts)
    elif isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ep = dt.timestamp()
    else:
        return None
    return ep if math.isfinite(ep) and 0 < ep < 4102444800 else None


def price(v):
    if isinstance(v, str):
        try:
            v = float(v)
        except ValueError:
            return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def late(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.lower() in ("true", "false"):
        return v.lower() == "true"
    return None


def recent_fires(reader, now):
    """(fires oldest->newest, feed_updated_age_s). Missing feed -> ([], None)."""
    try:
        text, truncated = reader.read_text(FIRES_FILE, MAX_READ, tail=True)
        mtime = reader.lstat(FIRES_FILE).st_mtime
    except FileNotFoundError:
        return [], None
    lines = text.split("\n")
    if truncated:
        lines = lines[1:]
    fires = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("kind") not in KINDS:
            continue
        ep = to_epoch(rec.get("ts"))
        if ep is None:
            continue
        row = {"time": iso(ep), "kind": rec["kind"], "symbol": clean(rec.get("symbol")),
               "setup": clean(rec.get("setup")), "price": price(rec.get("price"))}
        if "late" in rec:
            row["late"] = late(rec.get("late"))
        if "source" in rec:
            row["source"] = clean(rec.get("source"))
        fires.append((ep, row))
    fires.sort(key=lambda x: x[0])
    return [r for _, r in fires[-MAX_FIRES:]], age(now, mtime)


def parse_push_status(text):
    """`grokbot_push.py --status` lines -> {grok:{BOT:{...}}, bulldesk:{...}}. Only the whitelisted counters."""
    out = {"grok": {}, "bulldesk": {}}
    group = "grok"
    for line in (text or "").splitlines():
        if line.startswith("bulldesk credentials:"):
            group = "bulldesk"
            continue
        m = STATUS_RE.match(line)
        if not m or not BOT_RE.fullmatch(m.group(1)):
            continue
        last = to_epoch(m.group(3)) if m.group(3) != "-" else None
        out[group][m.group(1)] = {
            "configured": m.group(2) == "yes",
            "last_success": iso(last) if last else None,
            "consec_fail": int(m.group(4)),
            "sent_today": int(m.group(5)),
            "dead": int(m.group(7)),
        }
    return out


def scanner_cron(reader, now):
    rows = []
    for name in SCANNER_LOGS:
        path = f"{LOG_DIR}/{name}.log"
        try:
            rows.append({"name": name, "last_run_age_s": age(now, reader.lstat(path).st_mtime)})
        except OSError:
            rows.append({"name": name, "last_run_age_s": None})
    return rows


def collect(deps):
    now = deps.now()
    data, errors = {}, []
    try:
        data["recent_fires"], data["feed_updated_age_s"] = recent_fires(deps.reader, now)
    except Exception as e:
        data["recent_fires"], data["feed_updated_age_s"] = [], None
        errors.append(f"fires: {type(e).__name__}")
    try:
        data["push_status"] = parse_push_status(deps.push_status())
    except Exception as e:
        data["push_status"] = {"grok": {}, "bulldesk": {}}
        errors.append(f"push_status: {type(e).__name__}")
    data["scanner_cron"] = scanner_cron(deps.reader, now)
    if errors:
        return data, True, "; ".join(errors)
    return data
