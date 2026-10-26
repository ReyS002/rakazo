"""paper: Alpaca paper accounts + OANDA practice account. GET only; live hosts refused before any request.

Env discovery mirrors /opt/scripts/tba/export_paper_fills.py (same files, same key aliases, dedupe by key id).
Credentials are read at runtime, used only as request headers, and never put into the returned data.
"""

import re
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

from sections import fnum, iso_str

ET = ZoneInfo("America/New_York")
ALPACA_PAPER = "https://paper-api.alpaca.markets"
ALPACA_PAPER_HOST = "paper-api.alpaca.markets"
OANDA_PRACTICE = "https://api-fxpractice.oanda.com"
OANDA_PRACTICE_HOST = "api-fxpractice.oanda.com"
OANDA_ENV = "/opt/stacks/oanda-bridge/.env"
# export_paper_fills.ENV_FILES, then any other stack .env that names Alpaca keys
ENV_FILES = ("/opt/stacks/velez-trading-bot/.env", "/opt/stacks/bull-pilot/.env", "/opt/stacks/velez-swing-bot/.env",
             "/opt/stacks/bull-swarm/.env", "/opt/stacks/velez-crypto/.env", "/opt/stacks/dendrix-ai/.env",
             "/opt/stacks/trading-bull-ai/.env", "/opt/stacks/tba-repo/.env", "/opt/stacks/narrow-range-scanner/.env",
             "/opt/stacks/unchained-mentor/.env")
ENV_GLOB = "/opt/stacks/*/.env"
KID_KEYS = ("APCA_API_KEY_ID", "ALPACA_API_KEY_ID", "ALPACA_API_KEY", "UNCHAINED_ALPACA_KEY_ID")
SEC_KEYS = ("APCA_API_SECRET_KEY", "ALPACA_API_SECRET_KEY", "ALPACA_SECRET_KEY", "ALPACA_API_SECRET",
            "UNCHAINED_ALPACA_SECRET")
BASE_KEYS = ("APCA_API_BASE_URL", "ALPACA_BASE_URL")
TIMEOUT = 10
MAX_FILLS = 50
MAX_TX = 20
OANDA_ID_RE = re.compile(r"[0-9A-Za-z-]{3,40}")


def parse_env(text, wanted):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k in wanted:
            out[k] = v.strip().strip('"').strip("'")
    return out


def _first(env, keys):
    return next((env[k] for k in keys if env.get(k)), None)


def _host(url):
    try:
        return urllib.parse.urlsplit(url).hostname
    except ValueError:
        return None


def alpaca_accounts(reader):
    """[(label, kid, secret, base)] deduped by key id, in export_paper_fills order then glob order."""
    paths = list(ENV_FILES) + [p for p in reader.glob(ENV_GLOB) if p not in ENV_FILES]
    seen, out = set(), []
    for path in paths:
        try:
            text, _ = reader.read_text(path, 262144)
        except OSError:
            continue
        env = parse_env(text, set(KID_KEYS + SEC_KEYS + BASE_KEYS))
        kid, sec = _first(env, KID_KEYS), _first(env, SEC_KEYS)
        if not kid or not sec or kid in seen:
            continue
        seen.add(kid)
        out.append((path.rstrip("/").split("/")[-2], kid, sec, _first(env, BASE_KEYS)))
    return out


def alpaca_is_paper(kid, base):
    if base:
        return _host(base) == ALPACA_PAPER_HOST
    return kid.startswith("PK")  # no base URL: only paper-style key ids (live ids start with AK)


def alpaca_one(http_get, label, kid, sec, base, now):
    if not alpaca_is_paper(kid, base):
        return {"account_label": label, "ok": False, "error": "not paper"}
    hdr = {"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": sec, "Accept": "application/json"}

    def get(path, params=None):
        url = ALPACA_PAPER + path + ("?" + urllib.parse.urlencode(params) if params else "")
        return http_get(url, hdr, TIMEOUT)

    acct = get("/v2/account")
    positions = get("/v2/positions")
    day = datetime.fromtimestamp(now, ET).date().isoformat()
    fills = get("/v2/account/activities/FILL", {"date": day, "direction": "desc", "page_size": MAX_FILLS})
    orders = get("/v2/orders", {"status": "open", "limit": 100, "nested": "false"})
    return {
        "account_label": label,
        "ok": True,
        "equity": fnum(acct.get("equity")),
        "cash": fnum(acct.get("cash")),
        "buying_power": fnum(acct.get("buying_power")),
        "positions": [{"symbol": p.get("symbol"), "qty": fnum(p.get("qty")), "avg_entry": fnum(p.get("avg_entry_price")),
                       "mark": fnum(p.get("current_price")), "unrealized_pl": fnum(p.get("unrealized_pl"))}
                      for p in positions if isinstance(p, dict)],
        "fills_today": [{"time": iso_str(f.get("transaction_time")), "symbol": f.get("symbol"), "side": f.get("side"),
                         "qty": fnum(f.get("qty")), "price": fnum(f.get("price"))}
                        for f in fills if isinstance(f, dict)][:MAX_FILLS],
        "open_orders": [{"symbol": o.get("symbol"), "side": o.get("side"), "qty": fnum(o.get("qty")),
                         "type": o.get("type"), "stop": fnum(o.get("stop_price")), "limit": fnum(o.get("limit_price"))}
                        for o in orders if isinstance(o, dict)],
    }


