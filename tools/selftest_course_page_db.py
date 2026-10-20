"""
Course page redesign self-test: the number tiles, "Needs you", "This week", Run live on each
lesson and the folded-away settings - through the REAL page function, with a THROWAWAY database
in /tmp. Your real database is not opened. No AI is used.

Run (from ~/projects/coursecraft):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/selftest_course_page_db.py
Expected last line: "ALL COURSE PAGE CHECKS PASSED (12)"
"""
import datetime
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

TEMP_DB = "/tmp/coursecraft_course_page_selftest.db"
if os.path.exists(TEMP_DB):
    os.remove(TEMP_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{TEMP_DB}"

from starlette.requests import Request  # noqa: E402
from app import database  # noqa: E402

assert str(database.engine.url).endswith("coursecraft_course_page_selftest.db"), "SAFETY STOP: not the throwaway database"

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import (Teacher, Course, CourseTeacher, SpecVersion, Student, Submission, MarkingRun, CriterionMark,  # noqa: E402
                        LessonDeck, SchemeOfWork)
from app.routers import courses as cr  # noqa: E402

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not condition else ""))


def request(path):
    return Request({"type": "http", "method": "GET", "path": path, "query_string": b"", "headers": [],
                    "server": ("test", 80), "scheme": "http", "root_path": ""})


UNIT = {"name": "Unit 2 Networking", "learning_outcomes": [{"code": "A", "criteria": [
    {"code": "P1", "description": "Explain two topologies."}, {"code": "M1", "description": "Compare them."}]}]}

Base.metadata.create_all(bind=engine)
db = SessionLocal()
lecturer = Teacher(name="Self Test", email="selftest@example.invalid", hashed_password="x", role="lecturer")
db.add(lecturer)
db.commit()


def page(course):
    resp = cr.course_detail(course.id, request(f"/courses/{course.id}"), "", db, lecturer)
    return resp.status_code, resp.body.decode()


# ------------------------------------------------------------------ a brand-new, empty course
empty = Course(teacher_id=lecturer.id, name="Empty course")
db.add(empty)
db.commit()
db.add(CourseTeacher(course_id=empty.id, teacher_id=lecturer.id))
db.commit()
code, html = page(empty)
check("an empty course page loads", code == 200, html[-800:] if code != 200 else "")
check("...says it's all caught up, and opens the settings so a spec can be attached",
      "All caught up" in html and 'id="settings" open' in html and "No spec attached" in html)

# ------------------------------------------------------------------ a course in use
spec = SpecVersion(uploaded_by_teacher_id=lecturer.id, label="BTEC IT", raw_text="x", parse_status="parsed", structured_json={"units": [UNIT]})
db.add(spec)
db.commit()
today = datetime.date.today()
course = Course(teacher_id=lecturer.id, name="BTEC IT Year 1", spec_version_id=spec.id, start_date=today - datetime.timedelta(days=8))
db.add(course)
db.commit()
db.add(CourseTeacher(course_id=course.id, teacher_id=lecturer.id))
sow = SchemeOfWork(course_id=course.id, title="Autumn plan", status="ready", start_date=today - datetime.timedelta(days=8),
                   weeks_json=[{"week": 1, "title": "Topologies"}, {"week": 2, "title": "Network devices"}, {"week": 3, "title": "IP addressing"}])
db.add(sow)
db.commit()
decks = []
for i in range(8):
    d = LessonDeck(course_id=course.id, teacher_id=lecturer.id, content_area_name="x", title=f"Lesson number {i}", status="ready")
    db.add(d)
    decks.append(d)
db.commit()
marked = {"Amira Khan": [("P1", "Met"), ("M1", "Met")], "Ben Okafor": [("P1", "Not met"), ("M1", "Not met")]}
for name, marks in marked.items():
    st = Student(course_id=course.id, name=name)
    db.add(st)
    db.commit()
    sub = Submission(student_id=st.id, spec_version_id=spec.id, target_unit_name=UNIT["name"], original_filename="w.docx", status="needs_review",
                     achieved_grade="Merit" if name == "Amira Khan" else "Not yet achieved")
    db.add(sub)
    db.commit()
    run = MarkingRun(submission_id=sub.id, status="needs_review")
    db.add(run)
    db.commit()
    db.add_all([CriterionMark(marking_run_id=run.id, criterion_ref=r, ai_mark=m, final_mark=m) for r, m in marks])
    db.commit()
db.add(Student(course_id=course.id, name="Left Early", is_active=False))
db.commit()

code, html = page(course)
check("the course page loads", code == 200, html[-800:] if code != 200 else "")
check("tile: 2 students, 1 archived", '<span class="n">2</span>' in html and "1 archived" in html)
check("tile: 2 pieces of work to review, linking to the first", "To review" in html and "/submissions/1/review" in html)
check("tile and Needs you: Ben is at risk (not yet passing)", "Student at risk" in html and 'title="Ben Okafor"' in html)
check("tile: criteria met is the class average (2 of 4 = 50%)", "50%" in html)
check("This week follows the scheme's dates (week 2 of 3)", "week 2 of 3" in html and "Network devices" in html and "Next: IP addressing" in html)
check("...and offers to make that week's lesson", f"sow_id={sow.id}&amp;week=2" in html or f"sow_id={sow.id}&week=2" in html)
check("every ready lesson has Run live", html.count("/live/start") == 8)
check("only 6 lessons show at first, with search and See all", html.count("hidden data-more") == 2 and "See all 8" in html and "deck-search" in html)
check("the settings are folded away once a spec is attached", 'id="settings" open' not in html and "Remove from this course" in html)

db.close()
os.remove(TEMP_DB)
print()
if all(RESULTS):
    print(f"ALL COURSE PAGE CHECKS PASSED ({len(RESULTS)})")
else:
    print(f"{RESULTS.count(False)} OF {len(RESULTS)} CHECKS FAILED - send me this output")
    sys.exit(1)
