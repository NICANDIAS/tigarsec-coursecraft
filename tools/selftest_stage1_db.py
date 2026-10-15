"""
Stage 1 self-test for the DATABASE parts (in-use check, re-parse versions, honest status,
safer delete, the new pages). Runs the REAL route functions against a THROWAWAY database
in /tmp - your real database (data/tigarsec_assess.db) is only ever READ, once, to confirm
the 3 new columns exist. AI is switched off; no network calls are made.

Run (from ~/projects/coursecraft, after rebuilding the app so the migration has run):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/selftest_stage1_db.py
Expected last line: "ALL DATABASE CHECKS PASSED (24)"
"""
import os
import sqlite3
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # templates are loaded from "app/templates"

# 1) Point the app at a throwaway database BEFORE anything imports app.database.
TEMP_DB = "/tmp/coursecraft_stage1_selftest.db"
if os.path.exists(TEMP_DB):
    os.remove(TEMP_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{TEMP_DB}"

# 2) Switch AI off.
_stub = types.ModuleType("app.llm_gateway")


def _no_ai(**_kwargs):
    raise RuntimeError("AI disabled in self-test")


_stub.complete_json = _no_ai
_stub.get_effective_provider = lambda *a, **k: "none"
sys.modules["app.llm_gateway"] = _stub

from fastapi import BackgroundTasks  # noqa: E402
from starlette.requests import Request  # noqa: E402

from app import database  # noqa: E402

assert str(database.engine.url).endswith("coursecraft_stage1_selftest.db"), \
    f"SAFETY STOP: not using the throwaway database ({database.engine.url})"

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import Teacher, Course, SpecVersion, ExamExemplar, TeacherSpecExperience  # noqa: E402
from app.routers import specs as specs_router  # noqa: E402
from app.spec_versions import spec_usage, linked_columns  # noqa: E402

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not condition else ""))


def fake_request(path):
    return Request({"type": "http", "method": "GET", "path": path, "query_string": b"", "headers": [],
                    "server": ("test", 80), "scheme": "http", "root_path": ""})


def run_background(tasks: BackgroundTasks):
    for task in tasks.tasks:
        task.func(*task.args, **task.kwargs)


T_LEVEL_TEXT = """3   Core Component
Content area 1: Problem solving
1.1 Computational thinking
1.1.1 Know the definition and understand the purpose of computational thinking.
1.1.2 Know when to use computational thinking.
1.2 Algorithmic design
1.2.1 Know the purpose of algorithms.
"""

# ---------------------------------------------------------------------------
print("== Real database: are the 3 new columns there? (read-only)")
real_db = ROOT / "data" / "tigarsec_assess.db"
if real_db.exists():
    con = sqlite3.connect(f"file:{real_db}?mode=ro", uri=True)
    cols = {row[1] for row in con.execute("PRAGMA table_info(spec_versions)")}
    con.close()
    for c in ("parse_quality", "reparsed_from_id", "source_file_path"):
        check(f"real database has spec_versions.{c}", c in cols,
              "rebuild/restart the app so the automatic migration runs")
else:
    print("SKIP  no real database found at data/tigarsec_assess.db")

# ---------------------------------------------------------------------------
print("\n== Throwaway database")
Base.metadata.create_all(bind=engine)
db = SessionLocal()
admin = Teacher(name="Self Test", email="selftest@example.invalid", hashed_password="x", role="admin")
db.add(admin)
db.commit()

good = SpecVersion(uploaded_by_teacher_id=admin.id, label="Test T Level", raw_text=T_LEVEL_TEXT)
junk = SpecVersion(uploaded_by_teacher_id=admin.id, label="Unreadable", raw_text="Some intro text.\n" * 40)
db.add_all([good, junk])
db.commit()

# Honest status
specs_router._run_parse_job(good.id)
specs_router._run_parse_job(junk.id)
db.expire_all()
good, junk = db.get(SpecVersion, good.id), db.get(SpecVersion, junk.id)
check("readable spec -> status 'parsed'", good.parse_status == "parsed", good.parse_status)
check("readable spec -> quality report saved (verdict good)", (good.parse_quality or {}).get("verdict") == "good", good.parse_quality)
check("readable spec -> report NOT mixed into structured_json", set((good.structured_json or {}).keys()) == {"units"})
check("unreadable spec -> status 'failed' (was wrongly 'parsed' before)", junk.parse_status == "failed", junk.parse_status)
check("unreadable spec -> plain-English reason shown", "could not find any units" in (junk.parse_error or ""))

