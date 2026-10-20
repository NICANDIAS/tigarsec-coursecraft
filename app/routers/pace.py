"""
Teaching at the class's pace (app/pace.py): "How did week N go?" after a lesson, "Carry on next week"
(the same PowerPoint, picked up from the slide reached), what moves as a result, and combining two
later weeks to win the week back. The lecturer decides; CourseCraft only suggests.
"""
import datetime

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Teacher, Course, LessonDeck, SchemeOfWork, LiveSession, LiveResponse, QuizSet, Homework, HomeworkAttempt
from ..course_access import get_course_for_teacher
from ..auth import get_current_teacher
from ..pace import get_pace, with_extension, lesson_signals, suggestion, suggest_merges
from ..sow_calendar import scheme_calendar, scheme_now
from ..homework import local_now

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _deck_scheme(db, deck_id, teacher):
    """(deck, course, scheme) for a lesson made from a scheme-of-work week this lecturer can see - else Nones."""
    from .presentations import _can_access_deck
    deck = db.get(LessonDeck, deck_id)
    if not deck or not _can_access_deck(deck, teacher, db):
        return None, None, None
    course = get_course_for_teacher(db, deck.course_id, teacher)
    sow = db.get(SchemeOfWork, deck.source_sow_id) if deck.source_sow_id else None
    if not course or not sow or sow.course_id != course.id:
        return deck, course, None
    return deck, course, sow


def _responses(db, session):
    return [{"question_key": r.question_key, "choice": r.answer_choice, "text": r.answer_text, "screen": r.screen_index}
            for r in db.query(LiveResponse).filter(LiveResponse.session_id == session.id).all()]


def lesson_evidence(db, deck):
    """What the live lesson and its quizzes showed: where the lecturer got to, the class's signals and
    what they found hard. -> {"reached", "total", "signals", "gaps", "homework_pct", "session"}"""
    from .presentations import session_gaps, deck_layout
    out = {"reached": None, "total": None, "signals": {"not_sure_pct": None, "quiz_pct": None, "answers": 0},
           "gaps": [], "homework_pct": None, "session": None}
    layout = None
    try:
        layout = deck_layout(deck)
    except Exception:
        pass
    outline_slides = len((deck.outline_json or {}).get("slides", []))
    out["total"] = len(layout["slides"]) if layout else (outline_slides or None)
    session = (db.query(LiveSession).filter(LiveSession.lesson_deck_id == deck.id, LiveSession.started_at.isnot(None))
               .order_by(LiveSession.id.desc()).first())
    if session:
        out["session"] = session
        screens = session.screens_json or []
        if 0 <= (session.current_screen or 0) < len(screens):
            slide = screens[session.current_screen].get("slide")
            out["reached"] = (slide + 1) if isinstance(slide, int) else session.current_screen + 1
            if not isinstance(slide, int):
                out["total"] = len(screens)
        out["signals"] = lesson_signals(screens, _responses(db, session))
        out["gaps"] = [g["line"] for g in session_gaps(db, session)]
    quiz_ids = [q.id for q in db.query(QuizSet).filter(QuizSet.lesson_deck_id == deck.id).all()]
    if quiz_ids:
        quiz_run = (db.query(LiveSession).filter(LiveSession.quiz_set_id.in_(quiz_ids), LiveSession.started_at.isnot(None))
                    .order_by(LiveSession.id.desc()).first())
        if quiz_run:
            sig = lesson_signals(quiz_run.screens_json or [], _responses(db, quiz_run))
            if sig["quiz_pct"] is not None:
                out["signals"]["quiz_pct"] = sig["quiz_pct"]
            out["gaps"] += [g["line"] for g in session_gaps(db, quiz_run)]
        hw = db.query(Homework).filter(Homework.quiz_set_id.in_(quiz_ids)).order_by(Homework.id.desc()).first()
        if hw:
            done = [a for a in db.query(HomeworkAttempt).filter(HomeworkAttempt.homework_id == hw.id).all() if a.submitted_at and a.total]
            if done:
                out["homework_pct"] = round(100 * sum(a.score / a.total for a in done) / len(done))
    out["gaps"] = out["gaps"][:5]
    return out


def what_moves(sow, course, pace_now, pace_after, from_week):
    """Weeks after from_week whose teaching date changes, with the before/after dates, plus how far
    past the end the scheme would run."""
    before = scheme_calendar(sow, course.start_date, pace_now)
    after = scheme_calendar(sow, course.start_date, pace_after)
    titles = {w["week"]: w.get("title", "") for w in sow.weeks_json or [] if isinstance(w, dict) and isinstance(w.get("week"), int)}
    moves = []
    for n in sorted(titles):
        if n < from_week:
            continue
        a, b = before["week_dates"].get(n), after["week_dates"].get(n)
        if a != b:
            moves.append({"week": n, "title": titles[n], "before": a, "after": b})
    part2 = next((t for t in after["timeline"] if t["kind"] == "week" and t["week"] == from_week and t["part"] > 1), None)
    return {"moves": moves, "overflow": after["overflow"], "overflow_before": before["overflow"],
            "part2_date": part2["date"] if part2 else None}


