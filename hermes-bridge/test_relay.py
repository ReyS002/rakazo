import contextlib
import datetime
import hashlib
import hmac
import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import relay

HERE = os.path.dirname(os.path.abspath(__file__))
KEY = "k" * 40
OTHER_KEY = "z" * 40
SECRET = "test-hmac-secret-not-real"


class FakeUpstream:
    def __init__(self, status=200):
        self.requests = []
        self.status = status
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append((dict(self.headers), body))
                self.send_response(outer.status)
                if outer.status in (301, 302):
                    self.send_header("Location", "http://127.0.0.1:1/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/webhook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.kill = os.path.join(self.tmp.name, "KILL")
        patcher = unittest.mock.patch.object(relay, "KILL_FILE", self.kill)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.upstream = FakeUpstream()
        config = relay.load_config(
            {"BRIDGE_KEYS": f" {KEY} , ,{OTHER_KEY}", "BRIDGE_HMAC_SECRET": SECRET, "HERMES_WEBHOOK_URL": self.upstream.url}
        )
        self.limiter = relay.RateLimiter()
        self.server = relay.make_server(config, host="127.0.0.1", port=0, limiter=self.limiter)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.stdout = io.StringIO()
        redirect = contextlib.redirect_stdout(self.stdout)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.upstream.close()
        self.tmp.cleanup()

    def call(self, path="/notify", method="POST", body=None, key=KEY, headers=None):
        hdrs = dict(headers or {})
        if key is not None:
            hdrs["Authorization"] = f"Bearer {key}"
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        if data is not None:
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as err:
            with err:
                return err.code, err.headers, err.read()

    def test_health(self):
        status, _, body = self.call("/health", "GET", key=None)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})

    def test_routing(self):
        self.assertEqual(self.call("/nope", "GET")[0], 404)
        self.assertEqual(self.call("/nope", "POST", {"message": "x"})[0], 404)
        self.assertEqual(self.call("/notify", "GET")[0], 405)
        self.assertEqual(self.call("/notify", "PUT", {"message": "x"})[0], 405)
        self.assertEqual(self.call("/health", "POST", {"message": "x"})[0], 405)

    def test_auth(self):
        self.assertEqual(self.call(body={"message": "x"}, key=None)[0], 401)
        self.assertEqual(self.call(body={"message": "x"}, key="w" * 40)[0], 403)
        self.assertEqual(self.call(body={"message": "x"}, key="")[0], 403)
        self.assertEqual(
            self.call(body={"message": "x"}, key=None, headers={"Authorization": f"Basic {KEY}"})[0], 403
        )
        self.assertEqual(self.call(body={"message": "x"}, key=OTHER_KEY)[0], 202)
        self.assertEqual(len(self.upstream.requests), 1)

    def test_too_large(self):
        status, _, _ = self.call(body={"message": "x" * 5000})
        self.assertEqual(status, 413)
        self.assertEqual(self.upstream.requests, [])

    def test_missing_length(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        conn.putrequest("POST", "/notify")
        conn.putheader("Authorization", f"Bearer {KEY}")
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 411)
        conn.close()

    def test_unprocessable(self):
        self.limiter.windows[0] = (60.0, 100, self.limiter.windows[0][2])
        bad = [
            b"not json",
            b"[1,2]",
            {"title": "t"},
            {"message": ""},
            {"message": "\x00\x01 "},
            {"message": 5},
            {"message": "x" * 2001},
            {"message": "x", "title": "t" * 121},
            {"message": "x", "from": "f" * 41},
            {"message": "x", "title": ["t"]},
            b"\xff\xfe",
        ]
        for body in bad:
            with self.subTest(body=body):
                self.assertEqual(self.call(body=body)[0], 422)
        self.assertEqual(self.upstream.requests, [])

    def test_rate_limit(self):
        for _ in range(relay.PER_MINUTE):
            self.assertEqual(self.call(body={"message": "x"})[0], 202)
        status, headers, _ = self.call(body={"message": "x"})
        self.assertEqual(status, 429)
        self.assertGreaterEqual(int(headers["Retry-After"]), 1)
        self.assertEqual(len(self.upstream.requests), relay.PER_MINUTE)

    def test_rate_limit_windows(self):
        now = [0.0]
        limiter = relay.RateLimiter(per_minute=10, per_day=100, clock=lambda: now[0])
        for _ in range(10):
            self.assertEqual(limiter.acquire(), 0)
        self.assertEqual(limiter.acquire(), 60)
        now[0] = 60.0
        self.assertEqual(limiter.acquire(), 0)
        sent = 11
        while sent < 100:
            now[0] += 6.0
            self.assertEqual(limiter.acquire(), 0)
            sent += 1
        now[0] += 6.0
        self.assertGreater(limiter.acquire(), 3600)
        now[0] = 86400.0
        self.assertEqual(limiter.acquire(), 0)

    def test_kill_switch(self):
        open(self.kill, "w").close()
        self.assertEqual(self.call(body={"message": "x"})[0], 503)
        os.remove(self.kill)
        self.assertEqual(self.call(body={"message": "x"})[0], 202)

    def test_signature_and_reserialized_body(self):
        raw = b'{ "message" : "hi\\u0007 there\\nline2" ,"title":"T\\u202e","extra":1,  "from":"bot-1" }'
        status, _, body = self.call(body=raw)
        self.assertEqual((status, json.loads(body)), (202, {"ok": True}))
        (headers, sent), = self.upstream.requests
        self.assertEqual(sent, json.dumps({"title": "T", "message": "hi there\nline2", "from": "bot-1"}).encode())
        self.assertNotEqual(sent, raw)
        ts = headers["X-Webhook-Timestamp"]
        self.assertTrue(ts.isdigit())
        expected = hmac.new(SECRET.encode(), ts.encode() + b"." + sent, hashlib.sha256).hexdigest()
        self.assertTrue(hmac.compare_digest(headers["X-Webhook-Signature-V2"], expected))
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertNotIn("Authorization", headers)

    def test_defaults(self):
        self.assertEqual(self.call(body={"message": "x"})[0], 202)
        (_, sent), = self.upstream.requests
        self.assertEqual(json.loads(sent), {"title": "", "message": "x", "from": "bull-desk"})

    def test_upstream_failure(self):
        self.upstream.status = 500
        self.assertEqual(self.call(body={"message": "x"})[0], 502)

    def test_no_redirect_follow(self):
        self.upstream.status = 302
        self.assertEqual(self.call(body={"message": "x"})[0], 502)
        self.assertEqual(len(self.upstream.requests), 1)

    def test_upstream_down(self):
        self.upstream.close()
        self.assertEqual(self.call(body={"message": "x"})[0], 502)
        self.upstream = FakeUpstream()

    def test_logs_have_no_secrets(self):
        self.call(body={"message": "secret-message-body", "title": "secret-title", "from": "labelA"})
        self.call(body={"message": "x"}, key="w" * 40)
        self.call(body=b"{bad")
        out = self.stdout.getvalue()
        for needle in (KEY, OTHER_KEY, "w" * 40, SECRET, "secret-message-body", "secret-title", "Bearer", "Signature"):
            self.assertNotIn(needle, out)
        self.assertIn("status=202", out)
        self.assertIn("from=labelA", out)
        self.assertIn("status=403", out)
        self.assertIn("status=422", out)


