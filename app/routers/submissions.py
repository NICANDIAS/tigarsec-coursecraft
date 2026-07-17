import os
import json
import re
import uuid
import hashlib
import datetime
from fastapi import APIRouter, Request, Depends, Form, UploadFile, File, BackgroundTasks
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db, SessionLocal
from ..models import (
    Teacher, Course, Student, Submission, SubmissionFile, MarkingRun,
    CriterionMark, TeacherOverride, AuditLogEntry, FeedbackStyleTemplate,
)
from ..auth import get_current_teacher
from ..docx_annotator import extract_text, extract_text_with_locations, annotate_submission
from ..spec_parser import _strip_repeated_boilerplate_lines  # same fix as spec parsing - repeated page headers/footers confuse marking too, confirmed on a real submission
from ..llm_gateway import complete_json, get_effective_provider
from ..similarity_check import find_similar_pairs

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _bold_markdown(text: str):
    """Converts **word** to real bold, safely - escapes the surrounding text first (so
    nothing else in the AI's comment can inject HTML) and only unescapes the specific
    bold markup, then marks the result safe for Jinja to render as-is. Used so command
    words (explain/describe/justify/etc.) the AI identifies from the actual LO wording
    render as visibly bold in feedback, rather than showing literal asterisks."""
    from markupsafe import Markup, escape
    if not text:
        return text
    escaped = str(escape(text))
    bolded = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', escaped)
    return Markup(bolded)


templates.env.filters["bold_markdown"] = _bold_markdown

UPLOAD_DIR = "storage/uploads"
ANNOTATED_DIR = "storage/annotated"

FEEDBACK_STYLE_INSTRUCTIONS = {
    "standard": "Write each comment as clear, constructive, specific feedback in 1-3 sentences.",
    "wwebi": 'Structure EVERY comment using exactly this format: "What went well: ... Even better if: ...". '
             "Both parts are required for every criterion, even where the mark is low (What went well can "
             "note effort or a partial attempt) or high (Even better if can note a small stretch/refinement).",
}

def _is_criterion_met(final_mark: str) -> bool:
    """Heuristic over free-text marks (kept free-text to support rubric/numeric schemes
    too, not just P/M/D) - considers a criterion met only if it clearly says so, not
    partly, not not-met."""
    if not final_mark:
        return False
    text = final_mark.lower()
    if "not" in text:
        return False
    if "partly" in text or "partial" in text:
        return False
    return "met" in text or "achieved" in text or "pass" in text


def compute_overall_grade(marks) -> str | None:
    """
    Standard BTEC/Pearson aggregation rule: to achieve Merit, ALL Pass-band AND ALL
    Merit-band criteria must be met (not just some) - Merit implies Pass, Distinction
    implies Merit and Pass. Returns None if the criteria don't use recognisable P/M/D
    codes at all (e.g. T-Level decimal codes, or a rubric/numeric scheme) - this
    aggregation rule is specific to the P/M/D convention, not a general one.
    """
    bands = {"P": [], "M": [], "D": []}
    recognized_any = False
    for m in marks:
        match = re.search(r'\b([PMD])\d+\b', m.criterion_ref or "")
        if match:
            recognized_any = True
            bands[match.group(1)].append(m)

    if not recognized_any:
        return None

    pass_met = bool(bands["P"]) and all(_is_criterion_met(m.final_mark) for m in bands["P"])
    merit_met = pass_met and bool(bands["M"]) and all(_is_criterion_met(m.final_mark) for m in bands["M"])
    distinction_met = merit_met and bool(bands["D"]) and all(_is_criterion_met(m.final_mark) for m in bands["D"])

    if distinction_met:
        return "Distinction"
    if merit_met:
        return "Merit"
    if pass_met:
        return "Pass"
    return "Not yet achieved"