def oanda(http_get, reader):
    try:
        text, _ = reader.read_text(OANDA_ENV, 65536)
    except OSError:
        return {"ok": False, "error": "no oanda env"}
    env = parse_env(text, {"OANDA_API_KEY", "OANDA_ACCOUNT_ID", "OANDA_BASE_URL"})
    if _host(env.get("OANDA_BASE_URL") or "") != OANDA_PRACTICE_HOST:
        return {"ok": False, "error": "not practice"}
    key, acct_id = env.get("OANDA_API_KEY"), env.get("OANDA_ACCOUNT_ID")
    if not key or not acct_id or not OANDA_ID_RE.fullmatch(acct_id):
        return {"ok": False, "error": "oanda env incomplete"}
    hdr = {"Authorization": "Bearer " + key, "Accept-Datetime-Format": "RFC3339"}
    base = f"{OANDA_PRACTICE}/v3/accounts/{urllib.parse.quote(acct_id, safe='')}"

    def get(path, params=None):
        return http_get(base + path + ("?" + urllib.parse.urlencode(params) if params else ""), hdr, TIMEOUT)

    summary = get("/summary").get("account") or {}
    trades = get("/openTrades").get("trades") or []
    pending = get("/pendingOrders").get("orders") or []
    txs = []
    last = str(summary.get("lastTransactionID") or "")
    if last.isdigit() and int(last) > 0:
        first = max(1, int(last) - MAX_TX + 1)
        txs = get("/transactions/idrange", {"from": first, "to": int(last)}).get("transactions") or []
    return {
        "ok": True,
        "account_suffix": acct_id[-3:],
        "currency": summary.get("currency"),
        "balance": fnum(summary.get("balance")),
        "nav": fnum(summary.get("NAV")),
        "open_trades": [{"instrument": t.get("instrument"), "units": fnum(t.get("currentUnits")),
                         "price": fnum(t.get("price")), "unrealized_pl": fnum(t.get("unrealizedPL")),
                         "stop": fnum((t.get("stopLossOrder") or {}).get("price"))}
                        for t in trades if isinstance(t, dict)],
        "pending_orders": [{"instrument": o.get("instrument"), "units": fnum(o.get("units")), "type": o.get("type"),
                            "price": fnum(o.get("price"))}
                           for o in pending if isinstance(o, dict)],
        "recent_transactions": [{"time": iso_str(t.get("time")), "type": t.get("type"), "instrument": t.get("instrument"),
                                 "units": fnum(t.get("units")), "pl": fnum(t.get("pl"))}
                                for t in txs if isinstance(t, dict)][-MAX_TX:],
        "environment": "practice",
    }


def collect(deps):
    now = deps.now()
    alpaca, errors = [], []
    for label, kid, sec, base in alpaca_accounts(deps.reader):
        try:
            alpaca.append(alpaca_one(deps.http_get, label, kid, sec, base, now))
        except Exception as e:
            alpaca.append({"account_label": label, "ok": False, "error": type(e).__name__ + _code(e)})
    try:
        oanda_practice = oanda(deps.http_get, deps.reader)
    except Exception as e:
        oanda_practice = {"ok": False, "error": type(e).__name__ + _code(e)}
    for a in alpaca:
        if not a["ok"]:
            errors.append(f"alpaca {a['account_label']}: {a['error']}")
    if not oanda_practice["ok"]:
        errors.append(f"oanda: {oanda_practice['error']}")
    data = {"alpaca": alpaca, "oanda_practice": oanda_practice}
    if not errors:
        return data
    any_ok = oanda_practice["ok"] or any(a["ok"] for a in alpaca)
    return data, any_ok, "; ".join(errors)


def _code(e):
    msg = str(e)
    return f" ({msg})" if msg.startswith("HTTP ") and len(msg) <= 8 else ""
