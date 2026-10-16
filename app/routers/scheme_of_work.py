import os
import re
import json
import uuid
import shutil
import traceback
import datetime
from .. import blooms_taxonomy
from fastapi import APIRouter, Request, Depends, Form, UploadFile, File, BackgroundTasks
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..spec_context import spec_teaching_context
from ..database import get_db, SessionLocal
from ..models import Teacher, Course, CourseTeacher, SchemeOfWork, LessonDeck, SOWTemplate, ExamExemplar, SpecVersion
from ..course_access import get_course_for_teacher
from ..sow_docx_export import render_sow_docx
from ..sow_docx_fill import fill_sow_template_docx, count_available_weeks_in_range, _parse_append_labels
from ..auth import get_current_teacher
from ..llm_gateway import complete_json, get_effective_provider
from ..docx_annotator import extract_text

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

UPLOAD_DIR = "storage/uploads"


def _get_owned_sow(db: Session, course_id: int, sow_id: int, teacher: Teacher):
    """
    The ONLY correct way to look up a SchemeOfWork in this router. Standing fix for a real,
    serious vulnerability: nearly every route here previously looked up a SchemeOfWork by
    id + course_id alone, with NO check that the requesting teacher actually owned that
    course - meaning any authenticated teacher could view, edit, retry, delete, or replace
    the template on ANY other teacher's scheme of work just by changing the IDs in the URL.
    Confirmed across view/edit/retry/edit-week/delete/replace-template/remove-template/
    use-shared-template - not a one-off slip in a single route, a systemic gap.

    Joins through Course and CourseTeacher and checks course-management membership in the
    SAME query, so it can never be forgotten the way it was before - there is no second
    step to skip. Returns None if the scheme doesn't exist, doesn't belong to this course,
    OR this teacher doesn't manage that course - callers should treat all three
    identically (a clear "not found", never a partial render with someone else's data in
    it).
    """
    return (
        db.query(SchemeOfWork)
        .join(Course, SchemeOfWork.course_id == Course.id)
        .join(CourseTeacher, CourseTeacher.course_id == Course.id)
        .filter(SchemeOfWork.id == sow_id, SchemeOfWork.course_id == course_id, CourseTeacher.teacher_id == teacher.id)
        .first()
    )

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
      "learning_outcomes": ["the exact criterion/LO code(s) as given in the spec below, e.g. '2.1' or 'P2' - not a descriptive label built around it (e.g. NOT 'Unit LO general-2 (2.1)') since a coverage-tracking feature matches on this code directly"],
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
consolidation where the total week count allows for it.

OCCUPATIONAL SPECIALISM ASSESSMENT STRUCTURE - if given below, this describes the REAL exam/
project this content area is building toward (its actual task names/stages, Performance Outcome,
and assessment style - e.g. a synoptic scenario-response project marked on applied skills, not
recall of discrete criteria). Every assignment brief you design (when assignments are requested)
MUST be explicitly framed as PRACTICE for this real structure - name the actual task/stage it's
practising (e.g. "Practising Task 1: Analysing a problem and designing a solution") and write the
brief in the same scenario-response, portfolio-style character as the real assessment, not as a
generic "cover these criteria" checklist. The "criteria_covered" field is still useful for the
teacher's own tracking, but the brief text itself (title/brief) must read like genuine practice for
the named real task, not a knowledge quiz. If NO structure is given below, design assignment briefs
as you would normally.

TEACHER CONTACT TIME - if given below, this is roughly how many hours per week the teacher
actually has with this class. Use it as context for how much can realistically fit in one "week"
of this plan - don't assume a single lesson's worth of time if more hours are stated, and don't
cram a multi-topic week into a single short session if fewer hours are stated."""


LEARNING_OUTCOME_STATEMENT_SYSTEM_PROMPT = """You are writing ONE learning outcome sentence per week \
for an already-built UK further-education scheme of work. This is a narrow, focused task - do not \
build a scheme of work, do not invent weeks, do not change anything about the weeks given to you.

For each week given below (with its title and the exact spec criterion code(s) and description it \
covers), write ONE proper learning outcome sentence using a verb from the Bloom's Taxonomy list below \
- choose the verb that matches the ACTUAL demand of the given criterion text (an "evaluate" criterion \
needs an evaluate-level verb, not "describe" - do not water down a high-demand criterion into a \
low-demand statement, and do not inflate a low-demand one either).

""" + blooms_taxonomy.as_prompt_text() + """

Return ONLY raw JSON (no markdown fences, no commentary) matching exactly this shape:
{
  "statements": [
    {"week": 1, "statement": "string - one learning outcome sentence for this week, e.g. 'Explain the purpose and application of compliance principles in computer forensics'"}
  ]
}

Return exactly one entry per week given to you, using the same week numbers - do not skip any, do not \
add extra ones, do not merge weeks together."""


CONTEXT_IMPACT_SYSTEM_PROMPT = """You are writing a short summary for an already-built UK \
further-education scheme of work, given its finished week-by-week plan and the real spec \
criteria it covers, both below. This is a narrow, focused task - do not build a scheme of \
work, do not change anything about the weeks given to you.

