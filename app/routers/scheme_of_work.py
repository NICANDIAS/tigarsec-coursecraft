import os
import re
import json
import uuid
import traceback
import datetime
from fastapi import APIRouter, Request, Depends, Form, UploadFile, File, BackgroundTasks
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db, SessionLocal
from ..models import Teacher, Course, SchemeOfWork, LessonDeck
from ..sow_docx_export import render_sow_docx
from ..auth import get_current_teacher
from ..llm_gateway import complete_json, get_effective_provider
from ..docx_annotator import extract_text

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

UPLOAD_DIR = "storage/uploads"

# Not a hard pedagogical limit - some courses genuinely need longer schemes. This is just
# a sanity ceiling against an obviously mistyped number (e.g. "9999"). The actual judgment
# call on how to structure a large request is left to the model itself (see the prompt),
# which can legitimately return fewer weeks than asked if that's the better call, or go
# deep/broad as appropriate - it's told to do that explicitly, not capped from outside.
SANITY_MAX_WEEKS = 104  # two years' worth, as an outer bound

def _looks_like_filled_example(text: str, min_filled_rows: int = 3) -> bool:
    """
    Detects whether a format-sample document already contains real, specific content
    (dates, filled lesson topics) rather than being a blank template with just headers -
    the risky case where the AI might get confused between "match this style" and "copy
    or continue from this actual content", especially when the sample happens to be from a
    related/overlapping subject. Looks inside [TABLE]...[/TABLE] blocks for data rows where
    multiple cells contain substantial text - a genuinely blank template's rows would mostly
    be empty or just short labels.
    """
    filled_row_count = 0
    for table_match in re.finditer(r'\[TABLE\](.*?)\[/TABLE\]', text, re.DOTALL):
        rows = table_match.group(1).strip().split("\n")
        for row in rows[1:]:  # skip header row
            cells = [c.strip() for c in row.split(" | ")]
            substantial_cells = [c for c in cells if len(c) > 25]
            if len(substantial_cells) >= 2:
                filled_row_count += 1
    return filled_row_count >= min_filled_rows


