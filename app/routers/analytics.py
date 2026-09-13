import json
from collections import defaultdict
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Teacher, Course, Student, Submission, CriterionMark
from ..course_access import get_courses_for_teacher
from ..auth import get_current_teacher

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _aggregate(db: Session, submissions: list) -> dict:
    """Shared aggregation logic - used identically whether looking at all courses, one
    course, or one student, so the numbers are calculated the same way everywhere."""
    total_submissions = len(submissions)
    marked_submissions = [s for s in submissions if s.status in ("needs_review", "released")]

    all_marks = []
    for s in marked_submissions:
        if s.marking_run:
            all_marks.extend(
                db.query(CriterionMark).filter(CriterionMark.marking_run_id == s.marking_run.id).all()
            )

    mark_distribution = defaultdict(int)
    confidence_values = []
    overridden_count = 0
    per_criterion_override = defaultdict(lambda: {"total": 0, "overridden": 0})

    for m in all_marks:
        mark_distribution[m.final_mark or "unmarked"] += 1
        if m.ai_confidence is not None:
            confidence_values.append(m.ai_confidence)
        per_criterion_override[m.criterion_ref]["total"] += 1
        if m.overridden:
            overridden_count += 1
            per_criterion_override[m.criterion_ref]["overridden"] += 1

    avg_confidence = round(sum(confidence_values) / len(confidence_values) * 100, 1) if confidence_values else None
    override_rate = round(overridden_count / len(all_marks) * 100, 1) if all_marks else None

    criterion_override_table = sorted(
        [
            {
                "criterion": ref,
                "total": data["total"],
                "overridden": data["overridden"],
                "rate": round(data["overridden"] / data["total"] * 100, 1) if data["total"] else 0,
            }
            for ref, data in per_criterion_override.items()
        ],
        key=lambda x: x["rate"],
        reverse=True,
    )

    status_counts = defaultdict(int)
    for s in submissions:
        status_counts[s.status] += 1

    chart_data = {
        "mark_distribution": {"labels": list(mark_distribution.keys()), "values": list(mark_distribution.values())},
        "status_counts": {"labels": list(status_counts.keys()), "values": list(status_counts.values())},
    }

    return {
        "total_submissions": total_submissions,
        "marked_count": len(marked_submissions),
        "avg_confidence": avg_confidence,
        "override_rate": override_rate,
        "criterion_override_table": criterion_override_table,
        "chart_data_json": json.dumps(chart_data),
    }


@router.get("/analytics", response_class=HTMLResponse)
def analytics_dashboard(
    request: Request,
    course_id: int = 0,
    student_id: int = 0,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    courses = get_courses_for_teacher(db, teacher)
    course_ids = [c.id for c in courses]

    # Scope submissions: everything across your courses by default, narrowed to one course
    # if selected, narrowed further to one student within that course if also selected.
    selected_course = next((c for c in courses if c.id == course_id), None) if course_id else None
    students_in_selected_course = selected_course.students if selected_course else []
    selected_student = next((s for s in students_in_selected_course if s.id == student_id), None) if student_id else None

    if selected_student:
        scope_course_ids = [selected_student.course_id]
        submissions = [s for s in db.query(Submission).all() if s.student_id == selected_student.id]
    elif selected_course:
        submissions = [s for s in db.query(Submission).all() if s.student and s.student.course_id == selected_course.id]
    else:
        submissions = [s for s in db.query(Submission).all() if s.student and s.student.course_id in course_ids]

    aggregated = _aggregate(db, submissions)

    return templates.TemplateResponse(
        "analytics.html",
        {
            "request": request,
            "teacher": teacher,
            "courses": courses,
            "selected_course": selected_course,
            "selected_student": selected_student,
            "students_in_selected_course": students_in_selected_course,
            **aggregated,
        },
    )
