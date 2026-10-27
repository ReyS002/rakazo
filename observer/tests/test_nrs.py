import json
import os
import unittest
from unittest import mock

import fakes
import collect
import scrub
from sections import nrs


class Nrs(unittest.TestCase):
    def setUp(self):
        self.box = fakes.Box()
        self.deps = self.box.deps()

    def tearDown(self):
        self.box.close()

    def envelope(self):
        collect.run(["nrs"], self.box.out, self.deps, log=fakes.silent)
        with open(os.path.join(self.box.out, "nrs.json"), encoding="utf-8") as fh:
            return json.loads(fh.read())

    def test_contract(self):
        data = nrs.collect(self.deps)
        self.assertNotIsInstance(data, tuple)
        self.assertEqual(sorted(data["markets"]), ["crypto", "forex", "stock"])
        self.assertFalse(data["stale"])
        self.assertFalse(data["prev_snapshot"])
        s = data["markets"]["stock"]
        self.assertTrue(s["ok"])
        self.assertEqual((s["generated_at"], s["as_of"], s["age_s"]), ("2026-10-26T14:45:01Z", "2026-10-26T14:45:01Z", 899))
        self.assertEqual(s["source"], "/opt/stacks/ops-status/nrs-stock-latest.json")
        self.assertEqual(s["counts"], {"watchlist": 2, "recent_triggers_confirmed_unsuppressed_24_hours": 1})
        self.assertEqual(s["heartbeats"]["stock-live-poller"]["last_success_at"], "2026-10-26T14:44:13Z")
        w = s["watchlist"][0]
        self.assertEqual((w["symbol"], w["composite_score"], w["is_nr7"], w["velez_location"]),
                         ("AAPL", 60.8, True, "Near 20 SMA"))
        for dropped in ("chart_url", "tv_link", "account_id", "host"):
            self.assertNotIn(dropped, w)
        self.assertEqual(w["reasons_short"][0], "NR7")
        self.assertEqual(len(w["reasons_short"]), 2)  # nested object dropped
        t = s["recent_triggers"][0]
        self.assertEqual((t["symbol"], t["triggered_at"], t["direction"], t["rvol"], t["breakout_level"]),
                         ("AAPL", "2026-10-26T14:30:00Z", "long", 1.6, 100.2))
        self.assertIsNone(s["new_since_prev"])  # first run

    def test_new_since_prev(self):
        self.envelope()  # first snapshot: AAPL, MSFT
        fakes.write_nrs(self.box.root, "stock", fakes.nrs_doc(
            "stock", watch=("AAPL", "TSLA"),
            triggers=(("AAPL", "2026-10-26T14:30:00+00:00"), ("TSLA", "2026-10-26T14:45:00+00:00"))),
            stamp="2026-10-26T15:00:01Z")
        env = self.envelope()
        self.assertTrue(env["data"]["prev_snapshot"])
        new = env["data"]["markets"]["stock"]["new_since_prev"]
        self.assertEqual(new["watchlist"], ["TSLA"])
        self.assertEqual(new["recent_triggers"],
                         [{"symbol": "TSLA", "triggered_at": "2026-10-26T14:45:00Z", "direction": "long"}])
        self.assertEqual(env["data"]["markets"]["forex"]["new_since_prev"], {"watchlist": [], "recent_triggers": []})

    def test_entries_capped_at_60(self):
        fakes.write_nrs(self.box.root, "crypto", fakes.nrs_doc(
            "crypto", watch=[f"C{i}/USD" for i in range(100)],
            triggers=[(f"C{i}/USD", f"2026-10-26T{10 + i % 4:02d}:{i % 60:02d}:00+00:00") for i in range(90)]),
            stamp="2026-10-26T14:50:00Z")
        c = nrs.collect(self.deps)["markets"]["crypto"]
        self.assertEqual((len(c["watchlist"]), c["watchlist_count"]), (60, 100))
        self.assertEqual((len(c["recent_triggers"]), c["recent_triggers_count"]), (60, 90))
        times = [t["triggered_at"] for t in c["recent_triggers"]]
        self.assertEqual(times, sorted(times, reverse=True))  # newest first

    def test_missing_source_ok_false(self):
        os.unlink(os.path.join(self.box.root, "opt/stacks/ops-status/nrs-forex-latest.json"))
        data, ok, err = nrs.collect(self.deps)
        self.assertTrue(ok)  # other markets still fine
        self.assertEqual(err, "forex: source file not found")
        f = data["markets"]["forex"]
        self.assertEqual((f["ok"], f["reason"], f["stale"], f["new_since_prev"]),
                         (False, "source file not found", True, None))
        self.assertTrue(data["stale"])
        for m in ("stock", "crypto"):
            os.unlink(os.path.join(self.box.root, f"opt/stacks/ops-status/nrs-{m}-latest.json"))
        data, ok, err = nrs.collect(self.deps)
        self.assertFalse(ok)
        env = self.envelope()
        self.assertFalse(env["ok"])
        self.assertIn("stock: source file not found", env["error"])

    def test_malformed(self):
        fakes.write_nrs(self.box.root, "stock", "{not json", stamp="2026-10-26T14:50:00Z")
        fakes.write_nrs(self.box.root, "forex", json.dumps({"watchlist": "nope"}), stamp="2026-10-26T14:50:00Z")
        fakes.write_nrs(self.box.root, "crypto", json.dumps([1, 2]), stamp="2026-10-26T14:50:00Z")
        data, ok, err = nrs.collect(self.deps)
        self.assertFalse(ok)
        self.assertEqual(data["markets"]["stock"]["reason"], "source is not valid JSON")
        self.assertTrue(data["markets"]["forex"]["reason"].startswith("unexpected export shape"))
        self.assertTrue(data["markets"]["crypto"]["reason"].startswith("unexpected export shape"))

    def test_symlink_leaving_dir_refused(self):
        outside = fakes.write(self.box.root, "/elsewhere/nrs.json", json.dumps(fakes.nrs_doc("stock")))
        link = os.path.join(self.box.root, "opt/stacks/ops-status/nrs-stock-latest.json")
        os.unlink(link)
        os.symlink(outside, link)
        s = nrs.collect(self.deps)[0]["markets"]["stock"]
        self.assertEqual(s["reason"], "source symlink leaves source dir")

    def test_oversize_source_refused(self):
        with mock.patch.object(nrs, "MAX_READ", 1000):
            data, ok, err = nrs.collect(self.deps)
        self.assertFalse(ok)
        self.assertEqual(data["markets"]["stock"]["reason"], "source file larger than 1000 bytes")

    def test_oversize_snapshot_capped_150k(self):
        def big(deps):
            data = nrs.collect(deps)
            row = dict(data["markets"]["stock"]["watchlist"][0], velez_location="z " * 90)
            data["markets"]["stock"]["watchlist"] = [row] * 3000
            return data
        raw = collect.build("nrs", big, self.deps)
        self.assertLessEqual(len(raw), 150 * 1024)
        self.assertTrue(json.loads(raw)["ok"])
        self.assertEqual(collect.SECTION_MAX_BYTES["nrs"], 150 * 1024)

    def test_stale_weekday_26h(self):
        old = fakes.nrs_doc("stock", ts="2026-10-25T12:00:00Z")  # Sun; NOW is Mon 11:00 ET -> 27 h
        fakes.write_nrs(self.box.root, "stock", old, stamp="2026-10-25T12:00:00Z")
        fresh = fakes.nrs_doc("forex", ts="2026-10-25T14:00:00Z")  # 25 h
        fakes.write_nrs(self.box.root, "forex", fresh, stamp="2026-10-25T14:00:00Z")
        data = nrs.collect(self.deps)
        self.assertTrue(data["markets"]["stock"]["stale"])
        self.assertFalse(data["markets"]["forex"]["stale"])
        self.assertTrue(data["stale"])
        # weekends never flag stale
        self.assertFalse(nrs.stale_weekday(fakes.NOW - 5 * 86400, 1792944000.0))  # Sun 2026-10-25 12:00 ET

    def test_scrubbed(self):
        env = self.envelope()
        text = json.dumps(env)
        for s in fakes.SECRETS + ["PA3FAKE99999", "srv1668095", "tradingview.com"]:
            self.assertNotIn(s, text)
        self.assertEqual(scrub.find_leaks(env), [])
        self.assertTrue(env["ok"])
        self.assertEqual(env["data"]["markets"]["stock"]["recent_triggers"][0]["detail"], "15m breakout chat [NUM]")
        self.assertEqual(env["data"]["markets"]["stock"]["watchlist"][0]["reasons_short"][1], "note [REDACTED]")


if __name__ == "__main__":
    unittest.main()
