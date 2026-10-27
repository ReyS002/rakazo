import http.client
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import receiver

KEY = "tvkey-" + "a" * 30
KEY2 = "tvkey-" + "b" * 30
KEY3 = "tvkey-" + "c" * 30
BEARER = "TICKERBULL-BEARER-" + "z" * 24


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc).timestamp()


# Tue 2026-10-27 14:00 UTC = 10:00 EDT: Globex open.
NOW = utc(2026, 10, 27, 14, 0)
BAR_MS = int((NOW - 900) * 1000)


def alert(**over):
    body = {"key": KEY, "v": 1, "src": "tv-futures", "symbol": "MES1!", "tf": "15", "bar_time": BAR_MS,
            "close": 6012.25, "spread": 0.0012, "max_spread10": 0.0031, "atr_pct": 0.0009,
            "atr_pct_10_ago": 0.0011, "range": 3.5, "nr7": True}
    for k, v in over.items():
        if v is DROP:
            body.pop(k, None)
        else:
            body[k] = v
    return body


DROP = object()


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class FakeForwarder:
    def __init__(self, results=None):
        self.sent = []
        self.results = list(results or [])

    def send(self, payload):
        self.sent.append(payload)
        return self.results.pop(0) if self.results else (True, 200)


def make_cfg(state_dir, env=None):
    base = {"TV_KEYS": f"{KEY},{KEY2},{KEY3}", "BD_TICKERBULL_WEBHOOK_URL": "http://127.0.0.1:9/hook",
            "BD_TICKERBULL_WEBHOOK_AUTH": BEARER, "TV_KILL_DIR": state_dir, "TV_STATE_DIR": state_dir}
    base.update(env or {})
    return receiver.load_config(base, route_file="/nonexistent/route")


