"""
Stage 10 (A): marking feeds teaching - the class marking report for one unit.

Shows, from the class's marked work: how many met each criterion, which criteria the class
found hard, who still needs each one (lecturer only), every student's next steps, and - on
request - the AI's summary of the COMMON problems behind each gap. One click takes those gaps
into the next lesson's PowerPoint.
"""
from urllib.parse import quote

from fastapi import APIRouter, Request, Depends, Form, BackgroundTasks
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db, SessionLocal
from ..models import Teacher, Course, Student, Submission, CriterionMark, MarkingInsight
from ..course_access import get_course_for_teacher
from ..auth import get_current_teacher
from ..llm_gateway import complete_json, get_effective_provider
from ..marking_insights import (unit_criteria, match_criterion, criterion_report, next_steps, insight_prompt, clean_insight, deck_gaps,
                                INSIGHT_SYSTEM_PROMPT)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
MARKED_STATES = ("needs_review", "released")


def _units(course):
    spec = course.spec_version.structured_json if course and course.spec_version and course.spec_version.structured_json else {}
    return spec.get("units", [])


def class_marks(db, course, unit_name):
    """One entry per ACTIVE student on the course: their latest marked submission for this unit.
    -> [{"student_id", "name", "submission_id", "grade", "marks": [{"criterion_ref", "final_mark", "comment"}]}]"""
    out = []
    students = db.query(Student).filter(Student.course_id == course.id, Student.is_active.is_(True)).order_by(Student.name).all()
    for student in students:
        sub = (db.query(Submission).filter(Submission.student_id == student.id, Submission.target_unit_name == unit_name,
                                           Submission.status.in_(MARKED_STATES))
               .order_by(Submission.id.desc()).first())
        run = sub.marking_run if sub else None
        if not run:
            continue
        marks = db.query(CriterionMark).filter(CriterionMark.marking_run_id == run.id).all()
        if not marks:
            continue
        out.append({"student_id": student.id, "name": student.name, "submission_id": sub.id, "grade": sub.achieved_grade,
                    "released": sub.status == "released",
                    "marks": [{"criterion_ref": m.criterion_ref, "final_mark": m.final_mark or m.ai_mark,
                               "comment": m.final_comment or m.ai_comment or ""} for m in marks]})
    return out


def units_with_marking(db, course):
    """[(unit index, unit name, number of students marked)] - units that have marked work."""
    out = []
    for i, unit in enumerate(_units(course)):
        n = len(class_marks(db, course, unit.get("name")))
        if n:
            out.append((i, unit.get("name"), n))
    return out


def latest_insight(db, course_id, unit_name):
    return (db.query(MarkingInsight).filter(MarkingInsight.course_id == course_id, MarkingInsight.unit_name == unit_name)
            .order_by(MarkingInsight.id.desc()).first())


def unit_gaps(db, course, unit_index):
    """The class's gaps in one unit's marked work, ready for the next lesson's PowerPoint."""
    units = _units(course)
    if not 0 <= unit_index < len(units):
        return []
    unit = units[unit_index]
    rows = criterion_report(class_marks(db, course, unit.get("name")), unit_criteria(unit))
    insight = latest_insight(db, course.id, unit.get("name"))
    return deck_gaps(unit.get("name"), rows, insight.insight_json if insight and insight.status == "ready" else None)


@router.get("/courses/{course_id}/marking-report", response_class=HTMLResponse)
def marking_report(course_id: int, request: Request, unit: int = -1, db: Session = Depends(get_db),
                   teacher: Teacher = Depends(get_current_teacher)):
    course = get_course_for_teacher(db, course_id, teacher)
    if not course:
        return RedirectResponse(url="/", status_code=303)
    available = units_with_marking(db, course)
    if unit < 0 and available:
        unit = available[0][0]
    units = _units(course)
    context = {"request": request, "teacher": teacher, "course": course, "available": available, "unit_index": unit,
               "error": request.query_params.get("error")}
    if 0 <= unit < len(units):
        spec_unit = units[unit]
        criteria = unit_criteria(spec_unit)
        students = class_marks(db, course, spec_unit.get("name"))
        rows = criterion_report(students, criteria)
        insight = latest_insight(db, course.id, spec_unit.get("name"))
        plans = [{"student": s, "plan": next_steps(s["marks"], criteria)} for s in students]
        context.update({"unit": spec_unit, "rows": rows, "students": students, "plans": plans, "insight": insight,
                        "gaps": [r for r in rows if r["gap"]],
                        # marks whose criterion couldn't be matched to this unit's spec criteria (shown, never hidden)
                        "unmatched": sum(1 for st in students for m in st["marks"] if not match_criterion(m["criterion_ref"], criteria))})
    return templates.TemplateResponse("marking_report.html", context)


@router.post("/courses/{course_id}/marking-report/insight")
def marking_report_insight(course_id: int, background_tasks: BackgroundTasks, unit: int = Form(...),
                           db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    course = get_course_for_teacher(db, course_id, teacher)
    units = _units(course) if course else []
    if not course or not 0 <= unit < len(units):
        return RedirectResponse(url="/", status_code=303)
    back = f"/courses/{course.id}/marking-report?unit={unit}"
    rows = criterion_report(class_marks(db, course, units[unit].get("name")), unit_criteria(units[unit]))
    if not any(r["gap"] for r in rows):
        return RedirectResponse(url=f"{back}&error={quote('No criterion is a class-wide gap, so there is nothing to summarise.')}",
                                status_code=303)
    insight = MarkingInsight(course_id=course.id, teacher_id=teacher.id, unit_name=units[unit].get("name")[:500], status="generating")
    db.add(insight)
    db.commit()
    background_tasks.add_task(_run_insight_job, insight.id, unit)
    return RedirectResponse(url=back, status_code=303)


def _run_insight_job(insight_id: int, unit_index: int):
    db = SessionLocal()
    try:
        insight = db.get(MarkingInsight, insight_id)
        course = db.get(Course, insight.course_id) if insight else None
        if not insight or not course:
            return
        try:
            unit = _units(course)[unit_index]
            students = class_marks(db, course, unit.get("name"))
            rows = criterion_report(students, unit_criteria(unit))
            all_names = [s.name for s in db.query(Student).filter(Student.course_id == course.id).all()]
            result = complete_json(system=INSIGHT_SYSTEM_PROMPT, prompt=insight_prompt(unit.get("name"), rows, all_names),
                                   max_tokens=2500, for_draft_generation=True)
            cleaned = clean_insight(result, rows)
            if not cleaned:
                raise RuntimeError("The AI did not return a usable summary. Try again.")
            insight.insight_json = cleaned
            insight.based_on_count = len(students)
            insight.status = "ready"
            insight.generated_by_provider = get_effective_provider(for_draft_generation=True)
        except Exception as e:
            insight.status = "failed"
            insight.error_detail = f"Could not summarise the common problems: {e}"[:2000]
        db.commit()
    finally:
        db.close()
