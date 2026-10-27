"""Fake box for tests: temp-dir filesystem, fake subprocess.run, fake urllib opener. No network, no docker.

Real deps.Runner / deps.HttpGet / deps.Reader are used on top of these fakes so the allowlist,
GET-only and path rules are exercised for real.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import deps as d  # noqa: E402

NOW = 1793026800.0  # fixed clock: 2026-10-26T15:00:00Z

# ── planted fake secrets (none of these may appear in any output) ──────────
ALPACA_KID = "PKFAKE1234567890ABCD"
ALPACA_SEC = "fakeSecret0123456789abcdefGHIJKLmnopQRSTuv"
ALPACA_LIVE_KID = "AKLIVE0987654321WXYZ"
ALPACA_LIVE_SEC = "liveSecret9876543210zyxwvuTSRQPonmlkJIHG"
ALPACA2_KID = "PKSECOND111222333444"
ALPACA2_SEC = "second0Secret1Value2For3Paper4Acct5xyzAB"
OANDA_KEY = "0123456789abcdef0123456789abcdef-fedcba9876543210fedcba9876543210"
OANDA_ACCT = "101-004-7654321-001"
BEARER_TOK = "sk_FAKEabc123def456ghi789jkl"
WEBHOOK_KEY = "hookKey55aa66bb"
URL_SECRET = f"https://hooks.example.com/webhooks/abc/xyz?key={WEBHOOK_KEY}"
PEM_BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7fakefakefake"
PEM = f"-----BEGIN RSA PRIVATE KEY-----\n{PEM_BODY}\nZm9vYmFyYmF6cXV4\n-----END RSA PRIVATE KEY-----"
CHAT_ID = "-1009876543210"
PHONE = "+15557654321"
LOG_TOKEN = "abcDEF123456ghiJKL789mnoPQR"
TG_TOKEN = "7123456789:AAFakeTelegramTokenValueXYZ0123456789"
DB_PASSWORD = "hunter2fake"
BASIC = "dXNlcjpwYXNzd29yZGZha2U="

SECRETS = [ALPACA_KID, ALPACA_SEC, ALPACA_LIVE_KID, ALPACA_LIVE_SEC, ALPACA2_KID, ALPACA2_SEC, OANDA_KEY,
           OANDA_ACCT, BEARER_TOK, WEBHOOK_KEY, URL_SECRET, PEM_BODY, "Zm9vYmFyYmF6cXV4", "PRIVATE KEY",
           CHAT_ID, "9876543210", "5557654321", LOG_TOKEN, TG_TOKEN, "AAFakeTelegramTokenValueXYZ0123456789",
           DB_PASSWORD, BASIC]

SECRET_LINES = [
    f"TELEGRAM_BOT_TOKEN={TG_TOKEN}",
    f"DB_PASSWORD={DB_PASSWORD}",
    f"curl -H 'Authorization: Bearer {BEARER_TOK}' https://api.example.com/x",
    f"Authorization: Basic {BASIC}",
    f"posting to {URL_SECRET} now",
    f"sent to chat {CHAT_ID} and phone {PHONE}",
    f"retry with {LOG_TOKEN} later",
    *PEM.splitlines(),
    "plain line: contact tradingbull@example.com about nrs-crypto-export",
]


def write(root, path, text, mode=0o644, mtime=None):
    p = os.path.join(root, path.lstrip("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)
    os.chmod(p, mode)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def make_box(root, oanda_base="https://api-fxpractice.oanda.com", alpaca_base="https://paper-api.alpaca.markets"):
    w = lambda path, text, **kw: write(root, path, text, **kw)  # noqa: E731
    w("/proc/loadavg", "0.52 0.48 0.40 1/234 5678\n")
    w("/proc/meminfo", "MemTotal: 8000000 kB\nMemFree: 1000000 kB\nMemAvailable: 6000000 kB\n"
                       "SwapTotal: 2000000 kB\nSwapFree: 1500000 kB\n")
    w("/proc/uptime", "123456.78 400000.00\n")
    log_body = "\n".join(["start run"] + SECRET_LINES + [f"last line {LOG_TOKEN} {CHAT_ID}"]) + "\n"
    for i, name in enumerate(("stock-alerts", "forex-alerts", "grokbot-push", "nrs-crypto-export")):
        w(f"/opt/logs/tba/{name}.log", log_body, mtime=NOW - 60 * (i + 1))
    w("/outside/secret.log", f"OUTSIDE {LOG_TOKEN}\n", mtime=NOW)
    os.symlink(os.path.join(root, "outside/secret.log"), os.path.join(root, "opt/logs/tba/escape.log"))
    fires = [
        {"ts": "2026-10-26T13:00:00Z", "kind": "stock", "symbol": "AAPL", "setup": "nr7 breakout", "price": 231.5,
         "late": False, "nr7_unconfirmed": False, "lv": None},
        {"ts": NOW - 120, "kind": "crypto", "symbol": "BTC-USD", "setup": f"x {LOG_TOKEN}", "price": "67234.56"},
        {"ts": "2026-10-26T14:00:00Z", "kind": "forex", "symbol": "EURUSD=X", "setup": "gap", "price": 1.08765,
         "source": "nrs"},
        {"ts": "2026-10-26T14:00:00Z", "kind": "bogus", "symbol": "X"},
        "not a dict",
    ]
    w("/opt/stacks/ops-status/scanner-fires-latest.jsonl",
      "\n".join(json.dumps(f) for f in fires) + "\nnot json\n", mtime=NOW - 30)
    w("/opt/stacks/velez-trading-bot/.env",
      f"# paper\nAPCA_API_KEY_ID={ALPACA_KID}\nAPCA_API_SECRET_KEY='{ALPACA_SEC}'\n"
      f"APCA_API_BASE_URL={alpaca_base}\nTELEGRAM_CHAT_ID={CHAT_ID}\n", mode=0o600)
    w("/opt/stacks/bull-live/.env",
      f"ALPACA_API_KEY={ALPACA_LIVE_KID}\nALPACA_SECRET_KEY={ALPACA_LIVE_SEC}\n"
      f"ALPACA_BASE_URL=https://api.alpaca.markets\n", mode=0o600)
    w("/opt/stacks/bull-pilot/.env", f'export ALPACA_API_KEY_ID="{ALPACA2_KID}"\nALPACA_API_SECRET_KEY={ALPACA2_SEC}\n',
      mode=0o600)
    w("/opt/stacks/oanda-bridge/.env",
      f"OANDA_API_KEY={OANDA_KEY}\nOANDA_ACCOUNT_ID={OANDA_ACCT}\nOANDA_BASE_URL={oanda_base}\n", mode=0o600)
    w("/opt/stacks/tba-brain/Home.md", f"# Home {LOG_TOKEN}\n\nWelcome. Bearer {BEARER_TOK} and {URL_SECRET}\n",
      mtime=NOW - 10)
    w("/opt/stacks/tba-brain/notes/plan.md", "---\ntags: x\n---\n# Plan\n" + "\n".join(SECRET_LINES) + "\n",
      mtime=NOW - 20)
    w("/opt/stacks/tba-brain/notes/untitled.md", "no heading here, chat " + CHAT_ID + "\n", mtime=NOW - 30)
    w("/opt/stacks/tba-brain/.obsidian/hidden.md", "# hidden\n")
    w("/opt/stacks/tba-brain/secrets/keys.md", "# keys\n")
    w("/opt/stacks/tba-brain/my-password-notes.md", "# pw\n")
    w("/opt/stacks/tba-brain/big.md", "# big\n" + "x" * (310 * 1024))
    w("/opt/stacks/tba-brain/readme.txt", "not markdown\n")
    make_narrow_db(os.path.join(root, NARROW_DB.lstrip("/")))
    for repo in ("/opt/scripts/tba", "/opt/stacks/bull-desk", "/root/rakazo"):
        os.makedirs(os.path.join(root, repo.lstrip("/"), ".git"), exist_ok=True)
    os.makedirs(os.path.join(root, "opt/stacks/not-a-repo"), exist_ok=True)
    return root


# ── fake Bull Pilot narrow-gap lane db (schema copied from bot/shadow/narrow_gap_long_v2.py) ──

NARROW_DB = "/opt/stacks/bull-pilot/data/shadow-v2/long_narrow_gap_orb_r2_v2.sqlite3"
NARROW_SCHEMA = (
    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        setup_id TEXT NOT NULL, symbol TEXT NOT NULL, session_date TEXT NOT NULL,
        detected_at TEXT NOT NULL, breakout_time TEXT NOT NULL, entry_time TEXT NOT NULL,
        raw_entry REAL NOT NULL, opening_high REAL NOT NULL, opening_low REAL NOT NULL,
        gap_pct REAL NOT NULL, opening_range_atr REAL NOT NULL, relative_volume REAL NOT NULL,
        atr14 REAL NOT NULL, sma20_5m REAL, atr14_5m REAL, extension_atr REAL,
        extension_side TEXT, guardrail_status TEXT NOT NULL, guardrail_reason TEXT,
        stop_price REAL NOT NULL, quantity INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'open', exit_time TEXT, exit_reason TEXT, raw_exit REAL,
        normal_net_pnl REAL, normal_net_r REAL, harsh_net_pnl REAL, harsh_net_r REAL,
        UNIQUE(setup_id, symbol, session_date))""",
    """CREATE TABLE blocked (
        setup_id TEXT NOT NULL, symbol TEXT NOT NULL, session_date TEXT NOT NULL,
        entry_time TEXT NOT NULL, raw_entry REAL NOT NULL, extension_atr REAL,
        reason TEXT NOT NULL, UNIQUE(setup_id, symbol, session_date))""",
    """CREATE TABLE heartbeats (
        generated_at TEXT PRIMARY KEY, session_date TEXT NOT NULL, state TEXT NOT NULL,
        market_regime TEXT, eligible_symbols INTEGER NOT NULL DEFAULT 0,
        new_signals INTEGER NOT NULL DEFAULT 0, new_blocked INTEGER NOT NULL DEFAULT 0,
        total_signals INTEGER NOT NULL DEFAULT 0, total_closed INTEGER NOT NULL DEFAULT 0,
        error TEXT)""",
)
SETUP = "opening_gap_narrow_orb_r2"


