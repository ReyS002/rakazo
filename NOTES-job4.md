# JOB 4 — relay: compact `edu/*` reads inside the 12 KB cap

Worktree `/root/wt/bd-market2`, branch `feat/market-proxy-2` off `vps-ops`. Only
`hermes-bridge/{relay.py,test_relay.py,README.md}` touched.

## Changed

- `MARKET_PARAMS`: `edu/drill` and `edu/quiz` accept `bars` (`range(20,121)`) and `compact` (`("1",)`);
  `edu/brief` accepts `compact`; `edu/patterns` accepts `bars` and `compact`. Same validation style as the
  existing allowlist (`range` → digits-only membership, tuple → exact value), so `bars=19`, `bars=121`,
  `bars=40.0`, `bars=+40`, `bars=`, `compact=0`, `compact=true`, duplicates and `compact` on non-`edu`
  routes all return 400 without echoing input.
- `MARKET_EDU_DEFAULTS` + a GET-only block at the end of `market_target`: when the caller omits them the
  relay appends `compact=1` to every `edu/*` GET and `bars=40` to `edu/drill`/`edu/quiz` (so an omitted
  `bars` is well under the requested ≤60). Explicit caller values always win. POST answer routes and
  non-`edu` reads are untouched.
- README market section: new allowlist rows, the defaulting rule, the compact response shape, and the
  note that compact cannot be disabled through the relay.

## Tests

`cd hermes-bridge && python3 -m unittest` → **119 tests, OK** (138 s; no flake this run).

- `test_edu_compact_defaults` (new): expected query for `edu/drill`, `edu/quiz?symbol=ABC`,
  `edu/drill?bars=120` (explicit value kept), `edu/quiz?compact=1`, `edu/brief`, `edu/patterns?limit=5`;
  16 rejected `bars`/`compact` variants via direct `market_target` calls (keeps the HTTP suite under the
  30/minute limiter); `quote/ABC` query untouched; `edu/drill/answer` target gains no parameters.
- `test_allowlist_and_auth_forwarding` and `test_documented_queries` updated for the defaults and extended
  with `bars`/`compact` happy paths.

## Verified against the gateway

Measured on a private gateway instance (port 18790) with the matching gateway change: compact drill
(`bars=40`) 3033 B, compact brief 2707 B, compact patterns 1321 B — all inside `MAX_MARKET_RESPONSE`
(12288 B), versus ~15 KB for the old default drill that returned 413.

## NOT RUN / not done

- No push; local commit only. No relay process was started or restarted on this host; the running bridge
  still serves `vps-ops`.
- Not exercised end-to-end through the real relay against the real gateway (fake upstream only in tests).
- `bars` on `edu/patterns` is allowlisted for uniformity; the gateway validates but ignores it.
- The relay does not inspect compact response bodies — it still only enforces the byte cap.
