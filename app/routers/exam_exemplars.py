import os
import uuid

from fastapi import APIRouter, Request, Depends, Form, UploadFile, File
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Teacher, SpecVersion, ExamExemplar, AuditLogEntry
from ..auth import require_spec_uploader
from ..docx_annotator import extract_text

router = APIRouter()
UPLOAD_DIR = "storage/uploads"


@router.post("/spec-library/{spec_id}/exam-exemplars/upload")
async def upload_exam_exemplar(
    spec_id: int,
    request: Request,
    label: str = Form(...),
    exemplar_files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_spec_uploader),
):
    """
    Standing fix: previously its own separate "Admin: Exam Exemplars" page with its own
    spec-picking step - moved onto the spec's own detail page instead, since a curriculum
    manager managing exemplars is already looking at that spec's units when they'd want to
    add one. No need to re-select the spec from scratch, and one less standalone page
    cluttering the Admin section.

    unit_names is a real multi-select, not a single value - confirmed as a real
    requirement: one exemplar (e.g. a synoptic Occupational Specialism assessment) can
    genuinely cover several content areas at once, not just one.

    Accepts MULTIPLE files in one submission, not just one - confirmed as a real need: a
    real Occupational Specialism assessment can span several separate documents (a brief
    and a model answer per task, across several tasks), and having to repeat the whole
    label/unit-selection process once per file would be real, avoidable friction. Each
    file becomes its own ExamExemplar row (individually removable later without affecting
    the others) but all share the SAME unit selection and label from this one submission -
    the file list on the spec page distinguishes them by filename.
    """
    spec = db.get(SpecVersion, spec_id)
    if not spec:
        return RedirectResponse(url="/spec-library", status_code=303)

    form = await request.form()
    unit_names = form.getlist("unit_names")
    exemplar_files = [f for f in exemplar_files if f.filename]

    if not unit_names or not exemplar_files:
        error = "Choose at least one unit and at least one file to upload." if not unit_names else "Choose at least one file to upload."
        return RedirectResponse(url=f"/spec-library/{spec_id}?exemplar_error={error}", status_code=303)

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    added_filenames = []
    for exemplar_file in exemplar_files:
        stored_name = f"{uuid.uuid4().hex}_{exemplar_file.filename}"
        stored_path = os.path.join(UPLOAD_DIR, stored_name)
        contents = await exemplar_file.read()
        with open(stored_path, "wb") as f:
            f.write(contents)

        try:
            extracted = extract_text(stored_path, exemplar_file.filename)
        except Exception:
            extracted = ""

        exemplar = ExamExemplar(
            uploaded_by_teacher_id=teacher.id, spec_version_id=spec_id, unit_names=unit_names,
            label=label.strip() or exemplar_file.filename, file_path=stored_path,
            original_filename=exemplar_file.filename, extracted_text=extracted or None,
        )
        db.add(exemplar)
        added_filenames.append(exemplar_file.filename)

    db.add(AuditLogEntry(
        actor=teacher.email, action="exam_exemplar_uploaded",
        detail=f"Added {len(added_filenames)} exam exemplar file(s) under '{label.strip()}' "
               f"for unit(s): {', '.join(unit_names)}. Files: {', '.join(added_filenames)}.",
    ))
    db.commit()
    return RedirectResponse(url=f"/spec-library/{spec_id}", status_code=303)


@router.post("/exam-exemplars/{exemplar_id}/delete")
def delete_exam_exemplar(
    exemplar_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_spec_uploader),
):
    exemplar = db.get(ExamExemplar, exemplar_id)
    spec_id = exemplar.spec_version_id if exemplar else None
    if exemplar:
        db.add(AuditLogEntry(
            actor=teacher.email, action="exam_exemplar_deleted",
            detail=f"Removed exam exemplar '{exemplar.label}' (unit(s): {', '.join(exemplar.unit_names or [])}).",
        ))
        # File left on disk deliberately, same reasoning as SOW templates - any scheme
        # already generated using this exemplar's content already has that content baked
        # into its own weeks_json, not a live reference to this file.
        db.delete(exemplar)
        db.commit()
    return RedirectResponse(url=f"/spec-library/{spec_id}" if spec_id else "/spec-library", status_code=303)
