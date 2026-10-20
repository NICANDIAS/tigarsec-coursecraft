"""
Stage 14 self-test: plan the term (lessons and quizzes made one after another, stop, continue,
retry, download) and homework mode (set from a quiz, a student finds their name, answers, hands
in; results on the homework page, the student page, the tracker and the course page) - through
the REAL page functions, with a THROWAWAY database in /tmp. Your real database is not opened.
The AI jobs are replaced by quick fakes, so no AI is used.

Run (from ~/projects/coursecraft):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/selftest_stage14_db.py
Expected last line: "ALL STAGE 14 CHECKS PASSED (35)"
"""
import asyncio
import datetime
import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

TEMP_DB = "/tmp/coursecraft_stage14_selftest.db"
if os.path.exists(TEMP_DB):
    os.remove(TEMP_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{TEMP_DB}"

from starlette.requests import Request  # noqa: E402
from app import database  # noqa: E402

assert str(database.engine.url).endswith("coursecraft_stage14_selftest.db"), "SAFETY STOP: not the throwaway database"

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import (Teacher, Course, CourseTeacher, SpecVersion, SchemeOfWork, LessonDeck, QuizSet, Student,  # noqa: E402
                        TermPlan, Homework, HomeworkAttempt)
from app.routers import term_plan as tpr, homework as hwr, presentations as pr, quizzes as qz, courses as cr  # noqa: E402
from app.routers import students as st, tracker as tk  # noqa: E402

RESULTS = []
TMP = tempfile.mkdtemp(prefix="cc_stage14_")


def check(name, condition, detail=""):
    RESULTS.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not condition else ""))


def request(path, method="GET", form=None, cookie=None, query=""):
    body = urlencode(form or [], doseq=True).encode()
    headers = [(b"content-type", b"application/x-www-form-urlencoded")]
    if cookie:
        headers.append((b"cookie", cookie.encode()))

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    return Request({"type": "http", "method": method, "path": path, "query_string": query.encode(), "headers": headers,
                    "server": ("test", 80), "scheme": "http", "root_path": "", "client": ("1.2.3.4", 1)}, receive)


class Tasks:
    def __init__(self):
        self.jobs = []

    def add_task(self, fn, *a):
        self.jobs.append((fn, a))

    def run(self):
        jobs, self.jobs = self.jobs, []
        for fn, a in jobs:
            fn(*a)


# ------------------------------------------------------------------ fake AI jobs (record the order they ran in)
MADE = []
FAIL_DECK = set()


def fake_lesson(deck_id, content, minutes, previous, practicals, differentiate=False):
    db = SessionLocal()
    deck = db.get(LessonDeck, deck_id)
    MADE.append(("deck", deck.source_week, minutes, previous, differentiate, "P1" in content or "P2" in content or "P3" in content))
    if deck.source_week in FAIL_DECK:
        deck.status, deck.error_detail = "failed", "The AI provider timed out."
    else:
        path = os.path.join(TMP, f"{deck_id}.pptx")
        open(path, "wb").write(b"fake pptx")
        deck.status, deck.pptx_path = "ready", path
        deck.outline_json = {"lesson_title": deck.title, "slides": [{"type": "title", "title": deck.title}]}
    db.commit()
    db.close()


def fake_quiz(quiz_id, count):
    db = SessionLocal()
    quiz = db.get(QuizSet, quiz_id)
    MADE.append(("quiz", quiz.lesson_deck_id, count))
    quiz.questions_json = [{"question": f"Q{i}", "choices": ["a", "b"], "answer": 0, "explanation": ""} for i in range(count)]
    quiz.status = "ready"
    db.commit()
    db.close()


pr._run_generation_job = fake_lesson
qz._run_quiz_job = fake_quiz

# ------------------------------------------------------------------ a course with a spec and a scheme of work
UNIT = {"name": "Unit 2 Networking", "learning_outcomes": [{"code": "A", "title": "Networks", "criteria": [
    {"code": "P1", "description": "Explain topologies."}, {"code": "P2", "description": "Explain devices."},
    {"code": "P3", "description": "Explain addressing."}]}]}
