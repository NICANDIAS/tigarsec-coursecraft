from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Teacher, AuditLogEntry
from ..auth import hash_password, verify_password, create_access_token, create_pending_2fa_token, read_pending_2fa_token
from ..system_settings import get_system_settings, parse_allowed_domains, email_domain_allowed
from .. import otp as otp_module
from ..email_sender import send_email, is_configured as email_is_configured
from ..security import registration_open, login_throttle, wait_message, client_ip

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["registration_open"] = registration_open

CLOSED_MESSAGE = "New accounts are created by the admin here. Ask them for a login."


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db)):
    # Open by default (a college LAN). REGISTRATION=closed in .env (a public demo) means only
    # the admin creates accounts - except the very first one, so a fresh install gets its admin.
    is_first_account = db.query(Teacher).count() == 0
    if not is_first_account and not registration_open():
        return templates.TemplateResponse("login.html", {"request": request, "error": CLOSED_MESSAGE})
    settings = get_system_settings(db)
    allowed_domains = parse_allowed_domains(settings.allowed_email_domains)
    return templates.TemplateResponse(
        "register.html",
        {"request": request, "error": None, "is_first_account": is_first_account, "allowed_domains": allowed_domains},
    )


@router.post("/register")
def register_submit(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    is_first_account = db.query(Teacher).count() == 0
    if not is_first_account and not registration_open():
        return templates.TemplateResponse("login.html", {"request": request, "error": CLOSED_MESSAGE}, status_code=403)
    settings = get_system_settings(db)
    allowed_domains = parse_allowed_domains(settings.allowed_email_domains)

    # The very first account can't sensibly be domain-restricted - nobody could have
    # configured a restriction yet, since doing so requires being logged in as an admin,
    # and there is no admin until this account exists. Every account after that is
    # checked normally against whatever's actually configured at the time.
    if not is_first_account and not email_domain_allowed(email, allowed_domains):
        domains_text = ", ".join("@" + d for d in allowed_domains)
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": f"This college's CourseCraft only accepts {domains_text} email addresses.",
             "is_first_account": False, "allowed_domains": allowed_domains},
        )

    existing = db.query(Teacher).filter(Teacher.email == email).first()
    if existing:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "An account with that email already exists.",
             "is_first_account": is_first_account, "allowed_domains": allowed_domains},
        )

    teacher = Teacher(
        name=name,
        email=email,
        hashed_password=hash_password(password),
        role="admin" if is_first_account else "lecturer",
        is_primary_admin=is_first_account,
    )
    db.add(teacher)
    db.commit()
    return RedirectResponse(url="/login", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    ip = client_ip(request)
    wait = login_throttle.blocked_for(email, ip)
    if wait:
        return templates.TemplateResponse("login.html", {"request": request, "error": wait_message(wait)}, status_code=429)
    teacher = db.query(Teacher).filter(Teacher.email == email).first()
    if not teacher or not verify_password(password, teacher.hashed_password):
        login_throttle.failed(email, ip)
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Incorrect email or password."}
        )
    login_throttle.succeeded(email)
    if not teacher.is_active:
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "This account has been deactivated. Contact your admin."}
        )

    settings = get_system_settings(db)
    two_factor_required = teacher.two_factor_enabled or settings.two_factor_mandatory

    if not two_factor_required:
        token = create_access_token(teacher.id)
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(key="access_token", value=token, httponly=True, max_age=12 * 3600)
        return response

    # Standing fix, replacing an earlier version of this that failed OPEN on any delivery
    # problem - correctly reasoned for the schema-sync lockout (no security implications
    # either way there), wrongly carried over to 2FA, where letting someone in without the
    # second factor while claiming 2FA is "on" is a real, ongoing security gap for as long
    # as the misconfiguration lasts, not a neutral availability trade-off. Never silently
    # treat "couldn't verify the second factor" as equivalent to "verified".
    delivery_error = None
    code = None
    if not email_is_configured():
        delivery_error = "email sending is not configured on this server (SMTP_HOST missing from .env)"
    else:
        code = otp_module.create_and_store_otp(db, teacher.id)
        try:
            send_email(
                teacher.email, "Your CourseCraft login code",
                f"Your one-time login code is {code}. It expires in {otp_module.OTP_VALID_MINUTES} minutes.\n\n"
                f"If you didn't just try to log in, you can ignore this email - the code will simply expire.",
            )
        except Exception as e:
            delivery_error = str(e)

    if delivery_error:
        if teacher.two_factor_enabled and not settings.two_factor_mandatory:
            # This was the PERSON'S OWN choice to enable, for their own account only - safe
            # to self-correct: let them in this one time on the password alone, but VISIBLY
            # turn their own two-factor back off rather than silently pretending it still
            # applies. Never an unbounded, repeating gap - the next login is password-only
            # until they deliberately turn it on again, by which point they'll know why.
            teacher.two_factor_enabled = False
            db.add(AuditLogEntry(
                actor=teacher.email, action="two_factor_auto_disabled_delivery_failed",
                detail=f"Two-factor login was turned OFF for {teacher.email} automatically because "
                       f"the verification code could not be delivered ({delivery_error}). Logged in "
                       f"on the password alone this one time - re-enable it from Settings once fixed.",
            ))
            db.commit()
            token = create_access_token(teacher.id)
            response = RedirectResponse(url="/?two_factor_auto_disabled=1", status_code=303)
            response.set_cookie(key="access_token", value=token, httponly=True, max_age=12 * 3600)
            return response
        else:
            # Mandatory, set by an admin for EVERYONE - one person's delivery problem must
            # not silently strip two-factor protection from the whole institution just
            # because they happened to be the one who hit it. Refuse this login outright
            # for ordinary accounts, logged loudly, pointing at the actual fix.
            #
            # ADMIN accounts are the one deliberate exception, and only to let them log in
            # - the mandatory setting itself is never touched automatically. Reasoning: if
            # SMTP breaks AFTER being verified working when mandatory was turned on (the
            # only way this scenario can occur, since enabling mandatory without working
            # SMTP is already blocked), someone needs to be able to reach Admin: System
            # Settings to fix SMTP or turn mandatory off - and that must not depend on one
            # specific person having server/terminal access, or the whole institution can
            # be one broken mail relay away from nobody being able to log in at all. Any
            # admin account works, not just the primary one, so this doesn't concentrate
            # recovery into a single person either. Ordinary (non-admin) accounts are not
            # exempted - the security-weakening exposure is deliberately limited to the
            # fewer, higher-trust accounts who are also the ones responsible for fixing it.
            if teacher.role == "admin":
                db.add(AuditLogEntry(
                    actor=teacher.email, action="two_factor_admin_emergency_bypass",
                    detail=f"Admin {teacher.email} logged in on password alone - two-factor is "
                           f"mandatory, but the verification code could not be delivered "
                           f"({delivery_error}). Mandatory two-factor was NOT changed - go to "
                           f"Admin: System Settings to fix SMTP or turn it off.",
                ))
                db.commit()
                token = create_access_token(teacher.id)
                response = RedirectResponse(url="/?two_factor_admin_bypass=1", status_code=303)
                response.set_cookie(key="access_token", value=token, httponly=True, max_age=12 * 3600)
                return response

            db.add(AuditLogEntry(
                actor=teacher.email, action="two_factor_login_blocked_delivery_failed",
                detail=f"Login blocked for {teacher.email} - two-factor is mandatory for everyone, "
                       f"but the verification code could not be delivered ({delivery_error}). An "
                       f"admin needs to fix email sending, or turn off mandatory two-factor, from "
                       f"Admin: System Settings.",
            ))
            db.commit()
            return templates.TemplateResponse(
                "login.html",
                {"request": request, "error": "Two-factor login is required for every account here, "
                 "but the verification code couldn't be sent just now. This has been logged - "
                 "contact your admin."},
            )

    pending_token = create_pending_2fa_token(teacher.id)
    response = RedirectResponse(url="/login/verify-2fa", status_code=303)
    response.set_cookie(key="pending_2fa_token", value=pending_token, httponly=True, max_age=otp_module.OTP_VALID_MINUTES * 60)
    return response


