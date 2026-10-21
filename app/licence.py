"""CourseCraft licensing - the pure part (no database, no web framework), so it can be tested alone.

How it works, in short:
- TIGARSEC signs each licence with a private Ed25519 key that never leaves TIGARSEC.
  CourseCraft only holds the PUBLIC key (app/licence_keys.py), so it can check a licence
  but can never make one. Changing a single character of a licence breaks its signature.
- A licence file looks like:
      (optional human-readable lines)
      -----BEGIN COURSECRAFT LICENCE-----
      <base64url of the JSON payload>.<base64url of the signature>
      -----END COURSECRAFT LICENCE-----
- When a licence ends, CourseCraft winds down in stages and NEVER deletes data:
      licensed -> grace -> read_only -> export_only
  A copy with no licence runs as a 30-day evaluation, then follows the same path.
- Only TIGARSEC can see or change licensing: the console (/vendor) needs a one-time answer
  signed with the same private key (vendor_response). College admins only ever see a short
  "ends on ..., contact TIGARSEC" notice.

The issuing tool lives in TIGARSEC's separate private repo (tigarsec-licensing), never here.
"""
import base64
import datetime
import json
import re

FORMAT_VERSION = 1
BEGIN = "-----BEGIN COURSECRAFT LICENCE-----"
END = "-----END COURSECRAFT LICENCE-----"
VENDOR_PREFIX = "CCVENDOR."

EDITIONS = ("lecturer", "department", "campus", "college")
TYPES = ("paid", "pilot", "founding", "internal")
EVALUATION_DAYS = 30
DEFAULT_GRACE_DAYS = 30
DEFAULT_READONLY_DAYS = 60

STAGE_LABELS = {
    "evaluation": "Evaluation",
    "licensed": "Licensed",
    "grace": "Grace period",
    "read_only": "Read-only",
    "export_only": "Export-only",
}
FULL_USE_STAGES = ("evaluation", "licensed", "grace")

REQUIRED_FIELDS = ("v", "kid", "id", "licensee", "edition", "seats", "valid_from", "valid_until")


class LicenceError(ValueError):
    """A licence (or vendor answer) that can't be trusted. The message is safe to show."""


# ------------------------------------------------------------------ encoding
def b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _public_key(b64: str):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    raw = base64.b64decode(b64)
    if len(raw) != 32:
        raise LicenceError("A trusted public key is the wrong length - check app/licence_keys.py.")
    return Ed25519PublicKey.from_public_bytes(raw)


def _verify(keys: dict, kid: str, payload_bytes: bytes, signature: bytes):
    from cryptography.exceptions import InvalidSignature
    if not keys:
        raise LicenceError("Licensing isn't set up in this build (no TIGARSEC public key in app/licence_keys.py).")
    if kid not in keys:
        raise LicenceError(f"This was signed with key '{kid}', which this version of CourseCraft doesn't know.")
    try:
        _public_key(keys[kid]).verify(signature, payload_bytes)
    except InvalidSignature:
        raise LicenceError("The signature doesn't match - this licence was changed or wasn't issued by TIGARSEC.")


def _parse_date(value, field):
    try:
        return datetime.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise LicenceError(f"The licence's '{field}' date is not valid.")


# ------------------------------------------------------------------ licences
def extract_token(text: str) -> str:
    text = (text or "").replace("\r", "")
    m = re.search(re.escape(BEGIN) + r"\s*([A-Za-z0-9_\-.\s]+?)\s*" + re.escape(END), text)
    token = m.group(1) if m else text
    token = "".join(token.split())
    if token.count(".") != 1:
        raise LicenceError("That doesn't look like a CourseCraft licence file.")
    return token


