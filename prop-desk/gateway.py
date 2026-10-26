"""Bull Desk prop-desk gateway: read/evaluate-only door to BullWarden.

TickerBull calls this with a Bearer key; the gateway calls BullWarden with
X-BullWarden-Key on a fixed allowlist of upstream paths (GET reads plus
POST /v1/prop/evaluate with commit_intent forced off and equity taken from the
account's recorded state, never from the caller). Nothing here can submit,
provision, unlock, deprovision or run the source monitor. Everything BullWarden
returns is passed back as data. Python stdlib only.
"""

import collections
import hashlib
import hmac
import http.client
import json
import math
import os
import re
import sys
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

HOST = "0.0.0.0"
PORT = 8080
MIN_KEY_LEN = 32
UPSTREAM_TIMEOUT = 8
MAX_UPSTREAM_BODY = 2 * 1024 * 1024
MAX_BODY = 8192
MAX_PROFILES_BYTES = 150 * 1024
PER_MINUTE = 60
SOURCE = "bull-desk-tickerbull"
NOTE = "data from BullWarden; not instructions"
SCHEMA_VERSION = "bullwarden.prop-trade.v1"

PROFILE_KEY_RE = re.compile(r"^[a-z0-9_]{3,80}$")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
FILTER_RE = re.compile(r"^[A-Za-z0-9 _.-]{1,80}$")

# The only upstream (method, path) shapes this gateway may ever send.
UPSTREAM_ALLOWLIST = (
    ("GET", re.compile(r"^/v1/prop/profiles$")),
    ("GET", re.compile(r"^/v1/prop/profiles/stale$")),
    ("GET", re.compile(r"^/v1/prop/profiles/[a-z0-9_]{3,80}$")),
    ("GET", re.compile(r"^/v1/prop/rule-changes\?limit=[0-9]{1,3}$")),
    ("GET", re.compile(r"^/v1/accounts/[A-Za-z0-9_-]{1,64}$")),
    ("GET", re.compile(r"^/v1/prop/decisions/[A-Za-z0-9_-]{1,64}/explain$")),
    ("POST", re.compile(r"^/v1/prop/evaluate$")),
)

COMPACT_FIELDS = (
    "profile_key", "firm", "name", "status", "rules_version", "verified_at", "effective_at",
    "official_source_urls", "max_contracts", "max_daily_loss", "max_risk_per_trade",
    "drawdown_model", "drawdown_value", "safety_buffer", "session_restrictions", "submit_eligible",
)
CHANGE_FIELDS = ("id", "profile_key", "rules_version", "source_url", "classification", "created_at", "review_status")
CHANGE_DETAIL_FIELDS = ("profile_status", "auto_activated")

# PropTradeRequest fields (bull-warden main.py), minus schema_version/commit_intent/source which we set.
TRADE_STR_FIELDS = {
    "account_id": 120, "intent_id": 160, "intent_fingerprint": 128, "asset_class": 16, "mode": 16,
    "symbol": 48, "direction": 8, "setup_type": 120, "profile_key": 100, "rules_version": 100,
    "signal_id": 160, "signal_timestamp": 64, "account_observed_at": 64,
}
TRADE_NUM_FIELDS = {"quantity", "entry_price", "stop_price", "target_price", "point_value"}
# TradeFields.existing_mini_equivalent: float, ge=0, finite, default 0 upstream. Caller-supplied, advisory,
# unverified by BullWarden; forwarded unchanged only when sent. The 1000 cap is the gateway's own sanity bound.
EXPOSURE_FIELD = "existing_mini_equivalent"
EXPOSURE_MAX = 1000
FORBIDDEN_FIELD_PARTS = ("order", "submit", "execute", "arm", "commit")
# Equity is never taken from the caller: any field name containing this (any case) -> 422.
# Covers equity, prior_close_equity, peak_equity, day_start_equity, lifetime_peak_equity, last_equity.
EQUITY_FIELD_PART = "equity"
ALLOWED_MODES = ("paper",)
STATE_FUTURE_TOLERANCE_S = 5  # same tolerance BullWarden applies to account_observed_at
ALLOWED_FIELDS = sorted(set(TRADE_STR_FIELDS) | TRADE_NUM_FIELDS | {EXPOSURE_FIELD, "source", "schema_version"})
# Pydantic-required on PropTradeRequest, plus every TradeFields field BullWarden's
# _required_trade_violations turns into a <name>_missing hard violation.
REQUIRED_FIELDS = (
    "account_id", "asset_class", "symbol", "direction", "quantity", "entry_price", "stop_price",
    "target_price", "point_value", "setup_type", "profile_key", "rules_version", "signal_id",
    "signal_timestamp", "account_observed_at",
)
# mode is required upstream but the gateway always sends "paper"; the rest the gateway fills.
OPTIONAL_FIELDS = tuple(f for f in ALLOWED_FIELDS if f not in REQUIRED_FIELDS)
UNKNOWN_FIELD_HINT = ("stop must be sent as stop_price; dollar risk is computed from entry_price, stop_price, "
                      "quantity, point_value; do not send risk, equity, order, submit or commit fields")
