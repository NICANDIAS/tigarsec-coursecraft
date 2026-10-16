"""
Stage 10 self-test: the class marking report, next steps, the gaps going into the next lesson,
and support/stretch versions of activities - through the REAL page functions, with a THROWAWAY
database in /tmp. Your real database is not opened. The AI is replaced by fixed answers, and
what WOULD have been sent to it is checked for student names.

Run (from ~/projects/coursecraft):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/selftest_stage10_db.py
Expected last line: "ALL STAGE 10 CHECKS PASSED (16)"
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

TEMP_DB = "/tmp/coursecraft_stage10_selftest.db"
if os.path.exists(TEMP_DB):
    os.remove(TEMP_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{TEMP_DB}"
SENT = []


def fake_ai(system="", prompt="", **k):
    SENT.append(prompt)
    if "re-teach" in system:
        return {"criteria": [{"code": "M1", "common_problems": ["Describes each topology instead of comparing them"],
                              "reteach_tip": "Model a side-by-side comparison table."}]}
    if "SUPPORT" in system:
        return {"activities": [{"slide": 2, "support": {"steps": ["Look at photo 1", "Find the switch"], "scaffolds": ["A star topology has..."]},
                                "stretch": {"steps": ["Say which topology suits a hospital and why"], "challenge": "What if the switch fails?"}}]}
    raise RuntimeError("unexpected AI call in self-test")


_stub = types.ModuleType("app.llm_gateway")
_stub.complete_json = fake_ai
_stub.get_effective_provider = lambda *a, **k: "test-stub"
sys.modules["app.llm_gateway"] = _stub

from starlette.requests import Request  # noqa: E402
from app import database  # noqa: E402

assert str(database.engine.url).endswith("coursecraft_stage10_selftest.db"), "SAFETY STOP: not the throwaway database"

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import (Teacher, Course, CourseTeacher, SpecVersion, Student, Submission, MarkingRun, CriterionMark,  # noqa: E402
                        MarkingInsight, LessonDeck, LiveSession)
from app.routers import marking_report as mr, presentations as pr, submissions as sb, live as lv  # noqa: E402

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


RUN_NOW = types.SimpleNamespace(add_task=lambda fn, *a: fn(*a))
UNIT = {"name": "Unit 2 Networking", "code": "2", "learning_outcomes": [{"code": "A", "title": "Network types", "criteria": [
    {"code": "P1", "description": "Explain two network topologies."},
    {"code": "M1", "description": "Compare the performance of two topologies."},
    {"code": "D1", "description": "Evaluate the choice of topology for a given business."}]}]}

Base.metadata.create_all(bind=engine)
db = SessionLocal()
lecturer = Teacher(name="Self Test", email="selftest@example.invalid", hashed_password="x", role="lecturer")
other = Teacher(name="Other", email="other@example.invalid", hashed_password="x", role="lecturer")
db.add_all([lecturer, other])
db.commit()
spec = SpecVersion(uploaded_by_teacher_id=lecturer.id, label="BTEC IT", raw_text="x", parse_status="parsed", structured_json={"units": [UNIT]})
db.add(spec)
db.commit()
course = Course(teacher_id=lecturer.id, name="BTEC IT Y1", spec_version_id=spec.id)
db.add(course)
db.commit()
db.add(CourseTeacher(course_id=course.id, teacher_id=lecturer.id))
db.commit()

MARKS = {
    "Amira Khan": [("P1", "Met", ""), ("M1", "Not met", "Amira only described one topology, no comparison."), ("D1", "Not met", "")],
    "Ben O'Neill": [("P1", "Met", ""), ("M1", "Partly met", "Ben lists features but never says which performs better."), ("D1", "Not met", "")],
    "Chloe Price": [("P1", "Met", ""), ("M1", "Not met", "Chloe compares cost rather than performance."), ("D1", "Not met", "")],
    "Dan Old": [("P1", "Not met", "Only one topology explained."), ("M1", "Met", ""), ("D1", "Met", "")],
}
subs = {}
for name, marks in MARKS.items():
    st = Student(course_id=course.id, name=name)
    db.add(st)
    db.commit()
    sub = Submission(student_id=st.id, spec_version_id=spec.id, target_unit_name=UNIT["name"], original_filename="work.docx", status="needs_review")
    db.add(sub)
    db.commit()
    run = MarkingRun(submission_id=sub.id, status="needs_review")
    db.add(run)
    db.commit()
    db.add_all([CriterionMark(marking_run_id=run.id, criterion_ref=f"{ref} {UNIT['name']}", ai_mark=m, final_mark=m, final_comment=c)
                for ref, m, c in marks])
    db.commit()
    subs[name] = sub.id

# ------------------------------------------------------------------ the class marking report
page = mr.marking_report(course.id, request("/r"), -1, db, lecturer).body.decode()
check("the report finds the criteria the class found hard", "The class found 2 criteria hard" in page and "Compare the performance" in page)
check("...and who still needs each one (lecturer only)", "Still to secure: Amira Khan, Ben O&#39;Neill, Chloe Price" in page)
check("every student gets next steps by the BTEC rule (Pass first)",
      "To reach Merit:" in page and "To reach Pass:" in page and "Explain two network topologies." in page)
check("another lecturer cannot open it", mr.marking_report(course.id, request("/r"), -1, db, other).status_code == 303)

mr.marking_report_insight(course.id, RUN_NOW, 0, db, lecturer)
db.expire_all()
insight = db.query(MarkingInsight).first()
check("the AI summary of common problems is stored", insight and insight.status == "ready" and "M1" in (insight.insight_json or {}),
      insight and insight.error_detail)
check("no student name was sent to the AI", SENT and not any(n in SENT[-1] for n in ("Amira", "Ben", "O'Neill", "Chloe", "Dan", "Khan", "Price")), SENT[-1:] )
page = mr.marking_report(course.id, request("/r"), 0, db, lecturer).body.decode()
check("the report shows the common problems and a re-teach idea",
      "Describes each topology instead of comparing them" in page and "Model a side-by-side comparison table." in page)

# ------------------------------------------------------------------ the next lesson re-teaches them
new_page = pr.new_presentation_page(course.id, request("/n", query="revisit_unit=0"), 0, 0, 0, db, lecturer).body.decode()
check("the new-lesson page offers the marking gaps, ticked",
      'value="unit0:crit:M1"' in new_page and "checked" in new_page.split('value="unit0:crit:M1"')[1][:200])
queued = []
asyncio.run(pr.generate_presentation(course.id, request("/g", "POST", form=[("revisit", "unit0:crit:M1"), ("selected_los", "0"),
                                                                             ("differentiate", "yes")]),
                                     types.SimpleNamespace(add_task=lambda fn, *a: queued.append(a)), 0, "Topologies 2", 1.0, "", "",
                                     0, 0, db, lecturer))
sent = queued[0][3] if queued else ""
check("the ticked gap and its common problem go to the lesson writer, without names",
      "M1 Compare the performance" in sent and "Describes each topology" in sent and "Amira" not in sent, sent)
check("...and the lecturer's 'support and stretch' choice is passed on", queued and queued[0][5] is True)

# ------------------------------------------------------------------ next steps on the student's own feedback
review = sb.review_submission(subs["Ben O'Neill"], request("/rv"), db, lecturer).body.decode()
check("the student's review page shows their next steps", "Next steps to reach Merit" in review and "Compare the performance" in review)
check("...and they are in the Copy-for-Teams feedback", "Next steps to reach Merit:\n- M1: Compare the performance of two topologies. (partly met)" in review)

# ------------------------------------------------------------------ support and stretch
deck = LessonDeck(course_id=course.id, teacher_id=lecturer.id, content_area_name="Topologies", title="Topologies", status="ready",
                  pptx_path="/tmp/coursecraft_stage10_no_such_file.pptx", outline_json={"lesson_title": "Topologies", "slides": [
                      {"type": "title", "title": "Topologies", "subtitle": ""},
                      {"type": "objectives", "objectives": ["Compare star and mesh"]},
                      {"type": "activity", "title": "Topology hunt", "instructions": ["Find three topologies"], "grouping": "pairs"}]})
db.add(deck)
db.commit()
pr.differentiate_deck(deck.id, RUN_NOW, db, lecturer)
db.expire_all()
deck = db.get(LessonDeck, deck.id)
act = deck.outline_json["slides"][2]
check("support and stretch versions are written for the activity", deck.outline_json.get("differentiation_status") == "ready"
      and act["differentiation"]["support"]["steps"][0] == "Look at photo 1")
deck_page = pr.view_deck(deck.id, request(f"/lesson-decks/{deck.id}"), db, lecturer).body.decode()
check("the lesson page shows them and offers the worksheets", "Support and stretch" in deck_page and "What if the switch fails?" in deck_page
      and "Download activity resources (1)" in deck_page)
download = pr.download_deck_resources(deck.id, db, lecturer)
check("the worksheets download as a Word file", getattr(download, "media_type", "").endswith("wordprocessingml.document"))
if getattr(download, "path", None) and os.path.exists(download.path):
    os.remove(download.path)  # the self-test leaves nothing behind in storage/
lv.live_start_from_deck(deck.id, db, lecturer)
db.expire_all()
screen = next(s for s in db.query(LiveSession).first().screens_json if s["title"] == "Topology hunt")
check("in a live lesson the versions go to students' phones", screen["support"]["steps"] == ["Look at photo 1", "Find the switch"]
      and screen["stretch"]["challenge"] == "What if the switch fails?")

db.close()
os.remove(TEMP_DB)
print()
if all(RESULTS):
    print(f"ALL STAGE 10 CHECKS PASSED ({len(RESULTS)})")
else:
    print(f"{RESULTS.count(False)} OF {len(RESULTS)} CHECKS FAILED - send me this output")
    sys.exit(1)
