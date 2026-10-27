import os
import unittest

import fakes
from sections import health, logs, projects, scanner, vault


def unwrap(res):
    return res[0] if isinstance(res, tuple) else res


class Sections(unittest.TestCase):
    def setUp(self):
        self.box = fakes.Box()
        self.deps = self.box.deps()

    def tearDown(self):
        self.box.close()

    def test_health_contract(self):
        data = health.collect(self.deps)
        self.assertNotIsInstance(data, tuple)
        self.assertEqual(set(data), {"containers", "units", "resources", "cron_last"})
        c = data["containers"][0]
        self.assertEqual(set(c), {"name", "image", "status", "health", "started_at", "restart_count"})
        self.assertEqual(c["name"], "bull-desk-api-1")
        self.assertEqual(c["health"], "healthy")
        self.assertEqual(c["restart_count"], 3)
        self.assertEqual(c["started_at"], "2026-10-25T10:00:00Z")  # nanoseconds dropped, not "[NUM]"
        self.assertEqual(data["containers"][1]["health"], None)
        names = [u["name"] for u in data["units"]]
        self.assertIn("hermes-gateway", names)
        self.assertIn("hermes-robinhood-bridge", names)  # fixed unit kept even when not found
        self.assertIn("tba-fires.timer", names)
        self.assertNotIn("bull-ghost", names)
        self.assertEqual(set(data["units"][0]), {"name", "active", "sub"})
        r = data["resources"]
        self.assertEqual(set(r), {"load1", "load5", "load15", "mem_used_pct", "swap_used_pct", "disk", "uptime_s"})
        self.assertEqual((r["load1"], r["mem_used_pct"], r["swap_used_pct"], r["uptime_s"]), (0.52, 25.0, 25.0, 123456))
        self.assertEqual(r["disk"], [{"mount": "/", "used_pct": 42}, {"mount": "/mnt/my data", "used_pct": 99}])
        jobs = {j["job"]: j for j in data["cron_last"]}
        self.assertEqual(set(jobs), {"stock-alerts", "forex-alerts", "grokbot-push", "nrs-crypto-export"})
        self.assertEqual(jobs["stock-alerts"]["last_log_age_s"], 60)
        self.assertTrue(jobs["stock-alerts"]["last_line"].startswith("last line [REDACTED] [NUM]"))
        self.assertEqual(set(jobs["stock-alerts"]), {"job", "last_log_age_s", "last_line"})

    def test_health_partial_when_docker_down(self):
        self.box.run.fail = {"docker"}
        data, ok, err = health.collect(self.deps)
        self.assertTrue(ok)
        self.assertEqual(data["containers"], [])
        self.assertIn("containers", err)

    def test_scanner_contract(self):
        data = scanner.collect(self.deps)
        self.assertEqual(set(data), {"recent_fires", "feed_updated_age_s", "push_status", "scanner_cron"})
        fires = data["recent_fires"]
        self.assertEqual([f["kind"] for f in fires], ["stock", "forex", "crypto"])  # oldest -> newest, bogus dropped
        self.assertEqual(fires[0], {"time": "2026-10-26T13:00:00Z", "kind": "stock", "symbol": "AAPL",
                                    "setup": "nr7 breakout", "price": 231.5, "late": False})
        self.assertEqual(fires[1]["source"], "nrs")
        self.assertEqual(fires[2]["price"], 67234.56)
        self.assertEqual(data["feed_updated_age_s"], 30)
        ps = data["push_status"]
        self.assertEqual(ps["grok"]["BULLFX"], {"configured": True, "last_success": "2026-10-26T13:00:00Z",
                                                "consec_fail": 0, "sent_today": 3, "dead": 1})
        self.assertFalse(ps["grok"]["TICKERBULL"]["configured"])
        self.assertIsNone(ps["grok"]["TICKERBULL"]["last_success"])
        self.assertEqual(list(ps["bulldesk"]), ["BD_BULLFX"])
        self.assertNotIn("junk", ps["grok"])
        cron = {c["name"]: c["last_run_age_s"] for c in data["scanner_cron"]}
        self.assertEqual(cron["stock-alerts"], 60)
        self.assertIsNone(cron["options-alerts"])

    def test_scanner_cron_sources_and_retired(self):
        fakes.write(self.box.root, "/opt/logs/tba/fires-export.log", "", mtime=fakes.NOW - 86400 * 14)
        fakes.write(self.box.root, "/opt/logs/tba/narrow-gap-shadow-export.log", "x\n", mtime=fakes.NOW - 86400)
        rows = {r["name"]: r for r in scanner.collect(self.deps)["scanner_cron"]}
        self.assertEqual(list(rows), list(scanner.SCANNER_LOGS))
        # fires-export: age of the fires feed (NOW-30), not the 14-day-old empty log
        self.assertEqual(rows["fires-export"], {"name": "fires-export", "source": "output_file_mtime",
                                                "last_run_age_s": 30})
        self.assertEqual(rows["narrow-gap-shadow-export"],
                         {"name": "narrow-gap-shadow-export", "source": "log_mtime", "last_run_age_s": 86400,
                          "retired": True, "note": "retired; replaced by narrow-gap-v2-shadow-export"})
        # everything else unchanged apart from the added source field
        self.assertEqual(rows["stock-alerts"], {"name": "stock-alerts", "source": "log_mtime", "last_run_age_s": 60})
        self.assertEqual(rows["options-alerts"], {"name": "options-alerts", "source": "log_mtime",
                                                  "last_run_age_s": None})
        retired = [n for n, r in rows.items() if r.get("retired")]
        self.assertEqual(retired, ["narrow-gap-shadow-export"])

    def test_scanner_cron_missing_output_file(self):
        os.remove(os.path.join(self.box.root, "opt/stacks/ops-status/scanner-fires-latest.jsonl"))
        fakes.write(self.box.root, "/opt/logs/tba/fires-export.log", "", mtime=fakes.NOW)
        rows = {r["name"]: r for r in scanner.collect(self.deps)["scanner_cron"]}
        self.assertEqual(rows["fires-export"], {"name": "fires-export", "source": "output_file_mtime",
                                                "last_run_age_s": None, "note": "output file missing"})

    def test_scanner_caps_to_50(self):
        path = os.path.join(self.box.root, "opt/stacks/ops-status/scanner-fires-latest.jsonl")
        with open(path, "w") as f:
            for i in range(120):
                f.write('{"ts": %d, "kind": "stock", "symbol": "S%d", "setup": "x", "price": 1}\n' % (1790000000 + i, i))
        fires = scanner.collect(self.deps)["recent_fires"]
        self.assertEqual(len(fires), 50)
        self.assertEqual(fires[-1]["symbol"], "S119")

    def test_scanner_push_status_failure_is_partial(self):
        def boom():
            raise ImportError("no module")
        data, ok, err = scanner.collect(self.box.deps(push_status=boom))
        self.assertTrue(ok)
        self.assertEqual(data["push_status"], {"grok": {}, "bulldesk": {}})
        self.assertIn("push_status", err)

    def test_projects_contract(self):
        repos = projects.collect(self.deps)["repos"]
        self.assertEqual([r["path"] for r in repos], ["/opt/scripts/tba", "/opt/stacks/bull-desk", "/root/rakazo"])
        r = repos[0]
        self.assertEqual(set(r), {"path", "branch", "head", "subject", "dirty_count", "dirty_files", "ahead", "behind",
                                  "last_commit_age_s"})
        self.assertEqual((r["branch"], r["head"], r["ahead"], r["behind"], r["dirty_count"], r["last_commit_age_s"]),
                         ("main", "79d8fa07abcd", 5, 2, 4, 3600))
        self.assertEqual(r["dirty_files"][:3], ["app.py", "notes/new.md", "new.txt"])
        cwds = {c for c in self.box.run.calls if c[0] == "git"}
        self.assertTrue(cwds)

    def test_projects_git_errors_ignored(self):
        self.box.run.fail = {"git"}
        r = projects.collect(self.deps)["repos"][0]
        self.assertEqual((r["branch"], r["head"], r["dirty_count"]), (None, None, None))

    def test_vault_contract_and_skips(self):
        pages = vault.collect(self.deps)["pages"]
        self.assertEqual([p["path"] for p in pages], ["Home.md", "notes/plan.md", "notes/untitled.md"])
        self.assertEqual(set(pages[0]), {"path", "title", "mtime", "excerpt"})
        self.assertEqual(pages[0]["title"], "Home [REDACTED]")
        self.assertEqual(pages[1]["title"], "Plan")
        self.assertEqual(pages[2]["title"], "untitled")
        for p in pages:
            self.assertLessEqual(len(p["excerpt"]), 200)

    def test_logs_contract_and_symlink_escape(self):
        data, ok, err = unwrap_partial(logs.collect(self.deps))
        names = [f["name"] for f in data["files"]]
        self.assertNotIn("tba/escape.log", names)
        self.assertIn("tba/stock-alerts.log", names)
        self.assertIn("docker/bull-desk-api-1", names)
        self.assertIn("docker/oanda-bridge", names)
        self.assertNotIn("docker/postgres", names)
        f = data["files"][0]
        self.assertEqual(set(f), {"name", "mtime_age_s", "tail"})
        self.assertLessEqual(len(f["tail"]), 40)
        self.assertTrue(all(len(ln) <= 300 for ln in f["tail"]))
        joined = "\n".join(f["tail"])
        self.assertIn("[REDACTED]", joined)
        self.assertNotIn("OUTSIDE", str(data))

    def test_logs_symlink_inside_dir_allowed(self):
        os.symlink("stock-alerts.log", os.path.join(self.box.root, "opt/logs/tba/alias.log"))
        names = [f["name"] for f in unwrap(logs.collect(self.deps))["files"]]
        self.assertIn("tba/alias.log", names)


def unwrap_partial(res):
    return res if isinstance(res, tuple) else (res, True, None)


if __name__ == "__main__":
    unittest.main()
