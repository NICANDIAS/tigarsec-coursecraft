"""
Homework mode: set a quiz as homework, students answer it in their own time on their own phone
or laptop (no timer, no leaderboard), and the results land against the class list.

Student pages (/hw/...) need no login. A student types their name; it is matched to the class
list (they never see the list). A random token in a cookie keeps their attempt theirs, and each
student gets one attempt - the lecturer can reset it. Nothing here uses AI.
"""
import secrets

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Teacher, Student, QuizSet, Homework, HomeworkAttempt
from ..course_access import get_course_for_teacher
from ..auth import get_current_teacher
from ..homework import local_now, match_student, mark, clean_choice, question_stats, is_late, due_text, parse_due, review
from ..live_activities import new_join_code, is_local_only
from ..qr import qr_svg
from .live import join_base

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
COOKIE_DAYS = 60


def _own(db, homework_id, teacher):
    hw = db.get(Homework, homework_id)
    if not hw or not get_course_for_teacher(db, hw.course_id, teacher):
        return None
    return hw


def class_results(db, hw):
    """One row per ACTIVE student on the course: their attempt (if any), score and whether it was late."""
    students = db.query(Student).filter(Student.course_id == hw.course_id, Student.is_active.is_(True)).order_by(Student.name).all()
    attempts = {a.student_id: a for a in db.query(HomeworkAttempt).filter(HomeworkAttempt.homework_id == hw.id).all()}
    rows = []
    for st in students:
        a = attempts.get(st.id)
        state = "handed_in" if a and a.submitted_at else "started" if a else "not_started"
        rows.append({"student": st, "attempt": a, "state": state,
                     "pct": round(100 * a.score / a.total) if a and a.submitted_at and a.total else None,
                     "late": bool(a and is_late(hw.due_at, a.submitted_at))})
    return rows


def homework_summaries(db, course_id, limit=None):
    """The course's homework, newest first, with how many have handed in - for the course page and tracker."""
    q = db.query(Homework).filter(Homework.course_id == course_id).order_by(Homework.id.desc())
    hws = q.limit(limit).all() if limit else q.all()
    out = []
    for hw in hws:
        rows = class_results(db, hw)
        done = [r for r in rows if r["state"] == "handed_in"]
        out.append({"hw": hw, "done": len(done), "class_size": len(rows),
                    "avg": round(sum(r["pct"] for r in done) / len(done)) if done else None,
                    "by_student": {r["student"].id: r for r in rows}})
    return out