TASK_KEY = "t" * 40
TASK_SECRET = "test-task-hmac-secret-not-real"
TASK_ID_RE = r"^bd-\d{8}-\d{6}-[0-9a-f]{4}$"


class TaskRouteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.kill = os.path.join(self.tmp.name, "KILL")
        self.kill_task = os.path.join(self.tmp.name, "KILL_TASK")
        for name, value in (("KILL_FILE", self.kill), ("KILL_TASK_FILE", self.kill_task)):
            patcher = unittest.mock.patch.object(relay, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.notify_upstream = FakeUpstream()
        self.upstream = FakeUpstream()
        self.addCleanup(self.notify_upstream.close)
        self.addCleanup(self.upstream.close)
        self.stdout = io.StringIO()
        redirect = contextlib.redirect_stdout(self.stdout)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.start(
            {
                "BRIDGE_KEYS": KEY,
                "BRIDGE_HMAC_SECRET": SECRET,
                "HERMES_WEBHOOK_URL": self.notify_upstream.url,
                "TASK_KEYS": f"{TASK_KEY}, ",
                "TASK_HMAC_SECRET": TASK_SECRET,
                "HERMES_TASK_URL": self.upstream.url,
            }
        )

    def start(self, env, task_limiter=None):
        if getattr(self, "server", None):
            self.server.shutdown()
            self.server.server_close()
        self.task_limiter = task_limiter or relay.make_task_limiter()
        self.server = relay.make_server(relay.load_config(env), host="127.0.0.1", port=0, task_limiter=self.task_limiter)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    call = RelayTest.call

    def task(self, body=None, key=TASK_KEY, **kw):
        if body is None:
            body = {"title": "Build a thing", "task": "make a CLI that adds numbers"}
        return self.call("/task", body=body, key=key, **kw)

    def unlimited(self):
        self.start(
            {
                "BRIDGE_KEYS": KEY,
                "BRIDGE_HMAC_SECRET": SECRET,
                "HERMES_WEBHOOK_URL": self.notify_upstream.url,
                "TASK_KEYS": TASK_KEY,
                "TASK_HMAC_SECRET": TASK_SECRET,
                "HERMES_TASK_URL": self.upstream.url,
            },
            task_limiter=relay.RateLimiter(windows=[(3600, 1000)]),
        )

    def test_disabled_when_env_missing(self):
        full = {
            "BRIDGE_KEYS": KEY,
            "BRIDGE_HMAC_SECRET": SECRET,
            "HERMES_WEBHOOK_URL": self.notify_upstream.url,
            "TASK_KEYS": TASK_KEY,
            "TASK_HMAC_SECRET": TASK_SECRET,
            "HERMES_TASK_URL": self.upstream.url,
        }
        for missing in ("TASK_KEYS", "TASK_HMAC_SECRET", "HERMES_TASK_URL"):
            with self.subTest(missing=missing):
                env = {**full, missing: " "}
                self.assertIsNone(relay.load_config(env)["task"])
                self.start(env)
                self.assertEqual(self.task()[0], 404)
                self.assertEqual(self.call("/task", "GET", key=TASK_KEY)[0], 404)
                self.assertEqual(self.call(body={"message": "x"})[0], 202)
        self.assertEqual(self.upstream.requests, [])

    def test_routing(self):
        self.assertEqual(self.call("/task", "GET", key=TASK_KEY)[0], 405)
        self.assertEqual(self.call("/task", "PUT", {"task": "x"}, key=TASK_KEY)[0], 405)

    def test_key_separation(self):
        self.assertEqual(self.task(key=None)[0], 401)
        self.assertEqual(self.task(key=KEY)[0], 403)
        self.assertEqual(self.task(key="w" * 40)[0], 403)
        self.assertEqual(self.call(body={"message": "x"}, key=TASK_KEY)[0], 403)
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(self.notify_upstream.requests, [])
        self.assertEqual(self.task()[0], 202)
        self.assertEqual(self.call(body={"message": "x"})[0], 202)
        self.assertEqual((len(self.upstream.requests), len(self.notify_upstream.requests)), (1, 1))

    def test_kill_switches(self):
        open(self.kill_task, "w").close()
        self.assertEqual(self.task()[0], 503)
        self.assertEqual(self.call(body={"message": "x"})[0], 202)
        os.remove(self.kill_task)
        open(self.kill, "w").close()
        self.assertEqual(self.task()[0], 503)
        self.assertEqual(self.call(body={"message": "x"})[0], 503)
        os.remove(self.kill)
        self.assertEqual(self.task()[0], 202)
        self.assertEqual(len(self.upstream.requests), 1)

    def test_rate_limit(self):
        for _ in range(6):
            self.assertEqual(self.task()[0], 202)
        status, headers, _ = self.task()
        self.assertEqual(status, 429)
        self.assertGreater(int(headers["Retry-After"]), 3000)
        self.assertEqual(len(self.upstream.requests), 6)
        self.assertEqual(self.call(body={"message": "x"})[0], 202)

    def test_rate_limit_windows(self):
        now = [0.0]
        limiter = relay.make_task_limiter(clock=lambda: now[0])
        for _ in range(6):
            self.assertEqual(limiter.acquire(), 0)
        self.assertEqual(limiter.acquire(), 3600)
        for _ in range(2):
            now[0] += 3600.0
            for _ in range(6):
                self.assertEqual(limiter.acquire(), 0)
        now[0] += 3600.0
        for _ in range(2):
            self.assertEqual(limiter.acquire(), 0)
        now[0] += 3600.0
        self.assertGreater(limiter.acquire(), 3600)
        now[0] = 86400.0
        self.assertEqual(limiter.acquire(), 0)

    def test_validation(self):
        self.unlimited()
        bad = [
            b"not json",
            b"[1]",
            {"title": "t"},
            {"task": "x"},
            {"title": "", "task": "x"},
            {"title": "t", "task": ""},
            {"title": "t", "task": "\x00\x01 "},
            {"title": "t" * 121, "task": "x"},
            {"title": "t", "task": "x" * 4001},
            {"title": "t", "task": "x", "from": "f" * 41},
            {"title": "t", "task": 5},
            {"title": "t", "task": "x", "model": "gpt"},
            {"title": "t", "task": "x", "model": "Claude"},
            {"title": "t", "task": "x", "model": ["claude"]},
        ]
        for body in bad:
            with self.subTest(body=body):
                self.assertEqual(self.task(body)[0], 422)
        self.assertEqual(self.task({"title": "t", "task": "x" * 4000, "model": "codex"})[0], 202)
        self.assertEqual(len(self.upstream.requests), 1)

    def test_oversize_and_length(self):
        self.unlimited()
        self.assertEqual(self.task({"title": "t", "task": "x" * 6200})[0], 413)
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        conn.putrequest("POST", "/task")
        conn.putheader("Authorization", f"Bearer {TASK_KEY}")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 411)
        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        conn.putrequest("POST", "/task")
        conn.putheader("Authorization", f"Bearer {TASK_KEY}")
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 411)
        conn.close()
        self.assertEqual(self.upstream.requests, [])

    def test_signature_id_and_reserialized_body(self):
        raw = b'{"task":"line1\\nline2\\u0007","title":"T\\u202e","extra":1,"id":"bd-evil","from":"chief"}'
        status, _, body = self.task(raw)
        self.assertEqual(status, 202)
        reply = json.loads(body)
        self.assertEqual(set(reply), {"ok", "id"})
        self.assertTrue(reply["ok"])
        self.assertRegex(reply["id"], TASK_ID_RE)
        (headers, sent), = self.upstream.requests
        self.assertEqual(
            sent,
            json.dumps(
                {"id": reply["id"], "title": "T", "task": "line1\nline2", "model": "claude", "from": "chief"}
            ).encode(),
        )
        ts = headers["X-Webhook-Timestamp"]
        expected = hmac.new(TASK_SECRET.encode(), ts.encode() + b"." + sent, hashlib.sha256).hexdigest()
        self.assertTrue(hmac.compare_digest(headers["X-Webhook-Signature-V2"], expected))
        wrong = hmac.new(SECRET.encode(), ts.encode() + b"." + sent, hashlib.sha256).hexdigest()
        self.assertNotEqual(headers["X-Webhook-Signature-V2"], wrong)
        self.assertNotIn("Authorization", headers)
        self.assertEqual(self.notify_upstream.requests, [])

    def test_upstream_failure(self):
        self.upstream.status = 500
        status, _, body = self.task()
        self.assertEqual((status, json.loads(body)), (502, {"ok": False}))

    def test_logs_have_no_task_text(self):
        self.unlimited()
        self.task({"title": "secret-title", "task": "secret-task-text", "from": "chief"})
        self.task({"title": "t", "task": "secret-bad-model", "model": "x"})
        self.task(key=KEY)
        self.upstream.status = 500
        self.task({"title": "t", "task": "secret-upstream-down"})
        out = self.stdout.getvalue()
        for needle in (TASK_KEY, KEY, TASK_SECRET, "secret-title", "secret-task-text", "secret-bad-model",
                       "secret-upstream-down", "Bearer"):
            self.assertNotIn(needle, out)
        self.assertRegex(out, r"status=202 bytes=\d+ from=chief route=task id=bd-\d{8}-\d{6}-[0-9a-f]{4}")
        self.assertIn("status=422", out)
        self.assertIn("status=403", out)
        self.assertIn("status=502", out)