def narrow_signal(symbol, session_date, hhmm="09:41", status="open", reason=None, **kw):
    row = {"setup_id": SETUP, "symbol": symbol, "session_date": session_date,
           "detected_at": f"{session_date}T{int(hhmm[:2]) + 4:02d}:{hhmm[3:]}:30.123456+00:00",
           "breakout_time": f"{session_date}T09:40:00-04:00", "entry_time": f"{session_date}T{hhmm}:00-04:00",
           "raw_entry": 101.5, "opening_high": 101.2, "opening_low": 100.4, "gap_pct": 1.8,
           "opening_range_atr": 0.15, "relative_volume": 1.7, "atr14": 2.5, "sma20_5m": 100.0, "atr14_5m": 0.6,
           "extension_atr": 2.4, "extension_side": "above", "guardrail_status": "caution",
           "guardrail_reason": reason, "stop_price": 101.25, "quantity": 400, "status": status,
           "normal_net_pnl": 12345.0}
    row.update(kw)
    return row


def default_narrow_rows():
    signals = [narrow_signal("AAPL", "2026-10-20", status="closed", exit_time="2026-10-20T15:55:00-04:00",
                             exit_reason="eod", raw_exit=103.0, normal_net_r=1.4, harsh_net_r=1.2),
               narrow_signal("AAPL", "2026-10-26", reason=f"note {LOG_TOKEN}"),
               narrow_signal("MSFT", "2026-10-23", hhmm="09:45", guardrail_status="ok"),
               narrow_signal("NVDA", "2026-10-01")]
    blocked = [{"setup_id": SETUP, "symbol": "TSLA", "session_date": "2026-10-26",
                "entry_time": "2026-10-26T09:50:00-04:00", "raw_entry": 250.0, "extension_atr": 3.4,
                "reason": f"three-finger block Bearer {BEARER_TOK}"}]
    heartbeats = [{"generated_at": "2026-10-26T14:58:45.124343+00:00", "session_date": "2026-10-26",
                   "state": "collecting", "market_regime": "no_market_filter", "eligible_symbols": 7,
                   "new_signals": 1, "new_blocked": 1, "total_signals": 4, "total_closed": 1,
                   "error": f"retry {URL_SECRET} chat {CHAT_ID}"},
                  {"generated_at": "2026-10-23T20:00:00+00:00", "session_date": "2026-10-23",
                   "state": "outside_collection_window", "market_regime": None}]
    return signals, blocked, heartbeats


