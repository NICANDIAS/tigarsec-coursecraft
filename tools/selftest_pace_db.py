"""
Teaching at the class's pace - self-test: "How did week 5 go?" (where the live lesson reached, the
class's confidence and quiz answers), carrying the lesson on into next week, the planning page and
lesson page picking up from that slide, combining two later weeks, and undoing it all - through the
REAL page functions, with a THROWAWAY database in /tmp. Your real database is not opened. No AI is used.

Run (from ~/projects/coursecraft):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/selftest_pace_db.py
Expected last line: "ALL PACE CHECKS PASSED (14)"
"""
import datetime
import os
import sys
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

TEMP_DB = "/tmp/coursecraft_pace_selftest.db"
if os.path.exists(TEMP_DB):
    os.remove(TEMP_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{TEMP_DB}"

from starlette.requests import Request  # noqa: E402
from app import database  # noqa: E402

assert str(database.engine.url).endswith("coursecraft_pace_selftest.db"), "SAFETY STOP: not the throwaway database"

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import (Teacher, Course, CourseTeacher, SpecVersion, SchemeOfWork, LessonDeck, LiveSession,  # noqa: E402
                        LiveParticipant, LiveResponse)
from app.routers import pace as pr, term_plan as tpr, presentations as pres, live as lv  # noqa: E402

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not condition else ""))


def request(path, query=""):
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    return Request({"type": "http", "method": "GET", "path": path, "query_string": query.encode(), "headers": [],
                    "server": ("test", 80), "scheme": "http", "root_path": "", "client": ("1.2.3.4", 1)}, receive)


Base.metadata.create_all(bind=engine)
db = SessionLocal()
lecturer = Teacher(name="Self Test", email="selftest@example.invalid", hashed_password="x", role="lecturer")
other = Teacher(name="Other", email="other@example.invalid", hashed_password="x", role="lecturer")
db.add_all([lecturer, other])
db.commit()
UNIT = {"name": "Unit 2", "learning_outcomes": [{"code": "A", "criteria": [{"code": c, "description": c} for c in ("2.1", "2.2", "2.3", "2.4", "2.5")]}]}
spec = SpecVersion(uploaded_by_teacher_id=lecturer.id, label="DSS", raw_text="x", parse_status="parsed", structured_json={"units": [UNIT]})
db.add(spec)
db.commit()
today = datetime.date.today()
monday = today - datetime.timedelta(days=today.weekday())
start = monday - datetime.timedelta(days=28)   # so this week is week 5
course = Course(teacher_id=lecturer.id, name="DSS Y2", spec_version_id=spec.id)
db.add(course)
db.commit()
db.add(CourseTeacher(course_id=course.id, teacher_id=lecturer.id))
weeks = [{"week": n, "title": t, "learning_outcomes": lo} for n, t, lo in [
    (1, "Threats", ["2.1"]), (2, "Social engineering", ["2.1"]), (3, "Threat actors", ["2.2"]), (4, "Risk stages", ["2.2"]),
    (5, "CVE and CVSS", ["2.2"]), (6, "Mitigations", ["2.3"]), (7, "Risk types", ["2.4", "2.5"]), (8, "Risk measures", ["2.5"])]]
sow = SchemeOfWork(course_id=course.id, title="Content area 2", status="ready", start_date=start,
                   end_date=start + datetime.timedelta(days=7 * 7 + 4), weeks_json=weeks, selected_unit_names=["Unit 2"])
db.add(sow)
db.commit()
deck = LessonDeck(course_id=course.id, teacher_id=lecturer.id, content_area_name="x", title="CVE and CVSS Scoring", status="ready",
                  source_sow_id=sow.id, source_week=5,
                  outline_json={"lesson_title": "CVE", "slides": [{"type": "content", "title": f"Slide {i}"} for i in range(22)]})
db.add(deck)
db.commit()

# a live lesson that stopped on slide 14, with an unsure class
screens = [{"kind": "content", "title": f"Slide {i}", "body": [], "questions": [], "slide": i} for i in range(22)]
screens[3]["questions"] = [{"key": "c1", "kind": "confidence", "prompt": "I can read a CVSS vector",
                            "choices": ["Not yet", "Getting there", "Got it"]}]
