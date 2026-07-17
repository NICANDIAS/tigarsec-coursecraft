import os
import json
import uuid
import datetime
from fastapi import APIRouter, Request, Depends, Form, UploadFile, File, BackgroundTasks
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db, SessionLocal
from ..models import (
    Teacher, Submission, CriterionMark, IVRequest, IVCriterionCheck, SpecVersion, AuditLogEntry,
)
from ..auth import get_current_teacher
from ..docx_annotator import extract_text
from ..llm_gateway import complete_json

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

UPLOAD_DIR = "storage/uploads"

IV_SYSTEM_PROMPT = """You are supporting a UK further-education internal verification (IV) process. \
An original marker has already marked student work against a specification. Your job is NOT to \
re-mark from scratch - it's to give the IV-ing teacher a second opinion on whether each ORIGINAL \
mark and comment look well-supported by the evidence, so they can make their own final IV judgement \
faster. Be direct about disagreements - IV exists to catch marking errors, softening your view \
defeats the purpose.

If an IV format/style sample is provided, broadly match its tone/structure for your suggestion text, \
but the substance of your judgement must be your own honest assessment.

Return ONLY raw JSON (no markdown fences, no commentary) matching exactly this shape:
{
  "checks": [
    {
      "criterion_ref": "string - must match one of the original criteria given to you exactly",
      "ai_suggestion": "string - 1-3 sentences: does the original mark look supported by the evidence? \
flag anything questionable specifically",
      "confidence": 0.0
    }
  ],
  "overall_summary": "string - a short overall IV summary"
}

confidence is how confident YOU are in your own IV suggestion (0.0-1.0), not a re-statement of the \
original marker's confidence."""