Base.metadata.create_all(bind=engine)
db = SessionLocal()
lecturer = Teacher(name="Self Test", email="selftest@example.invalid", hashed_password="x", role="lecturer")
other = Teacher(name="Other", email="other@example.invalid", hashed_password="x", role="lecturer")
db.add_all([lecturer, other])
db.commit()
spec = SpecVersion(uploaded_by_teacher_id=lecturer.id, label="BTEC IT", raw_text="x", parse_status="parsed", structured_json={"units": [UNIT]})
db.add(spec)
db.commit()
today = datetime.date.today()
course = Course(teacher_id=lecturer.id, name="BTEC IT Year 1", spec_version_id=spec.id, start_date=today - datetime.timedelta(days=8))
db.add(course)
db.commit()
db.add(CourseTeacher(course_id=course.id, teacher_id=lecturer.id))
sow = SchemeOfWork(course_id=course.id, title="Autumn plan", status="ready", start_date=today - datetime.timedelta(days=8),
                   selected_unit_names=[UNIT["name"]], hours_per_week=3,
                   weeks_json=[{"week": 1, "title": "Topologies", "learning_outcomes": ["P1"]},
                               {"week": 2, "title": "Network devices", "learning_outcomes": ["P2"]},
                               {"week": 3, "title": "IP addressing", "learning_outcomes": ["P3"]},
                               {"week": 5, "title": "Review", "learning_outcomes": ["P1", "P2"]},
                               {"week": 6, "title": "Not in the spec", "learning_outcomes": ["Z9"]}])
db.add(sow)
db.commit()
made_by_hand = LessonDeck(course_id=course.id, teacher_id=lecturer.id, content_area_name="x", title="Topologies", status="ready",
                          source_sow_id=sow.id, source_week=1)
db.add(made_by_hand)
db.commit()

# ------------------------------------------------------------------ the plan page
page = tpr.plan_term_page(course.id, sow.id, request("/p"), db, lecturer).body.decode()
check("the plan page lists the weeks, with half term as a gap", "Week 4" in page and "No teaching planned" in page and "IP addressing" in page)
check("a week that already has a lesson can't be ticked", "Already made" in page and 'value="1" disabled' in page)
check("a week that matches none of the spec's criteria is shown as 'make by hand'", "Make by hand" in page and 'value="6" disabled' in page)
check("'This week' is week 2, ticked by 'Next 4 weeks'", "This week" in page and "checked" in page.split('value="2"')[1][:80])
check("lesson length starts at the scheme's hours a week", 'name="hours" value="3"' in page)
check("another lecturer can't open it", tpr.plan_term_page(course.id, sow.id, request("/p"), db, other).status_code == 303)

# ------------------------------------------------------------------ start a plan
tasks = Tasks()
form = [("weeks", "1"), ("weeks", "2"), ("weeks", "3"), ("weeks", "5"), ("weeks", "6"), ("hours", "1.5"),
        ("differentiate", "1"), ("quiz", "1"), ("quiz_count", "6"), ("join_up", "1")]
resp = asyncio.run(tpr.plan_term_start(course.id, sow.id, request("/p", "POST", form), tasks, db, lecturer))
db.expire_all()
plan = db.query(TermPlan).first()
check("the plan skips the made week and the unmatched week", plan and [i["week"] for i in plan.items_json] == [2, 3, 5])
queued = db.query(LessonDeck).filter(LessonDeck.status == "queued").all()
check("each week's lesson is waiting, with the right length and last week's recap",
      len(queued) == 3 and all(d.duration_minutes == 90 for d in queued)
      and any((d.previous_lesson_context or "").startswith("Week 1: Topologies") for d in queued))
again = asyncio.run(tpr.plan_term_start(course.id, sow.id, request("/p", "POST", form), Tasks(), db, lecturer))
check("pressing the button twice doesn't start a second plan", db.query(TermPlan).count() == 1 and again.headers["location"] == f"/term-plans/{plan.id}")
progress = tpr.plan_page(plan.id, request("/t"), db, lecturer).body.decode()
check("the progress page shows every week waiting, and does NOT say it was restarted",
      "0 of 3 lessons ready" in progress and "Waiting" in progress and "restarted" not in progress)

