"""Bull Desk tv-futures: TradingView futures-coil alert receiver.

TradingView (Pine alert() webhook, JSON text body, no custom headers) posts to
/alert. The shared key travels in the body ("key"). The receiver validates a
fixed schema, applies session / coil / dedupe / cooldown gates and forwards a
FIXED-SCHEMA sanitized JSON to the Bull Desk TickerBull webhook (Bearer).
No free text is ever forwarded. It never trades and never talks to BullWarden.
Python stdlib only.
"""

import collections
import hmac
import json
import math
import os
import re
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import ipaddress
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

HOST = "0.0.0.0"
PORT = 8080
MIN_KEY_LEN = 24
MAX_BODY = 2048
GLOBAL_PER_MINUTE = 30
IP_PER_MINUTE = 30
FORWARDS_PER_DAY = 300
AUTH_FAIL_LIMIT = 10
AUTH_FAIL_WINDOW_S = 600
IP_BLOCK_S = 900
MAX_TRACKED_IPS = 10000
BAR_MAX_AGE_S = 36 * 3600
DEDUPE_S = 48 * 3600
COOLDOWN_S = 30 * 60
SPREAD_MAX = 0.004
FORWARD_TIMEOUT = 8
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUPS = 2
DEFAULT_SYMBOLS = "MES,MNQ,MYM,M2K,MCL,MGC"
DEFAULT_TIMEFRAMES = "15"
SOURCE_IN = "tv-futures"
SOURCE_OUT = "tradingview-pine"
NY = ZoneInfo("America/New_York")

SYMBOL_RE = re.compile(r"^[A-Za-z0-9_:!.\-]{1,24}$")
TF_RE = re.compile(r"^[0-9A-Za-z]{1,6}$")
ROOT_RE = re.compile(r"^[A-Z0-9]{1,8}$")
# After the root: nothing, a continuous suffix (1!, 2!) or a month code + 1-4 digit year (Z6, Z26, Z2026).
CONTRACT_SUFFIX_RE = re.compile(r"^(?:[0-9]{1,2}!|[FGHJKMNQUVXZ][0-9]{1,4})?$")
FLOAT_FIELDS = ("spread", "max_spread10", "atr_pct", "atr_pct_10_ago", "range")
# Forward schema, in order. Nothing else is ever sent.
FORWARD_KEYS = ("kind", "symbol", "tf", "bar_time_utc", "close", "spread_pct", "max_spread10_pct", "atr_pct",
                "atr_pct_10_ago", "range", "source", "received_utc")


class ConfigError(Exception):
    pass


class Reject(Exception):
    """A request decision that is not a forward: HTTP status + JSON body."""

    def __init__(self, status, error, field=None, action="rejected"):
        super().__init__(error)
        self.status = status
        self.error = error
        self.field = field
        self.action = action


def split_list(env, name, default=""):
    return [k.strip() for k in env.get(name, default).split(",") if k.strip()]


def default_gateway(route_file="/proc/net/route"):
    """IPv4 default gateway of this container (the docker network gateway), or None."""
    try:
        with open(route_file) as fh:
            next(fh, None)
            for line in fh:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "00000000":
                    return socket.inet_ntoa(struct.pack("<L", int(parts[2], 16)))
    except (OSError, ValueError, struct.error):
        pass
    return None


