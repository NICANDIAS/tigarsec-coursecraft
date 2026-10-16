"""
Stage 8 self-test: importing a class list and running a live quiz - through the REAL page
functions, with a THROWAWAY database in /tmp. Your real database is not opened. The AI is
replaced by a fixed answer, so nothing is sent anywhere.

Run (from ~/projects/coursecraft):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/selftest_stage8_db.py
Expected last line: "ALL STAGE 8 CHECKS PASSED (25)"
"""
import asyncio
import datetime
import os
import re
import shutil
import sys
import tempfile
import types
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

TEMP_DB = "/tmp/coursecraft_stage8_selftest.db"
if os.path.exists(TEMP_DB):
    os.remove(TEMP_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{TEMP_DB}"
AI_ANSWER = {"questions": [
    {"question": "What does a firewall do?", "choices": ["Filters network traffic", "Stores passwords", "Speeds up Wi-Fi", "Backs up files"],
     "answer": 0, "explanation": "It allows or blocks traffic using rules."},
    {"question": "Which of these is phishing?", "choices": ["A fake bank email", "A software update", "A strong password", "A VPN"], "answer": 0},
]}
_stub = types.ModuleType("app.llm_gateway")
_stub.complete_json = lambda **k: AI_ANSWER
_stub.get_effective_provider = lambda *a, **k: "test-stub"
sys.modules["app.llm_gateway"] = _stub

from starlette.requests import Request  # noqa: E402
from app import database  # noqa: E402

assert str(database.engine.url).endswith("coursecraft_stage8_selftest.db"), "SAFETY STOP: not the throwaway database"

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import Teacher, Course, CourseTeacher, Student, LessonDeck, QuizSet, LiveSession, LiveResponse  # noqa: E402
from app.routers import students as st, quizzes as qr, live as lv, courses as cr, presentations as pr  # noqa: E402

# the import keeps uploads in storage/imports - point it at a throwaway folder for this test
TMP = tempfile.mkdtemp(prefix="cc_stage8_db_")
st.IMPORT_DIR = os.path.join(TMP, "imports")

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not condition else ""))


def request(path, method="GET", query="", form=None, cookies=None):
    body = urlencode(form or [], doseq=True).encode()
    headers = [(b"content-type", b"application/x-www-form-urlencoded")]
    if cookies:
        headers.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    return Request({"type": "http", "method": method, "path": path, "query_string": query.encode(), "headers": headers,
                    "server": ("test", 80), "scheme": "http", "root_path": "", "client": ("1.2.3.4", 1)}, receive)


def location(response):
    return response.headers.get("location", "")


Base.metadata.create_all(bind=engine)
db = SessionLocal()
lecturer = Teacher(name="Self Test", email="selftest@example.invalid", hashed_password="x", role="lecturer")
other = Teacher(name="Other", email="other@example.invalid", hashed_password="x", role="lecturer")
db.add_all([lecturer, other])
db.commit()
course = Course(teacher_id=lecturer.id, name="T Level Computing Y2")
db.add(course)
db.commit()
db.add(CourseTeacher(course_id=course.id, teacher_id=lecturer.id))
db.add_all([Student(course_id=course.id, name="Benjamin O'Neill", student_ref="S1234568"),
            Student(course_id=course.id, name="Dan Old", student_ref="S0000001")])
db.commit()

# ------------------------------------------------------------------ class list import
EXPORT = ("Dudley College - Class List\n\nStudent Ref,Forename,Surname,Email,Group Code\n"
          "S1234567,Amira,KHAN,a@x.example,TLCOMP2A\nS1234568,Ben,O'NEILL,b@x.example,TLCOMP2A\n"
          "S1234569,Chloe,PRICE,c@x.example,TLCOMP2B\n")
r = asyncio.run(st.import_students_upload(course.id, request("/i", "POST"), None, EXPORT, db, lecturer))
token = re.search(r"t=([A-Za-z0-9_-]+)", location(r))
check("a pasted ProSolution list is read and kept only for the preview", token is not None, location(r))
token = token.group(1) if token else ""
page = st.import_students_page(course.id, request("/i", query=f"t={token}"), db, lecturer).body.decode()
check("with two classes in the file, the lecturer is asked to pick theirs first", "Tick your class above" in page)
page = st.import_students_page(course.id, request("/i", query=urlencode(
    {"t": token, "mapped": 1, "col_ref": 0, "col_forename": 1, "col_surname": 2, "col_fullname": "", "col_group": 4,
     "group": "TLCOMP2A", "archive": 1})), db, lecturer).body.decode()
