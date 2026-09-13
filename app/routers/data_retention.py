import datetime

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Teacher, Submission, LessonDeck, AuditLogEntry
from ..auth import require_admin
from .. import retention

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/admin/data-retention", response_class=HTMLResponse)
def data_retention_page(
    request: Request,
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    policies = retention.get_or_create_policies(db)
    now = datetime.datetime.utcnow()

    pending = {}
    exempted = {}
    for category, (model, _) in retention.PURGE_FUNCTIONS.items():
        rows = (
            db.query(model)
            .filter(model.marked_for_deletion_at.isnot(None), model.purged_at.is_(None))
            .all()
        )
        policy = policies[category]
        pending[category] = [
            (row, row.marked_for_deletion_at + datetime.timedelta(days=policy.grace_period_days))
            for row in rows
        ]
        exempted[category] = (
            db.query(model)
            .filter(model.retention_exempt == True, model.purged_at.is_(None))  # noqa: E712
            .all()
        )

    return templates.TemplateResponse(
        "admin_data_retention.html",
        {
            "request": request, "teacher": admin, "policies": policies,
            "categories": retention.RETENTION_CATEGORIES, "pending": pending,
            "exempted": exempted, "now": now,
        },
    )


@router.post("/admin/data-retention/{category}")
def update_retention_policy(
    category: str,
    retention_days: str = Form(""),
    grace_period_days: str = Form("7"),
    enabled: str = Form(""),
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    if category not in retention.RETENTION_CATEGORIES:
        return RedirectResponse(url="/admin/data-retention", status_code=303)

    policies = retention.get_or_create_policies(db)
    policy = policies[category]
    policy.retention_days = int(retention_days) if retention_days.strip().isdigit() else None
    policy.grace_period_days = int(grace_period_days) if grace_period_days.strip().isdigit() else 7
    policy.enabled = bool(enabled)
    policy.updated_at = datetime.datetime.utcnow()
    policy.updated_by_teacher_id = admin.id
    db.add(AuditLogEntry(
        actor=admin.email, action="retention_policy_updated",
        detail=f"{category}: enabled={policy.enabled}, retention_days={policy.retention_days}, "
               f"grace_period_days={policy.grace_period_days}.",
    ))
    db.commit()
    return RedirectResponse(url="/admin/data-retention", status_code=303)


@router.post("/admin/data-retention/{category}/{item_id}/exempt")
def exempt_from_retention(
    category: str,
    item_id: int,
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    """The actual escape hatch behind the reminder emails - an admin reviewing what's about
    to be purged can pull any specific item out permanently (until manually un-exempted),
    e.g. because an appeal is in progress. Also cancels an in-progress countdown, not just
    future eligibility."""
    if category not in retention.PURGE_FUNCTIONS:
        return RedirectResponse(url="/admin/data-retention", status_code=303)
    model, _ = retention.PURGE_FUNCTIONS[category]
    row = db.get(model, item_id)
    if row:
        row.retention_exempt = True
        row.marked_for_deletion_at = None
        db.add(AuditLogEntry(
            submission_id=item_id if category == "submission_files" else None,
            actor=admin.email, action="retention_exemption_added",
            detail=f"{retention.describe_row(category, row)} exempted from retention policy by {admin.email}.",
        ))
        db.commit()
    return RedirectResponse(url="/admin/data-retention", status_code=303)


@router.post("/admin/data-retention/{category}/{item_id}/un-exempt")
def un_exempt_from_retention(
    category: str,
    item_id: int,
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    if category not in retention.PURGE_FUNCTIONS:
        return RedirectResponse(url="/admin/data-retention", status_code=303)
    model, _ = retention.PURGE_FUNCTIONS[category]
    row = db.get(model, item_id)
    if row:
        row.retention_exempt = False
        db.add(AuditLogEntry(
            submission_id=item_id if category == "submission_files" else None,
            actor=admin.email, action="retention_exemption_removed",
            detail=f"{retention.describe_row(category, row)} exemption removed by {admin.email} - "
                   f"eligible to be marked for deletion again on the next daily check.",
        ))
        db.commit()
    return RedirectResponse(url="/admin/data-retention", status_code=303)


@router.post("/admin/data-retention/run-now")
def run_retention_check_now(
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    """Manual trigger, mainly for testing a newly-configured policy without waiting for the
    daily cycle - runs the exact same function the background loop calls."""
    summary = retention.run_retention_check(db)
    db.add(AuditLogEntry(
        actor=admin.email, action="retention_check_run_manually",
        detail=f"Marked {summary['marked']}, purged {summary['purged']}, {summary['upcoming']} upcoming.",
    ))
    db.commit()
    return RedirectResponse(url="/admin/data-retention", status_code=303)
