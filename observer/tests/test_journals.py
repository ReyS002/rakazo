import json
import os
import unittest
from unittest import mock

import fakes
import collect
import scrub
from sections import journals

AFTER_CLOSE = fakes.NOW + 6 * 3600  # Mon 2026-10-26 17:00 ET


class Journals(unittest.TestCase):
    def setUp(self):
        self.box = fakes.Box()
        self.deps = self.box.deps()

    def tearDown(self):
        self.box.close()

    def path(self, name):
        return os.path.join(self.box.root, fakes.EXPORTS.lstrip("/"), name)

    def envelope(self, deps=None):
        collect.run(["journals"], self.box.out, deps or self.deps, log=fakes.silent)
        with open(os.path.join(self.box.out, "journals.json"), encoding="utf-8") as fh:
            return json.loads(fh.read())

    def test_contract(self):
        data = journals.collect(self.deps)
        self.assertNotIsInstance(data, tuple)
        self.assertEqual((data["today_et"], data["after_close"], data["stale"]), ("2026-10-26", False, False))
        today, prev = data["days"]["today"], data["days"]["prev_weekday"]
        self.assertEqual(prev["date"], "2026-10-23")  # Mon -> Fri
        self.assertIsNone(today["paper_fills"])
        self.assertEqual(today["missing"], ["/opt/arena/state/exports/paper-fills-20261026.json",
                                            "/opt/arena/state/exports/unchained-decisions-20261026.json"])
        self.assertEqual(prev["missing"], [])
        pf = prev["paper_fills"]
        self.assertTrue(pf["ok"])
        self.assertEqual(sorted(pf["bots"]), ["Bull Pilot", "Unchained"])
        u = pf["bots"]["Unchained"]
        self.assertEqual(u["fills"][0], {"time": "2026-10-23T13:30:49Z", "symbol": "AMZN", "side": "buy", "qty": 99.0,
                                         "price": 249.9, "fill_type": "fill", "position_after": 0.0})
        self.assertEqual((u["stop_orders"][0]["stop_price"], u["stop_orders"][0]["status"]), (249.14, "filled"))
        self.assertEqual(u["open_positions"][0]["symbol"], "QCOM")
        dec = prev["decisions"]["unchained"]
        self.assertTrue(dec["ok"])
        self.assertEqual((dec["decisions_total"], dec["decisions_shown"]), (201, 150))
        self.assertEqual([r["decision"] for r in dec["decisions"]].count("taken"), 1)
        self.assertEqual(dec["summary"]["passed_by_reason"], {"not_tradable_filtered": 200.0})
        self.assertEqual(dec["closed_trades"][0]["r_multiple"], -4.3)

    def test_drops_bars_and_ids(self):
        text = json.dumps(self.envelope())
        for s in ("bars_2m", "start_et", "PYPV", "JI68", "order_id", "aad1f6d6", "e0f85b49", "unchained-a89c",
                  "PA3FAKE99999", "account_number", '"account"'):
            self.assertNotIn(s, text, s)

    def test_rows_capped_150(self):
        doc = fakes.paper_fills_doc()
        doc["accounts"]["PYPV"]["fills"] = doc["accounts"]["PYPV"]["fills"][:1] * 400
        fakes.write(self.box.root, f"{fakes.EXPORTS}/paper-fills-20261023.json", json.dumps(doc))
        fakes.write(self.box.root, f"{fakes.EXPORTS}/unchained-decisions-20261023.json",
                    json.dumps(fakes.decisions_doc(passed=1000)))
        prev = journals.collect(self.deps)["days"]["prev_weekday"]
        u = prev["paper_fills"]["bots"]["Unchained"]
        self.assertEqual((len(u["fills"]), u["fill_count"], u["truncated"]), (150, 400, True))
        self.assertEqual(len(prev["decisions"]["unchained"]["decisions"]), 150)

    def test_today_after_close_missing_is_stale(self):
        data = journals.collect(self.box.deps(now=lambda: AFTER_CLOSE))
        self.assertTrue(data["after_close"])
        self.assertTrue(data["stale"])
        fakes.write(self.box.root, f"{fakes.EXPORTS}/paper-fills-20261026.json",
                    json.dumps(fakes.paper_fills_doc("2026-10-26")))
        data = journals.collect(self.box.deps(now=lambda: AFTER_CLOSE))
        self.assertFalse(data["stale"])
        self.assertTrue(data["days"]["today"]["paper_fills"]["ok"])

    def test_missing_everything_ok_false(self):
        for n in ("paper-fills-20261023.json", "unchained-decisions-20261023.json"):
            os.unlink(self.path(n))
        data, ok, err = journals.collect(self.deps)
        self.assertFalse(ok)
        self.assertTrue(err.startswith("no paper-fills or decisions exports"))
        self.assertTrue(data["stale"])
        env = self.envelope()
        self.assertFalse(env["ok"])

    def test_malformed_file_ok_false_block(self):
        fakes.write(self.box.root, f"{fakes.EXPORTS}/paper-fills-20261023.json", "{oops")
        fakes.write(self.box.root, f"{fakes.EXPORTS}/unchained-decisions-20261023.json", json.dumps({"decisions": 3}))
        data, ok, err = journals.collect(self.deps)
        self.assertTrue(ok)  # files exist, partial
        prev = data["days"]["prev_weekday"]
        self.assertEqual((prev["paper_fills"]["ok"], prev["paper_fills"]["reason"]), (False, "source is not valid JSON"))
        self.assertTrue(prev["decisions"]["unchained"]["reason"].startswith("unexpected decisions shape"))
        self.assertIn("paper-fills-20261023.json: source is not valid JSON", err)

    def test_oversize_source_refused(self):
        with mock.patch.object(journals, "MAX_READ", 500):
            prev = journals.collect(self.deps)[0]["days"]["prev_weekday"]
        self.assertEqual(prev["paper_fills"]["reason"], "source file larger than 500 bytes")

    def test_oversize_snapshot_capped_150k(self):
        def big(deps):
            data = journals.collect(deps)
            dec = data["days"]["prev_weekday"]["decisions"]["unchained"]
            dec["decisions"] = [dict(dec["decisions"][0], reason="r " * 80)] * 3000
            return data
        raw = collect.build("journals", big, self.deps)
        self.assertLessEqual(len(raw), 150 * 1024)
        self.assertTrue(json.loads(raw)["ok"])

    def test_scrubbed(self):
        env = self.envelope()
        text = json.dumps(env)
        for s in fakes.SECRETS:
            self.assertNotIn(s, text)
        self.assertEqual(scrub.find_leaks(env), [])
        taken = [r for r in env["data"]["days"]["prev_weekday"]["decisions"]["unchained"]["decisions"]
                 if r["decision"] == "taken"][0]
        self.assertEqual(taken["reason"], "ok Bearer [REDACTED]")


if __name__ == "__main__":
    unittest.main()
