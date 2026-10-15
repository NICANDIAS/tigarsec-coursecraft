import os
import json
import uuid
import traceback
import datetime
import requests
from urllib.parse import quote
from fastapi import APIRouter, Request, Depends, Form, UploadFile, File, BackgroundTasks
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db, SessionLocal
from ..models import Teacher, SpecVersion, Course, Submission, DiscoveredSpecFormat, ExamExemplar, AuditLogEntry
from ..course_access import get_course_for_teacher
from ..auth import get_current_teacher, require_spec_uploader
from ..docx_annotator import extract_text, extract_text_from_pdf_pages, pdf_page_count
from ..llm_gateway import get_effective_provider
from ..spec_parser import parse_spec_hybrid, read_piece
from ..spec_versions import spec_usage, usage_sentence, version_family, compare_units
from ..spec_editing import edit_unit, updated_quality, clean_units_payload, structure_problems
from ..web_fetch import fetch_spec_from_url, UnsafeURLError

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

UPLOAD_DIR = "storage/uploads"


@router.get("/spec-library", response_class=HTMLResponse)
def spec_library(
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    specs = db.query(SpecVersion).order_by(SpecVersion.id.desc()).all()
    can_upload = teacher.role in ("admin", "curriculum_manager")
    learned = db.query(DiscoveredSpecFormat).order_by(DiscoveredSpecFormat.id.desc()).all()
    return templates.TemplateResponse(
        "spec_library.html",
        {"request": request, "teacher": teacher, "specs": specs, "can_upload": can_upload,
         "built_in_readers": BUILT_IN_READERS, "learned_formats": learned},
    )


# What CourseCraft can read without being taught (shown on the library page).
BUILT_IN_READERS = [
    ("Pearson BTEC", "units, learning aims, Pass / Merit / Distinction criteria"),
    ("T Levels", "core component, occupational specialisms, content areas"),
    ("Gateway Qualifications", "units, unit aims, numbered criteria"),
    ("Pearson BTEC 'Practice'", "assessment units with U / Pass / Merit / Distinction grade grids"),
    ("City & Guilds, NCFE-style units", "learning outcomes, assessment criteria, credits, rules of combination"),
    ("WJEC / Eduqas A level and GCSE", "Content / Amplification tables, exams, assessment objectives"),
    ("Any table layout", "worked out from a new spec once, then remembered (listed below)"),
]


@router.post("/spec-library/learned-formats/{format_id}/forget")
def forget_learned_format(
    format_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_spec_uploader),
):
    """Removes a learned layout (e.g. one that read a later spec badly). Specs already read
    with it keep their content; only future uploads stop using it."""
    learned = db.get(DiscoveredSpecFormat, format_id)
    if learned:
        db.add(AuditLogEntry(actor=teacher.email, action="spec_edit",
                             detail=f"learned format {format_id}: forgotten ('{learned.label}', used {learned.times_used or 0} time(s))"))
        db.delete(learned)
        db.commit()
    return RedirectResponse(url="/spec-library", status_code=303)


@router.get("/spec-library/new", response_class=HTMLResponse)
def new_spec_page(
    request: Request,
    teacher: Teacher = Depends(require_spec_uploader),
):
    return templates.TemplateResponse("spec_upload.html", {"request": request, "teacher": teacher, "error": None})