def load_config(env, route_file="/proc/net/route"):
    keys = split_list(env, "TV_KEYS")
    if not keys:
        raise ConfigError("TV_KEYS missing or empty")
    if any(len(k) < MIN_KEY_LEN for k in keys):
        raise ConfigError(f"every TV_KEYS entry must be at least {MIN_KEY_LEN} chars")
    url = env.get("BD_TICKERBULL_WEBHOOK_URL", "").strip()
    auth = env.get("BD_TICKERBULL_WEBHOOK_AUTH", "").strip()
    if not url:
        raise ConfigError("BD_TICKERBULL_WEBHOOK_URL missing or empty")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError("BD_TICKERBULL_WEBHOOK_URL must be an http(s) URL")
    if not auth:
        raise ConfigError("BD_TICKERBULL_WEBHOOK_AUTH missing or empty")
    symbols = [s.upper() for s in split_list(env, "TV_SYMBOLS", DEFAULT_SYMBOLS)]
    if not symbols or any(not ROOT_RE.fullmatch(s) for s in symbols):
        raise ConfigError("TV_SYMBOLS must be a comma list of roots like MES")
    timeframes = split_list(env, "TV_TIMEFRAMES", DEFAULT_TIMEFRAMES)
    if not timeframes or any(not TF_RE.fullmatch(t) for t in timeframes):
        raise ConfigError("TV_TIMEFRAMES must be a comma list like 15")
    trusted = []
    try:
        for item in split_list(env, "TV_TRUSTED_PROXIES"):
            trusted.append(ipaddress.ip_network(item, strict=False))
    except ValueError:
        raise ConfigError("TV_TRUSTED_PROXIES must be a comma list of IPs/CIDRs") from None
    gw = default_gateway(route_file)
    if gw:
        trusted.append(ipaddress.ip_network(gw + "/32"))
    state_dir = env.get("TV_STATE_DIR", "/run/tv")
    return {
        "keys": [k.encode() for k in keys],
        "url": url,
        "auth": auth,
        "symbols": symbols,
        "timeframes": timeframes,
        "trusted_proxies": trusted,
        "kill_file": os.path.join(env.get("TV_KILL_DIR", "/run/tv"), "KILL_TV"),
        "state_dir": state_dir,
    }


# ── pure helpers ──

def match_key(keys, candidate):
    """True if candidate equals one of keys. Every key is compared (no early exit)."""
    token = candidate.encode() if isinstance(candidate, str) else b""
    matched = False
    for key in keys:
        matched |= hmac.compare_digest(token, key)
    return matched and bool(token)


def normalize_symbol(symbol, allowed):
    """Allowlisted root for "CME_MINI:MES1!", "MESZ2026", "MES1!", "MES"; else None."""
    s = symbol.upper()
    if ":" in s:
        prefix, _, s = s.rpartition(":")
        if not re.fullmatch(r"[A-Z0-9_]{1,16}", prefix):
            return None
    for root in sorted(allowed, key=len, reverse=True):
        if s.startswith(root) and CONTRACT_SUFFIX_RE.fullmatch(s[len(root):]):
            return root
    return None


def globex_open(when):
    """CME Globex open at aware datetime `when`. Closed Fri 17:00 ET -> Sun 18:00 ET and daily 17:00-18:00 ET.

    Exchange holidays are not modelled.
    """
    et = when.astimezone(NY)
    wd, hour = et.weekday(), et.hour  # Mon=0 .. Sun=6
    if wd == 5:
        return False
    if wd == 6:
        return hour >= 18
    if wd == 4:
        return hour < 17
    return hour != 17


