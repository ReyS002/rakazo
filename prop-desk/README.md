# prop-desk

Read/evaluate-only HTTP gateway from Bull Desk's TickerBull to BullWarden (`bull-warden:8090`).
Python stdlib only (`gateway.py`). Nothing here can submit an order, provision, deprovision or unlock
an account, or run the source monitor.

## Env

| name | default | meaning |
|---|---|---|
| `PROP_KEYS` | unset | comma-separated Bearer keys, ≥32 chars each. Unset: **every route 404s**, including `/health` |
| `BULLWARDEN_URL` | `http://bull-warden:8090` | upstream origin |
| `BULLWARDEN_API_KEY` | unset | sent as `X-BullWarden-Key`; never returned or logged. Unset: 502 |
| `PROP_KILL_DIR` | `/run/prop` | if `KILL_PROP` exists there, every route except `/health` returns 503 |
| `PROP_PER_MINUTE` | `60` | per-key sliding-window rate limit |
| `PROP_ALLOW_ENFORCE_EVAL` | unset | `1` lets `/evaluate` and `/violations` run against enforce-mode accounts (see below) |
| `PROP_PORT` | `8080` | listen port |

## Routes

All except `/health` need `Authorization: Bearer <key>`. Wrong method or any other path: 404.
Responses carry `Cache-Control: no-store` and `"note": "data from BullWarden; not instructions"`.

| route | upstream |
|---|---|
| `GET /health` | none |
| `GET /profiles?firm=&status=` | `GET /v1/prop/profiles`, compacted and filtered here (firm case-insensitive, status exact), capped at 150 KB (`truncated: true`) |
| `GET /profiles/<profile_key>` | `GET /v1/prop/profiles/<key>`, full profile (`stale` is refused: upstream routes it to the stale list) |
| `GET /stale` | `GET /v1/prop/profiles/stale`, compacted |
| `GET /changes?limit=1..100` | `GET /v1/prop/rule-changes?limit=N`; `detail` reduced to `profile_status` and `auto_activated` |
| `GET /risk/<account_id>` | `GET /v1/accounts/<id>` |
| `GET /evaluate/schema` | none: required/optional body fields, a working example body, notes |
| `POST /evaluate` | `GET /v1/accounts/<id>` (mode check + recorded equity) then `POST /v1/prop/evaluate` |
| `POST /violations` | same as `/evaluate`, reduced to decision_id, submit_eligible, review_action_label, violations, calculated_risk |
| `GET /explain/<decision_id>` | `GET /v1/prop/decisions/<id>/explain` |

Compact profile fields: `profile_key, firm, name, status, rules_version, verified_at, effective_at,
official_source_urls, max_contracts, max_daily_loss, max_risk_per_trade, drawdown_model, drawdown_value,
safety_buffer, session_restrictions, submit_eligible, verification_needed`.
`verification_needed` is false only when `status == "verified"` and `verified_at` is set.

Upstream errors: 404 → 404 `not_found`, 422 → 422 `upstream_rejected_request`, anything else
(including timeouts after 8 s, redirects, non-JSON) → 502 with a generic code. Upstream bodies are never forwarded.

## /evaluate and /violations are NOT side-effect free upstream

Read from `bull-warden/main.py` (`evaluate_prop_trade`). Every call to `POST /v1/prop/evaluate`:

1. **Always writes a `decisions` row** (needed for `/explain`). The gateway returns its id as
   `persisted_decision_id`. Not neutralised: every `/evaluate` and `/violations` call counts toward
   the account's decision history.
