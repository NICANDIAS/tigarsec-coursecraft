"""
Stage 5 self-test: the layout learner inside the REAL parse job and library page, with a
THROWAWAY database in /tmp. Your real database is not opened. AI is switched off.
Uses the small made-up test specs in tools/fixtures/ (no real spec PDFs needed).

  1. a spec in a new table layout is read with no AI, and the layout is SAVED
  2. a second spec in that layout is read with the saved layout (no discovery), counted
  3. a second, different layout is learned too
  4. the library page lists what has been learned; "Forget" removes one (and logs it)
  5. a spec that cannot be read stays honest ("failed"), and nothing is learned from it

Run (from ~/projects/coursecraft):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/selftest_stage5_db.py
Expected last line: "ALL STAGE 5 CHECKS PASSED (14)"
"""
import json
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
FIX = ROOT / "tools" / "fixtures"

TEMP_DB = "/tmp/coursecraft_stage5_selftest.db"
if os.path.exists(TEMP_DB):
    os.remove(TEMP_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{TEMP_DB}"

_stub = types.ModuleType("app.llm_gateway")
_stub.complete_json = lambda **k: (_ for _ in ()).throw(RuntimeError("AI disabled in self-test"))
_stub.get_effective_provider = lambda *a, **k: "none"
sys.modules["app.llm_gateway"] = _stub

from starlette.requests import Request  # noqa: E402
from app import database  # noqa: E402

assert str(database.engine.url).endswith("coursecraft_stage5_selftest.db"), "SAFETY STOP: not the throwaway database"

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import Teacher, SpecVersion, DiscoveredSpecFormat, AuditLogEntry  # noqa: E402
from app.docx_annotator import extract_text  # noqa: E402
from app.routers import specs as r  # noqa: E402
from app.spec_readers import table_layout  # noqa: E402

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not condition else ""))


def get_request(path):
    return Request({"type": "http", "method": "GET", "path": path, "query_string": b"", "headers": [],
                    "server": ("test", 80), "scheme": "http", "root_path": ""})


def upload(label, pdf):
    spec = SpecVersion(uploaded_by_teacher_id=admin.id, label=label,
                       raw_text=extract_text(str(pdf), pdf.name, include_table_copies=False), source_file_path=str(pdf))
    db.add(spec)
    db.commit()
    r._run_parse_job(spec.id)
    db.expire_all()
    return db.get(SpecVersion, spec.id)


def criteria(spec):
    return sum(len(lo["criteria"]) for u in spec.structured_json["units"] for lo in u["learning_outcomes"])


Base.metadata.create_all(bind=engine)
db = SessionLocal()
admin = Teacher(name="Self Test", email="selftest@example.invalid", hashed_password="x", role="admin")
db.add(admin)
# an older learned TEXT pattern must keep loading and must not get in the way
db.add(DiscoveredSpecFormat(label="Old text pattern", unit_header_regex=r"^Chapter (\d+) (.+)$", criterion_regex=r"^(K\d+) (.+)$"))
db.commit()

# 1. new layout -> read with no AI, saved
first = upload("Commerce (AQA-style test)", FIX / "layout_aqa_like.pdf")
q = first.parse_quality
check("new layout read: parsed, 2 units, 30 statements, no AI", first.parse_status == "parsed"
      and len(first.structured_json["units"]) == 2 and criteria(first) == 30 and q["ai_calls_used"] == 0, q)
check("first spec in a new layout is honestly 'needs review'", q["verdict"] == "needs_review"
      and any(p.startswith("New layout") for p in q["problems"]), q["problems"])
check("guidance column kept against the right statements",
      sum(1 for u in first.structured_json["units"] for lo in u["learning_outcomes"] for c in lo["criteria"] if c.get("guidance")) == 16)
saved = [f for f in db.query(DiscoveredSpecFormat).all() if f.layout_json]
check("the layout was saved", len(saved) == 1 and json.loads(saved[0].layout_json)["header"] == ["Content", "Additional information"]
      and "learned" in (first.parse_progress or ""), first.parse_progress)

# 2. same layout again -> saved layout used, no discovery
original = table_layout.discover_layout
table_layout.discover_layout = lambda *a, **k: (_ for _ in ()).throw(AssertionError("discovery should not run"))
try:
    second = upload("Commerce (second copy)", FIX / "layout_aqa_like.pdf")
finally:
    table_layout.discover_layout = original
q2 = second.parse_quality
check("same layout again: read with the SAVED layout, 'looks good'", second.parse_status == "parsed"
      and q2["ruleset_used"].startswith("learned layout") and q2["verdict"] == "good", q2)
check("same content both times", second.structured_json["units"] == first.structured_json["units"])
db.expire_all()
check("the saved layout counts how often it was used", db.get(DiscoveredSpecFormat, saved[0].id).times_used == 2)
check("nothing new learned the second time", db.query(DiscoveredSpecFormat).filter(DiscoveredSpecFormat.layout_json.isnot(None)).count() == 1)

# 3. a different layout is learned as well
third = upload("Enterprise (Edexcel-style test)", FIX / "layout_edexcel_like.pdf")
check("second layout read: codes kept (1.1, 1.1.1 ...)", third.parse_status == "parsed" and criteria(third) == 39
      and third.structured_json["units"][0]["learning_outcomes"][0]["code"] == "1.1"
      and third.structured_json["units"][0]["learning_outcomes"][0]["criteria"][0]["code"] == "1.1.1")
check("two layouts learned now", db.query(DiscoveredSpecFormat).filter(DiscoveredSpecFormat.layout_json.isnot(None)).count() == 2)

# 4. library page + forget
html = r.spec_library(get_request("/spec-library"), db, admin).body.decode()
check("library page lists built-in readers and the 2 learned layouts", "Spec layouts CourseCraft can read" in html
      and "Learned from your specs (3)" in html and "Content | Additional information" in html)
r.forget_learned_format(saved[0].id, db, admin)
check("Forget removes a learned layout and logs it", db.get(DiscoveredSpecFormat, saved[0].id) is None
      and db.query(AuditLogEntry).filter(AuditLogEntry.detail.like("learned format%forgotten%")).count() == 1)

# 5. unreadable with AI off -> failed, nothing learned
before = db.query(DiscoveredSpecFormat).count()
fourth = upload("Retail (header not bold, AI off)", FIX / "layout_plain_header.pdf")
check("a spec nothing can read is 'failed' (not a fake success)", fourth.parse_status == "failed", fourth.parse_status)
check("nothing is learned from a failed read", db.query(DiscoveredSpecFormat).count() == before)

db.close()
os.remove(TEMP_DB)
print()
if all(RESULTS):
    print(f"ALL STAGE 5 CHECKS PASSED ({len(RESULTS)})")
else:
    print(f"{RESULTS.count(False)} OF {len(RESULTS)} CHECKS FAILED - send me this output")
    sys.exit(1)