EQUITY_HINT = "equity is filled by the gateway"
EVALUATE_EXAMPLE = {
    "account_id": "<account_id>", "asset_class": "futures", "mode": "paper", "symbol": "MES",
    "direction": "long", "quantity": 1, "entry_price": 7706.75, "stop_price": 7696.75,
    "target_price": 7726.75, "point_value": 5, "setup_type": "dry-run-test", "profile_key": "tpt_test_25k",
    "rules_version": "2026-08-22.1", "signal_id": "<unique id>", "signal_timestamp": "<now ISO with timezone>",
    "account_observed_at": "<now ISO with timezone>",
}
EVALUATE_NOTES = (
    "stop is stop_price",
    "point_value is dollars per point (MES 5, ES 50, MNQ 2, NQ 20)",
    "monitor mode returns allowed=true with action MONITOR_BLOCK when a rule fails: treat as NO TRADE",
    "timestamps must include a time zone",
    "existing_mini_equivalent: your open exposure in minis (10 micros = 1 mini); omit if flat; advisory, caller-supplied",
)


class ConfigError(Exception):
    pass


class GatewayError(Exception):
    def __init__(self, status, error, extra=None):
        super().__init__(error)
        self.status = status
        self.error = error
        self.extra = extra or {}


def safe_field_name(name):
    """Caller-supplied field name, reduced to printable ASCII and 40 chars, for echoing back."""
    return "".join(c for c in str(name) if " " <= c <= "~")[:40]


def split_keys(env, name):
    return [k.strip() for k in env.get(name, "").split(",") if k.strip()]


def load_config(env):
    keys = split_keys(env, "PROP_KEYS")
    if any(len(k) < MIN_KEY_LEN for k in keys):
        raise ConfigError(f"every PROP_KEYS entry must be at least {MIN_KEY_LEN} chars")
    url = urllib.parse.urlsplit(env.get("BULLWARDEN_URL", "http://bull-warden:8090").strip())
    if url.scheme not in ("http", "https") or not url.hostname or url.query or url.fragment:
        raise ConfigError("BULLWARDEN_URL must be an http(s) origin")
    return {
        "keys": keys,
        "upstream_scheme": url.scheme,
        "upstream_host": url.hostname,
        "upstream_port": url.port or (443 if url.scheme == "https" else 80),
        "upstream_base": url.path.rstrip("/"),
        "upstream_key": env.get("BULLWARDEN_API_KEY", "").strip(),
        "kill_file": os.path.join(env.get("PROP_KILL_DIR", "/run/prop"), "KILL_PROP"),
        "per_minute": int(env.get("PROP_PER_MINUTE", PER_MINUTE)),
        "allow_enforce_eval": env.get("PROP_ALLOW_ENFORCE_EVAL", "") == "1",
        "timeout": float(env.get("PROP_UPSTREAM_TIMEOUT", UPSTREAM_TIMEOUT)),
    }


