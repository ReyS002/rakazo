import json
import os
import unittest
from datetime import date, timedelta
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
        no_dbs = [f"{p}: source db not found" for p in (fakes.VELEZ_DB, fakes.PILOT_DB, fakes.SWING_DB)]
        self.assertEqual(today["missing"], ["/opt/arena/state/exports/paper-fills-20261026.json",
                                            "/opt/arena/state/exports/unchained-decisions-20261026.json"] + no_dbs)
        self.assertEqual(prev["missing"], no_dbs)
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
        self.assertEqual((dec["decisions_total"], dec["decisions_shown"]), (201, 201))
        self.assertEqual(dec["lists"]["decisions"], {"total_rows": 201, "kept_rows": 201, "truncated": False})
        self.assertEqual((dec["label"], dec["code"]), ("Unchained", "PYPV"))
        self.assertEqual((u["code"], pf["bots"]["Bull Pilot"]["code"]), ("PYPV", "JI68"))
        self.assertEqual(u["lists"]["fills"], {"total_rows": 2, "kept_rows": 2, "truncated": False})
        self.assertEqual([r["decision"] for r in dec["decisions"]].count("taken"), 1)
        self.assertEqual(dec["summary"]["passed_by_reason"], {"not_tradable_filtered": 200.0})
        self.assertEqual(dec["closed_trades"][0]["r_multiple"], -4.3)

    def test_drops_bars_and_ids(self):
        text = json.dumps(self.envelope())
        # Bot codes (PYPV, JI68) are published on purpose (see BOTS); the account fields themselves are not.
        for s in ("bars_2m", "start_et", "order_id", "aad1f6d6", "e0f85b49", "unchained-a89c",
                  "PA3FAKE99999", "account_number", '"account"'):
            self.assertNotIn(s, text, s)

    def test_bot_codes_table(self):
        cases = {"Unchained": "PYPV", "unchained": "PYPV", "Bull Pilot": "JI68", "bull-pilot": "JI68",
                 "Velez Trading": "TYWQ", "velez-trading": "TYWQ", "Swing": "RH5Y", "Velez swing": "RH5Y",
                 "BullMatrix": "7E3U", "bull-swarm": "7E3U", "Mystery": None, None: None}
        for name, code in cases.items():
            with self.subTest(name=name):
                self.assertEqual(journals.bot_identity(name)[1], code)
        fakes.write(self.box.root, f"{fakes.EXPORTS}/mystery-decisions-20261023.json",
                    json.dumps(fakes.decisions_doc(passed=3)))
        dec = journals.collect(self.deps)["days"]["prev_weekday"]["decisions"]["mystery"]
        self.assertEqual((dec["label"], dec["code"]), ("mystery", None))

    def test_rows_capped(self):
        doc = fakes.paper_fills_doc()
        doc["accounts"]["PYPV"]["fills"] = doc["accounts"]["PYPV"]["fills"][:1] * 1200
        fakes.write(self.box.root, f"{fakes.EXPORTS}/paper-fills-20261023.json", json.dumps(doc))
        fakes.write(self.box.root, f"{fakes.EXPORTS}/unchained-decisions-20261023.json",
                    json.dumps(fakes.decisions_doc(passed=3500)))
        data = journals.collect(self.deps)
        prev = data["days"]["prev_weekday"]
        u = prev["paper_fills"]["bots"]["Unchained"]
        self.assertEqual((len(u["fills"]), u["fill_count"], u["truncated"]), (1000, 1200, True))
        self.assertEqual(u["lists"]["fills"], {"total_rows": 1200, "kept_rows": 1000, "truncated": True})
        dec = prev["decisions"]["unchained"]
        self.assertEqual(len(dec["decisions"]), 3000)
        self.assertEqual(dec["lists"]["decisions"], {"total_rows": 3501, "kept_rows": 3000, "truncated": True})
        self.assertEqual([r["decision"] for r in dec["decisions"]].count("taken"), 1)  # non-pass always kept
        times = [r["time"] for r in dec["decisions"]]
        self.assertEqual(times, sorted(times))
        cat = {b["code"]: b for b in data["catalog"]["prev_weekday"]["bots"]}
        self.assertEqual(sorted(cat["PYPV"]["truncated"]), ["decisions", "fills"])

    def test_decisions_chronological_full_day(self):
        doc = fakes.decisions_doc(passed=0)
        doc["decisions"] = [dict(doc["decisions"][0], time_et=f"2026-10-23T{h:02d}:{m:02d}:00-04:00",
                                 decision="passed" if m % 7 else "taken")
                            for h in range(15, 3, -1) for m in range(0, 60, 1)]  # 720 rows, newest first
        fakes.write(self.box.root, f"{fakes.EXPORTS}/unchained-decisions-20261023.json", json.dumps(doc))
        rows = journals.collect(self.deps)["days"]["prev_weekday"]["decisions"]["unchained"]["decisions"]
        self.assertEqual(len(rows), 720)
        self.assertEqual((rows[0]["time"], rows[-1]["time"]), ("2026-10-23T08:00:00Z", "2026-10-23T19:59:00Z"))
        self.assertEqual([r["time"] for r in rows], sorted(r["time"] for r in rows))

    def test_catalog(self):
        cat = journals.collect(self.deps)["catalog"]
        self.assertEqual(cat["today"], {"date": "2026-10-26", "bots": []})
        self.assertEqual(cat["prev_weekday"]["date"], "2026-10-23")
        u, bp = cat["prev_weekday"]["bots"]
        self.assertEqual(u, {"label": "Unchained", "code": "PYPV", "fills": 2, "stop_orders": 1, "open_positions": 1,
                             "closed_trades": 1, "decisions": 201, "truncated": [],
                             "first_time_et": "2026-10-23T09:45:00-04:00", "last_time_et": "2026-10-23T10:59:05-04:00"})
        self.assertEqual((bp["code"], bp["decisions"], bp["first_time_et"]), ("JI68", 0, None))

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

    def test_snapshot_cap_8mb(self):
        self.assertEqual(collect.SECTION_MAX_BYTES["journals"], 8 << 20)
        self.assertEqual(collect.PREV_MAX_BYTES, 8 << 20)
        self.assertEqual(collect.SECTION_MAX_BYTES["nrs"], 150 * 1024)

        def day(deps, n):
            data = journals.collect(deps)
            dec = data["days"]["prev_weekday"]["decisions"]["unchained"]
            dec["decisions"] = [dict(dec["decisions"][0], reason="r " * 80)] * n
            return data
        raw = collect.build("journals", lambda deps: day(deps, 12000), self.deps)  # ~4 MB: kept whole
        self.assertGreater(len(raw), 3 << 20)
        self.assertEqual(len(json.loads(raw)["data"]["days"]["prev_weekday"]["decisions"]["unchained"]["decisions"]),
                         12000)
        raw = collect.build("journals", lambda deps: day(deps, 30000), self.deps)
        self.assertLessEqual(len(raw), 8 << 20)
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


