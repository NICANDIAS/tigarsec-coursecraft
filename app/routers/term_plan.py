"""
Plan the term in one click: pick weeks of a scheme of work, and CourseCraft makes each week's
PowerPoint (with support/stretch versions and a quiz if wanted) ONE AFTER ANOTHER in the
background - the same proven job as "Generate PowerPoint for this week", run for many weeks.

One at a time on purpose: it never floods the AI provider (rate limits, a busy campus server).
Every week's lesson row is created up front as "queued", so if the app restarts mid-way the
progress page offers "Continue" and nothing is lost or made twice.
"""
import datetime
import io
import os
import re
import tempfile
import threading
import zipfile

from fastapi import APIRouter, Request, Depends, Form, BackgroundTasks
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db, SessionLocal
from ..models import Teacher, Course, LessonDeck, SchemeOfWork, QuizSet, TermPlan
from ..course_access import get_course_for_teacher
from ..auth import get_current_teacher
from ..course_overview import monday
from ..sow_calendar import scheme_now
from ..homework import local_now
from ..term_plan import plan_rows, timeline_rows, quick_pick, estimate_minutes, clamp_minutes, clamp_quiz, item_status, progress, MAX_WEEKS
from . import presentations as pr
from . import quizzes as qz

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

RUNNING = set()            # plan ids a worker is making right now (one app process)
_running_lock = threading.Lock()


# ------------------------------------------------------------------ helpers
def _units(course):
    spec = course.spec_version.structured_json if course and course.spec_version and course.spec_version.structured_json else {}
    return spec, spec.get("units", []) or []


def _made_weeks(db, sow):
    """Weeks of this scheme that already have a lesson (not counting failed ones)."""
    rows = db.query(LessonDeck.source_week, LessonDeck.status).filter(LessonDeck.source_sow_id == sow.id).all()
    return {w for w, status in rows if w is not None and status != "failed"}


def _current_week(sow, course, today=None):
    """This week's number (or the next teaching week in a break), the full answer, and the calendar."""
    now, cal = scheme_now(sow, course.start_date, today)
    return (now["week"] if now["state"] in ("teaching", "break", "before") else None), now, cal


def _merge_ideas(sow, now):
    from ..pace import suggest_merges
    return suggest_merges(sow.weeks_json or [], now.get("week") or 0, sow.pace_json,
                          [p.get("week") for p in (sow.planned_assessments or []) if isinstance(p, dict)])


def pace_changes(db, sow, course):
    """The carried-on and combined weeks on this scheme, for the "Changes to the plan" list (each can be undone)."""
    from ..pace import get_pace
    pace = get_pace(sow.pace_json)
    titles = {w["week"]: w.get("title", "") for w in sow.weeks_json or [] if isinstance(w, dict) and isinstance(w.get("week"), int)}
    out = []
    for i, e in enumerate(pace["extensions"]):
        out.append({"kind": "extension", "index": i, "week": e["week"], "title": titles.get(e["week"], ""),
                    "from_slide": e.get("from_slide"), "reason": e.get("reason"), "at": e.get("at")})
    for a, b in pace["merges"]:
        out.append({"kind": "merge", "a": a, "b": b, "title": f"{titles.get(a, '')} + {titles.get(b, '')}"})
    return out


def _own_plan(db, plan_id, teacher):
    plan = db.get(TermPlan, plan_id)
    if not plan or not get_course_for_teacher(db, plan.course_id, teacher):
        return None
    return plan