class RateLimiter:
    def __init__(self, per_minute):
        self.per_minute = per_minute
        self.hits = collections.defaultdict(collections.deque)
        self.lock = threading.Lock()

    def allow(self, bucket, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            q = self.hits[bucket]
            while q and now - q[0] >= 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            return True


def match_key(keys, header):
    """Index of the matching key, or None. Compares against every key (no early exit)."""
    if not header or not header.startswith("Bearer "):
        return None
    token = header[len("Bearer "):].encode()
    found = None
    for i, key in enumerate(keys):
        if hmac.compare_digest(token, key.encode()) and found is None:
            found = i
    return found


def upstream_allowed(method, path):
    return any(method == m and rx.match(path) for m, rx in UPSTREAM_ALLOWLIST)


class Upstream:
    def __init__(self, cfg):
        self.cfg = cfg

    def call(self, method, path, payload=None):
        if not upstream_allowed(method, path):
            raise GatewayError(500, "upstream_path_not_allowed")
        if not self.cfg["upstream_key"]:
            raise GatewayError(502, "upstream_unavailable")
        cls = http.client.HTTPSConnection if self.cfg["upstream_scheme"] == "https" else http.client.HTTPConnection
        conn = cls(self.cfg["upstream_host"], self.cfg["upstream_port"], timeout=self.cfg["timeout"])
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Accept": "application/json", "X-BullWarden-Key": self.cfg["upstream_key"]}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            # http.client never follows redirects, so the key cannot be bounced to another host.
            conn.request(method, self.cfg["upstream_base"] + path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read(MAX_UPSTREAM_BODY + 1)
            status = resp.status
        except (OSError, http.client.HTTPException):
            raise GatewayError(502, "upstream_unavailable") from None
        finally:
            conn.close()
        if status == 404:
            raise GatewayError(404, "not_found")
        if status == 422:
            raise GatewayError(422, "upstream_rejected_request")
        if status != 200 or len(raw) > MAX_UPSTREAM_BODY:
            raise GatewayError(502, "upstream_error")
        try:
            data = json.loads(raw)
        except ValueError:
            raise GatewayError(502, "upstream_error") from None
        if not isinstance(data, dict):
            raise GatewayError(502, "upstream_error")
        return data


# ── shaping ──

def verification_needed(profile):
    return not (profile.get("status") == "verified" and bool(profile.get("verified_at")))


def compact_profile(profile):
    out = {f: profile.get(f) for f in COMPACT_FIELDS}
    out["verification_needed"] = verification_needed(profile)
    return out


def profile_list(data):
    profiles = data.get("profiles")
    if not isinstance(profiles, list):
        raise GatewayError(502, "upstream_error")
    return [compact_profile(p) for p in profiles if isinstance(p, dict)]


def cap_list(items, limit_bytes):
    """Drop trailing items until the JSON list fits; report truncation."""
    size = len(json.dumps(items))
    truncated = False
    while items and size > limit_bytes:
        size -= len(json.dumps(items[-1])) + 2
        items = items[:-1]
        truncated = True
    return items, truncated


def compact_change(change):
    out = {f: change.get(f) for f in CHANGE_FIELDS}
    detail = change.get("detail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except ValueError:
            detail = None
    detail = detail if isinstance(detail, dict) else {}
    out["detail"] = {f: detail.get(f) for f in CHANGE_DETAIL_FIELDS if f in detail}
    return out


def validate_trade(body):
    """Return the upstream PropTradeRequest payload, or raise GatewayError(422)."""
    if not isinstance(body, dict):
        raise GatewayError(422, "body_must_be_object")
    names = sorted(body, key=str)
    for name in names:
        if EQUITY_FIELD_PART in str(name).lower():
            raise GatewayError(422, "equity_not_accepted", {"field": safe_field_name(name), "hint": EQUITY_HINT})
    for name in names:
        lowered = str(name).lower()
        if any(part in lowered for part in FORBIDDEN_FIELD_PARTS):
            raise GatewayError(422, "forbidden_field", {"field": safe_field_name(name)})
    mode = body.get("mode", "paper")
    if mode not in ALLOWED_MODES:
        raise GatewayError(422, "mode_not_allowed")
    unknown = [name for name in names if name not in ALLOWED_FIELDS]
    if unknown:
        raise GatewayError(422, "unknown_field", {
            "field": safe_field_name(unknown[0]), "allowed_fields": ALLOWED_FIELDS, "hint": UNKNOWN_FIELD_HINT,
        })
    if body.get("source", SOURCE) != SOURCE:
        raise GatewayError(422, "source_not_allowed")
    if body.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise GatewayError(422, "schema_version_not_allowed")
    payload = {}
    for name, limit in TRADE_STR_FIELDS.items():
        if name in body and body[name] is not None:
            if not isinstance(body[name], str) or len(body[name]) > limit:
                raise GatewayError(422, "invalid_field")
            payload[name] = body[name]
    for name in TRADE_NUM_FIELDS:
        if name in body and body[name] is not None:
            value = body[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
                raise GatewayError(422, "invalid_field")
            payload[name] = value
    if EXPOSURE_FIELD in body:
        value = body[EXPOSURE_FIELD]
        # Range before isfinite: a huge JSON int would overflow math.isfinite; NaN fails the range too.
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= EXPOSURE_MAX
                or not math.isfinite(value)):
            raise GatewayError(422, "invalid_field", {"field": EXPOSURE_FIELD})
        payload[EXPOSURE_FIELD] = value
    if not ID_RE.match(payload.get("account_id", "")):
        raise GatewayError(422, "invalid_account_id")
    payload["mode"] = "paper"
    payload["source"] = SOURCE
    payload["schema_version"] = SCHEMA_VERSION
    payload["commit_intent"] = False
    payload.setdefault("intent_id", f"{SOURCE}-eval-{uuid.uuid4()}")
    return payload


def _positive_number(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value > 0)


def recorded_equity(snap, now=None):
    """(equity, prior_close_equity, age_s) from a GET /v1/accounts/<id> snapshot, or raise 409.

    BullWarden's snapshot reports state.equity = round(last_equity, 2) and
    state.day_start_equity = round(day_start_equity, 2) (the session's prior close).
    """
    now = datetime.now(timezone.utc) if now is None else now
    state = snap.get("state")
    profile = snap.get("profile")
    policy = snap.get("policy") if isinstance(snap.get("policy"), dict) else {}
    if snap.get("observed") is not True or not isinstance(state, dict) or not isinstance(profile, dict):
        raise GatewayError(409, "account_state_unavailable")
    equity, prior_close = state.get("equity"), state.get("day_start_equity")
    max_age = profile.get("account_state_max_age_seconds")
    if not (_positive_number(equity) and _positive_number(prior_close) and _positive_number(max_age)):
        raise GatewayError(409, "account_state_unavailable")
    try:
        updated = datetime.fromisoformat(str(state.get("updated_at")).replace("Z", "+00:00"))
        zone = ZoneInfo(str(policy.get("timezone") or "America/New_York"))
    except (ValueError, KeyError, OSError):
        raise GatewayError(409, "account_state_unavailable") from None
    if updated.tzinfo is None:
        raise GatewayError(409, "account_state_unavailable")
    age = (now - updated).total_seconds()
    if age > max_age or age < -STATE_FUTURE_TOLERANCE_S:
        raise GatewayError(409, "account_state_unavailable")
    # A new session would make BullWarden reset day_start/peak from our values; only evaluate within
    # the recorded session so the upsert rewrites exactly what is stored.
    if state.get("session_date") != now.astimezone(zone).date().isoformat():
        raise GatewayError(409, "account_state_unavailable")
    return equity, prior_close, round(max(age, 0.0), 1)


# ── routes ──

class Gateway:
    def __init__(self, cfg, upstream=None):
        self.cfg = cfg
        self.upstream = upstream or Upstream(cfg)
        self.limiter = RateLimiter(cfg["per_minute"])

    def profiles(self, query):
        firm, status = query.get("firm"), query.get("status")
        items = profile_list(self.upstream.call("GET", "/v1/prop/profiles"))
        if firm is not None:
            items = [p for p in items if str(p.get("firm") or "").lower() == firm.lower()]
        if status is not None:
            items = [p for p in items if p.get("status") == status]
        items, truncated = cap_list(items, MAX_PROFILES_BYTES)
        return {"profiles": items, "count": len(items), "truncated": truncated}

    def profile(self, key):
        if key == "stale":  # upstream routes /profiles/stale to the stale list, not a profile
            raise GatewayError(404, "not_found")
        data = self.upstream.call("GET", f"/v1/prop/profiles/{key}")
        return {"profile": data, "verification_needed": verification_needed(data)}

    def stale(self, _query):
        items, truncated = cap_list(profile_list(self.upstream.call("GET", "/v1/prop/profiles/stale")), MAX_PROFILES_BYTES)
        return {"profiles": items, "count": len(items), "truncated": truncated}

    def changes(self, query):
        raw = query.get("limit", "100")
        if not re.fullmatch(r"[0-9]{1,3}", raw) or not 1 <= int(raw) <= 100:
            raise GatewayError(422, "invalid_limit")
        data = self.upstream.call("GET", f"/v1/prop/rule-changes?limit={int(raw)}")
        changes = data.get("changes")
        if not isinstance(changes, list):
            raise GatewayError(502, "upstream_error")
        items = [compact_change(c) for c in changes if isinstance(c, dict)]
        return {"changes": items, "count": len(items)}

    def risk(self, account_id):
        return {"risk": self.upstream.call("GET", f"/v1/accounts/{account_id}")}

    def evaluate_schema(self):
        return {
            "required_fields": list(REQUIRED_FIELDS),
            "optional_fields": list(OPTIONAL_FIELDS),
            "example": dict(EVALUATE_EXAMPLE),
            "notes": list(EVALUATE_NOTES),
        }

    def explain(self, decision_id):
        return {"explain": self.upstream.call("GET", f"/v1/prop/decisions/{decision_id}/explain")}

    def _evaluate(self, body):
        payload = validate_trade(body)
        snap = self.upstream.call("GET", f"/v1/accounts/{payload['account_id']}")
        if not self.cfg["allow_enforce_eval"]:
            # BullWarden LOCKS an enforce-mode account on any soft violation during evaluate.
            policy = snap.get("policy") if isinstance(snap.get("policy"), dict) else {}
            if policy.get("mode") != "monitor":
                raise GatewayError(409, "evaluate_refused_non_monitor_account")
        # Evaluate upserts account_state from these two fields; feed back what is already recorded.
        equity, prior_close, age = recorded_equity(snap)
        payload["equity"] = equity
        payload["prior_close_equity"] = prior_close
        if not payload.get("intent_fingerprint"):
            payload["intent_fingerprint"] = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        result = self.upstream.call("POST", "/v1/prop/evaluate", payload)
        return result, {
            "dry_run": True,
            "commit_intent": False,
            "source": SOURCE,
            "persisted_decision_id": result.get("decision_id"),
            "equity_used": equity,
            "prior_close_equity_used": prior_close,
            "equity_source": "account_state",
            "account_state_age_s": age,
            "upstream_side_effects": ["decision_record_written", "account_state_rewritten_with_recorded_equity"],
        }

    def evaluate(self, body):
        result, meta = self._evaluate(body)
        return {**meta, "decision": result}

    def violations(self, body):
        result, meta = self._evaluate(body)
        return {**meta, "violations": {
            "decision_id": result.get("decision_id"),
            "submit_eligible": result.get("submit_eligible", False),
            "review_action_label": result.get("review_action_label", "Conditions Not Met"),
            "violations": result.get("violations", []),
            "calculated_risk": result.get("calculated_risk"),
        }}


# (method, compiled path, route label, handler name, kind) — the full public surface.
ROUTES = (
    ("GET", re.compile(r"^/health$"), "/health", "health", "none"),
    ("GET", re.compile(r"^/profiles$"), "/profiles", "profiles", "query"),
    ("GET", re.compile(r"^/stale$"), "/stale", "stale", "query"),
    ("GET", re.compile(r"^/changes$"), "/changes", "changes", "query"),
    ("GET", re.compile(r"^/profiles/([a-z0-9_]{3,80})$"), "/profiles/<key>", "profile", "arg"),
    ("GET", re.compile(r"^/risk/([A-Za-z0-9_-]{1,64})$"), "/risk/<id>", "risk", "arg"),
    ("GET", re.compile(r"^/explain/([A-Za-z0-9_-]{1,64})$"), "/explain/<id>", "explain", "arg"),
    ("GET", re.compile(r"^/evaluate/schema$"), "/evaluate/schema", "evaluate_schema", "none"),
    ("POST", re.compile(r"^/evaluate$"), "/evaluate", "evaluate", "body"),
    ("POST", re.compile(r"^/violations$"), "/violations", "violations", "body"),
)
QUERY_PARAMS = {"profiles": {"firm", "status"}, "stale": set(), "changes": {"limit"}}


def find_route(method, path):
    for m, rx, label, name, kind in ROUTES:
        hit = rx.match(path)
        if hit and m == method:
            return label, name, kind, (hit.group(1) if hit.groups() else None)
    return None


def parse_query(name, raw):
    try:
        pairs = urllib.parse.parse_qsl(raw, keep_blank_values=True, strict_parsing=bool(raw), max_num_fields=4)
    except ValueError:
        raise GatewayError(422, "invalid_query") from None
    query = {}
    for k, v in pairs:
        if k not in QUERY_PARAMS[name] or k in query or not FILTER_RE.match(v):
            raise GatewayError(422, "invalid_query")
        query[k] = v
    return query


def make_handler(gw):
    class Handler(BaseHTTPRequestHandler):
        server_version = "prop-desk"
        sys_version = ""
        timeout = 15

        def log_message(self, *_args):  # default log would include the raw request line
            pass

        def _send(self, status, obj):
            body = json.dumps(obj, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _read_body(self):
            if self.headers.get("Transfer-Encoding"):
                raise GatewayError(411, "length_required")
            length = self.headers.get("Content-Length")
            if length is None or not length.isdigit():
                raise GatewayError(411, "length_required")
            if int(length) > MAX_BODY:
                raise GatewayError(413, "body_too_large")
            raw = self.rfile.read(int(length))
            try:
                return json.loads(raw)
            except ValueError:
                raise GatewayError(422, "invalid_json") from None

        def _dispatch(self):
            start = time.monotonic()
            self._label = "-"
            try:
                status, obj = self._route()
            except GatewayError as e:
                status, obj = e.status, {"ok": False, "error": e.error, **e.extra}
            except Exception as e:  # never leak details
                print(f"prop-desk internal error {type(e).__name__}", file=sys.stderr, flush=True)
                status, obj = 500, {"ok": False, "error": "internal_error"}
            if status >= 400 and status not in (401, 404, 429, 503):
                self.close_connection = True
            ms = int((time.monotonic() - start) * 1000)
            print(f"prop-desk {self.command} {self._label} {status} {ms}ms", file=sys.stderr, flush=True)
            self._send(status, obj)

        def _route(self):
            if not gw.cfg["keys"]:
                raise GatewayError(404, "not_found")
            # BaseHTTPRequestHandler rewrites a leading "//" in self.path; check the raw target too.
            raw_target = self.requestline.split(" ")[1] if self.requestline.count(" ") >= 2 else ""
            if raw_target.split("?", 1)[0] != self.path.split("?", 1)[0]:
                raise GatewayError(404, "not_found")
            path, _, raw_query = self.path.partition("?")
            route = find_route(self.command, path)
            if route is None:
                raise GatewayError(404, "not_found")
            label, name, kind, arg = route
            self._label = label
            if name == "health":
                return 200, {"ok": True}
            idx = match_key(gw.cfg["keys"], self.headers.get("Authorization"))
            if idx is None:
                raise GatewayError(401, "unauthorized")
            if os.path.exists(gw.cfg["kill_file"]):
                raise GatewayError(503, "disabled")
            if not gw.limiter.allow(idx):
                raise GatewayError(429, "rate_limited")
            if kind != "query" and raw_query:
                raise GatewayError(422, "invalid_query")
            if kind == "query":
                result = getattr(gw, name)(parse_query(name, raw_query))
            elif kind == "none":
                result = getattr(gw, name)()
            elif kind == "arg":
                result = getattr(gw, name)(arg)
            else:
                result = getattr(gw, name)(self._read_body())
            return 200, {"ok": True, "note": NOTE, **result}

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_TRACE = do_CONNECT = _dispatch

    return Handler


def make_server(cfg, host=HOST, port=PORT, upstream=None):
    return ThreadingHTTPServer((host, port), make_handler(Gateway(cfg, upstream)))


def main():
    try:
        cfg = load_config(os.environ)
    except (ConfigError, ValueError) as e:
        print(f"prop-desk config error: {e}", file=sys.stderr, flush=True)
        sys.exit(2)
    if not cfg["keys"]:
        print("prop-desk: PROP_KEYS unset; every route returns 404", file=sys.stderr, flush=True)
    server = make_server(cfg, port=int(os.environ.get("PROP_PORT", PORT)))
    print(f"prop-desk listening on :{server.server_address[1]}", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
