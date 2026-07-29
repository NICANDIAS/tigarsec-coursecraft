from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
import datetime

from ..database import get_db
from ..models import Teacher, Feedback
from ..auth import get_current_teacher, require_admin

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/feedback", response_class=HTMLResponse)
def feedback_page(
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """
    A quick way to report a bug or suggest a feature without leaving the app - also shows
    the person their own past feedback and any response left on it, so submitting
    something doesn't feel like shouting into a void.
    """
    my_feedback = (
        db.query(Feedback)
        .filter(Feedback.teacher_id == teacher.id)
        .order_by(Feedback.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        "feedback_new.html", {"request": request, "teacher": teacher, "my_feedback": my_feedback},
    )


@router.post("/feedback")
def submit_feedback(
    request: Request,
    category: str = Form("general"),
    message: str = Form(...),
    page_context: str = Form(""),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    if category not in ("bug", "feature_request", "general"):
        category = "general"
    if message.strip():
        db.add(Feedback(
            teacher_id=teacher.id,
            category=category,
            message=message.strip(),
            page_context=page_context.strip() or None,
        ))
        db.commit()
    return RedirectResponse(url="/feedback?submitted=1", status_code=303)


@router.get("/admin/feedback", response_class=HTMLResponse)
def admin_feedback(
    request: Request,
    status: str = "",
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    query = db.query(Feedback)
    if status:
        query = query.filter(Feedback.status == status)
    entries = query.order_by(Feedback.created_at.desc()).all()
    counts = {
        s: db.query(Feedback).filter(Feedback.status == s).count()
        for s in ("new", "acknowledged", "planned", "done", "declined")
    }
    return templates.TemplateResponse(
        "admin_feedback.html",
        {"request": request, "teacher": admin, "entries": entries, "counts": counts, "current_status": status},
    )


@router.post("/admin/feedback/{feedback_id}/update")
def update_feedback(
    feedback_id: int,
    status: str = Form(...),
    admin_response: str = Form(""),
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    entry = db.get(Feedback, feedback_id)
    if entry and status in ("new", "acknowledged", "planned", "done", "declined"):
        entry.status = status
        entry.admin_response = admin_response.strip() or None
        entry.updated_at = datetime.datetime.utcnow()
        db.commit()
    return RedirectResponse(url="/admin/feedback", status_code=303)
