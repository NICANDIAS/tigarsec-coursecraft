"""
Stage 6 (B): live interactive lessons (the Nearpod-style part of CourseCraft).

Lecturer side (logged in, only the lecturer who started the session):
  start from a lesson deck or a practice question set -> a join code -> present screen by
  screen, answers arrive live, optionally share the class's answers, end -> summary (+ CSV).
Student side (NO account): go to /join, type the code and a first name or nickname, answer the
questions on the current screen from a phone or laptop. A random token in a cookie keeps a
student's answers theirs; nothing else about them is stored.

Updates use simple polling (every 2 seconds) rather than websockets - it works through any
college proxy and needs nothing extra installed.
"""
import csv
import datetime
import io
import secrets

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Teacher, LessonDeck, PracticeSet, QuizSet, LiveSession, LiveParticipant, LiveResponse
from ..auth import get_current_teacher
from ..course_access import get_course_for_teacher
import os

from ..live_activities import (screens_from_deck, screens_from_practice, new_join_code, clean_name, clean_answer, tally,
                               clean_public_url, join_link, is_local_only, attach_pptx_slides, choose_join_base)
from ..qr import qr_svg
from ..quiz import screens_from_quiz, public_screen, points_for, is_open, leaderboard
from ..system_settings import get_system_settings
from .presentations import _can_access_deck, deck_layout, session_gaps

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
COOKIE_HOURS = 6


# ------------------------------------------------------------------------------- lecturer

LIVE_STATES = ("open", "paused")  # students can join and see the lesson (a paused one shows "break")


def _taken_codes(db):
    return {c for (c,) in db.query(LiveSession.join_code).filter(LiveSession.state.in_(LIVE_STATES)).all()}


def _new_session(db, teacher, course_id, title, screens, deck_id=None, practice_id=None, quiz_id=None):
    # Opening "Run live" again before pressing "Start the lesson" reuses that waiting session (same
    # code, students already joined stay) instead of leaving a trail of never-started sessions.
    waiting = (db.query(LiveSession).filter(LiveSession.teacher_id == teacher.id, LiveSession.state == "open",
                                            LiveSession.started_at.is_(None), LiveSession.lesson_deck_id == deck_id,
                                            LiveSession.practice_set_id == practice_id, LiveSession.quiz_set_id == quiz_id)
               .order_by(LiveSession.id.desc()).first())
    if waiting and waiting.current_screen == 0:
        waiting.screens_json, waiting.title = screens, title[:255]
        db.commit()
        return waiting
    taken = _taken_codes(db)
    session = LiveSession(teacher_id=teacher.id, course_id=course_id, lesson_deck_id=deck_id, practice_set_id=practice_id,
                          quiz_set_id=quiz_id, title=title[:255], join_code=new_join_code(taken), screens_json=screens,
                          current_screen=0, screen_started_at=datetime.datetime.utcnow(), revealed_screens=[], state="open")
    db.add(session)
    db.commit()
    return session


