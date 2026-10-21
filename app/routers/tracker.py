"""
Stage 12 (1): the class tracking sheet page and its Excel download (see app/tracker.py).
"""
import re

from fastapi import APIRouter, Request, Depends
from fastapi.responses import RedirectResponse, HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Teacher, Student
from ..course_access import get_course_for_teacher
from ..auth import get_current_teacher
from ..tracker import build_tracker, tracker_xlsx, SYMBOL
from .marking_report import class_marks, _units

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def course_tracker(db, course):
    units = _units(course)
    marks_by_unit = {}
    for u in units:
        work = class_marks(db, course, u.get("name"))
        if work:
            marks_by_unit[u.get("name")] = {w["student_id"]: w for w in work}
    students = [{"id": s.id, "name": s.name} for s in
                db.query(Student).filter(Student.course_id == course.id, Student.is_active.is_(True)).order_by(Student.name).all()]
    return build_tracker(units, students, marks_by_unit)


def homework_grid(db, course, limit=8):
    """The latest homework as columns, each active student's result as a cell:
    {"columns": [{"id", "title", "due", "avg"}], "cells": {student_id: [{"state", "pct", "late"}, ...]}} or None."""
    from .homework import homework_summaries
    hws = list(reversed(homework_summaries(db, course.id, limit=limit)))  # oldest first, like the units
    if not hws:
        return None
    cells = {}
    for h in hws:
        for sid, r in h["by_student"].items():
            cells.setdefault(sid, []).append({"state": r["state"], "pct": r["pct"], "late": r["late"]})
    return {"columns": [{"id": h["hw"].id, "title": h["hw"].title, "due": h["hw"].due_at, "avg": h["avg"]} for h in hws],
            "cells": cells}


@router.get("/courses/{course_id}/tracker", response_class=HTMLResponse)
def tracker_page(course_id: int, request: Request, unit: str = "all", risk: int = 0, db: Session = Depends(get_db),
                 teacher: Teacher = Depends(get_current_teacher)):
    course = get_course_for_teacher(db, course_id, teacher)
    if not course:
        return RedirectResponse(url="/", status_code=303)
    data = course_tracker(db, course)
    homework = homework_grid(db, course)
    shown = data["units"] if unit == "all" else [u for u in data["units"] if str(u["index"]) == unit] or data["units"]
    rows = [r for r in data["rows"] if r["risk"]] if risk else data["rows"]
    return templates.TemplateResponse("tracker.html", {
        "request": request, "teacher": teacher, "course": course, "data": data, "shown": shown, "rows": rows,
        "unit_filter": unit, "risk_only": bool(risk), "symbol": SYMBOL, "homework": homework})


@router.get("/courses/{course_id}/tracker.xlsx")
def tracker_download(course_id: int, db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    course = get_course_for_teacher(db, course_id, teacher)
    if not course:
        return RedirectResponse(url="/", status_code=303)
    safe = re.sub(r"[^A-Za-z0-9 _-]", "", course.name)[:60].strip() or "course"
    return Response(tracker_xlsx(course.name, course_tracker(db, course), homework_grid(db, course)),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{safe} - tracker.xlsx"'})