FAIL_DECK.add(3)
tasks.run()   # the worker: makes everything one after another
db.expire_all()
order = [(m[0], m[1]) for m in MADE]
deck_ids = {d.source_week: d.id for d in db.query(LessonDeck).filter(LessonDeck.source_sow_id == sow.id).all()}
check("lessons and quizzes are made one at a time, each quiz straight after its lesson",
      order == [("deck", 2), ("quiz", deck_ids[2]), ("deck", 3), ("deck", 5), ("quiz", deck_ids[5])], order)
check("the lecturer's choices reach every lesson (support & stretch) and quiz (6 questions)",
      all(m[4] for m in MADE if m[0] == "deck") and all(m[2] == 6 for m in MADE if m[0] == "quiz"))
check("each lesson is written from that week's criteria", all(m[5] for m in MADE if m[0] == "deck"))
db.refresh(plan)
check("the plan finishes, with the failed week marked", plan.state == "done" and
      tpr.plan_status(db, plan)["progress"] == {"done": 3, "total": 3, "ready": 2, "failed": 1, "pct": 100, "finished": True})
progress = tpr.plan_page(plan.id, request("/t"), db, lecturer).body.decode()
check("the progress page offers Retry for the failed week and a zip of the rest",
      f"/term-plans/{plan.id}/retry/3" in progress and "Download all (zip)" in progress)

FAIL_DECK.clear()
tasks = Tasks()
tpr.plan_retry(plan.id, 3, tasks, db, lecturer)
tasks.run()
db.expire_all()
db.refresh(plan)
check("Retry makes just that week (and its quiz)", [m[:2] for m in MADE[-2:]] == [("deck", 3), ("quiz", deck_ids[3])]
      and plan.state == "done" and tpr.plan_status(db, plan)["progress"]["ready"] == 3)
zipped = tpr.plan_download(plan.id, db, lecturer)
names = zipfile.ZipFile(io.BytesIO(zipped.body)).namelist()
check("the zip holds every PowerPoint, named by week", [n for n in names if n.endswith(".pptx")] ==
      ["Week 02 - Network devices.pptx", "Week 03 - IP addressing.pptx", "Week 05 - Review.pptx"], names)

# stop and continue (a fresh plan of one week, stopped before it starts)
db.query(LessonDeck).filter(LessonDeck.source_week == 5).delete()
db.commit()
tasks = Tasks()
asyncio.run(tpr.plan_term_start(course.id, sow.id, request("/p", "POST", [("weeks", "5"), ("hours", "1")]), tasks, db, lecturer))
db.expire_all()
plan2 = db.query(TermPlan).order_by(TermPlan.id.desc()).first()
tpr.plan_stop(plan2.id, db, lecturer)
made_before = len(MADE)
tasks.run()   # the worker sees it was stopped and does nothing
db.expire_all()
check("Stop removes weeks not started yet, and nothing more is made",
      plan2.state == "stopped" and db.query(LessonDeck).filter(LessonDeck.source_week == 5).count() == 0 and len(MADE) == made_before)
tpr.RUNNING.clear()
tasks = Tasks()
tpr.plan_continue(plan2.id, tasks, db, lecturer)
tasks.run()
db.expire_all()
check("'Make the rest' makes the stopped week after all", db.query(LessonDeck).filter(LessonDeck.source_week == 5, LessonDeck.status == "ready").count() == 1)
course_page = cr.course_detail(course.id, request(f"/courses/{course.id}"), "", db, lecturer).body.decode()
check("the course page offers Plan the term on the scheme", f"/scheme-of-work/{sow.id}/plan-term" in course_page)

# ------------------------------------------------------------------ homework
amira = Student(course_id=course.id, name="Amira Khan", student_ref="S100")
ben = Student(course_id=course.id, name="Ben O'Neill", student_ref="S200")
db.add_all([amira, ben])
db.commit()
quiz = QuizSet(course_id=course.id, teacher_id=lecturer.id, title="Networks homework", status="ready", seconds_per_question=20,
               questions_json=[{"question": "HTTPS port?", "choices": ["80", "443"], "answer": 1, "explanation": "443 is HTTPS."},
                               {"question": "Star centre?", "choices": ["switch", "ring"], "answer": 0, "explanation": ""}])
