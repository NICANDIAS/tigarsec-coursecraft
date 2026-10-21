"""
Stage 8: live quizzes - make one from a lesson deck (AI writes the questions from the deck's
own content, the lecturer checks them) or type one in, edit it, and run it live (routers/live.py)
with a countdown, points and a leaderboard.
"""
from fastapi import APIRouter, Request, Depends, Form, BackgroundTasks
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db, SessionLocal
from ..models import Teacher, LessonDeck, QuizSet, LiveSession
from ..course_access import get_course_for_teacher
from ..auth import get_current_teacher
from ..llm_gateway import complete_json, get_effective_provider
from ..quiz import QUIZ_SYSTEM_PROMPT, build_quiz_prompt, clean_quiz, clean_question, clamp_seconds, MAX_QUESTIONS
from .presentations import _can_access_deck

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
SECONDS_CHOICES = [10, 20, 30, 45, 60, 90]


@router.post("/lesson-decks/{deck_id}/quiz/generate")
def quiz_from_deck(deck_id: int, background_tasks: BackgroundTasks, count: int = Form(8),
                   db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    deck = db.get(LessonDeck, deck_id)
    if not _can_access_deck(deck, teacher, db) or not deck.outline_json or not get_course_for_teacher(db, deck.course_id, teacher):
        return RedirectResponse(url="/", status_code=303)
    count = max(3, min(15, count))
    quiz = QuizSet(course_id=deck.course_id, teacher_id=teacher.id, lesson_deck_id=deck.id, title=deck.title[:255],
                   questions_json=[], seconds_per_question=20, status="generating")
    db.add(quiz)
    db.commit()
    background_tasks.add_task(_run_quiz_job, quiz.id, count)
    return RedirectResponse(url=f"/quizzes/{quiz.id}", status_code=303)


def _run_quiz_job(quiz_id: int, count: int):
    db = SessionLocal()
    try:
        quiz = db.get(QuizSet, quiz_id)
        deck = db.get(LessonDeck, quiz.lesson_deck_id) if quiz and quiz.lesson_deck_id else None
        if not quiz:
            return
        try:
            if not deck or not deck.outline_json:
                raise RuntimeError("The lesson deck is no longer there.")
            questions, dropped = [], 0
            for attempt in (count, max(3, count // 2)):
                try:
                    result = complete_json(system=QUIZ_SYSTEM_PROMPT, prompt=build_quiz_prompt(deck.outline_json, attempt),
                                           max_tokens=3000, for_draft_generation=True)
                except Exception as e:  # cut off at its length limit -> ask for fewer questions once
                    if attempt != count or not any(w in str(e).lower() for w in ("max_tokens", "cut off")):
                        raise
                    continue
                questions, dropped = clean_quiz(result, seed=quiz.id)
                break
            if not questions:
                raise RuntimeError("The AI did not return any usable questions. Try again, or type the questions in yourself.")
            quiz.questions_json = questions
            quiz.status = "ready"
            quiz.error_detail = f"{dropped} question(s) were dropped because they were incomplete." if dropped else None
            quiz.generated_by_provider = get_effective_provider(for_draft_generation=True)
        except Exception as e:
            quiz.status = "failed"
            quiz.error_detail = f"Could not write the quiz: {e}"[:2000]
        db.commit()
    finally:
        db.close()


@router.post("/courses/{course_id}/quizzes/new")
def quiz_new(course_id: int, title: str = Form(""), db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    course = get_course_for_teacher(db, course_id, teacher)
    if not course:
        return RedirectResponse(url="/", status_code=303)
    quiz = QuizSet(course_id=course.id, teacher_id=teacher.id, title=(" ".join(title.split()) or "New quiz")[:255],
                   questions_json=[], seconds_per_question=20, status="ready")
    db.add(quiz)
    db.commit()
    return RedirectResponse(url=f"/quizzes/{quiz.id}?edit=1", status_code=303)


def _own_quiz(db, quiz_id, teacher):
    quiz = db.get(QuizSet, quiz_id)
    if not quiz or not get_course_for_teacher(db, quiz.course_id, teacher):
        return None
    return quiz


def _render(request, teacher, quiz, questions, editing, error=None, status_code=200, homeworks=None):
    return templates.TemplateResponse("quiz_detail.html", {
        "request": request, "teacher": teacher, "quiz": quiz, "questions": questions, "editing": editing,
        "error": error, "seconds_choices": SECONDS_CHOICES, "max_questions": MAX_QUESTIONS,
        "saved": request.query_params.get("saved"), "homeworks": homeworks or []}, status_code=status_code)


@router.get("/quizzes/{quiz_id}", response_class=HTMLResponse)
def quiz_detail(quiz_id: int, request: Request, db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    quiz = _own_quiz(db, quiz_id, teacher)
    if not quiz:
        return RedirectResponse(url="/", status_code=303)
    editing = request.query_params.get("edit") == "1" or (quiz.status == "ready" and not quiz.questions_json)
    from ..models import Homework
    homeworks = db.query(Homework).filter(Homework.quiz_set_id == quiz.id).order_by(Homework.id.desc()).all()
    return _render(request, teacher, quiz, quiz.questions_json or [], editing, homeworks=homeworks)


@router.post("/quizzes/{quiz_id}/save", response_class=HTMLResponse)
async def quiz_save(quiz_id: int, request: Request, db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    quiz = _own_quiz(db, quiz_id, teacher)
    if not quiz:
        return RedirectResponse(url="/", status_code=303)
    form = await request.form()
    try:
        n = max(0, min(MAX_QUESTIONS + 1, int(form.get("n", 0))))
    except ValueError:
        n = 0
    raw, problems = [], []
    for i in range(n):
        if form.get(f"q{i}_delete"):
            continue
        q = {"question": form.get(f"q{i}_question", ""), "choices": [form.get(f"q{i}_c{j}", "") for j in range(4)],
             "answer": form.get(f"q{i}_answer", ""), "explanation": form.get(f"q{i}_explanation", "")}
        if not str(q["question"]).strip() and not any(str(c).strip() for c in q["choices"]):
            continue  # an untouched empty question
        raw.append(q)
        cleaned = clean_question(q)
        if not cleaned:
            problems.append(f"Question {len(raw)} needs the question, at least two different answers, and the right one ticked.")
    title = " ".join(str(form.get("title", "")).split())[:255] or quiz.title
    if problems:
        quiz.title = title
        db.rollback()
        for q in raw:  # keep what was typed so nothing is lost
            try:
                q["answer"] = int(q["answer"])
            except (TypeError, ValueError):
                q["answer"] = -1
        return _render(request, teacher, quiz, raw, True, error=" ".join(problems), status_code=400)
    quiz.title = title
    quiz.seconds_per_question = clamp_seconds(form.get("seconds"), quiz.seconds_per_question or 20)
    quiz.questions_json = [clean_question(q) for q in raw][:MAX_QUESTIONS]
    quiz.status = "ready"
    db.commit()
    again = "&edit=1" if form.get("add_more") else ""
    return RedirectResponse(url=f"/quizzes/{quiz.id}?saved=1{again}", status_code=303)


@router.post("/quizzes/{quiz_id}/delete")
def quiz_delete(quiz_id: int, db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    quiz = _own_quiz(db, quiz_id, teacher)
    if not quiz:
        return RedirectResponse(url="/", status_code=303)
    course_id = quiz.course_id
    # live sessions already run from it keep their own copy of the questions and scores
    db.query(LiveSession).filter(LiveSession.quiz_set_id == quiz.id).update({"quiz_set_id": None}, synchronize_session=False)
    # homework set from it keeps its own copy of the questions and every student's results
    from ..models import Homework
    db.query(Homework).filter(Homework.quiz_set_id == quiz.id).update({"quiz_set_id": None}, synchronize_session=False)
    db.delete(quiz)
    db.commit()
    return RedirectResponse(url=f"/courses/{course_id}#quizzes", status_code=303)
