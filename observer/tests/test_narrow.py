import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import fakes
import collect
import scrub
from sections import narrow


def file_hashes(path):
    out = {}
    for suffix in ("", "-wal"):
        p = path + suffix
        if os.path.exists(p):
            with open(p, "rb") as fh:
                out[suffix] = (hashlib.sha256(fh.read()).hexdigest(), os.stat(p).st_mtime_ns)
    return out


class Narrow(unittest.TestCase):
    def setUp(self):
        self.box = fakes.Box()
        self.deps = self.box.deps()
        self.db = os.path.join(self.box.root, fakes.NARROW_DB.lstrip("/"))
        self.scratch = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.scratch.cleanup()
        self.box.close()

    def collect(self, **kw):
        return narrow.collect(self.deps, scratch=self.scratch.name, **kw)

    def live_writer(self, exclusive=False):
        """Open connection like Bull Pilot's running lane: -wal and -shm exist beside the db."""
        con = sqlite3.connect(self.db, isolation_level=None)
        con.execute("SELECT count(*) FROM heartbeats").fetchone()
        if exclusive:
            con.execute("PRAGMA locking_mode=EXCLUSIVE")
            con.execute("BEGIN EXCLUSIVE")
            con.execute("INSERT INTO heartbeats (generated_at, session_date, state) "
                        "VALUES ('2026-10-26T14:59:00+00:00', '2026-10-26', 'collecting')")
        self.assertTrue(os.path.exists(self.db + "-wal") and os.path.exists(self.db + "-shm"))
        self.addCleanup(con.close)
        return con

    def remake(self, **kw):
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.db + suffix):
                os.unlink(self.db + suffix)
        fakes.make_narrow_db(self.db, **kw)

    def envelope(self):
        collect.run(["narrow"], self.box.out, self.deps, log=fakes.silent)
        with open(os.path.join(self.box.out, "narrow.json"), encoding="utf-8") as fh:
            return json.loads(fh.read())

    def test_contract_fake_db(self):
        self.live_writer()
        data = self.collect()
        self.assertNotIsInstance(data, tuple)
        self.assertTrue(data["ok"])
        self.assertIsNone(data["reason"])
        self.assertEqual(data["source"], {"kind": "sqlite", "path": fakes.NARROW_DB,
                                          "lane": "long_narrow_gap_orb_r2_v2", "read_mode": "ro"})
        self.assertEqual(data["as_of"], "2026-10-26T14:58:45Z")
        self.assertFalse(data["stale"])
        self.assertEqual(sorted(data["schema"]), ["blocked", "heartbeats", "metadata", "signals"])
        self.assertIn("stop_price", data["schema"]["signals"])
        self.assertFalse(data["per_symbol_narrow_wide_in_source"])
        self.assertEqual(data["execution_mode"], "signal_only_no_broker")
        self.assertEqual(data["today_et"], "2026-10-26")
        self.assertEqual(data["lane_state"]["state"], "collecting")
        self.assertEqual(data["lane_state"]["eligible_symbols"], 7)
        self.assertEqual(data["counts"], {"signals": {"total": 4, "today": 1, "last_7d": 3},
                                          "blocked": {"total": 1, "today": 1, "last_7d": 1},
                                          "heartbeats": {"total": 2, "today": 1, "last_7d": 2}})
        self.assertEqual(data["last_signal_at"], "2026-10-26T13:41:30Z")
        latest = {r["symbol"]: r for r in data["latest_by_symbol"]}
        self.assertEqual(sorted(latest), ["AAPL", "MSFT", "NVDA"])
        a = latest["AAPL"]
        self.assertEqual((a["session_date"], a["as_of"], a["trigger"], a["stop"], a["entry"], a["gap_pct"]),
                         ("2026-10-26", "2026-10-26T13:41:30Z", 101.2, 101.25, 101.5, 1.8))
        self.assertEqual(a["entry_time"], "2026-10-26T13:41:00Z")
        # dollars / size are not exported
        for row in data["latest_by_symbol"] + data["recent"]:
            self.assertNotIn("quantity", row)
            self.assertNotIn("normal_net_pnl", row)
        kinds = [(r["kind"], r["symbol"]) for r in data["recent"]]
        self.assertEqual(kinds[0], ("blocked", "TSLA"))  # newest first
        self.assertEqual(len(kinds), 5)
        closed = [r for r in data["recent"] if r.get("status") == "closed"][0]
        self.assertEqual((closed["exit_reason"], closed["normal_net_r"], closed["harsh_net_r"]), ("eod", 1.4, 1.2))

    def test_recent_capped_at_30(self):
        sigs = [fakes.narrow_signal(f"S{i}", f"2026-10-{1 + i % 26:02d}", setup_id=f"x{i}") for i in range(80)]
        self.remake(signals=sigs)
        data = self.collect()
        self.assertEqual(len(data["recent"]), 30)
        self.assertEqual(len(data["latest_by_symbol"]), 80)
        self.assertEqual(data["counts"]["signals"]["total"], 80)

    def test_source_never_written(self):
        self.live_writer()
        src_dir = os.path.dirname(self.db)
        before, names = file_hashes(self.db), sorted(os.listdir(src_dir))
        self.assertEqual(self.collect()["source"]["read_mode"], "ro")
        self.envelope()
        self.assertEqual(file_hashes(self.db), before)
        self.assertEqual(sorted(os.listdir(src_dir)), names)
        self.assertEqual(os.listdir(self.scratch.name), [])

    def test_idle_writer_reads_copy_and_creates_no_files(self):
        src_dir = os.path.dirname(self.db)
        self.assertEqual(sorted(os.listdir(src_dir)), [os.path.basename(self.db)])  # writer closed: no -wal/-shm
        before = file_hashes(self.db)
        data = self.collect()
        self.assertTrue(data["ok"])
        self.assertEqual(data["source"]["read_mode"], "copy")
        self.assertEqual(data["as_of"], "2026-10-26T14:58:45Z")
        self.assertEqual(sorted(os.listdir(src_dir)), [os.path.basename(self.db)])
        self.assertEqual(file_hashes(self.db), before)
        self.assertEqual(os.listdir(self.scratch.name), [])

    def test_missing_db_ok_false(self):
        os.unlink(self.db)
        data, ok, err = self.collect()
        self.assertFalse(ok)
        self.assertEqual(err, "source db not found")
        self.assertEqual((data["ok"], data["reason"], data["stale"], data["as_of"]),
                         (False, "source db not found", True, None))
        env = self.envelope()
        self.assertEqual((env["ok"], env["error"], env["data"]["reason"]),
                         (False, "source db not found", "source db not found"))

    def test_symlink_db_refused(self):
        os.unlink(self.db)
        other = fakes.make_narrow_db(os.path.join(self.box.root, "elsewhere.sqlite3"))
        os.symlink(other, self.db)
        data, ok, err = self.collect()
        self.assertFalse(ok)
        self.assertEqual(err, "source db is not a regular file")

    def test_not_sqlite_ok_false(self):
        self.remake()
        for suffix in ("-wal", "-shm"):
            if os.path.exists(self.db + suffix):
                os.unlink(self.db + suffix)
        with open(self.db, "wb") as fh:
            fh.write(b"not a database at all" * 100)
        data, ok, err = self.collect()
        self.assertFalse(ok)
        self.assertIn("sqlite", err)

    def test_unexpected_schema_ok_false(self):
        schema = tuple(s.replace("stop_price REAL NOT NULL, ", "") for s in fakes.NARROW_SCHEMA
                       if "heartbeats" not in s)
        self.remake(schema=schema, signals=[], blocked=[], heartbeats=[])
        data, ok, err = self.collect()
        self.assertFalse(ok)
        self.assertTrue(err.startswith("unexpected schema: "), err)
        self.assertIn("missing table heartbeats", err)
        self.assertIn("signals missing stop_price", err)
        self.assertEqual(sorted(data["schema"]), ["blocked", "metadata", "signals"])  # names + columns only
        self.assertNotIn("latest_by_symbol", data)

    def test_locked_db_reads_copy(self):
        self.live_writer(exclusive=True)
        with mock.patch.object(narrow, "BUSY_TIMEOUT_S", 0.1), \
                mock.patch.object(narrow, "_copy_source", wraps=narrow._copy_source) as copy:
            data = self.collect()
        copy.assert_called_once()
        self.assertTrue(data["ok"], data.get("reason"))
        self.assertEqual(data["source"]["read_mode"], "copy")
        self.assertEqual(data["as_of"], "2026-10-26T14:58:45Z")  # uncommitted row not visible
        self.assertEqual(os.listdir(self.scratch.name), [])  # copy removed

    def test_locked_db_copy_fails_ok_false(self):
        self.live_writer(exclusive=True)
        with mock.patch.object(narrow, "BUSY_TIMEOUT_S", 0.1), \
                mock.patch.object(narrow, "_copy_source", side_effect=OSError("no space")):
            data, ok, err = self.collect()
        self.assertFalse(ok)
        self.assertTrue(err.startswith("source db locked/unreadable (database is locked)"), err)
        self.assertIn("copy failed: OSError", err)
        self.assertEqual(os.listdir(self.scratch.name), [])

    def test_scrubber_applied(self):
        env = self.envelope()
        text = json.dumps(env)
        for s in fakes.SECRETS:
            self.assertNotIn(s, text)
        self.assertEqual(scrub.find_leaks(env), [])
        self.assertTrue(env["ok"])
        self.assertEqual(env["data"]["lane_state"]["error"], "retry [REDACTED] chat [NUM]")
        aapl = [r for r in env["data"]["latest_by_symbol"] if r["symbol"] == "AAPL"][0]
        self.assertEqual(aapl["guardrail_reason"], "note [REDACTED]")
        tsla = [r for r in env["data"]["recent"] if r["symbol"] == "TSLA"][0]
        self.assertEqual(tsla["reason"], "three-finger block Bearer [REDACTED]")
        self.assertEqual(env["data"]["source"]["path"], fakes.NARROW_DB)  # path slug not mangled

    def test_free_text_clipped(self):
        self.remake(blocked=[{"setup_id": "x", "symbol": "TSLA", "session_date": "2026-10-26",
                              "entry_time": "2026-10-26T09:50:00-04:00", "raw_entry": 1.0, "reason": "y " * 1000}])
        tsla = [r for r in self.collect()["recent"] if r["kind"] == "blocked"][0]
        self.assertLessEqual(len(tsla["reason"]), narrow.REASON_MAX)

    def test_stale_flag(self):
        # NOW = Mon 2026-10-26 11:00 ET
        self.remake(signals=[], blocked=[], heartbeats=[
            {"generated_at": "2026-10-22T20:00:00+00:00", "session_date": "2026-10-22", "state": "collecting"}])
        data = self.collect()
        self.assertTrue(data["ok"])
        self.assertEqual(data["as_of"], "2026-10-22T20:00:00Z")
        self.assertTrue(data["stale"])  # Thu -> Mon is two trading days
        self.remake(signals=[], blocked=[], heartbeats=[
            {"generated_at": "2026-10-23T20:00:00+00:00", "session_date": "2026-10-23", "state": "collecting"}])
        self.assertFalse(self.collect()["stale"])  # Fri -> Mon is one trading day
        self.remake(signals=[], blocked=[], heartbeats=[])
        data = self.collect()
        self.assertEqual((data["as_of"], data["stale"]), (None, True))
        self.assertEqual(data["latest_by_symbol"], [])

    def test_stale_helper(self):
        now = fakes.NOW
        self.assertFalse(narrow.is_stale("2026-10-26T14:00:00Z", now))
        self.assertFalse(narrow.is_stale("2026-10-24T14:00:00Z", now))  # Sat
        self.assertTrue(narrow.is_stale("2026-10-22T23:59:00Z", now))
        self.assertTrue(narrow.is_stale(None, now))

    def test_oversize_capped_at_150k(self):
        def big(deps):
            data = self.collect()
            data["recent"] = [dict(data["recent"][0], guardrail_reason="z " * 200) for _ in range(2000)]
            return data
        raw = collect.build("narrow", big, self.deps)
        self.assertLessEqual(len(raw), 150 * 1024)
        env = json.loads(raw)
        self.assertTrue(env["ok"])
        self.assertGreater(len(env["data"]["recent"]), 100)

        def blob(deps):
            return {"x": "a " * 100000}
        env = json.loads(collect.build("narrow", blob, self.deps))
        self.assertEqual((env["ok"], env["error"], env["data"]), (False, "snapshot too large", {}))
        self.assertEqual(collect.SECTION_MAX_BYTES["narrow"], 150 * 1024)

    def test_only_cli(self):
        self.assertEqual(collect.parse_args(["--only", "narrow"]).names, ["narrow"])


if __name__ == "__main__":
    unittest.main()