def make_narrow_db(path, signals=None, blocked=None, heartbeats=None, schema=NARROW_SCHEMA):
    """WAL-mode lane db like Bull Pilot's. Rows default to default_narrow_rows()."""
    import sqlite3
    d_sig, d_blk, d_hb = default_narrow_rows()
    signals = d_sig if signals is None else signals
    blocked = d_blk if blocked is None else blocked
    heartbeats = d_hb if heartbeats is None else heartbeats
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    for stmt in schema:
        con.execute(stmt)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "metadata" in tables:
        con.execute("INSERT INTO metadata VALUES ('execution_mode','signal_only_no_broker'),"
                    f"('manifest_sha256','{'ab' * 32}')")
    for table, rows in (("signals", signals), ("blocked", blocked), ("heartbeats", heartbeats)):
        for row in rows if table in tables else ():
            cols = ",".join(row)
            con.execute(f"INSERT INTO {table} ({cols}) VALUES ({','.join('?' * len(row))})", list(row.values()))
    con.commit()
    con.close()
    return path


# ── fake subprocess.run ────────────────────────────────────────────────────

DOCKER_NAMES = ["bull-desk-api-1", "tba-worker", "velez-bot", "postgres", "oanda-bridge"]


def _git_out(argv):
    if argv == d.GIT_BRANCH:
        return "main\n"
    if argv == d.GIT_HEAD:
        return "79d8fa07abcd\n"
    if argv == d.GIT_LOG:
        return f"{int(NOW) - 3600}\x00fix: rotate key {LOG_TOKEN} for chat {CHAT_ID}\n"
    if argv == d.GIT_STATUS:
        return " M app.py\n?? notes/new.md\nR  old.txt -> new.txt\n" + f"?? leak-{LOG_TOKEN}.txt\n"
    if argv == d.GIT_AHEAD:
        return "2\t5\n"
    raise AssertionError(argv)