# In use?
found_tables = sorted({table.name for table, _col in linked_columns()})
expected_tables = ["courses", "exam_exemplars", "iv_requests", "schemes_of_work", "submissions",
                   "teacher_cpd_records", "teacher_iv_outcomes", "teacher_spec_experience",
                   "teacher_unit_qualifications"]
check("finds every table that links to a spec, automatically", all(t in found_tables for t in expected_tables),
      f"found {found_tables}")
check("new spec counts as NOT in use", not spec_usage(db, good.id)["in_use"])
exp_row = TeacherSpecExperience(teacher_id=admin.id, spec_version_id=good.id, years_experience=3)
db.add(exp_row)
db.commit()
check("spec used ONLY by a teacher experience record counts as IN use", spec_usage(db, good.id)["in_use"])
db.delete(exp_row)
db.commit()
course = Course(teacher_id=admin.id, name="Cyber Year 1", spec_version_id=good.id)
db.add(course)
db.commit()
usage = spec_usage(db, good.id)
check("spec attached to a course counts as IN use", usage["in_use"] and usage["counts"].get("course(s)") == 1, usage)

# Re-parse an IN-USE spec -> new version, original untouched
before_json, before_status = good.structured_json, good.parse_status
tasks = BackgroundTasks()
resp = specs_router.reparse_spec(good.id, tasks, db, admin)
new_id = int(resp.headers["location"].rstrip("/").split("/")[-1])
run_background(tasks)
db.expire_all()
original, new_version = db.get(SpecVersion, good.id), db.get(SpecVersion, new_id)
check("re-parse of in-use spec makes a NEW version", new_id != good.id and new_version.reparsed_from_id == good.id)
check("original is unchanged (same units, same status)", original.structured_json == before_json and original.parse_status == before_status)
check("course still points at the original", db.get(Course, course.id).spec_version_id == good.id)
check("new version parsed too", new_version.parse_status == "parsed", new_version.parse_status)

# Re-parse an UNUSED spec -> in place (no library clutter)
tasks = BackgroundTasks()
resp = specs_router.reparse_spec(junk.id, tasks, db, admin)
run_background(tasks)
check("re-parse of unused spec stays in place", resp.headers["location"].endswith(f"/spec-library/{junk.id}"))

# Pages render
page = specs_router.view_spec(good.id, fake_request(f"/spec-library/{good.id}"), "", db, admin)
html = page.body.decode()
check("spec page renders and says it is in use", "Re-parse as a new version" in html)
page = specs_router.view_spec(new_id, fake_request(f"/spec-library/{new_id}"), "", db, admin)
check("new version page links back to the original", f'/spec-library/{good.id}"' in page.body.decode())
page = specs_router.compare_spec_versions(new_id, fake_request(f"/spec-library/{new_id}/compare"), db, admin)
check("'what changed' page renders (same text -> no differences)", "No differences" in page.body.decode())
page = specs_router.spec_library(fake_request("/spec-library"), db, admin)
check("library renders and marks the new version", "new version of spec #" in page.body.decode())

# Safer delete
db.add(ExamExemplar(uploaded_by_teacher_id=admin.id, spec_version_id=new_id, unit_names=["x"],
                    label="Sample brief", file_path="/tmp/none", original_filename="none.pdf"))
db.commit()
resp = specs_router.delete_spec(new_id, db, admin)
check("delete blocked while an exemplar is linked", db.get(SpecVersion, new_id) is not None and "delete_error" in resp.headers["location"])
resp = specs_router.delete_spec(good.id, db, admin)
check("original can't be deleted while a newer version exists", db.get(SpecVersion, good.id) is not None)
db.query(ExamExemplar).delete()
db.commit()
specs_router.delete_spec(new_id, db, admin)
check("unused new version CAN be deleted", db.get(SpecVersion, new_id) is None)

db.close()
os.remove(TEMP_DB)
print()
if all(RESULTS):
    print(f"ALL DATABASE CHECKS PASSED ({len(RESULTS)})")
else:
    print(f"{RESULTS.count(False)} OF {len(RESULTS)} CHECKS FAILED - send me this output")
    sys.exit(1)