def _planned_assessment_weeks(sow):
    return [p.get("week") for p in (sow.planned_assessments or []) if isinstance(p, dict)]


@router.get("/lesson-decks/{deck_id}/how-did-it-go", response_class=HTMLResponse)
def how_did_it_go(deck_id: int, request: Request, db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    deck, course, sow = _deck_scheme(db, deck_id, teacher)
    if not deck:
        return RedirectResponse(url="/", status_code=303)
    if not sow:
        return RedirectResponse(url=f"/lesson-decks/{deck.id}", status_code=303)
    week = deck.source_week
    evidence = lesson_evidence(db, deck)
    hint = suggestion(evidence["reached"], evidence["total"], evidence["signals"])
    pace_after = with_extension(sow.pace_json, week)
    preview = what_moves(sow, course, sow.pace_json, pace_after, week)
    ideas = suggest_merges(sow.weeks_json or [], week, pace_after, _planned_assessment_weeks(sow)) if preview["overflow"] else []
    from_slide = evidence["reached"]  # pick up on the slide the lesson ended on - it may not have been finished
    return templates.TemplateResponse("how_did_it_go.html", {
        "request": request, "teacher": teacher, "deck": deck, "course": course, "sow": sow, "week": week,
        "evidence": evidence, "hint": hint, "preview": preview, "ideas": ideas, "from_slide": from_slide or 1,
        "already": sum(1 for e in get_pace(sow.pace_json)["extensions"] if e["week"] == week)})


@router.post("/lesson-decks/{deck_id}/carry-on")
def carry_on(deck_id: int, from_slide: int = Form(1), reason: str = Form(""), db: Session = Depends(get_db),
             teacher: Teacher = Depends(get_current_teacher)):
    """This week's lesson carries on into the next teaching week, from from_slide. Everything after moves one week."""
    deck, course, sow = _deck_scheme(db, deck_id, teacher)
    if not deck or not sow:
        return RedirectResponse(url="/", status_code=303)
    sow.pace_json = with_extension(sow.pace_json, deck.source_week, deck.id, max(1, from_slide), reason,
                                   local_now().date().isoformat())
    db.commit()
    return RedirectResponse(url=f"/courses/{course.id}/scheme-of-work/{sow.id}/plan-term?carried={deck.source_week}", status_code=303)


def _own_scheme(db, course_id, sow_id, teacher):
    course = get_course_for_teacher(db, course_id, teacher)
    sow = db.get(SchemeOfWork, sow_id)
    return (course, sow) if course and sow and sow.course_id == course.id else (None, None)


@router.post("/courses/{course_id}/scheme-of-work/{sow_id}/pace/undo")
def pace_undo(course_id: int, sow_id: int, index: int = Form(...), db: Session = Depends(get_db),
              teacher: Teacher = Depends(get_current_teacher)):
    """Takes back one "carried on next week"."""
    course, sow = _own_scheme(db, course_id, sow_id, teacher)
    if sow:
        pace = get_pace(sow.pace_json)
        if 0 <= index < len(pace["extensions"]):
            pace["extensions"].pop(index)
            sow.pace_json = pace
            db.commit()
    return RedirectResponse(url=f"/courses/{course_id}/scheme-of-work/{sow_id}/plan-term", status_code=303)


@router.post("/courses/{course_id}/scheme-of-work/{sow_id}/pace/merge")
def pace_merge(course_id: int, sow_id: int, a: int = Form(...), b: int = Form(...), undo: str = Form(""),
               db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    """Teaches week b together with week a (or, with undo, separately again)."""
    course, sow = _own_scheme(db, course_id, sow_id, teacher)
    if sow:
        pace = get_pace(sow.pace_json)
        weeks = {w["week"] for w in sow.weeks_json or [] if isinstance(w, dict)}
        pair = [a, b]
        if undo:
            pace["merges"] = [m for m in pace["merges"] if m != pair]
        elif a in weeks and b in weeks and a != b and pair not in pace["merges"]:
            pace["merges"].append(pair)
        sow.pace_json = pace
        db.commit()
    return RedirectResponse(url=f"/courses/{course_id}/scheme-of-work/{sow_id}/plan-term", status_code=303)