def _queue_week(db, course, sow, teacher, week, options):
    """Creates one week's lesson row as "queued" (made later by the worker). -> the deck, or None
    if the week can't be matched to a unit of the spec."""
    spec, units = _units(course)
    pick = pr.sow_week_selection(units, sow, week)
    if not _usable(pick):
        return None
    unit = units[pick["unit_index"]]
    deck = LessonDeck(
        course_id=course.id, teacher_id=teacher.id,
        content_area_name=f"{unit.get('name', 'Unit')} — week {week}",
        title=(pick["title"] or f"Week {week}")[:255],
        duration_minutes=options["minutes"],
        previous_lesson_context=(pick["previous_context"] if options.get("join_up") else "") or None,
        include_practicals=bool(options.get("practicals")),
        unit_index=pick["unit_index"],
        selected_lo_indices=pick["lo_indices"] or None,
        selected_criteria_by_lo=pick["criteria_selected"] or None,
        source_sow_id=sow.id, source_week=week, status="queued",
    )
    db.add(deck)
    db.flush()
    return deck


def _usable(pick):
    """A week can be planned only when it matched a unit AND some of its outcomes/criteria - otherwise
    (e.g. "Assessment week") the lesson would be written from the whole unit, which isn't what was meant."""
    return bool(pick and pick["unit_index"] is not None and (pick["lo_indices"] or pick["criteria_selected"]))


def _start(background_tasks, plan_id):
    # claimed BEFORE the page reloads, so the progress page never mistakes "about to start" for "stopped by a restart"
    with _running_lock:
        if plan_id in RUNNING:
            return
        RUNNING.add(plan_id)
    background_tasks.add_task(run_plan, plan_id, True)


# ------------------------------------------------------------------ the worker
def _next_step(db, plan):
    """The next thing to make: ("deck", item, deck) or ("quiz", item, deck), or None when done."""
    opts = plan.options_json or {}
    for item in plan.items_json or []:
        deck = db.get(LessonDeck, item["deck_id"]) if item.get("deck_id") else None
        if not deck:
            continue
        # "generating" here means a restart cut it off - this worker owns the plan, so make it again
        if deck.status in ("queued", "generating"):
            return "deck", item, deck
        if deck.status == "ready" and opts.get("quiz"):
            quiz = db.get(QuizSet, item["quiz_id"]) if item.get("quiz_id") else None
            if quiz is None or quiz.status == "generating":
                return "quiz", item, deck
    return None


def run_plan(plan_id: int, claimed: bool = False):
    """Makes the plan's lessons and quizzes one after another until done or stopped.
    claimed=True: _start already marked the plan as running."""
    if not claimed:
        with _running_lock:
            if plan_id in RUNNING:
                return
            RUNNING.add(plan_id)
    try:
        for _ in range(MAX_WEEKS * 3):  # a hard ceiling - each pass makes one lesson or one quiz
            db = SessionLocal()
            try:
                plan = db.get(TermPlan, plan_id)
                if not plan or plan.state != "running":
                    return
                step = _next_step(db, plan)
                if step is None:
                    plan.state = "done"
                    db.commit()
                    return
                kind, item, deck = step
                opts = plan.options_json or {}
                if kind == "deck":
                    course = db.get(Course, plan.course_id)
                    spec, units = _units(course)
                    if deck.unit_index is None or not (0 <= deck.unit_index < len(units)):
                        deck.status, deck.error_detail = "failed", "This week's unit is no longer in the course specification."
                        db.commit()
                        continue
                    content = pr._deck_content_text(spec, units[deck.unit_index], deck.selected_lo_indices or [],
                                                    {int(k): v for k, v in (deck.selected_criteria_by_lo or {}).items()})
                    deck.status, deck.error_detail = "generating", None
                    db.commit()
                    job = (deck.id, content, deck.duration_minutes, deck.previous_lesson_context or "",
                           bool(deck.include_practicals), bool(opts.get("differentiate")))
                else:
                    quiz = db.get(QuizSet, item["quiz_id"]) if item.get("quiz_id") else None
                    if quiz is None:
                        quiz = QuizSet(course_id=plan.course_id, teacher_id=plan.teacher_id, lesson_deck_id=deck.id,
                                       title=deck.title[:255], questions_json=[], seconds_per_question=20, status="generating")
                        db.add(quiz)
                        db.flush()
                        plan.items_json = [dict(i, quiz_id=quiz.id) if i is item else i for i in plan.items_json]
                        db.commit()
                    job = (quiz.id, clamp_quiz(opts.get("quiz_count")))
            finally:
                db.close()
            # the long AI work runs with no database session held open
            if kind == "deck":
                pr._run_generation_job(*job)
            else:
                qz._run_quiz_job(*job)
    finally:
        RUNNING.discard(plan_id)