@router.post("/spec-library/upload")
async def upload_spec(
    request: Request,
    background_tasks: BackgroundTasks,
    label: str = Form(...),
    pasted_text: str = Form(""),
    source_url: str = Form(""),
    spec_file: UploadFile = File(None),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_spec_uploader),
):
    raw_text = pasted_text.strip()
    label_from_source = None
    source_file_path = None

    if spec_file is not None and spec_file.filename:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        temp_name = f"{uuid.uuid4().hex}_{spec_file.filename}"
        temp_path = os.path.join(UPLOAD_DIR, temp_name)
        contents = await spec_file.read()
        with open(temp_path, "wb") as f:
            f.write(contents)
        # No duplicate table copies for specs - they made the parser read every criterion twice.
        raw_text = extract_text(temp_path, spec_file.filename, include_table_copies=False)
        source_file_path = temp_path

    elif source_url.strip():
        try:
            raw_text, label_from_source = fetch_spec_from_url(source_url.strip(), UPLOAD_DIR)
        except UnsafeURLError as e:
            return templates.TemplateResponse(
                "spec_upload.html", {"request": request, "teacher": teacher, "error": str(e)}, status_code=400
            )
        except requests.exceptions.RequestException as e:
            return templates.TemplateResponse(
                "spec_upload.html",
                {"request": request, "teacher": teacher, "error": f"Couldn't fetch that URL: {e}"},
                status_code=400,
            )

    if not raw_text.strip():
        return RedirectResponse(url="/spec-library/new", status_code=303)

    spec = SpecVersion(
        uploaded_by_teacher_id=teacher.id,
        label=label or label_from_source or "Untitled spec",
        raw_text=raw_text,
        source_file_path=source_file_path,
    )
    db.add(spec)
    db.commit()
    db.refresh(spec)

    # Kick off parsing in the background immediately so the upload doesn't block on it -
    # the library page shows "parsing..." and the person can navigate away and come back.
    spec.parse_status = "parsing"
    spec.parse_progress = "Queued..."
    spec.parse_progress_updated_at = datetime.datetime.utcnow()
    db.commit()
    background_tasks.add_task(_run_parse_job, spec.id)

    return RedirectResponse(url=f"/spec-library/{spec.id}", status_code=303)