Return ONLY raw JSON (no markdown fences, no commentary) matching exactly this shape:
{
  "context_rationale": "string - 2-4 sentences: why this content is sequenced in this order across the \
scheme (what builds on what, why harder/dependent topics come later) - a real answer to 'Context for \
the unit/project and rationale for the order of topics', not a generic summary of the unit.",
  "intended_impact": "string - 2-3 sentences: what students should be able to DO or BE by the end of \
this scheme that they couldn't before - a real answer to 'Intended impact of the unit/project', \
focused on outcomes for the student, not a restatement of the content covered.",
  "unit_outcomes_summary": "string - 2-4 sentences summarising what a student will be able to DO by \
the end of the whole unit, written as a genuine SUMMARY in your own words - not a list, not one line \
per criterion, not criterion codes or numbering. Read the actual criteria given below and synthesize \
the overall capability they add up to, the way a course handbook would describe a unit in a few \
sentences to someone who has never seen the detailed criteria. This goes in a small header field, so \
brevity matters as much as accuracy - if you find yourself listing more than 3-4 distinct capabilities, \
you are being too granular; group related ones together into a broader statement instead."
}"""


def _generate_learning_outcome_statements(weeks_data: list, units_to_use: list) -> dict:
    """
    Second, narrower AI call - given the already-generated weeks (title + LO codes), asks for ONE
    learning outcome sentence per week. Deliberately kept separate from the main week-generation
    call: confirmed as a real reliability problem when this was all one giant combined call - a
    small local model returned something completely different from what was asked (echoing a raw
    spec criterion back, not a scheme of work at all) rather than a genuine JSON-shape mistake,
    consistent with the instruction set being too much to follow reliably at once. A short,
    single-purpose call is far more achievable for a smaller model than one call asking for many
    different kinds of content in the same response.

    Since this only ENRICHES an already-successful generation, a failure here is caught and logged
    rather than failing the whole scheme - the scheme is still fully usable without these
    statements, exactly as it was before this feature existed. Returns {week_number: statement},
    empty dict on any failure.
    """
    code_to_description = {}
    for unit in units_to_use:
        for lo in unit.get("learning_outcomes", []):
            for crit in lo.get("criteria", []):
                code = crit.get("code")
                if code:
                    code_to_description[code] = crit.get("description", "")

    weeks_summary = []
    for week in weeks_data:
        codes = week.get("learning_outcomes") or []
        criteria_text = "; ".join(f"{c}: {code_to_description.get(c, '')}" for c in codes) or "(no specific criterion code given for this week - use the title as the basis for a sensible statement)"
        weeks_summary.append({"week": week.get("week"), "title": week.get("title", ""), "criteria": criteria_text})

    prompt = "Weeks to write a learning outcome statement for:\n" + json.dumps(weeks_summary, indent=2)
    try:
        result = complete_json(system=LEARNING_OUTCOME_STATEMENT_SYSTEM_PROMPT, prompt=prompt, max_tokens=4000, for_draft_generation=True)
        statements = result.get("statements", [])
        return {s["week"]: s["statement"] for s in statements if s.get("week") is not None and s.get("statement")}
    except Exception as e:
        print(f"[sow] Learning outcome statement generation failed (non-fatal, scheme still usable without them): {e}", flush=True)
        return {}


def _generate_context_and_impact(weeks_data: list, units_to_use: list) -> tuple:
    """
    Third, narrow AI call - given the finished week list and the real spec criteria, writes the
    scheme's "Context for the unit/project...", "Intended impact...", and a genuine unit-outcomes
    SUMMARY (not a list of every criterion - see the prompt's own note on brevity; this was a
    real, confirmed problem: a flat spec structure with many individual criteria and no natural
    higher-level grouping was producing a long, bulky per-criterion dump when shown verbatim,
    which is not what a reader of a short header field needs). Same reliability reasoning as the
    learning-outcome-statement call above: kept separate and small, and a failure here is caught
    and logged rather than failing the whole scheme. Returns (context_rationale, intended_impact,
    unit_outcomes_summary), each None on failure.
    """
    weeks_summary = [{"week": w.get("week"), "title": w.get("title", "")} for w in weeks_data]
    criteria_lines = []
    for unit in units_to_use:
        for lo in unit.get("learning_outcomes", []):
            for crit in lo.get("criteria", []):
                code = crit.get("code", "")
                desc = crit.get("description", "")
                if code or desc:
                    criteria_lines.append(f"{code}: {desc}".strip(": ").strip())
    prompt = (
        "Finished week-by-week plan:\n" + json.dumps(weeks_summary, indent=2)
        + "\n\nReal spec criteria this unit covers (for the unit_outcomes_summary specifically - "
        "synthesize from these, don't just list them back):\n" + "\n".join(criteria_lines)
    )
    try:
        result = complete_json(system=CONTEXT_IMPACT_SYSTEM_PROMPT, prompt=prompt, max_tokens=1500, for_draft_generation=True)
        return (
            result.get("context_rationale") or None,
            result.get("intended_impact") or None,
            result.get("unit_outcomes_summary") or None,
        )
    except Exception as e:
        print(f"[sow] Context/intended impact/outcomes-summary generation failed (non-fatal, scheme still usable without them): {e}", flush=True)
        return None, None, None


@router.get("/courses/{course_id}/scheme-of-work/new", response_class=HTMLResponse)
def new_sow_page(
    course_id: int,
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    course = get_course_for_teacher(db, course_id, teacher)
    has_parsed_spec = bool(course and course.spec_version and course.spec_version.structured_json)
    units = course.spec_version.structured_json.get("units", []) if has_parsed_spec else []
    sow_templates = db.query(SOWTemplate).order_by(SOWTemplate.label).all()
    return templates.TemplateResponse(
        "scheme_of_work_new.html",
        {"request": request, "teacher": teacher, "course": course, "has_parsed_spec": has_parsed_spec,
         "units": units, "max_weeks": SANITY_MAX_WEEKS, "sow_templates": sow_templates},
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
    tutor_names: str = Form(""),
    os_assessment_structure: str = Form(""),
    hours_per_week: str = Form(""),
    append_eligible_labels: str = Form(""),
    sow_template_id: str = Form(""),
    format_sample_file: UploadFile = File(None),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    course = get_course_for_teacher(db, course_id, teacher)
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

    format_sample_text = ""
    format_sample_path = None
    format_sample_filename = None
    selected_sow_template_id = int(sow_template_id) if sow_template_id.strip().isdigit() else None

    if selected_sow_template_id:
        # A shared library template takes priority over a one-off upload in the same
        # request (the form's own help text tells the lecturer this) - copied into this
        # SOW's own storage rather than referencing the shared file directly, so an admin
        # later replacing/removing the shared template never changes what an already-
        # generated scheme downloads (see SchemeOfWork.sow_template_id's docstring).
        sow_template = db.get(SOWTemplate, selected_sow_template_id)
        if sow_template and os.path.exists(sow_template.file_path):
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            copied_name = f"{uuid.uuid4().hex}_{sow_template.original_filename}"
            copied_path = os.path.join(UPLOAD_DIR, copied_name)
            shutil.copy(sow_template.file_path, copied_path)
            format_sample_path = copied_path
            format_sample_filename = sow_template.original_filename
            try:
                format_sample_text = extract_text(copied_path, sow_template.original_filename)
            except Exception:
                format_sample_text = ""
        else:
            selected_sow_template_id = None  # stale/removed reference - fall through to any uploaded file instead

    if not format_sample_path and format_sample_file is not None and format_sample_file.filename:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        stored_name = f"{uuid.uuid4().hex}_{format_sample_file.filename}"
        stored_path = os.path.join(UPLOAD_DIR, stored_name)
        contents = await format_sample_file.read()
        with open(stored_path, "wb") as f:
            f.write(contents)
        try:
            format_sample_text = extract_text(stored_path, format_sample_file.filename)
        except Exception:
            format_sample_text = ""
        # Kept on disk (not just its extracted text) so the "Download" step can fill this
        # ACTUAL file in place later, rather than only using it as an AI style reference -
        # only offered for .docx, since only that format can be edited in place this way.
        if format_sample_file.filename.lower().endswith(".docx"):
            format_sample_path = stored_path
            format_sample_filename = format_sample_file.filename

    parsed_start = None
    parsed_end = None
    computed_weeks = weeks
    date_range_note = None
    if start_date and end_date:
        try:
            parsed_start = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
            parsed_end = datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
            # Standing fix: a raw day-count between two dates assumes every calendar week
            # in between is a free teaching week - not true once a real template is
            # attached (induction weeks, conferences, half terms, Christmas all eat into
            # it). If a .docx template was uploaded in this same request, count its ACTUAL
            # available (blank) weeks in this date range instead - confirmed against a
            # real template where the day-count estimate and the true available count
            # differed enough to make a stated "finish by January" goal silently not
            # actually happen (content spilling to June while the day-count looked fine).
            template_based_weeks = (
                count_available_weeks_in_range(
                    format_sample_path, parsed_start, parsed_end,
                    _parse_append_labels(append_eligible_labels),
                )
                if format_sample_path else None
            )
            if template_based_weeks is not None:
                computed_weeks = max(1, template_based_weeks)
                if template_based_weeks == 0:
                    date_range_note = (
                        f"Your uploaded template has no available (blank) teaching weeks "
                        f"between {parsed_start} and {parsed_end} - every row in that range "
                        f"already has real college content (inductions, breaks, etc). Check "
                        f"the date range or the template."
                    )
            else:
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

    sow = SchemeOfWork(
        course_id=course.id,
        spec_version_id=course.spec_version_id,
        title=title,
        content_area=content_area_label,
        selected_unit_names=selected_unit_names or None,
        format_sample=format_sample_text or None,
        format_sample_file_path=format_sample_path,
        format_sample_original_filename=format_sample_filename,
        sow_template_id=selected_sow_template_id,
        tutor_names=tutor_names.strip() or None,
        os_assessment_structure=os_assessment_structure.strip() or None,
        append_eligible_labels=append_eligible_labels.strip() or None,
        hours_per_week=(float(hours_per_week) if hours_per_week.strip().replace(".", "", 1).isdigit() else None),
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


@router.get("/courses/{course_id}/scheme-of-work/{sow_id}/edit", response_class=HTMLResponse)
def edit_sow_page(
    course_id: int,
    sow_id: int,
    request: Request,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """
    Lets a teacher change this scheme of work's own parameters (dates, week count, notes,
    OS assessment structure, etc.) and regenerate in place - answers "do I have to delete
    and start over to change something": no, this is the alternative. The uploaded
    template (if any) is untouched by this - it's managed separately via Replace/Remove.
    """
    sow = _get_owned_sow(db, course_id, sow_id, teacher)
    if not sow:
        return RedirectResponse(url=f"/courses/{course_id}", status_code=303)
    course = sow.course
    has_parsed_spec = bool(course and course.spec_version and course.spec_version.structured_json)
    units = course.spec_version.structured_json.get("units", []) if has_parsed_spec else []
    return templates.TemplateResponse(
        "scheme_of_work_edit.html",
        {"request": request, "teacher": teacher, "course": course, "sow": sow,
         "units": units, "max_weeks": SANITY_MAX_WEEKS},
    )


@router.post("/courses/{course_id}/scheme-of-work/{sow_id}/edit")
async def update_sow(
    course_id: int,
    sow_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    weeks: int = Form(0),
    start_date: str = Form(""),
    end_date: str = Form(""),
    notes: str = Form(""),
    include_practicals: str = Form(""),
    practicals_notes: str = Form(""),
    design_assignments: str = Form(""),
    tutor_names: str = Form(""),
    os_assessment_structure: str = Form(""),
    hours_per_week: str = Form(""),
    append_eligible_labels: str = Form(""),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    sow = _get_owned_sow(db, course_id, sow_id, teacher)
    if not sow:
        return RedirectResponse(url=f"/courses/{course_id}", status_code=303)

    form = await request.form()
    selected_unit_names = form.getlist("selected_units")
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
            template_based_weeks = (
                count_available_weeks_in_range(
                    sow.format_sample_file_path, parsed_start, parsed_end,
                    _parse_append_labels(append_eligible_labels),
                )
                if sow.format_sample_file_path else None
            )
            if template_based_weeks is not None:
                computed_weeks = max(1, template_based_weeks)
            else:
                days = (parsed_end - parsed_start).days
                computed_weeks = max(1, round(days / 7))
        except ValueError:
            pass
    if computed_weeks < 1:
        computed_weeks = 1
    if computed_weeks > SANITY_MAX_WEEKS:
        date_range_note = f"Capped at the {SANITY_MAX_WEEKS}-week sanity ceiling."
        computed_weeks = SANITY_MAX_WEEKS

    if selected_unit_names:
        sow.selected_unit_names = selected_unit_names
        sow.content_area = ", ".join(selected_unit_names)
    else:
        sow.selected_unit_names = None
        sow.content_area = "whole spec (no units selected)"
    sow.start_date = parsed_start
    sow.end_date = parsed_end
    sow.weeks_requested = computed_weeks
    sow.planned_assessments = planned_assessments or None
    sow.include_practicals = bool(include_practicals)
    sow.practicals_notes = practicals_notes or None
    sow.design_assignments = bool(design_assignments)
    sow.extra_notes = notes or None
    sow.tutor_names = tutor_names.strip() or None
    sow.os_assessment_structure = os_assessment_structure.strip() or None
    sow.append_eligible_labels = append_eligible_labels.strip() or None
    sow.hours_per_week = float(hours_per_week) if hours_per_week.strip().replace(".", "", 1).isdigit() else None
    sow.status = "generating"
    sow.error_detail = date_range_note
    sow.weeks_json = None
    sow.last_download_warnings = None
    db.commit()

    background_tasks.add_task(_run_sow_generation, sow.id)
    return RedirectResponse(url=f"/courses/{course_id}/scheme-of-work/{sow_id}", status_code=303)


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
    sow = _get_owned_sow(db, course_id, sow_id, teacher)
    if sow:
        sow.status = "generating"
        sow.error_detail = None
        sow.weeks_json = None
        db.commit()
        background_tasks.add_task(_run_sow_generation, sow.id)

    return RedirectResponse(url=f"/courses/{course_id}/scheme-of-work/{sow_id}", status_code=303)


@router.post("/courses/{course_id}/scheme-of-work/{sow_id}/weeks/{week_number}")
async def update_sow_week(
    course_id: int,
    sow_id: int,
    week_number: int,
    request: Request,
    title: str = Form(""),
    learning_outcomes: str = Form(""),
    activities: str = Form(""),
    practical: str = Form(""),
    notes: str = Form(""),
    has_assignment: str = Form(""),
    assignment_title: str = Form(""),
    assignment_brief: str = Form(""),
    assignment_criteria_covered: str = Form(""),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """
    Edits ONE week of an already-generated scheme of work in place - e.g. adding a test,
    an extra activity, or a note - without touching any other week or re-running the AI at
    all. Because "Download" and PowerPoint generation both read directly from
    weeks_json every time, this takes effect on the very next download - there's no
    separate "regenerate the document" step needed.
    """
    sow = _get_owned_sow(db, course_id, sow_id, teacher)
    if not sow or not sow.weeks_json:
        return RedirectResponse(url=f"/courses/{course_id}/scheme-of-work/{sow_id}", status_code=303)

    weeks = list(sow.weeks_json)  # copy so SQLAlchemy sees a genuinely new JSON value to persist
    target_index = next((i for i, w in enumerate(weeks) if w.get("week") == week_number), None)
    if target_index is None:
        return RedirectResponse(url=f"/courses/{course_id}/scheme-of-work/{sow_id}", status_code=303)

    updated_week = dict(weeks[target_index])
    updated_week["title"] = title.strip() or updated_week.get("title", "")
    updated_week["learning_outcomes"] = [p.strip() for p in learning_outcomes.split(",") if p.strip()]
    updated_week["activities"] = [line.strip() for line in activities.splitlines() if line.strip()]
    updated_week["practical"] = practical.strip() or None
    updated_week["notes"] = notes.strip() or None
    if has_assignment and assignment_title.strip():
        updated_week["assignment"] = {
            "title": assignment_title.strip(),
            "brief": assignment_brief.strip(),
            "criteria_covered": [p.strip() for p in assignment_criteria_covered.split(",") if p.strip()],
        }
    else:
        updated_week["assignment"] = None

    weeks[target_index] = updated_week
    sow.weeks_json = weeks
    # Editing a week by hand invalidates any stale "last download" warnings from before the
    # edit (e.g. an overflow warning about a week count that's now changed) - clear them
    # rather than show a note that might no longer apply.
    sow.last_download_warnings = None
    db.commit()

    return RedirectResponse(url=f"/courses/{course_id}/scheme-of-work/{sow_id}#week-{week_number}", status_code=303)


@router.post("/courses/{course_id}/scheme-of-work/{sow_id}/delete")
def delete_sow(
    course_id: int,
    sow_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """
    Deletes this scheme of work entirely (its stored parameters, generated weeks, and any
    uploaded template file), so a teacher who wants to start over with different
    parameters isn't stuck editing in place. Any PowerPoint decks previously generated from
    this scheme's weeks are NOT deleted - they're independently useful files - but their
    back-link (source_sow_id) is cleared rather than left pointing at a deleted row.
    """
    sow = _get_owned_sow(db, course_id, sow_id, teacher)
    if sow:
        if sow.format_sample_file_path and os.path.exists(sow.format_sample_file_path):
            os.remove(sow.format_sample_file_path)
        db.query(LessonDeck).filter(LessonDeck.source_sow_id == sow_id).update({"source_sow_id": None})
        db.delete(sow)
        db.commit()

    return RedirectResponse(url=f"/courses/{course_id}", status_code=303)


@router.post("/courses/{course_id}/scheme-of-work/{sow_id}/template/use-shared")
def use_shared_sow_template(
    course_id: int,
    sow_id: int,
    sow_template_id: str = Form(""),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """
    Same idea as replace_sow_template, but pulling from the shared library instead of a
    fresh upload - lets a lecturer switch an already-generated scheme onto the college's
    shared template after the fact, not just at initial generation time. Copies the file
    (see SchemeOfWork.sow_template_id's docstring for why) rather than referencing it
    directly.
    """
    sow = _get_owned_sow(db, course_id, sow_id, teacher)
    template_id = int(sow_template_id) if sow_template_id.strip().isdigit() else None
    if sow and template_id:
        sow_template = db.get(SOWTemplate, template_id)
        if sow_template and os.path.exists(sow_template.file_path):
            if sow.format_sample_file_path and os.path.exists(sow.format_sample_file_path):
                os.remove(sow.format_sample_file_path)
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            copied_name = f"{uuid.uuid4().hex}_{sow_template.original_filename}"
            copied_path = os.path.join(UPLOAD_DIR, copied_name)
            shutil.copy(sow_template.file_path, copied_path)
            sow.format_sample_file_path = copied_path
            sow.format_sample_original_filename = sow_template.original_filename
            sow.sow_template_id = template_id
            try:
                sow.format_sample = extract_text(copied_path, sow_template.original_filename) or sow.format_sample
            except Exception:
                pass
            db.commit()

    return RedirectResponse(url=f"/courses/{course_id}/scheme-of-work/{sow_id}", status_code=303)


@router.post("/courses/{course_id}/scheme-of-work/{sow_id}/template")
async def replace_sow_template(
    course_id: int,
    sow_id: int,
    template_file: UploadFile = File(...),
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """
    Uploads a new template (or replaces the existing one) on an already-generated scheme
    of work, without needing to regenerate the AI content itself - the two are independent:
    this only changes which document "Download" fills into.
    """
    sow = _get_owned_sow(db, course_id, sow_id, teacher)
    if sow and template_file.filename:
        if sow.format_sample_file_path and os.path.exists(sow.format_sample_file_path):
            os.remove(sow.format_sample_file_path)
        sow.sow_template_id = None  # this is now a one-off upload, not the shared template it may have been before
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        stored_name = f"{uuid.uuid4().hex}_{template_file.filename}"
        stored_path = os.path.join(UPLOAD_DIR, stored_name)
        contents = await template_file.read()
        with open(stored_path, "wb") as f:
            f.write(contents)

        if template_file.filename.lower().endswith(".docx"):
            sow.format_sample_file_path = stored_path
            sow.format_sample_original_filename = template_file.filename
        else:
            # Not a .docx - can't be filled in place, so don't set it as the fill target,
            # but it can still be useful as an AI style reference on the next regenerate.
            sow.format_sample_file_path = None
            sow.format_sample_original_filename = None
        try:
            sow.format_sample = extract_text(stored_path, template_file.filename) or sow.format_sample
        except Exception:
            pass
        db.commit()

    return RedirectResponse(url=f"/courses/{course_id}/scheme-of-work/{sow_id}", status_code=303)


@router.post("/courses/{course_id}/scheme-of-work/{sow_id}/template/remove")
def remove_sow_template(
    course_id: int,
    sow_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """Removes the uploaded template without deleting the scheme of work itself -
    "Download" then falls back to the general-shape export until a new template is
    uploaded."""
    sow = _get_owned_sow(db, course_id, sow_id, teacher)
    if sow:
        if sow.format_sample_file_path and os.path.exists(sow.format_sample_file_path):
            os.remove(sow.format_sample_file_path)
        sow.format_sample_file_path = None
        sow.format_sample_original_filename = None
        sow.sow_template_id = None
        db.commit()

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
            if sow.hours_per_week:
                prompt_parts.append(f"TEACHER CONTACT TIME: {sow.hours_per_week} hours per week with this class")
            if sow.os_assessment_structure:
                os_structure_header = (
                    "OCCUPATIONAL SPECIALISM ASSESSMENT STRUCTURE (see the instruction above - "
                    "frame every assignment brief as explicit practice for this real structure):"
                )
                prompt_parts.append(os_structure_header + " " + sow.os_assessment_structure)
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
            # Stage 6: what the spec says about how these units are taught and assessed (exam
            # objectives, quantitative skills, teaching content, unit aims). "" for specs that
            # hold none of this, so their prompt is exactly as before.
            spec_json = course.spec_version.structured_json if course.spec_version else {}
            contexts = []
            for unit in units_to_use:
                context = spec_teaching_context(spec_json, unit)
                if context and context not in contexts:
                    contexts.append(f"For {unit.get('name')}:\n{context}")
            if contexts:
                prompt_parts.append("WHAT THE SPEC SAYS ABOUT TEACHING AND ASSESSING THIS CONTENT (use it to shape weeks, "
                                    "activities and assessment checkpoints):\n" + "\n\n".join(contexts))

            # Exam exemplar matching - closes a real, confirmed gap: the spec covers
            # underpinning content, but a real exam/Occupational Specialism task can
            # require specific skills/deliverables the spec doesn't itemize as its own
            # criterion (a real example given: a Gantt chart requirement for an ESP task
            # that isn't listed in the spec). Matched by exact unit name against whatever
            # was selected for THIS generation - a college that hasn't uploaded one for
            # this particular unit yet still gets a working scheme from the spec alone,
            # just clearly flagged as such rather than silently missing something.
            #
            # unit_names is a JSON list column (one real exemplar can genuinely span
            # several content areas, e.g. a synoptic OS assessment) - SQLite can't
            # reliably .in_() against a JSON array column, so this filters by spec first
            # (a normal, safe SQL filter) then checks for ANY overlap in Python.
            selected_unit_set = set(unit_names)
            matching_exemplars = [
                ex for ex in (
                    db.query(ExamExemplar).filter(ExamExemplar.spec_version_id == sow.spec_version_id).all()
                    if sow.spec_version_id else []
                )
                if set(ex.unit_names or []) & selected_unit_set
            ]
            if matching_exemplars:
                sow.generated_without_exemplar = False
                exemplar_blocks = [
                    f"[Exemplar for unit(s) {', '.join(ex.unit_names or [])}: {ex.label}]\n{(ex.extracted_text or '')[:4000]}"
                    for ex in matching_exemplars
                ]
                prompt_parts.append(
                    "REAL EXAM/OCCUPATIONAL SPECIALISM TASK EXEMPLAR(S) (IMPORTANT: the spec above covers "
                    "underpinning content, but a real assessment task can require specific skills/deliverables "
                    "it doesn't itemize as its own criterion - e.g. a Gantt chart, a specific document format, "
                    "a presentation. Read the exemplar(s) below and identify anything a student would actually "
                    "need to be TAUGHT/PRACTISED to complete this real task that isn't already covered by the "
                    "selected criteria - weave that into the relevant week(s) as genuine content/practice, and "
                    "note in that week's \"notes\" field that it reflects an exam-exemplar requirement, not just "
                    "a spec criterion, so the teacher understands why it's there. Do not invent content that "
                    "isn't actually present in the exemplar text below):\n\n" + "\n\n".join(exemplar_blocks)
                )
            else:
                sow.generated_without_exemplar = True

            prompt = "\n\n".join(prompt_parts)

            result = None
            last_error = None
            for attempt in (1, 2):
                try:
                    # No hard week cap anymore, so a genuinely large request needs real
                    # headroom here - if this still isn't enough for very long schemes,
                    # that's the next thing to raise.
                    result = complete_json(system=SOW_SYSTEM_PROMPT, prompt=prompt, max_tokens=16000, for_draft_generation=True)
                    break
                except Exception as e:
                    last_error = e
                    continue
            if result is None:
                raise last_error
            sow.generated_by_provider = get_effective_provider(for_draft_generation=True)

            weeks_data = result.get("weeks", [])
            if not weeks_data:
                provider_used = get_effective_provider(for_draft_generation=True)
                # Standing fix: this was previously called without for_draft_generation,
                # meaning it could report the WRONG provider in the error - draft
                # generation resolves independently of the everyday/marking provider (that
                # is the whole point of the category split), so omitting the flag here
                # checked a different, possibly different-valued category than the one
                # that actually ran.
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

            # Two further, deliberately SEPARATE and SMALLER calls to enrich the now-confirmed-
            # good week plan - split out specifically because asking for all of this in one
            # giant call was confirmed unreliable on a smaller local model (see the two
            # functions' own docstrings). Each degrades gracefully on its own failure - the
            # core week-by-week plan above is never put at risk by either of these.
            statements_by_week = _generate_learning_outcome_statements(weeks_data, units_to_use)
            for week in weeks_data:
                statement = statements_by_week.get(week.get("week"))
                if statement:
                    week["learning_outcome_statement"] = statement
            sow.context_rationale, sow.intended_impact, sow.unit_outcomes_summary = _generate_context_and_impact(weeks_data, units_to_use)

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
    sow = _get_owned_sow(db, course_id, sow_id, teacher)
    if not sow:
        return RedirectResponse(url=f"/courses/{course_id}", status_code=303)
    course = sow.course

    # Standing diagnostic for a real, reported confusion: the generic "no exam exemplar
    # was available" warning gives no way to tell "none were ever uploaded for this spec"
    # apart from "some exist, but they're tagged for different content area(s) than this
    # SOW covers" - which are very different things to act on. Computed fresh at view
    # time (read-only, no schema change) rather than stored, so it's always accurate to
    # what's actually in Exam Exemplars right now, not a snapshot from generation time.
    exemplar_diagnostic = None
    if sow.generated_without_exemplar and sow.spec_version_id:
        all_spec_exemplars = db.query(ExamExemplar).filter(ExamExemplar.spec_version_id == sow.spec_version_id).all()
        if all_spec_exemplars:
            covered_names = sorted({name for ex in all_spec_exemplars for name in (ex.unit_names or [])})
            exemplar_diagnostic = (
                f"{len(all_spec_exemplars)} exam exemplar(s) exist for this specification, but none are "
                f"tagged for the content area(s) this scheme was generated for ({sow.content_area}). "
                f"Exemplars currently cover: {', '.join(covered_names) if covered_names else 'no content areas'}."
            )

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

    sow_templates = db.query(SOWTemplate).order_by(SOWTemplate.label).all()
    return templates.TemplateResponse(
        "scheme_of_work_detail.html",
        {"request": request, "teacher": teacher, "course": course, "sow": sow, "decks_by_week": decks_by_week,
         "sow_templates": sow_templates, "exemplar_diagnostic": exemplar_diagnostic},
    )


@router.get("/courses/{course_id}/scheme-of-work/{sow_id}/download")
def download_sow(
    course_id: int,
    sow_id: int,
    db: Session = Depends(get_db),
    teacher: Teacher = Depends(get_current_teacher),
):
    """
    If a .docx template was uploaded for this scheme of work, fills it in place (see
    sow_docx_fill.py) - reusing the ALREADY-GENERATED weeks_json exactly as shown in the
    in-app view, just written into the real template's blank teaching weeks instead of a
    generic export. Falls back to the general table-shape export if no template was
    uploaded (or it wasn't a .docx), so a scheme of work without a template keeps working
    exactly as before.
    """
    sow = _get_owned_sow(db, course_id, sow_id, teacher)
    if not sow or sow.status != "ready":
        return RedirectResponse(url=f"/courses/{course_id}/scheme-of-work/{sow_id}", status_code=303)
    course = sow.course

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    output_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex}_sow_export.docx")

    if sow.format_sample_file_path and os.path.exists(sow.format_sample_file_path):
        # Prefer the AI-synthesized summary (genuinely summarized, 2-4 sentences - see
        # _generate_context_and_impact) - confirmed as a real requirement: a flat,
        # ungrouped spec can have a dozen-plus individual criteria, and listing every
        # one (even trimmed) reads as a bulky dump, not a summary. Only fall back to the
        # deterministic per-criterion listing below when no summary exists - an older
        # scheme generated before this existed, or that specific AI call failed (it's a
        # non-fatal enrichment, same as context_rationale/intended_impact).
        if sow.unit_outcomes_summary:
            unit_outcomes_lines = [sow.unit_outcomes_summary]
        else:
            # Deterministic, not AI-guessed - pulled directly from the spec's own parsed
            # learning outcomes for whichever unit(s) this scheme was actually generated for,
            # the same reasoning as course_title/unit_title below. Previously this field was
            # never wired up at all - confirmed as a real, standing gap (the header cell has
            # always existed in the template but nothing ever wrote to it).
            #
            # Real gap found and fixed: for a spec with a FLAT structure (criteria like 2.1,
            # 2.2 with no named grouping heading above them), the parser synthesizes a
            # placeholder group - code "general-N", title "General content (not further
            # sub-grouped in the source document)" - purely so the group has SOMETHING to be
            # keyed by internally. That placeholder text was leaking straight into "Unit
            # outcomes" as if it were a real outcome. A "general-" code is the parser's own
            # marker that this is synthetic, not real spec content - when seen, use the
            # actual criteria underneath instead (which DO have real descriptions), each
            # trimmed to stay a genuine summary rather than the full spec text.
            unit_outcomes_lines = []
            if sow.spec_version_id:
                spec = db.get(SpecVersion, sow.spec_version_id)
                if spec and spec.structured_json:
                    selected = set(sow.selected_unit_names or [])
                    for unit in spec.structured_json.get("units", []):
                        if selected and unit.get("name") not in selected:
                            continue
                        for lo in unit.get("learning_outcomes", []):
                            code = lo.get("code", "").strip()
                            title = lo.get("title", "").strip()
                            if code.startswith("general-"):
                                for criterion in lo.get("criteria", []):
                                    c_code = criterion.get("code", "").strip()
                                    c_desc = criterion.get("description", "").strip()
                                    # First sentence, or a hard cap if there's no clean sentence
                                    # break within a reasonable length - a summary line, not the
                                    # full assessment-criterion text.
                                    cut = c_desc.find(". ")
                                    short_desc = c_desc[:cut + 1] if 0 < cut < 160 else c_desc[:160].rstrip()
                                    if c_code or short_desc:
                                        unit_outcomes_lines.append(f"{c_code}: {short_desc}".strip(": ").strip())
                            elif code or title:
                                unit_outcomes_lines.append(f"{code}: {title}".strip(": ").strip())

        header_updates = {
            "start_date": sow.start_date,
            "end_date": sow.end_date,
            "tutor_names": sow.tutor_names,
            # Deterministic, not AI-guessed - the app already knows the real course and
            # unit this scheme was generated for, so these correct whatever placeholder
            # text the template started with (confirmed as a real, standing problem: a
            # template can arrive with a stale/wrong course title pre-filled in it).
            "course_title": course.name,
            "unit_title": sow.title,
            "unit_outcomes": unit_outcomes_lines or None,
            "context_rationale": sow.context_rationale,
            "intended_impact": sow.intended_impact,
        }
        try:
            report, warnings = fill_sow_template_docx(
                sow.format_sample_file_path, sow.weeks_json or [], header_updates, output_path,
                _parse_append_labels(sow.append_eligible_labels),
            )
            # Surfaced on the next page load rather than failing the download - the file
            # is still real and usable even when there's something worth the teacher's
            # attention (e.g. weeks that didn't fit).
            sow.error_detail = None
            if warnings:
                sow.last_download_warnings = warnings
            db.commit()
        except Exception as e:
            # Template-fill failed outright (e.g. couldn't find a recognisable grid table) -
            # fall back to the general export rather than giving the teacher nothing, but
            # say so clearly rather than silently substituting a different document.
            sow.last_download_warnings = [
                f"Could not fill your uploaded template ({e}) - the document below is the "
                f"general table-shape export instead. Check the template's structure, or "
                f"remove/replace it if this keeps happening."
            ]
            db.commit()
            render_sow_docx(sow, course.name, output_path)
    else:
        render_sow_docx(sow, course.name, output_path)

    return FileResponse(
        output_path,
        filename=f"{sow.title}.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
