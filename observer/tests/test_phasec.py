import json
import os
import unittest
from unittest import mock

import fakes
import collect
import scrub
from sections import phasec


class PhaseC(unittest.TestCase):
    def setUp(self):
        self.box = fakes.Box()
        self.deps = self.box.deps()

    def tearDown(self):
        self.box.close()

    def envelope(self):
        collect.run(["phasec"], self.box.out, self.deps, log=fakes.silent)
        with open(os.path.join(self.box.out, "phasec.json"), encoding="utf-8") as fh:
            return json.loads(fh.read())

    def test_tb_vsl_009_counts(self):
        data, ok, err = phasec.collect(self.deps)
        self.assertTrue(ok)  # 009 ok, 014 ok:false -> partial
        lane = data["lanes"]["tb_vsl_009"]
        self.assertTrue(lane["ok"])
        # counted: NVDA (+30, 1.5R, mega), MU (-10, -1R, mid_smaller); everything else excluded
        self.assertEqual((lane["n"], lane["net_usd"], lane["profit_factor"], lane["exp_r"], lane["max_dd_usd"]),
                         (2, 20.0, 3.0, 0.25, 10.0))
        self.assertEqual(lane["excluded"], {"SPCX fail-closed": 1, "entry on/before 2026-09-21": 1,
                                            "symbol not in A1 watchlist_all 25": 1, "setup not elephant_bar": 1,
                                            "incomplete close": 1, "timeframe not daily": 1})
        self.assertEqual(lane["timeframe_unverified_n"], 2)
        self.assertEqual((lane["buckets"]["mega"]["n"], lane["buckets"]["mid_smaller"]["n"]), (1, 1))
        self.assertEqual(lane["buckets"]["mega"]["net_usd"], 30.0)
        self.assertEqual(lane["days_elapsed"], 34)
        self.assertEqual(lane["gate"], {"rule": "n >= 50 OR 90 calendar days from 2026-09-22",
                                        "deadline": "2026-12-21", "status": "open", "n_remaining": 48,
                                        "days_remaining": 56})
        self.assertEqual(lane["alerts_crossed"], [])
        self.assertEqual(lane["fail_triggers_hit"], [])
        self.assertIsNone(lane["max_dd_pct"])
        self.assertEqual(lane["coverage"]["complete"], True)
        self.assertEqual(lane["last_close"], "2026-10-08T19:00:00Z")
        self.assertEqual(lane["last_updated"], "2026-10-26T12:56:03Z")
        self.assertFalse(lane["stale"])
        self.assertEqual([c["symbol"] for c in lane["closes"]], ["NVDA", "MU"])

    def test_tb_vsl_014_ok_false_no_numbers(self):
        data, ok, err = phasec.collect(self.deps)
        lane = data["lanes"]["tb_vsl_014"]
        self.assertFalse(lane["ok"])
        self.assertIsNone(lane["n"])
        self.assertIn("count rule not reproducible", lane["reason"])
        self.assertEqual(lane["rule_known"]["mega13"], fakes.MEGA)
        self.assertIn("tb_vsl_014: count rule not reproducible", err)
        for k in ("net_usd", "profit_factor", "exp_r"):
            self.assertNotIn(k, lane)

    def test_gate_alerts_and_fail_triggers(self):
        losers = [fakes.ctrade("NVDA", f"2026-10-{1 + i % 25:02d}T10:00:00-04:00",
                               f"2026-10-{1 + i % 25:02d}T15:00:00-04:00", -1.0, -1.0) for i in range(41)]
        fakes.write_closed(self.box.root, fakes.closed_doc(trades=losers, fill_count=82))
        lane = phasec.collect(self.deps)[0]["lanes"]["tb_vsl_009"]
        self.assertEqual(lane["n"], 41)
        self.assertEqual(lane["alerts_crossed"], [10, 25, 40])
        self.assertEqual(lane["fail_triggers_hit"], ["net <= 0 at n >= 30", "PF < 1 at n >= 40"])
        self.assertEqual(lane["profit_factor"], 0.0)
        self.assertEqual(lane["gate"]["status"], "open")
        mixed = losers + [fakes.ctrade("MU", "2026-10-02T10:00:00-04:00", "2026-10-03T10:00:00-04:00", 5.0, 0.5)]
        fakes.write_closed(self.box.root, fakes.closed_doc(trades=mixed * 2, fill_count=0))
        lane = phasec.collect(self.deps)[0]["lanes"]["tb_vsl_009"]
        self.assertEqual(lane["gate"]["status"], "hit")  # n=84 >= 50
        self.assertIn("PF < 1 at n >= 40", lane["fail_triggers_hit"])
        self.assertFalse(lane["coverage"]["complete"])

    def test_gate_hit_by_days(self):
        lane = phasec.collect(self.box.deps(now=lambda: fakes.NOW + 60 * 86400))[0]["lanes"]["tb_vsl_009"]
        self.assertEqual((lane["days_elapsed"], lane["gate"]["status"]), (94, "hit"))
        self.assertTrue(lane["stale"])

    def test_missing_export_ok_false(self):
        os.unlink(os.path.join(self.box.root, "opt/stacks/ops-status/bullpilot-closed-trades-latest.json"))
        data, ok, err = phasec.collect(self.deps)
        self.assertFalse(ok)
        lane = data["lanes"]["tb_vsl_009"]
        self.assertEqual((lane["ok"], lane["reason"], lane["n"]), (False, "closed-trade export file not found", None))
        self.assertIsNone(lane["found"]["closed_trades_export"])
        self.assertTrue(data["stale"])
        self.assertFalse(self.envelope()["ok"])

    def test_malformed_inputs(self):
        fakes.write_closed(self.box.root, "{bad")
        self.assertEqual(phasec.collect(self.deps)[0]["lanes"]["tb_vsl_009"]["reason"],
                         "closed-trade export is not valid JSON")
        fakes.write_closed(self.box.root, json.dumps({"export_type": "other", "trades": []}))
        self.assertIn("unexpected shape", phasec.collect(self.deps)[0]["lanes"]["tb_vsl_009"]["reason"])
        fakes.write_closed(self.box.root, fakes.closed_doc())
        fakes.write(self.box.root, f"{fakes.EVAL}/universe_a1_watchlist_equity_buckets_v1.yaml",
                    fakes.buckets_yaml(mega=fakes.MEGA[:12]))
        lane = phasec.collect(self.deps)[0]["lanes"]["tb_vsl_009"]
        self.assertFalse(lane["ok"])
        self.assertIn("A1 bucket yaml unusable", lane["reason"])

    def test_parse_buckets(self):
        b = phasec.parse_buckets(fakes.buckets_yaml())
        self.assertEqual((len(b["watchlist_all"]), b["mega"], len(b["mid_smaller"])), (25, fakes.MEGA, 12))
        self.assertNotIn("ES1!", b["watchlist_all"])

    def test_oversize_source_refused(self):
        with mock.patch.object(phasec, "MAX_READ", 200):
            lane = phasec.collect(self.deps)[0]["lanes"]["tb_vsl_009"]
        self.assertEqual(lane["reason"], "closed-trade export file larger than 200 bytes")

    def test_oversize_snapshot_capped_150k(self):
        def big(deps):
            data = phasec.collect(deps)[0]
            lane = data["lanes"]["tb_vsl_009"]
            lane["closes"] = [dict(lane["closes"][0], exit_reason="x " * 40)] * 5000
            return data
        raw = collect.build("phasec", big, self.deps)
        self.assertLessEqual(len(raw), 150 * 1024)

    def test_scrubbed(self):
        env = self.envelope()
        text = json.dumps(env)
        for s in fakes.SECRETS + ["JI68"]:
            self.assertNotIn(s, text)
        self.assertEqual(scrub.find_leaks(env), [])
        self.assertTrue(env["ok"])
        self.assertIn("tb_vsl_014", env["error"])
        self.assertIn("/opt/stacks/bull-pilot/bot/reports/evaluation/universe_a1_watchlist_equity_buckets_v1.yaml",
                      env["data"]["lanes"]["tb_vsl_009"]["sources"])


if __name__ == "__main__":
    unittest.main()
