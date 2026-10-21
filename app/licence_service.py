"""CourseCraft licensing - the database part. The rules live in app/licence.py; this file stores
licences, works out today's stage (cached for a minute), records history, sends admin reminders,
picks up licence files dropped into data/licence/inbox/, and blocks requests a stage doesn't allow.

Only the TIGARSEC console (/vendor, app/routers/vendor.py) installs licences or shows details.
"""
import datetime
import json
import os
import secrets
import shutil
import threading
import time
from http.cookies import SimpleCookie

from . import licence as L
from .licence_keys import TRUSTED_PUBLIC_KEYS
from .models import LicenceRecord, LicenceMeta, LicenceEvent, Teacher

CONTACT = os.getenv("LICENCE_CONTACT", "support@tigarsec.com").strip() or "support@tigarsec.com"
LICENCE_DIR = os.path.join("data", "licence")
INBOX = os.path.join(LICENCE_DIR, "inbox")
INSTALLED_DIR = os.path.join(LICENCE_DIR, "installed")
CACHE_SECONDS = 60


def uk_today() -> datetime.date:
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Europe/London")).date()
    except Exception:
        return datetime.date.today()


def log_event(db, action: str, detail: str = ""):
    db.add(LicenceEvent(action=action, detail=detail))


def get_meta(db) -> LicenceMeta:
    meta = db.get(LicenceMeta, 1)
    if meta is None:
        raw = secrets.token_hex(8).upper()
        meta = LicenceMeta(id=1, install_id="-".join(raw[i:i + 4] for i in range(0, 16, 4)),
                           first_started_at=datetime.datetime.utcnow(), reminders_sent=[])
        db.add(meta)
        log_event(db, "installation_created",
                  f"Install ID {meta.install_id}. No licence yet: {L.EVALUATION_DAYS}-day evaluation starts today.")
        db.commit()
        try:
            os.makedirs(LICENCE_DIR, exist_ok=True)
            with open(os.path.join(LICENCE_DIR, "install-id.txt"), "w") as f:
                f.write(meta.install_id + "\n")
        except OSError:
            pass
    return meta


def installed_licences(db, keys=None):
    """-> (valid licences [payload + '_record'], problems [(record, reason)])"""
    keys = TRUSTED_PUBLIC_KEYS if keys is None else keys
    good, bad = [], []
    for rec in db.query(LicenceRecord).order_by(LicenceRecord.id).all():
        try:
            lic = L.verify_licence(rec.raw, keys)
            lic["_record"] = rec
            good.append(lic)
        except L.LicenceError as e:
            bad.append((rec, str(e)))
    return good, bad


def current_state(db, keys=None, today=None) -> dict:
    meta = get_meta(db)
    today = today or uk_today()
    good, bad = installed_licences(db, keys)
    lic = L.choose_licence(good, today)
    state = L.compute_state(lic, today, meta.first_started_at.date())
    state.update(install_id=meta.install_id, first_started=meta.first_started_at.date(),
                 licences=good, problems=bad, today=today)
    return state


# ------------------------------------------------------------------ cached view for every request
_cache = {"at": 0.0, "state": None}
_cache_lock = threading.Lock()


def invalidate_cache():
    with _cache_lock:
        _cache["at"] = 0.0


def cached_state():
    """Today's state, worked out at most once a minute. None if it couldn't be worked out
    (then nothing is blocked - a licensing fault must never stop teaching)."""
    with _cache_lock:
        if _cache["state"] is not None and time.monotonic() - _cache["at"] < CACHE_SECONDS:
            return _cache["state"]
    from .database import SessionLocal
    db = SessionLocal()
    try:
        state = current_state(db)
    except Exception as e:
        print(f"[licence] could not work out the licence state (nothing blocked): {e}", flush=True)
        state = None
    finally:
        db.close()
    with _cache_lock:
        _cache["state"], _cache["at"] = state, time.monotonic()
    return state


def licence_banner(teacher):
    """For base.html: the short notice for this person, or None."""
    state = cached_state()
    if not state or teacher is None:
        return None
    return L.banner_for(state, getattr(teacher, "role", None), CONTACT)


# ------------------------------------------------------------------ installing
def install(db, text: str, actor: str, keys=None) -> dict:
    """Check and store a licence. Raises LicenceError (nothing stored) if it isn't genuine."""
    keys = TRUSTED_PUBLIC_KEYS if keys is None else keys
    lic = L.verify_licence(text, keys)
    raw = text.strip() + "\n"
    token = L.extract_token(raw)
    for rec in db.query(LicenceRecord).filter(LicenceRecord.licence_id == lic["id"]).all():
        try:
            if L.extract_token(rec.raw) == token:
                return lic  # the same file again: nothing to do
        except L.LicenceError:
            pass
    db.add(LicenceRecord(licence_id=lic["id"], raw=raw, installed_by=actor))
    log_event(db, "licence_installed",
              f"{lic['id']} for {lic['licensee']}: {lic['edition']}, {lic['seats']} seats, "
              f"{lic['valid_from']} to {lic['valid_until']} ({lic['type']}). Installed by {actor}.")
    db.commit()
    invalidate_cache()
    return lic