def verify_licence(text: str, keys: dict) -> dict:
    """-> the licence payload (dict) with dates as date objects under '_from'/'_until'.
    Raises LicenceError with a plain-English reason."""
    token = extract_token(text)
    body, sig = token.split(".")
    try:
        payload_bytes, signature = b64u_decode(body), b64u_decode(sig)
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        raise LicenceError("That licence file is damaged (it can't be decoded).")
    if not isinstance(payload, dict):
        raise LicenceError("That licence file is damaged.")
    missing = [f for f in REQUIRED_FIELDS if f not in payload]
    if missing:
        raise LicenceError(f"That licence is missing: {', '.join(missing)}.")
    if payload.get("purpose"):
        raise LicenceError("That is a console sign-in answer, not a licence.")
    if payload["v"] != FORMAT_VERSION:
        raise LicenceError("That licence is a newer format than this version of CourseCraft understands - update CourseCraft.")
    _verify(keys, str(payload["kid"]), payload_bytes, signature)
    lic = dict(payload)
    lic["_from"] = _parse_date(payload["valid_from"], "valid_from")
    lic["_until"] = _parse_date(payload["valid_until"], "valid_until")
    if lic["_until"] < lic["_from"]:
        raise LicenceError("That licence ends before it starts.")
    lic["grace_days"] = int(payload.get("grace_days", DEFAULT_GRACE_DAYS))
    lic["readonly_days"] = int(payload.get("readonly_days", DEFAULT_READONLY_DAYS))
    lic["type"] = payload.get("type", "paid")
    return lic


def choose_licence(licences: list, today: datetime.date):
    """From every verified licence installed, the one in force today: among those that have
    started, the one that runs longest. None if none has started yet."""
    started = [l for l in licences if l["_from"] <= today]
    if not started:
        return None
    return max(started, key=lambda l: (l["_until"], l["_from"]))


def compute_state(licence, today: datetime.date, first_started: datetime.date) -> dict:
    """Where this copy stands today.
    -> {"stage", "label", "licence" (payload or None), "ends", "grace_until", "readonly_until",
        "days_left" (to the end date; negative after it), "next_stage", "next_change" (date)}"""
    if licence is None:
        ends = first_started + datetime.timedelta(days=EVALUATION_DAYS)
        grace, readonly = 0, DEFAULT_READONLY_DAYS
        in_force = "evaluation"
    else:
        ends = licence["_until"]
        grace, readonly = licence["grace_days"], licence["readonly_days"]
        in_force = "licensed"
    grace_until = ends + datetime.timedelta(days=grace)
    readonly_until = grace_until + datetime.timedelta(days=readonly)
    if today <= ends:
        stage, next_stage, next_change = in_force, ("grace" if grace else "read_only"), ends + datetime.timedelta(days=1)
    elif today <= grace_until:
        stage, next_stage, next_change = "grace", "read_only", grace_until + datetime.timedelta(days=1)
    elif today <= readonly_until:
        stage, next_stage, next_change = "read_only", "export_only", readonly_until + datetime.timedelta(days=1)
    else:
        stage, next_stage, next_change = "export_only", None, None
    return {
        "stage": stage,
        "label": STAGE_LABELS[stage],
        "licence": licence,
        "ends": ends,
        "grace_until": grace_until,
        "readonly_until": readonly_until,
        "days_left": (ends - today).days,
        "next_stage": next_stage,
        "next_change": next_change,
    }


# ------------------------------------------------------------------ who sees which notice
def banner_for(state: dict, role: str, contact: str):
    """The short notice shown at the top of staff pages, or None. Deliberately no licence
    details - only TIGARSEC sees those. -> {"level": "info"|"warning"|"danger", "text"}"""
    stage, days, ends = state["stage"], state["days_left"], state["ends"]
    is_admin = role == "admin"
    when = ends.strftime("%-d %b %Y")
    renew = f"Contact TIGARSEC to renew: {contact}"
    if stage == "evaluation":
        if is_admin or days <= 7:
            return {"level": "info" if days > 7 else "warning",
                    "text": f"This is an evaluation copy of CourseCraft ({max(days, 0)} day{'s' if days != 1 else ''} left). {renew}"}
        return None
    if stage == "licensed":
        if (is_admin and days <= 60) or days <= 14:
            return {"level": "info" if days > 14 else "warning",
                    "text": f"This CourseCraft licence ends on {when} ({days} day{'s' if days != 1 else ''}). {renew}"}
        return None
    if stage == "grace":
        return {"level": "warning",
                "text": f"This CourseCraft licence ended on {when}. Everything still works until "
                        f"{state['grace_until'].strftime('%-d %b %Y')}, then CourseCraft becomes read-only. {renew}"}
    if stage == "read_only":
        return {"level": "danger",
                "text": "CourseCraft is read-only: you can view, download and export, but not create, mark or "
                        f"generate. From {state['readonly_until'].strftime('%-d %b %Y')} only admins can sign in, "
                        f"to export data. {renew}"}
    return None  # export_only has its own page


