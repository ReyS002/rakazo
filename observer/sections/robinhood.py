"""robinhood: watch-only Robinhood snapshot via the local Hermes Robinhood MCP bridge (hermes-robinhood-bridge.service).

Bridge (/root/hermes-ai-router/src/hermes-os/robinhood/bridge.ts, dist/.../main.js) listens on 127.0.0.1:28188
(config default; the port can be overridden in /etc/hermes-robinhood-bridge.env, which is never read here).
Routes: GET /health (no auth: connected/authenticated/authorization_pending/last_error), GET /oauth/callback,
POST /oauth/start, GET /v1/tools, POST /v1/tools/<tool> -- all but /health and /oauth/callback need the bridge's
bearer key, and positions/cash/orders are only reachable as POST tool calls. The collector is GET-only, sends no
credential and may fetch only /health (deps.LOCAL_ROUTES), so positions are reported as unreadable (ok:false)
with the bridge status attached. No account numbers ever: the account label is always "robinhood".
"""

import scrub
from sections import iso

PORT = 28188
HEALTH = "/health"
TIMEOUT = 5
UNREADABLE = ("positions/cash/buying power/open orders are only exposed as authenticated POST /v1/tools/<tool> "
              "calls (bridge bearer key); the collector is GET-only and does not read the key")


def _bridge(status, body):
    if not isinstance(body, dict) or not isinstance(body.get("connected"), bool) \
            or not isinstance(body.get("authenticated"), bool):
        return None
    return {"http_status": status, "connected": body["connected"], "authenticated": body["authenticated"],
            "authorization_pending": body.get("authorization_pending")
            if isinstance(body.get("authorization_pending"), bool) else None,
            "last_error": scrub.clip(body["last_error"], 160) if isinstance(body.get("last_error"), str) else None}


def collect(deps):
    now = deps.now()
    data = {"ok": False, "reason": None, "account_label": "robinhood", "watch_only": True,
            "bridge": {"host": "127.0.0.1", "port": PORT, "route": "GET " + HEALTH, "reachable": False},
            "positions": None, "cash": None, "buying_power": None, "open_orders_count": None,
            "as_of": None, "stale": True, "checked_at": iso(now)}
    if deps.local_get is None:
        data["reason"] = "no loopback client configured"
        return data, False, data["reason"]
    try:
        status, body = deps.local_get(PORT, HEALTH, TIMEOUT)
    except Exception as e:
        data["reason"] = scrub.clip(f"bridge not reachable on 127.0.0.1:{PORT}: {type(e).__name__}: {e}", 200)
        return data, False, data["reason"]
    br = _bridge(status, body)
    if br is None:
        data["bridge"].update({"reachable": True, "http_status": status})
        data["reason"] = (f"127.0.0.1:{PORT}{HEALTH} answered HTTP {status} without the bridge's health shape "
                          "(another listener on the port, or the bridge runs on a different port)")
        return data, False, data["reason"]
    data["bridge"].update(br, reachable=True)
    data["reason"] = UNREADABLE + ("" if br["authenticated"] else "; bridge also reports not authenticated")
    return data, False, data["reason"]
