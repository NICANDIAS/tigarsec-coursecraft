import os
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
from ..models import Teacher, SpecVersion, Course, Submission, DiscoveredSpecFormat, ExamExemplar
from ..course_access import get_course_for_teacher
from ..auth import get_current_teacher, require_spec_uploader
from ..docx_annotator import extract_text
from ..llm_gateway import get_effective_provider
from ..spec_parser import parse_spec_hybrid
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
    return templates.TemplateResponse(
        "spec_library.html",
        {"request": request, "teacher": teacher, "specs": specs, "can_upload": can_upload},
    )


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

    if spec_file is not None and spec_file.filename:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        temp_name = f"{uuid.uuid4().hex}_{spec_file.filename}"
        temp_path = os.path.join(UPLOAD_DIR, temp_name)
        contents = await spec_file.read()
        with open(temp_path, "wb") as f:
            f.write(contents)
        raw_text = extract_text(temp_path, spec_file.filename)

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
            known_patterns = [
                {
                    "label": p.label,
                    "unit_header_regex": p.unit_header_regex,
                    "sub_heading_regex": p.sub_heading_regex,
                    "criterion_regex": p.criterion_regex,
                }
                for p in db.query(DiscoveredSpecFormat).all()
            ]

            result = parse_spec_hybrid(
                spec.raw_text, progress_callback=progress, known_patterns=known_patterns,
                cancel_check=is_cancelled,
            )
            spec.structured_json = {"units": result.get("units", [])}
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
                if discovered:
                    db.add(DiscoveredSpecFormat(
                        label=discovered.get("label", "Unnamed format")[:255],
                        unit_header_regex=discovered["unit_header_regex"],
                        sub_heading_regex=discovered.get("sub_heading_regex"),
                        criterion_regex=discovered["criterion_regex"],
                        description=discovered.get("description", ""),
                    ))
                    summary += " (New format learned and saved for future specs.)"

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
    spec = db.get(SpecVersion, spec_id)
    if spec:
        spec.parse_status = "parsing"
        spec.parse_progress = "Queued..."
        spec.parse_progress_updated_at = datetime.datetime.utcnow()
        spec.parse_error = None
        spec.cancel_requested = False
        db.commit()
        background_tasks.add_task(_run_parse_job, spec.id)
    return RedirectResponse(url=f"/spec-library/{spec_id}", status_code=303)


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
    return templates.TemplateResponse(
        "spec_detail.html",
        {"request": request, "teacher": teacher, "spec": spec, "can_upload": can_upload,
         "delete_error": delete_error or None, "exemplars": exemplars},
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

    if courses_using or submissions_using_count:
        parts = []
        if courses_using:
            names = ", ".join(c.name for c in courses_using)
            parts.append(f"attached to course(s): {names} - detach it there first (course page → attach a different spec)")
        if submissions_using_count:
            parts.append(
                f"{submissions_using_count} submission(s) were marked against it - that history is kept "
                f"permanently by design, so this spec can't be deleted while those exist"
            )
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
