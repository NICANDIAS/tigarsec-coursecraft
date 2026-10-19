"""
Live classroom self-test: "Poll it", removing a student, small groups, photo answers, students'
notes, and deleting a student from a course - through the REAL page functions, with a THROWAWAY
database in /tmp. Your real database is not opened. No AI.

Run (from ~/projects/coursecraft):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/selftest_live_classroom_db.py
Expected last line: "ALL LIVE CLASSROOM CHECKS PASSED (17)"
"""
import io
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

TEMP_DB = "/tmp/coursecraft_classroom_selftest.db"
if os.path.exists(TEMP_DB):
    os.remove(TEMP_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{TEMP_DB}"
_stub = types.ModuleType("app.llm_gateway")
_stub.complete_json = lambda **k: (_ for _ in ()).throw(RuntimeError("AI disabled in self-test"))
_stub.get_effective_provider = lambda *a, **k: "none"
sys.modules["app.llm_gateway"] = _stub

from starlette.requests import Request  # noqa: E402
from app import database  # noqa: E402

assert str(database.engine.url).endswith("coursecraft_classroom_selftest.db"), "SAFETY STOP: not the throwaway database"

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import (Teacher, Course, CourseTeacher, LessonDeck, LiveSession, LiveParticipant, LiveResponse,  # noqa: E402
                        Student, Submission)
from app.routers import live as lv, students as st  # noqa: E402

lv.UPLOAD_DIR = tempfile.mkdtemp(prefix="cc_live_uploads_")  # photos go to a throwaway folder too
RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not condition else ""))


def request(path, cookie=None):
    headers = [(b"cookie", cookie.encode())] if cookie else []
    return Request({"type": "http", "method": "GET", "path": path, "query_string": b"", "headers": headers,
                    "server": ("test", 80), "scheme": "http", "root_path": ""})


def photo_upload():
    from PIL import Image
    buf = io.BytesIO()
    img = Image.new("RGB", (3000, 2000), (200, 30, 30))
    exif = img.getexif()
    exif[0x010F] = "PhoneMaker"  # stands in for hidden data such as where it was taken
    img.save(buf, "JPEG", exif=exif)
    buf.seek(0)
    return types.SimpleNamespace(filename="work.jpg", file=buf)


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
                      {"type": "content", "title": "CVE", "key_points": ["A public ID"], "check_question": "What does CVE stand for?"}]})
db.add(deck)
db.commit()
lv.live_start_from_deck(deck.id, db, lecturer)
db.expire_all()
session = db.query(LiveSession).first()
lv.live_begin(session.id, db, lecturer)


def join(name):
    resp = lv.join_submit(request("/join"), session.join_code, name, db)
    return resp.headers["set-cookie"].split(";")[0]


cookies = {n: join(n) for n in ("Amira", "Ben", "Chloe", "Rude Name")}
ids = {p.display_name: p.id for p in db.query(LiveParticipant).all()}

# ------------------------------------------------------------------ Poll it
lv.live_set_screen(session.id, 2, db, lecturer)        # on the content slide
db.expire_all()
check_key = db.get(LiveSession, session.id).screens_json[2]["questions"][0]["key"]
lv.student_answer(session.join_code, request("/a", cookies["Amira"]), 2, check_key, "Common Vulnerabilities and Exposures", -1, db)
lv.live_set_screen(session.id, 1, db, lecturer)        # back to the starter, then a quick poll
lv.live_quick_poll(session.id, "Ready to move on?", "yesno", "", db, lecturer)
db.expire_all()
s = db.get(LiveSession, session.id)
check("a quick poll appears straight after the current slide, and is shown now",
      s.current_screen == 2 and s.screens_json[2]["kind"] == "poll" and len(s.screens_json) == 4)
moved = db.query(LiveResponse).filter(LiveResponse.question_key == check_key).first()
check("answers on later slides move with their slide", moved.screen_index == 3)
poll_key = s.screens_json[2]["questions"][0]["key"]
lv.student_answer(session.join_code, request("/a", cookies["Ben"]), 2, poll_key, "", 0, db)
tally = lv.live_teacher_data(session.id, db, lecturer)["tally"][poll_key]
check("the class's votes are counted", tally["counts"] == [1, 0, 0] and tally["choices"] == ["Yes", "No", "Not sure"])

# ------------------------------------------------------------------ removing a student
lv.student_answer(session.join_code, request("/a", cookies["Rude Name"]), 2, poll_key, "", 1, db)
lv.live_remove_student(session.id, ids["Rude Name"], db, lecturer)
db.expire_all()
check("a removed student is gone, with their answers", db.get(LiveParticipant, ids["Rude Name"]) is None
      and lv.live_teacher_data(session.id, db, lecturer)["tally"][poll_key]["counts"] == [1, 0, 0])
