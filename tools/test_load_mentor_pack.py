import contextlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("lmp", os.path.join(HERE, "load-mentor-pack.py"))
lmp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lmp)

def read(p):
    with open(p) as f:
        return f.read()


FAKE_SECRET = "sk-test-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"


def stream(*files, preamble=""):
    # Every marker must start its own line, so separate entries with "\n" when needed.
    out = preamble
    for p, c in files:
        if out and not out.endswith("\n"):
            out += "\n"
        out += "=== FILE: %s ===\n%s" % (p, c)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lmp-test-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.cred = os.path.join(self.tmp, "creds")
        os.mkdir(self.cred)
        with open(os.path.join(self.cred, "x.env"), "w") as f:
            f.write("SHORT=abc\nFAKE_API_KEY=%s\nSOME_URL=https://example.invalid/aaaaaaaaaaaaaaaaaaaa\n"
                    % FAKE_SECRET)
        p = mock.patch.dict(os.environ, {"BD_LOAD_CRED_GLOB": os.path.join(self.cred, "*.env")})
        p.start()
        self.addCleanup(p.stop)
        self.dest = os.path.join(self.tmp, "bots", "bull-mentor")
        os.mkdir(os.path.dirname(self.dest))

    def write_streams(self, s1, s2=""):
        a, b = os.path.join(self.tmp, "s1.md"), os.path.join(self.tmp, "s2.md")
        with open(a, "w") as f:
            f.write(s1)
        with open(b, "w") as f:
            f.write(s2)
        return a, b

    def run_tool(self, s1, s2="", apply=False):
        a, b = self.write_streams(s1, s2)
        out, err = io.StringIO(), io.StringIO()
        argv = ["--stream1", a, "--stream2", b, "--dest", self.dest] + (["--apply"] if apply else [])
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = lmp.main(argv)
        return rc, out.getvalue() + err.getvalue()


class PathTests(unittest.TestCase):
    def test_rejects(self):
        bad = {
            "../etc/passwd.md": "'..' segment",
            "knowledge/../../x.md": "'..' segment",
            "/abs/x.md": "absolute path",
            "knowledge/a$b.md": "bad chars",
            "knowledge/[REDACTED-TOKEN].md": "bad chars",
            "a//b.md": "empty segment",
            "a/./b.md": "'.' segment",
            "a/b/c/d/e/f/g/h/i.md": "depth 9 > 8",
            "x" * 101 + ".md": "segment > 100 chars",
            "knowledge/pic.png": "extension not .md/.txt",
            "knowledge/data.json": "extension not .md/.txt",
            "knowledge/.md": "extension not .md/.txt",
            "INDEX.md": "reserved name",
        }
        for p, why in bad.items():
            self.assertEqual(lmp.check_path(p), why, p)
        self.assertEqual(lmp.check_path("/".join(["abcdefghij"] * 30) + ".md"), "path > 300 chars")

    def test_allows(self):
        for p in ("a/b/c/d/e.md", "a/b/c/d/e/f/g/h.md", "features/x.json.txt", "src/a.ts.txt",
                  "data/t.csv.txt", "notes/My Note v2.md", "INSTRUCTIONS.md"):
            self.assertIsNone(lmp.check_path(p), p)


class ParseTests(unittest.TestCase):
    def test_preamble_and_verbatim(self):
        s = stream(("a.md", "x [REDACTED-TOKEN]\n\n"), ("b.md", "y [REDACTED-URL]"), preamble="hdr\n")
        self.assertEqual(lmp.parse_stream(s), [
            ("_preamble.md", "hdr\n"), ("a.md", "x [REDACTED-TOKEN]\n\n"), ("b.md", "y [REDACTED-URL]")])

    def test_blank_preamble_dropped(self):
        self.assertEqual(lmp.parse_stream("\n\n" + stream(("a.md", "x"))), [("a.md", "x")])