class ServerCase(unittest.TestCase):
    env = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.clock = Clock()
        self.fwd = FakeForwarder()
        self.start()

    def start(self):
        self.cfg = make_cfg(self.dir, self.env)
        self.rx = receiver.Receiver(self.cfg, forwarder=self.fwd, clock=self.clock)
        self.server = receiver.make_server(self.cfg, host="127.0.0.1", port=0, receiver=self.rx)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.port = self.server.server_address[1]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def tearDown(self):
        self.stop()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None), resp
        finally:
            conn.close()

    def post(self, body, headers=None):
        if not isinstance(body, (bytes, str)):
            body = json.dumps(body)
        return self.request("POST", "/alert", body, {"Content-Type": "text/plain", **(headers or {})})

    def log_lines(self):
        path = os.path.join(self.dir, "alerts.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return [json.loads(line) for line in fh]


class TestAuth(ServerCase):
    def test_good_key_forwards(self):
        status, body, _ = self.post(alert())
        self.assertEqual((status, body), (200, {"ok": True, "action": "forwarded"}))
        self.assertEqual(len(self.fwd.sent), 1)

    def test_second_and_third_keys_accepted(self):
        self.assertEqual(self.post(alert(key=KEY2))[0], 200)
        self.assertEqual(self.post(alert(key=KEY3, symbol="MNQ1!"))[1]["action"], "forwarded")

    def test_bad_key_403_generic(self):
        status, body, _ = self.post(alert(key=KEY[:-1] + "X"))
        self.assertEqual((status, body), (403, {"ok": False, "error": "forbidden"}))
        self.assertEqual(self.fwd.sent, [])

    def test_missing_and_non_string_key_403(self):
        for key in (DROP, None, 123, ["x"], "", KEY + "x"):
            status, body, _ = self.post(alert(key=key))
            self.assertEqual((status, body), (403, {"ok": False, "error": "forbidden"}), key)
        self.assertEqual(self.rx.health()["rejected_today"], 6)

    def test_auth_before_schema(self):
        # A wrong key on a broken body must not reveal which field is wrong.
        status, body, _ = self.post({"key": "nope", "v": 2})
        self.assertEqual((status, body), (403, {"ok": False, "error": "forbidden"}))

    def test_constant_time_compare_every_key(self):
        real = receiver.hmac.compare_digest
        calls = []

        def spy(a, b):
            calls.append(1)
            return real(a, b)

        with mock.patch.object(receiver.hmac, "compare_digest", spy):
            self.assertEqual(self.post(alert(key=KEY))[0], 200)  # first key matches: still 3 compares
            self.assertEqual(len(calls), 3)
            calls.clear()
            self.assertEqual(self.post(alert(key="w" * 30))[0], 403)
            self.assertEqual(len(calls), 3)

    def test_ip_block_after_10_wrong_keys(self):
        for _ in range(9):
            self.assertEqual(self.post(alert(key="wrong"))[0], 403)
        self.assertEqual(self.post(alert())[0], 200)  # 9 wrongs: still allowed
        self.assertEqual(self.post(alert(key="wrong"))[0], 403)  # 10th wrong -> block
        status, body, resp = self.post(alert(symbol="MNQ1!"))
        self.assertEqual((status, body["error"]), (429, "blocked"))
        self.assertEqual(resp.getheader("Retry-After"), "60")
        self.clock.now += 14 * 60
        self.assertEqual(self.post(alert(symbol="MNQ1!"))[0], 429)
        self.clock.now += 61
        self.assertEqual(self.post(alert(symbol="MNQ1!", bar_time=BAR_MS + 900000))[0], 200)

    def test_wrong_keys_outside_10_min_window_do_not_block(self):
        for _ in range(9):
            self.post(alert(key="wrong"))
        self.clock.now += 601
        self.assertEqual(self.post(alert(key="wrong"))[0], 403)
        self.assertEqual(self.post(alert())[0], 200)


class TestLimits(ServerCase):
    def test_size_cap(self):
        big = json.dumps(alert(pad="x" * receiver.MAX_BODY))
        status, body, _ = self.post(big)
        self.assertEqual((status, body["error"]), (413, "body_too_large"))
        exact = json.dumps(alert())
        exact = exact + " " * (receiver.MAX_BODY - len(exact))
        self.assertEqual(len(exact), receiver.MAX_BODY)
        self.assertEqual(self.post(exact)[0], 200)

    def test_chunked_rejected(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", "/alert")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        conn.send(b"0\r\n\r\n")
        self.assertEqual(conn.getresponse().status, 411)
        conn.close()

    def test_global_rate_limit_30_per_minute(self):
        self.assertEqual(self.post(alert())[0], 200)
        for i in range(29):
            self.assertEqual(self.post("{")[0], 400, i)
        status, body, _ = self.post(alert())
        self.assertEqual((status, body["error"]), (429, "rate_limited"))
        self.clock.now += 60
        self.assertNotEqual(self.post(alert())[0], 429)

    def test_per_ip_limit_independent_of_other_ips(self):
        rx = self.rx
        for _ in range(30):
            rx.ip_rate.setdefault("10.0.0.9", receiver.SlidingWindow(60, 30)).allow(self.clock())
        with self.assertRaises(receiver.Reject) as cm:
            rx.admit("10.0.0.9")
        self.assertEqual(cm.exception.status, 429)
        rx.admit("10.0.0.10")  # another IP still admitted

    def test_daily_forward_cap(self):
        self.rx.state.day = "2026-10-27"
        self.rx.state.forwarded = receiver.FORWARDS_PER_DAY
        status, body, _ = self.post(alert())
        self.assertEqual(body, {"ok": True, "action": "ignored", "reason": "daily_cap"})
        self.assertEqual(self.fwd.sent, [])


class TestXForwardedFor(unittest.TestCase):
    def test_untrusted_peer_ignores_xff(self):
        with tempfile.TemporaryDirectory() as d:
            rx = receiver.Receiver(make_cfg(d), forwarder=FakeForwarder())
            self.assertEqual(rx.client_ip("172.18.0.5", "1.2.3.4"), "172.18.0.5")

    def test_gateway_peer_uses_proxy_appended_hop(self):
        with tempfile.TemporaryDirectory() as d:
            route = os.path.join(d, "route")
            with open(route, "w") as fh:
                fh.write("Iface\tDestination\tGateway\tFlags\n")
                fh.write("eth0\t00000000\t010012AC\t0003\n")  # 172.18.0.1 little-endian
            cfg = receiver.load_config({"TV_KEYS": KEY, "BD_TICKERBULL_WEBHOOK_URL": "http://x/h",
                                        "BD_TICKERBULL_WEBHOOK_AUTH": BEARER, "TV_STATE_DIR": d}, route_file=route)
            rx = receiver.Receiver(cfg, forwarder=FakeForwarder())
            self.assertEqual(rx.client_ip("172.18.0.1", "9.9.9.9, 5.6.7.8"), "5.6.7.8")
            self.assertEqual(rx.client_ip("172.18.0.1", "garbage"), "172.18.0.1")
            self.assertEqual(rx.client_ip("172.18.0.1", ""), "172.18.0.1")
            self.assertEqual(rx.client_ip("172.18.0.7", "5.6.7.8"), "172.18.0.7")

    def test_trusted_proxies_env(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = make_cfg(d, {"TV_TRUSTED_PROXIES": "172.18.0.0/16"})
            rx = receiver.Receiver(cfg, forwarder=FakeForwarder())
            self.assertEqual(rx.client_ip("172.18.0.7", "5.6.7.8"), "5.6.7.8")


class TestSchema(ServerCase):
    BAD = {
        "v": [2, "1", True, None, DROP],
        "src": ["tv", None, 1, DROP],
        "symbol": ["MES 1!", "MES\"1", "x" * 25, "", 5, DROP, "MES1!\n"],
        "tf": ["15m!", "1234567", "", 15, DROP],
        "bar_time": [str(BAR_MS), 1.5, True, None, int((NOW - 37 * 3600) * 1000), int((NOW + 37 * 3600) * 1000),
                     10 ** 400, -1, DROP],
        "close": [0, -1, "6000", None, True, DROP],
        "spread": [-0.001, "0.1", None, DROP],
        "max_spread10": [-1, None, DROP],
        "atr_pct": [-1, None, DROP],
        "atr_pct_10_ago": [-1, None, DROP],
        "range": [-0.25, None, False, DROP],
        "nr7": [1, "true", None, DROP],
    }

    def test_each_field(self):
        for field, values in self.BAD.items():
            for value in values:
                self.clock.now += 3  # stay under 30 req/min
                status, body, _ = self.post(alert(**{field: value}))
                self.assertEqual(status, 422, (field, value))
                self.assertEqual(body, {"ok": False, "error": "invalid_field", "field": field}, (field, value))
        self.assertEqual(self.fwd.sent, [])

    def test_overflowing_number_literal(self):
        # 1e400 parses to inf without going through parse_constant: must still be rejected as the field.
        for field in ("close", "spread", "range"):
            raw = json.dumps(alert(**{field: 1.5})).replace(f'"{field}": 1.5', f'"{field}": 1e400')
            self.assertIn("1e400", raw)
            self.assertEqual(self.post(raw)[1], {"ok": False, "error": "invalid_field", "field": field})

    def test_nan_infinity_and_bad_json(self):
        for raw in ('{"key":"%s","close":NaN}' % KEY, '{"key":"%s","close":Infinity}' % KEY, "{", "[1]", "",
                    b"\xff\xfe"):
            status, body, _ = self.post(raw)
            self.assertEqual((status, body["error"]), (400, "invalid_json"), raw)

    def test_bom_and_whitespace(self):
        raw = "﻿ \n\t" + json.dumps(alert()) + "\r\n "
        self.assertEqual(self.post(raw.encode("utf-8"))[1]["action"], "forwarded")

    def test_unknown_fields_dropped_and_integral_float_bar_time(self):
        status, body, _ = self.post(alert(note="buy now!", extra={"a": 1}, bar_time=float(BAR_MS)))
        self.assertEqual(body["action"], "forwarded")
        self.assertNotIn("buy now", json.dumps(self.fwd.sent))

    def test_symbol_not_allowed(self):
        for sym in ("ES1!", "CME_MINI:ES1!", "MESS", "AAPL", "MES1!X", "BAD-PREFIX:MES1!"):
            status, body, _ = self.post(alert(symbol=sym))
            self.assertEqual((status, body), (422, {"ok": False, "error": "symbol_not_allowed", "field": "symbol"}),
                             sym)

    def test_tf_not_allowed(self):
        status, body, _ = self.post(alert(tf="5"))
        self.assertEqual((status, body), (422, {"ok": False, "error": "tf_not_allowed", "field": "tf"}))


class TestEnvAllowlists(ServerCase):
    env = {"TV_SYMBOLS": "MES", "TV_TIMEFRAMES": "5,15"}

    def test_custom_lists(self):
        self.assertEqual(self.post(alert(symbol="MNQ1!"))[0], 422)
        self.assertEqual(self.post(alert(tf="5"))[1]["action"], "forwarded")


class TestSymbols(unittest.TestCase):
    ALLOWED = receiver.DEFAULT_SYMBOLS.split(",")

    def test_normalize(self):
        cases = {
            "CME_MINI:MES1!": "MES", "MES1!": "MES", "MES": "MES", "mes1!": "MES", "MESZ2026": "MES",
            "MESZ26": "MES", "MESZ6": "MES", "CME_MINI:MESZ2026": "MES", "MES2!": "MES",
            "CME_MINI:MNQ1!": "MNQ", "CBOT_MINI:MYM1!": "MYM", "CME_MINI:M2K1!": "M2K", "M2KH2027": "M2K",
            "NYMEX:MCL1!": "MCL", "MCLF27": "MCL", "COMEX:MGC1!": "MGC", "MGCG2027": "MGC",
        }
        for sym, root in cases.items():
            self.assertEqual(receiver.normalize_symbol(sym, self.ALLOWED), root, sym)

    def test_reject(self):
        for sym in ("ES1!", "MESA2026", "MES1", "MES123!", "MESZ20261", "MESZ", "M2", "MGCX!", ":MES1!", "A:B:MES1!x"):
            self.assertIsNone(receiver.normalize_symbol(sym, self.ALLOWED), sym)


class TestSession(unittest.TestCase):
    def open_at(self, *args):
        return receiver.globex_open(datetime(*args, tzinfo=timezone.utc))

    def test_points(self):
        # 2026-10-31 Sat; 2026-10-30 Fri; EDT (UTC-4) until Sun 2026-11-01 02:00, then EST (UTC-5).
        self.assertFalse(self.open_at(2026, 10, 31, 16, 0))         # Sat 12:00 EDT
        self.assertFalse(self.open_at(2026, 10, 30, 21, 30))        # Fri 17:30 EDT
        self.assertTrue(self.open_at(2026, 10, 30, 20, 59))         # Fri 16:59 EDT
        self.assertFalse(self.open_at(2026, 11, 1, 22, 59))         # Sun 17:59 EST (DST just ended)
        self.assertTrue(self.open_at(2026, 11, 1, 23, 1))           # Sun 18:01 EST
        self.assertTrue(self.open_at(2026, 11, 2, 15, 0))           # Mon 10:00 EST
        self.assertFalse(self.open_at(2026, 10, 25, 21, 59))        # Sun 17:59 EDT
        self.assertTrue(self.open_at(2026, 10, 25, 22, 1))          # Sun 18:01 EDT
        self.assertTrue(self.open_at(2026, 10, 26, 14, 0))          # Mon 10:00 EDT
        self.assertTrue(self.open_at(2026, 3, 9, 14, 0))            # Mon 10:00 EDT (DST began 03-08)
        self.assertTrue(self.open_at(2026, 3, 6, 15, 0))            # Fri 10:00 EST

    def test_daily_break_follows_dst(self):
        self.assertFalse(self.open_at(2026, 10, 27, 21, 30))        # Tue 17:30 EDT
        self.assertTrue(self.open_at(2026, 11, 3, 21, 30))          # Tue 16:30 EST
        self.assertFalse(self.open_at(2026, 11, 3, 22, 30))         # Tue 17:30 EST
        self.assertTrue(self.open_at(2026, 11, 3, 23, 0))           # Tue 18:00 EST
        self.assertFalse(self.open_at(2026, 11, 3, 22, 0))          # Tue 17:00 EST


class TestGates(ServerCase):
    def ignored(self, reason, **over):
        status, body, _ = self.post(alert(**over))
        self.assertEqual((status, body), (200, {"ok": True, "action": "ignored", "reason": reason}), over)

    def test_session_closed(self):
        # Bar opened Mon 2026-10-26 17:15 EDT (daily break), within 36h.
        self.ignored("session_closed", bar_time=int(utc(2026, 10, 26, 21, 15) * 1000))
        self.clock.now = utc(2026, 10, 31, 16, 0)  # Sat
        self.ignored("session_closed", bar_time=int((self.clock.now - 900) * 1000))
        self.assertEqual(self.fwd.sent, [])

    def test_coil_boundaries(self):
        self.ignored("not_coil_nr7", nr7=False)
        self.ignored("not_coil_nr7", spread=0.004)
        self.ignored("not_coil_nr7", max_spread10=0.004)
        self.ignored("not_coil_nr7", atr_pct=0.0011, atr_pct_10_ago=0.0011)
        self.ignored("not_coil_nr7", atr_pct=0.0012)
        self.assertEqual(self.fwd.sent, [])
        status, body, _ = self.post(alert(spread=0.003999, max_spread10=0.003999, atr_pct=0.00109999))
        self.assertEqual(body["action"], "forwarded")

    def test_dedupe_and_survives_restart(self):
        self.assertEqual(self.post(alert())[1]["action"], "forwarded")
        self.ignored("duplicate")
        self.ignored("duplicate", symbol="CME_MINI:MES1!")  # same root
        self.stop()
        self.start()
        self.ignored("duplicate")
        self.clock.now += 48 * 3600 + 1  # past dedupe window, and bar now too old
        self.assertEqual(self.post(alert())[0], 422)

    def test_cooldown(self):
        self.assertEqual(self.post(alert())[1]["action"], "forwarded")
        self.clock.now += 15 * 60
        self.ignored("cooldown", bar_time=BAR_MS + 900000)
        self.assertEqual(self.post(alert(symbol="MNQ1!", bar_time=BAR_MS + 900000))[1]["action"], "forwarded")
        self.clock.now += 15 * 60
        self.assertEqual(self.post(alert(bar_time=BAR_MS + 1800000))[1]["action"], "forwarded")

    def test_forward_failure_releases_dedupe_and_cooldown(self):
        self.fwd.results = [(False, 503)]
        status, body, _ = self.post(alert())
        self.assertEqual((status, body), (502, {"ok": False, "error": "forward_failed"}))
        self.assertEqual(self.rx.health()["forwarded_today"], 0)
        self.assertEqual(self.post(alert())[1]["action"], "forwarded")
        self.assertEqual(self.rx.health()["forwarded_today"], 1)


class TestForwardBody(ServerCase):
    def test_exact_schema(self):
        self.post(alert(symbol="CME_MINI:MESZ2026", note="x", key=KEY2))
        (sent,) = self.fwd.sent
        self.assertEqual(tuple(sent), receiver.FORWARD_KEYS)
        self.assertEqual(sent, {
            "kind": "futures_coil", "symbol": "MES", "tf": "15", "bar_time_utc": "2026-10-27T13:45:00Z",
            "close": 6012.25, "spread_pct": 0.0012, "max_spread10_pct": 0.0031, "atr_pct": 0.0009,
            "atr_pct_10_ago": 0.0011, "range": 3.5, "source": "tradingview-pine", "received_utc": "2026-10-27T14:00:00Z",
        })
        text = json.dumps(sent)
        for secret in (KEY, KEY2, KEY3, BEARER, "key"):
            self.assertNotIn(secret, text)


class FakeTickerBull:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.requests = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                fake.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                status = fake.statuses.pop(0) if fake.statuses else 200
                self.send_response(status)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hooks/tickerbull"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class TestForwarder(unittest.TestCase):
    CFG = {"url": "http://example.invalid/h", "auth": BEARER}

    def run_with(self, results):
        calls = []

        def post(url, body, headers, timeout):
            calls.append((url, body, headers, timeout))
            return results.pop(0)

        ok, status = receiver.Forwarder(self.CFG, post=post, sleep=lambda s: None).send({"a": 1})
        return ok, status, calls

    def test_success_first_try(self):
        ok, status, calls = self.run_with([204])
        self.assertEqual((ok, status, len(calls)), (True, 204, 1))
        _, body, headers, timeout = calls[0]
        self.assertEqual(headers, {"Content-Type": "application/json", "Authorization": "Bearer " + BEARER})
        self.assertEqual((body, timeout), (b'{"a":1}', 8))

    def test_retry_once_on_5xx_then_ok(self):
        self.assertEqual(self.run_with([500, 200])[:2], (True, 200))

    def test_retry_once_on_timeout_then_fail(self):
        ok, status, calls = self.run_with([None, None, 200])
        self.assertEqual((ok, status, len(calls)), (False, None, 2))

    def test_two_5xx_stop(self):
        ok, status, calls = self.run_with([502, 503, 200])
        self.assertEqual((ok, status, len(calls)), (False, 503, 2))

    def test_no_retry_on_4xx_or_3xx(self):
        for code in (400, 401, 404, 302):
            ok, status, calls = self.run_with([code, 200])
            self.assertEqual((ok, status, len(calls)), (False, code, 1), code)

    def test_failure_log_has_no_bearer(self):
        with mock.patch("sys.stderr") as err:
            self.run_with([500, 500])
        written = "".join(str(c) for c in err.write.call_args_list)
        self.assertIn("forward failed", written)
        self.assertNotIn(BEARER, written)

    def test_real_http_post_with_retry(self):
        fake = FakeTickerBull([500, 200])
        try:
            fwd = receiver.Forwarder({"url": fake.url, "auth": BEARER}, sleep=lambda s: None)
            self.assertEqual(fwd.send({"kind": "futures_coil"}), (True, 200))
            self.assertEqual(len(fake.requests), 2)
            req = fake.requests[1]
            self.assertEqual(req["path"], "/hooks/tickerbull")
            self.assertEqual(req["headers"]["Authorization"], "Bearer " + BEARER)
            self.assertEqual(req["headers"]["Content-Type"], "application/json")
            self.assertEqual(json.loads(req["body"]), {"kind": "futures_coil"})
        finally:
            fake.close()

    def test_real_http_post_connection_refused(self):
        fake = FakeTickerBull([])
        url = fake.url
        fake.close()
        self.assertEqual(receiver.http_post(url, b"{}", {}, 2), None)

    def test_redirect_not_followed(self):
        fake = FakeTickerBull([307])
        try:
            self.assertEqual(receiver.http_post(fake.url, b"{}", {}, 2), 307)
            self.assertEqual(len(fake.requests), 1)
        finally:
            fake.close()


class TestKillHealthRoutes(ServerCase):
    def test_health_content(self):
        self.post(alert())
        self.post(alert(key="bad"))
        status, body, resp = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "kill": False, "forwarded_today": 1, "rejected_today": 1,
                                "last_forward_utc": "2026-10-27T14:00:00Z"})
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")

    def test_daily_counters_reset(self):
        self.post(alert())
        self.clock.now += 86400
        self.assertEqual(self.request("GET", "/health")[1]["forwarded_today"], 0)

    def test_kill_file(self):
        open(os.path.join(self.dir, "KILL_TV"), "w").close()
        status, body, _ = self.request("GET", "/health")
        self.assertEqual((status, body["kill"], body["ok"]), (503, True, False))
        self.assertEqual(self.post(alert())[0], 503)
        self.assertEqual(self.request("GET", "/nope")[0], 503)
        self.assertEqual(self.fwd.sent, [])
        os.remove(os.path.join(self.dir, "KILL_TV"))
        self.assertEqual(self.post(alert())[0], 200)

    def test_404_405(self):
        self.assertEqual(self.request("GET", "/")[0], 404)
        self.assertEqual(self.request("POST", "/tv-futures/alert", "{}")[0], 404)
        self.assertEqual(self.request("GET", "/alert/x")[0], 404)
        status, _, resp = self.request("GET", "/alert")
        self.assertEqual((status, resp.getheader("Allow")), (405, "POST"))
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            self.assertEqual(self.request(method, "/alert")[0], 405, method)
        status, _, resp = self.request("POST", "/health", "{}")
        self.assertEqual((status, resp.getheader("Allow")), (405, "GET"))

    def test_internal_error_does_not_leak(self):
        with mock.patch.object(self.rx, "handle_alert", side_effect=RuntimeError(KEY)):
            status, body, _ = self.post(alert())
        self.assertEqual((status, body), (500, {"ok": False, "error": "internal_error"}))


class TestLog(ServerCase):
    def test_log_has_no_key_or_body(self):
        self.post(alert())
        self.post(alert(key=KEY[:-1] + "Q", symbol="MNQ1!"))
        self.post(alert(nr7=False, symbol="MGC1!"))
        self.post(alert(v=9))
        raw = ""
        for name in ("alerts.jsonl", "state.json"):
            with open(os.path.join(self.dir, name)) as fh:
                raw += fh.read()
        for secret in (KEY, KEY[:-1] + "Q", BEARER, "6012.25"):
            self.assertNotIn(secret, raw)
        lines = self.log_lines()
        self.assertEqual([(l["action"], l["reason"], l["status"]) for l in lines], [
            ("forwarded", None, 200), ("auth_failed", "forbidden", 403), ("ignored", "not_coil_nr7", 200),
            ("rejected", "invalid_field:v", 422),
        ])
        self.assertEqual((lines[0]["symbol"], lines[0]["tf"], lines[0]["bar_time"]), ("MES", "15", BAR_MS))
        self.assertEqual(lines[0]["upstream_status"], 200)
        self.assertIsNone(lines[1]["symbol"])  # nothing from an unauthenticated body
        self.assertEqual(set(lines[2]), {"ts", "symbol", "tf", "bar_time", "action", "reason", "status"})

    def test_rotation_keeps_two(self):
        with mock.patch.object(receiver, "LOG_MAX_BYTES", 200):
            for _ in range(12):
                self.post(alert(nr7=False))
        names = sorted(n for n in os.listdir(self.dir) if n.startswith("alerts.jsonl"))
        self.assertEqual(names, ["alerts.jsonl", "alerts.jsonl.1", "alerts.jsonl.2"])
        for name in names:
            self.assertLessEqual(os.path.getsize(os.path.join(self.dir, name)), 400)

    def test_unwritable_state_dir_keeps_serving(self):
        self.rx.state.dir = self.rx.state.path = self.rx.state.log_path = "/nonexistent/dir/x"
        with mock.patch("sys.stderr"):
            self.assertEqual(self.post(alert())[1]["action"], "forwarded")


class TestConfig(unittest.TestCase):
    BASE = {"TV_KEYS": KEY, "BD_TICKERBULL_WEBHOOK_URL": "https://x/h", "BD_TICKERBULL_WEBHOOK_AUTH": BEARER}

    def load(self, **over):
        env = dict(self.BASE)
        env.update(over)
        return receiver.load_config(env, route_file="/nonexistent")

    def test_ok_and_defaults(self):
        cfg = self.load()
        self.assertEqual(cfg["symbols"], ["MES", "MNQ", "MYM", "M2K", "MCL", "MGC"])
        self.assertEqual(cfg["timeframes"], ["15"])
        self.assertEqual(cfg["kill_file"], "/run/tv/KILL_TV")
        self.assertEqual(cfg["state_dir"], "/run/tv")

    def test_rejects(self):
        for over in ({"TV_KEYS": ""}, {"TV_KEYS": "short"}, {"TV_KEYS": KEY + ",x" * 1},
                     {"BD_TICKERBULL_WEBHOOK_URL": ""}, {"BD_TICKERBULL_WEBHOOK_URL": "ftp://x"},
                     {"BD_TICKERBULL_WEBHOOK_AUTH": ""}, {"TV_SYMBOLS": "ME S"}, {"TV_TIMEFRAMES": "15m!"},
                     {"TV_TRUSTED_PROXIES": "nope"}):
            with self.assertRaises(receiver.ConfigError, msg=over) as cm:
                self.load(**over)
            for secret in (KEY, BEARER):
                self.assertNotIn(secret, str(cm.exception))


if __name__ == "__main__":
    unittest.main()
