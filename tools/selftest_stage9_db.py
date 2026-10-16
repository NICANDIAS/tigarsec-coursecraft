"""
Stage 9 self-test: a quiz the class found hard shapes the next lesson's PowerPoint - through
the REAL page functions, with a THROWAWAY database in /tmp. Your real database is not opened.
No AI is called (the deck is not actually written - only what would be sent to the AI is checked).

Run (from ~/projects/coursecraft):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/selftest_stage9_db.py
Expected last line: "ALL STAGE 9 CHECKS PASSED (8)"
"""
import asyncio
import os
import sys
import types
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

TEMP_DB = "/tmp/coursecraft_stage9_selftest.db"
if os.path.exists(TEMP_DB):
    os.remove(TEMP_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{TEMP_DB}"
_stub = types.ModuleType("app.llm_gateway")
_stub.complete_json = lambda **k: (_ for _ in ()).throw(RuntimeError("AI disabled in self-test"))
_stub.get_effective_provider = lambda *a, **k: "none"
sys.modules["app.llm_gateway"] = _stub

from starlette.requests import Request  # noqa: E402
from app import database  # noqa: E402

assert str(database.engine.url).endswith("coursecraft_stage9_selftest.db"), "SAFETY STOP: not the throwaway database"

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import Teacher, Course, CourseTeacher, SpecVersion, QuizSet, LiveSession, LessonDeck  # noqa: E402
from app.routers import live as lv, presentations as pr  # noqa: E402
from app.class_feedback import HEADER  # noqa: E402

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not condition else ""))


def request(path, method="GET", query="", form=None):
    body = urlencode(form or [], doseq=True).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    return Request({"type": "http", "method": method, "path": path, "query_string": query.encode(),
                    "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
                    "server": ("test", 80), "scheme": "http", "root_path": "", "client": ("1.2.3.4", 1)}, receive)


Base.metadata.create_all(bind=engine)
db = SessionLocal()
lecturer = Teacher(name="Self Test", email="selftest@example.invalid", hashed_password="x", role="lecturer")
other = Teacher(name="Other", email="other@example.invalid", hashed_password="x", role="lecturer")
db.add_all([lecturer, other])
db.commit()
spec = SpecVersion(uploaded_by_teacher_id=lecturer.id, label="Test spec", raw_text="x", parse_status="parsed", structured_json={
    "units": [{"name": "Network security", "code": "1", "learning_outcomes": [
        {"code": "1.1", "title": "Firewalls", "criteria": [{"code": "1.1a", "description": "Explain what a firewall does"}]}]}]})
db.add(spec)
db.commit()
course = Course(teacher_id=lecturer.id, name="T Level Computing Y2", spec_version_id=spec.id)
db.add(course)
db.commit()
db.add_all([CourseTeacher(course_id=course.id, teacher_id=lecturer.id), CourseTeacher(course_id=course.id, teacher_id=other.id)])
quiz = QuizSet(course_id=course.id, teacher_id=lecturer.id, title="Network security", seconds_per_question=20, status="ready",
               questions_json=[{"question": "What does a firewall do?", "choices": ["Filters network traffic", "Stores passwords",
                                                                                     "Speeds up Wi-Fi"], "answer": 0, "explanation": ""},
                               {"question": "Which port does HTTPS use?", "choices": ["80", "443"], "answer": 1, "explanation": ""}])
db.add(quiz)
db.commit()

# ------------------------------------------------------------------ a quiz the class found hard
lv.live_start_from_quiz(quiz.id, db, lecturer)
db.expire_all()
session = db.query(LiveSession).first()
lv.live_set_screen(session.id, 1, db, lecturer)
db.expire_all()
key = session.screens_json[1]["questions"][0]["key"]
for name, choice in (("Amira", 1), ("Ben", 1), ("Chloe", 1), ("Dan", 0)):
    resp = lv.join_submit(request("/join", "POST"), session.join_code, name, db)
    cookie = resp.headers["set-cookie"].split(";")[0]
    req = Request({"type": "http", "method": "POST", "path": "/a", "query_string": b"", "headers": [(b"cookie", cookie.encode())],
                   "server": ("test", 80), "scheme": "http", "root_path": ""})
    lv.student_answer(session.join_code, req, 1, key, "", choice, db)
lv.live_end(session.id, db, lecturer)
summary = lv.live_summary(session.id, request("/s"), db, lecturer).body.decode()
check("the summary says what to revisit, with the likely misconception",
      "What to revisit next lesson" in summary and "only 25% answered correctly" in summary and "Stores passwords" in summary)
check("...and offers to make the next lesson's PowerPoint from it", f"presentations/new?revisit={session.id}" in summary)

page = pr.new_presentation_page(course.id, request("/n", query=f"revisit={session.id}"), 0, 0, 0, db, lecturer).body.decode()
check("the new-lesson page lists the gap, already ticked", f'value="{session.id}:{key}"' in page and "checked" in page.split(f'value="{session.id}:{key}"')[1][:200])
other_page = pr.new_presentation_page(course.id, request("/n"), 0, 0, 0, db, other).body.decode()
check("another lecturer on the course does not see this lecturer's class results", "What does a firewall do?" not in other_page)

# ------------------------------------------------------------------ generating the next deck
queued = []
asyncio.run(pr.generate_presentation(
    course.id, request("/g", "POST", form=[("revisit", f"{session.id}:{key}"), ("selected_los", "0")]),
    types.SimpleNamespace(add_task=lambda fn, *a: queued.append(a)), 0, "Encryption basics", 1.0,
    "Last lesson covered firewalls.", "", 0, 0, db, lecturer))
db.expire_all()
deck = db.query(LessonDeck).first()
sent = queued[0][3] if queued else ""
check("the ticked gap is sent to the AI with the lecturer's own text",
      sent.startswith("Last lesson covered firewalls.") and HEADER in sent and "What does a firewall do?" in sent, sent)
check("no student names go to the AI", not any(n in sent for n in ("Amira", "Ben", "Chloe", "Dan")))
check("it is kept on the deck, so 'Regenerate' revisits the same points", deck and HEADER in (deck.previous_lesson_context or ""))
deck.status, deck.outline_json = "ready", {"lesson_title": "Encryption basics", "slides": []}
db.commit()
deck_page = pr.view_deck(deck.id, request(f"/lesson-decks/{deck.id}"), db, lecturer).body.decode()
check("the lesson page shows what it revisits", "Revisits what the class found hard" in deck_page and "What does a firewall do?" in deck_page)

db.close()
os.remove(TEMP_DB)
print()
if all(RESULTS):
    print(f"ALL STAGE 9 CHECKS PASSED ({len(RESULTS)})")
else:
    print(f"{RESULTS.count(False)} OF {len(RESULTS)} CHECKS FAILED - send me this output")
    sys.exit(1)
