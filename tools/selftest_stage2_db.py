"""
Stage 2 self-test: the Media reader inside the real parse job, the review/edit screens,
the in-use lock, editable copies and the audit trail - using the REAL route functions and
a THROWAWAY database in /tmp. Your real database is not opened at all. AI is switched off.
Needs golden/pdfs/media_btec_cmp.pdf (the Media spec PDF).

Run (from ~/projects/coursecraft):
  docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/selftest_stage2_db.py
Expected last line: "ALL STAGE 2 CHECKS PASSED (20)"
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
MEDIA_PDF = ROOT / "golden" / "pdfs" / "media_btec_cmp.pdf"
if not MEDIA_PDF.exists():
    sys.exit("Put the Media spec PDF at golden/pdfs/media_btec_cmp.pdf first.")

TEMP_DB = "/tmp/coursecraft_stage2_selftest.db"
if os.path.exists(TEMP_DB):
    os.remove(TEMP_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{TEMP_DB}"

_stub = types.ModuleType("app.llm_gateway")
_stub.complete_json = lambda **k: (_ for _ in ()).throw(RuntimeError("AI disabled in self-test"))
_stub.get_effective_provider = lambda *a, **k: "none"
sys.modules["app.llm_gateway"] = _stub

from starlette.requests import Request  # noqa: E402
from app import database  # noqa: E402

assert str(database.engine.url).endswith("coursecraft_stage2_selftest.db"), "SAFETY STOP: not the throwaway database"

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import Teacher, Course, SpecVersion, AuditLogEntry  # noqa: E402
from app.docx_annotator import extract_text  # noqa: E402
from app.routers import specs as r  # noqa: E402

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not condition else ""))


def get_request(path, query=""):
    return Request({"type": "http", "method": "GET", "path": path, "query_string": query.encode(), "headers": [],
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


def unit_form(unit, **changes):
    """The form a browser would send for this unit with nothing changed, plus `changes`."""
    form = {"unit-name": unit["name"]}
    for i, lo in enumerate(unit["learning_outcomes"]):
        form[f"lo-{i}-code"], form[f"lo-{i}-title"] = lo["code"] or "", lo["title"] or ""
        for j, c in enumerate(lo["criteria"]):
            form[f"c-{i}-{j}-code"] = c["code"]
            if c.get("bands"):
                form[f"c-{i}-{j}-title"] = c["description"].split(" - ")[0]
                for band, text in c["bands"].items():
                    form[f"c-{i}-{j}-band-{band}"] = text
            else:
                form[f"c-{i}-{j}-desc"] = c["description"] or ""
    form.update(changes)
    return form


def save(spec_id, unit_index, form, teacher):
    req = post_request(f"/spec-library/{spec_id}/review/unit/{unit_index}", form)
    return asyncio.run(r.save_spec_unit(spec_id, unit_index, req, db, teacher))


Base.metadata.create_all(bind=engine)
db = SessionLocal()
admin = Teacher(name="Self Test", email="selftest@example.invalid", hashed_password="x", role="admin")
db.add(admin)
db.commit()

# 1) Upload-equivalent + real parse job
raw = extract_text(str(MEDIA_PDF), "media.pdf", include_table_copies=False)
media = SpecVersion(uploaded_by_teacher_id=admin.id, label="Creative Media Practice", raw_text=raw, source_file_path=str(MEDIA_PDF))
db.add(media)
db.commit()
r._run_parse_job(media.id)
db.expire_all()
media = db.get(SpecVersion, media.id)
sj = media.structured_json or {}
check("Media parses: status 'parsed', verdict good", media.parse_status == "parsed" and (media.parse_quality or {}).get("verdict") == "good",
      f"{media.parse_status} {(media.parse_quality or {}).get('problems')}")
check("4 assessment units, 20 objectives", len(sj.get("units", [])) == 4 and sum(len(u["learning_outcomes"]) for u in sj["units"]) == 20)
check("grade bands stored per criterion", "bands" in sj["units"][0]["learning_outcomes"][0]["criteria"][0])
check("qualification, grading and teaching content stored next to units",
      all(k in sj for k in ("qualification", "grading", "content_areas")))
check("no AI was used", (media.parse_quality or {}).get("ai_calls_used") == 0)

# 2) Pages render
html = r.view_spec(media.id, get_request(f"/spec-library/{media.id}"), "", db, admin).body.decode()
check("spec page shows qualification sizes and band grid", "540 Diploma" in html and "band-grid" in html)
html = r.review_spec(media.id, get_request(f"/spec-library/{media.id}/review"), db, admin).body.decode()
check("review overview renders with 0 of 4 checked", "0 of 4 unit(s) checked" in html)
html = r.review_spec_unit(media.id, 0, get_request(f"/spec-library/{media.id}/review/unit/0"), "", db, admin).body.decode()
check("unit edit form renders, not locked", 'name="c-0-0-band-Merit"' in html and "<fieldset disabled" not in html)

# 3) Unchanged save changes nothing
before = media.structured_json
save(media.id, 0, unit_form(before["units"][0]), admin)
db.expire_all()
media = db.get(SpecVersion, media.id)
check("unchanged save leaves the spec identical", media.structured_json == before)
check("unchanged save writes no audit entry", db.query(AuditLogEntry).filter(AuditLogEntry.action == "spec_edit").count() == 0)

# 4) Real edit + review tick
unit0 = media.structured_json["units"][0]
save(media.id, 0, unit_form(unit0, **{"c-0-0-band-Merit": "Edited merit wording", "reviewed": "on"}), admin)
db.expire_all()
media = db.get(SpecVersion, media.id)
crit = media.structured_json["units"][0]["learning_outcomes"][0]["criteria"][0]
check("band edit saved and description rebuilt", crit["bands"]["Merit"] == "Edited merit wording" and "Merit: Edited merit wording" in crit["description"])
check("other units untouched", media.structured_json["units"][1:] == before["units"][1:])
log = db.query(AuditLogEntry).filter(AuditLogEntry.action == "spec_edit").all()
check("edit recorded in audit log with who and what", len(log) == 1 and log[0].actor == admin.email and "Merit wording changed" in log[0].detail)
q = media.parse_quality
check("unit marked checked, spec flagged as edited", q.get("reviewed_units") == [unit0["name"]] and q.get("edited") is True)

# 5) Bad edit refused, nothing changed
snapshot = media.structured_json
resp = save(media.id, 1, unit_form(snapshot["units"][1], **{"unit-name": snapshot["units"][0]["name"]}), admin)
db.expire_all()
check("renaming a unit to an existing name is refused", "error=" in resp.headers["location"] and db.get(SpecVersion, media.id).structured_json == snapshot)

# 6) In-use lock + editable copy
db.add(Course(teacher_id=admin.id, name="Media Year 1", spec_version_id=media.id))
db.commit()
locked_before = db.get(SpecVersion, media.id).structured_json
resp = save(media.id, 0, unit_form(locked_before["units"][0], **{"unit-name": "Renamed while in use"}), admin)
db.expire_all()
check("editing a spec IN USE is refused", "error=" in resp.headers["location"] and db.get(SpecVersion, media.id).structured_json == locked_before)
html = r.review_spec_unit(media.id, 0, get_request("/x"), "", db, admin).body.decode()
check("in-use edit form is shown read-only", "<fieldset disabled" in html)
resp = r.make_editable_copy(media.id, db, admin)
copy_id = int(resp.headers["location"].split("/")[2])
copy = db.get(SpecVersion, copy_id)
check("editable copy is a new version with the same units", copy.reparsed_from_id == media.id and copy.structured_json == locked_before)
save(copy_id, 0, unit_form(copy.structured_json["units"][0], **{"unit-name": "Assessment unit A1: Skills Development (fixed)"}), admin)
db.expire_all()
check("the copy CAN be edited", db.get(SpecVersion, copy_id).structured_json["units"][0]["name"].endswith("(fixed)"))
check("the original in-use spec is still unchanged", db.get(SpecVersion, media.id).structured_json == locked_before)

db.close()
os.remove(TEMP_DB)
print()
if all(RESULTS):
    print(f"ALL STAGE 2 CHECKS PASSED ({len(RESULTS)})")
else:
    print(f"{RESULTS.count(False)} OF {len(RESULTS)} CHECKS FAILED - send me this output")
    sys.exit(1)