@router.get("/login/verify-2fa", response_class=HTMLResponse)
def verify_2fa_page(request: Request):
    teacher_id = read_pending_2fa_token(request.cookies.get("pending_2fa_token"))
    if teacher_id is None:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse("login_verify_2fa.html", {"request": request, "error": None})


@router.post("/login/verify-2fa")
def verify_2fa_submit(
    request: Request,
    code: str = Form(...),
    db: Session = Depends(get_db),
):
    teacher_id = read_pending_2fa_token(request.cookies.get("pending_2fa_token"))
    if teacher_id is None:
        return RedirectResponse(url="/login", status_code=303)

    otp_key = f"2fa-teacher-{teacher_id}"
    ip = client_ip(request)
    wait = login_throttle.blocked_for(otp_key, ip)
    if wait:
        return templates.TemplateResponse("login_verify_2fa.html", {"request": request, "error": wait_message(wait)}, status_code=429)
    if not otp_module.verify_otp(db, teacher_id, code):
        login_throttle.failed(otp_key, ip)
        return templates.TemplateResponse(
            "login_verify_2fa.html",
            {"request": request, "error": "That code is incorrect or has expired. Request a new one by logging in again."},
        )

    login_throttle.succeeded(otp_key)
    token = create_access_token(teacher_id)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(key="access_token", value=token, httponly=True, max_age=12 * 3600)
    response.delete_cookie("pending_2fa_token")
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("access_token")
    return response
