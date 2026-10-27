import json
import os
import unittest

import fakes
import collect
import deps as d
import scrub
from sections import robinhood


class Robinhood(unittest.TestCase):
    def setUp(self):
        self.box = fakes.Box()
        self.deps = self.box.deps()

    def tearDown(self):
        self.box.close()

    def envelope(self):
        collect.run(["robinhood"], self.box.out, self.deps, log=fakes.silent)
        with open(os.path.join(self.box.out, "robinhood.json"), encoding="utf-8") as fh:
            return json.loads(fh.read())

    def test_health_only_positions_unreadable(self):
        data, ok, err = robinhood.collect(self.deps)
        self.assertFalse(ok)
        self.assertIn("authenticated POST /v1/tools", err)
        self.assertIn("not authenticated", err)
        self.assertEqual(data["account_label"], "robinhood")
        self.assertEqual(data["bridge"], {"host": "127.0.0.1", "port": 28188, "route": "GET /health",
                                          "reachable": True, "http_status": 503, "connected": False,
                                          "authenticated": False, "authorization_pending": False,
                                          "last_error": data["bridge"]["last_error"]})
        self.assertIsNone(data["positions"])
        self.assertEqual(self.box.local_opener.calls, [("GET", "http://127.0.0.1:28188/health", {}, None)])

    def test_authenticated_bridge_still_ok_false(self):
        self.box.local_opener.mode = "health200"
        data, ok, err = robinhood.collect(self.deps)
        self.assertFalse(ok)
        self.assertTrue(data["bridge"]["authenticated"])
        self.assertNotIn("not authenticated", err)
        self.assertNotIn("positions", json.dumps(data["bridge"]))

    def test_unreachable_and_foreign_listener(self):
        self.box.local_opener.mode = "refused"
        data, ok, err = robinhood.collect(self.deps)
        self.assertFalse(ok)
        self.assertTrue(err.startswith("bridge not reachable on 127.0.0.1:28188"), err)
        self.box.local_opener.mode = "foreign"
        data, ok, err = robinhood.collect(self.deps)
        self.assertIn("without the bridge's health shape", err)
        data, ok, err = robinhood.collect(self.box.deps(local_get=None))
        self.assertEqual(err, "no loopback client configured")

    def test_scrubbed_no_account_numbers(self):
        self.box.local_opener.mode = "health200"
        env = self.envelope()
        text = json.dumps(env)
        for s in fakes.SECRETS + ["5RH123456789", "account_number"]:
            self.assertNotIn(s, text)
        self.assertEqual(scrub.find_leaks(env), [])
        self.assertFalse(env["ok"])
        self.assertIn("[REDACTED]", env["data"]["bridge"]["last_error"])

    # ── guard: GET + explicit allowlist only ──────────────────────────────

    def test_local_get_refuses_non_get_and_other_paths(self):
        opener = fakes.FakeLocalOpener("health200")
        get = d.LocalGet(d.Deadline(60), opener=opener)
        for port, path in ((28188, "/v1/tools"), (28188, "/v1/tools/get_positions"), (28188, "/oauth/start"),
                           (28188, "/oauth/callback"), (28188, "/health?x=1"), (28188, "/health/"),
                           (28188, "/health/../v1/tools"), (28188, "//health"), (28188, "/HEALTH"), (28188, ""),
                           (28188, "/orders"), (28188, "/arm"), (28188, "/authorize"), (9999, "/health"),
                           (80, "/health"), ("28188", "/health"), (True, "/health")):
            with self.assertRaises(d.HttpError, msg=(port, path)):
                get(port, path)

        class Post(d.LocalGet):
            METHOD = "POST"

        class Put(d.LocalGet):
            METHOD = "PUT"

        for cls in (Post, Put):
            with self.assertRaises(d.HttpError):
                cls(d.Deadline(60), opener=opener)(28188, "/health")
        for m in ("POST", "PUT", "DELETE", "PATCH", "HEAD"):
            with self.assertRaises(d.HttpError):
                d.LocalGet.check(m, 28188, "/health")
        self.assertEqual(opener.calls, [])
        self.assertEqual(d.LOCAL_ROUTES, {28188: frozenset({"/health"})})
        self.assertEqual(get(28188, "/health")[0], 200)
        self.assertEqual(opener.calls[0][:2], ("GET", "http://127.0.0.1:28188/health"))
        self.assertEqual(opener.calls[0][2], {})  # no credential header ever sent

    def test_real_local_opener_no_redirect_or_proxy(self):
        kinds = {type(h).__name__ for h in d.LocalGet(d.Deadline(60)).opener.handlers}
        self.assertIn("_NoRedirect", kinds)
        self.assertNotIn("HTTPRedirectHandler", kinds)


if __name__ == "__main__":
    unittest.main()