# ------------------------------------------------------------------ pages
@router.get("/courses/{course_id}/scheme-of-work/{sow_id}/plan-term", response_class=HTMLResponse)
def plan_term_page(course_id: int, sow_id: int, request: Request, db: Session = Depends(get_db),
                   teacher: Teacher = Depends(get_current_teacher)):
    course = get_course_for_teacher(db, course_id, teacher)
    sow = db.get(SchemeOfWork, sow_id)
    if not course or not sow or sow.course_id != course.id:
        return RedirectResponse(url="/", status_code=303)
    running = (db.query(TermPlan).filter(TermPlan.sow_id == sow.id, TermPlan.state == "running")
               .order_by(TermPlan.id.desc()).first())
    spec, units = _units(course)
    current, now, cal = _current_week(sow, course)
    rows = timeline_rows(cal["timeline"], sow.weeks_json or [], now, _made_weeks(db, sow), datetime.date.today(),
                         sow.pace_json) if sow.status == "ready" else []
    for r in rows:
        if not r["gap"]:
            r["no_unit"] = not _usable(pr.sow_week_selection(units, sow, r["week"]))
    usable = [r for r in rows if not r["gap"] and not r.get("no_unit")]
    picks = {p: [w for w in quick_pick([dict(r, made=r.get("made") or r.get("no_unit", False)) for r in rows], current, p)]
             for p in ("next4", "half", "rest")}
    hours = sow.hours_per_week if sow.hours_per_week and sow.hours_per_week <= 6 else 1
    return templates.TemplateResponse("term_plan_new.html", {
        "request": request, "teacher": teacher, "course": course, "sow": sow, "rows": rows, "current": current,
        "picks": picks, "default_hours": hours, "has_spec": bool(units), "usable": len(usable), "running": running,
        "error": request.query_params.get("error"), "estimate_each": estimate_minutes(1, True, True),
        "now": now, "from_template": cal["from_template"], "changes": pace_changes(db, sow, course), "overflow": cal["overflow"],
        "merge_ideas": _merge_ideas(sow, now) if cal["overflow"] else []})


@router.post("/courses/{course_id}/scheme-of-work/{sow_id}/this-week")
def set_this_week(course_id: int, sow_id: int, week: str = Form(""), back: str = Form(""), db: Session = Depends(get_db),
                  teacher: Teacher = Depends(get_current_teacher), clear: str = Form("")):
    """The lecturer's correction: "we're actually in week N". Saved against this Monday and counted on
    from there each week. An empty week clears it (back to the college calendar / start date)."""
    course = get_course_for_teacher(db, course_id, teacher)
    sow = db.get(SchemeOfWork, sow_id)
    if course and sow and sow.course_id == course.id:
        if not clear and week.strip().isdigit() and 1 <= int(week) <= 60:
            sow.week_anchor_json = {"week": int(week), "monday": monday(local_now().date()).isoformat()}
        else:
            sow.week_anchor_json = None
        db.commit()
    target = back if back.startswith("/courses/") else f"/courses/{course_id}/scheme-of-work/{sow_id}/plan-term"
    return RedirectResponse(url=target, status_code=303)