@router.get("/iv", response_class=HTMLResponse)
def iv_list(
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    assigned_to_me = db.query(IVRequest).filter(IVRequest.assigned_to_teacher_id == teacher.id).order_by(IVRequest.id.desc()).all()
    sent_by_me = db.query(IVRequest).filter(IVRequest.requested_by_teacher_id == teacher.id).order_by(IVRequest.id.desc()).all()
    return templates.TemplateResponse(
        "iv_list.html", {"request": request, "teacher": teacher, "assigned_to_me": assigned_to_me, "sent_by_me": sent_by_me}
    )


@router.get("/iv/send/{submission_id}", response_class=HTMLResponse)
def send_for_iv_page(
    submission_id: int,
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    submission = db.query(Submission).filter(Submission.id == submission_id).first()
    other_teachers = db.query(Teacher).filter(Teacher.id != teacher.id, Teacher.is_active == True).all()
    return templates.TemplateResponse(
        "iv_send.html", {"request": request, "teacher": teacher, "submission": submission, "other_teachers": other_teachers}
    )


@router.post("/iv/send/{submission_id}")
async def send_for_iv(
    submission_id: int,
    background_tasks: BackgroundTasks,
    assigned_to_teacher_id: int = Form(...),
    iv_format_file: UploadFile = File(None),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    submission = db.query(Submission).filter(Submission.id == submission_id).first()
    if not submission or not submission.marking_run:
        return RedirectResponse(url="/iv", status_code=303)

    iv_format_text = ""
    if iv_format_file is not None and iv_format_file.filename:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        temp_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex}_{iv_format_file.filename}")
        contents = await iv_format_file.read()
        with open(temp_path, "wb") as f:
            f.write(contents)
        iv_format_text = extract_text(temp_path, iv_format_file.filename)

    iv_request = IVRequest(
        submission_id=submission.id,
        spec_version_id=submission.spec_version_id,
        requested_by_teacher_id=teacher.id,
        assigned_to_teacher_id=assigned_to_teacher_id,
        iv_format_sample=iv_format_text or None,
        status="pending",
    )
    db.add(iv_request)
    db.commit()
    db.refresh(iv_request)

    original_marks = db.query(CriterionMark).filter(CriterionMark.marking_run_id == submission.marking_run.id).all()
    for m in original_marks:
        db.add(IVCriterionCheck(
            iv_request_id=iv_request.id, criterion_ref=m.criterion_ref,
            original_mark=m.final_mark, original_comment=m.final_comment,
        ))
    submission.status = "sent_for_iv"
    db.add(AuditLogEntry(
        submission_id=submission.id, actor=teacher.email, action="sent_for_iv",
        detail=f"Sent to teacher id {assigned_to_teacher_id} for IV.",
    ))
    db.commit()

    background_tasks.add_task(_run_iv_ai_assist, iv_request.id)
    return RedirectResponse(url="/iv", status_code=303)


def _run_iv_ai_assist(iv_request_id: int):
    db = SessionLocal()
    try:
        iv_request = db.get(IVRequest, iv_request_id)
        if not iv_request:
            return
        try:
            iv_request.status = "ai_assisting"
            db.commit()

            checks = db.query(IVCriterionCheck).filter(IVCriterionCheck.iv_request_id == iv_request.id).all()
            spec = iv_request.spec_version

            if iv_request.submission_id and iv_request.submission:
                submission_text = extract_text(
                    iv_request.submission.stored_path, iv_request.submission.original_filename
                )
            else:
                submission_text = ""
                if iv_request.external_file_path:
                    submission_text = extract_text(iv_request.external_file_path, "external_submission.docx")

            original_marks_summary = "\n".join(
                f"- {c.criterion_ref}: mark='{c.original_mark}', comment='{c.original_comment}'" for c in checks
            )
            if iv_request.external_original_marks_summary:
                original_marks_summary += f"\n\nTeacher-provided summary of marks given:\n{iv_request.external_original_marks_summary}"

            prompt_parts = [
                f"Course specification criteria:\n{json.dumps(spec.structured_json, indent=2) if spec.structured_json else 'Not parsed - proceed with general judgement.'}",
                f"Original marks/comments to verify:\n{original_marks_summary}",
                f"Submission text:\n{submission_text[:12000]}",
            ]
            if iv_request.iv_format_sample:
                prompt_parts.append(f"IV format/style sample:\n{iv_request.iv_format_sample[:3000]}")

            result = complete_json(system=IV_SYSTEM_PROMPT, prompt="\n\n".join(prompt_parts), max_tokens=6000)

            result_by_ref = {c.get("criterion_ref"): c for c in result.get("checks", [])}
            for check in checks:
                match = result_by_ref.get(check.criterion_ref)
                if match:
                    check.ai_suggestion = match.get("ai_suggestion", "")
                    check.ai_confidence = float(match.get("confidence", 0.0))

            iv_request.ai_overall_summary = result.get("overall_summary", "")
            iv_request.status = "ready_for_review"

        except Exception as e:
            iv_request.status = "failed"
            iv_request.error_detail = str(e)

        db.commit()
    finally:
        db.close()


@router.get("/iv/{iv_id}/review", response_class=HTMLResponse)
def iv_review(
    iv_id: int,
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    iv_request = db.get(IVRequest, iv_id)
    checks = db.query(IVCriterionCheck).filter(IVCriterionCheck.iv_request_id == iv_id).all()
    return templates.TemplateResponse(
        "iv_review.html", {"request": request, "teacher": teacher, "iv_request": iv_request, "checks": checks}
    )


@router.post("/iv/{iv_id}/submit")
async def submit_iv_review(
    iv_id: int,
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    form = await request.form()
    iv_request = db.get(IVRequest, iv_id)
    checks = db.query(IVCriterionCheck).filter(IVCriterionCheck.iv_request_id == iv_id).all()
    for check in checks:
        check.iv_agree = form.get(f"agree_{check.id}") == "yes"
        check.iv_comment = form.get(f"comment_{check.id}", "")

    iv_request.status = "completed"
    iv_request.completed_at = datetime.datetime.utcnow()
    if iv_request.submission:
        iv_request.submission.status = "iv_complete"
    db.add(AuditLogEntry(
        submission_id=iv_request.submission_id, actor=teacher.email, action="iv_completed",
        detail=f"IV completed by {teacher.email}.",
    ))
    db.commit()
    return RedirectResponse(url="/iv", status_code=303)


# --- External flow: work already marked outside this tool ---

@router.get("/iv/external/new", response_class=HTMLResponse)
def new_external_iv_page(
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    specs = db.query(SpecVersion).filter(SpecVersion.parse_status == "parsed").all()
    other_teachers = db.query(Teacher).filter(Teacher.id != teacher.id, Teacher.is_active == True).all()
    return templates.TemplateResponse(
        "iv_external_new.html", {"request": request, "teacher": teacher, "specs": specs, "other_teachers": other_teachers}
    )


@router.post("/iv/external/new")
async def create_external_iv(
    background_tasks: BackgroundTasks,
    spec_version_id: int = Form(...),
    assigned_to_teacher_id: int = Form(...),
    marks_summary: str = Form(...),
    marked_file: UploadFile = File(...),
    iv_format_file: UploadFile = File(None),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    marked_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex}_{marked_file.filename}")
    contents = await marked_file.read()
    with open(marked_path, "wb") as f:
        f.write(contents)

    iv_format_text = ""
    if iv_format_file is not None and iv_format_file.filename:
        temp_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex}_{iv_format_file.filename}")
        iv_contents = await iv_format_file.read()
        with open(temp_path, "wb") as f:
            f.write(iv_contents)
        iv_format_text = extract_text(temp_path, iv_format_file.filename)

    iv_request = IVRequest(
        spec_version_id=spec_version_id,
        requested_by_teacher_id=teacher.id,
        assigned_to_teacher_id=assigned_to_teacher_id,
        external_file_path=marked_path,
        external_original_marks_summary=marks_summary,
        iv_format_sample=iv_format_text or None,
        status="pending",
    )
    db.add(iv_request)
    db.commit()
    db.refresh(iv_request)

    # For the external flow there's no existing CriterionMark rows to snapshot from -
    # the AI job below creates IVCriterionCheck rows directly from its own reading of
    # the spec + the marks summary the teacher typed in.
    spec = db.get(SpecVersion, spec_version_id)
    if spec and spec.structured_json:
        for unit in spec.structured_json.get("units", []):
            for lo in unit.get("learning_outcomes", []):
                for c in lo.get("criteria", []):
                    ref = f"{unit.get('name')} / {lo.get('code')} / {c.get('code')}"
                    db.add(IVCriterionCheck(iv_request_id=iv_request.id, criterion_ref=ref))
    db.commit()

    background_tasks.add_task(_run_iv_ai_assist, iv_request.id)
    return RedirectResponse(url="/iv", status_code=303)