def _run_parse_job(spec_id: int):
    """
    Runs in a background thread after the response has already been sent. Uses its OWN
    database session (not the request's) since the request's session may already be
    closed/reused by the time this runs on a busy server - safer to be self-contained.
    """
    db = SessionLocal()
    try:
        spec = db.get(SpecVersion, spec_id)
        if not spec:
            return

        spec.cancel_requested = False
        db.commit()

        def progress(msg: str):
            spec.parse_progress = msg
            spec.parse_progress_updated_at = datetime.datetime.utcnow()
            db.commit()

        def is_cancelled():
            db.refresh(spec)
            return spec.cancel_requested

        try:
            known_patterns = []
            for p in db.query(DiscoveredSpecFormat).all():
                entry = {
                    "label": p.label,
                    "unit_header_regex": p.unit_header_regex,
                    "sub_heading_regex": p.sub_heading_regex,
                    "criterion_regex": p.criterion_regex,
                }
                if p.layout_json:  # Stage 5: a learned TABLE layout
                    try:
                        entry["layout"] = {**json.loads(p.layout_json), "format_id": p.id}
                    except (ValueError, TypeError):
                        continue
                known_patterns.append(entry)

            # The original file (kept since Stage 1) lets readers use the page layout, e.g.
            # the Media grade grid. Specs uploaded before that have no file - readers cope.
            pdf_path = spec.source_file_path if spec.source_file_path and os.path.exists(spec.source_file_path) else None
            result = parse_spec_hybrid(
                spec.raw_text, progress_callback=progress, known_patterns=known_patterns,
                cancel_check=is_cancelled, pdf_path=pdf_path,
            )
            # "units" is what every tool reads; a style reader may add extras next to it
            # (qualification sizes, grading scheme, teaching content).
            spec.structured_json = {"units": result.get("units", []), **(result.get("extras") or {})}
            unit_count = len(result.get("units", []))
            rule_count = result.get("rule_based_unit_count", 0)
            ai_sections = result.get("ai_fallback_section_count", 0)
            ai_calls_used = result.get("ai_calls_used", 0)
            ai_call_budget = result.get("ai_call_budget", 0)
            failed_labels = result.get("failed_sections")

            # Only meaningful if AI was actually used - this is a hybrid rule-based + AI
            # fallback pipeline, and plenty of specs parse entirely via rules with zero AI
            # calls, where recording a "provider" would be misleading (none was used at all).
            if ai_calls_used > 0:
                spec.generated_by_provider = get_effective_provider(for_spec_parsing=True)

            quality = result.get("quality") or {}
            spec.parse_quality = quality

            if result.get("was_cancelled"):
                spec.parse_status = "cancelled"
                spec.parse_progress = (
                    f"Cancelled — {unit_count} unit(s) found before stopping "
                    f"({ai_calls_used} AI call(s) used). You can re-parse to try again."
                )
            else:
                ruleset_used = result.get("ruleset_used", "unknown")
                summary = f"Done — {unit_count} unit(s) found via {ruleset_used} ({rule_count} directly"
                if ai_sections:
                    summary += f", {ai_sections} section(s) needed AI review ({ai_calls_used}/{ai_call_budget} AI calls used)"
                summary += ")."
                if result.get("budget_stopped_early"):
                    summary += (
                        f" Stopped after the {ai_call_budget}-call safety limit — some sections may be "
                        f"incomplete. If this document needs more AI than that regularly, consider setting "
                        f"SPEC_PARSING_LLM_PROVIDER=anthropic in .env for faster, more reliable parsing, "
                        f"or raise MAX_AI_CALLS_PER_PARSE (accepting a longer wait)."
                    )
                if failed_labels:
                    summary += f" Section(s) that failed AI review and were skipped: {', '.join(failed_labels)}."

                discovered = result.get("newly_discovered_pattern")
                if discovered and discovered.get("kind") == "table":
                    layout = {k: v for k, v in discovered.items() if k != "format_id"}
                    db.add(DiscoveredSpecFormat(
                        label=layout.get("label", "Table layout")[:255],
                        unit_header_regex="", sub_heading_regex=None, criterion_regex="",
                        description=f"Table layout learned from '{spec.label}'"[:1000],
                        layout_json=json.dumps(layout), times_used=1,
                    ))
                    summary += " (New table layout learned and saved - the next spec in this layout needs no AI.)"
                elif discovered:
                    db.add(DiscoveredSpecFormat(
                        label=discovered.get("label", "Unnamed format")[:255],
                        unit_header_regex=discovered["unit_header_regex"],
                        sub_heading_regex=discovered.get("sub_heading_regex"),
                        criterion_regex=discovered["criterion_regex"],
                        description=discovered.get("description", ""),
                    ))
                    summary += " (New format learned and saved for future specs.)"
                if result.get("learned_layout_id"):
                    used = db.get(DiscoveredSpecFormat, result["learned_layout_id"])
                    if used:
                        used.times_used = (used.times_used or 0) + 1

                if unit_count == 0:
                    # Honesty fix: this used to show a green "Parsed" with nothing usable in it.
                    # "failed" is an existing status every screen already understands, and it
                    # keeps an empty spec out of the course / IV / timetabling dropdowns.
                    spec.parse_status = "failed"
                    spec.parse_progress = summary
                    problems = " ".join(quality.get("problems", []))
                    spec.parse_error = (
                        "CourseCraft could not find any units in this document, so it has not been marked as parsed. "
                        + (problems + " " if problems else "")
                        + "This spec's layout is not supported yet - nothing else in CourseCraft was changed."
                    )
                    db.commit()
                    return
                spec.parse_status = "parsed"
                spec.parse_progress = summary
            spec.parse_error = None
        except Exception:
            # Full traceback, not just str(e) - if this ever fails again, the error box
            # on the spec page will show exactly where and why, instead of a vague message.
            spec.parse_status = "failed"
            spec.parse_error = traceback.format_exc()
            spec.parse_progress = None
        db.commit()
    finally:
        db.close()


@router.post("/spec-library/{spec_id}/cancel-parse")
def cancel_parse(
    spec_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """Lets anyone stop a slow/stuck parse and reclaim control - never trapped waiting again."""
    spec = db.get(SpecVersion, spec_id)
    if spec and spec.parse_status == "parsing":
        spec.cancel_requested = True
        db.commit()
    return RedirectResponse(url=f"/spec-library/{spec_id}", status_code=303)


@router.post("/spec-library/{spec_id}/reparse")
def reparse_spec(
    spec_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_spec_uploader),
):
    """
    A spec that is IN USE (anything links to it - see spec_versions.LINKED_TABLES) is never
    overwritten: the re-parse becomes a NEW version and the original stays exactly as it was.
    A spec nothing uses yet (e.g. a fresh upload that parsed badly) is re-parsed in place,
    as before, so the library doesn't fill up with copies.
    """
    spec = db.get(SpecVersion, spec_id)
    if not spec or spec.parse_status == "parsing":
        return RedirectResponse(url=f"/spec-library/{spec_id}", status_code=303)

    target = spec
    if spec_usage(db, spec.id)["in_use"]:
        version_count = db.query(SpecVersion).filter(SpecVersion.reparsed_from_id == spec.id).count()
        target = SpecVersion(
            uploaded_by_teacher_id=teacher.id,
            label=f"{spec.label} (re-parse {version_count + 1}, {datetime.date.today():%d %b %Y})"[:255],
            raw_text=spec.raw_text,
            source_file_path=spec.source_file_path,
            reparsed_from_id=spec.id,
        )
        db.add(target)
        db.commit()
        db.refresh(target)

    target.parse_status = "parsing"
    target.parse_progress = "Queued..."
    target.parse_progress_updated_at = datetime.datetime.utcnow()
    target.parse_error = None
    target.parse_quality = None
    target.cancel_requested = False
    db.commit()
    background_tasks.add_task(_run_parse_job, target.id)
    return RedirectResponse(url=f"/spec-library/{target.id}", status_code=303)