SOW_SYSTEM_PROMPT = """You are helping a UK further-education lecturer build a scheme of work \
(week-by-week teaching plan) from a course specification's structured units/learning outcomes.

CRITICAL - what you are building is a CALENDAR of teaching weeks, NOT an outline or reorganisation \
of the source content. Do NOT group the criteria into thematic sections/topics and return that \
grouping as your answer - that is a different task and the wrong output. Every single entry in your \
response must represent ONE ACTUAL WEEK of teaching (week 1, week 2, week 3...), each with its own \
lesson title and its own slice of the content - not a topic-by-topic breakdown of the specification.
If you find yourself producing keys like "sections", "topics", or "items" instead of "weeks" with \
sequential week numbers, you have done the wrong task - stop and rebuild the response as a week-by-
week calendar instead.

Return ONLY raw JSON (no markdown fences, no commentary) matching exactly this shape:
{
  "weeks": [
    {
      "week": 1,
      "title": "string - short lesson/topic title",
      "learning_outcomes": ["LO codes covered this week, e.g. Unit 3 LO1"],
      "activities": ["short bullet strings describing planned activities"],
      "practical": "string - a hands-on practical exercise for this week, or empty string if none requested/appropriate this week",
      "assignment": {
        "title": "string - assignment brief title",
        "brief": "string - 2-4 sentence assignment brief for students",
        "criteria_covered": ["criterion codes this assignment evidences, e.g. P2, M1"]
      },
      "notes": "string - any teaching notes, optional, can be empty string"
    }
  ]
}

If NO assignment is appropriate for a given week, set "assignment" to null rather than inventing one -
assignments should map to weeks where the spec's criteria are actually ready to be assessed, not every week.

IMPORTANT - if the selected content is narrow relative to the number of weeks requested: do NOT
return an empty "weeks" array, and do NOT invent unrelated content to pad it out. Instead, go into
GREATER DEPTH on the material that does exist - more practice, more practicals, more consolidation/
review weeks, revisiting earlier learning outcomes from different angles - so the requested week
count is still filled with genuinely useful, on-topic teaching rather than filler. Only return fewer
weeks than requested if going into further depth genuinely would not add value, and even then,
return however many weeks of real content you can, never an empty list.

IMPORTANT - if the selected content is LARGE (many learning outcomes/criteria) relative to the weeks
requested: prioritise and group related criteria together into sensible weekly chunks rather than
trying to cram everything - it's fine to focus on the most important/foundational material and note
in a week's "notes" field that related criteria continue in a later week, rather than attempting
shallow coverage of everything at once or returning nothing because there's "too much" to plan.

The requested week count is a TARGET informed by the teacher's actual term dates, not a rigid
requirement - you are trusted to make the pedagogically sound call. If the content genuinely needs
more weeks than requested to teach properly without rushing, say so explicitly in the final week's
"notes" field and provide as many weeks as the content actually needs (even if that's more than
asked). If it needs fewer, say so and provide fewer. Never return an empty list, and never pad with
irrelevant content just to hit a number - a scheme of work that's honest about the mismatch is far
more useful to a real teacher than one that's the "right" length but wrong in substance.

Only build the scheme of work from the SELECTED units/learning outcomes given below - do not cover
unselected content. Distribute the selected learning outcomes sensibly across the number of weeks
given. If practicals were requested, include one most weeks where it makes sense for hands-on
learning (not necessarily every single week if the topic doesn't suit it) - leave "practical" as an
empty string for weeks where a practical wouldn't add value.

ASSESSMENT CHECKPOINTS - if the teacher has specified fixed weeks where assessments must happen
(given below as "Planned assessment checkpoints"), these are NOT optional suggestions - they are
fixed points from the college calendar that the plan MUST be built around. Pace the content so that
by each specified week, enough of the relevant material has genuinely been taught for that
assessment to be meaningful, and place the assignment for that checkpoint at EXACTLY that week
(using the given label as its title/theme) - only drift from the exact week if honouring it would
force genuinely incoherent content grouping, and even then only by the smallest amount necessary. If
NO checkpoints are given, decide assessment placement freely as normal (only if assignment design
was requested) - the scheme of work works fine either way, checkpoints are an enhancement, not a
requirement.

If a college scheme-of-work FORMAT SAMPLE is given,
match its general structure/level of detail/tone as closely as the JSON shape above allows - treat
it as a style guide for how much detail per week, not as literal content to copy. Build up
foundational content before assessment-heavy weeks. Leave the final 1-2 weeks for review/
consolidation where the total week count allows for it."""