db.add(quiz)
db.commit()
hwr.homework_create(quiz.id, (today + datetime.timedelta(days=3)).isoformat(), "17:00", "1", db, lecturer)
db.expire_all()
homework = db.query(Homework).first()
check("a quiz is set as homework with its own code, questions and due date",
      homework and len(homework.code) == 6 and len(homework.questions_json) == 2 and homework.due_at.hour == 17)

code = homework.code
first = hwr.homework_student(code, request(f"/hw/{code}"), db).body.decode()
check("a student sees a name box - never the class list", "Your first and last name" in first and "Amira" not in first and "Ben" not in first)
bad = hwr.homework_start(code, request("/s"), "Amira", "", db)
check("a first name alone isn't accepted", "not%20on%20the%20class%20list" in bad.headers["location"])
start = hwr.homework_start(code, request("/s"), "  ben o neill ", "", db)
cookie = start.headers["set-cookie"].split(";")[0]
check("'ben o neill' finds Ben O'Neill and keeps his attempt on his phone", cookie.startswith(f"hw_{homework.id}=")
      and db.query(HomeworkAttempt).count() == 1)
answering = hwr.homework_student(code, request("/hw", cookie=cookie), db).body.decode()
check("his phone shows the questions without the answers", "HTTPS port?" in answering and "443 is HTTPS" not in answering and "Hand it in" in answering)
hwr.homework_save(code, request("/save", cookie=cookie), 0, 1, db)
asyncio.run(hwr.homework_submit(code, request("/submit", "POST", [("q1", "1")], cookie=cookie), db))
db.expire_all()
attempt = db.query(HomeworkAttempt).first()
check("answers saved as he went count when he hands in (1 of 2 right)", attempt.submitted_at and attempt.score == 1 and attempt.total == 2)
done = hwr.homework_student(code, request("/hw", cookie=cookie), db).body.decode()
check("after handing in he sees his score and the right answers", "1 / 2" in done and "Answer: switch" in done)
twice = hwr.homework_start(code, request("/s"), "Ben O'Neill", "", db)
check("someone else typing his name can't redo it", "already%20been%20handed%20in" in twice.headers["location"])

lect = hwr.homework_page(homework.id, request("/h"), db, lecturer).body.decode()
check("the lecturer sees who has handed in, scores and the hardest question",
      "1 / 2" in lect and "50%" in lect and "Not started" in lect and 'most common wrong answer: "ring"' in lect)
check("another lecturer can't see it", hwr.homework_page(homework.id, request("/h"), db, other).status_code == 303)
student_page = st.student_detail(course.id, ben.id, request("/st"), db, lecturer).body.decode()
check("Ben's student page shows his homework result", "Networks homework" in student_page and "1/2" in student_page)
tracker = tk.tracker_page(course.id, request("/t"), "all", 0, db, lecturer).body.decode()
check("the tracking sheet has a homework column with his score", "Networks homework" in tracker and "50%" in tracker)
course_page = cr.course_detail(course.id, request(f"/courses/{course.id}"), "", db, lecturer).body.decode()
check("the course page shows the homework and how many have handed in", "Networks homework" in course_page and "1/2 in" in course_page)
hwr.homework_reset(homework.id, ben.id, db, lecturer)
check("Reset lets him do it again", db.query(HomeworkAttempt).count() == 0)
qz.quiz_delete(quiz.id, db, lecturer)
db.expire_all()
check("deleting the quiz keeps the homework and its questions", db.get(Homework, homework.id).questions_json and db.get(Homework, homework.id).quiz_set_id is None)

db.close()
os.remove(TEMP_DB)
print()
if all(RESULTS):
    print(f"ALL STAGE 14 CHECKS PASSED ({len(RESULTS)})")
else:
    print(f"{RESULTS.count(False)} OF {len(RESULTS)} CHECKS FAILED - send me this output")
    sys.exit(1)