check("the preview shows who is new, who changed and who would be archived",
      "Amira Khan" in page and "was Benjamin O&#39;Neill" in page and "Dan Old" in page and "Chloe Price" not in page)
check("emails in the file are never shown or kept", "a@x.example" not in page)
check("another lecturer can't open this upload", "expired" in st.import_students_page(
    course.id, request("/i", query=f"t={token}"), db, other).body.decode() or
      st.import_students_page(course.id, request("/i", query=f"t={token}"), db, other).status_code == 303)
form = [("t", token), ("col_ref", "0"), ("col_forename", "1"), ("col_surname", "2"), ("col_fullname", ""), ("col_group", "4"),
        ("group", "TLCOMP2A"), ("archive", "1"), ("pick", "add:s1234567")]
students_before = db.query(Student).count()
student_ids = {s.name: s.id for s in db.query(Student).all()}
ben_id, dan_id = student_ids["Benjamin O'Neill"], student_ids["Dan Old"]
form += [("pick", f"s:{ben_id}"), ("pick", f"x:{dan_id}")]
r = asyncio.run(st.import_students_confirm(course.id, request("/c", "POST", form=form), db, lecturer))
db.expire_all()
names = {s.name: (s.student_ref, s.is_active) for s in db.query(Student).filter(Student.course_id == course.id).all()}
check("ticked students are imported: one added, one renamed, one archived",
      names.get("Amira Khan") == ("S1234567", True) and names.get("Ben O'Neill") == ("S1234568", True)
      and names.get("Dan Old") == ("S0000001", False), str(names))
check("nobody is deleted (archived students keep their history)", db.query(Student).count() == students_before + 1)
check("the course page is told what happened", "1+added" in location(r) or "1%20added" in location(r), location(r))
check("the uploaded list is deleted once used", not os.listdir(st.IMPORT_DIR))
r = asyncio.run(st.import_students_upload(course.id, request("/i", "POST"), None, "", db, lecturer))
check("an empty upload gets a plain message, not an error page", "error=" in location(r))

# ------------------------------------------------------------------ quiz from a lesson deck (AI stubbed)
deck = LessonDeck(course_id=course.id, teacher_id=lecturer.id, content_area_name="Network security", title="Network security",
                  status="ready", outline_json={"lesson_title": "Network security", "slides": [
                      {"type": "content", "title": "Firewalls", "key_points": ["Filter traffic using rules"]}]})
db.add(deck)
db.commit()
r = qr.quiz_from_deck(deck.id, types.SimpleNamespace(add_task=lambda fn, *a: fn(*a)), 8, db, lecturer)
db.expire_all()
quiz = db.query(QuizSet).first()
check("a quiz is written from the lesson and checked", quiz and quiz.status == "ready" and len(quiz.questions_json) == 2,
      quiz and quiz.error_detail)
check("another lecturer can't open it", qr.quiz_detail(quiz.id, request("/q"), db, other).status_code == 303)
bad = asyncio.run(qr.quiz_save(quiz.id, request("/s", "POST", form=[
    ("title", "Network security"), ("seconds", "20"), ("n", "1"), ("q0_question", "Half typed"), ("q0_c0", "Only one")]), db, lecturer))
check("a half-typed question is refused with what to fix - and what was typed is kept",
      bad.status_code == 400 and "Half typed" in bad.body.decode() and "needs the question" in bad.body.decode())
form = [("title", "Network security quiz"), ("seconds", "30"), ("n", "3")]
for i, q in enumerate(quiz.questions_json):
    form += [(f"q{i}_question", q["question"]), (f"q{i}_answer", str(q["answer"])), (f"q{i}_explanation", q.get("explanation", ""))]
    form += [(f"q{i}_c{j}", c) for j, c in enumerate(q["choices"])]