@router.get("/spec-library/{spec_id}", response_class=HTMLResponse)
def view_spec(
    spec_id: int,
    request: Request,
    delete_error: str = "",
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    spec = db.get(SpecVersion, spec_id)
    can_upload = teacher.role in ("admin", "curriculum_manager")
    exemplars = db.query(ExamExemplar).filter(ExamExemplar.spec_version_id == spec_id).order_by(ExamExemplar.created_at.desc()).all() if spec else []
    usage = spec_usage(db, spec_id) if spec else {"counts": {}, "course_names": [], "in_use": False}
    family = version_family(db, spec) if spec else {"parent": None, "children": []}
    return templates.TemplateResponse(
        "spec_detail.html",
        {"request": request, "teacher": teacher, "spec": spec, "can_upload": can_upload,
         "delete_error": delete_error or None, "exemplars": exemplars,
         "usage": usage, "usage_text": usage_sentence(usage), "family": family},
    )


@router.get("/spec-library/{spec_id}/compare", response_class=HTMLResponse)
def compare_spec_versions(
    spec_id: int,
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """Read-only "what changed" page: this re-parsed version vs the version it came from."""
    spec = db.get(SpecVersion, spec_id)
    parent = db.get(SpecVersion, spec.reparsed_from_id) if spec and spec.reparsed_from_id else None
    if not spec or not parent:
        return RedirectResponse(url=f"/spec-library/{spec_id}", status_code=303)
    report = compare_units(
        (parent.structured_json or {}).get("units", []),
        (spec.structured_json or {}).get("units", []),
    )
    return templates.TemplateResponse(
        "spec_compare.html",
        {"request": request, "teacher": teacher, "spec": spec, "parent": parent, "report": report,
         "parent_usage_text": usage_sentence(spec_usage(db, parent.id))},
    )


@router.post("/spec-library/{spec_id}/delete")
def delete_spec(
    spec_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_spec_uploader),
):
    """
    Deletes a spec from the library - mainly for cleaning up ones that failed to parse or
    were uploaded by mistake. Blocked (not silently ignored) if the spec is actually in use,
    since deleting one still attached to a course or referenced by marked submissions would
    leave dangling references and break the "never lose the spec a submission was actually
    marked against" principle from the architecture doc.
    """
    spec = db.get(SpecVersion, spec_id)
    if not spec:
        return RedirectResponse(url="/spec-library", status_code=303)

    courses_using = db.query(Course).filter(Course.spec_version_id == spec_id).all()
    submissions_using_count = db.query(Submission).filter(Submission.spec_version_id == spec_id).count()

    other_usage = {
        label: n for label, n in spec_usage(db, spec_id)["counts"].items()
        if label not in ("course(s)", "marked submission(s)")
    }
    newer_versions = db.query(SpecVersion).filter(SpecVersion.reparsed_from_id == spec_id).count()

    if courses_using or submissions_using_count or other_usage or newer_versions:
        parts = []
        if courses_using:
            names = ", ".join(c.name for c in courses_using)
            parts.append(f"attached to course(s): {names} - detach it there first (course page → attach a different spec)")
        if submissions_using_count:
            parts.append(
                f"{submissions_using_count} submission(s) were marked against it - that history is kept "
                f"permanently by design, so this spec can't be deleted while those exist"
            )
        if other_usage:
            parts.append(
                "linked to " + ", ".join(f"{n} {label}" for label, n in other_usage.items())
                + " - deleting it would leave those records pointing at nothing"
            )
        if newer_versions:
            parts.append(f"the original of {newer_versions} newer re-parsed version(s) - delete those first")
        message = "Can't delete - this spec is " + "; and ".join(parts) + "."
        return RedirectResponse(url=f"/spec-library/{spec_id}?delete_error={quote(message)}", status_code=303)

    db.delete(spec)
    db.commit()
    return RedirectResponse(url="/spec-library", status_code=303)


@router.get("/spec-library/{spec_id}/status")
def spec_status(
    spec_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """Polled by JS on the spec detail page so the UI reflects real progress instead of hanging silently."""
    spec = db.get(SpecVersion, spec_id)
    if not spec:
        return JSONResponse({"error": "not found"}, status_code=404)
    unit_count = len(spec.structured_json.get("units", [])) if spec.structured_json else 0
    return {
        "status": spec.parse_status,
        "progress": spec.parse_progress,
        "unit_count": unit_count,
        "error": spec.parse_error,
        "updated_at": spec.parse_progress_updated_at.isoformat() if spec.parse_progress_updated_at else None,
    }


@router.post("/courses/{course_id}/attach-spec")
def attach_spec(
    course_id: int,
    spec_version_id: int = Form(...),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    course = get_course_for_teacher(db, course_id, teacher)
    if course:
        course.spec_version_id = spec_version_id
        db.commit()
    return RedirectResponse(url=f"/courses/{course_id}", status_code=303)


@router.post("/courses/{course_id}/detach-spec")
def detach_spec(
    course_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """Unlinks the spec from this course only - the spec itself stays in the shared
    library untouched. Full deletion of a spec from the library is a separate action
    on the spec's own page."""
    course = get_course_for_teacher(db, course_id, teacher)
    if course:
        course.spec_version_id = None
        db.commit()
    return RedirectResponse(url=f"/courses/{course_id}", status_code=303)


# ----------------------------------------------------------------------------- review / edit (Stage 2)
def _spec_edit_log(db: Session, spec_id: int, limit: int = 20):
    return (
        db.query(AuditLogEntry)
        .filter(AuditLogEntry.action == "spec_edit", AuditLogEntry.detail.like(f"spec {spec_id}:%"))
        .order_by(AuditLogEntry.id.desc()).limit(limit).all()
    )


@router.get("/spec-library/{spec_id}/review", response_class=HTMLResponse)
def review_spec(
    spec_id: int,
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_spec_uploader),
):
    """Overview for checking a parsed spec unit by unit."""
    spec = db.get(SpecVersion, spec_id)
    if not spec or not spec.structured_json:
        return RedirectResponse(url=f"/spec-library/{spec_id}", status_code=303)
    usage = spec_usage(db, spec_id)
    units = (spec.structured_json or {}).get("units", [])
    unit_problems = {i: structure_problems([u]) for i, u in enumerate(units)}
    return templates.TemplateResponse(
        "spec_review.html",
        {"request": request, "teacher": teacher, "spec": spec, "usage": usage, "usage_text": usage_sentence(usage),
         "reviewed": (spec.parse_quality or {}).get("reviewed_units", []), "log": _spec_edit_log(db, spec_id),
         "unit_problems": unit_problems, "has_pdf": bool(_spec_pdf(spec))},
    )


@router.get("/spec-library/{spec_id}/review/unit/{unit_index}", response_class=HTMLResponse)
def review_spec_unit(
    spec_id: int,
    unit_index: int,
    request: Request,
    error: str = "",
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_spec_uploader),
):
    spec = db.get(SpecVersion, spec_id)
    units = (spec.structured_json or {}).get("units", []) if spec else []
    if not spec or not 0 <= unit_index < len(units):
        return RedirectResponse(url=f"/spec-library/{spec_id}/review", status_code=303)
    usage = spec_usage(db, spec_id)
    return templates.TemplateResponse(
        "spec_review_unit.html",
        {"request": request, "teacher": teacher, "spec": spec, "unit": units[unit_index], "unit_index": unit_index,
         "unit_count": len(units), "locked": usage["in_use"], "usage_text": usage_sentence(usage),
         "is_reviewed": units[unit_index].get("name") in (spec.parse_quality or {}).get("reviewed_units", []),
         "error": error or None},
    )


@router.post("/spec-library/{spec_id}/review/unit/{unit_index}")
async def save_spec_unit(
    spec_id: int,
    unit_index: int,
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_spec_uploader),
):
    """Saves one unit's edits. Refused for a spec that is in use (see spec_editing.py)."""
    spec = db.get(SpecVersion, spec_id)
    units = list((spec.structured_json or {}).get("units", [])) if spec else []
    back = f"/spec-library/{spec_id}/review/unit/{unit_index}"
    if not spec or not 0 <= unit_index < len(units) or spec.parse_status == "parsing":
        return RedirectResponse(url=f"/spec-library/{spec_id}/review", status_code=303)
    if spec_usage(db, spec_id)["in_use"]:
        return RedirectResponse(url=f"{back}?error={quote('This spec is in use, so it cannot be edited. Make an editable copy instead.')}", status_code=303)

    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    old_name = units[unit_index].get("name")
    others = {u.get("name") for i, u in enumerate(units) if i != unit_index}
    new_unit, changes, errors = edit_unit(units[unit_index], form, others)
    if errors:
        return RedirectResponse(url=f"{back}?error={quote(' '.join(errors))}", status_code=303)

    units[unit_index] = new_unit
    reviewed = [n for n in (spec.parse_quality or {}).get("reviewed_units", []) if n != old_name]
    if form.get("reviewed"):
        reviewed.append(new_unit["name"])
    if form.get("reviewed") and old_name not in (spec.parse_quality or {}).get("reviewed_units", []):
        changes.append("marked as checked")
    elif not form.get("reviewed") and old_name in (spec.parse_quality or {}).get("reviewed_units", []):
        changes.append("check mark removed")

    # JSON columns are only saved when a NEW object is assigned (in-place changes are missed).
    spec.structured_json = {**spec.structured_json, "units": units}
    quality = updated_quality(spec.parse_quality, units, reviewed, teacher.name or teacher.email)
    if any(c not in ("marked as checked", "check mark removed") for c in changes):
        quality["edited"] = True
    spec.parse_quality = quality
    if changes:
        db.add(AuditLogEntry(
            actor=teacher.email, action="spec_edit",
            detail=f"spec {spec_id}: unit '{new_unit['name']}': " + "; ".join(changes),
        ))
    db.commit()
    if form.get("next") == "1" and unit_index + 1 < len(units):
        return RedirectResponse(url=f"/spec-library/{spec_id}/review/unit/{unit_index + 1}", status_code=303)
    return RedirectResponse(url=f"/spec-library/{spec_id}/review?saved={quote(new_unit['name'])}", status_code=303)


@router.post("/spec-library/{spec_id}/editable-copy")
def make_editable_copy(
    spec_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_spec_uploader),
):
    """For a spec IN USE: copies it (same units, nothing re-read) as a new version that can be
    edited. The original stays exactly as it is, and so does everything linked to it."""
    spec = db.get(SpecVersion, spec_id)
    if not spec or not spec.structured_json:
        return RedirectResponse(url=f"/spec-library/{spec_id}", status_code=303)
    n = db.query(SpecVersion).filter(SpecVersion.reparsed_from_id == spec.id).count() + 1
    quality = dict(spec.parse_quality or {})
    quality["notes"] = list(quality.get("notes", [])) + [f"Editable copy of spec #{spec.id} made by {teacher.name or teacher.email}."]
    copy = SpecVersion(
        uploaded_by_teacher_id=teacher.id,
        label=f"{spec.label} (edited copy {n}, {datetime.date.today():%d %b %Y})"[:255],
        raw_text=spec.raw_text,
        source_file_path=spec.source_file_path,
        reparsed_from_id=spec.id,
        structured_json=spec.structured_json,
        parse_status="parsed",
        parse_progress=f"Editable copy of spec #{spec.id} - nothing was re-read.",
        parse_quality=quality,
        generated_by_provider=spec.generated_by_provider,
    )
    db.add(copy)
    db.commit()
    db.add(AuditLogEntry(actor=teacher.email, action="spec_edit",
                         detail=f"spec {copy.id}: created as an editable copy of spec {spec.id}"))
    db.commit()
    return RedirectResponse(url=f"/spec-library/{copy.id}/review", status_code=303)


