# observer: oversight feed collector

Trusted, host-side, read-only. Writes seven sanitized JSON snapshots for the relay
(`GET` with bearer key; mounted read-only at `/run/bridge/observe/`). Spec: `observe-spec.md` (oversight feed v1).

```sh
python3 -I collect.py [--only health,scanner,paper,projects,vault,logs,narrow] [--out DIR] [--once]
# default out: /opt/stacks/bull-desk/hermes-bridge-run/observe
python3 -m unittest discover -s /opt/stacks/bull-desk/observer/tests
```

`--once` is accepted for timer units; the collector always does one pass and exits (systemd does the scheduling).

## Output

`<name>.json` for `health scanner paper projects vault logs narrow` only. Each file is
`{"name","generated_at","ok","error","data"}`, max 200 KB (`narrow`: 150 KB) (longest lists are halved until it fits),
written atomically (tmp + `os.replace`, fsync, 0644; dir 0755). Leftover `.tmp` files older than 10 min are removed.
`.<name>.lock` files (0600) sit next to the snapshots; the relay only serves the seven fixed names (`narrow` added to `OBSERVE_NAMES` in hermes-bridge/relay.py).

- A section that raises -> `ok:false`, short scrubbed `error`, `data:{}`. Other sections still run.
- Partial failures (one paper account, docker down, ...) -> `ok:true` with a joined `error`; per-account `ok`/`error`.
- Run budget 60 s (each subprocess/HTTP call is clipped to what is left; hard stop at 75 s). A section that runs
  out of budget is **not written**: the previous snapshot stays and its `generated_at` shows its age.
- One lock per section: two timers can run different sections side by side; the same section never overlaps
  (the second run reports `busy` and writes nothing).

## What each section reads

| section | reads | commands / HTTP |
|---|---|---|
| health | `/proc/loadavg`, `/proc/meminfo`, `/proc/uptime`; last line of the 20 newest `/opt/logs/tba/*.log` | `docker ps -a --format {{.Names}}`, `docker inspect --type container --format <fixed tpl>` (name, image, status, health, start, restarts only; never env/labels), `systemctl show --no-pager --property=Id,LoadState,ActiveState,SubState` for hermes-gateway, hermes-robinhood-bridge, docker, ssh, cron + `bull-*`, `tba-*`, `df -P -k -x tmpfs ...` |
| scanner | `/opt/stacks/ops-status/scanner-fires-latest.jsonl` (the same feed and parsing as `tba-ro-status fires`: last 4 MB, kinds stock/forex/crypto, same field cleaner), last 50 fires oldest->newest; mtime of scanner logs in `/opt/logs/tba` (`source: log_mtime`), except `fires-export` = mtime of the fires feed (`source: output_file_mtime`, `note` if missing); retired `narrow-gap-shadow-export` kept with `retired: true` + `note` — exclude retired rows from staleness checks | push status: calls `grokbot_push.status()` in-process (same output as `grokbot_push.py --status`) and keeps only `configured,last_success,consec_fail,sent_today,dead` per bot name |
| paper | Alpaca: the `export_paper_fills.py` env file list + any `/opt/stacks/*/.env` with its key aliases (deduped by key id, label = stack dir). OANDA: `/opt/stacks/oanda-bridge/.env` | GET only, 10 s timeout, no retries, no redirects, no proxy. Alpaca `/v2/account`, `/v2/positions`, `/v2/account/activities/FILL?date=<today ET>`, `/v2/orders?status=open` on `paper-api.alpaca.markets` only. OANDA `summary`, `openTrades`, `pendingOrders`, `transactions/idrange` (last 20) on `api-fxpractice.oanda.com` only |
| projects | `.git` presence in `/opt/scripts/tba`, `/opt/stacks/*`, `/root/{dendrix-ai,hermes-ai-router,rakazo,trading-bull-ai}` | `git -c core.fsmonitor=false -c safe.directory=* --no-optional-locks` + `status --porcelain` / `rev-parse --abbrev-ref HEAD` / `rev-parse --short=12 HEAD` / `log -1 --format=%ct%x00%s` / `rev-list --left-right --count @{u}...HEAD`, 5 s each, errors -> null |
| vault | `/opt/stacks/tba-brain/**/*.md`: first 4 KB of each (skips dotdirs, symlinks, files > 300 KB, paths containing secret/credential/token/password/.env); 400 newest | none |
| narrow | Bull Pilot `/opt/stacks/bull-pilot/data/shadow-v2/long_narrow_gap_orb_r2_v2.sqlite3` (SQLite WAL), opened `file:...?mode=ro&immutable=0` + `PRAGMA query_only` only while the writer's `-wal`/`-shm` exist (a ro open would otherwise create them); else, or if locked, db + `-wal` are copied into a private temp dir and the copy is read. Output: `schema` (table -> columns), `lane_state` (latest heartbeat), `counts` (total/today/last_7d by ET `session_date`), `latest_by_symbol` (latest signal per symbol: gap_pct, opening_range_atr, rvol, `trigger` = opening-range high, entry, `stop`, guardrail, exit, R), `recent` (30 newest signals+blocked, newest first), `as_of`, `source`, `stale` (as_of > 1 weekday behind today ET). Missing db / not sqlite / unexpected schema / locked and copy failed -> `ok:false` + `data.reason`. No quantity/$ P&L exported. The NARROW/WIDE StateMachine state is not persisted in this db (`per_symbol_narrow_wide_in_source:false`) | none |
| logs | tail 40 of the 15 newest `/opt/logs/tba/*.log` (symlinks leaving the dir refused); each line <= 300 chars | `docker logs --tail 40 <name>` for containers named `bull-desk*`, `tba*`, `velez*`, `narrow-range*`, `oanda*` |