MARKING_SYSTEM_PROMPT_TEMPLATE = """You are an experienced UK further-education marker helping a teacher \
mark student coursework against a specific course specification. You give fair, evidence-based \
marks and constructive, specific feedback - never invent evidence that isn't in the submission.

Match the teacher's own feedback style/tone shown in their exemplars where provided, but the \
substance and accuracy of the marking judgement must be your own honest assessment, not softened \
to match tone.

Feedback format instruction: {feedback_instruction}

The submission may consist of MULTIPLE files (e.g. a report, a presentation, a spreadsheet, a PDF) -
each is clearly marked with a "=== FILE: <filename> ===" header, and within each file, locations are
tagged where available ("[Slide N]" for presentations, "[Page N]" for PDFs, "[Sheet: X, Row Y]" for
spreadsheets). A single criterion's evidence may come from ANY of the files, or be split across more
than one - always identify exactly which file and, where a location tag is present, which location
the evidence came from.

STRICT COMMAND-WORD MARKING: mark strictly against the actual command word(s) used in each
criterion's own wording in the specification (e.g. "explain", "describe", "identify", "analyse",
"evaluate", "justify", "compare") - these words define what LEVEL of response is actually required,
and a criterion is not met just because the topic was mentioned. "Describe" needs a clear account of
what something is/does; "explain" needs the reasoning of why/how; "analyse" needs breaking something
down and examining relationships; "evaluate"/"justify" need a reasoned judgement, not just description.
If the student's response only describes when the criterion says "explain" (or similarly falls short
of the actual command word's demand), that is specifically why it is not met or only partly met - say
so explicitly. In your "comment", wrap the exact command word(s) from the spec in double asterisks
(e.g. **explain**, **justify**) wherever you reference them, so the teacher can see precisely which
required command word drove the judgement.

Return ONLY raw JSON (no markdown fences, no commentary) matching exactly this shape:
{{
  "marks": [
    {{
      "criterion_ref": "string - e.g. 'Unit 3 / LO2 / P2'",
      "mark": "string - one of 'Met', 'Partly met', 'Not met', or a numeric/lettered mark if the rubric is numeric",
      "confidence": 0.0,
      "evidence_text": "string - a short (under 15 words) exact quote from the submission that most \
supports this judgement, so it can be anchored as a comment. Empty string if no clear single quote applies.",
      "source_file": "string - the filename this evidence came from, e.g. 'presentation.pptx'. Empty string if not applicable/no marks yet.",
      "source_location": "string - e.g. 'Slide 2', 'Page 3', 'Sheet1 Row 12', 'Q25' if a question number is visible in the text, or empty string if the file format has no location tags (e.g. a .docx report)",
      "comment": "string - specific feedback for the student on this criterion, following the feedback \
format instruction above, in the teacher's style. Wrap the spec's own command word(s) in **asterisks** \
wherever referenced (see STRICT COMMAND-WORD MARKING below). If source_file/source_location are known, \
weave them in naturally, e.g. 'This is well evidenced in your presentation, Slide 2' or 'Your answer to Q25 shows...'",
      "needs_human_review": false,
      "human_review_reason": "string - ONLY set if needs_human_review is true: explain specifically why you \
couldn't assess this yourself, e.g. 'This criterion likely requires evidence from a diagram/image that isn't \
extractable as text from this file format' or 'No content addressing this criterion was found in any uploaded file'"
    }}
  ],
  "overall_summary": "string - a short overall summary comment for the whole submission"
}}

confidence must be a number between 0.0 (very unsure, needs teacher review) and 1.0 (very confident). \
Be honest about confidence - do not default to high confidence.

IMPORTANT on needs_human_review: if you genuinely cannot find or assess evidence for a criterion - most \
commonly because the actual answer is only visible as an image, diagram, drawing, or other non-text \
content that didn't extract as text - set needs_human_review to true with a specific reason, rather than \
guessing a mark, inventing evidence, or leaving the criterion out entirely. Every criterion given to you \
must appear in your response, either with a real judgement or flagged for human review - never omit one \
and never return an empty marks list."""