DST_NOW = 1793631600.0  # Mon 2026-11-02 15:00Z = 10:00 EST (DST ended Sun Nov 1)


class JournalsDb(unittest.TestCase):
    """Bot SQLite `decisions` tables (TYWQ / JI68 / RH5Y)."""

    def setUp(self):
        self.box = fakes.Box()
        self.deps = self.box.deps()
        self.cons = []

    def tearDown(self):
        for con in self.cons:
            con.close()
        self.box.close()

    def db(self, path=fakes.VELEZ_DB, rows=(), **kw):
        con = fakes.make_decisions_db(self.box.root, path, rows, **kw)
        if con is not None:
            self.cons.append(con)

    def host(self, path):
        return os.path.join(self.box.root, path.lstrip("/"))

    def day(self, key="today", deps=None):
        return journals.collect(deps or self.deps)

    def test_rows_shape_catalog_and_no_blobs(self):
        self.db(rows=[fakes.decision_db_row("2026-10-26T14:31:02.123456+00:00", status="proposed",
                                            reason=f"ok Bearer {fakes.BEARER_TOK}"),
                      fakes.decision_db_row("2026-10-26T13:45:00+00:00", symbol="MSFT", timeframe=5),
                      fakes.decision_db_row("2026-10-23T19:59:00+00:00", symbol="NVDA")])
        self.db(fakes.SWING_DB, rows=[fakes.decision_db_row("2026-10-23T15:00:00+00:00", play="swing_pullback")])
        data = journals.collect(self.deps)
        today, prev = data["days"]["today"], data["days"]["prev_weekday"]
        v = today["decisions"]["velez-trading"]
        self.assertEqual((v["ok"], v["label"], v["code"], v["source"]), (True, "Velez Trading", "TYWQ", "sqlite"))
        self.assertEqual(v["lists"]["decisions"], {"total_rows": 2, "kept_rows": 2, "truncated": False})
        self.assertEqual(v["day_utc"], ["2026-10-26T04:00:00Z", "2026-10-27T04:00:00Z"])
        self.assertEqual(v["decisions"][0]["timeframe"], "5")
        self.assertEqual(v["decisions"][1], {
            "time": "2026-10-26T14:31:02Z", "time_et": "10:31:02", "symbol": "AAPL", "decision": "proposed",
            "reason": "ok Bearer [REDACTED]", "side": "buy", "play": "elephant_bar", "qty": 10.0,
            "order_type": "stop_limit", "entry": 230.5, "stop": 229.9, "target": 232.0, "timeframe": "2m",
            "location": "above 20ma", "max_dollar_risk": 50.0})
        self.assertEqual([r["symbol"] for r in prev["decisions"]["velez-trading"]["decisions"]], ["NVDA"])
        s = prev["decisions"]["velez-swing"]
        self.assertEqual((s["label"], s["code"], s["decisions"][0]["play"]), ("Swing", "RH5Y", "swing_pullback"))
        self.assertIn(f"{fakes.PILOT_DB}: source db not found", prev["missing"])
        cat = {b["code"]: b for b in data["catalog"]["today"]["bots"]}
        self.assertEqual(cat["TYWQ"], {"label": "Velez Trading", "code": "TYWQ", "fills": 0, "stop_orders": 0,
                                       "open_positions": 0, "closed_trades": 0, "decisions": 2, "truncated": [],
                                       "first_time_et": "2026-10-26T09:45:00-04:00",
                                       "last_time_et": "2026-10-26T10:31:02-04:00"})
        prev_cat = {b["code"]: b for b in data["catalog"]["prev_weekday"]["bots"]}
        self.assertEqual((prev_cat["RH5Y"]["decisions"], prev_cat["JI68"]["decisions"]), (1, 0))  # JI68: fills only
        env = json.loads(collect.build("journals", journals.collect, self.deps))
        text = json.dumps(env)
        for secret in fakes.BLOB_SECRETS + fakes.SECRETS:
            self.assertNotIn(secret, text, secret)
        for col in ("order_payload_json", "broker_response_json", "snapshot_json", "alert_ref", '"id"'):
            self.assertNotIn(col, text, col)
        self.assertEqual(scrub.find_leaks(env), [])
        self.assertTrue(env["ok"])

    def test_never_selects_blob_columns(self):
        self.assertNotIn("json", journals.DB_QUERY)
        self.assertNotIn("*", journals.DB_QUERY)
        self.assertNotIn("alert_ref", journals.DB_QUERY)

    def test_missing_db_and_empty_file(self):
        fakes.write(self.box.root, fakes.PILOT_DB, "")
        prev = journals.collect(self.deps)["days"]["prev_weekday"]
        self.assertIn(f"{fakes.VELEZ_DB}: source db not found", prev["missing"])
        self.assertIn(f"{fakes.PILOT_DB}: source db is empty (no decisions table)", prev["missing"])
        self.assertEqual(sorted(prev["decisions"]), ["unchained"])

    def test_missing_table_goes_to_missing(self):
        self.db(schema=("CREATE TABLE watchlist (symbol TEXT)",))
        data = journals.collect(self.deps)
        self.assertNotIsInstance(data, tuple)  # not an error
        self.assertIn(f"{fakes.VELEZ_DB}: no decisions table", data["days"]["today"]["missing"])
        self.assertNotIn("velez-trading", data["days"]["today"]["decisions"])

    def test_missing_column_ok_false(self):
        self.db(schema=("CREATE TABLE decisions (id INTEGER PRIMARY KEY, timestamp TEXT, status TEXT)",))
        data, ok, err = journals.collect(self.deps)
        v = data["days"]["today"]["decisions"]["velez-trading"]
        self.assertFalse(v["ok"])
        self.assertTrue(v["reason"].startswith("decisions table missing columns: reason,symbol,side"), v["reason"])
        self.assertIn("today TYWQ decisions db: decisions table missing columns", err)

    def test_locked_db_ok_false(self):
        self.db(rows=[fakes.decision_db_row("2026-10-26T14:00:00+00:00")], wal=False)
        import sqlite3
        holder = sqlite3.connect(self.host(fakes.VELEZ_DB), isolation_level=None)
        holder.execute("BEGIN EXCLUSIVE")
        self.cons.append(holder)
        with mock.patch.object(journals, "BUSY_TIMEOUT_S", 0.2):
            data, ok, err = journals.collect(self.deps)
        v = data["days"]["today"]["decisions"]["velez-trading"]
        self.assertEqual((v["ok"], v["reason"]), (False, "query failed: OperationalError: database is locked"))
        self.assertTrue(ok)  # partial

    def test_malformed_db_ok_false(self):
        fakes.write(self.box.root, fakes.VELEZ_DB, "not a database at all " * 20)
        v = journals.collect(self.deps)[0]["days"]["today"]["decisions"]["velez-trading"]
        self.assertEqual((v["ok"], v["reason"]), (False, "source is not a sqlite db"))
        with open(self.host(fakes.VELEZ_DB), "r+b") as fh:  # valid header, garbage pages
            fh.write(b"SQLite format 3\x00" + b"\x10\x00\x01\x01" + b"\xff" * 300)
        v = journals.collect(self.deps)[0]["days"]["today"]["decisions"]["velez-trading"]
        self.assertFalse(v["ok"])

    def test_wal_db_without_sidecars_not_opened(self):
        self.db(rows=[fakes.decision_db_row("2026-10-26T14:00:00+00:00")])
        self.cons.pop().close()  # last connection closes: -wal/-shm removed, db stays WAL mode
        self.assertFalse(os.path.exists(self.host(fakes.VELEZ_DB) + "-wal"))
        before = sorted(os.listdir(os.path.dirname(self.host(fakes.VELEZ_DB))))
        v = journals.collect(self.deps)[0]["days"]["today"]["decisions"]["velez-trading"]
        self.assertFalse(v["ok"])
        self.assertTrue(v["reason"].startswith("WAL db without the writer's -wal/-shm"))
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.host(fakes.VELEZ_DB)))), before)

    def test_reading_does_not_touch_db_files(self):
        self.db(rows=[fakes.decision_db_row("2026-10-26T14:00:00+00:00")])
        d = os.path.dirname(self.host(fakes.VELEZ_DB))
        snap = lambda: {n: os.stat(os.path.join(d, n)).st_mtime_ns for n in os.listdir(d)}  # noqa: E731
        before = snap()
        journals.collect(self.deps)
        self.assertEqual(snap(), before)

    def test_empty_day(self):
        self.db(rows=[fakes.decision_db_row("2026-10-20T14:00:00+00:00")])
        data = journals.collect(self.deps)
        v = data["days"]["today"]["decisions"]["velez-trading"]
        self.assertEqual((v["ok"], v["decisions"], v["lists"]["decisions"]["total_rows"]), (True, [], 0))
        cat = {b["code"]: b for b in data["catalog"]["today"]["bots"]}
        self.assertEqual((cat["TYWQ"]["decisions"], cat["TYWQ"]["first_time_et"]), (0, None))

    def test_et_day_boundaries(self):
        ts = ["2026-10-26T03:59:00+00:00",          # Sun 23:59 ET -> not today
              "2026-10-26T04:01:00+00:00",          # 00:01 ET
              "2026-10-26T09:30:00-04:00",          # other offset, 13:30Z
              "2026-10-26T13:00:00Z",
              "2026-10-26T13:15:00",                # naive = UTC
              "2026-10-27T03:59:59.999999+00:00",   # 23:59:59 ET
              "2026-10-27T04:00:00+00:00",          # next ET day
              "2026-10-26 garbage",                 # in the indexed range, unparseable -> counted, dropped
              "garbage"]                            # outside the indexed range, never fetched
        self.db(rows=[fakes.decision_db_row(t) for t in ts])
        v = journals.collect(self.deps)["days"]["today"]["decisions"]["velez-trading"]
        self.assertEqual([(r["time"], r["time_et"]) for r in v["decisions"]],
                         [("2026-10-26T04:01:00Z", "00:01:00"), ("2026-10-26T13:00:00Z", "09:00:00"),
                          ("2026-10-26T13:15:00Z", "09:15:00"), ("2026-10-26T13:30:00Z", "09:30:00"),
                          ("2026-10-27T03:59:59Z", "23:59:59")])
        self.assertEqual(v["unparsed_timestamps"], 1)

    def test_dst_edge(self):
        self.db(rows=[fakes.decision_db_row(t) for t in (
            "2026-10-30T03:59:00+00:00",   # Thu 23:59 EDT
            "2026-10-30T04:01:00+00:00",   # Fri 00:01 EDT
            "2026-11-01T05:30:00+00:00",   # Sun 01:30 EDT (first 01:30)
            "2026-11-02T04:30:00+00:00",   # Sun 23:30 EST -> not Monday
            "2026-11-02T05:01:00+00:00")])  # Mon 00:01 EST
        data = journals.collect(self.box.deps(now=lambda: DST_NOW))
        today, prev = data["days"]["today"], data["days"]["prev_weekday"]
        self.assertEqual((today["date"], prev["date"]), ("2026-11-02", "2026-10-30"))
        t, p = today["decisions"]["velez-trading"], prev["decisions"]["velez-trading"]
        self.assertEqual(t["day_utc"], ["2026-11-02T05:00:00Z", "2026-11-03T05:00:00Z"])
        self.assertEqual(p["day_utc"], ["2026-10-30T04:00:00Z", "2026-10-31T04:00:00Z"])
        self.assertEqual([r["time_et"] for r in t["decisions"]], ["00:01:00"])
        self.assertEqual([r["time_et"] for r in p["decisions"]], ["00:01:00"])
        start, end = journals.et_day_utc(date(2026, 11, 1))
        self.assertEqual(end - start, timedelta(hours=25))

    def test_cap_chronological(self):
        rows = [fakes.decision_db_row(f"2026-10-26T{14 + i // 3600 % 6:02d}:{i // 60 % 60:02d}:{i % 60:02d}+00:00")
                for i in range(3100)][::-1]
        self.db(rows=rows)
        data = journals.collect(self.deps)
        v = data["days"]["today"]["decisions"]["velez-trading"]
        self.assertEqual(len(v["decisions"]), 3000)
        self.assertEqual(v["lists"]["decisions"], {"total_rows": 3100, "kept_rows": 3000, "truncated": True})
        self.assertEqual((v["decisions_total"], v["decisions_shown"]), (3100, 3000))
        times = [r["time"] for r in v["decisions"]]
        self.assertEqual(times, sorted(times))
        self.assertEqual(times[0], "2026-10-26T14:00:00Z")
        cat = {b["code"]: b for b in data["catalog"]["today"]["bots"]}
        self.assertEqual(cat["TYWQ"]["truncated"], ["decisions"])

    def test_query_budget(self):
        self.db(rows=[fakes.decision_db_row(f"2026-10-26T14:{i // 60:02d}:{i % 60:02d}+00:00") for i in range(3000)])
        with mock.patch.object(journals, "QUERY_BUDGET_S", 0.0):
            v = journals.collect(self.deps)[0]["days"]["today"]["decisions"]["velez-trading"]
        self.assertEqual((v["ok"], v["reason"]), (False, "query exceeded the 0 s budget"))

    def test_export_takes_precedence(self):
        self.db(fakes.PILOT_DB, rows=[fakes.decision_db_row("2026-10-23T14:00:00+00:00")])
        fakes.write(self.box.root, f"{fakes.EXPORTS}/bull-pilot-decisions-20261023.json",
                    json.dumps(fakes.decisions_doc(passed=3)))
        prev = journals.collect(self.deps)["days"]["prev_weekday"]
        b = prev["decisions"]["bull-pilot"]
        self.assertEqual((b["code"], b.get("source"), b["decisions_total"]), ("JI68", None, 4))


if __name__ == "__main__":
    unittest.main()