# ----------------------------------------------------------------------------- add a unit (Stage 3)
MAX_PIECE_PAGES = 15        # small pieces are where reading (and AI) is reliable
MAX_PASTE_CHARS = 40000


def _spec_pdf(spec):
    path = spec.source_file_path if spec else None
    return path if path and path.lower().endswith(".pdf") and os.path.exists(path) else None


def _save_units(db, spec, units, teacher, reviewed=None):
    """Stores a new units list and refreshes the honesty report (JSON columns need a new object)."""
    spec.structured_json = {**(spec.structured_json or {}), "units": units}
    if reviewed is None:
        reviewed = (spec.parse_quality or {}).get("reviewed_units", [])
    quality = updated_quality(spec.parse_quality or {"problems": [], "notes": []}, units, reviewed, teacher.name or teacher.email)
    quality["edited"] = True
    spec.parse_quality = quality
    if units and spec.parse_status != "parsed":
        spec.parse_status = "parsed"  # e.g. a spec that read as "Not usable" now has units a person added
        spec.parse_error = None
    elif not units:
        spec.parse_status = "failed"
        spec.parse_error = "This spec has no units. Use 'Add a unit' to read them from the PDF's pages or from pasted text."


@router.get("/spec-library/{spec_id}/add-unit", response_class=HTMLResponse)
def add_unit_page(
    spec_id: int,
    request: Request,
    error: str = "",
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_spec_uploader),
):
    spec = db.get(SpecVersion, spec_id)
    if not spec:
        return RedirectResponse(url="/spec-library", status_code=303)
    usage = spec_usage(db, spec_id)
    pdf = _spec_pdf(spec)
    return templates.TemplateResponse(
        "spec_add_unit.html",
        {"request": request, "teacher": teacher, "spec": spec, "locked": usage["in_use"], "usage_text": usage_sentence(usage),
         "has_pdf": bool(pdf), "page_count": pdf_page_count(pdf) if pdf else 0, "max_pages": MAX_PIECE_PAGES,
         "error": error or None},
    )


