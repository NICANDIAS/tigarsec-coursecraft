"""
Stage 6 self-test: exam practice questions and live lessons through the REAL page functions,
with a THROWAWAY database in /tmp. Your real database is not opened. AI is replaced by a
fake that returns fixed answers, so nothing is sent anywhere. No PDFs needed.

  practice: page offered only for exam specs, questions written + checked, cut-off answer
            retried with fewer questions, set shown / deleted, other lecturers kept out
  live:     start from a deck, two students join with the code (no account), answer a
            question and a confidence poll, can't answer a closed question, lecturer sees
            names + counts, "share with class" shows answers WITHOUT names, end -> students
            told, summary + CSV (spreadsheet-formula safe), other lecturers kept out, delete
  decks:    a deck's AI prompt gets the spec's objectives for an exam spec, and is unchanged
            for a spec without them

Run (from ~/projects/coursecraft):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/selftest_stage6_db.py
Expected last line: "ALL STAGE 6 CHECKS PASSED (27)"
"""
import asyncio
import json
import os
import sys
import types
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

TEMP_DB = "/tmp/coursecraft_stage6_selftest.db"
if os.path.exists(TEMP_DB):
    os.remove(TEMP_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{TEMP_DB}"

FAKE = {"mode": "ok", "calls": []}


def fake_ai(**kwargs):
    FAKE["calls"].append(kwargs.get("prompt", ""))
    if FAKE["mode"] == "cut_off_once" and len(FAKE["calls"]) == 1:
        raise ValueError("The response was cut off before it finished, because it hit the max_tokens limit")
    return {"questions": [
        {"question": "Explain what is meant by cash flow.", "marks": 2, "ao": ["AO1"], "mark_scheme": ["money in and out of a business"]},
        {"question": "Analyse why a profitable cafe might run out of cash.", "marks": 6, "ao": ["AO2", "AO3"],
         "context": "Ada runs a small cafe.", "mark_scheme": ["timing of payments"]},
        {"question": "", "marks": 3, "ao": ["AO1"]},
    ]}


_stub = types.ModuleType("app.llm_gateway")
_stub.complete_json = fake_ai
_stub.get_effective_provider = lambda *a, **k: "fake"
sys.modules["app.llm_gateway"] = _stub

from fastapi import BackgroundTasks  # noqa: E402
from starlette.requests import Request  # noqa: E402
from app import database  # noqa: E402

assert str(database.engine.url).endswith("coursecraft_stage6_selftest.db"), "SAFETY STOP: not the throwaway database"

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import Teacher, Course, CourseTeacher, SpecVersion, LessonDeck, PracticeSet, LiveSession, LiveResponse  # noqa: E402
from app.routers import practice as pr, live as lv, presentations as pres  # noqa: E402

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not condition else ""))


def request(path, method="GET", form=None, cookie=None):
    body = urlencode(form or {}).encode()
    headers = [(b"content-type", b"application/x-www-form-urlencoded"), (b"content-length", str(len(body)).encode())]
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}
    return Request({"type": "http", "method": method, "path": path, "query_string": b"", "headers": headers,
                    "server": ("test", 80), "scheme": "http", "root_path": ""}, receive)


def body(resp):
    return json.loads(resp.body) if hasattr(resp, "body") and not isinstance(resp, dict) else resp


Base.metadata.create_all(bind=engine)
db = SessionLocal()
lecturer = Teacher(name="Self Test", email="selftest@example.invalid", hashed_password="x", role="lecturer")
other = Teacher(name="Other", email="other@example.invalid", hashed_password="x", role="lecturer")
db.add_all([lecturer, other])
db.commit()
exam_spec = SpecVersion(uploaded_by_teacher_id=lecturer.id, label="A level (test)", raw_text="x", parse_status="parsed", structured_json={
    "units": [{"name": "Component 1: Business", "code": "Component 1", "assessment": "Written examination: 2 hours 15 minutes",
               "learning_outcomes": [{"code": "LO1", "title": "Cash flow", "criteria": [{"code": "1.1", "description": "Explain what is meant by cash flow"}]},
                                     {"code": "LO2", "title": "Budgets", "criteria": [{"code": "2.1", "description": "Explain what is meant by a budget"}]}]}],
    "assessment": {"objectives": [{"code": f"AO{i}", "description": d} for i, d in enumerate(["Knowledge", "Application", "Analysis", "Evaluation"], 1)],
                   "weightings": [{"name": "Component 1", "AO1": "12-14%", "AO2": "7-9%", "AO3": "6-8%", "AO4": "6-8%"}]}})