# ------------------------------------------------------------------ lecturer
@router.post("/quizzes/{quiz_id}/homework")
def homework_create(quiz_id: int, due_date: str = Form(""), due_time: str = Form(""), show_answers: str = Form(""),
                    db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    quiz = db.get(QuizSet, quiz_id)
    if not quiz or not get_course_for_teacher(db, quiz.course_id, teacher):
        return RedirectResponse(url="/", status_code=303)
    questions = [q for q in quiz.questions_json or [] if q.get("question") and q.get("choices")]
    if quiz.status != "ready" or not questions:
        return RedirectResponse(url=f"/quizzes/{quiz.id}", status_code=303)
    taken = {c for (c,) in db.query(Homework.code).all()}
    hw = Homework(course_id=quiz.course_id, teacher_id=teacher.id, quiz_set_id=quiz.id, title=quiz.title[:255],
                  code=new_join_code(taken), questions_json=questions, due_at=parse_due(due_date, due_time),
                  show_answers=bool(show_answers), is_open=True)
    db.add(hw)
    db.commit()
    return RedirectResponse(url=f"/homework/{hw.id}", status_code=303)


@router.get("/homework/{homework_id}", response_class=HTMLResponse)
def homework_page(homework_id: int, request: Request, db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    hw = _own(db, homework_id, teacher)
    if not hw:
        return RedirectResponse(url="/", status_code=303)
    base, _ = join_base(request, db)
    link = f"{base}/hw/{hw.code}"
    rows = class_results(db, hw)
    handed = [r for r in rows if r["state"] == "handed_in"]
    stats = question_stats(hw.questions_json or [], [r["attempt"].answers_json or {} for r in handed])
    return templates.TemplateResponse("homework_detail.html", {
        "request": request, "teacher": teacher, "hw": hw, "link": link, "qr": qr_svg(link, size_px=200),
        "local_only": is_local_only(base), "rows": rows, "handed": len(handed), "stats": stats,
        "avg": round(sum(r["pct"] for r in handed) / len(handed)) if handed else None,
        "due": due_text(hw.due_at), "quiz": db.get(QuizSet, hw.quiz_set_id) if hw.quiz_set_id else None})


@router.post("/homework/{homework_id}/settings")
def homework_settings(homework_id: int, due_date: str = Form(""), due_time: str = Form(""), show_answers: str = Form(""),
                      is_open: str = Form(""), db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    hw = _own(db, homework_id, teacher)
    if hw:
        hw.due_at = parse_due(due_date, due_time)
        hw.show_answers = bool(show_answers)
        hw.is_open = bool(is_open)
        db.commit()
    return RedirectResponse(url=f"/homework/{homework_id}", status_code=303)


@router.post("/homework/{homework_id}/reset/{student_id}")
def homework_reset(homework_id: int, student_id: int, db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    """Wipes one student's attempt so they can do it again (e.g. someone else used their name)."""
    hw = _own(db, homework_id, teacher)
    if hw:
        db.query(HomeworkAttempt).filter(HomeworkAttempt.homework_id == hw.id, HomeworkAttempt.student_id == student_id).delete()
        db.commit()
    return RedirectResponse(url=f"/homework/{homework_id}", status_code=303)


@router.post("/homework/{homework_id}/delete")
def homework_delete(homework_id: int, db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    hw = _own(db, homework_id, teacher)
    if not hw:
        return RedirectResponse(url="/", status_code=303)
    course_id = hw.course_id
    db.query(HomeworkAttempt).filter(HomeworkAttempt.homework_id == hw.id).delete()
    db.delete(hw)
    db.commit()
    return RedirectResponse(url=f"/courses/{course_id}#quizzes", status_code=303)


# ------------------------------------------------------------------ students (no login)
def _by_code(db, code):
    return db.query(Homework).filter(Homework.code == (code or "").strip().upper()).first()


def _attempt(db, request, hw):
    token = request.cookies.get(f"hw_{hw.id}")
    if not token:
        return None
    return db.query(HomeworkAttempt).filter(HomeworkAttempt.homework_id == hw.id, HomeworkAttempt.token == token).first()


def _student_page(request, hw, **ctx):
    return templates.TemplateResponse("homework_student.html", {"request": request, "hw": hw, "due": due_text(hw.due_at), **ctx})


@router.get("/hw/{code}", response_class=HTMLResponse)
def homework_student(code: str, request: Request, db: Session = Depends(get_db)):
    hw = _by_code(db, code)
    if not hw:
        return HTMLResponse("<p style='font-family:sans-serif;padding:20px;'>That homework link isn't right - check it with your lecturer.</p>", status_code=404)
    attempt = _attempt(db, request, hw)
    questions = hw.questions_json or []
    if attempt and attempt.submitted_at:
        student = db.get(Student, attempt.student_id)
        return _student_page(request, hw, view="done", name=student.name if student else "", attempt=attempt,
                             late=is_late(hw.due_at, attempt.submitted_at), review=review(questions, attempt.answers_json or {}, hw.show_answers))
    if not hw.is_open:
        return _student_page(request, hw, view="closed")
    if attempt:
        student = db.get(Student, attempt.student_id)
        public = [{"question": q.get("question", ""), "choices": q.get("choices") or []} for q in questions]
        return _student_page(request, hw, view="answer", name=student.name if student else "", questions=public,
                             saved=attempt.answers_json or {}, error=request.query_params.get("error"))
    return _student_page(request, hw, view="name", error=request.query_params.get("error"), ask_ref=request.query_params.get("ref") == "1",
                         typed=request.query_params.get("name", ""))


@router.post("/hw/{code}/start")
def homework_start(code: str, request: Request, name: str = Form(""), student_ref: str = Form(""), db: Session = Depends(get_db)):
    from urllib.parse import quote
    hw = _by_code(db, code)
    if not hw:
        return RedirectResponse(url="/", status_code=303)
    if not hw.is_open:
        return RedirectResponse(url=f"/hw/{hw.code}", status_code=303)
    students = [{"id": s.id, "name": s.name, "ref": s.student_ref} for s in
                db.query(Student).filter(Student.course_id == hw.course_id, Student.is_active.is_(True)).all()]
    result, student_id = match_student(name, students, student_ref)
    if result == "many":
        return RedirectResponse(url=f"/hw/{hw.code}?ref=1&name={quote(name[:80])}&error={quote('Two students have that name - add your student reference too.')}", status_code=303)
    if result == "none":
        return RedirectResponse(url=f"/hw/{hw.code}?name={quote(name[:80])}&error={quote('That name is not on the class list. Type your first and last name as your lecturer has it - or ask them.')}", status_code=303)
    attempt = db.query(HomeworkAttempt).filter(HomeworkAttempt.homework_id == hw.id, HomeworkAttempt.student_id == student_id).first()
    if attempt and attempt.submitted_at:
        return RedirectResponse(url=f"/hw/{hw.code}?error={quote('That homework has already been handed in. If that was not you, tell your lecturer.')}", status_code=303)
    if attempt:
        attempt.token = secrets.token_urlsafe(24)  # carrying on from another phone/laptop moves the attempt there
    else:
        attempt = HomeworkAttempt(homework_id=hw.id, student_id=student_id, token=secrets.token_urlsafe(24), answers_json={})
        db.add(attempt)
    db.commit()
    resp = RedirectResponse(url=f"/hw/{hw.code}", status_code=303)
    resp.set_cookie(f"hw_{hw.id}", attempt.token, max_age=COOKIE_DAYS * 86400, httponly=True, samesite="lax")
    return resp


@router.post("/hw/{code}/save")
def homework_save(code: str, request: Request, q: int = Form(...), choice: int = Form(...), db: Session = Depends(get_db)):
    """Saves one answer as it is picked, so nothing is lost if the phone locks or the tab closes."""
    hw = _by_code(db, code)
    attempt = _attempt(db, request, hw) if hw else None
    if not attempt or attempt.submitted_at or not hw.is_open:
        return JSONResponse({"ok": False}, status_code=409)
    questions = hw.questions_json or []
    if not 0 <= q < len(questions) or clean_choice(choice, questions[q]) is None:
        return JSONResponse({"ok": False}, status_code=400)
    attempt.answers_json = dict(attempt.answers_json or {}, **{str(q): choice})
    db.commit()
    return {"ok": True}


@router.post("/hw/{code}/submit")
async def homework_submit(code: str, request: Request, db: Session = Depends(get_db)):
    hw = _by_code(db, code)
    attempt = _attempt(db, request, hw) if hw else None
    if not attempt:
        return RedirectResponse(url=f"/hw/{code}", status_code=303)
    if attempt.submitted_at or not hw.is_open:
        return RedirectResponse(url=f"/hw/{hw.code}", status_code=303)
    form = await request.form()
    questions = hw.questions_json or []
    answers = dict(attempt.answers_json or {})
    for i, q in enumerate(questions):
        picked = clean_choice(form.get(f"q{i}"), q)
        if picked is not None:
            answers[str(i)] = picked
    result = mark(questions, answers)
    attempt.answers_json = answers
    attempt.score, attempt.total = result["score"], result["total"]
    attempt.submitted_at = local_now()
    db.commit()
    return RedirectResponse(url=f"/hw/{hw.code}", status_code=303)