@router.get("/courses/{course_id}/students/{student_id}/submissions/new", response_class=HTMLResponse)
def new_submission_page(
    course_id: int,
    student_id: int,
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    course = db.query(Course).filter(Course.id == course_id, Course.teacher_id == teacher.id).first()
    student = db.query(Student).filter(Student.id == student_id, Student.course_id == course_id).first()
    has_parsed_spec = bool(course and course.spec_version and course.spec_version.structured_json)
    units = course.spec_version.structured_json.get("units", []) if has_parsed_spec else []
    return templates.TemplateResponse(
        "submission_upload.html",
        {"request": request, "teacher": teacher, "course": course, "student": student,
         "has_parsed_spec": has_parsed_spec, "units": units, "today": datetime.date.today().isoformat()},
    )


@router.post("/courses/{course_id}/students/{student_id}/submissions/upload")
async def upload_submission(
    course_id: int,
    student_id: int,
    submission_type: str = Form("essay"),
    target_unit_name: str = Form(""),
    submitted_at: str = Form(""),
    marking_scheme_notes: str = Form(""),
    marking_scheme_file: UploadFile = File(None),
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """
    Accepts one or more files for a single submission - e.g. a report, presentation,
    spreadsheet, and PDF all evidencing the same learning outcome. All of them get
    considered together when marking.
    """
    course = db.query(Course).filter(Course.id == course_id, Course.teacher_id == teacher.id).first()
    if not course or not course.spec_version:
        return RedirectResponse(url=f"/courses/{course_id}", status_code=303)

    parsed_submitted_at = None
    if submitted_at:
        try:
            parsed_submitted_at = datetime.datetime.strptime(submitted_at, "%Y-%m-%d").date()
        except ValueError:
            pass  # malformed date - leave as None rather than fail the whole upload over it

    combined_scheme_notes_parts = [marking_scheme_notes.strip()] if marking_scheme_notes.strip() else []
    if marking_scheme_file is not None and marking_scheme_file.filename:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        temp_name = f"{uuid.uuid4().hex}_{marking_scheme_file.filename}"
        temp_path = os.path.join(UPLOAD_DIR, temp_name)
        contents = await marking_scheme_file.read()
        with open(temp_path, "wb") as out:
            out.write(contents)
        try:
            combined_scheme_notes_parts.append(extract_text(temp_path, marking_scheme_file.filename))
        except Exception:
            pass  # a bad rubric file shouldn't block the whole upload
    combined_scheme_notes = "\n\n".join(combined_scheme_notes_parts) or None

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    saved_files = []
    hash_input = b""
    for f in files:
        if not f.filename:
            continue
        stored_name = f"{uuid.uuid4().hex}_{f.filename}"
        stored_path = os.path.join(UPLOAD_DIR, stored_name)
        contents = await f.read()
        with open(stored_path, "wb") as out:
            out.write(contents)
        hash_input += contents
        saved_files.append((f.filename, stored_path))

    if not saved_files:
        return RedirectResponse(url=f"/courses/{course_id}/students/{student_id}", status_code=303)

    content_hash = hashlib.sha256(hash_input).hexdigest()
    display_name = saved_files[0][0] if len(saved_files) == 1 else f"{saved_files[0][0]} + {len(saved_files) - 1} more"

    submission = Submission(
        student_id=student_id,
        spec_version_id=course.spec_version_id,
        target_unit_name=target_unit_name or None,
        submission_type=submission_type,
        original_filename=display_name,
        content_hash=content_hash,
        submitted_at=parsed_submitted_at or datetime.date.today(),
        marking_scheme_notes=combined_scheme_notes,
        status="uploaded",
    )
    db.add(submission)
    db.commit()
    db.refresh(submission)

    for filename, stored_path in saved_files:
        db.add(SubmissionFile(submission_id=submission.id, original_filename=filename, stored_path=stored_path))
    db.commit()

    db.add(AuditLogEntry(
        submission_id=submission.id, actor=teacher.email, action="uploaded",
        detail=f"Files: {', '.join(fn for fn, _ in saved_files)}, sha256: {content_hash}",
    ))
    db.commit()

    # Automatic similarity check against other students' submissions for the SAME
    # assignment in this course - runs right here (not as a separate manual step) since
    # it's pure local text comparison with no AI call, fast enough not to need a
    # background task. Only compares within the same target_unit_name, since comparing
    # across genuinely different assignments isn't meaningful.
    try:
        this_submission_text = ""
        for filename, stored_path in saved_files:
            try:
                this_submission_text += "\n" + extract_text_with_locations(stored_path, filename)
            except Exception:
                continue

        other_student_ids = [
            s.id for s in db.query(Student).filter(Student.course_id == course_id).all()
        ]
        other_submissions = (
            db.query(Submission)
            .filter(
                Submission.student_id.in_(other_student_ids),
                Submission.id != submission.id,
                Submission.target_unit_name == (target_unit_name or None),
            )
            .all()
        )
        others_with_text = []
        for other_sub in other_submissions:
            other_files = db.query(SubmissionFile).filter(SubmissionFile.submission_id == other_sub.id).all()
            other_text = ""
            for sf in other_files:
                try:
                    other_text += "\n" + extract_text_with_locations(sf.stored_path, sf.original_filename)
                except Exception:
                    continue
            other_student = db.query(Student).filter(Student.id == other_sub.student_id).first()
            others_with_text.append({
                "submission_id": other_sub.id,
                "student_name": other_student.name if other_student else "?",
                "text": other_text,
            })

        matches = find_matches_for_one(this_submission_text, others_with_text)
        if matches:
            top = matches[0]
            submission.similarity_warning = (
                f"{top['similarity']}% text similarity with {top['student_name']}'s submission for "
                f"this same assignment" + (f" (and {len(matches) - 1} other match(es))" if len(matches) > 1 else "") + "."
            )
            db.commit()
    except Exception:
        pass  # the similarity check is a bonus signal, not core functionality - never let it block an upload

    return RedirectResponse(url=f"/submissions/{submission.id}/review", status_code=303)


@router.post("/submissions/{submission_id}/mark")
def mark_submission(
    submission_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    submission = db.query(Submission).filter(Submission.id == submission_id).first()
    if not submission:
        return RedirectResponse(url="/", status_code=303)

    run = MarkingRun(submission_id=submission.id, status="in_progress")
    db.add(run)
    submission.status = "marking"
    db.commit()
    db.refresh(run)

    background_tasks.add_task(_run_marking_job, run.id, submission.id, teacher.id)

    return RedirectResponse(url=f"/submissions/{submission_id}/review", status_code=303)


def _run_marking_job(run_id: int, submission_id: int, teacher_id: int):
    """Runs in the background so the review page doesn't hang waiting on a slow model call."""
    db = SessionLocal()
    try:
        run = db.get(MarkingRun, run_id)
        submission = db.get(Submission, submission_id)
        teacher = db.get(Teacher, teacher_id)
        if not run or not submission or not teacher:
            return

        try:
            spec = submission.spec_version

            # Build combined text from all of this submission's files, each clearly
            # delimited so the model knows which file is which - old rows from before
            # multi-file support (single stored_path, no SubmissionFile rows) still work.
            submission_files = db.query(SubmissionFile).filter(SubmissionFile.submission_id == submission.id).all()
            if submission_files:
                text_parts = []
                for sf in submission_files:
                    try:
                        file_text = extract_text_with_locations(sf.stored_path, sf.original_filename)
                    except Exception as e:
                        file_text = f"(Could not extract text from this file: {e})"
                    file_text = _strip_repeated_boilerplate_lines(file_text)
                    text_parts.append(f"=== FILE: {sf.original_filename} ===\n{file_text}")
                submission_text = "\n\n".join(text_parts)
            elif submission.stored_path:
                submission_text = extract_text(submission.stored_path, submission.original_filename)
                submission_text = _strip_repeated_boilerplate_lines(submission_text)
            else:
                raise RuntimeError("This submission has no files attached to mark.")

            style_profile = teacher.style_profile or (
                "No specific style sample provided - use a clear, encouraging, constructive UK FE marking tone."
            )
            # Custom feedback styles (added via Settings, no code change needed) take priority
            # if the teacher's saved preference matches one; otherwise fall back to the two
            # built-in ones (standard / What Went Well-Even Better If).
            custom_template = db.query(FeedbackStyleTemplate).filter(
                FeedbackStyleTemplate.name == teacher.feedback_style
            ).first()
            if custom_template:
                feedback_instruction = custom_template.instructions
            else:
                feedback_instruction = FEEDBACK_STYLE_INSTRUCTIONS.get(
                    teacher.feedback_style, FEEDBACK_STYLE_INSTRUCTIONS["standard"]
                )
            system_prompt = MARKING_SYSTEM_PROMPT_TEMPLATE.format(feedback_instruction=feedback_instruction)

            # Scope the criteria to the submission's target unit if one was set, rather than
            # sending the WHOLE spec (which for a 20-40+ unit spec is a huge, unfocused prompt
            # that makes empty/malformed AI responses far more likely - this is the fix for
            # "review page shows nothing after marking"). Older submissions without a target
            # unit set fall back to the whole spec, same as before.
            all_units = (spec.structured_json or {}).get("units", [])
            if submission.target_unit_name:
                matching_units = [u for u in all_units if u.get("name") == submission.target_unit_name]
                criteria_source = {"units": matching_units} if matching_units else spec.structured_json
            else:
                criteria_source = spec.structured_json

            prompt_parts = [
                f"Course specification (structured criteria - scoped to the relevant unit):\n{json.dumps(criteria_source, indent=2)}",
                f"Teacher's feedback style to match:\n{style_profile}",
            ]
            if submission.marking_scheme_notes:
                prompt_parts.append(
                    f"CUSTOM MARKING SCHEME (overrides the default Met/Partly met/Not met scale - use "
                    f"THIS scale/rubric for the \"mark\" field instead, applying it per criterion exactly "
                    f"as described):\n{submission.marking_scheme_notes[:4000]}"
                )
            prompt_parts.append(f"Student submission text:\n{submission_text}")
            prompt = "\n\n".join(prompt_parts)

            # One retry, same reasoning as spec parsing: a single essay is much less likely
            # to hit the 10-minute timeout than an 81-chunk spec, but it's the same call
            # shape and there's no reason not to be equally resilient here.
            result = None
            last_error = None
            for attempt in (1, 2):
                try:
                    result = complete_json(system=system_prompt, prompt=prompt, max_tokens=8000)
                    break
                except Exception as e:
                    last_error = e
                    continue
            if result is None:
                raise last_error

            marks_data = result.get("marks", [])
            if not marks_data:
                provider_used = get_effective_provider()
                # Fail loudly instead of silently landing on an empty "needs_review" page -
                # a submission with zero marks isn't actually ready for review, it's a
                # failure. States which provider actually handled the call and includes the
                # raw response - a run of malformed/empty responses is a classic sign of a
                # small local model struggling, which should be an observable fact here, not
                # something to guess at from a generic message.
                raise RuntimeError(
                    f"[Provider used: {provider_used}] The AI returned zero marks for this submission. "
                    f"If provider is 'ollama', this is very likely a model-capability issue rather than "
                    f"a unit-selection problem - switch LLM_PROVIDER=anthropic in .env and rebuild. "
                    f"Raw model response: {json.dumps(result)[:1500]}"
                )

            for m in marks_data:
                db.add(CriterionMark(
                    marking_run_id=run.id,
                    criterion_ref=m.get("criterion_ref", "unknown"),
                    ai_mark=m.get("mark"),
                    ai_confidence=float(m.get("confidence", 0.0)),
                    evidence_text=m.get("evidence_text", ""),
                    ai_comment=m.get("comment", ""),
                    source_file=m.get("source_file") or None,
                    source_location=m.get("source_location") or None,
                    needs_human_review=bool(m.get("needs_human_review", False)),
                    human_review_reason=m.get("human_review_reason") or None,
                    final_mark=m.get("mark"),
                    final_comment=m.get("comment", ""),
                ))

            run.overall_summary = result.get("overall_summary", "")
            run.status = "needs_review"
            run.completed_at = datetime.datetime.utcnow()
            submission.status = "needs_review"
            db.flush()  # so the CriterionMark rows just added have IDs before we read them back
            submission.achieved_grade = compute_overall_grade(
                db.query(CriterionMark).filter(CriterionMark.marking_run_id == run.id).all()
            )

            db.add(AuditLogEntry(
                submission_id=submission.id, actor="ai", action="marked",
                detail=f"{len(result.get('marks', []))} criteria marked.",
            ))

        except Exception as e:
            run.status = "failed"
            run.error_detail = str(e)
            submission.status = "failed"
            db.add(AuditLogEntry(
                submission_id=submission.id, actor="ai", action="marking_failed", detail=str(e),
            ))

        db.commit()
    finally:
        db.close()


@router.get("/submissions/{submission_id}/status")
def submission_status(
    submission_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """Polled by JS on the review page while marking is in progress."""
    submission = db.query(Submission).filter(Submission.id == submission_id).first()
    if not submission:
        return JSONResponse({"error": "not found"}, status_code=404)
    run = submission.marking_run
    return {
        "submission_status": submission.status,
        "run_status": run.status if run else None,
        "error": run.error_detail if run else None,
    }


@router.get("/submissions/{submission_id}/review", response_class=HTMLResponse)
def review_submission(
    submission_id: int,
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    submission = db.query(Submission).filter(Submission.id == submission_id).first()
    marks = []
    if submission and submission.marking_run:
        marks = db.query(CriterionMark).filter(CriterionMark.marking_run_id == submission.marking_run.id).all()
    return templates.TemplateResponse(
        "submission_review.html",
        {"request": request, "teacher": teacher, "submission": submission, "marks": marks},
    )


@router.post("/submissions/{submission_id}/approve")
async def approve_submission(
    submission_id: int,
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    form = await request.form()
    submission = db.query(Submission).filter(Submission.id == submission_id).first()
    if not submission or not submission.marking_run:
        return RedirectResponse(url="/", status_code=303)

    marks = db.query(CriterionMark).filter(CriterionMark.marking_run_id == submission.marking_run.id).all()

    for mark in marks:
        new_mark = form.get(f"final_mark_{mark.id}", mark.ai_mark)
        new_comment = form.get(f"final_comment_{mark.id}", mark.ai_comment)
        changed = (new_mark != mark.ai_mark) or (new_comment != mark.ai_comment)
        mark.final_mark = new_mark
        mark.final_comment = new_comment
        mark.overridden = changed
        if changed:
            db.add(TeacherOverride(
                criterion_mark_id=mark.id, teacher_id=teacher.id,
                ai_mark=mark.ai_mark, final_mark=new_mark,
                comment_diff=f"AI: {mark.ai_comment}\n---\nFinal: {new_comment}",
            ))
    submission.achieved_grade = compute_overall_grade(marks)  # recompute using the teacher's final marks, not the AI's original ones
    db.commit()

    os.makedirs(ANNOTATED_DIR, exist_ok=True)

    # With multiple files, in-place Word comments only make sense for whichever one is a
    # .docx - find it among the uploaded files. Evidence quoted from OTHER files simply
    # won't be found in this one, and falls through to the existing "general feedback"
    # note at the end of the document, same graceful behaviour as before.
    submission_files = db.query(SubmissionFile).filter(SubmissionFile.submission_id == submission.id).all()
    docx_file = next((sf for sf in submission_files if sf.original_filename.lower().endswith(".docx")), None)
    if not docx_file and not submission_files and submission.stored_path and submission.original_filename.lower().endswith(".docx"):
        # Backward compatibility with pre-multi-file submissions
        docx_path, docx_filename = submission.stored_path, submission.original_filename
    elif docx_file:
        docx_path, docx_filename = docx_file.stored_path, docx_file.original_filename
    else:
        docx_path, docx_filename = None, None

    output_name = f"{uuid.uuid4().hex}_MARKED_{docx_filename}" if docx_filename else None
    output_path = os.path.join(ANNOTATED_DIR, output_name) if output_name else None

    annotation_input = [
        {"evidence_text": m.evidence_text, "comment": f"[{m.criterion_ref}] {m.final_comment}"}
        for m in marks
    ]

    unmatched = []
    if docx_path:
        unmatched = annotate_submission(docx_path, annotation_input, output_path)
        submission.annotated_path = output_path
    else:
        db.add(AuditLogEntry(
            submission_id=submission.id, actor=teacher.email, action="annotation_skipped",
            detail="No .docx file among this submission's files - in-place Word comments aren't "
                   "available for pptx/xlsx/pdf; review marks in-app and share feedback manually.",
        ))

    submission.status = "released"
    db.add(AuditLogEntry(
        submission_id=submission.id, actor=teacher.email, action="approved_and_released",
        detail=f"{len(unmatched)} comment(s) could not be anchored and were added as general feedback."
        if unmatched else "All comments anchored successfully.",
    ))
    db.commit()

    return RedirectResponse(url=f"/submissions/{submission_id}/review", status_code=303)


@router.post("/submissions/{submission_id}/unapprove")
def unapprove_submission(
    submission_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """
    Pulls a released submission back into review - e.g. you spot something after approving,
    or the student needs a mark corrected. Keeps all the marks/comments/overrides exactly as
    they were (nothing is deleted), just re-opens the fields for editing and stops treating
    it as final. Re-approving afterward regenerates the annotated document from scratch.
    """
    submission = db.query(Submission).filter(Submission.id == submission_id).first()
    if submission and submission.status == "released":
        submission.status = "needs_review"
        db.add(AuditLogEntry(
            submission_id=submission.id, actor=teacher.email, action="pulled_back_for_review",
            detail="Submission moved back from released to needs_review.",
        ))
        db.commit()
    return RedirectResponse(url=f"/submissions/{submission_id}/review", status_code=303)


@router.get("/courses/{course_id}/similarity-check", response_class=HTMLResponse)
def similarity_check(
    course_id: int,
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """
    Compares submissions for the SAME target unit against each other, to catch students
    copying from one another - grouped per assignment since comparing across genuinely
    different topics isn't meaningful. Not a replacement for a full Turnitin-style
    corpus-wide check (that compares against the whole internet/academic archive, not
    just your own class), but a real, free, fast first line of defence for the specific
    case of students in the same class copying each other.
    """
    course = db.query(Course).filter(Course.id == course_id, Course.teacher_id == teacher.id).first()
    if not course:
        return RedirectResponse(url="/", status_code=303)

    students = db.query(Student).filter(Student.course_id == course_id, Student.is_active == True).all()
    student_ids = [s.id for s in students]
    student_names = {s.id: s.name for s in students}
    all_submissions = db.query(Submission).filter(Submission.student_id.in_(student_ids)).all()

    groups = {}
    for sub in all_submissions:
        key = sub.target_unit_name or "(no unit specified)"
        groups.setdefault(key, []).append(sub)

    report = []
    for unit_name, subs in groups.items():
        if len(subs) < 2:
            continue
        subs_with_text = []
        for sub in subs:
            sub_files = db.query(SubmissionFile).filter(SubmissionFile.submission_id == sub.id).all()
            combined_text = ""
            for sf in sub_files:
                try:
                    combined_text += "\n" + extract_text_with_locations(sf.stored_path, sf.original_filename)
                except Exception:
                    continue
            subs_with_text.append({
                "submission_id": sub.id, "student_name": student_names.get(sub.student_id, "?"),
                "text": combined_text,
            })
        pairs = find_similar_pairs(subs_with_text)
        if pairs:
            report.append({"unit_name": unit_name, "pairs": pairs})

    return templates.TemplateResponse(
        "similarity_check.html",
        {"request": request, "teacher": teacher, "course": course, "report": report},
    )


@router.get("/submissions/{submission_id}/download")
def download_annotated(
    submission_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    submission = db.query(Submission).filter(Submission.id == submission_id).first()
    if not submission or not submission.annotated_path or not os.path.exists(submission.annotated_path):
        return RedirectResponse(url=f"/submissions/{submission_id}/review", status_code=303)
    return FileResponse(
        submission.annotated_path,
        filename=submission.original_filename,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
