# tv-futures

TradingView Pine `alert()` webhook -> `POST /tv-futures/alert` (Traefik strips prefix) -> `receiver.py`
validates -> fixed-schema JSON to TickerBull webhook (Bearer). Never trades, no BullWarden network.

TradingView side: `TB_Futures_Coil_NR7.pine` on **one** 15m chart watches all six symbols (`request.security`),
**one** alert ("Any alert() function call"); one `alert()` / webhook per symbol that fires. Steps: `PINE_CHECKLIST.md`.

## Env (`/root/.hermes/credentials/bull-desk-tv.env`)

| var | req | meaning |
|-----|-----|---------|
| `TV_KEYS` | yes | comma list of shared keys (>=24 chars). Goes in the alert body field `key`. |
| `BD_TICKERBULL_WEBHOOK_URL` | yes | TickerBull webhook URL |
| `BD_TICKERBULL_WEBHOOK_AUTH` | yes | bearer secret value (sent as `Authorization: Bearer <v>`) |
| `TV_SYMBOLS` | no | roots, default `MES,MNQ,MYM,M2K,MCL,MGC` |
| `TV_TIMEFRAMES` | no | default `15` |
| `TV_TRUSTED_PROXIES` | no | extra IPs/CIDRs whose `X-Forwarded-For` is trusted (docker gateway is always trusted) |
| `TV_KILL_DIR` / `TV_STATE_DIR` | no | default `/run/tv` |

Missing/short required vars -> refuses to start (exit 2), message names the var, never the value.

## Routes

- `GET /health` -> `{ok, kill, forwarded_today, rejected_today, last_forward_utc}` (503 when killed).
- `POST /alert` -> `200 {"ok":true,"action":"forwarded"}` | `200 {"ok":true,"action":"ignored","reason":...}`
  (`session_closed`, `not_coil_nr7`, `duplicate`, `cooldown`, `daily_cap`) | 400 `invalid_json` | 403 `forbidden` |
  411 | 413 | 422 `{"error":..,"field":..}` | 429 `rate_limited`/`blocked` | 502 `forward_failed` | 503 `disabled`.
- Anything else 404 / 405.

## Decisions

- Order: kill -> IP block -> 30/min global -> 30/min per IP -> 2 KB cap -> JSON -> key -> schema -> allowlists -> session -> coil -> dedupe -> cooldown -> 300/day -> forward.
- Session gate uses the **bar open time** (`bar_time`), so the 16:45 ET bar (alert at 17:00) still counts. CME holidays not modelled.
- Client IP: peer address; if the peer is the container's default gateway (or in `TV_TRUSTED_PROXIES`) the **last** `X-Forwarded-For` hop (the one the proxy appended; leftmost is client-controlled).
- Dedupe (48 h) and cooldown (30 min, per root) are reserved before forwarding and released if the forward fails, so a TradingView re-fire can still go through.
- Forward values are passed through as fractions (`spread_pct` 0.0012 = 0.12 %), same as the Pine inputs.
- Log: `TV_STATE_DIR/alerts.jsonl`, one line per decision (ts, symbol, tf, bar_time, action, reason, status[, upstream_status]); auth failures log no body-derived field. Rotates at 1 MB into `.1`, `.2`.

## Tests

`cd /opt/stacks/bull-desk/tv-futures && python3 -m unittest`
