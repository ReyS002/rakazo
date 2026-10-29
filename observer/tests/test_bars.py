import json
import os
import shutil
import unittest
import urllib.parse
from unittest import mock

import fakes
import collect
import scrub
from sections import bars

MON = fakes.NOW  # Mon 2026-10-26 11:00 ET (session open); prev weekday Fri 2026-10-23
SAT = fakes.NOW + 5 * 86400  # Sat 2026-10-31 11:00 ET


def env(name, data):
    return {"name": name, "generated_at": "2026-10-26T14:59:00Z", "ok": True, "error": None, "data": data}


def snapshots(triggers=(), fires=(), fills=(), closed=(), decisions=(), day="2026-10-26"):
    """Minimal nrs/scanner/journals envelopes as the collector writes them."""
    return {
        "nrs": env("nrs", {"markets": {"stock": {"ok": True, "recent_triggers": [
            {"symbol": s, "triggered_at": t, "direction": "long"} for s, t in triggers]}}}),
        "scanner": env("scanner", {"recent_fires": [{"time": t, "kind": k, "symbol": s} for k, s, t in fires]}),
        "journals": env("journals", {"days": {"today": {
            "date": day,
            "paper_fills": {"ok": True, "bots": {"Unchained": {"fills": [{"time": t, "symbol": s} for s, t in fills]}}},
            "decisions": {"unchained": {"ok": True, "closed_trades": [
                {"symbol": s, "entry_time": t, "exit_time": t} for s, t in closed],
                "decisions": [{"time": t, "symbol": s, "entry": e} for s, t, e in decisions]}}}}}),
    }


