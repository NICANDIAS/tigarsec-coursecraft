"""The TIGARSEC licence console (/vendor). Not linked from anywhere in CourseCraft, and no
CourseCraft account - admin or otherwise - can open it.

Signing in needs TIGARSEC's private signing key (which never comes near this server):
  1. /vendor shows a one-time challenge code (valid 10 minutes).
  2. On TIGARSEC's Mac:  python ccl.py vendor-login <code>   prints a signed answer.
  3. Paste the answer here -> a console session for 1 hour (kept in memory only, so a restart
     signs you out; nothing an admin could copy from the database or .env).
"""
import datetime
import secrets
import threading
import time

from fastapi import APIRouter, Request, Depends, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import LicenceEvent, Teacher
from .. import licence as L
from .. import licence_service as svc
from ..licence_keys import TRUSTED_PUBLIC_KEYS
from ..security import login_throttle, wait_message, client_ip

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

CHALLENGE_SECONDS = 600
SESSION_SECONDS = 3600
COOKIE = "cc_vendor"
_lock = threading.Lock()
_challenges = {}  # code -> expiry (monotonic)
_sessions = {}    # token -> expiry (monotonic)
THROTTLE_KEY = "tigarsec-console"


def _prune(store):
    now = time.monotonic()
    for k in [k for k, exp in store.items() if exp < now]:
        store.pop(k, None)


def _new_challenge() -> str:
    with _lock:
        _prune(_challenges)
        if len(_challenges) > 50:
            _challenges.clear()
        code = "-".join(secrets.token_hex(3).upper() for _ in range(3))
        _challenges[code] = time.monotonic() + CHALLENGE_SECONDS
        return code


def _take_challenge(code: str) -> bool:
    with _lock:
        _prune(_challenges)
        return _challenges.pop(code, None) is not None


def _vendor_signed_in(request: Request) -> bool:
    token = request.cookies.get(COOKIE)
    if not token:
        return False
    with _lock:
        _prune(_sessions)
        return token in _sessions


def _set_session(response):
    token = secrets.token_urlsafe(32)
    with _lock:
        _prune(_sessions)
        _sessions[token] = time.monotonic() + SESSION_SECONDS
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite="strict", path="/vendor")
    return response


def _challenge_page(request, error=None, status_code=200):
    code = _new_challenge()
    meta_install = None
    try:
        from ..database import SessionLocal
        db = SessionLocal()
        try:
            meta_install = svc.get_meta(db).install_id
        finally:
            db.close()
    except Exception:
        pass
    return templates.TemplateResponse(
        "vendor_challenge.html",
        {"request": request, "code": code, "install_id": meta_install, "error": error,
         "keys_ready": bool(TRUSTED_PUBLIC_KEYS), "minutes": CHALLENGE_SECONDS // 60},
        status_code=status_code,
    )


@router.get("/vendor", response_class=HTMLResponse)
def vendor_home(request: Request, db: Session = Depends(get_db), msg: str = "", err: str = ""):
    if not _vendor_signed_in(request):
        return _challenge_page(request)
    state = svc.current_state(db)
    today = state["today"]
    rows = []
    in_force = state["licence"]
    for lic in sorted(state["licences"], key=lambda l: l["_record"].id, reverse=True):
        if in_force is not None and lic["_record"].id == in_force["_record"].id:
            status = "In force"
        elif lic["_from"] > today:
            status = "Starts later"
        else:
            status = "Superseded or ended"
        rows.append({"lic": L.public_view(lic), "rec": lic["_record"], "status": status})
    staff_active = db.query(Teacher).filter(Teacher.is_active.is_(True)).count()
    admins_active = db.query(Teacher).filter(Teacher.is_active.is_(True), Teacher.role == "admin").count()
    events = db.query(LicenceEvent).order_by(LicenceEvent.id.desc()).limit(50).all()
    return templates.TemplateResponse("vendor_console.html", {
        "request": request, "state": state, "lic": L.public_view(in_force) if in_force else None,
        "rows": rows, "problems": state["problems"], "events": events,
        "staff_active": staff_active, "admins_active": admins_active,
        "admin_banner": L.banner_for(state, "admin", svc.CONTACT),
        "staff_banner": L.banner_for(state, "lecturer", svc.CONTACT),
        "msg": msg, "err": err, "contact": svc.CONTACT,
    })


@router.post("/vendor/login")
def vendor_login(request: Request, code: str = Form(...), answer: str = Form(...), db: Session = Depends(get_db)):
    ip = client_ip(request)
    wait = login_throttle.blocked_for(THROTTLE_KEY, ip)
    if wait:
        return _challenge_page(request, wait_message(wait), 429)
    try:
        if not _take_challenge(code.strip()):
            raise L.LicenceError("That challenge code has expired or was already used - use the new one on this page.")
        L.verify_vendor_response(answer, TRUSTED_PUBLIC_KEYS, code.strip(), time.time())
    except L.LicenceError as e:
        login_throttle.failed(THROTTLE_KEY, ip)
        svc.log_event(db, "console_signin_failed", f"from {ip}: {e}")
        db.commit()
        return _challenge_page(request, str(e), 403)
    login_throttle.succeeded(THROTTLE_KEY)
    svc.log_event(db, "console_signin", f"TIGARSEC console signed in from {ip}")
    db.commit()
    return _set_session(RedirectResponse("/vendor", status_code=303))


@router.post("/vendor/licence")
async def vendor_install(request: Request, licence_text: str = Form(""), licence_file: UploadFile | None = File(None),
                         db: Session = Depends(get_db)):
    if not _vendor_signed_in(request):
        return RedirectResponse("/vendor", status_code=303)
    text = licence_text.strip()
    if licence_file is not None and licence_file.filename:
        data = await licence_file.read(64 * 1024)
        text = data.decode("utf-8", errors="replace")
    if not text:
        return RedirectResponse("/vendor?err=Paste+a+licence+or+choose+a+file.", status_code=303)
    try:
        lic = svc.install(db, text, "TIGARSEC console")
    except L.LicenceError as e:
        svc.log_event(db, "licence_rejected", f"console: {e}")
        db.commit()
        from urllib.parse import quote
        return RedirectResponse(f"/vendor?err={quote(str(e))}", status_code=303)
    from urllib.parse import quote
    return RedirectResponse(f"/vendor?msg={quote('Installed ' + lic['id'] + ' for ' + lic['licensee'] + '.')}", status_code=303)


@router.post("/vendor/logout")
def vendor_logout(request: Request):
    token = request.cookies.get(COOKIE)
    with _lock:
        _sessions.pop(token, None)
    response = RedirectResponse("/vendor", status_code=303)
    response.delete_cookie(COOKIE, path="/vendor")
    return response
