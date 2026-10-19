"""
Live lesson lifecycle self-test: "not started" until Start is pressed, breaks, continuing an
ended session, and deleting - through the REAL page functions, with a THROWAWAY database in
/tmp. Your real database is not opened. No AI.

Run (from ~/projects/coursecraft):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/selftest_live_lifecycle_db.py
Expected last line: "ALL LIVE LIFECYCLE CHECKS PASSED (14)"
"""
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

TEMP_DB = "/tmp/coursecraft_lifecycle_selftest.db"
if os.path.exists(TEMP_DB):
    os.remove(TEMP_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{TEMP_DB}"
_stub = types.ModuleType("app.llm_gateway")
_stub.complete_json = lambda **k: (_ for _ in ()).throw(RuntimeError("AI disabled in self-test"))
_stub.get_effective_provider = lambda *a, **k: "none"
sys.modules["app.llm_gateway"] = _stub

from starlette.requests import Request  # noqa: E402
from app import database  # noqa: E402

assert str(database.engine.url).endswith("coursecraft_lifecycle_selftest.db"), "SAFETY STOP: not the throwaway database"

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import Teacher, Course, CourseTeacher, LessonDeck, LiveSession, LiveResponse, QuizSet  # noqa: E402
from app.routers import live as lv, courses as cr  # noqa: E402

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not condition else ""))


def request(path, cookies=None):
    headers = [(b"cookie", cookies.encode())] if cookies else []
    return Request({"type": "http", "method": "GET", "path": path, "query_string": b"", "headers": headers,
                    "server": ("test", 80), "scheme": "http", "root_path": ""})


Base.metadata.create_all(bind=engine)
db = SessionLocal()
lecturer = Teacher(name="Self Test", email="selftest@example.invalid", hashed_password="x", role="lecturer")
db.add(lecturer)
db.commit()
course = Course(teacher_id=lecturer.id, name="Course")
db.add(course)
db.commit()
db.add(CourseTeacher(course_id=course.id, teacher_id=lecturer.id))
deck = LessonDeck(course_id=course.id, teacher_id=lecturer.id, content_area_name="x", title="CVEs", status="ready",
                  outline_json={"lesson_title": "CVEs", "slides": [
                      {"type": "title", "title": "CVEs", "subtitle": ""},
                      {"type": "retrieval_starter", "title": "Do now", "questions": ["What is a vulnerability?"]},
                      {"type": "objectives", "objectives": ["Explain CVEs"]}]})
db.add(deck)
db.commit()


def course_page():
    return cr.course_detail(course.id, request(f"/courses/{course.id}"), "", db, lecturer).body.decode()


# ------------------------------------------------------------------ not started until "Start the lesson"
lv.live_start_from_deck(deck.id, db, lecturer)
db.expire_all()
first = db.query(LiveSession).first()
check("opening the join screen does not count as started", first.state == "open" and first.started_at is None
      and "Not started" in course_page() and "Live now" not in course_page())
lv.live_start_from_deck(deck.id, db, lecturer)
db.expire_all()
check("clicking 'Run live' again reuses the waiting session (no duplicates)", db.query(LiveSession).count() == 1)
lv.live_begin(first.id, db, lecturer)
db.expire_all()
check("pressing 'Start the lesson' starts it", db.get(LiveSession, first.id).started_at is not None and "Live now" in course_page())

# ------------------------------------------------------------------ a break
resp = lv.join_submit(request("/join"), first.join_code, "Amira", db)
cookie = resp.headers["set-cookie"].split(";")[0]
lv.live_set_screen(first.id, 1, db, lecturer)
key = db.get(LiveSession, first.id).screens_json[1]["questions"][0]["key"]
lv.student_answer(first.join_code, request("/a", cookie), 1, key, "A weakness", -1, db)
lv.live_pause(first.id, db, lecturer)
db.expire_all()
check("a break keeps the session (students' phones show 'Break')", db.get(LiveSession, first.id).state == "paused"
      and lv.student_data(first.join_code, request("/d", cookie), db)["state"] == "paused")
check("no answers are taken during the break", lv.student_answer(first.join_code, request("/a", cookie), 1, key, "x", -1, db).status_code == 409)
check("the course page shows 'On a break'", "On a break" in course_page())
lv.live_resume(first.id, db, lecturer)
db.expire_all()
check("continuing after the break - same code, answers kept", db.get(LiveSession, first.id).state == "open"
      and lv.student_data(first.join_code, request("/d", cookie), db)["mine"][key]["text"] == "A weakness")

# ------------------------------------------------------------------ continue a session that was ended
lv.live_end(first.id, db, lecturer)
db.expire_all()
code_before = db.get(LiveSession, first.id).join_code
check("an ended session offers 'Continue this session'", "Continue this session" in
      lv.live_summary(first.id, request("/s"), db, lecturer).body.decode())
lv.live_resume(first.id, db, lecturer)
db.expire_all()
again = db.get(LiveSession, first.id)
check("an ended session can be continued, with its answers", again.state == "open" and again.ended_at is None
      and db.query(LiveResponse).filter(LiveResponse.session_id == first.id).count() == 1 and again.join_code == code_before)
check("a running session still can't be deleted", (lv.live_delete(first.id, db, lecturer), db.expire_all(), db.get(LiveSession, first.id))[2] is not None)
lv.live_end(first.id, db, lecturer)
page = course_page()
check("the course page offers Delete, asking twice when it has answers", "1 answer" in page and "Delete them for good?" in page)
lv.live_delete(first.id, db, lecturer)
db.expire_all()
check("an ended session is deleted with its answers", db.get(LiveSession, first.id) is None and db.query(LiveResponse).count() == 0)

# ------------------------------------------------------------------ a never-started session and a failed quiz can be cleared
lv.live_start_from_deck(deck.id, db, lecturer)
db.expire_all()
waiting = db.query(LiveSession).first()
lv.live_delete(waiting.id, db, lecturer)
db.expire_all()
check("a session that was never started can be deleted straight away", db.query(LiveSession).count() == 0)
quiz = QuizSet(course_id=course.id, teacher_id=lecturer.id, title="CVE quiz", status="failed", questions_json=[])
db.add(quiz)
db.commit()
check("a failed quiz has a Delete button on the course page", f'action="/quizzes/{quiz.id}/delete"' in course_page())

db.close()
os.remove(TEMP_DB)
print()
if all(RESULTS):
    print(f"ALL LIVE LIFECYCLE CHECKS PASSED ({len(RESULTS)})")
else:
    print(f"{RESULTS.count(False)} OF {len(RESULTS)} CHECKS FAILED - send me this output")
    sys.exit(1)
