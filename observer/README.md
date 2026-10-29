# observer: oversight feed collector

Trusted, host-side, read-only. Writes twelve sanitized JSON snapshots for the relay
(`GET` with bearer key; mounted read-only at `/run/bridge/observe/`). Spec: `observe-spec.md` (oversight feed v1).

```sh
python3 -I collect.py [--only health,scanner,paper,projects,vault,logs,narrow,nrs,journals,phasec,robinhood,bars] [--out DIR] [--once]
# default out: /opt/stacks/bull-desk/hermes-bridge-run/observe
python3 -m unittest discover -s /opt/stacks/bull-desk/observer/tests
```

`--once` is accepted for timer units; the collector always does one pass and exits (systemd does the scheduling).

## Output

`<name>.json` for `health scanner paper projects vault logs narrow nrs journals phasec robinhood bars` only. Each file is
`{"name","generated_at","ok","error","data"}`, max 200 KB (`narrow`, `nrs`, `phasec`, `robinhood`: 150 KB; `journals`: 8 MB, `bars`: 2 MB, never served whole — the relay slices them) (longest lists are halved until it fits),
written atomically (tmp + `os.replace`, fsync, 0644; dir 0755). Leftover `.tmp` files older than 10 min are removed.
`.<name>.lock` files (0600) sit next to the snapshots; the relay only serves the twelve fixed names (`OBSERVE_NAMES` in hermes-bridge/relay.py).
`collect.run` passes each section a `read_prev(name)` that reads the previous `<name>.json` (O_NOFOLLOW, <= 8 MB, the largest section cap) for diffs.

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
| nrs | `/opt/stacks/ops-status/nrs-{stock,forex,crypto}-latest.json` (symlink followed only to a regular file in the same dir; the `nrs-*-export` jobs rewrite it every 15 min; schema 1.0 `nrs_<market>_redacted`), <= 4 MB each. Per market: `generated_at` (`ts_utc`), `as_of`, `age_s`, `file_mtime`, `watchlist_scan_date`, `trading_mode`, `export_error`, `counts`, `heartbeats` (last_success_at, detail), `watchlist` + `recent_triggers` (newest first) with every scalar field the export has (symbol, scores, NR7, ATR%, velez location, reasons, direction, rvol, breakout level, trigger time ...), <= 60 each; keys with url/chart/link/href/account/host, URL values and nested objects dropped; `_24h` keys spelled `_24_hours` (scrubber). `new_since_prev` = watchlist symbols / triggers (symbol, triggered_at, direction) not in the previous nrs.json (null on first run or when that market was not ok). `stale` per market: > 26 h old on a weekday (ET). A missing/malformed market -> that market `ok:false` + `reason`; all three -> section ok:false | none |
| journals | `/opt/arena/state/exports/paper-fills-<YYYYMMDD>.json` (schema 2.0) and `*-decisions-<YYYYMMDD>.json` (`unchained-decisions` today) for today and the previous weekday (ET; no holiday calendar), <= 16 MB each. Per bot (keyed by the export's `label`, plus a stable `code` from the one table `journals.BOTS`: Unchained=PYPV, Bull Pilot=JI68, Velez Trading=TYWQ, Swing (Velez swing)=RH5Y, BullMatrix (bull-swarm)=7E3U; unknown label -> `code: null`; decisions blocks carry `label` + `code` too): fills (time, symbol, side, qty, price, fill_type, position_after), stop orders (type, qty, stop/limit/trail, status, tif, submitted/filled/cancelled), open positions, closed trades, <= 1000 rows each; decisions (time, symbol, setup, timeframe, side, decision, reason, entry, stop, qty, vol_ratio, location) <= 3000 per bot-day, in time order (over the cap every non-`passed` row is kept and the oldest `passed` rows go first); summary counters. Every list has `lists.<name> = {total_rows, kept_rows, truncated}` next to it (old count fields kept). `data.catalog.<today|prev_weekday> = {date, bots:[{label, code, fills, stop_orders, open_positions, closed_trades, decisions, truncated:[lists], first_time_et, last_time_et}]}` (kept-row counts; decision time range in ET). Row `time` values are UTC (`...Z`). Dropped: `bars_2m`, `bars_2m_meta`, account keys/`account`, order/client/parent ids. `missing` lists absent files; `stale` = previous weekday's paper-fills missing, or today's missing after 16:30 ET on a weekday. Any `<bot>-decisions-*.json` with the same shape is picked up. **Velez Trading (TYWQ), Bull Pilot (JI68), Velez swing (RH5Y)** have no export; their decisions come from each bot's own SQLite `decisions` table (`journals.DB_SOURCES`: `/opt/stacks/{velez-trading-bot,bull-pilot,velez-swing-bot}/data/trading_bull_desk.sqlite3`), keyed `decisions.{velez-trading,bull-pilot,velez-swing}` with `source: "sqlite"` and `day_utc: [start, end)` (the ET calendar day 00:00–24:00 via zoneinfo America/New_York; 23 h / 25 h on DST days). Read in place, never copied: `file:...?mode=ro` + `PRAGMA query_only`, 3 s busy timeout, a progress handler stops each query after 8 s; a WAL db is only opened while the writer's own `-wal`/`-shm` exist (a read-only open would create them), else `ok:false`. Only `id, timestamp, status, reason, symbol, side, play, qty, order_type, entry_price, stop_price, take_profit_price, timeframe, location, max_dollar_risk` are selected (indexed `timestamp` range widened a day each side, exact ET-day cut on the parsed timestamp); `order_payload_json`, `broker_response_json`, `snapshot_json`, `alert_ref` and `id` never leave the db. Rows: `{time (UTC Z), time_et (HH:MM:SS), symbol, decision (= status), reason (scrubbed, <= 300), side, play, qty, order_type, entry, stop, target, timeframe, location, max_dollar_risk}`, chronological, first 3000 per bot-day kept (`lists.decisions` / `decisions_total` / `decisions_shown`), `unparsed_timestamps` counted and dropped. No db / empty file / no `decisions` table -> a `"<path>: <reason>"` entry in `missing`; locked, timed out, malformed or missing columns -> `ok:false` block + section error. A bot with its own `<bot>-decisions-*.json` that day uses the export instead. Real Oct 26 + 27: TYWQ 2867 + 2585, JI68 1694 + 1452, RH5Y 790 + 650 rows, ~3.1 MB, + ~790 KB Unchained; a heavier pair of days would hit the 8 MB cap, the longest list gets halved and the relay reports `truncated_at_source: true`. Read it through the relay: `GET /observe/journals` (index) then `?day=&bot=&section=` slices, see hermes-bridge/README.md | none |
| bars | nothing of its own: the symbol list comes from the snapshots already in the output dir (`read_prev`): today's `nrs.json` stock `recent_triggers`, `scanner.json` stock `recent_fires`, `journals.json` fills, closed trades and decisions with an `entry`, each only if it falls on today or the previous weekday (ET); plain stock symbols only (`[A-Z]{1,5}(.[A-Z]{1,2})?`, so forex pairs / crypto never match); newest reference first, capped at 40 (`dropped_for_cap`). Paper key pair: found exactly like `paper` (`paper.alpaca_accounts`, first paper account), sent only as request headers | `GET https://data.alpaca.markets/v2/stocks/bars?symbols=..&timeframe=2Min|5Min&start&end&feed&adjustment=raw&limit=10000` (paged via `next_page_token`, <= 10 pages), one call per day x timeframe (4 + 1 feed probe). Feed: `sip` first, `iex` if the plan refuses SIP (403/422; normal for recent data on a free plan), same order as `/opt/scripts/tba/export_paper_fills.py`; the feed used is recorded. Window: 09:30-16:00 ET of the previous weekday (no holiday calendar: a holiday just has 0 bars) and of today (up to now; skipped before 09:30 and on weekends); extended-hours and still-open bars dropped. Output: `{source, feed, as_of, stale, error, inputs, timeframes, days: {today|prev_weekday: {date, session}}, symbols, symbol_sources, dropped_for_cap, dropped_for_size, latest_bar_et, bar_format, counts: {SYM: {day: {2m, 5m}}}, bars: {SYM: {day: {2m|5m: [["HH:MM", o, h, l, c, v], ...]}}}}`. Over 1.9 MB the symbols with the oldest reference are dropped first (`dropped_for_size`); real 22 symbols = ~0.3 MB. `stale` = a request failed, or the session is open and the newest bar is > 20 min old. A failed request leaves its lists empty and names itself in `error` (`today 2m: HttpError (HTTP 500)`); all failed / no paper key -> `ok:false`. Bars are never filled in or carried over. Read it through the relay: `GET /observe/bars` then `?symbol=&tf=&day=` |
| phasec | TB-VSL-009: `/opt/stacks/ops-status/bullpilot-closed-trades-latest.json` (Bull Pilot reconciled closed paper trades), `/opt/stacks/bull-pilot/bot/reports/evaluation/universe_a1_watchlist_equity_buckets_v1.yaml` (A1 watchlist_all 25 = 13 mega + 12 mid_smaller), newest `paper-fills-*.json` (coverage cross-check: bull-pilot `fill_history_count` == export `fill_count`). Rule text: Bull Pen "TB-VSL-009 Phase C weekday digest" routine in `/root/grok-bot-instructions/bull-pen-strategies.md` (the Lab `TB-VSL-009*.md` files are not on this box; `found.lab_md` lists any that appear). Counts complete closes (exit time+price+pnl), setup `elephant_bar`, entry date ET >= 2026-09-22, symbol in A1 25, SPCX excluded (fail-closed), timeframe daily when the export states one (`timeframe_unverified_n` otherwise). Output: n, net_usd, profit_factor, exp_r, max_dd_usd (trade-PnL path), max_dd_pct (null: no equity base), mega/mid_smaller buckets, days_elapsed, gate (n>=50 OR 90 days from 2026-09-22 -> 2026-12-21), alerts_crossed (10/25/40), fail_triggers (net<=0 at n>=30; DD>8 % = not evaluable; PF<1 at n>=40), excluded tallies, last 60 closes, last_updated, stale (> 26 h on a weekday). TB-VSL-014: **ok:false** — no count md on the box and the source account is ambiguous (fab4 is allowlisted on the Bull Pilot swing lane, Bull Pen notes put Fab4 Phase C' closes on Velez Trading, which has no reconciled closed-trade export); `rule_known` carries setup, entry cut-off and the mega 13 from `tb_vsl_014_candidate01_manifest.json`. No numbers are produced for it | none |
| robinhood | nothing on disk (never `/etc/hermes-robinhood-bridge.env`, OAuth store or tokens) | `GET http://127.0.0.1:28188/health` only, no headers, 5 s (`deps.LocalGet`: method must be GET, port+path must be in `LOCAL_ROUTES = {28188: {"/health"}}`, no query, no redirects/proxy; everything else is refused before a request). The bridge (`/root/hermes-ai-router/src/hermes-os/robinhood/bridge.ts`) exposes positions/cash/orders only as bearer-key `POST /v1/tools/<tool>` calls, so the section is always **ok:false** with `reason` + bridge status (reachable, http_status, connected, authenticated, authorization_pending, last_error). `positions`, `cash`, `buying_power`, `open_orders_count` stay null; `account_label` is always `robinhood`. A non-bridge answer on 28188 (shape check) -> ok:false "without the bridge's health shape" |
| logs | tail 40 of the 15 newest `/opt/logs/tba/*.log` (symlinks leaving the dir refused); each line <= 300 chars | `docker logs --tail 40 <name>` for containers named `bull-desk*`, `tba*`, `velez*`, `narrow-range*`, `oanda*` |

Refusals: OANDA -> `{"ok":false,"error":"not practice"}` unless `OANDA_BASE_URL` host is exactly
`api-fxpractice.oanda.com` (missing counts as not practice). Alpaca -> `"not paper"` unless the base URL host is exactly
`paper-api.alpaca.markets`; with no base URL only `PK...` (paper) key ids are accepted. Requests always go to the
fixed paper/practice host constants, never to the URL from the env file.

## Safety

- `deps.check_argv` allows only the exact argv shapes above (no shell, container/unit names regex-checked);
  `deps.HttpGet` only does `GET` to the three hosts (paper-api.alpaca.markets, api-fxpractice.oanda.com, data.alpaca.markets) over https; `deps.LocalGet` only does `GET` to `127.0.0.1` port+path pairs in `LOCAL_ROUTES` (no credentials sent). Sections never touch subprocess/urllib/open directly.
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
| `bull-observe-slow` | projects, vault, narrow, nrs, journals, phasec, robinhood | 15 min |
| `bull-observe-bars` | bars | 5 min, Mon-Fri 09:30-16:25 ET (+ once after boot) |

nrs/journals stay out of the 5-min paper unit: their sources change every 15 min (nrs exports) or once a day
(paper-fills ~16:23 ET, closed-trades ~16:28 ET, decisions 04:10 ET), so the 15-min slow timer already has them by
~16:45 ET, and the paper unit is the only one with outside network. The slow unit allows AF_INET to localhost only
(`IPAddressDeny=any` + `IPAddressAllow=localhost`) for robinhood's loopback GET.

Install (Hermes, after review):

```sh
install -d -m 0755 /opt/stacks/bull-desk/hermes-bridge-run/observe
cp /opt/stacks/bull-desk/observer/systemd/bull-observe-*.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl start bull-observe-fast.service   # one pass, then inspect the JSON
systemctl enable --now bull-observe-fast.timer bull-observe-paper.timer bull-observe-slow.timer
```

### Deploy `bars` + the sliced `/observe/nrs` (Hermes)

Files: `observer/sections/bars.py`, `observer/{collect,deps}.py`, `observer/systemd/bull-observe-bars.{service,timer}`,
`hermes-bridge/relay.py` (the relay code is bind-mounted read-only, so a container restart picks it up).

```sh
cd /opt/stacks/bull-desk
python3 -m unittest discover -s observer/tests                      # expect OK
(cd hermes-bridge && python3 -m unittest)                            # expect OK
systemd-analyze calendar --iterations=3 'Mon..Fri 09:30/5 America/New_York'
cp observer/systemd/bull-observe-bars.service observer/systemd/bull-observe-bars.timer /etc/systemd/system/
systemctl daemon-reload
systemctl start bull-observe-bars.service                            # one pass (needs nrs/scanner/journals.json)
journalctl -u bull-observe-bars.service -n 5 --no-pager              # "observe bars: ok <ms>"
python3 -c "import json;d=json.load(open('hermes-bridge-run/observe/bars.json'));print(d['ok'],d['error'],d['data']['feed'],len(d['data']['symbols']))"
systemctl enable --now bull-observe-bars.timer
docker compose --env-file .env -f infra/compose/docker-compose.yml -f docker-compose.vps.yml restart hermes-bridge
```

Smoke test through the relay (Bearer = an `OBS_KEYS` entry): `GET /hermes-bridge/observe` lists `bars`;
`GET /hermes-bridge/observe/nrs` is now the small index (old whole snapshot: `?full=1`);
`GET /hermes-bridge/observe/bars?symbol=<one of symbols>&tf=2m&day=prev_weekday`.
Roll back: `systemctl disable --now bull-observe-bars.timer`, restore `relay.py`, restart the container.
