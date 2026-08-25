from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
import datetime

from ..database import get_db, engine, Base
from ..models import Teacher, AuditLogEntry, Submission, Student, Course
from ..auth import require_admin, hash_password, ROLES
from ..schema_check import diff_schema, apply_schema_fix
from ..system_settings import get_system_settings, parse_allowed_domains
from ..email_sender import is_configured as email_is_configured

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/admin/logs", response_class=HTMLResponse)
def audit_logs(
    request: Request,
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    """
    Admin-only record of who did what and when - uploads, approvals, pull-backs, course
    deletions, and anything else logged via AuditLogEntry. This is the app's own audit
    trail, not raw server/application logs (which are a different thing, viewable via
    `docker compose logs app` on the server itself - this covers user actions for record
    and accountability purposes).
    """
    entries = db.query(AuditLogEntry).order_by(AuditLogEntry.id.desc()).limit(200).all()

    # Attach readable context (student/course) for entries tied to a submission, without
    # a separate query per row.
    submission_ids = [e.submission_id for e in entries if e.submission_id]
    context_by_submission_id = {}
    if submission_ids:
        subs = db.query(Submission).filter(Submission.id.in_(submission_ids)).all()
        for s in subs:
            student = db.query(Student).filter(Student.id == s.student_id).first()
            course = db.query(Course).filter(Course.id == student.course_id).first() if student else None
            context_by_submission_id[s.id] = {
                "filename": s.original_filename,
                "student_name": student.name if student else "?",
                "course_name": course.name if course else "?",
            }

    return templates.TemplateResponse(
        "admin_logs.html",
        {"request": request, "teacher": admin, "entries": entries, "context_by_submission_id": context_by_submission_id},
    )


@router.get("/admin/users", response_class=HTMLResponse)
def list_users(
    request: Request,
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    users = db.query(Teacher).order_by(Teacher.id).all()
    return templates.TemplateResponse(
        "admin_users.html", {"request": request, "teacher": admin, "users": users, "roles": ROLES}
    )


@router.post("/admin/users/create")
def create_user(
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    existing = db.query(Teacher).filter(Teacher.email == email).first()
    if existing:
        return RedirectResponse(url="/admin/users", status_code=303)

    role = role if role in ROLES else "lecturer"
    new_user = Teacher(name=name, email=email, hashed_password=hash_password(password), role=role)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    db.add(AuditLogEntry(
        actor=admin.email, action="user_created",
        detail=f"Created account for {email} with role '{role}'.",
    ))
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/role")
def change_role(
    user_id: int,
    role: str = Form(...),
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    user = db.query(Teacher).filter(Teacher.id == user_id).first()
    if not user or user.is_primary_admin or role not in ROLES:
        # Primary admin's role can never be changed - protected by design, not just UI.
        return RedirectResponse(url="/admin/users", status_code=303)

    old_role = user.role
    user.role = role
    db.add(AuditLogEntry(
        actor=admin.email, action="role_changed",
        detail=f"Changed {user.email}'s role from '{old_role}' to '{role}'.",
    ))
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/toggle-active")
def toggle_active(
    user_id: int,
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    user = db.query(Teacher).filter(Teacher.id == user_id).first()
    if not user or user.is_primary_admin:
        # The primary admin account can never be deactivated or deleted - by design.
        return RedirectResponse(url="/admin/users", status_code=303)

    user.is_active = not user.is_active
    db.add(AuditLogEntry(
        actor=admin.email, action="account_deactivated" if not user.is_active else "account_reactivated",
        detail=f"{user.email} was {'deactivated' if not user.is_active else 'reactivated'}.",
    ))
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/reset-password")
def admin_reset_password(
    user_id: int,
    new_password: str = Form(...),
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    user = db.query(Teacher).filter(Teacher.id == user_id).first()
    if user:
        user.hashed_password = hash_password(new_password)
        db.add(AuditLogEntry(
            actor=admin.email, action="password_reset_by_admin",
            detail=f"Password reset for {user.email} by {admin.email}.",
        ))
        db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/disable-two-factor")
def admin_disable_two_factor(
    user_id: int,
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    """
    A real escape hatch for someone OTHER than the admin getting locked out the same way -
    e.g. they turned on 2FA before SMTP was configured, or their email address stopped
    working. The admin can rescue them here without needing database access. (If the admin
    themselves is the one locked out, this page isn't reachable either - see the terminal
    command in the docs/README for that case, since no in-app action can escape that one.)
    """
    user = db.query(Teacher).filter(Teacher.id == user_id).first()
    if user:
        user.two_factor_enabled = False
        db.add(AuditLogEntry(
            actor=admin.email, action="two_factor_disabled_by_admin",
            detail=f"Two-factor login disabled for {user.email} by {admin.email}.",
        ))
        db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.get("/admin/schema-check", response_class=HTMLResponse)
def schema_check_page(
    request: Request,
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    """
    Compares what the app's models expect against what's actually in the live database -
    replaces the recurring "run this ALTER TABLE command" pattern from every recent
    update, which kept getting missed. Purely diagnostic - the fix itself only runs when
    explicitly requested via the button below, never automatically.
    """
    diff = diff_schema(engine, Base)
    return templates.TemplateResponse(
        "admin_schema_check.html",
        {"request": request, "teacher": admin, "diff": diff, "fix_report": None},
    )


@router.post("/admin/schema-check/fix", response_class=HTMLResponse)
def schema_check_fix(
    request: Request,
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    """
    Applies whatever the diff finds - purely additive (creates missing tables, adds
    missing columns), never drops or modifies anything that already exists, so there's no
    real risk to running this even speculatively.
    """
    diff = diff_schema(engine, Base)
    fix_report = apply_schema_fix(engine, Base, diff)

    db.add(AuditLogEntry(
        actor=admin.email, action="schema_fix_applied",
        detail="; ".join(fix_report) if fix_report else "Nothing to fix - schema already matched.",
    ))
    db.commit()

    # Re-diff after fixing, so the page shows the genuinely current state, not a promise
    new_diff = diff_schema(engine, Base)
    return templates.TemplateResponse(
        "admin_schema_check.html",
        {"request": request, "teacher": admin, "diff": new_diff, "fix_report": fix_report},
    )


@router.get("/admin/system-settings", response_class=HTMLResponse)
def system_settings_page(
    request: Request,
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    settings = get_system_settings(db)
    return templates.TemplateResponse(
        "admin_system_settings.html",
        {"request": request, "teacher": admin, "settings": settings,
         "parsed_domains": parse_allowed_domains(settings.allowed_email_domains)},
    )


@router.post("/admin/system-settings/smtp")
def update_smtp_settings(
    smtp_host: str = Form(""),
    smtp_port: str = Form(""),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from: str = Form(""),
    smtp_use_tls: str = Form(""),
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    """
    Configurable here rather than only .env - same reasoning as AI provider settings, and
    directly closes the gap that made the mandatory-2FA lockout scenario harder to recover
    from: an admin who can log in (e.g. via the emergency bypass below) can now fix SMTP
    from inside the app, without needing server/terminal access at all.
    """
    settings = get_system_settings(db)
    settings.smtp_host = smtp_host.strip() or None
    settings.smtp_port = int(smtp_port) if smtp_port.strip().isdigit() else None
    settings.smtp_user = smtp_user.strip() or None
    # Blank means "leave the existing password alone" - a password field is never
    # re-displayed once saved, so submitting the form without retyping it must not wipe it.
    if smtp_password.strip():
        settings.smtp_password = smtp_password
    settings.smtp_from = smtp_from.strip() or None
    settings.smtp_use_tls = bool(smtp_use_tls)
    settings.updated_at = datetime.datetime.utcnow()
    settings.updated_by_teacher_id = admin.id
    db.add(AuditLogEntry(
        actor=admin.email, action="smtp_settings_updated",
        detail=f"SMTP settings updated (host={settings.smtp_host!r}, from={settings.smtp_from!r}). "
               f"Password {'changed' if smtp_password.strip() else 'left unchanged'}.",
    ))
    db.commit()
    return RedirectResponse(url="/admin/system-settings", status_code=303)


@router.post("/admin/system-settings")
def update_system_settings(
    allowed_email_domains: str = Form(""),
    two_factor_mandatory: str = Form(""),
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    wants_mandatory = bool(two_factor_mandatory)
    # Standing fix, after a real lockout on the PERSONAL toggle: making this mandatory
    # system-wide without working email would strand EVERY account at once, not just one -
    # worse than what already happened. Refuse here too, for the same reason.
    if wants_mandatory and not email_is_configured():
        return RedirectResponse(url="/admin/system-settings?error=smtp_not_configured", status_code=303)

    settings = get_system_settings(db)
    settings.allowed_email_domains = allowed_email_domains.strip() or None
    settings.two_factor_mandatory = wants_mandatory
    settings.updated_at = datetime.datetime.utcnow()
    settings.updated_by_teacher_id = admin.id
    db.add(AuditLogEntry(
        actor=admin.email, action="system_settings_updated",
        detail=f"allowed_email_domains={settings.allowed_email_domains!r}, two_factor_mandatory={settings.two_factor_mandatory}",
    ))
    db.commit()
    return RedirectResponse(url="/admin/system-settings", status_code=303)