def reminder_due(state: dict, already_sent: set):
    """The admin email reminder to send now, or None. -> (key, subject, body_intro)
    Keys are unique per licence and step so each email goes once."""
    lic = state["licence"]
    lid = lic["id"] if lic else "evaluation"
    stage, days = state["stage"], state["days_left"]
    candidates = []
    if stage in ("licensed", "evaluation"):
        for step in (60, 30, 7):
            if days <= step:
                candidates.append((f"{lid}:{step}", f"CourseCraft licence ends in {days} days"))
    if stage == "grace":
        candidates.append((f"{lid}:grace", "CourseCraft licence has ended - grace period started"))
    if stage == "read_only":
        candidates.append((f"{lid}:read_only", "CourseCraft is now read-only"))
    if stage == "export_only":
        candidates.append((f"{lid}:export_only", "CourseCraft is now export-only"))
    for key, subject in candidates[-1:]:  # only the most urgent step, never a burst of old ones
        if key not in already_sent:
            return key, subject
    return None


# ------------------------------------------------------------------ what each stage allows
ALWAYS_ALLOWED = ("/static/", "/vendor", "/login", "/logout", "/favicon.ico")
STUDENT_PREFIXES = ("/join", "/live/s/", "/hw/")
READONLY_POST_ALLOWED = ("/login", "/settings/change-password", "/admin/export")
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _starts(path, prefixes):
    """'/vendor' matches /vendor and /vendor/... but not /vendors; '/static/' matches anything under it."""
    for p in prefixes:
        if p.endswith("/"):
            if path.startswith(p):
                return True
        elif path == p or path.startswith(p + "/"):
            return True
    return False


def decide(stage: str, method: str, path: str, role=None):
    """None = let the request through. Otherwise (kind, http_status) where kind is
    "readonly" (staff tried to change something), "student_ended" (a student page) or
    "ended" (export-only: everyone but admins exporting) or "to_export" (admin: send them to
    the export page)."""
    method = method.upper()
    if stage in FULL_USE_STAGES or _starts(path, ALWAYS_ALLOWED):
        return None
    student = _starts(path, STUDENT_PREFIXES)
    if stage == "read_only":
        if method in SAFE_METHODS or _starts(path, READONLY_POST_ALLOWED):
            return None
        return ("student_ended", 403) if student else ("readonly", 403)
    # export_only
    if student:
        return ("student_ended", 403)
    if role == "admin":
        if _starts(path, ("/admin/export",)):
            return None
        return ("to_export", 303) if method in SAFE_METHODS else ("ended", 403)
    return ("ended", 403)


# ------------------------------------------------------------------ TIGARSEC console sign-in
def verify_vendor_response(text: str, keys: dict, challenge: str, now_ts: float):
    """The console sign-in answer TIGARSEC's tool makes for a one-time challenge. Raises
    LicenceError unless it's signed by TIGARSEC, for THIS challenge, and not expired."""
    text = "".join((text or "").split())
    if not text.startswith(VENDOR_PREFIX):
        raise LicenceError("That isn't a TIGARSEC console answer.")
    token = text[len(VENDOR_PREFIX):]
    if token.count(".") != 1:
        raise LicenceError("That answer is damaged.")
    body, sig = token.split(".")
    try:
        payload_bytes, signature = b64u_decode(body), b64u_decode(sig)
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        raise LicenceError("That answer is damaged.")
    if not isinstance(payload, dict) or payload.get("purpose") != "vendor-login":
        raise LicenceError("That isn't a TIGARSEC console answer.")
    _verify(keys, str(payload.get("kid")), payload_bytes, signature)
    if payload.get("challenge") != challenge:
        raise LicenceError("That answer is for a different challenge - make a new one for the code on this page.")
    if float(payload.get("exp", 0)) < now_ts:
        raise LicenceError("That answer has expired - make a new one.")
    return payload


def public_view(lic) -> dict:
    """A licence payload without the internal date objects (for storing/showing)."""
    return {k: v for k, v in (lic or {}).items() if not k.startswith("_")}