def iso_utc(epoch_s):
    return datetime.fromtimestamp(epoch_s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _reject_constant(_value):
    raise ValueError("non-finite")


def parse_json(raw):
    """Decode a TradingView body: tolerates BOM and surrounding whitespace. Raises Reject(400)."""
    try:
        text = raw.decode("utf-8-sig").strip()
        data = json.loads(text, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Reject(400, "invalid_json") from None
    if not isinstance(data, dict):
        raise Reject(400, "invalid_json")
    return data


def _finite(value, field, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Reject(422, "invalid_field", field)
    try:
        value = float(value)
    except OverflowError:
        raise Reject(422, "invalid_field", field) from None
    if not math.isfinite(value) or value < 0 or (positive and value <= 0):
        raise Reject(422, "invalid_field", field)
    return value


def validate_alert(data, cfg, now):
    """Return the sanitized alert dict or raise Reject(422, field). Unknown fields are dropped."""
    if data.get("v") != 1 or isinstance(data.get("v"), bool):
        raise Reject(422, "invalid_field", "v")
    if data.get("src") != SOURCE_IN:
        raise Reject(422, "invalid_field", "src")
    symbol = data.get("symbol")
    if not isinstance(symbol, str) or not SYMBOL_RE.fullmatch(symbol):
        raise Reject(422, "invalid_field", "symbol")
    tf = data.get("tf")
    if not isinstance(tf, str) or not TF_RE.fullmatch(tf):
        raise Reject(422, "invalid_field", "tf")
    bar_time = data.get("bar_time")
    if isinstance(bar_time, float) and math.isfinite(bar_time) and bar_time.is_integer():
        bar_time = int(bar_time)
    # Range before division: a huge JSON int would overflow float division.
    if (isinstance(bar_time, bool) or not isinstance(bar_time, int) or not 0 < bar_time < 10 ** 14
            or abs(bar_time / 1000 - now) > BAR_MAX_AGE_S):
        raise Reject(422, "invalid_field", "bar_time")
    out = {"symbol": symbol, "tf": tf, "bar_time": bar_time, "close": _finite(data.get("close"), "close", True)}
    for name in FLOAT_FIELDS:
        out[name] = _finite(data.get(name), name)
    if not isinstance(data.get("nr7"), bool):
        raise Reject(422, "invalid_field", "nr7")
    out["nr7"] = data["nr7"]
    root = normalize_symbol(symbol, cfg["symbols"])
    if root is None:
        raise Reject(422, "symbol_not_allowed", "symbol")
    if tf not in cfg["timeframes"]:
        raise Reject(422, "tf_not_allowed", "tf")
    out["root"] = root
    return out


def coil_fire(alert):
    return (alert["nr7"] is True and alert["spread"] < SPREAD_MAX and alert["max_spread10"] < SPREAD_MAX
            and alert["atr_pct"] < alert["atr_pct_10_ago"])


def forward_body(alert, now):
    body = {
        "kind": "futures_coil",
        "symbol": alert["root"],
        "tf": alert["tf"],
        "bar_time_utc": iso_utc(alert["bar_time"] / 1000),
        "close": alert["close"],
        "spread_pct": alert["spread"],
        "max_spread10_pct": alert["max_spread10"],
        "atr_pct": alert["atr_pct"],
        "atr_pct_10_ago": alert["atr_pct_10_ago"],
        "range": alert["range"],
        "source": SOURCE_OUT,
        "received_utc": iso_utc(now),
    }
    assert tuple(body) == FORWARD_KEYS
    return body


# ── forwarding ──

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def http_post(url, body, headers, timeout):
    """One POST. Returns the HTTP status, or None on timeout / connection error."""
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as err:
        status = err.code
        err.close()
        return status
    except (urllib.error.URLError, OSError, ValueError):
        return None


class Forwarder:
    """POST to TickerBull with Bearer auth; one retry on 5xx / timeout. Never logs the bearer."""

    def __init__(self, cfg, post=http_post, sleep=time.sleep):
        self.cfg = cfg
        self.post = post
        self.sleep = sleep

    def send(self, payload):
        """(ok, last_status) where last_status is an int or None (no response)."""
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + self.cfg["auth"]}
        status = None
        for attempt in (1, 2):
            status = self.post(self.cfg["url"], body, headers, FORWARD_TIMEOUT)
            if status is not None and 200 <= status < 300:
                return True, status
            print(f"tv-futures forward failed attempt={attempt} status={status}", file=sys.stderr, flush=True)
            if status is not None and status < 500:
                break  # 3xx/4xx: retrying will not help
            if attempt == 1:
                self.sleep(1)
        return False, status


# ── state ──

class SlidingWindow:
    def __init__(self, span, limit):
        self.span = span
        self.limit = limit
        self.hits = collections.deque()

    def allow(self, now):
        while self.hits and self.hits[0] <= now - self.span:
            self.hits.popleft()
        if len(self.hits) >= self.limit:
            return False
        self.hits.append(now)
        return True

    def count(self, now):
        while self.hits and self.hits[0] <= now - self.span:
            self.hits.popleft()
        return len(self.hits)


class State:
    """Daily counters, dedupe and cooldown; persisted to TV_STATE_DIR/state.json; decisions to alerts.jsonl."""

    def __init__(self, state_dir):
        self.dir = state_dir
        self.path = os.path.join(state_dir, "state.json")
        self.log_path = os.path.join(state_dir, "alerts.jsonl")
        self.day = ""
        self.forwarded = 0
        self.rejected = 0
        self.last_forward = None
        self.seen = {}      # "ROOT|tf|bar_time" -> epoch s
        self.cooldown = {}  # ROOT -> epoch s of last forward
        self._warned = False
        self.load()

    def _warn(self, what):
        if not self._warned:
            print(f"tv-futures state dir not writable ({what}); continuing in memory", file=sys.stderr, flush=True)
            self._warned = True

    def load(self):
        try:
            with open(self.path, "rb") as fh:
                data = json.loads(fh.read(4 * 1024 * 1024))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        self.day = data.get("day") if isinstance(data.get("day"), str) else ""
        self.forwarded = data.get("forwarded") if isinstance(data.get("forwarded"), int) else 0
        self.rejected = data.get("rejected") if isinstance(data.get("rejected"), int) else 0
        self.last_forward = data.get("last_forward_utc") if isinstance(data.get("last_forward_utc"), str) else None
        for name in ("seen", "cooldown"):
            raw = data.get(name)
            if isinstance(raw, dict):
                setattr(self, name, {str(k): float(v) for k, v in raw.items()
                                     if isinstance(v, (int, float)) and not isinstance(v, bool)})

    def roll(self, now):
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        if day != self.day:
            self.day, self.forwarded, self.rejected = day, 0, 0
        self.seen = {k: t for k, t in self.seen.items() if now - t < DEDUPE_S}
        self.cooldown = {k: t for k, t in self.cooldown.items() if now - t < COOLDOWN_S}

    def save(self):
        data = {"day": self.day, "forwarded": self.forwarded, "rejected": self.rejected,
                "last_forward_utc": self.last_forward, "seen": self.seen, "cooldown": self.cooldown}
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(data, fh, separators=(",", ":"))
            os.replace(tmp, self.path)
        except OSError:
            self._warn("state")

    def log(self, entry):
        line = json.dumps(entry, separators=(",", ":")) + "\n"
        try:
            if os.path.exists(self.log_path) and os.path.getsize(self.log_path) >= LOG_MAX_BYTES:
                for i in range(LOG_BACKUPS, 0, -1):
                    src = self.log_path if i == 1 else f"{self.log_path}.{i - 1}"
                    if os.path.exists(src):
                        os.replace(src, f"{self.log_path}.{i}")
            with open(self.log_path, "a") as fh:
                fh.write(line)
        except OSError:
            self._warn("log")


class Receiver:
    def __init__(self, cfg, forwarder=None, clock=time.time):
        self.cfg = cfg
        self.forwarder = forwarder or Forwarder(cfg)
        self.clock = clock
        self.lock = threading.Lock()
        self.state = State(cfg["state_dir"])
        self.global_rate = SlidingWindow(60, GLOBAL_PER_MINUTE)
        self.ip_rate = {}
        self.ip_fails = {}
        self.ip_blocked = {}

    def killed(self):
        return os.path.exists(self.cfg["kill_file"])

    def health(self):
        with self.lock:
            self.state.roll(self.clock())
            return {"ok": not self.killed(), "kill": self.killed(), "forwarded_today": self.state.forwarded,
                    "rejected_today": self.state.rejected, "last_forward_utc": self.state.last_forward}

    def client_ip(self, peer, xff):
        """Peer address; the X-Forwarded-For hop the proxy appended only when the peer is a trusted proxy."""
        try:
            peer_ip = ipaddress.ip_address(peer)
        except ValueError:
            return peer
        if xff and any(peer_ip in net for net in self.cfg["trusted_proxies"]):
            hop = xff.split(",")[-1].strip()
            try:
                return str(ipaddress.ip_address(hop))
            except ValueError:
                return peer
        return peer

    def _prune_ips(self, now):
        if len(self.ip_rate) + len(self.ip_fails) + len(self.ip_blocked) < MAX_TRACKED_IPS:
            return
        self.ip_blocked = {ip: t for ip, t in self.ip_blocked.items() if t > now}
        self.ip_fails = {ip: w for ip, w in self.ip_fails.items() if w.count(now)}
        self.ip_rate = {ip: w for ip, w in self.ip_rate.items() if w.count(now)}

    def admit(self, ip):
        """Rate limits and IP block before the body is read. Raises Reject(429)."""
        now = self.clock()
        with self.lock:
            self._prune_ips(now)
            if self.ip_blocked.get(ip, 0) > now:
                raise Reject(429, "blocked")
            if not self.global_rate.allow(now):
                raise Reject(429, "rate_limited")
            if not self.ip_rate.setdefault(ip, SlidingWindow(60, IP_PER_MINUTE)).allow(now):
                raise Reject(429, "rate_limited")

    def auth_failed(self, ip):
        now = self.clock()
        with self.lock:
            window = self.ip_fails.setdefault(ip, SlidingWindow(AUTH_FAIL_WINDOW_S, 10 ** 9))
            window.allow(now)
            if window.count(now) >= AUTH_FAIL_LIMIT:
                self.ip_blocked[ip] = now + IP_BLOCK_S
                window.hits.clear()

    def record(self, entry, rejected=False):
        with self.lock:
            self.state.roll(self.clock())
            if rejected:
                self.state.rejected += 1
            self.state.log(entry)
            self.state.save()

    def handle_alert(self, raw):
        """Authenticated-or-not body -> (status, response, log fields). Never echoes the body or key."""
        now = self.clock()
        data = parse_json(raw)
        if not match_key(self.cfg["keys"], data.get("key")):
            raise Reject(403, "forbidden", action="auth_failed")
        alert = validate_alert(data, self.cfg, now)
        fields = {"symbol": alert["root"], "tf": alert["tf"], "bar_time": alert["bar_time"]}
        if not globex_open(datetime.fromtimestamp(alert["bar_time"] / 1000, timezone.utc)):
            return 200, {"ok": True, "action": "ignored", "reason": "session_closed"}, fields
        if not coil_fire(alert):
            return 200, {"ok": True, "action": "ignored", "reason": "not_coil_nr7"}, fields
        dedupe_key = f"{alert['root']}|{alert['tf']}|{alert['bar_time']}"
        with self.lock:
            st = self.state
            st.roll(now)
            if dedupe_key in st.seen:
                return 200, {"ok": True, "action": "ignored", "reason": "duplicate"}, fields
            if alert["root"] in st.cooldown:
                return 200, {"ok": True, "action": "ignored", "reason": "cooldown"}, fields
            if st.forwarded >= FORWARDS_PER_DAY:
                return 200, {"ok": True, "action": "ignored", "reason": "daily_cap"}, fields
            # Reserve so a concurrent identical alert is deduped while we forward.
            prev_cooldown = st.cooldown.get(alert["root"])
            st.seen[dedupe_key] = now
            st.cooldown[alert["root"]] = now
            st.forwarded += 1
        ok, status = self.forwarder.send(forward_body(alert, now))
        with self.lock:
            st = self.state
            if ok:
                st.last_forward = iso_utc(now)
            else:
                st.seen.pop(dedupe_key, None)
                if prev_cooldown is None:
                    st.cooldown.pop(alert["root"], None)
                else:
                    st.cooldown[alert["root"]] = prev_cooldown
                st.forwarded = max(0, st.forwarded - 1)
        fields["upstream_status"] = status
        if ok:
            return 200, {"ok": True, "action": "forwarded"}, fields
        raise Reject(502, "forward_failed", action="forward_failed")


# ── HTTP ──

def make_handler(rx):
    class Handler(BaseHTTPRequestHandler):
        server_version = "tv-futures"
        sys_version = ""
        timeout = 10

        def log_message(self, *_args):  # default log would include the raw request line
            pass

        def _send(self, status, obj, headers=None):
            body = json.dumps(obj, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _read_body(self):
            if self.headers.get("Transfer-Encoding"):
                raise Reject(411, "length_required")
            length = self.headers.get("Content-Length")
            if length is None or not length.strip().isdigit():
                raise Reject(411, "length_required")
            if int(length) > MAX_BODY:
                self.close_connection = True
                raise Reject(413, "body_too_large")
            return self.rfile.read(int(length))

        def _dispatch(self):
            try:
                status, obj, headers = self._route()
            except Exception as e:  # never leak details
                print(f"tv-futures internal error {type(e).__name__}", file=sys.stderr, flush=True)
                status, obj, headers = 500, {"ok": False, "error": "internal_error"}, None
            if status >= 400 and status not in (404, 405, 429, 503):
                self.close_connection = True
            self._send(status, obj, headers)

        def _route(self):
            path = urllib.parse.urlsplit(self.path).path
            if rx.killed():
                if path == "/health" and self.command == "GET":
                    return 503, rx.health(), None
                return 503, {"ok": False, "error": "disabled"}, None
            if path == "/health":
                if self.command != "GET":
                    return 405, {"ok": False, "error": "method_not_allowed"}, {"Allow": "GET"}
                return 200, rx.health(), None
            if path != "/alert":
                return 404, {"ok": False, "error": "not_found"}, None
            if self.command != "POST":
                return 405, {"ok": False, "error": "method_not_allowed"}, {"Allow": "POST"}
            return self._alert()

        def _alert(self):
            ip = rx.client_ip(self.client_address[0], self.headers.get("X-Forwarded-For", ""))
            fields = {}
            try:
                rx.admit(ip)
                status, obj, fields = rx.handle_alert(self._read_body())
                rx.record(self._entry(fields, obj["action"], obj.get("reason"), status))
                return status, obj, None
            except Reject as e:
                if e.action == "auth_failed":
                    rx.auth_failed(ip)
                    fields = {}  # never log anything taken from an unauthenticated body
                reason = e.error if e.field is None else f"{e.error}:{e.field}"
                rx.record(self._entry(fields, e.action, reason, e.status), rejected=True)
                obj = {"ok": False, "error": e.error}
                if e.field is not None:
                    obj["field"] = e.field
                return e.status, obj, ({"Retry-After": "60"} if e.status == 429 else None)

        def _entry(self, fields, action, reason, status):
            entry = {"ts": iso_utc(rx.clock()), "symbol": fields.get("symbol"), "tf": fields.get("tf"),
                     "bar_time": fields.get("bar_time"), "action": action, "reason": reason, "status": status}
            if "upstream_status" in fields:
                entry["upstream_status"] = fields["upstream_status"]
            return entry

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _dispatch

    return Handler


def make_server(cfg, host=HOST, port=PORT, receiver=None):
    server = ThreadingHTTPServer((host, port), make_handler(receiver or Receiver(cfg)))
    server.daemon_threads = True
    return server


def main():
    try:
        cfg = load_config(os.environ)
    except ConfigError as e:
        print(f"tv-futures: refusing to start: {e}", file=sys.stderr, flush=True)
        return 2
    server = make_server(cfg, port=int(os.environ.get("TV_PORT", PORT)))
    print(f"tv-futures listening on :{server.server_address[1]} symbols={','.join(cfg['symbols'])} "
          f"tf={','.join(cfg['timeframes'])} trusted_proxies={len(cfg['trusted_proxies'])}",
          file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