form += [("q2_question", "Which port does HTTPS use?"), ("q2_c0", "80"), ("q2_c1", "443"), ("q2_answer", "1")]
asyncio.run(qr.quiz_save(quiz.id, request("/s", "POST", form=form), db, lecturer))
db.expire_all()
quiz = db.get(QuizSet, quiz.id)
check("the lecturer can edit it and add their own question", len(quiz.questions_json) == 3 and quiz.seconds_per_question == 30
      and quiz.questions_json[2] == {"question": "Which port does HTTPS use?", "choices": ["80", "443"], "answer": 1, "explanation": ""})

course_page = cr.course_detail(course.id, request(f"/courses/{course.id}"), "", db, lecturer).body.decode()
deck_page = pr.view_deck(deck.id, request(f"/lesson-decks/{deck.id}"), db, lecturer).body.decode()
check("the course page offers the class list import and lists the quiz",
      "Import class list" in course_page and "Network security quiz" in course_page and "3 questions" in course_page)
check("the lesson page offers 'Make a quiz from this lesson' and lists its quiz",
      "/quiz/generate" in deck_page and "Network security quiz" in deck_page)

# ------------------------------------------------------------------ run it live
lv.live_start_from_quiz(quiz.id, db, lecturer)
db.expire_all()
session = db.query(LiveSession).filter(LiveSession.quiz_set_id == quiz.id).first()
check("a live quiz starts with a get-ready screen, the questions and a leaderboard",
      [s["kind"] for s in session.screens_json] == ["quiz_intro"] + ["quiz_question"] * 3 + ["leaderboard"])


def join(name):
    resp = lv.join_submit(request("/join", "POST"), session.join_code, name, db)
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)
    return {cookie[0]: cookie[1]}


amira, ben = join("Amira"), join("Ben")
lv.live_set_screen(session.id, 1, db, lecturer)
db.expire_all()
q0 = session.screens_json[1]["questions"][0]
seen = lv.student_data(session.join_code, request("/d", cookies=amira), db)
check("a phone does not get the right answer before the reveal", "answer" not in seen["screen"]["questions"][0]
      and seen["elapsed"] is not None)
lv.student_answer(session.join_code, request("/a", "POST", cookies=amira), 1, q0["key"], "", q0["answer"], db)
again = lv.student_answer(session.join_code, request("/a", "POST", cookies=amira), 1, q0["key"], "", (q0["answer"] + 1) % 4, db)
check("one answer per question - it can't be changed after seeing others", again.status_code == 409)
session.screen_started_at = datetime.datetime.utcnow() - datetime.timedelta(seconds=40)
db.commit()
late = lv.student_answer(session.join_code, request("/a", "POST", cookies=ben), 1, q0["key"], "", q0["answer"], db)
check("an answer after the time is up is refused", late.status_code == 409)
points = db.query(LiveResponse).filter(LiveResponse.session_id == session.id).first().points
check("a quick right answer scores close to 1000 points", points is not None and 900 <= points <= 1000, str(points))
lv.live_reveal(session.id, 1, 1, db, lecturer)
seen = lv.student_data(session.join_code, request("/d", cookies=amira), db)
check("after the reveal, the phone shows the answer, its points and its place",
      seen["screen"]["questions"][0]["answer"] == q0["answer"] and seen["mine"][q0["key"]]["points"] == points
      and seen["me"] == {"rank": 1, "points": points, "players": 2})
teacher_view = lv.live_teacher_data(session.id, db, lecturer)
check("the lecturer sees the leaderboard and how the class voted",
      teacher_view["leaderboard"][0]["name"] == "Amira" and teacher_view["tally"][q0["key"]]["correct_count"] == 1)
lv.live_end(session.id, db, lecturer)
summary = lv.live_summary(session.id, request("/s"), db, lecturer).body.decode()
csv_text = lv.live_summary_csv(session.id, db, lecturer).body.decode()
check("the summary has the final leaderboard and each question's results",
      "Quiz leaderboard" in summary and "1 of 1 right" in summary and "Amira" in summary)
check("the CSV export has right/wrong and points", "Correct,Points" in csv_text.splitlines()[0] and f",yes,{points}" in csv_text)

db.close()
os.remove(TEMP_DB)
shutil.rmtree(TMP, ignore_errors=True)
print()
if all(RESULTS):
    print(f"ALL STAGE 8 CHECKS PASSED ({len(RESULTS)})")
else:
    print(f"{RESULTS.count(False)} OF {len(RESULTS)} CHECKS FAILED - send me this output")
    sys.exit(1)