@router.post("/courses/{course_id}/scheme-of-work/{sow_id}/plan-term")
async def plan_term_start(course_id: int, sow_id: int, request: Request, background_tasks: BackgroundTasks,
                          db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    course = get_course_for_teacher(db, course_id, teacher)
    sow = db.get(SchemeOfWork, sow_id)
    if not course or not sow or sow.course_id != course.id or sow.status != "ready":
        return RedirectResponse(url="/", status_code=303)
    running = db.query(TermPlan).filter(TermPlan.sow_id == sow.id, TermPlan.state == "running").first()
    if running:  # one plan per scheme at a time - pressing the button twice never doubles the work
        return RedirectResponse(url=f"/term-plans/{running.id}", status_code=303)
    form = await request.form()
    options = {"minutes": clamp_minutes(form.get("hours")), "differentiate": bool(form.get("differentiate")),
               "quiz": bool(form.get("quiz")), "quiz_count": clamp_quiz(form.get("quiz_count")),
               "practicals": bool(form.get("practicals")), "join_up": bool(form.get("join_up"))}
    made = _made_weeks(db, sow)
    wanted = sorted({int(w) for w in form.getlist("weeks") if str(w).isdigit()} - made)[:MAX_WEEKS]
    items = []
    for week in wanted:
        deck = _queue_week(db, course, sow, teacher, week, options)
        if deck:
            items.append({"week": week, "title": deck.title, "deck_id": deck.id, "quiz_id": None})
    if not items:
        db.rollback()
        return RedirectResponse(url=f"/courses/{course.id}/scheme-of-work/{sow.id}/plan-term?error=Tick+at+least+one+week+to+plan.",
                                status_code=303)
    plan = TermPlan(course_id=course.id, teacher_id=teacher.id, sow_id=sow.id, options_json=options, items_json=items, state="running")
    db.add(plan)
    db.commit()
    _start(background_tasks, plan.id)
    return RedirectResponse(url=f"/term-plans/{plan.id}", status_code=303)


def plan_status(db, plan):
    """Everything the progress page shows, read fresh from the lesson and quiz rows."""
    opts = plan.options_json or {}
    rows, states = [], []
    for item in plan.items_json or []:
        deck = db.get(LessonDeck, item["deck_id"]) if item.get("deck_id") else None
        quiz = db.get(QuizSet, item["quiz_id"]) if item.get("quiz_id") else None
        state = item_status(deck.status if deck else None, quiz.status if quiz else None, bool(opts.get("quiz")))
        states.append(state)
        rows.append({"week": item["week"], "title": deck.title if deck else item.get("title"), "state": state,
                     "deck_id": deck.id if deck else None, "deck_status": deck.status if deck else None,
                     "deck_error": (deck.error_detail if deck and deck.status == "failed" else None),
                     "quiz_id": quiz.id if quiz else None, "quiz_status": quiz.status if quiz else None,
                     "stopped": bool(item.get("stopped"))})
    prog = progress(states)
    left = sum(1 for s in states if s in ("waiting", "working"))
    low, high = estimate_minutes(left, bool(opts.get("quiz")), bool(opts.get("differentiate")))
    return {"state": plan.state, "alive": plan.id in RUNNING, "rows": rows, "progress": prog,
            "minutes_left": [low, high] if left else None}


@router.get("/term-plans/{plan_id}", response_class=HTMLResponse)
def plan_page(plan_id: int, request: Request, db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    plan = _own_plan(db, plan_id, teacher)
    if not plan:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse("term_plan.html", {
        "request": request, "teacher": teacher, "plan": plan, "course": db.get(Course, plan.course_id),
        "sow": db.get(SchemeOfWork, plan.sow_id), "status": plan_status(db, plan), "opts": plan.options_json or {}})


@router.get("/term-plans/{plan_id}/status")
def plan_status_json(plan_id: int, db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    plan = _own_plan(db, plan_id, teacher)
    if not plan:
        return JSONResponse({"error": "not found"}, status_code=404)
    return plan_status(db, plan)


@router.post("/term-plans/{plan_id}/stop")
def plan_stop(plan_id: int, db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    """Stops after the lesson being made now. Weeks not started yet are removed (they have no content)."""
    plan = _own_plan(db, plan_id, teacher)
    if not plan:
        return RedirectResponse(url="/", status_code=303)
    items = []
    for item in plan.items_json or []:
        deck = db.get(LessonDeck, item["deck_id"]) if item.get("deck_id") else None
        if deck and deck.status == "queued":
            db.delete(deck)
            item = dict(item, deck_id=None, stopped=True)
        items.append(item)
    plan.items_json = items
    plan.state = "stopped"
    db.commit()
    return RedirectResponse(url=f"/term-plans/{plan.id}", status_code=303)


@router.post("/term-plans/{plan_id}/continue")
def plan_continue(plan_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db),
                  teacher: Teacher = Depends(get_current_teacher)):
    """Carries on a plan the app was restarted in the middle of, or makes the weeks that were
    stopped. Never makes a week twice."""
    plan = _own_plan(db, plan_id, teacher)
    if not plan:
        return RedirectResponse(url="/", status_code=303)
    course, sow = db.get(Course, plan.course_id), db.get(SchemeOfWork, plan.sow_id)
    if any(i.get("stopped") for i in plan.items_json or []) and course and sow:
        made = _made_weeks(db, sow)
        items = []
        for item in plan.items_json:
            if item.get("stopped") and item["week"] not in made:
                deck = _queue_week(db, course, sow, teacher, item["week"], plan.options_json or {})
                item = dict(item, deck_id=deck.id if deck else None, stopped=not deck)
            items.append(item)
        plan.items_json = items
    plan.state = "running"
    db.commit()
    _start(background_tasks, plan.id)
    return RedirectResponse(url=f"/term-plans/{plan.id}", status_code=303)


@router.post("/term-plans/{plan_id}/retry/{week}")
def plan_retry(plan_id: int, week: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db),
               teacher: Teacher = Depends(get_current_teacher)):
    """Tries a failed week again - its lesson if that failed, otherwise just its quiz."""
    plan = _own_plan(db, plan_id, teacher)
    if not plan:
        return RedirectResponse(url="/", status_code=303)
    items = []
    for item in plan.items_json or []:
        if item["week"] == week:
            deck = db.get(LessonDeck, item["deck_id"]) if item.get("deck_id") else None
            quiz = db.get(QuizSet, item["quiz_id"]) if item.get("quiz_id") else None
            if deck and deck.status == "failed":
                deck.status, deck.error_detail = "queued", None
            if quiz and quiz.status == "failed":
                db.delete(quiz)
                item = dict(item, quiz_id=None)
        items.append(item)
    plan.items_json = items
    plan.state = "running"
    db.commit()
    _start(background_tasks, plan.id)
    return RedirectResponse(url=f"/term-plans/{plan.id}", status_code=303)


def _safe(name):
    return re.sub(r"[^A-Za-z0-9 _.,&()-]", "", name or "")[:80].strip() or "lesson"


@router.get("/term-plans/{plan_id}/download")
def plan_download(plan_id: int, db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    """Every finished PowerPoint in the plan, plus each lesson's activity worksheets, in one zip."""
    plan = _own_plan(db, plan_id, teacher)
    if not plan:
        return RedirectResponse(url="/", status_code=303)
    buf = io.BytesIO()
    added = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf, tempfile.TemporaryDirectory() as tmp:
        for item in plan.items_json or []:
            deck = db.get(LessonDeck, item["deck_id"]) if item.get("deck_id") else None
            if not deck or deck.status != "ready":
                continue
            name = f"Week {item['week']:02d} - {_safe(deck.title)}"
            if deck.pptx_path and os.path.exists(deck.pptx_path):
                zf.write(deck.pptx_path, f"{name}.pptx")
                added += 1
            if deck.outline_json:
                try:
                    out = os.path.join(tmp, f"{deck.id}.docx")
                    if pr.render_resources_docx(deck.outline_json, deck.title, out):
                        zf.write(out, f"{name} - worksheets.docx")
                except Exception:
                    pass  # a lesson with no activity resources simply has no worksheet file
    if not added:
        return RedirectResponse(url=f"/term-plans/{plan.id}", status_code=303)
    sow = db.get(SchemeOfWork, plan.sow_id)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{_safe(sow.title if sow else "term")} - lessons.zip"'})
