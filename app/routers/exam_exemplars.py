import os
import uuid
import mimetypes

from fastapi import APIRouter, Request, Depends, Form, UploadFile, File, HTTPException
from fastapi.responses import RedirectResponse, FileResponse
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

    Each file's full processing (disk write, text extraction, DB row) is isolated in its
    own try/except - previously only the extraction step was guarded, so a failure
    anywhere else in one file's handling could take the entire request down as a 500,
    losing every other file in the same submission along with it. Now one bad file is
    skipped and reported by name; everything else in the same batch still succeeds.

    Commits PER FILE, not once for the whole batch - confirmed as a real remaining gap:
    a commit-time-only failure (something that only surfaces when SQLAlchemy actually
    flushes to the database, not during the file's own processing) previously still hit
    one shared, unguarded commit() after the loop and could take the whole batch down
    even though every individual file's own handling had already "succeeded". Per-file
    commits mean that failure mode is now isolated exactly like every other one.

    A top-level try/except wraps the whole function as a last-resort safety net - any
    failure that still isn't caught by the above produces a real, readable error message
    on the spec page instead of a bare 500 with no information to go on.
    """
    try:
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
        failed_files = []
        for exemplar_file in exemplar_files:
            base_name = os.path.basename(exemplar_file.filename)
            if base_name.startswith("."):
                # Folder selection always picks up OS metadata files alongside real
                # documents (.DS_Store on macOS is the common one) - these were never a
                # real exemplar the person meant to add, so skip silently rather than
                # reporting them as a failure the person would need to do anything about.
                continue
            try:
                stored_name = f"{uuid.uuid4().hex}_{base_name}"
                stored_path = os.path.join(UPLOAD_DIR, stored_name)
                contents = await exemplar_file.read()
                with open(stored_path, "wb") as f:
                    f.write(contents)

                try:
                    extracted = extract_text(stored_path, base_name)
                except Exception:
                    extracted = ""

                exemplar = ExamExemplar(
                    uploaded_by_teacher_id=teacher.id, spec_version_id=spec_id, unit_names=unit_names,
                    unit_name=unit_names[0] if unit_names else "",  # legacy column, see the model's own docstring - satisfies a real, physical NOT NULL constraint the live database still has from before the multiunit migration
                    label=label.strip() or base_name, file_path=stored_path,
                    original_filename=base_name, extracted_text=extracted or None,
                )
                db.add(exemplar)
                db.commit()
                added_filenames.append(base_name)
            except Exception as e:
                db.rollback()
                failed_files.append(f"{base_name} ({e})")

        if added_filenames:
            db.add(AuditLogEntry(
                actor=teacher.email, action="exam_exemplar_uploaded",
                detail=f"Added {len(added_filenames)} exam exemplar file(s) under '{label.strip()}' "
                       f"for unit(s): {', '.join(unit_names)}. Files: {', '.join(added_filenames)}."
                       + (f" ({len(failed_files)} file(s) failed)" if failed_files else ""),
            ))
            db.commit()

        if failed_files:
            error = f"{len(failed_files)} file(s) could not be added: {', '.join(failed_files)}." + (f" {len(added_filenames)} other file(s) uploaded successfully." if added_filenames else "")
            return RedirectResponse(url=f"/spec-library/{spec_id}?exemplar_error={error}", status_code=303)
        return RedirectResponse(url=f"/spec-library/{spec_id}", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/spec-library/{spec_id}?exemplar_error=Upload failed: {e}", status_code=303)


@router.get("/exam-exemplars/{exemplar_id}/file")
def view_exemplar_file(
    exemplar_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(require_spec_uploader),
):
    """
    Serves the original uploaded file for viewing - confirmed as a real need: with
    several files uploaded at once, there was no way to actually open one and check it's
    the right document short of trusting the filename shown in the list.

    PDFs are served for inline viewing, so a link to this route opening in a new tab
    shows the actual document immediately, matching exactly what was asked. DOCX files
    have no native browser renderer at all - the browser downloads them regardless of
    what the server sends, which is a real limitation of the file type, not something
    fixable from here.
    """
    exemplar = db.get(ExamExemplar, exemplar_id)
    if not exemplar or not os.path.exists(exemplar.file_path):
        raise HTTPException(status_code=404, detail="This file is no longer available - it may have been removed from storage.")
    media_type, _ = mimetypes.guess_type(exemplar.original_filename)
    disposition = "inline" if media_type == "application/pdf" else "attachment"
    return FileResponse(
        exemplar.file_path, filename=exemplar.original_filename,
        media_type=media_type or "application/octet-stream", content_disposition_type=disposition,
    )


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
