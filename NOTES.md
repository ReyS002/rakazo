Market proxy work

- Added an optional stdlib gateway proxy with separate keys, startup checks, global sliding
  limits (30/minute, 600/day), kill switch, no-store responses and status-only audit logs.
- Exact route/query validation; only drill/quiz answer submissions may use POST. Bodies are
  schema-checked, capped at 2 KiB and forwarded verbatim. Gateway authorization is constructed
  separately; redirects, error details, malformed JSON and known credential echoes are refused.
- Total upstream deadline is 10 seconds. Responses are capped at 12 KiB before and after
  JSON serialization, returning a complete structured error on overflow.
- Added fake-upstream tests and an unapplied 1,323-character bot instruction appendix.
- No existing deployment files or bot settings were changed.

Validation

- Initial 11 market tests passed. Regression testing exposed an overly broad request classifier;
  corrected it to classify paths only. The 21 market/NRS tests then passed.
- Full relay suite: 117 tests passed with `cd hermes-bridge && python3 -m unittest`.
- Final focused market suite: 16 tests passed, including the additional literal-tab request guard.
- `git diff --check` passed; only the five requested files changed. Appendix marker and
  1,323-character length verified. Changes are uncommitted on feat/market-proxy.

NOT RUN: live gateway calls, deployment, container/service/firewall actions, bot instruction
application, or git push. Only local fake-upstream traffic was used for relay verification.

Contract choices (README became available during implementation):
- Public /market paths map to gateway /v1 paths. No caller-controlled origin or path prefix.
- Both answer endpoints document drill_id; quiz additionally accepts the requested quiz_id alias,
  and both accept id. Signed token punctuation and lengths are supported; bytes remain unchanged.
- Unspecified query ranges are deliberately conservative: seed 0..2147483647, filing/pattern limits
  1..20, dates 1970 through current UTC day. Only documented pattern mark=reclaim is exposed.
- Sector slugs accept lowercase letter segments separated by hyphens, at most 64 characters.
- Gateway payloads may exceed the relay's stricter size/deadline budgets; these return bounded
  errors. In particular, a full history or slow briefing is not guaranteed to fit.
- Enabled routes require an upstream key and origin-only URL. JSON must be an object; error
  envelopes are suppressed even on HTTP 200. Ambiguous POST fields and HTTP framing are rejected.
- DNS/connect work is bounded to 30 workers; cancellation prevents a late connection from sending
  a request after timeout. No redirects or environment proxy settings are used.
