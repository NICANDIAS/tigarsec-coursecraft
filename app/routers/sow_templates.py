import os
import uuid

from fastapi import APIRouter, Request, Depends, Form, UploadFile, File
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Teacher, SOWTemplate, AuditLogEntry
from ..auth import require_spec_uploader

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
UPLOAD_DIR = "storage/uploads"


@router.get("/admin/sow-templates", response_class=HTMLResponse)
def list_sow_templates(
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_spec_uploader),
):
    sow_templates = db.query(SOWTemplate).order_by(SOWTemplate.created_at.desc()).all()
    return templates.TemplateResponse(
        "admin_sow_templates.html", {"request": request, "teacher": teacher, "sow_templates": sow_templates, "error": None},
    )


@router.post("/admin/sow-templates/upload")
async def upload_sow_template(
    request: Request,
    label: str = Form(...),
    template_file: UploadFile = File(...),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_spec_uploader),
):
    """
    Shared, institution-wide template - same role gate as the spec library, so lecturers
    don't each need their own copy of the same college scheme-of-work template. Only .docx
    is accepted, matching the existing per-generation upload's own restriction (only that
    format can be filled in place).
    """
    if not template_file.filename or not template_file.filename.lower().endswith(".docx"):
        sow_templates = db.query(SOWTemplate).order_by(SOWTemplate.created_at.desc()).all()
        return templates.TemplateResponse(
            "admin_sow_templates.html",
            {"request": request, "teacher": teacher, "sow_templates": sow_templates,
             "error": "Only .docx files can be used as a scheme-of-work template."},
        )

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}_{template_file.filename}"
    stored_path = os.path.join(UPLOAD_DIR, stored_name)
    contents = await template_file.read()
    with open(stored_path, "wb") as f:
        f.write(contents)

    sow_template = SOWTemplate(
        uploaded_by_teacher_id=teacher.id, label=label.strip() or template_file.filename,
        file_path=stored_path, original_filename=template_file.filename,
    )
    db.add(sow_template)
    db.flush()
    db.add(AuditLogEntry(
        actor=teacher.email, action="sow_template_uploaded",
        detail=f"Added shared scheme-of-work template '{sow_template.label}' ({template_file.filename}).",
    ))
    db.commit()
    return RedirectResponse(url="/admin/sow-templates", status_code=303)


@router.post("/admin/sow-templates/{template_id}/delete")
def delete_sow_template(
    template_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_spec_uploader),
):
    template = db.get(SOWTemplate, template_id)
    if template:
        db.add(AuditLogEntry(
            actor=teacher.email, action="sow_template_deleted",
            detail=f"Removed shared scheme-of-work template '{template.label}'.",
        ))
        # The file itself is deliberately left on disk, not removed here - any already-
        # generated scheme that used this template already has its OWN copy of the file
        # (see SchemeOfWork.sow_template_id's docstring), so this only stops it being
        # offered for NEW generations, matching how removing a spec doesn't retroactively
        # affect schemes already built from it.
        db.delete(template)
        db.commit()
    return RedirectResponse(url="/admin/sow-templates", status_code=303)
