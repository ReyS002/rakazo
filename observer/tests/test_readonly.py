import unittest

import fakes
import collect
import deps as d

ALLOWED_SHAPES = {
    ("git", "status"), ("git", "rev-parse"), ("git", "log"), ("git", "rev-list"),
    ("docker", "ps"), ("docker", "inspect"), ("docker", "logs"),
    ("systemctl", "show"), ("systemctl", "is-active"), ("df", None),
}


def shape(argv):
    if argv[0] == "git":
        return ("git", argv[len(d.GIT_PREFIX)])
    if argv[0] == "df":
        return ("df", None)
    return (argv[0], argv[1])


class ReadOnly(unittest.TestCase):
    def setUp(self):
        self.box = fakes.Box()

    def tearDown(self):
        self.box.close()

    def test_full_run_only_get_and_allowlisted_argvs(self):
        res = collect.run(list(collect.SECTIONS), self.box.out, self.box.deps(), log=fakes.silent)
        self.assertEqual(set(res), set(collect.SECTIONS))
        self.assertTrue(self.box.run.calls)
        for argv in self.box.run.calls:
            d.check_argv(argv)  # raises if not allowlisted
            self.assertIn(shape(argv), ALLOWED_SHAPES, argv)
        self.assertTrue(self.box.opener.calls)
        for method, url, headers, body in self.box.opener.calls:
            self.assertEqual(method, "GET", url)
            self.assertIsNone(body)
            self.assertTrue(url.startswith(("https://paper-api.alpaca.markets/", "https://api-fxpractice.oanda.com/")))
            for s in fakes.SECRETS:
                self.assertNotIn(s, url.replace(fakes.OANDA_ACCT, ""))  # account id is part of the OANDA path

    def test_check_argv_rejects(self):
        bad = [
            ("rm", "-rf", "/"), ("sh", "-c", "ls"), ("python3", "x.py"),
            ("git", "push"), ("git", "status"), d.GIT_PREFIX + ("log", "-1", "--output=/tmp/x"),
            d.GIT_PREFIX + ("checkout", "main"), ("git", "-c", "core.pager=sh", "log"),
            ("docker", "exec", "x", "sh"), ("docker", "rm", "x"), ("docker", "run", "img"),
            d.DOCKER_LOGS + ("-f",), d.DOCKER_LOGS + ("a", "b"), d.DOCKER_LOGS + ("x;rm",),
            d.DOCKER_INSPECT + ("--format", "x"), ("docker", "inspect", "x"),
            ("systemctl", "restart", "docker"), ("systemctl", "stop", "x"), ("systemctl", "is-active", "--user"),
            d.SYSTEMCTL_SHOW + ("x y",), ("df", "-h"), (),
        ]
        for argv in bad:
            with self.assertRaises(d.CommandRefused, msg=argv):
                d.check_argv(argv)

    def test_runner_never_executes_refused_argv(self):
        run = fakes.FakeRun()
        runner = d.Runner(d.Deadline(60), run=run)
        with self.assertRaises(d.CommandRefused):
            runner(("docker", "exec", "x", "sh"))
        self.assertEqual(run.calls, [])

    def test_http_refuses_other_hosts_schemes_methods(self):
        opener = fakes.FakeOpener()
        get = d.HttpGet(d.Deadline(60), opener=opener)
        for url in ("https://api.alpaca.markets/v2/account", "https://api-fxtrade.oanda.com/v3/accounts",
                    "http://paper-api.alpaca.markets/v2/account", "https://u:p@paper-api.alpaca.markets/v2",
                    "https://paper-api.alpaca.markets:8443/v2", "file:///etc/passwd"):
            with self.assertRaises(d.HttpError, msg=url):
                get(url)

        class Post(d.HttpGet):
            METHOD = "POST"

        with self.assertRaises(d.HttpError):
            Post(d.Deadline(60), opener=opener)("https://paper-api.alpaca.markets/v2/account")
        self.assertEqual(opener.calls, [])

    def test_http_error_message_has_no_url(self):
        opener = fakes.FakeOpener(fail_substr=("/v2/account",))
        get = d.HttpGet(d.Deadline(60), opener=opener)
        with self.assertRaises(d.HttpError) as cm:
            get("https://paper-api.alpaca.markets/v2/account?x=1", {"APCA-API-KEY-ID": "k"})
        self.assertEqual(str(cm.exception), "HTTP 500")

    def test_real_opener_has_no_redirect_or_proxy(self):
        get = d.HttpGet(d.Deadline(60))
        kinds = {type(h).__name__ for h in get.opener.handlers}
        self.assertIn("_NoRedirect", kinds)
        self.assertNotIn("HTTPRedirectHandler", kinds)

    def test_budget_exhausted_raises_before_call(self):
        clock = iter([0.0, 100.0, 100.0, 100.0]).__next__
        dl = d.Deadline(60, clock=clock)
        run = fakes.FakeRun()
        with self.assertRaises(d.BudgetExceeded):
            d.Runner(dl, run=run)(d.DF)
        self.assertEqual(run.calls, [])

    def test_no_shell_strings(self):
        # all fixed shapes are tuples of plain strings; nothing is interpolated into a shell
        for argv in (d.GIT_STATUS, d.GIT_LOG, d.DOCKER_PS, d.DF, d.SYSTEMCTL_SHOW):
            self.assertTrue(all(isinstance(a, str) for a in argv))


if __name__ == "__main__":
    unittest.main()