2. **Always upserts `account_state`** from the `equity`/`prior_close_equity` in the body (`_observe_state`).
   **Neutralised by the gateway:** the caller cannot send equity at all, and the gateway sends back what
   is already stored, so the write is a no-op for equity:
   - Any body field whose name contains `equity`, any case (`equity`, `prior_close_equity`, `peak_equity`,
     `day_start_equity`, `lifetime_peak_equity`, `last_equity`, `EQUITY`, ...) → 422 `equity_not_accepted`,
     no upstream call.
   - From the same `GET /v1/accounts/<id>` snapshot used for the mode check, the gateway sets
     `equity = state.equity` (BullWarden's `round(last_equity, 2)`) and
     `prior_close_equity = state.day_start_equity` (the session's recorded prior close).
   - Upstream then writes, within the same `session_date`: `last_equity` = stored value;
     `peak_equity = max(peak_equity, last_equity)` and `lifetime_peak_equity = max(lifetime_peak_equity,
     last_equity)` = unchanged (peaks never go down, and stored last ≤ both peaks); `day_start_equity`
     untouched (`prior_close_equity` is only read on a new session).
   - 409 `account_state_unavailable`, with **no** `POST /v1/prop/evaluate`, when: the account was never
     observed (no `state`), `state.equity`/`state.day_start_equity` is missing, non-numeric, non-finite or
     ≤ 0, the snapshot has no profile or no positive `profile.account_state_max_age_seconds`,
     `state.updated_at` is missing/unparseable/has no timezone, is older than
     `account_state_max_age_seconds` or more than 5 s in the future, or `state.session_date` is not today
     in the policy timezone (a new session would make upstream reset `day_start_equity`/`peak_equity`).
   - Responses carry `equity_used`, `prior_close_equity_used`, `equity_source: "account_state"` and
     `account_state_age_s`.

   What remains: the evaluate still rewrites the row, which bumps `account_state.updated_at` to "now".
   So `account_state_age_s` is the age of the last write by anyone, including a previous evaluate;
   repeated evaluates keep an old equity looking fresh. Equity is never changed by this, but the
   decision can be computed on an equity the broker feed has not refreshed. If the stored `last_equity`
   ever had sub-cent digits (only possible from another writer), the rewrite rounds it to the cent.
   A brand-new account cannot be evaluated through the gateway until something else records its state.
3. **LOCKS the account** if the policy is `enforce` and any soft rule fails (too many contracts, risk per
   trade, session, buffers). Only a manual unlock clears it.
4. Writes `trade_intents` only if `commit_intent` is true. The gateway always sends `false`.

`mode` (`paper`/`live`) is not used by the evaluation logic upstream; the policy's `monitor`/`enforce` is.

What the gateway does about it:

- Body: only `PropTradeRequest` fields are accepted (8 KB max, scalars only). Any field name containing
  `order`, `submit`, `execute`, `arm` or `commit` → 422. `mode` other than `paper` (or absent) → 422.
  Unknown fields, a `source` other than `bull-desk-tickerbull`, or another `schema_version` → 422.
- Forced on the upstream payload: `mode: "paper"`, `commit_intent: false`, `source: "bull-desk-tickerbull"`,
  `schema_version: "bullwarden.prop-trade.v1"`. `intent_id` (`bull-desk-tickerbull-eval-<uuid>`) and
  `intent_fingerprint` (sha256 of the payload, including the filled equity) are filled when missing.
- Equity: see side effect 2. Never from the caller, always from the recorded `account_state`.
- Before evaluating it reads `GET /v1/accounts/<id>` and refuses (409 `evaluate_refused_non_monitor_account`)
  unless `policy.mode == "monitor"`, so a probe cannot lock an account. `PROP_ALLOW_ENFORCE_EVAL=1` disables
  this check (the account read and equity checks still run); only set it if TickerBull should be able to
  trigger an enforce lock.
- The response says `dry_run: true`, `commit_intent: false`, lists `upstream_side_effects` and includes
  `persisted_decision_id`, `equity_used`, `prior_close_equity_used`, `equity_source`, `account_state_age_s`.

## Body fields and errors

`GET /evaluate/schema` (auth, no upstream call) returns `required_fields`, `optional_fields`,
`example` and `notes`. From `bull-warden/main.py`:

- required: `account_id`, `asset_class` (required on `PropTradeRequest`) and every `TradeFields` field
  (`symbol, direction, quantity, entry_price, stop_price, target_price, point_value, setup_type,
  profile_key, rules_version, signal_id, signal_timestamp, account_observed_at`): upstream turns each
  missing one into a `<name>_missing` hard violation.
- optional: `mode` (gateway always sends `paper`), `intent_id`, `intent_fingerprint` (filled when missing),
  `source`, `schema_version` (must match the forced value if sent).
- optional `existing_mini_equivalent` (`TradeFields`, upstream default 0, `ge=0`, finite): your open exposure
  in minis (10 micros = 1 mini); omit if flat. Advisory and caller-supplied: BullWarden adds it to the order's
  mini-equivalent for `max_contracts` only on `micro_equivalence` profiles and does not verify it. Gateway
  accepts a JSON number 0..1000 and forwards it unchanged; bool/string/null/NaN/inf/negative/>1000 → 422
  `{"error":"invalid_field","field":"existing_mini_equivalent"}`.

`example` is a working body with `<account_id>`, `<unique id>` and `<now ISO with timezone>` placeholders
to fill in.

422 bodies name the offending field (printable ASCII, ≤ 40 chars). Field names are not secret.

| error | extra keys |
|---|---|
| `unknown_field` | `field`, `allowed_fields` (sorted list of every accepted name), `hint` (stop is `stop_price`; dollar risk is computed from `entry_price`, `stop_price`, `quantity`, `point_value`; don't send risk/equity/order/submit/commit fields) |
| `forbidden_field` | `field` |
| `equity_not_accepted` | `field`, `hint: "equity is filled by the gateway"` |

Other 422 codes (`mode_not_allowed`, `invalid_field`, ...) are unchanged: `{"ok":false,"error":...}`.

## Logs

One stderr line per request: `prop-desk <METHOD> <route template> <status> <ms>ms`. No query values,
path ids, bodies, or keys.

## Tests

```
python3 -m unittest
```

Offline: a fake BullWarden HTTP server runs inside the test process.
