import fcntl
import json
import os
import stat
import unittest
from unittest import mock

import fakes
import collect
import deps as d
import scrub

NAMES = ["health", "scanner", "paper", "projects", "vault", "logs", "narrow", "nrs", "journals", "phasec", "robinhood"]
EXPECTED = {n: "ok" for n in NAMES} | {"robinhood": "error"}  # robinhood: positions need the bridge key -> ok:false


class Collect(unittest.TestCase):
    def setUp(self):
        self.box = fakes.Box()

    def tearDown(self):
        self.box.close()

    def run_all(self, deps=None, names=NAMES):
        return collect.run(names, self.box.out, deps or self.box.deps(), log=fakes.silent)

    def test_no_planted_secret_survives_anywhere(self):
        res = self.run_all()
        self.assertEqual(res, EXPECTED)
        outs = self.box.outputs()
        self.assertEqual(sorted(outs), sorted(n + ".json" for n in NAMES))
        for fname, text in outs.items():
            for s in fakes.SECRETS:
                self.assertNotIn(s, text, f"{s!r} leaked into {fname}")
            env = json.loads(text)
            self.assertEqual(scrub.find_leaks(env), [], fname)
        # the planted material really reached the collector (scrubbed, not silently skipped)
        self.assertIn("[REDACTED]", outs["logs.json"])
        self.assertIn("[NUM]", outs["logs.json"])
        self.assertIn("[REDACTED]", outs["vault.json"])
        self.assertIn("[REDACTED]", outs["projects.json"])
        self.assertIn("tradingbull@example.com", outs["logs.json"])
        self.assertIn("[REDACTED]", outs["narrow.json"])
        for n in ("nrs", "journals", "robinhood"):
            self.assertIn("[REDACTED]", outs[n + ".json"], n)

    def test_envelope_shape(self):
        self.run_all()
        for name, text in self.box.outputs().items():
            env = json.loads(text)
            self.assertEqual(list(env), ["name", "generated_at", "ok", "error", "data"])
            self.assertEqual(env["name"] + ".json", name)
            self.assertRegex(env["generated_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
            self.assertLessEqual(len(text.encode()), collect.MAX_BYTES)
        paper = json.loads(self.box.outputs()["paper.json"])
        self.assertTrue(paper["ok"])
        self.assertIn("not paper", paper["error"])  # partial: bull-live refused

    def test_files_atomic_0644_dir_0755_no_tmp_left(self):
        self.run_all()
        self.assertEqual(stat.S_IMODE(os.stat(self.box.out).st_mode), 0o755)
        for n in os.listdir(self.box.out):
            p = os.path.join(self.box.out, n)
            if n.endswith(".json"):
                self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o644)
            else:
                self.assertTrue(n.endswith(".lock"), n)

    def test_failed_write_keeps_previous_file(self):
        self.run_all(names=["health"])
        before = self.box.outputs()["health.json"]
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.run_all(names=["health"])
        self.assertEqual(self.box.outputs()["health.json"], before)
        self.assertEqual([n for n in os.listdir(self.box.out) if n.endswith(".tmp")], [])

    def test_section_exception_isolated(self):
        def boom(deps):
            raise RuntimeError(f"failed with token={fakes.LOG_TOKEN} " + "x" * 500)
        with mock.patch.dict(collect.SECTIONS, {"vault": boom}):
            res = self.run_all()
        self.assertEqual(res["vault"], "error")
        self.assertEqual(res["health"], "ok")
        env = json.loads(self.box.outputs()["vault.json"])
        self.assertFalse(env["ok"])
        self.assertEqual(env["data"], {})
        self.assertTrue(env["error"].startswith("RuntimeError: failed with token=[REDACTED]"))
        self.assertLessEqual(len(env["error"]), collect.ERROR_MAX)

    def test_leak_after_scrub_drops_section(self):
        def raw(deps):
            return {"line": "\n".join(fakes.SECRET_LINES)}
        with mock.patch.object(collect.scrub, "scrub_tree", side_effect=lambda x: x), \
                mock.patch.dict(collect.SECTIONS, {"logs": raw}):
            self.run_all(names=["logs"])
        env = json.loads(self.box.outputs()["logs.json"])
        self.assertFalse(env["ok"])
        self.assertTrue(env["error"].startswith("scrub check failed:"))
        self.assertEqual(env["data"], {})
        for s in fakes.SECRETS:
            self.assertNotIn(s, self.box.outputs()["logs.json"])

    def test_lock_prevents_overlap(self):
        os.makedirs(self.box.out)
        fd = os.open(os.path.join(self.box.out, ".health.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            res = self.run_all(names=["health", "scanner"])
        finally:
            os.close(fd)
        self.assertEqual(res, {"health": "busy", "scanner": "ok"})
        self.assertNotIn("health.json", self.box.outputs())

    def test_budget_exceeded_skips_and_keeps_old_file(self):
        self.run_all(names=["projects"])
        before = self.box.outputs()["projects.json"]
        ticks = iter([0.0] + [30.0] * 3 + [1000.0] * 1000)
        deadline = d.Deadline(60, clock=lambda: next(ticks))
        deps = self.box.deps(deadline=deadline, runner=d.Runner(deadline, run=self.box.run))
        res = self.run_all(deps, names=["projects", "health"])
        self.assertEqual(res, {"projects": "skipped", "health": "skipped"})
        self.assertEqual(self.box.outputs()["projects.json"], before)
        self.assertNotIn("health.json", self.box.outputs())

    def test_fit_shrinks_big_lists(self):
        env = {"name": "vault", "generated_at": "x", "ok": True, "error": None,
               "data": {"pages": [{"excerpt": "y" * 1000} for _ in range(400)]}}
        raw = collect.fit(env)
        self.assertLessEqual(len(raw), collect.MAX_BYTES)
        out = json.loads(raw)
        self.assertTrue(out["ok"])
        self.assertGreater(len(out["data"]["pages"]), 50)

    def test_fit_gives_up_cleanly(self):
        env = {"name": "x", "generated_at": "x", "ok": True, "error": None, "data": {"blob": "z" * 300000}}
        out = json.loads(collect.fit(env))
        self.assertEqual((out["ok"], out["error"], out["data"]), (False, "snapshot too large", {}))

    def test_cli_args(self):
        a = collect.parse_args(["--only", "health,logs,health", "--out", "/tmp/x", "--once"])
        self.assertEqual((a.names, a.out, a.once), (["health", "logs"], "/tmp/x", True))
        self.assertEqual(collect.parse_args([]).names, NAMES)
        self.assertEqual(collect.parse_args([]).out, "/opt/stacks/bull-desk/hermes-bridge-run/observe")
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            collect.parse_args(["--only", "health,../etc"])

    def test_output_names_are_exact_allowlist(self):
        self.run_all()
        self.assertEqual(sorted(self.box.outputs()), sorted(n + ".json" for n in collect.SECTIONS))
        self.assertEqual(list(collect.SECTIONS), NAMES)


if __name__ == "__main__":
    unittest.main()