live = LiveSession(teacher_id=lecturer.id, course_id=course.id, title="CVE", join_code="ABC123", lesson_deck_id=deck.id,
                   screens_json=screens, current_screen=13, state="ended", started_at=datetime.datetime.now())
db.add(live)
db.commit()
for i, c in enumerate((0, 0, 1, 2, 1)):
    p = LiveParticipant(session_id=live.id, display_name=f"S{i}", token=f"t{i}")
    db.add(p)
    db.commit()
    db.add(LiveResponse(session_id=live.id, participant_id=p.id, screen_index=3, question_key="c1", answer_choice=c))
db.commit()

page = pr.how_did_it_go(deck.id, request("/h"), db, lecturer).body.decode()
check("'How did week 5 go?' shows where the live lesson stopped", "How did week 5 go?" in page and "slide 14 of 22" in page)
check("...the class's confidence, and CourseCraft's suggestion", "80% not sure yet" in page and "CourseCraft suggests another week" in page)
check("...offers to pick up from slide 14 with the same PowerPoint", 'name="from_slide" value="14"' in page and "this same PowerPoint" in page)
check("...and shows what would move (week 6 a week later)", "Week 6" in page and "Mitigations" in page)
check("...and warns it would run past the scheme's end date", "past the end of its end date" in page)
check("another lecturer can't open it", pr.how_did_it_go(deck.id, request("/h"), db, other).status_code == 303)

resp = pr.carry_on(deck.id, 14, "CVSS vectors not secure", db, lecturer)
db.expire_all()
ext = db.get(SchemeOfWork, sow.id).pace_json["extensions"]
check("'Carry on next week' is recorded with the slide and the reason", resp.headers["location"].endswith("?carried=5")
      and ext[0]["week"] == 5 and ext[0]["from_slide"] == 14 and ext[0]["reason"] == "CVSS vectors not secure")

plan = tpr.plan_term_page(course.id, sow.id, request("/p", "carried=5"), db, lecturer).body.decode()
check("the planning page shows week 5 continued, same PowerPoint from slide 14", "CVE and CVSS (continued)" in plan
      and "Same PowerPoint, from slide 14" in plan and "Changes to the plan" in plan)
check("...says it now runs past the end, and suggests combining weeks 7 + 8 (they share 2.5)",
      "past the end of its end date" in plan and "Weeks 7 + 8" in plan and "Both cover 2.5" in plan)

lesson = pres.view_deck(deck.id, request("/l"), db, lecturer).body.decode()
check("the lesson page offers Present / Run live from slide 14", "present?from=14" in lesson and "Run live from slide 14" in lesson
      and "How did it go?" in lesson)
lv.live_start_from_deck(deck.id, db, lecturer, 14)
db.expire_all()
new = db.query(LiveSession).order_by(LiveSession.id.desc()).first()
check("Run live from slide 14 opens on slide 14", new.id != live.id and new.current_screen == 13)

pr.pace_merge(course.id, sow.id, 7, 8, "", db, lecturer)
db.expire_all()
plan = tpr.plan_term_page(course.id, sow.id, request("/p"), db, lecturer).body.decode()
check("combining weeks 7 + 8 wins the week back", db.get(SchemeOfWork, sow.id).pace_json["merges"] == [[7, 8]]
      and "past the end" not in plan and "Risk types + Risk measures" in plan)
pr.pace_merge(course.id, sow.id, 7, 8, "1", db, lecturer)
pr.pace_undo(course.id, sow.id, 0, db, lecturer)
db.expire_all()
check("both can be undone", db.get(SchemeOfWork, sow.id).pace_json == {"extensions": [], "merges": []})
check("another lecturer can't change the pace", (pr.carry_on(deck.id, 3, "", db, other), db.expire_all(),
                                                 db.get(SchemeOfWork, sow.id).pace_json["extensions"])[2] == [])

db.close()
os.remove(TEMP_DB)
print()
if all(RESULTS):
    print(f"ALL PACE CHECKS PASSED ({len(RESULTS)})")
else:
    print(f"{RESULTS.count(False)} OF {len(RESULTS)} CHECKS FAILED - send me this output")
    sys.exit(1)