@router.post("/spec-library/{spec_id}/add-unit/preview", response_class=HTMLResponse)
def add_unit_preview(
    spec_id: int,
    request: Request,
    first_page: str = Form(""),
    last_page: str = Form(""),
    pasted_text: str = Form(""),
    unit_name: str = Form(""),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_spec_uploader),
):
    """Reads a few pages (or pasted text) and shows what was found. Nothing is saved here."""
    spec = db.get(SpecVersion, spec_id)
    back = f"/spec-library/{spec_id}/add-unit"
    if not spec:
        return RedirectResponse(url="/spec-library", status_code=303)
    if spec_usage(db, spec_id)["in_use"]:
        return RedirectResponse(url=f"{back}?error={quote('This spec is in use - make an editable copy first.')}", status_code=303)

    pdf = _spec_pdf(spec)
    if first_page.strip():
        if not pdf:
            return RedirectResponse(url=f"{back}?error={quote('No PDF is stored for this spec - paste the text instead.')}", status_code=303)
        try:
            first = int(first_page)
            last = int(last_page or first_page)
        except ValueError:
            return RedirectResponse(url=f"{back}?error={quote('Page numbers must be whole numbers.')}", status_code=303)
        total = pdf_page_count(pdf)
        if first < 1 or last < first or last > total:
            return RedirectResponse(url=f"{back}?error={quote(f'Choose pages between 1 and {total}, first page before last.')}", status_code=303)
        if last - first + 1 > MAX_PIECE_PAGES:
            return RedirectResponse(url=f"{back}?error={quote(f'Please choose at most {MAX_PIECE_PAGES} pages at a time (one unit is usually 2-6 pages).')}", status_code=303)
        text = extract_text_from_pdf_pages(pdf, first, last)
        source = f"pages {first}-{last}" if last != first else f"page {first}"
    elif pasted_text.strip():
        if len(pasted_text) > MAX_PASTE_CHARS:
            return RedirectResponse(url=f"{back}?error={quote('That is a lot of text - paste one unit at a time.')}", status_code=303)
        text, source = pasted_text, "pasted text"
    else:
        return RedirectResponse(url=f"{back}?error={quote('Enter page numbers or paste some text first.')}", status_code=303)

    result = read_piece(text, unit_name.strip()[:500])
    existing = {u.get("name") for u in (spec.structured_json or {}).get("units", [])}
    return templates.TemplateResponse(
        "spec_add_unit_preview.html",
        {"request": request, "teacher": teacher, "spec": spec, "result": result, "source": source,
         "units_json": json.dumps(result["units"]), "existing_names": existing},
    )