@router.post("/lesson-decks/{deck_id}/live/start")
def live_start_from_deck(deck_id: int, db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    deck = db.get(LessonDeck, deck_id)
    if not _can_access_deck(deck, teacher, db) or not deck.outline_json:
        return RedirectResponse(url="/", status_code=303)
    screens = screens_from_deck(deck.outline_json)
    layout = deck_layout(deck)
    if layout:
        attach_pptx_slides(screens, deck.outline_json, len(layout["slides"]))  # show the REAL slides on the big screen
    session = _new_session(db, teacher, deck.course_id, deck.title, screens, deck_id=deck.id)
    return RedirectResponse(url=f"/live/{session.id}/teach", status_code=303)


@router.post("/practice-questions/{set_id}/live/start")
def live_start_from_practice(set_id: int, db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    practice = db.get(PracticeSet, set_id)
    if not practice or not get_course_for_teacher(db, practice.course_id, teacher) or not practice.questions_json:
        return RedirectResponse(url="/", status_code=303)
    screens = screens_from_practice(practice.questions_json)
    session = _new_session(db, teacher, practice.course_id, f"Practice: {practice.unit_name}", screens, practice_id=practice.id)
    return RedirectResponse(url=f"/live/{session.id}/teach", status_code=303)


@router.post("/quizzes/{quiz_id}/live/start")
def live_start_from_quiz(quiz_id: int, db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    quiz = db.get(QuizSet, quiz_id)
    if not quiz or not get_course_for_teacher(db, quiz.course_id, teacher):
        return RedirectResponse(url="/", status_code=303)
    if not quiz.questions_json:
        return RedirectResponse(url=f"/quizzes/{quiz.id}", status_code=303)
    from ..live_activities import _number
    screens = _number(screens_from_quiz(quiz.questions_json, quiz.seconds_per_question or 20, quiz.title))
    session = _new_session(db, teacher, quiz.course_id, f"Quiz: {quiz.title}", screens, quiz_id=quiz.id)
    return RedirectResponse(url=f"/live/{session.id}/teach", status_code=303)


def _elapsed(session):
    """Seconds since the current screen was shown (None for sessions from before Stage 8)."""
    if not session.screen_started_at:
        return None
    return max(0.0, (datetime.datetime.utcnow() - session.screen_started_at).total_seconds())


def _is_quiz(screens):
    return any(q.get("kind") == "mcq" for s in screens for q in s.get("questions", []))


def _quiz_board(db, session, final=False):
    """The leaderboard so far: each student's points from the questions whose answer has been
    REVEALED (so the scores on the projector never give away the right answer early). The final
    leaderboard (and the summary) counts every question."""
    screens = session.screens_json or []
    answers = {q["key"]: q.get("answer") for s in screens for q in s.get("questions", []) if q.get("kind") == "mcq"}
    rows = db.query(LiveResponse).filter(LiveResponse.session_id == session.id).all()
    if not final:
        shown = set(session.revealed_screens or [])
        rows = [r for r in rows if r.screen_index in shown]
    names = {p.id: p.display_name for p in db.query(LiveParticipant).filter(LiveParticipant.session_id == session.id).all()}
    scored = [{"participant": r.participant_id, "points": r.points or 0,
               "correct": r.question_key in answers and r.answer_choice == answers[r.question_key]} for r in rows]
    return leaderboard(scored, names)


def _own(db, session_id, teacher):
    session = db.get(LiveSession, session_id)
    return session if session and session.teacher_id == teacher.id else None


def _responses(db, session, screen_index=None):
    q = db.query(LiveResponse, LiveParticipant).join(LiveParticipant, LiveResponse.participant_id == LiveParticipant.id) \
        .filter(LiveResponse.session_id == session.id)
    if screen_index is not None:
        q = q.filter(LiveResponse.screen_index == screen_index)
    return [{"question_key": r.question_key, "name": p.display_name, "participant": p.id, "text": r.answer_text,
             "choice": r.answer_choice, "screen": r.screen_index, "points": r.points} for r, p in q.order_by(LiveResponse.id).all()]


@router.get("/live/{session_id}/teach", response_class=HTMLResponse)
def live_teach(session_id: int, request: Request, db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    session = _own(db, session_id, teacher)
    if not session:
        return RedirectResponse(url="/", status_code=303)
    if session.state not in LIVE_STATES:
        return RedirectResponse(url=f"/live/{session.id}/summary", status_code=303)
    base, from_setting, stale_setting = join_base_details(request, db)
    link = join_link(base, session.join_code)
    # the deck's real slides, when this session was started from a deck whose file still matches
    layout = None
    if session.lesson_deck_id and any("slide" in s for s in (session.screens_json or [])):
        deck = db.get(LessonDeck, session.lesson_deck_id)
        if deck and _can_access_deck(deck, teacher, db):
            layout = deck_layout(deck)
            wanted = max(s.get("slide", -1) for s in session.screens_json)
            if layout and wanted >= len(layout["slides"]):
                layout = None
    return templates.TemplateResponse("live_teach.html", {
        "request": request, "teacher": teacher, "session": session, "screens": session.screens_json or [],
        "join_url": base + "/join", "join_link": link, "qr": qr_svg(link, size_px=320),
        "local_only": is_local_only(base), "from_setting": from_setting, "is_admin": teacher.role == "admin",
        "layout": layout, "presenter": request.query_params.get("view") == "presenter",
        "stale_setting": stale_setting})


def join_base(request, db):
    """The address students' phones use: the admin's setting, else the COURSECRAFT_PUBLIC_URL
    server setting, else the address this browser used. Returns (address, came_from_a_setting)."""
    base, from_setting, _stale = join_base_details(request, db)
    return base, from_setting


def join_base_details(request, db):
    """(address, came_from_a_setting, the saved address if it was ignored as out of date)."""
    saved = None
    for value in (getattr(get_system_settings(db), "public_base_url", None), os.getenv("COURSECRAFT_PUBLIC_URL")):
        saved = clean_public_url(value)
        if saved:
            break
    return choose_join_base(saved, str(request.base_url))


@router.get("/live/{session_id}/data")
def live_teacher_data(session_id: int, db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    session = _own(db, session_id, teacher)
    if not session:
        return JSONResponse({"error": "not found"}, status_code=404)
    screens = session.screens_json or []
    index = session.current_screen
    people = db.query(LiveParticipant).filter(LiveParticipant.session_id == session.id).order_by(LiveParticipant.id).all()
    screen = screens[index] if 0 <= index < len(screens) else {"questions": []}
    data = {"state": session.state, "current": index, "joined": [p.display_name for p in people],
            "revealed": index in (session.revealed_screens or []), "tally": tally(screen, _responses(db, session, index))}
    if _is_quiz(screens):
        data["elapsed"] = _elapsed(session)
        final = screen.get("kind") == "leaderboard"
        data["leaderboard"] = [{k: t[k] for k in ("rank", "name", "points", "correct")} for t in _quiz_board(db, session, final)[:10]]
    return data


@router.post("/live/{session_id}/screen")
def live_set_screen(session_id: int, screen: int = Form(...), db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    session = _own(db, session_id, teacher)
    if not session or session.state != "open":
        return JSONResponse({"ok": False}, status_code=404)
    new_index = max(0, min(screen, len(session.screens_json or []) - 1))
    if new_index != session.current_screen and not session.started_at:
        session.started_at = datetime.datetime.utcnow()  # moving on means the lesson has started
    if new_index != session.current_screen or session.screen_started_at is None:
        session.screen_started_at = datetime.datetime.utcnow()  # the quiz countdown starts now
    session.current_screen = new_index
    db.commit()
    return {"ok": True, "current": session.current_screen}


@router.post("/live/{session_id}/reveal")
def live_reveal(session_id: int, screen: int = Form(...), show: int = Form(1), db: Session = Depends(get_db),
                teacher: Teacher = Depends(get_current_teacher)):
    """Shares (or hides again) the class's answers for one screen on the students' devices - names are never shown to them."""
    session = _own(db, session_id, teacher)
    if not session:
        return JSONResponse({"ok": False}, status_code=404)
    revealed = set(session.revealed_screens or [])
    (revealed.add if show else revealed.discard)(screen)
    session.revealed_screens = sorted(revealed)  # a new list, so the JSON column is saved
    db.commit()
    return {"ok": True}


@router.post("/live/{session_id}/begin")
def live_begin(session_id: int, db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    """"Start the lesson" on the join screen - only now does the session count as started."""
    session = _own(db, session_id, teacher)
    if session and session.state == "open" and not session.started_at:
        session.started_at = datetime.datetime.utcnow()
        db.commit()
    return {"ok": bool(session)}


@router.post("/live/{session_id}/pause")
def live_pause(session_id: int, db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    """A break: students' phones say "Break - back soon" and keep waiting; nothing is lost."""
    session = _own(db, session_id, teacher)
    if session and session.state == "open":
        session.state = "paused"
        db.commit()
    return RedirectResponse(url=f"/live/{session_id}/teach" if session else "/", status_code=303)


@router.post("/live/{session_id}/resume")
def live_resume(session_id: int, db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    """Carry on after a break - or continue a session that was ended (e.g. ended by mistake, or the
    second half of a split lesson). Students rejoin with the same code where possible."""
    session = _own(db, session_id, teacher)
    if not session:
        return RedirectResponse(url="/", status_code=303)
    if session.state == "ended":
        if session.join_code in _taken_codes(db):  # the code went to another class meanwhile
            session.join_code = new_join_code(_taken_codes(db))
        session.ended_at = None
    if session.state in ("paused", "ended"):
        session.state = "open"
        session.screen_started_at = datetime.datetime.utcnow()
        db.commit()
    return RedirectResponse(url=f"/live/{session_id}/teach", status_code=303)


@router.post("/live/{session_id}/end")
def live_end(session_id: int, db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    session = _own(db, session_id, teacher)
    if session and session.state in LIVE_STATES:
        session.state = "ended"
        session.ended_at = datetime.datetime.utcnow()
        db.commit()
    return RedirectResponse(url=f"/live/{session_id}/summary" if session else "/", status_code=303)


@router.get("/live/{session_id}/summary", response_class=HTMLResponse)
def live_summary(session_id: int, request: Request, db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    session = _own(db, session_id, teacher)
    if not session:
        return RedirectResponse(url="/", status_code=303)
    screens = session.screens_json or []
    responses = _responses(db, session)
    people = db.query(LiveParticipant).filter(LiveParticipant.session_id == session.id).order_by(LiveParticipant.id).all()
    per_screen = [(i, s, tally(s, [r for r in responses if r["screen"] == i])) for i, s in enumerate(screens) if s.get("questions")]
    answered = {r["participant"] for r in responses}
    # students who said "Not yet" to any objective - the ones to check on next lesson
    not_yet = sorted({r["name"] for i, s, t in per_screen for e in t.values() if e["kind"] == "confidence"
                      for r in e["answers"] if r.get("choice") == 0})
    board = _quiz_board(db, session, final=True) if _is_quiz(screens) else None
    gaps = session_gaps(db, session) if session.state == "ended" else []
    return templates.TemplateResponse("live_summary.html", {
        "request": request, "teacher": teacher, "session": session, "per_screen": per_screen, "people": people,
        "answered_count": len(answered), "not_yet": not_yet, "response_count": len(responses), "board": board,
        "gaps": gaps})


@router.get("/live/{session_id}/summary.csv")
def live_summary_csv(session_id: int, db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    session = _own(db, session_id, teacher)
    if not session:
        return RedirectResponse(url="/", status_code=303)
    screens = session.screens_json or []
    prompts = {q["key"]: (s.get("title", ""), q["prompt"], q.get("choices")) for s in screens for q in s.get("questions", [])}
    out = io.StringIO()
    writer = csv.writer(out)
    answers = {q["key"]: q.get("answer") for s in screens for q in s.get("questions", []) if q.get("kind") == "mcq"}
    writer.writerow(["Student", "Screen", "Question", "Answer", "Correct", "Points"])
    for r in _responses(db, session):
        title, prompt, choices = prompts.get(r["question_key"], ("", r["question_key"], None))
        answer = choices[r["choice"]] if choices and r["choice"] is not None and 0 <= r["choice"] < len(choices) else (r["text"] or "")
        is_quiz = r["question_key"] in answers
        # a leading = + - @ would make a spreadsheet run it as a formula - prefix it so it stays text
        cells = [r["name"], title, prompt, answer, ("yes" if r["choice"] == answers[r["question_key"]] else "no") if is_quiz else "",
                 r["points"] if r["points"] is not None else ""]
        writer.writerow(["'" + c if c[:1] in ("=", "+", "-", "@") else c for c in (str(x) for x in cells)])
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="live-session-{session.id}.csv"'})


@router.post("/live/{session_id}/delete")
def live_delete(session_id: int, db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    session = _own(db, session_id, teacher)
    if not session:
        return RedirectResponse(url="/", status_code=303)
    answers = db.query(LiveResponse).filter(LiveResponse.session_id == session.id).count()
    never_started = session.state == "open" and not session.started_at and not answers
    if session.state in LIVE_STATES and not never_started:  # a class may be answering right now - end it first
        return RedirectResponse(url=f"/live/{session.id}/summary", status_code=303)
    course_id = session.course_id
    db.query(LiveResponse).filter(LiveResponse.session_id == session.id).delete()
    db.query(LiveParticipant).filter(LiveParticipant.session_id == session.id).delete()
    db.delete(session)
    db.commit()
    return RedirectResponse(url=f"/courses/{course_id}", status_code=303)


# ------------------------------------------------------------------------------- students (no account)

def _open_session(db, code):
    code = "".join(ch for ch in (code or "").upper() if ch.isalnum())[:12]
    if not code:
        return None
    return db.query(LiveSession).filter(LiveSession.join_code == code, LiveSession.state.in_(LIVE_STATES)).first()


def _participant(db, request, session):
    token = request.cookies.get(f"live_{session.id}")
    if not token:
        return None
    return db.query(LiveParticipant).filter(LiveParticipant.session_id == session.id, LiveParticipant.token == token).first()


@router.get("/join", response_class=HTMLResponse)
def join_page(request: Request, code: str = ""):
    return templates.TemplateResponse("live_join.html", {"request": request, "code": code[:12], "error": None})


@router.post("/join", response_class=HTMLResponse)
def join_submit(request: Request, code: str = Form(""), name: str = Form(""), db: Session = Depends(get_db)):
    session = _open_session(db, code)
    display = clean_name(name)
    if not session or not display:
        error = "That code is not open right now - check it with your lecturer." if not session else "Please type your first name or a nickname."
        return templates.TemplateResponse("live_join.html", {"request": request, "code": code[:12], "error": error}, status_code=400)
    participant = _participant(db, request, session)
    if participant:
        participant.display_name = display
    else:
        participant = LiveParticipant(session_id=session.id, display_name=display, token=secrets.token_urlsafe(24))
        db.add(participant)
    db.commit()
    response = RedirectResponse(url=f"/live/s/{session.join_code}", status_code=303)
    response.set_cookie(f"live_{session.id}", participant.token, max_age=COOKIE_HOURS * 3600, httponly=True, samesite="lax")
    return response


@router.get("/live/s/{code}", response_class=HTMLResponse)
def student_page(code: str, request: Request, db: Session = Depends(get_db)):
    session = _open_session(db, code)
    if not session or not _participant(db, request, session):
        return RedirectResponse(url=f"/join?code={code[:12]}", status_code=303)
    return templates.TemplateResponse("live_student.html", {"request": request, "session": session,
                                                            "name": _participant(db, request, session).display_name})


@router.get("/live/s/{code}/data")
def student_data(code: str, request: Request, db: Session = Depends(get_db)):
    session = _open_session(db, code)
    participant = _participant(db, request, session) if session else None
    if not session or not participant:
        return {"state": "ended"}
    if session.state == "paused":
        return {"state": "paused", "current": session.current_screen}  # "Break - back soon": the phone keeps waiting
    screens = session.screens_json or []
    index = session.current_screen
    screen = screens[index] if 0 <= index < len(screens) else {"title": "", "body": [], "questions": []}
    mine = {r.question_key: {"text": r.answer_text, "choice": r.answer_choice}
            for r in db.query(LiveResponse).filter(LiveResponse.session_id == session.id, LiveResponse.participant_id == participant.id,
                                                   LiveResponse.screen_index == index).all()}
    revealed = index in (session.revealed_screens or [])
    shared = None
    if revealed:
        # the class's answers WITHOUT names
        shared = {k: {"answers": [a["text"] for a in v["answers"] if a.get("text")], "counts": v.get("counts"), "choices": v.get("choices")}
                  for k, v in tally(screen, _responses(db, session, index)).items()}
    data = {"state": session.state, "current": index, "total": len(screens), "screen": public_screen(screen, revealed),
            "mine": mine, "shared": shared, "revealed": revealed}
    if _is_quiz(screens):
        mine_points = {r.question_key: r.points for r in db.query(LiveResponse).filter(
            LiveResponse.session_id == session.id, LiveResponse.participant_id == participant.id).all()}
        for key, entry in mine.items():
            entry["points"] = mine_points.get(key) if revealed else None  # how they did is shown with the answer
        data["elapsed"] = _elapsed(session)
        if revealed or screen.get("kind") == "leaderboard":
            board = _quiz_board(db, session, final=screen.get("kind") == "leaderboard")
            me = next((t for t in board if t["participant"] == participant.id), None)
            data["me"] = {"rank": me["rank"], "points": me["points"], "players": len(board)} if me else None
            if screen.get("kind") == "leaderboard":
                data["top"] = [{k: t[k] for k in ("rank", "name", "points")} for t in board[:5]]
    return data


@router.post("/live/s/{code}/answer")
def student_answer(code: str, request: Request, screen: int = Form(...), question_key: str = Form(...),
                   text: str = Form(""), choice: int = Form(-1), db: Session = Depends(get_db)):
    session = _open_session(db, code)
    participant = _participant(db, request, session) if session else None
    if not session or not participant:
        return JSONResponse({"ok": False, "message": "This session has ended."}, status_code=403)
    if session.state == "paused":
        return JSONResponse({"ok": False, "message": "It's a break - answers open again when the lesson continues."}, status_code=409)
    screens = session.screens_json or []
    if screen != session.current_screen or not 0 <= screen < len(screens):
        return JSONResponse({"ok": False, "message": "Your lecturer has moved on - this question is closed."}, status_code=409)
    question = next((q for q in screens[screen].get("questions", []) if q["key"] == question_key), None)
    if not question:
        return JSONResponse({"ok": False, "message": "Unknown question."}, status_code=400)
    existing = db.query(LiveResponse).filter(LiveResponse.session_id == session.id, LiveResponse.participant_id == participant.id,
                                             LiveResponse.question_key == question_key).first()
    if question["kind"] == "mcq":
        # quiz: ONE answer per question (it is a race), only while the clock is running
        if existing:
            return JSONResponse({"ok": False, "message": "You've already answered this one."}, status_code=409)
        elapsed = _elapsed(session)
        if not is_open(elapsed, question.get("seconds"), screen in (session.revealed_screens or [])):
            return JSONResponse({"ok": False, "message": "Time's up for this question."}, status_code=409)
        if not 0 <= choice < len(question.get("choices", [])):
            return JSONResponse({"ok": False, "message": "Pick one option."}, status_code=400)
        points = points_for(choice == question.get("answer"), elapsed or 0, question.get("seconds") or 20)
        db.add(LiveResponse(session_id=session.id, participant_id=participant.id, screen_index=screen,
                            question_key=question_key, answer_choice=choice, points=points))
        db.commit()
        return {"ok": True}  # whether it was right is only shown when the lecturer reveals it
    if question["kind"] == "confidence":
        if not 0 <= choice < len(question.get("choices", [])):
            return JSONResponse({"ok": False, "message": "Pick one option."}, status_code=400)
        value_text, value_choice = None, choice
    else:
        value_text, value_choice = clean_answer(text), None
        if not value_text:
            return JSONResponse({"ok": False, "message": "Type an answer first."}, status_code=400)
    if existing:
        existing.answer_text, existing.answer_choice, existing.screen_index = value_text, value_choice, screen
    else:
        db.add(LiveResponse(session_id=session.id, participant_id=participant.id, screen_index=screen,
                            question_key=question_key, answer_text=value_text, answer_choice=value_choice))
    db.commit()
    return {"ok": True}
