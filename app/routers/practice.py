"""
Stage 6 (A): exam practice questions for an exam-assessed course - pick a unit and topics,
CourseCraft writes AO-tagged questions with mark schemes from the spec, the lecturer reviews,
prints them, or runs them live in class (see routers/live.py).
"""
from fastapi import APIRouter, Request, Depends, Form, BackgroundTasks
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db, SessionLocal
from ..models import Teacher, Course, PracticeSet, LiveSession
from ..course_access import get_course_for_teacher
from ..auth import get_current_teacher
from ..llm_gateway import complete_json, get_effective_provider
from ..spec_context import has_exam_objectives
from ..practice_questions import PRACTICE_SYSTEM_PROMPT, build_prompt, clean_questions, ao_summary, MAX_QUESTIONS

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _spec(course):
    return course.spec_version.structured_json if course and course.spec_version and course.spec_version.structured_json else {}


def _exam_units(spec_json):
    """Units that have topics to test (an appendix of skills is used as context, not as a unit to test)."""
    return [(i, u) for i, u in enumerate(spec_json.get("units", []))
            if u.get("learning_outcomes") and not (u.get("code") or "").lower().startswith("appendix")]


@router.get("/courses/{course_id}/practice-questions", response_class=HTMLResponse)
def practice_home(course_id: int, request: Request, unit: int = -1,
                  db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    course = get_course_for_teacher(db, course_id, teacher)
    if not course:
        return RedirectResponse(url="/", status_code=303)
    spec_json = _spec(course)
    sets = db.query(PracticeSet).filter(PracticeSet.course_id == course.id).order_by(PracticeSet.id.desc()).all()
    units = _exam_units(spec_json)
    chosen = next((u for i, u in units if i == unit), None)
    return templates.TemplateResponse("practice_home.html", {
        "request": request, "teacher": teacher, "course": course, "sets": sets,
        "is_exam_spec": has_exam_objectives(spec_json), "units": units, "unit_index": unit, "chosen": chosen,
        "error": request.query_params.get("error"),
    })


@router.post("/courses/{course_id}/practice-questions/generate")
async def practice_generate(course_id: int, request: Request, background_tasks: BackgroundTasks,
                            db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    course = get_course_for_teacher(db, course_id, teacher)
    if not course:
        return RedirectResponse(url="/", status_code=303)
    form = await request.form()
    spec_json = _spec(course)
    units = dict(_exam_units(spec_json))
    try:
        unit_index = int(form.get("unit_index", -1))
        count = max(3, min(MAX_QUESTIONS, int(form.get("count", 6))))
    except ValueError:
        unit_index, count = -1, 6
    unit = units.get(unit_index)
    back = f"/courses/{course_id}/practice-questions"
    if not unit or not has_exam_objectives(spec_json):
        return RedirectResponse(url=f"{back}?error=Choose+a+unit+first.", status_code=303)
    picked = [int(k.split("-", 1)[1]) for k in form.keys() if k.startswith("lo-") and k.split("-", 1)[1].isdigit()]
    los = [lo for i, lo in enumerate(unit["learning_outcomes"]) if i in picked]
    if not los:
        return RedirectResponse(url=f"{back}?unit={unit_index}&error=Tick+at+least+one+topic.", status_code=303)
    practice = PracticeSet(course_id=course.id, teacher_id=teacher.id, unit_name=unit["name"][:500],
                           topic_titles=[lo.get("title") for lo in los], question_count=count, status="generating")
    db.add(practice)
    db.commit()
    background_tasks.add_task(_run_practice_job, practice.id, unit_index, [i for i in picked if i < len(unit["learning_outcomes"])], count)
    return RedirectResponse(url=f"/practice-questions/{practice.id}", status_code=303)


def _run_practice_job(set_id: int, unit_index: int, lo_indices: list, count: int):
    db = SessionLocal()
    try:
        practice = db.get(PracticeSet, set_id)
        course = db.get(Course, practice.course_id) if practice else None
        if not practice or not course:
            return
        try:
            spec_json = _spec(course)
            unit = spec_json["units"][unit_index]
            los = [unit["learning_outcomes"][i] for i in lo_indices]
            allowed = [a.get("code", "") for a in spec_json.get("assessment", {}).get("objectives", [])]
            questions, dropped = [], 0
            for attempt_count in (count, max(3, count // 2)):
                try:
                    result = complete_json(system=PRACTICE_SYSTEM_PROMPT, prompt=build_prompt(spec_json, unit, los, attempt_count),
                                           max_tokens=4000, for_draft_generation=True)
                except Exception as e:  # an answer cut off at its length limit -> ask for fewer questions once
                    if attempt_count != count or not any(w in str(e).lower() for w in ("max_tokens", "cut off")):
                        raise
                    continue
                questions, dropped = clean_questions(result, allowed)
                break
            if not questions:
                raise RuntimeError("The AI did not return any usable questions. Try again, or choose fewer topics.")
            practice.questions_json = questions
            practice.status = "ready"
            practice.error_detail = (f"{dropped} question(s) were dropped because they were incomplete." if dropped else None)
            practice.generated_by_provider = get_effective_provider(for_draft_generation=True)
        except Exception as e:
            practice.status = "failed"
            practice.error_detail = f"Could not write the questions: {e}"[:2000]
        db.commit()
    finally:
        db.close()


def _own_set(db, set_id, teacher):
    practice = db.get(PracticeSet, set_id)
    if not practice or not get_course_for_teacher(db, practice.course_id, teacher):
        return None
    return practice


@router.get("/practice-questions/{set_id}", response_class=HTMLResponse)
def practice_detail(set_id: int, request: Request, db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    practice = _own_set(db, set_id, teacher)
    if not practice:
        return RedirectResponse(url="/", status_code=303)
    questions = practice.questions_json or []
    return templates.TemplateResponse("practice_detail.html", {
        "request": request, "teacher": teacher, "practice": practice, "questions": questions,
        "ao_totals": ao_summary(questions), "total_marks": sum(q["marks"] for q in questions),
    })


@router.post("/practice-questions/{set_id}/delete")
def practice_delete(set_id: int, db: Session = Depends(get_db), teacher: Teacher = Depends(get_current_teacher)):
    practice = _own_set(db, set_id, teacher)
    if not practice:
        return RedirectResponse(url="/", status_code=303)
    course_id = practice.course_id
    # live sessions run from this set keep their own snapshot of the questions and answers
    db.query(LiveSession).filter(LiveSession.practice_set_id == practice.id).update({"practice_set_id": None}, synchronize_session=False)
    db.delete(practice)
    db.commit()
    return RedirectResponse(url=f"/courses/{course_id}/practice-questions", status_code=303)
