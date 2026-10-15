"""
Stages 3 + 4 self-test: the City & Guilds reader inside the real parse job, and the
"add a unit from pages / pasted text" fix tools (preview, add, delete, in-use lock) - using
the REAL route functions and a THROWAWAY database in /tmp. Your real database is not opened.
AI is switched off (anything that would need AI simply finds nothing).
Needs golden/pdfs/cityguilds_6502_l4.pdf and golden/pdfs/alevel_business_eduqas.pdf.

Run (from ~/projects/coursecraft):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/selftest_stage3_db.py
Expected last line: "ALL STAGE 3+4 CHECKS PASSED (21)"
"""
import asyncio
import json
import os
import sys
import types
from pathlib import Path
from urllib.parse import urlencode, unquote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
CG_PDF = ROOT / "golden" / "pdfs" / "cityguilds_6502_l4.pdf"
AL_PDF = ROOT / "golden" / "pdfs" / "alevel_business_eduqas.pdf"
for f in (CG_PDF, AL_PDF):
    if not f.exists():
        sys.exit(f"Put the PDF at {f.relative_to(ROOT)} first.")

TEMP_DB = "/tmp/coursecraft_stage3_selftest.db"
if os.path.exists(TEMP_DB):
    os.remove(TEMP_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{TEMP_DB}"

_stub = types.ModuleType("app.llm_gateway")
_stub.complete_json = lambda **k: (_ for _ in ()).throw(RuntimeError("AI disabled in self-test"))
_stub.get_effective_provider = lambda *a, **k: "none"
sys.modules["app.llm_gateway"] = _stub

from starlette.requests import Request  # noqa: E402
from app import database  # noqa: E402

assert str(database.engine.url).endswith("coursecraft_stage3_selftest.db"), "SAFETY STOP: not the throwaway database"

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import Teacher, Course, SpecVersion, AuditLogEntry  # noqa: E402
from app.docx_annotator import extract_text  # noqa: E402
from app.routers import specs as r  # noqa: E402

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not condition else ""))


def get_request(path):
    return Request({"type": "http", "method": "GET", "path": path, "query_string": b"", "headers": [],
                    "server": ("test", 80), "scheme": "http", "root_path": ""})


def post_request(path, form: dict):
    body = urlencode(form).encode()
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}
    return Request({"type": "http", "method": "POST", "path": path, "query_string": b"", "server": ("test", 80),
                    "scheme": "http", "root_path": "",
                    "headers": [(b"content-type", b"application/x-www-form-urlencoded"),
                                (b"content-length", str(len(body)).encode())]}, receive)


def preview(spec_id, **form):
    defaults = {"first_page": "", "last_page": "", "pasted_text": "", "unit_name": ""}
    defaults.update(form)
    return r.add_unit_preview(spec_id, get_request("/p"), defaults["first_page"], defaults["last_page"],
                              defaults["pasted_text"], defaults["unit_name"], db, admin)


def confirm(spec_id, units, include=(0,), names=None):
    form = {"units_json": json.dumps(units), "source": "self-test"}
    for k in include:
        form[f"include-{k}"] = "on"
    for k, name in (names or {}).items():
        form[f"name-{k}"] = name
    return asyncio.run(r.add_unit_confirm(spec_id, post_request("/c", form), db, admin))


def preview_units(resp):
    return resp.context["result"]["units"]


def fresh(spec_id):
    db.expire_all()
    return db.get(SpecVersion, spec_id)


Base.metadata.create_all(bind=engine)
db = SessionLocal()
admin = Teacher(name="Self Test", email="selftest@example.invalid", hashed_password="x", role="admin")
db.add(admin)
db.commit()

# --- Stage 4: City & Guilds through the real parse job
cg = SpecVersion(uploaded_by_teacher_id=admin.id, label="C&G 6502",
                 raw_text=extract_text(str(CG_PDF), "cg.pdf", include_table_copies=False), source_file_path=str(CG_PDF))
db.add(cg)
db.commit()
r._run_parse_job(cg.id)
cg = fresh(cg.id)
units = cg.structured_json["units"]
check("City & Guilds parses: 'parsed', verdict good, no AI", cg.parse_status == "parsed"
      and cg.parse_quality["verdict"] == "good" and cg.parse_quality["ai_calls_used"] == 0, cg.parse_quality)
check("5 mandatory units, 19 outcomes, 54 criteria", len(units) == 5
      and sum(len(u["learning_outcomes"]) for u in units) == 19
      and sum(len(lo["criteria"]) for u in units for lo in u["learning_outcomes"]) == 54)
check("unit facts kept (level, credits, GLH, group, aim)", units[1].get("credits") == 3 and units[1].get("group") == "mandatory"
      and units[1].get("aim", "").startswith("The purpose"))