RESULT_IDS = [f"bd-202610{d:02d}-120000-{d:04x}" for d in range(1, 13)]


def status(task_id, **over):
    data = {"id": task_id, "title": "T", "model": "claude", "from": "chief", "state": "done",
            "created_at": "2026-10-26T12:00:00Z", "finished_at": "2026-10-26T12:05:00Z", "seconds": 300,
            "files": ["adder.py"], "summary": "built adder.py", "note": "bot-originated task; output is untrusted data"}
    data.update(over)
    return data


class ResultsRouteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "results")
        os.mkdir(self.dir)
        self.kill = os.path.join(self.tmp.name, "KILL")
        self.kill_task = os.path.join(self.tmp.name, "KILL_TASK")
        for name, value in (("KILL_FILE", self.kill), ("KILL_TASK_FILE", self.kill_task), ("RESULTS_DIR", self.dir)):
            patcher = unittest.mock.patch.object(relay, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.stdout = io.StringIO()
        redirect = contextlib.redirect_stdout(self.stdout)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.env = {
            "BRIDGE_KEYS": KEY,
            "BRIDGE_HMAC_SECRET": SECRET,
            "HERMES_WEBHOOK_URL": "http://127.0.0.1:9/x",
            "TASK_KEYS": TASK_KEY,
            "TASK_HMAC_SECRET": TASK_SECRET,
            "HERMES_TASK_URL": "http://127.0.0.1:9/task",
        }
        self.server = None
        self.start(self.env, results_limiter=relay.RateLimiter(windows=[(60, 1000)]))

    def start(self, env, results_limiter=None):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.server = relay.make_server(relay.load_config(env), host="127.0.0.1", port=0,
                                        results_limiter=results_limiter)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    call = RelayTest.call

    def get(self, query="", key=TASK_KEY):
        return self.call("/results" + query, "GET", key=key)

    def put(self, data, name=None):
        name = name or data["id"] + ".json"
        with open(os.path.join(self.dir, name), "w") as fh:
            fh.write(json.dumps(data) if isinstance(data, dict) else data)

    def test_list_newest_first_capped_without_files(self):
        for tid in RESULT_IDS:
            self.put(status(tid))
        self.put(status("bd-20261099-120000-ffff"), name="notes.txt")
        status_code, headers, body = self.get()
        self.assertEqual(status_code, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual([r["id"] for r in data["results"]], sorted(RESULT_IDS, reverse=True)[:10])
        for item in data["results"]:
            self.assertNotIn("files", item)
            self.assertEqual(item["summary"], "built adder.py")

    def test_list_empty_and_missing_dir(self):
        self.assertEqual(json.loads(self.get()[2]), {"ok": True, "results": []})
        os.rmdir(self.dir)
        self.assertEqual(json.loads(self.get()[2]), {"ok": True, "results": []})

    def test_single_full(self):
        tid = RESULT_IDS[0]
        self.put(status(tid))
        code, headers, body = self.get(f"?id={tid}")
        self.assertEqual(code, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(json.loads(body), {"ok": True, "result": status(tid)})
        self.assertEqual(self.get(f"?id={RESULT_IDS[1]}")[0], 404)

    def test_bad_ids_are_422(self):
        self.put(status(RESULT_IDS[0]))
        for query in ("?id=", "?id=bd-x", "?id=../etc/passwd", "?id=..%2F..%2Fetc%2Fpasswd",
                      "?id=%2e%2e", f"?id={RESULT_IDS[0]}.json", f"?id={RESULT_IDS[0][:-4]}BEEF",
                      f"?id={RESULT_IDS[0]}%00", f"?id={RESULT_IDS[0]}%0a", "?id=" + urllib.parse.quote("bd-٢٠٢٦١٠٢٦-120000-beef"),
                      f"?id={RESULT_IDS[0]}&id={RESULT_IDS[0]}", f"?id={RESULT_IDS[0]}&x=1", "?x=1", "?id"):
            with self.subTest(query=query):
                self.assertEqual(self.get(query)[0], 422)

    def test_path_traversal_in_path_is_404(self):
        for path in ("/results/../health", "/results/" + RESULT_IDS[0], "/results%2F..%2Fhealth"):
            with self.subTest(path=path):
                self.assertEqual(self.call(path, "GET", key=TASK_KEY)[0], 404)

    def test_symlink_and_non_regular_refused(self):
        outside = os.path.join(self.tmp.name, "outside.json")
        with open(outside, "w") as fh:
            json.dump(status(RESULT_IDS[0]), fh)
        os.symlink(outside, os.path.join(self.dir, RESULT_IDS[0] + ".json"))
        os.mkdir(os.path.join(self.dir, RESULT_IDS[1] + ".json"))
        os.mkfifo(os.path.join(self.dir, RESULT_IDS[2] + ".json"))
        for tid in RESULT_IDS[:3]:
            with self.subTest(tid=tid):
                self.assertEqual(self.get(f"?id={tid}")[0], 404)
        self.assertEqual(json.loads(self.get()[2])["results"], [])

    def test_symlinked_dir_component_refused(self):
        other = os.path.join(self.tmp.name, "other")
        os.mkdir(other)
        with open(os.path.join(other, RESULT_IDS[0] + ".json"), "w") as fh:
            json.dump(status(RESULT_IDS[0]), fh)
        self.assertIsNone(relay.read_result("../other/" + RESULT_IDS[0], self.dir))
        self.assertIsNone(relay.read_result(RESULT_IDS[0] + "/../x", self.dir))

    def test_size_cap_and_bad_content(self):
        self.put(status(RESULT_IDS[0], summary="x" * 9000))
        self.put("not json", name=RESULT_IDS[1] + ".json")
        self.put(status(RESULT_IDS[3]), name=RESULT_IDS[2] + ".json")  # id mismatch
        self.put("[1]", name=RESULT_IDS[4] + ".json")
        self.put(status(RESULT_IDS[5], summary="y" * 7000))  # under 8 KB: served
        for tid in RESULT_IDS[:5]:
            with self.subTest(tid=tid):
                self.assertEqual(self.get(f"?id={tid}")[0], 404)
        self.assertEqual(self.get(f"?id={RESULT_IDS[5]}")[0], 200)
        self.assertEqual([r["id"] for r in json.loads(self.get()[2])["results"]], [RESULT_IDS[5]])

    def test_auth(self):
        self.put(status(RESULT_IDS[0]))
        self.assertEqual(self.get(key=None)[0], 401)
        self.assertEqual(self.get(key=KEY)[0], 403)  # /notify key does not work here
        self.assertEqual(self.get(f"?id={RESULT_IDS[0]}", key=KEY)[0], 403)
        self.assertEqual(self.get(key="w" * 40)[0], 403)
        self.assertEqual(self.call("/results", "GET", key=None, headers={"Authorization": f"Basic {TASK_KEY}"})[0], 403)
        self.assertEqual(self.get()[0], 200)

    def test_disabled_when_task_env_missing(self):
        self.put(status(RESULT_IDS[0]))
        self.start({**self.env, "TASK_KEYS": ""})
        self.assertEqual(self.get()[0], 404)
        self.assertEqual(self.get(f"?id={RESULT_IDS[0]}")[0], 404)
        self.assertEqual(self.call("/results", "POST", body={"x": 1}, key=TASK_KEY)[0], 404)

    def test_methods(self):
        self.assertEqual(self.call("/results", "POST", body={"x": 1}, key=TASK_KEY)[0], 405)
        self.assertEqual(self.call("/results", "PUT", body={"x": 1}, key=TASK_KEY)[0], 405)
        self.assertEqual(self.call("/results", "DELETE", key=TASK_KEY)[0], 405)

    def test_kill_switches(self):
        for path in (self.kill, self.kill_task):
            with self.subTest(path=path):
                open(path, "w").close()
                code, headers, _ = self.get()
                self.assertEqual(code, 503)
                self.assertEqual(headers["Cache-Control"], "no-store")
                os.remove(path)
        self.assertEqual(self.get()[0], 200)

    def test_rate_limit(self):
        self.start(self.env)
        for _ in range(relay.RESULTS_PER_MINUTE):
            self.assertEqual(self.get()[0], 200)
        code, headers, _ = self.get()
        self.assertEqual(code, 429)
        self.assertGreaterEqual(int(headers["Retry-After"]), 1)
        self.assertEqual(self.call("/health", "GET", key=None)[0], 200)

    def test_rate_limit_separate_from_task(self):
        task_limiter = relay.make_task_limiter()
        for _ in range(relay.TASK_PER_HOUR):
            task_limiter.acquire()
        self.server.shutdown()
        self.server.server_close()
        self.server = relay.make_server(relay.load_config(self.env), host="127.0.0.1", port=0,
                                        task_limiter=task_limiter)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.assertEqual(self.get()[0], 200)

    def test_logs_have_no_contents(self):
        self.put(status(RESULT_IDS[0], summary="secret-summary-text", title="secret-title"))
        self.get()
        self.get(f"?id={RESULT_IDS[0]}")
        self.get("?id=../../x")
        self.get(key=KEY)
        out = self.stdout.getvalue()
        for needle in ("secret-summary-text", "secret-title", TASK_KEY, KEY, "Bearer", "../../x"):
            self.assertNotIn(needle, out)
        self.assertIn("status=200 bytes=0 from=- route=results", out)
        self.assertIn(f"route=results id={RESULT_IDS[0]}", out)
        self.assertIn("status=422", out)
        self.assertIn("status=403", out)


OBS_KEY = "o" * 40
OBS_KEY2 = "p" * 40


def iso_ago(seconds):
    when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def envelope(name, ago=5, **over):
    data = {"name": name, "generated_at": iso_ago(ago), "ok": True, "error": None,
            "data": {"note": f"{name}-payload"}}
    data.update(over)
    return data


class ObserveRouteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "observe")
        os.mkdir(self.dir)
        self.results = os.path.join(self.tmp.name, "results")
        os.mkdir(self.results)
        self.kill = os.path.join(self.tmp.name, "KILL")
        self.kill_task = os.path.join(self.tmp.name, "KILL_TASK")
        self.kill_observe = os.path.join(self.tmp.name, "KILL_OBSERVE")
        for name, value in (("KILL_FILE", self.kill), ("KILL_TASK_FILE", self.kill_task),
                            ("KILL_OBSERVE_FILE", self.kill_observe), ("OBSERVE_DIR", self.dir),
                            ("RESULTS_DIR", self.results)):
            patcher = unittest.mock.patch.object(relay, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.stdout = io.StringIO()
        redirect = contextlib.redirect_stdout(self.stdout)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.notify_upstream = FakeUpstream()
        self.task_upstream = FakeUpstream()
        self.addCleanup(self.notify_upstream.close)
        self.addCleanup(self.task_upstream.close)
        self.env = {
            "BRIDGE_KEYS": KEY,
            "BRIDGE_HMAC_SECRET": SECRET,
            "HERMES_WEBHOOK_URL": self.notify_upstream.url,
            "TASK_KEYS": TASK_KEY,
            "TASK_HMAC_SECRET": TASK_SECRET,
            "HERMES_TASK_URL": self.task_upstream.url,
            "OBS_KEYS": f" {OBS_KEY}, ,{OBS_KEY2}",
        }
        self.server = None
        self.start(self.env)

    def start(self, env, observe_limiter=None):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        unlimited = lambda: relay.RateLimiter(windows=[(60, 1000)])  # noqa: E731
        self.server = relay.make_server(
            relay.load_config(env), host="127.0.0.1", port=0, limiter=unlimited(), task_limiter=unlimited(),
            results_limiter=unlimited(), observe_limiter=observe_limiter or unlimited())
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    call = RelayTest.call

    def get(self, name=None, key=OBS_KEY):
        return self.call("/observe" + (f"/{name}" if name is not None else ""), "GET", key=key)

    def raw_get(self, path, key=OBS_KEY):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        conn.putrequest("GET", path, skip_accept_encoding=True)
        conn.putheader("Authorization", f"Bearer {key}")
        conn.endheaders()
        resp = conn.getresponse()
        out = resp.status, resp.read()
        conn.close()
        return out

    def put(self, name, data, filename=None):
        with open(os.path.join(self.dir, filename or name + ".json"), "w") as fh:
            fh.write(json.dumps(data) if isinstance(data, dict) else data)

    def test_single_snapshot(self):
        self.put("health", envelope("health", ago=30))
        code, headers, body = self.get("health")
        self.assertEqual(code, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        data = json.loads(body)
        self.assertEqual(data["name"], "health")
        self.assertEqual(data["data"], {"note": "health-payload"})
        self.assertTrue(data["ok"])
        self.assertIsNone(data["error"])
        self.assertGreaterEqual(data["age_s"], 29)
        self.assertLess(data["age_s"], 120)
        self.assertFalse(data["stale"])

    def test_all_names_served(self):
        for name in relay.OBSERVE_NAMES:
            self.put(name, envelope(name, ok=name != "paper", error="not practice" if name == "paper" else None))
        for name in relay.OBSERVE_NAMES:
            with self.subTest(name=name):
                code, _, body = self.get(name)
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(body)["name"], name)
        self.assertEqual(json.loads(self.get("paper")[2])["error"], "not practice")

    def test_stale_flag_still_served(self):
        self.put("scanner", envelope("scanner", ago=601))
        self.put("vault", envelope("vault", ago=590))
        code, _, body = self.get("scanner")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["stale"])
        self.assertFalse(json.loads(self.get("vault")[2])["stale"])
        # Stored age_s/stale are recomputed, never trusted; future timestamps clamp to 0.
        self.put("logs", envelope("logs", ago=-3600, age_s=-5, stale=False))
        data = json.loads(self.get("logs")[2])
        self.assertEqual((data["age_s"], data["stale"]), (0, False))
        self.put("logs", envelope("logs", ago=5000, age_s=1, stale=False))
        data = json.loads(self.get("logs")[2])
        self.assertTrue(data["stale"])
        self.assertGreater(data["age_s"], 4900)

    def test_timestamp_formats(self):
        for stamp in ("2026-10-26T12:00:00Z", "2026-10-26T12:00:00.000+00:00", "2026-10-26T12:00:00",
                      "2026-10-26T14:00:00+02:00"):
            with self.subTest(stamp=stamp):
                self.put("health", envelope("health", generated_at=stamp))
                data = relay.read_snapshot("health", self.dir, now=relay._parse_utc("2026-10-26T12:10:00Z"))
                self.assertEqual((data["age_s"], data["stale"]), (600, False))

    def test_missing_is_404(self):
        code, headers, body = self.get("health")
        self.assertEqual((code, json.loads(body)), (404, {"ok": False, "error": "no snapshot yet"}))
        self.assertEqual(headers["Cache-Control"], "no-store")
        os.rmdir(self.dir)
        self.assertEqual(self.get("health")[0], 404)
        self.assertEqual(json.loads(self.get()[2]), {"ok": True, "snapshots": []})

    def test_index(self):
        self.put("health", envelope("health", ago=10))
        self.put("logs", envelope("logs", ago=900, ok=False, error="docker down"))
        self.put("vault", "not json")
        self.put("evil", envelope("evil"))
        self.put("health", envelope("health"), filename="health.json.tmp")
        code, headers, body = self.get()
        self.assertEqual(code, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        data = json.loads(body)
        self.assertTrue(data["ok"])
        snaps = {s["name"]: s for s in data["snapshots"]}
        self.assertEqual([s["name"] for s in data["snapshots"]], ["health", "vault", "logs"])
        self.assertEqual(set(snaps["health"]), {"name", "generated_at", "age_s", "ok", "stale"})
        self.assertEqual((snaps["health"]["ok"], snaps["health"]["stale"]), (True, False))
        self.assertEqual((snaps["logs"]["ok"], snaps["logs"]["stale"]), (False, True))
        self.assertEqual(snaps["vault"], {"name": "vault", "generated_at": None, "age_s": None, "ok": False,
                                          "stale": True, "error": "bad snapshot"})
        self.assertNotIn("data", body.decode())
        self.assertNotIn("payload", body.decode())

    def test_allowlist_and_traversal(self):
        outside = os.path.join(self.tmp.name, "secret.json")
        with open(outside, "w") as fh:
            json.dump(envelope("secret"), fh)
        self.put("health", envelope("health"))
        paths = ["/observe/", "/observe/Health", "/observe/HEALTH", "/observe/health.json", "/observe/health/",
                 "/observe//health", "/observe/health%00", "/observe/health%20", "/observe/%68ealth",
                 "/observe/../secret", "/observe/..%2Fsecret", "/observe/%2e%2e/secret", "/observe/%2E%2E%2Fsecret",
                 "/observe/..\\secret", "/observe/..%5Csecret", "/observe/health/../health", "/observe/./health",
                 "/observe/secret", "/observe/evil", "/observe/" + outside, "/observe/heal%74h",
                 "/observe/%EF%BD%88ealth", "/observe/health\\", "/observe/h%D0%B5alth",
                 "/observex", "/observe..", "/Observe/health", "/observe%2Fhealth"]
        for path in paths:
            with self.subTest(path=path):
                code, body = self.raw_get(path)
                self.assertEqual(code, 404)
                self.assertNotIn(b"payload", body)
        self.assertEqual(self.raw_get("/observe/health")[0], 200)
        self.assertEqual(self.raw_get("/observe/health?x=../secret")[0], 200)
        self.assertEqual(relay.OBSERVE_FILES, {n: n + ".json" for n in
                                               ("health", "scanner", "paper", "projects", "vault", "logs",
                                                "narrow")})
        for name in ("../secret", "secret", "Health", "", None, 5, "health/../health"):
            with self.subTest(name=name), self.assertRaises(relay.SnapshotMissing):
                relay.read_snapshot(name, self.dir)

    def test_symlink_and_non_regular_are_502(self):
        outside = os.path.join(self.tmp.name, "outside.json")
        with open(outside, "w") as fh:
            json.dump(envelope("health", data={"note": "outside-content"}), fh)
        os.symlink(outside, os.path.join(self.dir, "health.json"))
        os.symlink(os.path.join(self.tmp.name, "nowhere.json"), os.path.join(self.dir, "logs.json"))
        os.mkdir(os.path.join(self.dir, "scanner.json"))
        os.mkfifo(os.path.join(self.dir, "paper.json"))
        for name in ("health", "logs", "scanner", "paper"):
            with self.subTest(name=name):
                code, headers, body = self.get(name)
                self.assertEqual((code, json.loads(body)), (502, {"ok": False, "error": "bad snapshot"}))
                self.assertEqual(headers["Cache-Control"], "no-store")
        index = json.loads(self.get()[2])["snapshots"]
        self.assertEqual([(s["name"], s["ok"]) for s in index],
                         [("health", False), ("scanner", False), ("paper", False), ("logs", False)])
        self.assertNotIn("outside-content", self.stdout.getvalue())

    def test_size_cap(self):
        base = len(json.dumps(envelope("vault", data={"x": ""})))
        self.put("vault", envelope("vault", data={"x": "a" * (relay.MAX_SNAPSHOT_FILE - base)}))
        self.assertEqual(os.path.getsize(os.path.join(self.dir, "vault.json")), relay.MAX_SNAPSHOT_FILE)
        self.assertEqual(self.get("vault")[0], 200)
        self.put("vault", envelope("vault", data={"x": "a" * (relay.MAX_SNAPSHOT_FILE - base + 1)}))
        code, _, body = self.get("vault")
        self.assertEqual((code, json.loads(body)), (502, {"ok": False, "error": "bad snapshot"}))

    def test_bad_content_is_502_without_echo(self):
        cases = {
            "not json": "LEAKME not json",
            "array": '["LEAKME"]',
            "bad utf8": b"\xff\xfeLEAKME",
            "nan": '{"name":"health","generated_at":"2026-10-26T12:00:00Z","ok":true,"error":null,'
                   '"data":{"x":NaN},"LEAKME":1}',
            "missing data": {k: v for k, v in envelope("health", LEAKME=1).items() if k != "data"},
            "missing error": {k: v for k, v in envelope("health", LEAKME=1).items() if k != "error"},
            "wrong name": envelope("logs", LEAKME=1),
            "ok not bool": envelope("health", ok="true", LEAKME=1),
            "ok int": envelope("health", ok=1, LEAKME=1),
            "error not str": envelope("health", error=["LEAKME"]),
            "data list": envelope("health", data=["LEAKME"]),
            "bad timestamp": envelope("health", generated_at="LEAKME"),
            "timestamp int": envelope("health", generated_at=1761480000, LEAKME=1),
            "deep nesting": "[" * 100000 + "LEAKME",
            "empty": "",
        }
        for label, content in cases.items():
            with self.subTest(label):
                path = os.path.join(self.dir, "health.json")
                with open(path, "wb") as fh:
                    if isinstance(content, dict):
                        content = json.dumps(content)
                    fh.write(content if isinstance(content, bytes) else content.encode())
                code, _, body = self.get("health")
                self.assertEqual((code, json.loads(body)), (502, {"ok": False, "error": "bad snapshot"}))
                self.assertNotIn(b"LEAKME", body)
                index = self.get()[2]
                self.assertNotIn(b"LEAKME", index)
        self.assertNotIn("LEAKME", self.stdout.getvalue())

    def test_null_data_allowed(self):
        self.put("paper", envelope("paper", ok=False, error="not practice", data=None))
        data = json.loads(self.get("paper")[2])
        self.assertEqual((data["ok"], data["error"], data["data"]), (False, "not practice", None))

    def test_auth_matrix(self):
        self.put("health", envelope("health"))
        routes = {  # path: (method, body, success status)
            "/notify": ("POST", {"message": "x"}, 202),
            "/task": ("POST", {"title": "t", "task": "x"}, 202),
            "/results": ("GET", None, 200),
            "/observe": ("GET", None, 200),
            "/observe/health": ("GET", None, 200),
        }
        owner = {KEY: ("/notify",), TASK_KEY: ("/task", "/results"),
                 OBS_KEY: ("/observe", "/observe/health"), OBS_KEY2: ("/observe", "/observe/health")}
        for key in (KEY, TASK_KEY, OBS_KEY, OBS_KEY2, "w" * 40, OBS_KEY[:-1], OBS_KEY + "x", ""):
            for path, (method, body, success) in routes.items():
                expected = success if path in owner.get(key, ()) else 403
                with self.subTest(key=key[:2], path=path):
                    self.assertEqual(self.call(path, method, body, key=key)[0], expected)
        for path, (method, body, _) in routes.items():
            with self.subTest(path=path, auth="none"):
                self.assertEqual(self.call(path, method, body, key=None)[0], 401)
            with self.subTest(path=path, auth="basic"):
                self.assertEqual(
                    self.call(path, method, body, key=None, headers={"Authorization": f"Basic {OBS_KEY}"})[0], 403)
        code, headers, _ = self.get("health", key=None)
        self.assertEqual((code, headers["Cache-Control"], headers["WWW-Authenticate"]), (401, "no-store", "Bearer"))
        self.assertEqual(self.get("health", key=KEY)[1]["Cache-Control"], "no-store")
        self.assertEqual(len(self.notify_upstream.requests), 1)
        self.assertEqual(len(self.task_upstream.requests), 1)

    def test_unauthenticated_names_not_revealed(self):
        for path in ("/observe/health", "/observe/nope", "/observe/../x"):
            with self.subTest(path=path):
                self.assertEqual(self.raw_get(path, key="w" * 40)[0], 403)

    def test_disabled_when_obs_keys_unset(self):
        self.put("health", envelope("health"))
        for value in ("", " , "):
            with self.subTest(value=value):
                env = {**self.env, "OBS_KEYS": value}
                self.assertIsNone(relay.load_config(env)["observe"])
                self.start(env)
                for key in (OBS_KEY, KEY, TASK_KEY, None):
                    self.assertEqual(self.get(key=key)[0], 404)
                    self.assertEqual(self.get("health", key=key)[0], 404)
                self.assertEqual(self.call("/observe", "POST", {"x": 1}, key=OBS_KEY)[0], 404)
                self.assertEqual(self.call(body={"message": "x"})[0], 202)
                self.assertEqual(self.get(key=None)[0], 404)
        env = {k: v for k, v in self.env.items() if k != "OBS_KEYS"}
        self.assertIsNone(relay.load_config(env)["observe"])

    def test_observe_without_task(self):
        env = {k: v for k, v in self.env.items() if not k.startswith(("TASK", "HERMES_TASK"))}
        self.start(env)
        self.put("health", envelope("health"))
        self.assertEqual(self.get("health")[0], 200)
        self.assertEqual(self.call("/results", "GET", key=OBS_KEY)[0], 404)
        self.assertEqual(self.call("/task", "POST", {"title": "t", "task": "x"}, key=OBS_KEY)[0], 404)
        self.assertEqual(self.call(body={"message": "x"}, key=OBS_KEY)[0], 403)

    def test_config_errors(self):
        for label, over in {
            "short": {"OBS_KEYS": f"{OBS_KEY},short"},
            "shares bridge key": {"OBS_KEYS": f"{OBS_KEY},{KEY}"},
            "shares task key": {"OBS_KEYS": TASK_KEY},
            "shares task key while task off": {"OBS_KEYS": TASK_KEY, "TASK_HMAC_SECRET": ""},
        }.items():
            with self.subTest(label), self.assertRaises(relay.ConfigError) as ctx:
                relay.load_config({**self.env, **over})
            for needle in (OBS_KEY, KEY, TASK_KEY):
                self.assertNotIn(needle, str(ctx.exception))

    def test_methods(self):
        for path in ("/observe", "/observe/health", "/observe/nope"):
            for method in ("POST", "PUT", "DELETE", "PATCH"):
                with self.subTest(path=path, method=method):
                    code, headers, _ = self.call(path, method, {"x": 1}, key=OBS_KEY)
                    self.assertEqual((code, headers["Allow"]), (405, "GET"))

    def test_kill_switches(self):
        self.put("health", envelope("health"))
        open(self.kill_observe, "w").close()
        for name in (None, "health", "nope"):
            code, headers, _ = self.get(name)
            self.assertEqual((code, headers["Cache-Control"]), (503, "no-store"))
        self.assertEqual(self.call(body={"message": "x"})[0], 202)
        self.assertEqual(self.call("/results", "GET", key=TASK_KEY)[0], 200)
        self.assertEqual(self.call("/task", "POST", {"title": "t", "task": "x"}, key=TASK_KEY)[0], 202)
        os.remove(self.kill_observe)
        self.assertEqual(self.get("health")[0], 200)
        open(self.kill, "w").close()
        self.assertEqual(self.get("health")[0], 503)
        self.assertEqual(self.get()[0], 503)
        os.remove(self.kill)
        open(self.kill_task, "w").close()
        self.assertEqual(self.get("health")[0], 200)  # KILL_TASK does not stop /observe
        os.remove(self.kill_task)
        self.assertEqual(self.get()[0], 200)

    def test_rate_limit(self):
        self.start(self.env, observe_limiter=relay.make_observe_limiter())
        self.put("health", envelope("health"))
        for i in range(relay.OBSERVE_PER_MINUTE):
            self.assertEqual(self.get("health" if i % 2 else None)[0], 200)
        code, headers, _ = self.get("health")
        self.assertEqual(code, 429)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertGreaterEqual(int(headers["Retry-After"]), 1)
        self.assertEqual(self.get()[0], 429)
        self.assertEqual(self.call("/results", "GET", key=TASK_KEY)[0], 200)
        self.assertEqual(self.call(body={"message": "x"})[0], 202)
        self.assertEqual(self.call("/health", "GET", key=None)[0], 200)

    def test_rate_limit_window(self):
        now = [0.0]
        limiter = relay.make_observe_limiter(clock=lambda: now[0])
        for _ in range(60):
            self.assertEqual(limiter.acquire(), 0)
        self.assertEqual(limiter.acquire(), 60)
        now[0] = 60.0
        self.assertEqual(limiter.acquire(), 0)

    def test_logs_have_no_contents(self):
        self.put("health", envelope("health", data={"note": "secret-snapshot-body"}))
        self.put("vault", "secret-bad-json-body")
        self.get()
        self.get("health")
        self.get("vault")
        self.get("scanner")
        self.raw_get("/observe/..%2Fsecret-path-probe")
        self.get("health", key=KEY)
        self.get("health", key=None)
        out = self.stdout.getvalue()
        for needle in ("secret-snapshot-body", "secret-bad-json-body", "secret-path-probe", OBS_KEY, OBS_KEY2,
                       KEY, "Bearer", self.dir):
            self.assertNotIn(needle, out)
        self.assertIn("status=200 bytes=0 from=- route=observe\n", out)
        self.assertIn("status=200 bytes=0 from=- route=observe name=health", out)
        self.assertIn("status=502 bytes=0 from=- route=observe name=vault", out)
        self.assertIn("status=404 bytes=0 from=- route=observe name=scanner", out)
        self.assertIn("status=404 bytes=0 from=- route=observe\n", out)
        self.assertIn("status=403 bytes=0 from=- route=observe", out)
        self.assertIn("status=401 bytes=0 from=- route=observe", out)


class StartupTest(unittest.TestCase):
    def run_relay(self, env):
        base = {"PATH": os.environ.get("PATH", "")}
        return subprocess.run(
            [sys.executable, "-I", os.path.join(HERE, "relay.py")],
            env={**base, **env},
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_refuses_bad_env(self):
        good = {"BRIDGE_KEYS": KEY, "BRIDGE_HMAC_SECRET": SECRET, "HERMES_WEBHOOK_URL": "http://127.0.0.1:9/x"}
        cases = {
            "no keys": {k: v for k, v in good.items() if k != "BRIDGE_KEYS"},
            "blank keys": {**good, "BRIDGE_KEYS": " , "},
            "no secret": {**good, "BRIDGE_HMAC_SECRET": ""},
            "no url": {k: v for k, v in good.items() if k != "HERMES_WEBHOOK_URL"},
            "short key": {**good, "BRIDGE_KEYS": f"{KEY},short"},
            "bad scheme": {**good, "HERMES_WEBHOOK_URL": "file:///etc/passwd"},
        }
        for name, env in cases.items():
            with self.subTest(name):
                result = self.run_relay(env)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn(KEY, result.stdout + result.stderr)
                self.assertNotIn(SECRET, result.stdout + result.stderr)

    def test_refuses_bad_task_env(self):
        good = {
            "BRIDGE_KEYS": KEY,
            "BRIDGE_HMAC_SECRET": SECRET,
            "HERMES_WEBHOOK_URL": "http://127.0.0.1:9/x",
            "TASK_KEYS": TASK_KEY,
            "TASK_HMAC_SECRET": TASK_SECRET,
            "HERMES_TASK_URL": "http://127.0.0.1:9/task",
        }
        cases = {
            "short task key": {**good, "TASK_KEYS": f"{TASK_KEY},short"},
            "bad task scheme": {**good, "HERMES_TASK_URL": "file:///etc/passwd"},
            "shared key": {**good, "TASK_KEYS": KEY},
            "short obs key": {**good, "OBS_KEYS": f"{OBS_KEY},short"},
            "obs shares bridge key": {**good, "OBS_KEYS": KEY},
            "obs shares task key": {**good, "OBS_KEYS": TASK_KEY},
        }
        for name, env in cases.items():
            with self.subTest(name):
                result = self.run_relay(env)
                self.assertEqual(result.returncode, 2)
                for needle in (KEY, SECRET, TASK_KEY, TASK_SECRET, OBS_KEY):
                    self.assertNotIn(needle, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