plain_spec = SpecVersion(uploaded_by_teacher_id=lecturer.id, label="BTEC (test)", raw_text="x", parse_status="parsed", structured_json={
    "units": [{"name": "Unit 1", "learning_outcomes": [{"code": "LO1", "title": "Networks", "criteria": [{"code": "P1", "description": "Explain networks"}]}]}]})
db.add_all([exam_spec, plain_spec])
db.commit()
course = Course(teacher_id=lecturer.id, name="A level Business Y12", spec_version_id=exam_spec.id)
plain_course = Course(teacher_id=lecturer.id, name="BTEC IT", spec_version_id=plain_spec.id)
db.add_all([course, plain_course])
db.commit()
db.add_all([CourseTeacher(course_id=course.id, teacher_id=lecturer.id), CourseTeacher(course_id=plain_course.id, teacher_id=lecturer.id)])
db.commit()

# ------------------------------------------------------------------ practice questions
html = pr.practice_home(plain_course.id, request("/p"), -1, db, lecturer).body.decode()
check("practice page explains it needs an exam spec (BTEC course)", "not assessed by exam objectives" in html)
html = pr.practice_home(course.id, request("/p"), 0, db, lecturer).body.decode()
check("exam course: units and topics offered", "Component 1: Business" in html and "LO2 Budgets" in html)


def generate(form):
    tasks = BackgroundTasks()
    resp = asyncio.run(pr.practice_generate(course.id, request("/g", "POST", form), tasks, db, lecturer))
    for t in tasks.tasks:
        t.func(*t.args, **t.kwargs)
    return resp


resp = generate({"unit_index": "0", "count": "6"})
check("no topic ticked -> asked to tick one, nothing created", "error=" in resp.headers["location"] and db.query(PracticeSet).count() == 0)
FAKE["calls"].clear()
resp = generate({"unit_index": "0", "count": "6", "lo-0": "on"})
db.expire_all()
ps = db.query(PracticeSet).order_by(PracticeSet.id.desc()).first()
check("questions written and checked (incomplete one dropped)", ps.status == "ready" and len(ps.questions_json) == 2
      and "1 question(s) were dropped" in (ps.error_detail or ""), (ps.status, ps.error_detail))
check("the AI was given the spec's statements and objectives",
      "Explain what is meant by cash flow" in FAKE["calls"][0] and "AO3: Analysis" in FAKE["calls"][0] and "Budgets" not in FAKE["calls"][0])
html = pr.practice_detail(ps.id, request("/d"), db, lecturer).body.decode()
check("set page shows marks per objective and the mark scheme", "AO1 2" in html and "Mark scheme" in html and "Run live in class" in html)
FAKE["mode"], FAKE["calls"] = "cut_off_once", []
generate({"unit_index": "0", "count": "8", "lo-0": "on", "lo-1": "on"})
db.expire_all()
retried = db.query(PracticeSet).order_by(PracticeSet.id.desc()).first()
check("an answer cut off at its limit is retried asking for fewer questions", retried.status == "ready"
      and len(FAKE["calls"]) == 2 and "Write 8" in FAKE["calls"][0] and "Write 4" in FAKE["calls"][1])
FAKE["mode"] = "ok"
check("another lecturer cannot open the set", "location" in pr.practice_detail(ps.id, request("/d"), db, other).headers)
pr.practice_delete(retried.id, db, lecturer)
check("a set can be deleted", db.get(PracticeSet, retried.id) is None)

# ------------------------------------------------------------------ live lesson from a deck
deck = LessonDeck(course_id=course.id, teacher_id=lecturer.id, content_area_name="Cash flow", title="Cash flow lesson", status="ready",
                  outline_json={"slides": [
                      {"type": "title", "title": "Cash flow", "subtitle": "Lesson 3"},
                      {"type": "retrieval_starter", "title": "Do Now", "questions": ["What is revenue?"]},
                      {"type": "objectives_review", "objectives_check": [{"objective": "Explain cash flow"}]}]})
db.add(deck)
db.commit()
resp = lv.live_start_from_deck(deck.id, db, lecturer)
db.expire_all()
session = db.query(LiveSession).first()
check("go live: session with a 6-character code and the deck's screens", session and len(session.join_code) == 6
      and len(session.screens_json) == 3 and resp.headers["location"] == f"/live/{session.id}/teach")
check("another lecturer cannot start a live session from this deck",
      lv.live_start_from_deck(deck.id, db, other).headers["location"] == "/" and db.query(LiveSession).count() == 1)
html = lv.live_teach(session.id, request("/t"), db, lecturer).body.decode()
check("presenting screen shows the join code", session.join_code in html and "/join" in html)


def join(name, code=None):
    resp = lv.join_submit(request("/join", "POST"), code or session.join_code.lower(), name, db)
    cookie = resp.headers.get("set-cookie", "")
    return resp, cookie.split(";")[0]