class ToolTests(Base):
    def test_dry_run_writes_nothing(self):
        rc, out = self.run_tool(stream(("knowledge/a.md", "# A\n")))
        self.assertEqual(rc, 0, out)
        self.assertIn("DRY RUN", out)
        self.assertIn("INDEX.md preview:", out)
        self.assertEqual(os.listdir(os.path.dirname(self.dest)), [])

    def test_skips_listed_never_written(self):
        s1 = stream(("../evil.md", "x"), ("/abs.md", "x"), ("a/b$.md", "x"),
                    ("a/b/c/d/e/f/g/h/i.md", "x"), ("a/b/c/d/e.md", "ok"), ("img/p.png", "x"))
        rc, out = self.run_tool(s1, apply=True)
        self.assertEqual(rc, 0, out)
        for p in ("../evil.md", "/abs.md", "a/b$.md", "a/b/c/d/e/f/g/h/i.md", "img/p.png"):
            self.assertIn(p, out)
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "a/b/c/d/e.md")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "bots", "evil.md")))
        self.assertFalse(os.path.exists(os.path.join(self.dest, "img")))
        self.assertFalse(os.path.exists(os.path.join(self.dest, "a/b/c/d/e/f")))
        log = json.loads(read(os.path.join(self.dest, "LOAD_LOG.json")))
        self.assertEqual(log["counts"]["skipped"], 5)

    def test_secret_aborts_without_value(self):
        s1 = stream(("knowledge/a.md", "# A\n"), ("notes/leak.md", "token: %s\n" % FAKE_SECRET))
        rc, out = self.run_tool(s1, apply=True)
        self.assertEqual(rc, 3)
        self.assertIn("FAKE_API_KEY", out)
        self.assertIn("notes/leak.md", out)
        self.assertNotIn(FAKE_SECRET, out)
        self.assertEqual(os.listdir(os.path.dirname(self.dest)), [])

    def test_secret_comma_split_and_url_ignored(self):
        with open(os.path.join(self.cred, "y.env"), "w") as f:
            f.write('export MULTI="short,%s"\n' % ("Q" * 25))
        rc, out = self.run_tool(stream(("a.md", "https://example.invalid/aaaaaaaaaaaaaaaaaaaa")))
        self.assertEqual(rc, 0, out)
        rc, out = self.run_tool(stream(("a.md", "x" + "Q" * 25)))
        self.assertEqual(rc, 3)
        self.assertIn("MULTI", out)
        self.assertNotIn("Q" * 25, out)

    def test_allow_var_skips_only_that_name(self):
        with open(os.path.join(self.cred, "z.env"), "w") as f:
            f.write("BRAND_NAME=The Trading Bull Academy Desk\n")
        s1 = stream(("a.md", "Welcome to The Trading Bull Academy Desk\n"))
        rc, out = self.run_tool(s1)
        self.assertEqual(rc, 3)
        self.assertIn("BRAND_NAME in a.md", out)
        a, b = self.write_streams(s1)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = lmp.main(["--stream1", a, "--stream2", b, "--dest", self.dest, "--allow-var", "BRAND_NAME"])
        self.assertEqual(rc, 0, buf.getvalue())
        # allowing one name must not disable the others
        a, b = self.write_streams(stream(("a.md", FAKE_SECRET)))
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = lmp.main(["--stream1", a, "--stream2", b, "--dest", self.dest, "--allow-var", "BRAND_NAME"])
        self.assertEqual(rc, 3)
        self.assertNotIn(FAKE_SECRET, buf.getvalue())

    def test_private_key_aborts(self):
        rc, out = self.run_tool(stream(("a.md", "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n")))
        self.assertEqual(rc, 3)
        self.assertIn("PRIVATE_KEY_HEADER in a.md", out)

    def test_collision_stream2_wins(self):
        rc, out = self.run_tool(stream(("k/a.md", "one"), ("k/b.md", "b")),
                                stream(("k/a.md", "two")), apply=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("k/a.md  (stream2 wins over stream1)", out)
        self.assertEqual(read(os.path.join(self.dest, "k/a.md")), "two")
        log = json.loads(read(os.path.join(self.dest, "LOAD_LOG.json")))
        self.assertEqual(log["collisions"], [{"path": "k/a.md", "winner": "stream2", "loser": "stream1"}])
        self.assertEqual(log["files"]["k/a.md"]["stream"], "stream2")

    def test_preamble_written(self):
        rc, out = self.run_tool(stream(("a.md", "x"), preamble="# Pack header\n"), apply=True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(os.path.join(self.dest, "_preamble.md")), "# Pack header\n")

    def test_atomic_replace_keeps_old_and_idempotent(self):
        s1 = stream(("knowledge/a.md", "# A\n"), ("features/f.md", "F"))
        rc, _ = self.run_tool(s1, apply=True)
        self.assertEqual(rc, 0)
        marker = os.path.join(self.dest, "marker.txt")
        with open(marker, "w") as f:
            f.write("first")
        first = sorted(os.listdir(self.dest))
        rc, out = self.run_tool(s1, apply=True)
        self.assertEqual(rc, 0, out)
        parent = os.path.dirname(self.dest)
        olds = [n for n in os.listdir(parent) if n.startswith("bull-mentor.old.")]
        self.assertEqual(len(olds), 1)
        self.assertTrue(os.path.isfile(os.path.join(parent, olds[0], "marker.txt")))
        self.assertFalse(any(".tmp." in n for n in os.listdir(parent)))
        first.remove("marker.txt")
        self.assertEqual(sorted(os.listdir(self.dest)), first)
        self.assertEqual(oct(os.stat(os.path.join(self.dest, "knowledge/a.md")).st_mode & 0o777), "0o644")
        self.assertEqual(oct(os.stat(os.path.join(self.dest, "knowledge")).st_mode & 0o777), "0o755")
        # third run in the same second must not clobber the existing .old
        rc, _ = self.run_tool(s1, apply=True)
        self.assertEqual(rc, 0)
        self.assertEqual(len([n for n in os.listdir(parent) if ".old." in n]), 2)

    def test_dest_symlink_refused(self):
        real = os.path.join(self.tmp, "elsewhere")
        os.mkdir(real)
        os.symlink(real, self.dest)
        rc, out = self.run_tool(stream(("a.md", "x")), apply=True)
        self.assertEqual(rc, 2)
        self.assertIn("symlink", out)
        self.assertEqual(os.listdir(real), [])

    def test_index_content(self):
        s1 = stream(("knowledge/a.md", "\n# Alpha Heading\nbody\n"),
                    ("knowledge-drafts/d.md", "first line no heading\n"),
                    ("features/f.json.txt", "{}\n"),
                    ("other/o.md", "# hidden from listing\n"),
                    ("INSTRUCTIONS.md", "# I\n"))
        rc, out = self.run_tool(s1, apply=True)
        self.assertEqual(rc, 0, out)
        idx = read(os.path.join(self.dest, "INDEX.md"))
        self.assertIn("/workspace/bull-mentor", idx)
        self.assertIn("[REDACTED-TOKEN]", idx)
        self.assertIn("[REDACTED-URL]", idx)
        self.assertIn("- knowledge: 1 files, 22 bytes", idx)
        self.assertIn("- knowledge-drafts: 1 files, 22 bytes - DRAFT (not approved doctrine)", idx)
        self.assertIn("- (root): 1 files", idx)
        self.assertIn("- knowledge/a.md - Alpha Heading - 22 bytes", idx)
        self.assertIn("- knowledge-drafts/d.md - first line no heading - 22 bytes", idx)
        self.assertIn("- features/f.json.txt - {} - 3 bytes", idx)
        self.assertIn("## knowledge-drafts/ (DRAFT - not approved doctrine)", idx)
        self.assertNotIn("other/o.md", idx)
        log = json.loads(read(os.path.join(self.dest, "LOAD_LOG.json")))
        self.assertEqual(log["counts"]["files"], 5)
        self.assertEqual(len(log["files"]["knowledge/a.md"]["sha256"]), 64)
        self.assertIn("loaded_at", log)

    def test_heading_truncated(self):
        self.assertEqual(len(lmp.first_heading(("# " + "z" * 300).encode())), 100)

    def test_file_cap_2001(self):
        s1 = stream(*[("k/f%04d.md" % i, "x") for i in range(2001)])
        rc, out = self.run_tool(s1)
        self.assertEqual(rc, 0, out)
        self.assertIn("total: 2000 files", out)
        self.assertIn("k/f2000.md  -- file cap 2000", out)

    def test_size_cap(self):
        big = "y" * (4 * 1024 * 1024)
        rc, out = self.run_tool(stream(("a.md", big), ("b.md", big)))
        self.assertEqual(rc, 0, out)
        self.assertIn("b.md  -- size cap", out)


if __name__ == "__main__":
    unittest.main()