def _canned(argv):
    argv = tuple(argv)
    if argv[0] == "git":
        return _git_out(argv), ""
    if argv == d.DOCKER_PS:
        return "\n".join(DOCKER_NAMES) + "\n", ""
    if argv[:len(d.DOCKER_INSPECT)] == d.DOCKER_INSPECT:
        rows = []
        for n in argv[len(d.DOCKER_INSPECT):]:
            health = "healthy" if n.startswith("bull") else ""
            image = f"registry.example.com/{n}:latest" if n != "postgres" else f"https://x.example.com/img?token={WEBHOOK_KEY}"
            rows.append(f"/{n}\t{image}\trunning\t{health}\t2026-10-25T10:00:00.123456789Z\t3")
        return "\n".join(rows) + "\n", ""
    if argv[:len(d.DOCKER_LOGS)] == d.DOCKER_LOGS:
        return "\n".join(SECRET_LINES) + "\n", f"stderr {BEARER_TOK}\n"
    if argv[:len(d.SYSTEMCTL_SHOW)] == d.SYSTEMCTL_SHOW:
        return ("Id=hermes-gateway.service\nLoadState=loaded\nActiveState=active\nSubState=running\n\n"
                "Id=hermes-robinhood-bridge.service\nLoadState=not-found\nActiveState=inactive\nSubState=dead\n\n"
                "Id=docker.service\nLoadState=loaded\nActiveState=active\nSubState=running\n\n"
                "Id=tba-fires.timer\nLoadState=loaded\nActiveState=active\nSubState=waiting\n\n"
                "Id=bull-ghost.service\nLoadState=not-found\nActiveState=inactive\nSubState=dead\n"), ""
    if argv == d.DF:
        return ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                "/dev/sda1 100000 42000 58000 42% /\n/dev/sdb1 100 99 1 99% /mnt/my data\n"), ""
    raise AssertionError(f"unexpected argv {argv}")