@router.post("/spec-library/{spec_id}/add-unit/confirm")
async def add_unit_confirm(
    spec_id: int,
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_spec_uploader),
):
    spec = db.get(SpecVersion, spec_id)
    back = f"/spec-library/{spec_id}/add-unit"
    if not spec:
        return RedirectResponse(url="/spec-library", status_code=303)
    if spec_usage(db, spec_id)["in_use"]:
        return RedirectResponse(url=f"{back}?error={quote('This spec is in use - make an editable copy first.')}", status_code=303)
    form = await request.form()
    try:
        data = json.loads(form.get("units_json") or "[]")
    except (TypeError, ValueError):
        data = None
    units, errors = clean_units_payload(data)
    chosen = []
    for k, unit in enumerate(units):
        if not form.get(f"include-{k}"):
            continue
        new_name = str(form.get(f"name-{k}") or unit["name"]).strip()[:500]
        unit["name"] = new_name
        chosen.append(unit)
    existing = list((spec.structured_json or {}).get("units", []))
    names = [u.get("name", "").lower() for u in existing] + [u["name"].lower() for u in chosen]
    if not chosen:
        errors.append("Tick at least one unit to add.")
    if any(not u["name"] for u in chosen):
        errors.append("Every unit needs a name.")
    if len(set(names)) != len(names):
        errors.append("Two units would have the same name - give each a different name.")
    if errors:
        return RedirectResponse(url=f"{back}?error={quote(' '.join(sorted(set(errors))))}", status_code=303)

    _save_units(db, spec, existing + chosen, teacher)
    db.add(AuditLogEntry(actor=teacher.email, action="spec_edit",
                         detail=f"spec {spec_id}: added unit(s) {', '.join(repr(u['name']) for u in chosen)} "
                                f"({sum(len(u['learning_outcomes']) for u in chosen)} learning outcome(s)) from {str(form.get('source') or 'a preview')[:60]}"))
    db.commit()
    return RedirectResponse(url=f"/spec-library/{spec_id}/review?saved={quote(', '.join(u['name'] for u in chosen))}", status_code=303)