check("rules of combination stored", cg.structured_json["qualification"]["rules"][0].startswith("candidates must achieve a minimum of 36 credits"))
html = r.view_spec(cg.id, get_request(f"/spec-library/{cg.id}"), "", db, admin).body.decode()
check("spec page shows rules and the unit list", "Rules of combination" in html and "separate unit pack" in html)

# --- Stage 3: a spec nothing could read -> build it from pages
al = SpecVersion(uploaded_by_teacher_id=admin.id, label="A level Business",
                 raw_text=extract_text(str(AL_PDF), "al.pdf", include_table_copies=False), source_file_path=str(AL_PDF))
db.add(al)
db.commit()
r._run_parse_job(al.id)
al = fresh(al.id)
check("unreadable spec is 'failed' with a build button", al.parse_status == "failed"
      and "Build it from pages" in r.view_spec(al.id, get_request("/x"), "", db, admin).body.decode())
html = r.add_unit_page(al.id, get_request("/a"), "", db, admin).body.decode()
check("add-unit page offers pages AND paste (PDF is stored)", "Read these pages" in html and "Paste the unit" in html)
pasted = "1.1 Explain the meaning of enterprise\n1.2 Identify business opportunities\n1.3 Evaluate the impact of entrepreneurs"
resp = preview(al.id, pasted_text=pasted, unit_name="Component 1: Enterprise")
found = preview_units(resp)
check("pasted numbered list is read by rules (no AI)", len(found) == 1 and resp.context["result"]["method"].startswith("rules")
      and found[0]["name"] == "Component 1: Enterprise")
check("preview saves nothing", fresh(al.id).parse_status == "failed")
confirm(al.id, found)
al = fresh(al.id)
check("after adding: status 'parsed', 1 unit, verdict needs review", al.parse_status == "parsed"
      and len(al.structured_json["units"]) == 1 and al.parse_quality["verdict"] == "needs_review")
log = db.query(AuditLogEntry).filter(AuditLogEntry.detail.like(f"spec {al.id}:%")).all()
check("the add is in the audit log", len(log) == 1 and "added unit(s)" in log[0].detail)
resp = confirm(al.id, found)
check("adding a unit with an existing name is refused", "error=" in resp.headers["location"] and len(fresh(al.id).structured_json["units"]) == 1)
resp = confirm(al.id, found, include=())
check("adding with nothing ticked is refused", "error=" in resp.headers["location"])
resp = asyncio.run(r.add_unit_confirm(al.id, post_request("/c", {"units_json": "not json", "include-0": "on"}), db, admin))
check("broken preview data is refused safely", "error=" in resp.headers["location"])

# --- page range on the C&G spec, choosing one of two units found
resp = preview(cg.id, first_page="30", last_page="33")
found = preview_units(resp)
check("pages 30-33 read by rules: Unit 401 (+ part of 402)", [u["code"] for u in found][:1] == ["401"] and len(found) == 2)
resp = preview(cg.id, first_page="1", last_page="40")
check("too many pages at once is refused with advice", "error=" in resp.headers["location"] and "at most" in unquote(resp.headers["location"]))

# --- delete unit
cg_units_before = len(fresh(cg.id).structured_json["units"])
r.delete_spec_unit(cg.id, 4, db, admin)
check("delete unit removes it and logs it", len(fresh(cg.id).structured_json["units"]) == cg_units_before - 1
      and db.query(AuditLogEntry).filter(AuditLogEntry.detail.like(f"spec {cg.id}:%deleted%")).count() == 1)
r.delete_spec_unit(al.id, 0, db, admin)
check("deleting the last unit turns the spec back to 'failed'", fresh(al.id).parse_status == "failed")

# --- in-use lock for all fix tools
db.add(Course(teacher_id=admin.id, name="Teacher training", spec_version_id=cg.id))
db.commit()
before = fresh(cg.id).structured_json
resp = preview(cg.id, pasted_text=pasted)
check("in use: preview refused", "error=" in resp.headers["location"])
resp = confirm(cg.id, [{"name": "X", "learning_outcomes": []}])
check("in use: add refused", "error=" in resp.headers["location"])
r.delete_spec_unit(cg.id, 0, db, admin)
check("in use: delete refused, spec unchanged", fresh(cg.id).structured_json == before)

db.close()
os.remove(TEMP_DB)
print()
if all(RESULTS):
    print(f"ALL STAGE 3+4 CHECKS PASSED ({len(RESULTS)})")
else:
    print(f"{RESULTS.count(False)} OF {len(RESULTS)} CHECKS FAILED - send me this output")
    sys.exit(1)
