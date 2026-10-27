# TB Futures Coil NR7 — TradingView checklist

**One chart, one indicator, one alert** watches all six front-month micros on the **15 minute** timeframe
(`request.security` per symbol). Default symbol inputs:

| # | Root | Input (TradingView symbol) | Receiver sees `symbol` | Normalized root |
|---|------|----------------------------|------------------------|-----------------|
| 1 | MES  | `CME_MINI:MES1!`   | `MES1!`                | MES |
| 2 | MNQ  | `CME_MINI:MNQ1!`   | `MNQ1!`                | MNQ |
| 3 | MYM  | `CBOT_MINI:MYM1!`  | `MYM1!`                | MYM |
| 4 | M2K  | `CME_MINI:M2K1!`   | `M2K1!`                | M2K |
| 5 | MCL  | `NYMEX:MCL1!`      | `MCL1!`                | MCL |
| 6 | MGC  | `COMEX:MGC1!`      | `MGC1!`                | MGC |

The chart timeframe must be `15` (receiver allowlist `TV_TIMEFRAMES`, default `15`; anything else returns 422).
On any other timeframe the script fires nothing and the table shows a red **Set chart to 15m** cell.

## 1. Add the script (once)

1. TradingView -> bottom panel **Pine Editor** -> **Open** -> **New blank indicator**.
2. Select everything, paste `TB_Futures_Coil_NR7.pine`, **Save** (name `TB Futures Coil NR7`).
3. Check the compiler console shows no errors (copy any error line verbatim if it does).

## 2. One chart

1. Symbol search -> `CME_MINI:MES1!` (any 15m chart works; MES recommended) -> Enter.
2. Timeframe -> **15m**.
3. Indicators -> **My scripts** -> **TB Futures Coil NR7** -> it opens in a pane below the chart.
4. Indicator **Settings (gear)** -> **Inputs**:
   - group **Webhook** -> **Webhook key**: paste the shared key (one of `TV_KEYS`); **Send webhook alerts**: ticked.
   - group **Symbols**: leave the six defaults.
   - group **Rules**: leave defaults (0.004 / 10 / 14 / 10 / 7 / 20 / 200). -> **OK**.

## 3. Verify

- Table (top right of the indicator pane) shows **six rows**: `MES1!`, `MNQ1!`, `MYM1!`, `M2K1!`, `MCL1!`, `MGC1!`,
  each with `coil+NR7` yes/no and a spread in % (e.g. `0.215 %`; green when under Spread max).
  A `—` means no data for that symbol — check the symbol input / data subscription.
- Last row: `Webhook` **armed** (green). `off` = key empty or alerts unticked. `Set chart to 15m` = wrong timeframe.

## 4. One alert

**Alerts** (clock icon, right toolbar) -> **Create alert** (`+`):

- **Condition**: `TB Futures Coil NR7` -> **Any alert() function call**.
- **Trigger**: whatever is shown — irrelevant for `alert()` calls (the script fires once per confirmed bar).
- **Expiration**: open-ended / maximum.
- **Alert name**: `TB Coil 6x 15`.
- **Message**: leave the default — it is not used; `alert()` sends its own JSON.
- **Notifications** tab -> tick **Webhook URL** -> `https://desk.72.62.169.3.nip.io/tv-futures/alert`.
- **Create**.

TradingView allows several `alert()` calls in one bar: if 2+ symbols fire on the same bar close, each is its own
webhook (own JSON, own `symbol`). The alert snapshots the inputs at creation: after changing the key, symbols or
rules, **edit and re-save the alert** (or delete and re-create it).

## 5. Test (optional, then revert)

1. Settings -> Inputs -> **Spread max** = `0.05` -> OK -> edit the alert and **Save** it again (so it picks up the input).
2. Wait for the next 15m bar close during Globex hours. Symbols that pass the loosened coil + NR7 fire webhooks.
3. Expect: TradingView alert log shows the fires and the receiver logs them (`/run/tv/alerts.jsonl`).
   **Note:** the receiver re-checks the coil with its own fixed `SPREAD_MAX = 0.004`, so a fire that only passes because
   of the 0.05 setting is answered `200 ignored not_coil_nr7` and does **not** reach TickerBull. A TickerBull message
   appears only if that symbol also meets the real 0.004 rule. The test proves the TradingView -> receiver path;
   check `alerts.jsonl` for the entries.
4. Set **Spread max** back to `0.004` -> OK -> edit the alert and **Save** it again. Table: spreads green only under 0.400 %.

## Receiver gates (why a fire may not become a message)

- **Session**: CME Globex only, judged on the bar open time (`bar_time`): closed Fri 17:00 ET -> Sun 18:00 ET and daily
  17:00–18:00 ET. Holidays not modelled. Outside -> `200 ignored session_closed`.
- **Cooldown**: 30 min per root (MES, MNQ, ...) after a forward -> `200 ignored cooldown`.
- **Dedupe** (same root + tf + bar_time, 48 h) -> `duplicate`; **daily cap** 300 -> `daily_cap`.

## Safety

- Never publish or share a chart / layout / alert screenshot while the key is set (inputs are visible to anyone with the layout).
- To stop everything immediately without touching TradingView: `touch /opt/stacks/bull-desk/tv-futures-run/KILL_TV` on the VPS (every route 503).
- Rotating the key: add the new key to `TV_KEYS` (comma list), restart, paste the new key, re-save the one alert, then remove the old key.
- TradingView may disable an alert after repeated webhook failures; the receiver returns 200 for gated/ignored fires so that only real errors (403/422/429/502/503) count.