@router.get("/courses/{course_id}/scheme-of-work/new", response_class=HTMLResponse)
def new_sow_page(
    course_id: int,
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    course = db.query(Course).filter(Course.id == course_id, Course.teacher_id == teacher.id).first()
    has_parsed_spec = bool(course and course.spec_version and course.spec_version.structured_json)
    units = course.spec_version.structured_json.get("units", []) if has_parsed_spec else []
    return templates.TemplateResponse(
        "scheme_of_work_new.html",
        {"request": request, "teacher": teacher, "course": course, "has_parsed_spec": has_parsed_spec,
         "units": units, "max_weeks": SANITY_MAX_WEEKS},
    )


@router.post("/courses/{course_id}/scheme-of-work/generate")
async def generate_sow(
    course_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    title: str = Form(...),
    weeks: int = Form(0),
    start_date: str = Form(""),
    end_date: str = Form(""),
    notes: str = Form(""),
    include_practicals: str = Form(""),
    practicals_notes: str = Form(""),
    design_assignments: str = Form(""),
    format_sample_file: UploadFile = File(None),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    course = db.query(Course).filter(Course.id == course_id, Course.teacher_id == teacher.id).first()
    if not course or not course.spec_version or not course.spec_version.structured_json:
        return RedirectResponse(url=f"/courses/{course_id}", status_code=303)

    form = await request.form()
    selected_unit_names = form.getlist("selected_units")
    content_area_label = ", ".join(selected_unit_names) if selected_unit_names else "whole spec (no units selected)"

    # Assessment checkpoints - optional. Pairs up the parallel week/label fields, skipping
    # any row where either half wasn't filled in. If any checkpoint is given, that implies
    # the teacher wants assignments there even if they forgot to also tick the general
    # "design assignments" box - don't make them remember to tick two things for one intent.
    checkpoint_weeks = form.getlist("assessment_week")
    checkpoint_labels = form.getlist("assessment_label")
    planned_assessments = [
        {"week": int(week), "label": label.strip()}
        for week, label in zip(checkpoint_weeks, checkpoint_labels)
        if week.strip().isdigit() and label.strip()
    ]
    if planned_assessments:
        design_assignments = "yes"

    parsed_start = None
    parsed_end = None
    computed_weeks = weeks
    date_range_note = None
    if start_date and end_date:
        try:
            parsed_start = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
            parsed_end = datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
            days = (parsed_end - parsed_start).days
            computed_weeks = max(1, round(days / 7))
        except ValueError:
            pass

    if computed_weeks < 1:
        computed_weeks = 1
    if computed_weeks > SANITY_MAX_WEEKS:
        date_range_note = (
            f"Your date range/week count implied {computed_weeks} weeks - that's beyond the "
            f"{SANITY_MAX_WEEKS}-week sanity ceiling (two years), so it's been capped there. If you "
            f"genuinely need longer than that, let me know and I'll raise it further."
        )
        computed_weeks = SANITY_MAX_WEEKS

    format_sample_text = ""
    if format_sample_file is not None and format_sample_file.filename:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        temp_name = f"{uuid.uuid4().hex}_{format_sample_file.filename}"
        temp_path = os.path.join(UPLOAD_DIR, temp_name)
        contents = await format_sample_file.read()
        with open(temp_path, "wb") as f:
            f.write(contents)
        try:
            format_sample_text = extract_text(temp_path, format_sample_file.filename)
        except Exception:
            format_sample_text = ""

    sow = SchemeOfWork(
        course_id=course.id,
        spec_version_id=course.spec_version_id,
        title=title,
        content_area=content_area_label,
        selected_unit_names=selected_unit_names or None,
        format_sample=format_sample_text or None,
        start_date=parsed_start,
        end_date=parsed_end,
        weeks_requested=computed_weeks,
        planned_assessments=planned_assessments or None,
        include_practicals=bool(include_practicals),
        practicals_notes=practicals_notes or None,
        design_assignments=bool(design_assignments),
        extra_notes=notes or None,
        status="generating",
        error_detail=date_range_note,  # shown as a heads-up note even before generation starts
    )
    db.add(sow)
    db.commit()
    db.refresh(sow)

    background_tasks.add_task(_run_sow_generation, sow.id)

    return RedirectResponse(url=f"/courses/{course_id}/scheme-of-work/{sow.id}", status_code=303)


@router.post("/courses/{course_id}/scheme-of-work/{sow_id}/retry")
def retry_sow(
    course_id: int,
    sow_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """
    Re-runs generation on the SAME row using its already-stored parameters, instead of
    requiring a brand new scheme of work each time - this is what stops the list from
    accumulating a new entry per failed attempt.
    """
    sow = db.query(SchemeOfWork).filter(SchemeOfWork.id == sow_id, SchemeOfWork.course_id == course_id).first()
    if sow:
        sow.status = "generating"
        sow.error_detail = None
        sow.weeks_json = None
        db.commit()
        background_tasks.add_task(_run_sow_generation, sow.id)

    return RedirectResponse(url=f"/courses/{course_id}/scheme-of-work/{sow_id}", status_code=303)


def _unit_content_and_stats(course, selected_unit_names):
    """Rebuilds the same scoped unit list used at generation time, from the course's
    CURRENT spec structure - used by both the original generation and retry."""
    all_units = course.spec_version.structured_json.get("units", []) if course.spec_version and course.spec_version.structured_json else []
    units_to_use = (
        [u for u in all_units if u.get("name") in selected_unit_names]
        if selected_unit_names else all_units
    )
    total_criteria = sum(
        len(lo.get("criteria", [])) for u in units_to_use for lo in u.get("learning_outcomes", [])
    )
    return units_to_use, total_criteria


def _run_sow_generation(sow_id: int):
    """Runs in the background so a slow/failed LLM call never surfaces as a raw server
    error - same pattern as marking, spec parsing, and presentation generation. Reads
    everything it needs from the stored SchemeOfWork row, so both the original generation
    and a later retry go through this exact same path."""
    db = SessionLocal()
    try:
        sow = db.get(SchemeOfWork, sow_id)
        if not sow:
            return
        course = db.query(Course).filter(Course.id == sow.course_id).first()

        try:
            units_to_use, total_criteria = _unit_content_and_stats(course, sow.selected_unit_names or [])
            unit_names = [u.get("name", "?") for u in units_to_use]

            if not units_to_use or total_criteria == 0:
                raise RuntimeError(
                    f"No usable criteria were found in the selected content area(s) "
                    f"({', '.join(unit_names) or 'none'}) - {len(units_to_use)} unit(s) matched, "
                    f"{total_criteria} total criteria. This means the selection sent to the AI was "
                    f"empty, not that the AI failed - check the spec's structured data for this "
                    f"content area on its page in the Spec Library."
                )

            prompt_parts = [
                f"Number of weeks: {sow.weeks_requested}",
                f"Selected content area(s): {sow.content_area}",
                f"Include practical activities: {'yes' if sow.include_practicals else 'no'}"
                + (f" - notes on what kind: {sow.practicals_notes}" if sow.include_practicals and sow.practicals_notes else ""),
                f"Design assignment briefs where appropriate: {'yes' if sow.design_assignments else 'no'}",
                f"Extra teacher notes/preferences: {sow.extra_notes or 'none'}",
            ]
            if sow.planned_assessments:
                checkpoints_text = "; ".join(f"Week {a['week']}: {a['label']}" for a in sow.planned_assessments)
                prompt_parts.append(
                    f"Planned assessment checkpoints (fixed - see the ASSESSMENT CHECKPOINTS "
                    f"instruction above): {checkpoints_text}"
                )
            if sow.format_sample:
                if _looks_like_filled_example(sow.format_sample):
                    prompt_parts.append(
                        f"College scheme-of-work FORMAT SAMPLE (IMPORTANT: this sample already contains "
                        f"REAL, SPECIFIC content - actual dates, actual lesson topics, actual assignments - "
                        f"it is NOT a blank template. It is very likely for a DIFFERENT unit/topic/year than "
                        f"what you are building now. Use it STRICTLY as a reference for structure, columns, "
                        f"level of detail per week, and tone. Do NOT reuse its specific topics, wording, "
                        f"dates, or examples under any circumstances - every week you produce must be built "
                        f"entirely from the SELECTED content given below, not from this sample):\n{sow.format_sample[:4000]}"
                    )
                else:
                    prompt_parts.append(f"College scheme-of-work FORMAT SAMPLE to match the style of:\n{sow.format_sample[:4000]}")
            prompt_parts.append(f"Selected units/LOs/criteria to build the scheme from:\n{json.dumps({'units': units_to_use}, indent=2)}")
            prompt = "\n\n".join(prompt_parts)

            result = None
            last_error = None
            for attempt in (1, 2):
                try:
                    # No hard week cap anymore, so a genuinely large request needs real
                    # headroom here - if this still isn't enough for very long schemes,
                    # that's the next thing to raise.
                    result = complete_json(system=SOW_SYSTEM_PROMPT, prompt=prompt, max_tokens=16000)
                    break
                except Exception as e:
                    last_error = e
                    continue
            if result is None:
                raise last_error

            weeks_data = result.get("weeks", [])
            if not weeks_data:
                provider_used = get_effective_provider()
                # Distinguish the two genuinely different failure modes we've now seen:
                # the model either didn't use the "weeks" key at all (wrong schema/task
                # entirely - it built an outline instead of a calendar), or it used the
                # right key but returned nothing in it (different problem). Knowing which
                # one happened is the difference between "the prompt needs to be more
                # forceful" and "something else is wrong" - no more guessing between them.
                # Also states which provider actually handled this call - a run of
                # increasingly malformed responses is a classic sign of a small local model
                # struggling, which should be visible as a fact, not something to take on trust.
                if "weeks" not in result:
                    raise RuntimeError(
                        f"[Provider used: {provider_used}] The AI's response didn't use the expected "
                        f"'weeks' key at all - it returned a different structure entirely (top-level "
                        f"keys: {list(result.keys())}), which means it did the wrong task (e.g. "
                        f"outlining the content instead of building a weekly calendar) rather than "
                        f"failing to follow it correctly. If provider is 'ollama', this is very likely "
                        f"a model-capability issue, not a prompt issue - switch LLM_PROVIDER=anthropic "
                        f"in .env and rebuild. Raw model response: {json.dumps(result)[:1500]}"
                    )
                raise RuntimeError(
                    f"[Provider used: {provider_used}] The AI used the correct 'weeks' key but returned "
                    f"it empty, despite {total_criteria} criteria across {len(units_to_use)} unit(s) "
                    f"being sent ({', '.join(unit_names)}) for {sow.weeks_requested} weeks. "
                    f"Raw model response: {json.dumps(result)[:1500]}"
                )

            sow.weeks_json = weeks_data
            sow.status = "ready"
            # Keep the date-range-capped note if there was one, don't overwrite it with None
            if sow.error_detail and "capped at" not in sow.error_detail:
                sow.error_detail = None
        except Exception:
            sow.status = "failed"
            sow.error_detail = traceback.format_exc()
        db.commit()
    finally:
        db.close()


@router.get("/courses/{course_id}/scheme-of-work/{sow_id}/status")
def sow_status(
    course_id: int,
    sow_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    sow = db.get(SchemeOfWork, sow_id)
    if not sow:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"status": sow.status, "error_detail": sow.error_detail}


@router.get("/courses/{course_id}/scheme-of-work/{sow_id}", response_class=HTMLResponse)
def view_sow(
    course_id: int,
    sow_id: int,
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    course = db.query(Course).filter(Course.id == course_id, Course.teacher_id == teacher.id).first()
    sow = db.query(SchemeOfWork).filter(SchemeOfWork.id == sow_id, SchemeOfWork.course_id == course_id).first()

    # Most recent deck generated for each week - if a week was regenerated more than once,
    # link to the newest attempt rather than an old one.
    decks_by_week = {}
    if sow:
        decks = (
            db.query(LessonDeck)
            .filter(LessonDeck.source_sow_id == sow_id)
            .order_by(LessonDeck.id.desc())
            .all()
        )
        for deck in decks:
            if deck.source_week is not None and deck.source_week not in decks_by_week:
                decks_by_week[deck.source_week] = deck

    return templates.TemplateResponse(
        "scheme_of_work_detail.html",
        {"request": request, "teacher": teacher, "course": course, "sow": sow, "decks_by_week": decks_by_week},
    )


@router.get("/courses/{course_id}/scheme-of-work/{sow_id}/download")
def download_sow(
    course_id: int,
    sow_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """
    Generated fresh from weeks_json on each request rather than stored - the SOW view
    itself stays exactly as it is (per your request), this just adds a real downloadable
    document alongside it in a general table shape close to what most college SOW
    templates use (not an exact replica of any specific one).
    """
    course = db.query(Course).filter(Course.id == course_id, Course.teacher_id == teacher.id).first()
    sow = db.query(SchemeOfWork).filter(SchemeOfWork.id == sow_id, SchemeOfWork.course_id == course_id).first()
    if not sow or sow.status != "ready" or not course:
        return RedirectResponse(url=f"/courses/{course_id}/scheme-of-work/{sow_id}", status_code=303)

    output_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex}_sow_export.docx")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    render_sow_docx(sow, course.name, output_path)

    return FileResponse(
        output_path,
        filename=f"{sow.title}.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