def import_inbox(db, keys=None) -> list:
    """Licence files copied into data/licence/inbox/ (e.g. by TIGARSEC over SSH) are checked
    and installed, then moved to data/licence/installed/ (or renamed .rejected)."""
    results = []
    if not os.path.isdir(INBOX):
        return results
    os.makedirs(INSTALLED_DIR, exist_ok=True)
    for name in sorted(os.listdir(INBOX)):
        path = os.path.join(INBOX, name)
        if not os.path.isfile(path) or name.endswith(".rejected"):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                lic = install(db, f.read(), "file drop", keys)
            shutil.move(path, os.path.join(INSTALLED_DIR, name))
            results.append((name, lic["id"]))
        except (L.LicenceError, UnicodeDecodeError) as e:
            os.replace(path, path + ".rejected")
            log_event(db, "licence_rejected", f"{name}: {e}")
            db.commit()
            results.append((name, None))
    return results


# ------------------------------------------------------------------ hourly checks
def run_checks(db, keys=None, today=None, send=None, email_ready=None) -> dict:
    """Hourly: pick up dropped files, note stage changes, send due admin reminders."""
    import_inbox(db, keys)
    state = current_state(db, keys, today)
    meta = get_meta(db)
    if meta.last_stage != state["stage"]:
        if meta.last_stage is not None:
            log_event(db, "stage_changed", f"{L.STAGE_LABELS.get(meta.last_stage, meta.last_stage)} -> {state['label']}")
        meta.last_stage = state["stage"]
        db.commit()
        invalidate_cache()
    sent = set(meta.reminders_sent or [])
    due = L.reminder_due(state, sent)
    if due:
        if send is None:
            from .email_sender import send_email as send
        if email_ready is None:
            from .email_sender import is_configured as email_ready
        if email_ready():
            key, subject = due
            admins = db.query(Teacher).filter(Teacher.role == "admin", Teacher.is_active.is_(True)).all()
            banner = L.banner_for(state, "admin", CONTACT)
            delivered = 0
            for admin in admins:
                try:
                    send(admin.email, subject,
                         f"Hello {admin.name},\n\n{banner['text'] if banner else subject}\n\n"
                         "Nothing is ever deleted when a licence ends: you can always export all of your "
                         "data from Admin -> Data export.\n\nCourseCraft")
                    delivered += 1
                except Exception as e:
                    log_event(db, "reminder_failed", f"{key} to {admin.email}: {e}")
            if delivered:
                meta.reminders_sent = sorted(sent | {key})
                log_event(db, "reminder_sent", f"{key} to {delivered} admin(s)")
            db.commit()
    return state


# ------------------------------------------------------------------ blocking what a stage doesn't allow
MESSAGES = {
    "readonly": ("CourseCraft is read-only",
                 "Your college's CourseCraft licence has ended. You can still view, download and export, "
                 "but you can't create, mark or generate anything until it's renewed."),
    "ended": ("CourseCraft has closed",
              "Your college's CourseCraft licence has ended. Admins can still sign in to export all of "
              "your data - nothing has been deleted."),
    "student_ended": ("This session has ended", "This activity is no longer open. Ask your lecturer."),
}


def blocked_page(kind: str) -> str:
    title, text = MESSAGES[kind]
    extra = "" if kind == "student_ended" else (
        f'<p>Contact TIGARSEC to renew: <a href="mailto:{CONTACT}">{CONTACT}</a></p>'
        '<p><a href="/">Back</a> &middot; <a href="/logout">Sign out</a></p>')
    return ("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            "content='width=device-width, initial-scale=1'><title>" + title + "</title>"
            "<link rel='stylesheet' href='/static/css/style.css'></head><body><div class='container' "
            "style='max-width:560px;margin:60px auto;'><div class='card'><h1>" + title + "</h1><p>" + text +
            "</p>" + extra + "</div></div></body></html>")


def _wants_json(scope) -> bool:
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    path = scope.get("path", "")
    return ("application/json" in headers.get("accept", "") or "application/json" in headers.get("content-type", "")
            or headers.get("x-requested-with") or path.endswith(("/data", "/answer", "/save", "/status")))


def role_from_scope(scope, session_factory=None):
    """The signed-in person's role, read from their login cookie (only needed when export-only)."""
    import jwt
    from .auth import SECRET_KEY, ALGORITHM
    raw = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}.get("cookie", "")
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:
        return None
    token = jar.get("access_token")
    if not token:
        return None
    try:
        teacher_id = int(jwt.decode(token.value, SECRET_KEY, algorithms=[ALGORITHM]).get("sub"))
    except Exception:
        return None
    if session_factory is None:
        from .database import SessionLocal as session_factory
    db = session_factory()
    try:
        t = db.get(Teacher, teacher_id)
        return t.role if t and t.is_active else None
    finally:
        db.close()


class LicenceMiddleware:
    """Pure ASGI middleware applying app/licence.decide() to every request."""

    def __init__(self, app, state_getter=None, session_factory=None):
        self.app = app
        self.state_getter = state_getter or cached_state
        self.session_factory = session_factory

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        state = self.state_getter()
        stage = state["stage"] if state else "licensed"
        if stage in L.FULL_USE_STAGES:
            return await self.app(scope, receive, send)
        path, method = scope.get("path", "/"), scope.get("method", "GET")
        role = role_from_scope(scope, self.session_factory) if stage == "export_only" else None
        decision = L.decide(stage, method, path, role)
        if decision is None:
            return await self.app(scope, receive, send)
        kind, status = decision
        if kind == "to_export":
            await send({"type": "http.response.start", "status": 303, "headers": [(b"location", b"/admin/export")]})
            await send({"type": "http.response.body", "body": b""})
            return
        if _wants_json(scope):
            body = json.dumps({"error": MESSAGES[kind][0] + ". " + MESSAGES[kind][1]}).encode()
            ctype = b"application/json"
        else:
            body = blocked_page(kind).encode()
            ctype = b"text/html; charset=utf-8"
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", ctype), (b"content-length", str(len(body)).encode()),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})