check("...and their phone is sent back to join again", lv.student_data(session.join_code, request("/d", cookies["Rude Name"]), db)["state"] == "removed")
cookies["Dan"] = join("Dan")  # joins again properly

# ------------------------------------------------------------------ small groups
lv.live_set_screen(session.id, 1, db, lecturer)
starter_key = db.get(LiveSession, session.id).screens_json[1]["questions"][0]["key"]
lv.live_groups_start(session.id, 2, db, lecturer)
db.expire_all()
data = lv.live_teacher_data(session.id, db, lecturer)
check("everyone is put in a group", data["groups_on"] and all(p["group"] for p in data["people"]))
amira = lv.student_data(session.join_code, request("/d", cookies["Amira"]), db)
mate = next(n for n in amira["group"]["members"] if n != "Amira")
lv.student_answer(session.join_code, request("/a", cookies["Amira"]), 1, starter_key, "A weakness someone can use", -1, db)
mate_view = lv.student_data(session.join_code, request("/d", cookies[mate]), db)
check("a group shares one answer - the others see it on their phones",
      mate_view["group_answers"][starter_key] == {"text": "A weakness someone can use", "by": "Amira"})
check("answers given in groups carry the group number",
      db.query(LiveResponse).filter(LiveResponse.question_key == starter_key).first().group_no == amira["group"]["no"])
late = join("Eve")
check("a latecomer joins a group", lv.student_data(session.join_code, request("/d", late), db).get("group") is not None)
lv.live_groups_end(session.id, db, lecturer)
check("ending group work puts everyone back on their own", "group" not in lv.student_data(session.join_code, request("/d", cookies["Amira"]), db))

# ------------------------------------------------------------------ photos
lv.student_answer(session.join_code, request("/a", cookies["Ben"]), 1, starter_key, "", -1, db, photo_upload())
db.expire_all()
r = db.query(LiveResponse).filter(LiveResponse.participant_id == ids["Ben"], LiveResponse.question_key == starter_key).first()
from PIL import Image  # noqa: E402
saved = Image.open(r.image_path) if r and r.image_path else None
check("a photo can be sent as an answer, made smaller", saved is not None and max(saved.size) <= 1600)
check("...with hidden data (e.g. location) removed", saved is not None and len(saved.getexif()) == 0)
check("only the lecturer can open it", lv.live_answer_photo(session.id, r.id, db, lecturer).status_code == 200)

# ------------------------------------------------------------------ students' notes
lv.live_end(session.id, db, lecturer)
notes = lv.student_notes_page(session.id, request("/n", cookies["Amira"]), 1, db)
text = notes.body.decode()
check("after the lesson a student can download their notes and answers",
      "attachment" in notes.headers.get("content-disposition", "") and "My answer: Common Vulnerabilities and Exposures" in text
      and "A public ID" in text)
check("nobody else can read them", lv.student_notes_page(session.id, request("/n"), 0, db).status_code == 404)
lv.live_delete(session.id, db, lecturer)
check("deleting the session deletes the photos", not os.path.exists(os.path.join(lv.UPLOAD_DIR, str(session.id))))

# ------------------------------------------------------------------ deleting a student from a course
plain = Student(course_id=course.id, name="Added By Mistake")
worked = Student(course_id=course.id, name="Has Work")
db.add_all([plain, worked])
db.commit()
from app.models import SpecVersion  # noqa: E402
spec = SpecVersion(uploaded_by_teacher_id=lecturer.id, label="x", raw_text="x")
db.add(spec)
db.commit()
db.add(Submission(student_id=worked.id, spec_version_id=spec.id, original_filename="a.docx"))
db.commit()
st.delete_student(course.id, plain.id, "", db, lecturer)
st.delete_student(course.id, worked.id, "wrong name", db, lecturer)
db.expire_all()
kept = db.get(Student, worked.id)
st.delete_student(course.id, worked.id, "Has Work", db, lecturer)
db.expire_all()
check("a student can be deleted; one with work only when their name is typed",
      db.get(Student, plain.id) is None and kept is not None and db.get(Student, worked.id) is None
      and db.query(Submission).count() == 0)

db.close()
os.remove(TEMP_DB)
print()
if all(RESULTS):
    print(f"ALL LIVE CLASSROOM CHECKS PASSED ({len(RESULTS)})")
else:
    print(f"{RESULTS.count(False)} OF {len(RESULTS)} CHECKS FAILED - send me this output")
    sys.exit(1)
