"""Shared scrubber: every string leaving the box goes through scrub_tree(), then find_leaks() must be empty.

Rules (spec "Shared scrubber"):
  a) token-like runs (24+ chars of [A-Za-z0-9_-+/=] with a digit and a letter) -> [REDACTED]
     (dash/slash word slugs such as "narrow-gap-v2-shadow-export" are kept; `head` git SHAs are kept)
  b) Bearer <x> / Authorization: <x>
  c) KEY=VALUE (also KEY: VALUE, "key": "value") where KEY contains KEY|TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE|CREDENTIAL
  d) URLs containing ?, /webhooks/, key=, token=, secret= (or user:pass@) -> whole URL redacted
  e) -----BEGIN ... PRIVATE KEY----- blocks (also unterminated)
  f) 9+ digit runs (chat ids, phone numbers) -> [NUM]; emails are kept
Scrub the full text first, truncate afterwards: truncating first could leave a partial secret too short to match.
"""

import re

RED = "[REDACTED]"
NUM = "[NUM]"
_NOT_RED = r"(?!\[REDACTED\])"

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩]")
PEM_RE = re.compile(r"(?:-----)?BEGIN [A-Z0-9 ]*PRIVATE KEY.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S)
PEM_HINT_RE = re.compile(r"BEGIN [A-Z0-9 ]*PRIVATE KEY")
URL_RE = re.compile(r"(?i)\b(?:https?|wss?|ftp|redis|postgres(?:ql)?|mysql|mongodb(?:\+srv)?|amqp)://[^\s\"'<>`]+")
_BAD_URL = re.compile(r"(?i)\?|/webhooks?/|key=|token=|secret=|://[^/\s]*@")
BEARER_RE = re.compile(r"(?i)\bbearer\s+" + _NOT_RED + r"[^\s\"',;]+")
AUTH_RE = re.compile(
    r"(?i)\b((?:proxy-)?authorization|x-api-key|apca-api-key-id|apca-api-secret-key)(\"?\s*[:=]\s*\"?)"
    r"(?:(?:basic|bearer|digest|token)\s+)?" + _NOT_RED + r"[^\s\"',;]+")
KV_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE|CREDENTIAL)[A-Za-z0-9_.-]*)"
    r"(\"?\s*[=:]\s*[\"']?)" + _NOT_RED + r"([^\s\"',;&}\]]+)")
RUN_RE = re.compile(r"[A-Za-z0-9_\-+/=]{24,}")
ALPACA_ID_RE = re.compile(r"\b[PA]K[A-Z0-9]{16,22}\b")
NUM_RE = re.compile(r"(?<![0-9])-?[0-9]{9,}(?![0-9])")
SHA_RE = re.compile(r"[0-9a-f]{7,12}")
_SEG_OK = re.compile(r"[a-z]{1,12}|[A-Z][a-z]{0,11}|[A-Z]{1,12}|[0-9]{1,8}|[a-z]{1,10}[0-9]{1,2}|[A-Z]{1,10}[0-9]{1,2}")

# keys whose value is allowed to be a short hex SHA (spec: `head` fields)
SHA_KEYS = frozenset({"head"})


def _slug(run):
    """A dash/slash/underscore separated run of plain words ("nrs-crypto-export", "2026/10/26")."""
    if "+" in run or "=" in run:
        return False
    segs = [s for s in re.split(r"[-/_]", run) if s]
    return bool(segs) and len(segs) >= 2 and all(_SEG_OK.fullmatch(s) for s in segs)


def _token_like(run):
    return (any(c.isdigit() for c in run) and any(c.isalpha() for c in run)) and not _slug(run)


def _url_sub(m):
    return RED if _BAD_URL.search(m.group(0)) else m.group(0)


def _run_sub(m):
    return RED if _token_like(m.group(0)) else m.group(0)


def scrub_text(text):
    if not isinstance(text, str):
        return text
    text = _CTRL.sub("", text)
    text = PEM_RE.sub(RED, text)
    text = URL_RE.sub(_url_sub, text)
    text = BEARER_RE.sub("Bearer " + RED, text)
    text = AUTH_RE.sub(lambda m: m.group(1) + m.group(2) + RED, text)
    text = KV_RE.sub(lambda m: m.group(1) + m.group(2) + RED, text)
    text = RUN_RE.sub(_run_sub, text)
    text = ALPACA_ID_RE.sub(RED, text)
    text = NUM_RE.sub(NUM, text)
    return text


def scrub_tree(obj, key=None):
    if isinstance(obj, dict):
        return {scrub_text(str(k)): scrub_tree(v, k) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub_tree(v, key) for v in obj]
    if isinstance(obj, str):
        if key in SHA_KEYS and SHA_RE.fullmatch(obj):
            return obj
        return scrub_text(obj)
    return obj


def text_leaks(text):
    """Names of rules that still match text (empty list = clean)."""
    out = []
    if PEM_HINT_RE.search(text):
        out.append("pem")
    if any(_BAD_URL.search(m.group(0)) for m in URL_RE.finditer(text)):
        out.append("url")
    if BEARER_RE.search(text):
        out.append("bearer")
    if AUTH_RE.search(text):
        out.append("authorization")
    if KV_RE.search(text):
        out.append("key=value")
    if any(_token_like(m.group(0)) for m in RUN_RE.finditer(text)):
        out.append("token")
    if ALPACA_ID_RE.search(text):
        out.append("alpaca-id")
    if NUM_RE.search(text):
        out.append("number")
    return out


def find_leaks(obj, key=None):
    """Defence in depth: rule names still matching anywhere in the tree (keys included)."""
    found = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            found.update(text_leaks(str(k)))
            found.update(find_leaks(v, k))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            found.update(find_leaks(v, key))
    elif isinstance(obj, str):
        if not (key in SHA_KEYS and SHA_RE.fullmatch(obj)):
            found.update(text_leaks(obj))
    return sorted(found)


def clip(text, limit):
    """Scrub, then truncate (never the other way round)."""
    text = scrub_text(text)
    return text if len(text) <= limit else text[:limit]
