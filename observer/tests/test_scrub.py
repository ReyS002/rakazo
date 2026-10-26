import unittest

import fakes  # noqa: F401  (sys.path)
import scrub
from scrub import NUM, RED, find_leaks, scrub_text, scrub_tree


class ScrubRules(unittest.TestCase):
    def assertGone(self, text, *secrets):
        out = scrub_text(text)
        for s in secrets:
            self.assertNotIn(s, out, out)
        self.assertEqual(scrub.text_leaks(out), [], out)
        self.assertEqual(scrub_text(out), out, "idempotent")
        return out

    def test_token_runs(self):
        self.assertGone("x abcDEF123456ghiJKL789mnoPQR y", "abcDEF123456ghiJKL789mnoPQR")
        self.assertGone("k=" + "a1" * 20, "a1a1a1a1a1a1")
        self.assertGone("0123456789abcdef0123456789abcdef-fedcba9876543210fedcba9876543210", "0123456789abcdef")
        self.assertGone("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlMTIzNDU2Nzg5",
                        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9")
        self.assertGone("dXNlcjpwYXNzd29yZGZha2Uxx==", "dXNlcjpwYXNzd29yZGZha2U")

    def test_keeps_benign(self):
        for text in ("narrow-gap-v2-shadow-export.log", "/opt/stacks/bull-desk/hermes-bridge-run/observe",
                     "2026-10-26T14:27:00Z", "contact tradingbull@example.com", "AAPL 231.50 long",
                     "abcdefghijklmnopqrstuvwxyzabcdef", "notes/2026/10/26/daily-review.md", "https://example.com/a/b"):
            self.assertEqual(scrub_text(text), text)

    def test_short_alpaca_key_id(self):
        self.assertGone("id PKFAKE1234567890ABCD here", "PKFAKE1234567890ABCD")

    def test_bearer_and_authorization(self):
        self.assertGone("Authorization: Bearer abc.def", "abc.def")
        self.assertGone("authorization=Basic dXNlcjpw", "dXNlcjpw")
        self.assertGone('{"Authorization": "tok123"}', "tok123")
        self.assertGone("bearer short", "short")

    def test_key_value(self):
        for line, secret in (("TELEGRAM_BOT_TOKEN=7123:AAx", "7123:AAx"), ("DB_PASSWORD=hunter2", "hunter2"),
                             ("api_key: abc", "abc"), ('"client_secret": "s3cr3t"', "s3cr3t"),
                             ("MY_PRIVATE=zz", "zz"), ("aws_credential_file=/x/y", "/x/y"), ("PASSWD='p'", "p'")):
            out = self.assertGone(line, secret)
            self.assertIn(RED, out)

    def test_urls(self):
        for url in ("https://h.example.com/webhooks/abc", "https://h.example.com/x?key=1",
                    "http://h.example.com/a?b=c", "https://user:pass@h.example.com/", "wss://h/x?token=t",
                    "https://h.example.com/path/secret=abc"):
            self.assertEqual(scrub_text(f"go {url} now"), f"go {RED} now")

    def test_pem(self):
        pem = "a\n-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\nBBBB\n-----END OPENSSH PRIVATE KEY-----\nb"
        self.assertEqual(scrub_text(pem), f"a\n{RED}\nb")
        self.assertGone("-----BEGIN PRIVATE KEY-----\nMIIEvQ truncated", "MIIEvQ")

    def test_numbers(self):
        self.assertEqual(scrub_text("chat -1001234567890 phone +15551234567"), f"chat {NUM} phone +{NUM}")
        self.assertEqual(scrub_text("price 12345678 ok"), "price 12345678 ok")

    def test_control_chars(self):
        self.assertEqual(scrub_text("a\x1b[31mb‮c"), "a[31mbc")

    def test_tree_keys_values_and_sha(self):
        tree = {"head": "79d8fa07abcd", "subject": "79d8fa07abcd",
                "nested": [{"TOKEN=abc": "x"}, ("Bearer zzz",)], "n": 1234567890, "f": 1.5, "b": None}
        out = scrub_tree(tree)
        self.assertEqual(out["head"], "79d8fa07abcd")
        self.assertEqual(out["n"], 1234567890)
        self.assertEqual(list(out["nested"][0]), ["TOKEN=" + RED])
        self.assertEqual(out["nested"][1], ["Bearer " + RED])
        self.assertEqual(find_leaks(out), [])

    def test_head_must_be_sha(self):
        self.assertEqual(scrub_tree({"head": "Bearer x"})["head"], "Bearer " + RED)

    def test_find_leaks_detects_each_rule(self):
        cases = {"pem": "BEGIN RSA PRIVATE KEY", "url": "https://x/y?z=1", "bearer": "Bearer x",
                 "authorization": "Authorization: x", "key=value": "SECRET=x", "token": "abcDEF123456ghiJKL789mnoPQR",
                 "alpaca-id": "PKFAKE1234567890ABCD", "number": "123456789"}
        for rule, text in cases.items():
            self.assertIn(rule, find_leaks({"x": [text]}), rule)
        self.assertEqual(find_leaks({"x": [scrub_text(t) for t in cases.values()]}), [])

    def test_clip_scrubs_before_truncating(self):
        text = "x" * 190 + " abcDEF123456ghiJKL789mnoPQR"
        out = scrub.clip(text, 200)
        self.assertLessEqual(len(out), 200)
        self.assertNotIn("abcDEF", out)

    def test_planted_secret_lines(self):
        out = scrub_text("\n".join(fakes.SECRET_LINES))
        for s in fakes.SECRETS:
            self.assertNotIn(s, out)
        self.assertIn("tradingbull@example.com", out)
        self.assertIn("nrs-crypto-export", out)


if __name__ == "__main__":
    unittest.main()