Refusals: OANDA -> `{"ok":false,"error":"not practice"}` unless `OANDA_BASE_URL` host is exactly
`api-fxpractice.oanda.com` (missing counts as not practice). Alpaca -> `"not paper"` unless the base URL host is exactly
`paper-api.alpaca.markets`; with no base URL only `PK...` (paper) key ids are accepted. Requests always go to the
fixed paper/practice host constants, never to the URL from the env file.

## Safety

- `deps.check_argv` allows only the exact argv shapes above (no shell, container/unit names regex-checked);
  `deps.HttpGet` only does `GET` to the two hosts over https. Sections never touch subprocess/urllib/open directly.
- `scrub.py` runs on every string (values and keys) before writing: token-like runs, Bearer/Authorization,
  `*KEY*/*TOKEN*/*SECRET*/*PASSWORD*/... = value`, URLs with `?`/`/webhooks/`/`key=`/`token=`/`secret=`/userinfo,
  PEM private keys, Alpaca key ids, 9+ digit runs -> `[NUM]`. Text is scrubbed before it is truncated. After scrubbing,
  `find_leaks()` re-checks the whole envelope; any hit drops the section to `ok:false`, `data:{}`.
  Deviations from the spec text: the 9+ digit rule applies to every string, not only log tails (numbers stay numbers);
  dash/slash word slugs such as `narrow-gap-v2-shadow-export` are not treated as tokens; RFC3339 timestamps are
  normalised to whole seconds so nanosecond fractions are not turned into `[NUM]`.
- Known limit: `git status` runs the repo's own configured clean filters on stat-dirty files. All repos listed are
  the owner's; do not add untrusted repos to the list.

## Schedule (examples in `systemd/`, not installed)

| unit | sections | every |
|---|---|---|
| `bull-observe-fast` | health, scanner, logs | 2 min |
| `bull-observe-paper` | paper | 5 min |
| `bull-observe-slow` | projects, vault, narrow | 15 min |

Install (Hermes, after review):

```sh
install -d -m 0755 /opt/stacks/bull-desk/hermes-bridge-run/observe
cp /opt/stacks/bull-desk/observer/systemd/bull-observe-*.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl start bull-observe-fast.service   # one pass, then inspect the JSON
systemctl enable --now bull-observe-fast.timer bull-observe-paper.timer bull-observe-slow.timer
```