@router.post("/spec-library/{spec_id}/review/unit/{unit_index}/delete")
def delete_spec_unit(
    spec_id: int,
    unit_index: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_spec_uploader),
):
    spec = db.get(SpecVersion, spec_id)
    units = list((spec.structured_json or {}).get("units", [])) if spec else []
    if not spec or not 0 <= unit_index < len(units):
        return RedirectResponse(url=f"/spec-library/{spec_id}/review", status_code=303)
    if spec_usage(db, spec_id)["in_use"]:
        return RedirectResponse(url=f"/spec-library/{spec_id}/review/unit/{unit_index}?error={quote('This spec is in use, so units cannot be deleted. Make an editable copy instead.')}", status_code=303)
    removed = units.pop(unit_index)
    reviewed = [n for n in (spec.parse_quality or {}).get("reviewed_units", []) if n != removed.get("name")]
    _save_units(db, spec, units, teacher, reviewed)
    db.add(AuditLogEntry(actor=teacher.email, action="spec_edit",
                         detail=f"spec {spec_id}: unit '{removed.get('name')}' deleted ({len(removed.get('learning_outcomes', []))} learning outcome(s))"))
    db.commit()
    return RedirectResponse(url=f"/spec-library/{spec_id}/review?saved={quote('Deleted ' + str(removed.get('name')))}", status_code=303)