class Bars(unittest.TestCase):
    def setUp(self):
        self.box = fakes.Box()

    def tearDown(self):
        self.box.close()

    def deps(self, snaps=None, now=MON, **over):
        snaps = snapshots(triggers=[("AAPL", "2026-10-26T14:30:00Z")]) if snaps is None else snaps
        return self.box.deps(read_prev=lambda n: snaps.get(n), now=lambda: now, **over)

    def bars_calls(self):
        return [urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)
                for _, u, _, _ in self.box.opener.calls if "data.alpaca.markets" in u]

    def test_full_run_contract(self):
        res = collect.run(["nrs", "scanner", "journals", "bars"], self.box.out, self.box.deps(), log=fakes.silent)
        self.assertEqual(res["bars"], "ok")
        raw = self.box.outputs()["bars.json"]
        self.assertLess(len(raw), collect.SECTION_MAX_BYTES["bars"])
        e = json.loads(raw)
        d = e["data"]
        self.assertTrue(e["ok"])
        self.assertIsNone(e["error"])
        self.assertEqual(d["feed"], "iex")  # fake plan refuses SIP -> fallback, like export_paper_fills.py
        self.assertEqual(d["symbols"], ["AAPL", "AMZN"])  # AAPL: nrs + scanner today; AMZN: Friday journals
        self.assertEqual(d["symbol_sources"], {"AAPL": ["nrs", "scanner"], "AMZN": ["journals"]})
        self.assertEqual(d["inputs"], {"nrs": "ok", "scanner": "ok", "journals": "ok"})
        self.assertEqual(d["days"], {"today": {"date": "2026-10-26", "session": "open"},
                                     "prev_weekday": {"date": "2026-10-23", "session": "closed"}})
        # today 09:30..10:58 (now 11:00), Friday full session; extended-hours and future bars dropped
        self.assertEqual(d["counts"]["AAPL"], {"today": {"2m": 45, "5m": 18}, "prev_weekday": {"2m": 195, "5m": 78}})
        self.assertEqual(d["latest_bar_et"], "2026-10-26 10:58")
        self.assertFalse(d["stale"])
        rows = d["bars"]["AAPL"]["prev_weekday"]["5m"]
        self.assertEqual((rows[0][0], rows[-1][0]), ("09:30", "15:55"))
        self.assertEqual(len(rows[0]), 6)
        self.assertIsInstance(rows[0][5], int)
        self.assertEqual(d["bars"]["AAPL"]["today"]["2m"][-1][0], "10:58")
        self.assertEqual(scrub.find_leaks(e), [])

    def test_in_progress_bars_dropped(self):
        data = bars.collect(self.deps(now=MON + 150))  # 11:02:30 ET: 2m 11:00 closed, 5m 11:00 still open
        self.assertEqual(data["bars"]["AAPL"]["today"]["2m"][-1][0], "11:00")
        self.assertEqual(data["bars"]["AAPL"]["today"]["5m"][-1][0], "10:55")

    def test_no_key_or_secret_anywhere(self):
        collect.run(["nrs", "scanner", "journals", "bars"], self.box.out, self.box.deps(), log=fakes.silent)
        raw = self.box.outputs()["bars.json"]
        for s in fakes.SECRETS:
            self.assertNotIn(s, raw)
        for _, url, headers, _ in self.box.opener.calls:
            if "data.alpaca.markets" in url:
                self.assertEqual(headers["Apca-api-key-id"], fakes.ALPACA_KID)  # header only
                for s in fakes.SECRETS:
                    self.assertNotIn(s, url)

    def test_sip_used_when_plan_allows_it(self):
        self.box.opener.sip = "ok"
        data = bars.collect(self.deps())
        self.assertEqual(data["feed"], "sip")
        self.assertTrue(all(q["feed"] == ["sip"] for q in self.bars_calls()))

    def test_request_shape_and_paging(self):
        with mock.patch.object(fakes, "BARS_PAGE", 50):
            data = bars.collect(self.deps())
        self.assertEqual(data["counts"]["AAPL"]["prev_weekday"], {"2m": 195, "5m": 78})  # pages joined
        calls = self.bars_calls()
        ok = [q for q in calls if q["feed"] == ["iex"]]
        self.assertEqual([q["feed"] for q in calls if q["feed"] == ["sip"]], [["sip"]])  # one refused probe
        for q in ok:
            self.assertEqual((q["symbols"], q["adjustment"]), (["AAPL"], ["raw"]))
            self.assertIn(q["timeframe"], (["2Min"], ["5Min"]))
        starts = {(q["start"][0], q["end"][0]) for q in ok}
        self.assertEqual(starts, {("2026-10-26T13:30:00Z", "2026-10-26T15:00:00Z"),
                                  ("2026-10-23T13:30:00Z", "2026-10-23T20:00:00Z")})
        self.assertTrue(any("page_token" in q for q in ok))

    def test_symbol_window_and_filters(self):
        snaps = snapshots(
            triggers=[("NVDA", "2026-10-26T14:00:00Z"), ("OLD", "2026-10-23T14:00:00Z")],  # nrs: today only
            fires=[("stock", "QQQ", "2026-10-23T15:00:00Z"), ("forex", "EURUSD", "2026-10-26T14:00:00Z"),
                   ("stock", "STALE", "2026-10-21T15:00:00Z"), ("crypto", "BTC/USD", "2026-10-26T14:00:00Z")],
            fills=[("TSLA", "2026-10-26T14:10:00Z"), ("usdjpy", "2026-10-26T14:10:00Z")],
            closed=[("OSCR", "2026-10-23T18:00:00Z")],
            decisions=[("AMD", "2026-10-26T14:20:00Z", 101.5), ("SKIP", "2026-10-26T14:21:00Z", None)])
        data = bars.collect(self.deps(snaps))
        self.assertEqual(data["symbols"], ["AMD", "TSLA", "NVDA", "OSCR", "QQQ"])  # newest reference first

    def test_symbol_cap_keeps_newest(self):
        trig = [(f"S{chr(65 + i // 26)}{chr(65 + i % 26)}", f"2026-10-26T14:{i:02d}:00Z") for i in range(55)]
        data = bars.collect(self.deps(snapshots(triggers=trig)))
        self.assertEqual(len(data["symbols"]), bars.MAX_SYMBOLS)
        self.assertEqual(data["symbols"][0], trig[-1][0])
        self.assertEqual(len(data["dropped_for_cap"]), 15)
        self.assertEqual(data["dropped_for_cap"][-1], trig[0][0])
        self.assertEqual(sorted(data["bars"]), sorted(data["symbols"]))

    def test_size_cap_drops_oldest_symbols(self):
        trig = [("AAA", "2026-10-26T14:01:00Z"), ("BBB", "2026-10-26T14:02:00Z"), ("CCC", "2026-10-26T14:03:00Z")]
        with mock.patch.object(bars, "MAX_BYTES", 25 * 1024):
            data = bars.collect(self.deps(snapshots(triggers=trig)))
        self.assertEqual(data["symbols"], ["CCC"])
        self.assertEqual(data["dropped_for_size"], ["AAA", "BBB"])
        self.assertEqual(list(data["bars"]), ["CCC"])
        self.assertLessEqual(len(json.dumps(data, separators=(",", ":"))), 25 * 1024)

    def test_api_failure_is_an_error_never_bars(self):
        self.box.opener.fail_substr = ("data.alpaca.markets",)
        data, ok, error = bars.collect(self.deps())
        self.assertFalse(ok)
        self.assertIn("HTTP 500", error)
        self.assertEqual(data["error"], error)
        self.assertTrue(data["stale"])
        self.assertEqual(data["counts"]["AAPL"], {"today": {"2m": 0, "5m": 0}, "prev_weekday": {"2m": 0, "5m": 0}})
        self.assertIsNone(data["feed"])
        for s in fakes.SECRETS:
            self.assertNotIn(s, json.dumps(data))
        # through the collector (inputs read from the output dir): ok:false envelope, error named, still no key
        os.makedirs(self.box.out, exist_ok=True)
        for name, snap in snapshots(triggers=[("AAPL", "2026-10-26T14:30:00Z")]).items():
            with open(os.path.join(self.box.out, name + ".json"), "w") as fh:
                json.dump(snap, fh)
        collect.run(["bars"], self.box.out, self.box.deps(), log=fakes.silent)
        raw = self.box.outputs()["bars.json"]
        for s in fakes.SECRETS:
            self.assertNotIn(s, raw)
        e = json.loads(raw)
        self.assertFalse(e["ok"])
        self.assertIn("HTTP 500", e["error"])

    def test_no_paper_key(self):
        for stack in ("velez-trading-bot", "bull-pilot", "bull-live"):
            shutil.rmtree(os.path.join(self.box.root, "opt/stacks", stack), ignore_errors=True)
        data, ok, error = bars.collect(self.deps())
        self.assertFalse(ok)
        self.assertEqual(error, "no paper Alpaca key in the stack env files")
        self.assertEqual(self.bars_calls(), [])

    def test_no_symbols_is_ok_and_makes_no_request(self):
        data = bars.collect(self.deps(snapshots()))
        self.assertEqual((data["symbols"], data["bars"], data["stale"], data["error"]), ([], {}, False, None))
        self.assertEqual(self.bars_calls(), [])

    def test_missing_inputs_listed(self):
        data = bars.collect(self.box.deps(read_prev=lambda n: None, now=lambda: MON))
        self.assertEqual(data["inputs"], {"nrs": "missing", "scanner": "missing", "journals": "missing"})

    def test_stale_when_open_session_has_no_recent_bar(self):
        with mock.patch.object(fakes, "BARS_PAGE", 10 ** 6), \
                mock.patch.object(bars, "bar_row", lambda *a: None):
            data = bars.collect(self.deps())
        self.assertTrue(data["stale"])
        self.assertIsNone(data["error"])

    def test_weekend_fetches_previous_weekday_only(self):
        snaps = snapshots(fires=[("stock", "SPY", "2026-10-30T15:00:00Z")])
        data = bars.collect(self.deps(snaps, now=SAT))
        self.assertEqual(data["days"], {"today": {"date": "2026-10-31", "session": "no_session"},
                                        "prev_weekday": {"date": "2026-10-30", "session": "closed"}})
        self.assertEqual(data["counts"]["SPY"]["prev_weekday"], {"2m": 195, "5m": 78})
        self.assertFalse(data["stale"])
        self.assertTrue(all(q["start"][0].startswith("2026-10-30") for q in self.bars_calls()))

    def test_before_open_skips_today(self):
        pre = MON - 2 * 3600  # 09:00 ET
        data = bars.collect(self.deps(snapshots(fires=[("stock", "SPY", "2026-10-23T15:00:00Z")]), now=pre))
        self.assertEqual(data["days"]["today"]["session"], "not_started")
        self.assertEqual(data["counts"]["SPY"]["today"], {"2m": 0, "5m": 0})
        self.assertFalse(data["stale"])


if __name__ == "__main__":
    unittest.main()
