import datetime
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from urllib.parse import quote

from ..database import get_db
from ..models import (
    Teacher, Course, CourseTeacher, SpecVersion, FeedbackStyleTemplate, LessonDeck, Student, Submission,
    CriterionMark, MarkingRun, SubmissionFile, TeacherOverride, IVRequest, IVCriterionCheck,
    AuditLogEntry, SchemeOfWork, CourseUnitRequirement, TimetableSession, ResourceShare, LiveSession,
    LiveParticipant, LiveResponse, PracticeSet,
)
from ..spec_context import has_exam_objectives
from ..course_access import get_course_for_teacher, get_courses_for_teacher
from ..coverage import compute_coverage, summarize_coverage
from ..auth import get_current_teacher, verify_password, hash_password, require_admin
from ..system_settings import get_system_settings
from ..email_sender import is_configured as email_is_configured

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    courses = get_courses_for_teacher(db, teacher)
    joined_ids = {c.id for c in courses}
    joinable_courses = [c for c in db.query(Course).order_by(Course.name).all() if c.id not in joined_ids]
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "teacher": teacher, "courses": courses, "joinable_courses": joinable_courses},
    )


@router.post("/courses/join")
def join_course(
    course_id: int = Form(...),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """
    Self-service: a teacher picks an existing, admin-created course they're actually
    teaching, rather than each teacher creating their own separate copy of the same real
    cohort - the fix for the original problem (two lecturers covering different content
    areas of the same course each ending up with their own disconnected record).
    """
    course = db.get(Course, course_id)
    if not course:
        return RedirectResponse(url="/", status_code=303)
    already = db.query(CourseTeacher).filter(
        CourseTeacher.course_id == course_id, CourseTeacher.teacher_id == teacher.id
    ).first()
    if not already:
        db.add(CourseTeacher(course_id=course_id, teacher_id=teacher.id))
        db.commit()
    return RedirectResponse(url="/", status_code=303)


@router.post("/courses/{course_id}/leave")
def leave_course(
    course_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """A teacher no longer teaching part of a course removes themselves - the other
    teachers still on it keep their own access untouched, since membership is per-teacher,
    not shared state that one person's departure could affect."""
    membership = db.query(CourseTeacher).filter(
        CourseTeacher.course_id == course_id, CourseTeacher.teacher_id == teacher.id
    ).first()
    if membership:
        db.delete(membership)
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
    course = get_course_for_teacher(db, course_id, teacher)
    if not course:
        return RedirectResponse(url="/", status_code=303)

    try:
        all_specs = db.query(SpecVersion).order_by(SpecVersion.label).all()
        # all of the course's lessons (newest first) - the page shows 6 and searches the rest in the browser
        recent_decks = (
            db.query(LessonDeck)
            .filter(LessonDeck.course_id == course_id)
            .order_by(LessonDeck.id.desc())
            .limit(300)
            .all()
        )
        # Stage 6: live lessons this lecturer ran on this course, and whether the spec is exam-assessed
        live_sessions = (
            db.query(LiveSession)
            .filter(LiveSession.course_id == course_id, LiveSession.teacher_id == teacher.id)
            .order_by(LiveSession.id.desc())
            .limit(15)
            .all()
        )
        # how many answers each listed session holds - deleting one with answers asks twice
        from sqlalchemy import func
        live_answer_counts = dict(db.query(LiveResponse.session_id, func.count(LiveResponse.id))
                                  .filter(LiveResponse.session_id.in_([s.id for s in live_sessions] or [0]))
                                  .group_by(LiveResponse.session_id).all())
        from ..models import QuizSet
        quizzes = db.query(QuizSet).filter(QuizSet.course_id == course_id).order_by(QuizSet.id.desc()).limit(10).all()
        spec_json = course.spec_version.structured_json if course.spec_version and course.spec_version.structured_json else {}
        overview = _course_overview(db, course, teacher, recent_decks)
        from .homework import homework_summaries
        homeworks = homework_summaries(db, course.id, limit=6)
        return templates.TemplateResponse(
            "course_detail.html",
            {
                "request": request,
                "teacher": teacher,
                "course": course,
                "all_specs": all_specs,
                "recent_decks": recent_decks,
                "delete_error": delete_error or None,
                "live_sessions": live_sessions,
                "quizzes": quizzes,
                "live_answer_counts": live_answer_counts,
                "is_exam_spec": has_exam_objectives(spec_json),
                "overview": overview,
                "homeworks": homeworks,
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


def _course_overview(db, course, teacher, decks):
    """The course page's "needs you" numbers and "This week" card. Each part is worked out on its
    own and a problem in one never stops the page loading - it just leaves that part out."""
    from ..course_overview import this_week
    out = {"to_review": 0, "review_next": None, "at_risk": [], "open_live": None, "this_week": None, "met_pct": None,
           "running_plan": None, "plan_ready": 0, "plan_total": 0}
    try:  # Plan the term still making lessons
        from ..models import TermPlan
        plan = (db.query(TermPlan).filter(TermPlan.course_id == course.id, TermPlan.state == "running")
                .order_by(TermPlan.id.desc()).first())
        if plan:
            ids = [i["deck_id"] for i in plan.items_json or [] if i.get("deck_id")]
            out["running_plan"], out["plan_total"] = plan, len(ids)
            out["plan_ready"] = sum(1 for d in decks if d.id in ids and d.status == "ready")
    except Exception:
        pass
    waiting = (db.query(Submission.id).join(Student, Submission.student_id == Student.id)
               .filter(Student.course_id == course.id, Student.is_active.is_(True), Submission.status == "needs_review")
               .order_by(Submission.id).all())
    out["to_review"] = len(waiting)
    out["review_next"] = waiting[0][0] if waiting else None
    out["open_live"] = (db.query(LiveSession).filter(LiveSession.course_id == course.id, LiveSession.teacher_id == teacher.id,
                                                     LiveSession.state.in_(("open", "paused")), LiveSession.started_at.isnot(None))
                        .order_by(LiveSession.id.desc()).first())
    try:
        from .tracker import course_tracker
        tracked = course_tracker(db, course)
        out["at_risk"] = [r["student"]["name"] for r in tracked["rows"] if r["risk"]]
        cells = [c for r in tracked["rows"] for c in r["cells"].values() if c["has_work"]]
        assessed = sum(c["assessed"] for c in cells)
        out["met_pct"] = round(100 * sum(c["met"] for c in cells) / assessed) if assessed else None  # class average, % of marked criteria met
    except Exception:
        pass
    try:
        from ..sow_calendar import scheme_now
        from ..pace import resume_slide
        schemes = []
        for s in course.schemes_of_work:
            entry = {"id": s.id, "title": s.title, "status": s.status, "start_date": s.start_date, "weeks": s.weeks_json or []}
            if s.status == "ready" and s.weeks_json:
                now, _cal = scheme_now(s, course.start_date)  # college calendar, corrections and carried-on weeks
                entry.update(now=now, corrected=bool(s.week_anchor_json),
                             resume={(now["week"], now.get("part", 1)): resume_slide(s.pace_json, now["week"], now.get("part", 1))})
            schemes.append(entry)
        out["this_week"] = this_week(schemes, course.start_date, [
            {"id": d.id, "status": d.status, "source_sow_id": d.source_sow_id, "source_week": d.source_week} for d in decks],
            datetime.date.today())
    except Exception:
        pass
    return out


def _delete_live_and_practice(db, course_id: int):
    """Stage 6: a course's live lesson sessions (with the students' nicknames and answers)
    and its practice question sets go with the course - nothing about students is left behind."""
    session_ids = [s.id for s in db.query(LiveSession).filter(LiveSession.course_id == course_id).all()]
    import shutil
    for sid in session_ids:  # photos students sent with answers
        shutil.rmtree(f"storage/live_uploads/{int(sid)}", ignore_errors=True)
    if session_ids:
        db.query(LiveResponse).filter(LiveResponse.session_id.in_(session_ids)).delete(synchronize_session=False)
        db.query(LiveParticipant).filter(LiveParticipant.session_id.in_(session_ids)).delete(synchronize_session=False)
        db.query(LiveSession).filter(LiveSession.id.in_(session_ids)).delete(synchronize_session=False)
    db.query(PracticeSet).filter(PracticeSet.course_id == course_id).delete(synchronize_session=False)
    from ..models import QuizSet, MarkingInsight, Homework, HomeworkAttempt, TermPlan  # Stage 8, Stage 10, Stage 14
    hw_ids = [h.id for h in db.query(Homework).filter(Homework.course_id == course_id).all()]
    if hw_ids:  # homework and every student's answers to it
        db.query(HomeworkAttempt).filter(HomeworkAttempt.homework_id.in_(hw_ids)).delete(synchronize_session=False)
        db.query(Homework).filter(Homework.id.in_(hw_ids)).delete(synchronize_session=False)
    db.query(TermPlan).filter(TermPlan.course_id == course_id).delete(synchronize_session=False)
    db.query(QuizSet).filter(QuizSet.course_id == course_id).delete(synchronize_session=False)
    db.query(MarkingInsight).filter(MarkingInsight.course_id == course_id).delete(synchronize_session=False)


@router.post("/courses/{course_id}/delete")
def delete_course(
    course_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """
    Only deletable if it has no real work attached yet - no students, no scheme of work, no
    lesson decks, no timetabling data. This is deliberately conservative: a course with any
    of those represents actual usage worth protecting, same reasoning as blocking spec
    deletion when in use.

    CourseTeacher membership rows for this course ARE cleaned up here even though they're
    not treated as a blocker - a course with just one teacher joined and nothing else done
    yet is still "empty" in every real sense, but db.delete(course) alone won't remove
    those membership rows (no ORM cascade is configured), which would otherwise be left
    silently pointing at a course_id that no longer exists.
    """
    course = get_course_for_teacher(db, course_id, teacher)
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
    requirements = db.query(CourseUnitRequirement).filter(CourseUnitRequirement.course_id == course_id).all()
    if requirements:
        blockers.append(f"{len(requirements)} timetabling requirement(s)")
    sessions = db.query(TimetableSession).filter(TimetableSession.course_id == course_id).all()
    if sessions:
        blockers.append(f"{len(sessions)} scheduled session(s)")

    if blockers:
        message = f"Can't delete - this course has {', '.join(blockers)} attached. Remove those first if you really want to delete it."
        return RedirectResponse(url=f"/courses/{course_id}?delete_error={quote(message)}", status_code=303)

    _delete_live_and_practice(db, course_id)
    db.query(CourseTeacher).filter(CourseTeacher.course_id == course_id).delete(synchronize_session=False)

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

    deck_ids = [d.id for d in db.query(LessonDeck).filter(LessonDeck.course_id == course_id).all()]
    if deck_ids:
        db.query(ResourceShare).filter(ResourceShare.lesson_deck_id.in_(deck_ids)).delete(synchronize_session=False)

    db.query(Student).filter(Student.course_id == course_id).delete(synchronize_session=False)
    db.query(SchemeOfWork).filter(SchemeOfWork.course_id == course_id).delete(synchronize_session=False)
    db.query(LessonDeck).filter(LessonDeck.course_id == course_id).delete(synchronize_session=False)
    _delete_live_and_practice(db, course_id)
    db.query(CourseTeacher).filter(CourseTeacher.course_id == course_id).delete(synchronize_session=False)
    db.query(CourseUnitRequirement).filter(CourseUnitRequirement.course_id == course_id).delete(synchronize_session=False)
    db.query(TimetableSession).filter(TimetableSession.course_id == course_id).delete(synchronize_session=False)
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
    system_settings = get_system_settings(db)
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request, "teacher": teacher, "password_error": None, "password_success": False,
            "custom_feedback_styles": custom_feedback_styles,
            "system_two_factor_mandatory": system_settings.two_factor_mandatory,
        },
    )


@router.post("/settings/two-factor")
def update_two_factor(
    two_factor_enabled: str = Form(""),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    wants_enabled = bool(two_factor_enabled)
    # Standing fix, after a real lockout: turning this on when the server can't actually
    # send the code either strands the person immediately, or (with the fail-open fix in
    # login) silently weakens their own security without them realising why. Refuse at the
    # point of enabling instead, with a clear reason, rather than letting either happen.
    if wants_enabled and not email_is_configured():
        return RedirectResponse(url="/settings?error=smtp_not_configured", status_code=303)
    teacher.two_factor_enabled = wants_enabled
    db.add(AuditLogEntry(
        actor=teacher.email, action="two_factor_enabled" if wants_enabled else "two_factor_disabled",
        detail=f"{teacher.email} turned two-factor login {'on' if wants_enabled else 'off'} for their own account.",
    ))
    db.commit()
    return RedirectResponse(url="/settings", status_code=303)


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
    system_two_factor_mandatory = get_system_settings(db).two_factor_mandatory

    if not verify_password(current_password, teacher.hashed_password):
        return templates.TemplateResponse(
            "settings.html",
            {"request": request, "teacher": teacher, "password_error": "Current password is incorrect.",
             "password_success": False, "custom_feedback_styles": custom_feedback_styles,
             "system_two_factor_mandatory": system_two_factor_mandatory},
        )
    if new_password != confirm_password:
        return templates.TemplateResponse(
            "settings.html",
            {"request": request, "teacher": teacher, "password_error": "New passwords don't match.",
             "password_success": False, "custom_feedback_styles": custom_feedback_styles,
             "system_two_factor_mandatory": system_two_factor_mandatory},
        )
    if len(new_password) < 8:
        return templates.TemplateResponse(
            "settings.html",
            {"request": request, "teacher": teacher, "password_error": "New password must be at least 8 characters.",
             "password_success": False, "custom_feedback_styles": custom_feedback_styles,
             "system_two_factor_mandatory": system_two_factor_mandatory},
        )

    teacher.hashed_password = hash_password(new_password)
    db.commit()
    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "teacher": teacher, "password_error": None, "password_success": True,
         "custom_feedback_styles": custom_feedback_styles,
         "system_two_factor_mandatory": system_two_factor_mandatory},
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
    course = get_course_for_teacher(db, course_id, teacher)
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

    report = compute_coverage(units, schemes_of_work_data, marks_data, course_start_date=course.start_date)
    summary = summarize_coverage(report)

    return templates.TemplateResponse(
        "coverage_dashboard.html",
        {"request": request, "teacher": teacher, "course": course, "has_parsed_spec": True,
         "report": report, "summary": summary},
    )


@router.post("/courses/{course_id}/start-date")
def set_course_start_date(
    course_id: int,
    start_date: str = Form(""),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """
    Sets (or clears) when teaching actually begins for this course - the anchor point
    "taught, not assessed" tracking needs to work out which weeks of a scheme of work have
    genuinely happened yet. Clearing the field is a valid, deliberate choice (falls back
    to the old, less precise "every week counts" behavior), not an error.
    """
    course = get_course_for_teacher(db, course_id, teacher)
    if not course:
        return RedirectResponse(url="/", status_code=303)
    if not start_date:
        course.start_date = None
    else:
        try:
            course.start_date = datetime.date.fromisoformat(start_date)
        except ValueError:
            pass
    db.commit()
    return RedirectResponse(url=f"/courses/{course_id}/coverage", status_code=303)