class FakeRun:
    """Stands in for subprocess.run; records every argv."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail or set()

    def __call__(self, argv, **kw):
        assert kw.get("shell") is False, "shell must be False"
        self.calls.append(tuple(argv))
        if argv[0] in self.fail:
            return subprocess.CompletedProcess(argv, 1, b"", b"boom")
        out, err = _canned(argv)
        return subprocess.CompletedProcess(argv, 0, out.encode(), err.encode())


# ── fake urllib opener ─────────────────────────────────────────────────────

class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def alpaca_payload(path, kid):
    if path == "/v2/account":
        return {"account_number": "PA3FAKE99999", "equity": "100123.45", "cash": "50000", "buying_power": "200000",
                "id": ALPACA_KID}
    if path == "/v2/positions":
        return [{"symbol": "AAPL", "qty": "10", "avg_entry_price": "230.1", "current_price": "231.5",
                 "unrealized_pl": "14.0", "asset_id": "904837e3-3b76-47ec-b432-046db621571b"}]
    if path == "/v2/account/activities/FILL":
        return [{"transaction_time": "2026-10-26T14:31:00Z", "symbol": "AAPL", "side": "buy", "qty": "10",
                 "price": "230.1", "order_id": "61e69015-8549-4bfd-b9c3-01e75843f47d"},
                {"transaction_time": "2026-10-26T14:32:00Z", "symbol": f"Bearer {BEARER_TOK}", "side": "sell",
                 "qty": "1", "price": "1"}]
    if path == "/v2/orders":
        return [{"symbol": "AAPL", "side": "sell", "qty": "10", "type": "stop", "stop_price": "225.0",
                 "limit_price": None}]
    raise AssertionError(path)


def oanda_payload(path):
    if path.endswith("/summary"):
        return {"account": {"id": OANDA_ACCT, "currency": "USD", "balance": "100000.0", "NAV": "100050.5",
                            "lastTransactionID": "45"}}
    if path.endswith("/openTrades"):
        return {"trades": [{"instrument": "EUR_USD", "currentUnits": "1000", "price": "1.08765",
                            "unrealizedPL": "12.3", "stopLossOrder": {"price": "1.0800"}}]}
    if path.endswith("/pendingOrders"):
        return {"orders": [{"instrument": "GBP_USD", "units": "-500", "type": "LIMIT", "price": "1.3000"}]}
    if path.endswith("/transactions/idrange"):
        return {"transactions": [{"time": "2026-10-26T12:00:00.123456789Z", "type": "ORDER_FILL", "instrument": "EUR_USD",
                                  "units": "1000", "pl": "0.0", "accountID": OANDA_ACCT} for _ in range(25)]}
    raise AssertionError(path)


class FakeOpener:
    """Stands in for the urllib opener; records (method, url, headers)."""

    def __init__(self, fail_substr=()):
        self.calls = []
        self.fail_substr = tuple(fail_substr)

    def open(self, req, timeout=None):
        import urllib.parse
        self.calls.append((req.get_method(), req.full_url, dict(req.header_items()), req.data))
        url = req.full_url
        for s in self.fail_substr:
            if s in url or s in str(req.header_items()):
                raise urllib.error.HTTPError(url, 500, "fail", {}, io.BytesIO(b""))
        parts = urllib.parse.urlsplit(url)
        if parts.hostname == "paper-api.alpaca.markets":
            body = alpaca_payload(parts.path, req.get_header("Apca-api-key-id"))
        elif parts.hostname == "api-fxpractice.oanda.com":
            body = oanda_payload(parts.path)
        else:
            raise AssertionError(f"unexpected host {url}")
        return _Resp(json.dumps(body).encode())


# ── assembled deps ─────────────────────────────────────────────────────────

PUSH_STATUS_TEXT = (
    "credentials: ok   kill switch: off   day: 2026-10-26\n"
    "BULLFX      configured=yes  last_success=2026-10-26T13:00:00+00:00  consec_fail=0  sent_today=3"
    "  retries=0  dead=1\n"
    "TICKERBULL  configured=no  last_success=-  consec_fail=2  sent_today=0  retries=1  dead=0\n"
    f"junk {URL_SECRET} configured=yes\n"
    "bulldesk credentials: ok   kill switch: off   inbox: 0\n"
    "BD_BULLFX    configured=yes  last_success=-  consec_fail=0  sent_today=5  retries=0  dead=0\n"
)


class Box:
    def __init__(self, **box_kw):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_box(os.path.join(self.tmp.name, "box"), **box_kw)
        self.out = os.path.join(self.tmp.name, "observe")
        self.run = FakeRun()
        self.opener = FakeOpener()
        self.deadline = d.Deadline(60)
        self.push_text = PUSH_STATUS_TEXT

    def deps(self, **over):
        kw = dict(runner=d.Runner(self.deadline, run=self.run),
                  http_get=d.HttpGet(self.deadline, opener=self.opener),
                  reader=d.Reader(self.root), deadline=self.deadline, now=lambda: NOW,
                  push_status=lambda: self.push_text)
        kw.update(over)
        return d.Deps(**kw)

    def outputs(self):
        res = {}
        for n in sorted(os.listdir(self.out)):
            if n.endswith(".json"):
                with open(os.path.join(self.out, n), encoding="utf-8") as f:
                    res[n] = f.read()
        return res

    def close(self):
        self.tmp.cleanup()


def silent(*a, **k):
    pass

