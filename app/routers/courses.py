from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from urllib.parse import quote

from ..database import get_db
from ..models import (
    Teacher, Course, SpecVersion, FeedbackStyleTemplate, LessonDeck, Student, Submission,
    CriterionMark, MarkingRun, SubmissionFile, TeacherOverride, IVRequest, IVCriterionCheck,
    AuditLogEntry, SchemeOfWork,
)
from ..coverage import compute_coverage, summarize_coverage
from ..auth import get_current_teacher, verify_password, hash_password, require_admin

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    courses = db.query(Course).filter(Course.teacher_id == teacher.id).all()
    return templates.TemplateResponse(
        "dashboard.html", {"request": request, "teacher": teacher, "courses": courses}
    )


@router.post("/courses/create")
def create_course(
    name: str = Form(...),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    course = Course(name=name, teacher_id=teacher.id)
    db.add(course)
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@router.get("/courses/{course_id}", response_class=HTMLResponse)
def course_detail(
    course_id: int,
    request: Request,
    delete_error: str = "",
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    course = db.query(Course).filter(Course.id == course_id, Course.teacher_id == teacher.id).first()
    if not course:
        return RedirectResponse(url="/", status_code=303)

    try:
        all_specs = db.query(SpecVersion).order_by(SpecVersion.label).all()
        recent_decks = (
            db.query(LessonDeck)
            .filter(LessonDeck.course_id == course_id)
            .order_by(LessonDeck.id.desc())
            .limit(20)
            .all()
        )
        return templates.TemplateResponse(
            "course_detail.html",
            {
                "request": request,
                "teacher": teacher,
                "course": course,
                "all_specs": all_specs,
                "recent_decks": recent_decks,
                "delete_error": delete_error or None,
            },
        )
    except Exception:
        # Never let a data quirk on one specific course raw-crash the page - show what
        # actually broke instead, so it's diagnosable rather than a mystery 500.
        import traceback
        return templates.TemplateResponse(
            "course_detail_error.html",
            {"request": request, "teacher": teacher, "course": course, "error_detail": traceback.format_exc()},
            status_code=500,
        )


@router.post("/courses/{course_id}/delete")
def delete_course(
    course_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """
    Only deletable if it has no real work attached yet - no students, no scheme of work, no
    lesson decks. This is deliberately conservative: a course with any of those represents
    actual usage worth protecting, same reasoning as blocking spec deletion when in use.
    """
    course = db.query(Course).filter(Course.id == course_id, Course.teacher_id == teacher.id).first()
    if not course:
        return RedirectResponse(url="/", status_code=303)

    blockers = []
    if course.students:
        blockers.append(f"{len(course.students)} student(s)")
    if course.schemes_of_work:
        blockers.append(f"{len(course.schemes_of_work)} scheme(s) of work")
    lesson_decks = db.query(LessonDeck).filter(LessonDeck.course_id == course_id).all()
    if lesson_decks:
        blockers.append(f"{len(lesson_decks)} lesson PowerPoint(s)")

    if blockers:
        message = f"Can't delete - this course has {', '.join(blockers)} attached. Remove those first if you really want to delete it."
        return RedirectResponse(url=f"/courses/{course_id}?delete_error={quote(message)}", status_code=303)

    db.delete(course)
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@router.post("/courses/{course_id}/force-delete")
def force_delete_course(
    course_id: int,
    confirm_name: str = Form(...),
    db: Session = Depends(get_db),
    admin: Teacher = Depends(require_admin),
):
    """
    Admin-only, deletes a course and everything under it regardless of students or
    submitted/marked work - this destroys real academic records (submissions, marks,
    override history, IV records) permanently, with no way to recover them. Requires
    typing the exact course name as confirmation, and logs who did it and what was
    destroyed before anything is actually deleted, since that's the only record left
    afterward.
    """
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        return RedirectResponse(url="/", status_code=303)

    if confirm_name.strip() != course.name:
        message = "Course name didn't match exactly - nothing was deleted. Type it precisely to confirm."
        return RedirectResponse(url=f"/courses/{course_id}?delete_error={quote(message)}", status_code=303)

    students = db.query(Student).filter(Student.course_id == course_id).all()
    student_ids = [s.id for s in students]
    submissions = db.query(Submission).filter(Submission.student_id.in_(student_ids)).all() if student_ids else []
    submission_ids = [s.id for s in submissions]

    # Log BEFORE deleting anything - this is the only record left once the data is gone.
    db.add(AuditLogEntry(
        submission_id=None, actor=admin.email, action="force_deleted_course",
        detail=(
            f"Force-deleted course '{course.name}' (id={course.id}): {len(students)} student(s), "
            f"{len(submissions)} submission(s) permanently destroyed."
        ),
    ))
    db.commit()

    # Explicit cascade in dependency order - most relationships here aren't configured
    # with ORM cascade, so this has to be done manually rather than relying on
    # db.delete(course) alone, which would either fail or leave orphaned rows.
    if submission_ids:
        marking_runs = db.query(MarkingRun).filter(MarkingRun.submission_id.in_(submission_ids)).all()
        marking_run_ids = [r.id for r in marking_runs]
        if marking_run_ids:
            criterion_marks = db.query(CriterionMark).filter(CriterionMark.marking_run_id.in_(marking_run_ids)).all()
            criterion_mark_ids = [m.id for m in criterion_marks]
            if criterion_mark_ids:
                db.query(TeacherOverride).filter(TeacherOverride.criterion_mark_id.in_(criterion_mark_ids)).delete(synchronize_session=False)
            db.query(CriterionMark).filter(CriterionMark.marking_run_id.in_(marking_run_ids)).delete(synchronize_session=False)
        db.query(MarkingRun).filter(MarkingRun.submission_id.in_(submission_ids)).delete(synchronize_session=False)

        iv_requests = db.query(IVRequest).filter(IVRequest.submission_id.in_(submission_ids)).all()
        iv_request_ids = [r.id for r in iv_requests]
        if iv_request_ids:
            db.query(IVCriterionCheck).filter(IVCriterionCheck.iv_request_id.in_(iv_request_ids)).delete(synchronize_session=False)
        db.query(IVRequest).filter(IVRequest.submission_id.in_(submission_ids)).delete(synchronize_session=False)

        db.query(SubmissionFile).filter(SubmissionFile.submission_id.in_(submission_ids)).delete(synchronize_session=False)
        db.query(AuditLogEntry).filter(AuditLogEntry.submission_id.in_(submission_ids)).delete(synchronize_session=False)
        db.query(Submission).filter(Submission.id.in_(submission_ids)).delete(synchronize_session=False)

    db.query(Student).filter(Student.course_id == course_id).delete(synchronize_session=False)
    db.query(SchemeOfWork).filter(SchemeOfWork.course_id == course_id).delete(synchronize_session=False)
    db.query(LessonDeck).filter(LessonDeck.course_id == course_id).delete(synchronize_session=False)
    db.commit()

    db.delete(course)
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    custom_feedback_styles = db.query(FeedbackStyleTemplate).order_by(FeedbackStyleTemplate.name).all()
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request, "teacher": teacher, "password_error": None, "password_success": False,
            "custom_feedback_styles": custom_feedback_styles,
        },
    )


@router.post("/settings/style-profile")
def update_style_profile(
    style_profile: str = Form(...),
    feedback_style: str = Form("standard"),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    teacher.style_profile = style_profile
    # Accept the two built-ins OR any custom template name that actually exists - anything
    # else silently falls back to "standard" rather than saving a dangling reference.
    is_custom_valid = db.query(FeedbackStyleTemplate).filter(FeedbackStyleTemplate.name == feedback_style).first()
    teacher.feedback_style = feedback_style if (feedback_style in ("standard", "wwebi") or is_custom_valid) else "standard"
    db.commit()
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/feedback-styles/create")
def create_feedback_style(
    name: str = Form(...),
    instructions: str = Form(...),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """
    Lets any teacher add a new feedback-comment category (e.g. a college-specific format)
    without needing a code change - shared across everyone once added, same pattern as the
    spec library. Silently ignores an exact-name clash rather than erroring, to keep this simple.
    """
    name = name.strip()
    if name and name not in ("standard", "wwebi") and not db.query(FeedbackStyleTemplate).filter(FeedbackStyleTemplate.name == name).first():
        db.add(FeedbackStyleTemplate(name=name, instructions=instructions.strip(), created_by_teacher_id=teacher.id))
        db.commit()
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/change-password", response_class=HTMLResponse)
def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    custom_feedback_styles = db.query(FeedbackStyleTemplate).order_by(FeedbackStyleTemplate.name).all()

    if not verify_password(current_password, teacher.hashed_password):
        return templates.TemplateResponse(
            "settings.html",
            {"request": request, "teacher": teacher, "password_error": "Current password is incorrect.",
             "password_success": False, "custom_feedback_styles": custom_feedback_styles},
        )
    if new_password != confirm_password:
        return templates.TemplateResponse(
            "settings.html",
            {"request": request, "teacher": teacher, "password_error": "New passwords don't match.",
             "password_success": False, "custom_feedback_styles": custom_feedback_styles},
        )
    if len(new_password) < 8:
        return templates.TemplateResponse(
            "settings.html",
            {"request": request, "teacher": teacher, "password_error": "New password must be at least 8 characters.",
             "password_success": False, "custom_feedback_styles": custom_feedback_styles},
        )

    teacher.hashed_password = hash_password(new_password)
    db.commit()
    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "teacher": teacher, "password_error": None, "password_success": True,
         "custom_feedback_styles": custom_feedback_styles},
    )


@router.get("/courses/{course_id}/coverage", response_class=HTMLResponse)
def coverage_dashboard(
    course_id: int,
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """
    Shows which criteria in the attached spec have never been taught (per the scheme of
    work), taught but never assessed, or assessed - plus which assessed criteria show more
    not-met than met results across the cohort. Uses only data already in the database -
    no AI calls, so this is free and fast to run as often as you like.

    Teaching coverage is tracked at the learning-outcome level, not per individual P/M/D
    criterion, since that's the level a scheme of work actually plans at - stated plainly
    on the page itself, not just here.
    """
    course = db.query(Course).filter(Course.id == course_id, Course.teacher_id == teacher.id).first()
    if not course:
        return RedirectResponse(url="/", status_code=303)

    has_parsed_spec = bool(course.spec_version and course.spec_version.structured_json)
    if not has_parsed_spec:
        return templates.TemplateResponse(
            "coverage_dashboard.html",
            {"request": request, "teacher": teacher, "course": course, "has_parsed_spec": False,
             "report": [], "summary": None},
        )

    units = course.spec_version.structured_json.get("units", [])

    schemes_of_work_data = [
        {"status": sow.status, "selected_unit_names": sow.selected_unit_names, "weeks_json": sow.weeks_json}
        for sow in course.schemes_of_work
    ]

    student_ids = [s.id for s in db.query(Student).filter(Student.course_id == course_id).all()]
    submissions = db.query(Submission).filter(Submission.student_id.in_(student_ids)).all() if student_ids else []
    submission_target_units = {s.id: s.target_unit_name for s in submissions}
    submission_ids = [s.id for s in submissions]

    marks_data = []
    marking_runs = db.query(MarkingRun).filter(MarkingRun.submission_id.in_(submission_ids)).all() if submission_ids else []
    marking_run_to_submission = {r.id: r.submission_id for r in marking_runs}
    if marking_runs:
        run_ids = list(marking_run_to_submission.keys())
        all_marks = db.query(CriterionMark).filter(CriterionMark.marking_run_id.in_(run_ids)).all()
        for m in all_marks:
            sub_id = marking_run_to_submission.get(m.marking_run_id)
            marks_data.append({
                "criterion_ref": m.criterion_ref,
                "final_mark": m.final_mark,
                "target_unit_name": submission_target_units.get(sub_id),
            })

    report = compute_coverage(units, schemes_of_work_data, marks_data)
    summary = summarize_coverage(report)

    return templates.TemplateResponse(
        "coverage_dashboard.html",
        {"request": request, "teacher": teacher, "course": course, "has_parsed_spec": True,
         "report": report, "summary": summary},
    )
