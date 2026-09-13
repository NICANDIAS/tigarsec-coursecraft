from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Teacher, Course, Submission, Student, SpecVersion, SchemeOfWork, LessonDeck
from ..course_access import get_courses_for_teacher
from ..auth import get_current_teacher

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# Matches MAX_SOW_WEEKS in scheme_of_work.py - same "keep lists bounded" principle applied
# consistently across the app, per feedback that these should use the same number.
TASKS_RECENT_LIMIT = 20


def _teacher_course_ids(db: Session, teacher: Teacher) -> list[int]:
    return [c.id for c in get_courses_for_teacher(db, teacher)]


def _gather_tasks(db: Session, teacher: Teacher) -> dict:
    course_ids = _teacher_course_ids(db, teacher)

    # Standing fix: these two queries had no teacher filter at all - every teacher's Tasks
    # page showed EVERY teacher's spec-parsing activity, institution-wide. The spec
    # library's own browsing page is deliberately shared (any teacher can pick from any
    # parsed spec) - but a personal Tasks page is "things I started", not a global
    # activity feed, and showing someone else's in-progress or failed parse there is
    # exactly the kind of cross-user leak this page must never have. Scoped to who
    # actually uploaded each spec, the same way every other task type below is already
    # scoped to the current teacher's own courses.
    specs_parsing = db.query(SpecVersion).filter(
        SpecVersion.parse_status == "parsing", SpecVersion.uploaded_by_teacher_id == teacher.id
    ).all()
    specs_recent = (
        db.query(SpecVersion)
        .filter(
            SpecVersion.parse_status.in_(["parsed", "failed", "cancelled"]),
            SpecVersion.uploaded_by_teacher_id == teacher.id,
        )
        .order_by(SpecVersion.id.desc()).limit(TASKS_RECENT_LIMIT).all()
    )

    submissions_marking = [
        s for s in db.query(Submission).filter(Submission.status == "marking").all()
        if s.student and s.student.course_id in course_ids
    ]
    submissions_recent = [
        s for s in db.query(Submission).filter(Submission.status.in_(["needs_review", "released", "failed"])).order_by(Submission.id.desc()).all()
        if s.student and s.student.course_id in course_ids
    ][:TASKS_RECENT_LIMIT]

    sow_generating = db.query(SchemeOfWork).filter(
        SchemeOfWork.course_id.in_(course_ids), SchemeOfWork.status == "generating"
    ).all()
    sow_recent = db.query(SchemeOfWork).filter(
        SchemeOfWork.course_id.in_(course_ids), SchemeOfWork.status.in_(["ready", "failed"])
    ).order_by(SchemeOfWork.id.desc()).limit(TASKS_RECENT_LIMIT).all()

    decks_generating = db.query(LessonDeck).filter(
        LessonDeck.course_id.in_(course_ids), LessonDeck.status.in_(["generating", "queued"])
    ).all()
    decks_recent = db.query(LessonDeck).filter(
        LessonDeck.course_id.in_(course_ids), LessonDeck.status.in_(["ready", "failed"])
    ).order_by(LessonDeck.id.desc()).limit(TASKS_RECENT_LIMIT).all()

    return {
        "specs_parsing": specs_parsing, "specs_recent": specs_recent,
        "submissions_marking": submissions_marking, "submissions_recent": submissions_recent,
        "sow_generating": sow_generating, "sow_recent": sow_recent,
        "decks_generating": decks_generating, "decks_recent": decks_recent,
    }


@router.get("/tasks", response_class=HTMLResponse)
def tasks_page(
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    tasks = _gather_tasks(db, teacher)
    return templates.TemplateResponse("tasks.html", {"request": request, "teacher": teacher, "recent_limit": TASKS_RECENT_LIMIT, **tasks})


@router.get("/tasks/summary")
def tasks_summary(
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """Polled from the sidebar to show a live in-progress count and power the
    'something just finished' notification - see app/static/js/tasks.js."""
    tasks = _gather_tasks(db, teacher)
    in_progress = (
        len(tasks["specs_parsing"]) + len(tasks["submissions_marking"])
        + len(tasks["sow_generating"]) + len(tasks["decks_generating"])
    )
    return {"in_progress": in_progress}