resp, _ = join("Ada", "WRONG1")
check("a wrong code is refused politely", resp.status_code == 400 and "not open" in resp.body.decode())
resp_ada, ada = join("Ada")
_, ben = join("Ben")
check("students join with the code (lower case is fine) and get their own cookie",
      resp_ada.status_code == 303 and ada.startswith(f"live_{session.id}=") and ben != ada)

lv.live_set_screen(session.id, 1, db, lecturer)
data = lv.student_data(session.join_code, request("/s", cookie=ada), db)
key = data["screen"]["questions"][0]["key"]
check("student sees the current screen and its question", data["current"] == 1 and data["screen"]["questions"][0]["prompt"] == "What is revenue?")
answer = lambda cookie, **kw: lv.student_answer(session.join_code, request("/a", "POST", cookie=cookie), db=db, **kw)
r1 = answer(ada, screen=1, question_key=key, text="=SUM(A1) money from sales", choice=-1)
r2 = answer(ben, screen=1, question_key=key, text="income", choice=-1)
r3 = answer(ben, screen=1, question_key=key, text="income from sales", choice=-1)  # changes his answer
check("answers are saved, and a student can change theirs", body(r1)["ok"] and body(r3)["ok"]
      and db.query(LiveResponse).count() == 2)
r4 = answer(ada, screen=2, question_key="s2q0", text="", choice=0)
check("a question that is not on the current screen is closed", r4.status_code == 409)
teacher_view = body(lv.live_teacher_data(session.id, db, lecturer))
t = teacher_view["tally"][key]
check("lecturer sees who answered what", teacher_view["joined"] == ["Ada", "Ben"] and t["count"] == 2
      and {a["name"] for a in t["answers"]} == {"Ada", "Ben"})
lv.live_reveal(session.id, 1, 1, db, lecturer)
shared = lv.student_data(session.join_code, request("/s", cookie=ben), db)["shared"][key]
check("'share with class' shows the answers WITHOUT names", "income from sales" in shared["answers"]
      and "Ada" not in json.dumps(shared) and "Ben" not in json.dumps(shared))
lv.live_set_screen(session.id, 2, db, lecturer)
poll_key = lv.student_data(session.join_code, request("/s", cookie=ada), db)["screen"]["questions"][0]["key"]
answer(ada, screen=2, question_key=poll_key, text="", choice=0)
answer(ben, screen=2, question_key=poll_key, text="", choice=2)
check("confidence poll counted", body(lv.live_teacher_data(session.id, db, lecturer))["tally"][poll_key]["counts"] == [1, 0, 1])
check("another lecturer cannot see the live answers", lv.live_teacher_data(session.id, db, other).status_code == 404)

lv.live_end(session.id, db, lecturer)
check("after the end, students are told it has finished", lv.student_data(session.join_code, request("/s", cookie=ada), db)["state"] == "ended")
html = lv.live_summary(session.id, request("/sum"), db, lecturer).body.decode()
check("summary: who joined, the answers, and who said 'Not yet'", "Check in with these students" in html and "Ada" in html and "income from sales" in html)
csv_text = lv.live_summary_csv(session.id, db, lecturer).body.decode()
check("CSV download, with a formula-like answer kept as text", "'=SUM(A1) money from sales" in csv_text and "Got it" in csv_text)
lv.live_delete(session.id, db, lecturer)
check("a session and all its answers can be deleted", db.query(LiveSession).count() == 0 and db.query(LiveResponse).count() == 0)

# ------------------------------------------------------------------ live from a practice set
lv.live_start_from_practice(ps.id, db, lecturer)
db.expire_all()
practice_session = db.query(LiveSession).first()
check("a practice set can run live: one screen per question", practice_session and len(practice_session.screens_json) == 2
      and practice_session.screens_json[1]["title"].startswith("Question 2 (6 marks"))

# ------------------------------------------------------------------ decks get the spec's context
unit = exam_spec.structured_json["units"][0]
with_context = pres._deck_content_text(exam_spec.structured_json, unit, [], {})
plain_unit = plain_spec.structured_json["units"][0]
check("deck prompt: exam spec gets its objectives; a plain spec is unchanged",
      "AO2: Application" in with_context and pres._deck_content_text(plain_spec.structured_json, plain_unit, [], {}) == pres._unit_content_text(plain_unit, [], {}))

db.close()
os.remove(TEMP_DB)
print()
if all(RESULTS):
    print(f"ALL STAGE 6 CHECKS PASSED ({len(RESULTS)})")
else:
    print(f"{RESULTS.count(False)} OF {len(RESULTS)} CHECKS FAILED - send me this output")
    sys.exit(1)
