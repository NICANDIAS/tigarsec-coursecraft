import datetime
from sqlalchemy import (
    Column, Integer, String, Text, ForeignKey, DateTime, Date, Float, Boolean, JSON
)
from sqlalchemy.orm import relationship
from .database import Base


class MarkingTemplate(Base):
    """
    A saved rubric/marking scheme reusable across every student's submission for the same
    assignment, so the teacher doesn't have to re-paste or re-upload it every single time -
    select it once per upload instead.
    """
    __tablename__ = "marking_templates"
    id = Column(Integer, primary_key=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    name = Column(String(255), nullable=False)
    marking_scheme_notes = Column(Text, nullable=True)
    assignment_brief_text = Column(Text, nullable=True)  # the actual task set to students for this assignment - lets the AI tell "the brief never asked for this part of the criterion" apart from "the student missed it", reused across every submission for the same assignment
    pass_threshold_pct = Column(Float, nullable=True)
    part_weightings = Column(JSON, nullable=True)  # e.g. {"MCQ": 45, "Longform": 55} - marker-supplied weighting of each assessment part toward the weighted overall %, reused on every submission that selects this template. Never assumed/hardcoded - null means "not specified", and the app falls back to an explicitly-flagged equal weighting rather than guessing a split.
    rubric_file_path = Column(String(1000), nullable=True)  # the ACTUAL uploaded rubric file - filled in place, never rebuilt as a new document
    created_by_teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class FeedbackStyleTemplate(Base):
    """
    Lets teachers add new feedback-comment styles (beyond the two built-in ones) without
    needing a code change - e.g. a college-specific format. Shared across everyone, same
    pattern as the spec library / media library.
    """
    __tablename__ = "feedback_style_templates"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False, unique=True)
    instructions = Column(Text, nullable=False)  # fed directly into the marking prompt
    created_by_teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Teacher(Base):
    __tablename__ = "teachers"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    style_profile = Column(Text, nullable=True)  # pasted feedback exemplars, used in marking prompts
    feedback_style = Column(String(50), default="standard", nullable=False)  # "standard" | "wwebi"
    role = Column(String(50), default="lecturer", nullable=False)
    is_primary_admin = Column(Boolean, default=False, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    two_factor_enabled = Column(Boolean, default=False, nullable=False)  # personal opt-in from Settings; can also be forced on for everyone via SystemSettings.two_factor_mandatory
    early_access_features = Column(JSON, nullable=True)  # list of feature keys this specific person has been granted access to while still "Coming Soon" for everyone else - deliberately per-person, not per-role, since the point is inviting a specific reviewer in (e.g. a curriculum manager checking progress on something being built), not opening it to a whole role
    contracted_hours_per_week = Column(Float, nullable=True)  # timetabling hard ceiling - a session can never push someone past this
    non_teaching_days = Column(JSON, nullable=True)  # list of weekday names (e.g. ["Wednesday"]) - a recurring, fixed pattern, distinct from one-off StaffUnavailability entries below
    is_dummy = Column(Boolean, nullable=False, default=False)  # bulk-generated test data for exercising the timetable generator at scale - flagged explicitly rather than identified by name, so a "clear all dummy data" action can never risk touching a real teacher
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    courses = relationship("Course", back_populates="teacher")


class CourseTeacher(Base):
    """
    Which teachers manage which course - many-to-many, replacing single-owner
    Course.teacher_id as the real access-control mechanism. Confirmed as a real,
    necessary change: two teachers covering different content areas of the same real
    cohort (e.g. one teaching Year 1's ESP component, another Year 2's OS component) were
    each creating their OWN separate "Course" record for what is conceptually the same
    programme, since a course could only ever have one owner - splitting students,
    schemes of work, and progress tracking across disconnected copies of the same cohort.

    No role/tier distinction (e.g. owner vs contributor) - confirmed as a deliberate
    choice: every teacher assigned to a course has equal access to it, and a teacher not
    assigned has none at all, full stop, including admins with no membership row of their
    own - this matches the strict ownership-based access already used everywhere else in
    the app (submissions, students, schemes of work all currently check exact ownership
    with no admin-override case), so this preserves that same model rather than quietly
    introducing a broader one.

    Course.teacher_id itself is left in place, unchanged, rather than migrated away from -
    altering an existing NOT NULL column's meaning or nullability is a real schema-
    migration risk this doesn't need to take on. It now means "who the record was
    originally created by" for audit/history purposes only; every real access check goes
    through this table instead. See the startup backfill in main.py, which gives every
    pre-existing course's original teacher_id a matching row here automatically, so
    nobody loses access to their own course the moment this ships.
    """
    __tablename__ = "course_teachers"
    id = Column(Integer, primary_key=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    course = relationship("Course")
    teacher = relationship("Teacher")


class Course(Base):
    __tablename__ = "courses"
    id = Column(Integer, primary_key=True)
    # No longer the access-control source (see CourseTeacher below) - kept as a plain audit
    # trail of who originally registered this course record. Column is unchanged from
    # before, so this is a pure meaning change, not a schema change - no risk to existing
    # data. Set to the admin/curriculum_manager who creates the course going forward.
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    name = Column(String(255), nullable=False)
    # A course attaches to ONE spec from the shared library at a time (can be changed).
    # This replaces v1's per-course spec upload - specs are now uploaded once, centrally,
    # and any teacher selects from the library rather than re-uploading the same document.
    spec_version_id = Column(Integer, ForeignKey("spec_versions.id"), nullable=True)
    is_dummy = Column(Boolean, nullable=False, default=False)  # same reasoning as Teacher.is_dummy - lets a dummy course, and everything scoped under it (its requirements, generated sessions), be cleared completely and safely
    start_date = Column(Date, nullable=True)  # when teaching actually begins - distinct from created_at (when the record was set up in the system). Needed to map a Scheme of Work's week numbers to real calendar dates, so "taught" can reflect what's actually happened, not just what's been planned
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    teacher = relationship("Teacher", back_populates="courses")
    spec_version = relationship("SpecVersion")
    students = relationship("Student", back_populates="course")
    schemes_of_work = relationship("SchemeOfWork", back_populates="course", order_by="SchemeOfWork.id.desc()")


class SpecVersion(Base):
    """
    Now a shared, institution-wide library entry rather than something each teacher
    uploads per-course (item 11: only admin/curriculum_manager can upload; everyone
    else picks from this list via a dropdown on their course).
    """
    __tablename__ = "spec_versions"
    id = Column(Integer, primary_key=True)
    uploaded_by_teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    label = Column(String(255), nullable=False)
    raw_text = Column(Text, nullable=False)
    structured_json = Column(JSON, nullable=True)
    parse_status = Column(String(50), default="not_parsed", nullable=False)
    # not_parsed | parsing | parsed | failed | cancelled
    parse_error = Column(Text, nullable=True)
    parse_progress = Column(String(255), nullable=True)
    parse_progress_updated_at = Column(DateTime, nullable=True)  # heartbeat - if this stops moving, it's actually stuck, not just slow
    cancel_requested = Column(Boolean, default=False, nullable=False)
    generated_by_provider = Column(String(50), nullable=True)  # which AI provider actually parsed this - spec parsing has its own optional override slot (Admin: AI Provider), separate from marking/drafts
    # Stage 1 (Oct 2026) - all nullable, so every spec that existed before keeps working exactly as before.
    parse_quality = Column(JSON, nullable=True)  # plain-English honesty report from spec_parser.assess_parse_quality: verdict good/needs_review/failed, counts, problems. Kept OUT of structured_json on purpose - marking and IV send structured_json to the AI, and this report must not leak into those prompts
    reparsed_from_id = Column(Integer, ForeignKey("spec_versions.id"), nullable=True)  # set when "Re-parse" was pressed on a spec already IN USE: the re-parse becomes this NEW version and the original is left untouched, so nothing linked to it (courses, schemes of work, marks, exemplars, timetabling) can break. Null = an original upload
    source_file_path = Column(String(1000), nullable=True)  # the uploaded PDF/DOCX kept on disk - future spec readers (Media grade grids, A level tables) need the real page layout, not just the extracted text. Null for specs uploaded before this existed, or pasted as text
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class SchemeOfWork(Base):
    __tablename__ = "schemes_of_work"
    id = Column(Integer, primary_key=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    spec_version_id = Column(Integer, ForeignKey("spec_versions.id"), nullable=True)
    title = Column(String(255), nullable=False)
    content_area = Column(Text, nullable=True)
    selected_unit_names = Column(JSON, nullable=True)  # stored so "Retry" can rebuild the same request
    format_sample = Column(Text, nullable=True)  # extracted text, used only as an AI style-reference (unchanged behaviour)
    format_sample_file_path = Column(String(1000), nullable=True)  # the ACTUAL uploaded template file, kept on disk so it can be filled in place - separate from the extracted text above, and independently replaceable/removable
    format_sample_original_filename = Column(String(500), nullable=True)  # for display, and to check the extension is .docx before offering template-fill
    sow_template_id = Column(Integer, ForeignKey("sow_templates.id"), nullable=True)  # which SHARED library template (if any) was used - the actual file gets copied into format_sample_file_path above at generation time, so an admin later replacing/removing the shared template never changes what an already-generated scheme downloads; this FK is purely for showing "generated using: [template name]" and is not itself read when filling
    generated_without_exemplar = Column(Boolean, default=False, nullable=False)  # True if none of the selected units had a matching ExamExemplar at generation time - shown as a clear notice so the lecturer knows exam-specific requirements beyond the spec (e.g. a deliverable the spec doesn't itemize) may not be captured. Recomputed fresh on every regeneration, so adding an exemplar later and regenerating clears this automatically.
    tutor_names = Column(String(500), nullable=True)  # optional - if given, infilled into a template's "Tutor(s)" field; never guessed if left blank
    os_assessment_structure = Column(Text, nullable=True)  # teacher-supplied description of the REAL occupational specialism exam/project's task structure (e.g. "Task 1: Analysing a problem...") for THIS content area - assignment briefs are framed around this exact structure rather than a generic invented one. Per-scheme-of-work, since each content area maps to a different Performance Outcome.
    hours_per_week = Column(Float, nullable=True)  # teacher-supplied contact hours per week with this class - context for how much material can genuinely fit per week, not a hard constraint
    append_eligible_labels = Column(Text, nullable=True)  # comma-separated labels (e.g. "Discover, Understand, Develop") that the teacher has confirmed are STILL real teaching weeks even though the template already has a short induction/pastoral label in them - content is appended under the existing text rather than the row being skipped. Deliberately never auto-detected - only the teacher reliably knows which of their own calendar's existing labels mean "still teach something this week" vs a genuine closure.
    last_download_warnings = Column(JSON, nullable=True)  # non-fatal notes from the most recent template-fill download (e.g. weeks that didn't fit, rows left blank) - shown on the detail page since a GET download can't otherwise surface them to the teacher
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    weeks_requested = Column(Integer, nullable=True)  # the actual (capped) week count used
    planned_assessments = Column(JSON, nullable=True)  # teacher-specified checkpoints: [{"week": int, "label": str}, ...] - optional, generation still works fine without any
    include_practicals = Column(Boolean, default=False, nullable=False)
    practicals_notes = Column(Text, nullable=True)
    design_assignments = Column(Boolean, default=False, nullable=False)
    extra_notes = Column(Text, nullable=True)
    weeks_json = Column(JSON, nullable=True)
    week_anchor_json = Column(JSON, nullable=True)
    pace_json = Column(JSON, nullable=True)  # teaching at the class's pace: weeks carried on into the next, weeks combined (app/pace.py)  # the lecturer's "this is week N" correction: {"week": N, "monday": "YYYY-MM-DD"} - counted on from there
    context_rationale = Column(Text, nullable=True)  # AI-generated answer to the template's own "Context for the unit/project and rationale for the order of topics" question - generated once, alongside the week plan, not re-computed on every download
    intended_impact = Column(Text, nullable=True)  # AI-generated answer to the template's own "Intended impact of the unit/project" question
    unit_outcomes_summary = Column(Text, nullable=True)  # AI-synthesized 2-4 sentence summary of what a student can do by the end of the unit - deliberately a genuine summary, not a per-criterion list (a flat, ungrouped spec can have a dozen+ individual criteria, which read as a bulky dump if shown verbatim). Falls back to the deterministic per-criterion listing at download time if this is None (older schemes generated before this existed, or the AI call failed - see the download route)
    status = Column(String(50), default="generating", nullable=False)  # generating, ready, failed
    error_detail = Column(Text, nullable=True)
    generated_by_provider = Column(String(50), nullable=True)  # which AI provider actually generated this - drafts default to Ollama unless overridden, so this is the only way to confirm which one actually ran for a given scheme
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    course = relationship("Course", back_populates="schemes_of_work")


class Student(Base):
    __tablename__ = "students"
    id = Column(Integer, primary_key=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    name = Column(String(255), nullable=False)
    student_ref = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    course = relationship("Course", back_populates="students")
    submissions = relationship("Submission", back_populates="student", order_by="Submission.id.desc()")


class SubmissionFile(Base):
    """
    One of potentially several files making up a single submission - e.g. a report (.docx),
    a presentation (.pptx), a spreadsheet (.xlsx), and a PDF export all for the same LO.
    Marking draws evidence from ALL of a submission's files, and records which file (and
    where within it) each criterion's evidence came from.
    """
    __tablename__ = "submission_files"
    id = Column(Integer, primary_key=True)
    submission_id = Column(Integer, ForeignKey("submissions.id"), nullable=False)
    original_filename = Column(String(500), nullable=False)
    stored_path = Column(String(1000), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Submission(Base):
    __tablename__ = "submissions"
    id = Column(Integer, primary_key=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    spec_version_id = Column(Integer, ForeignKey("spec_versions.id"), nullable=False)
    target_unit_name = Column(String(500), nullable=True)  # which unit this submission is marked against - keeps the marking prompt scoped instead of sending the whole spec
    submission_type = Column(String(50), default="essay")
    original_filename = Column(String(500), nullable=False)  # display summary - e.g. "report.docx + 2 more" once multi-file
    stored_path = Column(String(1000), nullable=True)  # deprecated single-file path, kept nullable for old rows - new uploads use the `files` relationship instead
    annotated_path = Column(String(1000), nullable=True)
    content_hash = Column(String(128), nullable=True)
    status = Column(String(50), default="uploaded")
    submitted_at = Column(Date, nullable=True)  # when the student actually submitted, distinct from created_at (upload timestamp) - lets late-submission tracking work even if you upload something days after the fact
    achieved_grade = Column(String(50), nullable=True)  # computed via the BTEC aggregation rule (must meet ALL criteria in a band, and all lower bands) - null if criteria don't use recognisable P/M/D codes
    marking_scheme_notes = Column(Text, nullable=True)  # optional - overrides the default Met/Partly met/Not met scale with a rubric/numeric scheme (e.g. "score 2/5/7/10") the course actually uses
    pregraded_components = Column(JSON, nullable=True)  # components already scored externally (e.g. an auto-marked MCQ) - [{"label": str, "section": str, "score": float, "max_score": float}] - given to the AI as context only, never re-derived, and included in the deterministic section totals
    pass_threshold_pct = Column(Float, nullable=True)  # e.g. 70.0 - if set, each "section" (grouped by the section field on marks/pregraded components) must independently reach this % to count as an overall pass
    part_weightings = Column(JSON, nullable=True)  # e.g. {"MCQ": 45, "Longform": 55} - marker-supplied weight of each part toward the weighted overall % (see section_grading.py). Null = not specified; the app then uses an explicitly-flagged equal weighting rather than assuming a split.
    detected_scoring_mechanism = Column(Text, nullable=True)  # what the AI identified as this rubric's own scoring vocabulary (e.g. "Numeric 2/5/7/10 for Longform criteria; Met/Partly met/Not met for MCQ") - stated upfront so the marker can confirm the rubric was read correctly before trusting the marks
    section_results = Column(JSON, nullable=True)  # computed after marking: {"MCQ": {"total":.., "max":.., "pct":.., "passed":..}, ...} plus "overall_passed", "weighted_overall_pct", "weighted_overall_passed", "weighting_source", "criteria_below_threshold"
    rubric_file_path = Column(String(1000), nullable=True)  # the ACTUAL uploaded rubric file - filled in place, never rebuilt as a new document
    similarity_warning = Column(Text, nullable=True)  # set automatically at upload time if this matches another student's submission for the same assignment above the threshold
    assignment_brief_text = Column(Text, nullable=True)  # the actual task/brief set to students, if given - lets the AI distinguish "this response doesn't cover part of a criterion because the TASK never asked for it" from "the student missed it". Without this, a criterion is marked against its full spec wording regardless of what the assignment actually asked for.
    marked_for_deletion_at = Column(DateTime, nullable=True)  # retention policy grace-period countdown start - files are purged (not the row/marks) once this plus the category's grace_period_days has passed; null means not currently scheduled
    purged_at = Column(DateTime, nullable=True)  # when the files were actually removed, if they have been - the row and marks are kept as the permanent record even after this
    retention_exempt = Column(Boolean, default=False, nullable=False)  # an admin can mark a specific submission exempt (e.g. an appeal in progress) - stops it being auto-marked for deletion again regardless of age, until explicitly cleared
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    student = relationship("Student", back_populates="submissions")
    spec_version = relationship("SpecVersion")
    marking_run = relationship("MarkingRun", back_populates="submission", uselist=False)
    files = relationship("SubmissionFile", order_by="SubmissionFile.id", cascade="all, delete-orphan",
                          primaryjoin="Submission.id==SubmissionFile.submission_id")


class MarkingRun(Base):
    __tablename__ = "marking_runs"
    id = Column(Integer, primary_key=True)
    submission_id = Column(Integer, ForeignKey("submissions.id"), nullable=False)
    status = Column(String(50), default="queued")
    overall_summary = Column(Text, nullable=True)
    error_detail = Column(Text, nullable=True)
    pii_redactions_applied = Column(JSON, nullable=True)  # e.g. ["student name", "email address"] - what was found and redacted from the submission text BEFORE it was sent to the AI provider for marking, shown to the teacher rather than done silently. Empty/null if nothing matched.
    generated_by_provider = Column(String(50), nullable=True)  # which AI provider actually marked this ("anthropic"/"ollama"/"azure_openai"/"openai_compatible") - previously only ever surfaced in failure-path error messages, never recorded against successful runs, so there was no way to check after the fact which provider produced a given result
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    submission = relationship("Submission", back_populates="marking_run")
    criterion_marks = relationship("CriterionMark", back_populates="marking_run")


class CriterionMark(Base):
    __tablename__ = "criterion_marks"
    id = Column(Integer, primary_key=True)
    marking_run_id = Column(Integer, ForeignKey("marking_runs.id"), nullable=False)
    criterion_ref = Column(String(255), nullable=False)
    ai_mark = Column(String(100), nullable=True)
    ai_confidence = Column(Float, nullable=True)
    evidence_text = Column(Text, nullable=True)
    ai_comment = Column(Text, nullable=True)
    source_file = Column(String(500), nullable=True)  # which uploaded file the evidence came from
    source_location = Column(String(255), nullable=True)  # e.g. "Slide 2", "Q25", "Sheet1 Row 12"
    needs_human_review = Column(Boolean, default=False, nullable=False)
    human_review_reason = Column(Text, nullable=True)  # why the AI couldn't confidently assess this itself
    numeric_score = Column(Float, nullable=True)  # only populated for numeric/points rubrics - real arithmetic sums these, never trusts an AI's own claimed total
    max_score = Column(Float, nullable=True)
    section = Column(String(255), nullable=True)  # groups criteria for section-level pass thresholds (e.g. "MCQ" vs "LF")
    scoring_vocabulary = Column(String(255), nullable=True)  # the literal scale/vocabulary the rubric itself uses for THIS criterion (e.g. "Met/Partly met/Not met" or "numeric 0-10"), read off the rubric rather than assumed - lets one document mix scales per criterion
    matched_descriptor_quote = Column(Text, nullable=True)  # the exact band descriptor text quoted from the rubric row that the AI matched the response against, so the marker can check the judgement against the rubric's own wording rather than a paraphrase
    descriptor_ambiguous = Column(Boolean, default=False, nullable=False)  # true if the rubric's own descriptor could support two different readings of this response - flagged rather than silently picking one
    ambiguity_note = Column(Text, nullable=True)  # only set when descriptor_ambiguous is true: explains the two readings
    evidence_on_topic = Column(Boolean, nullable=True)  # concrete confidence factor: was the cited evidence directly on-topic for this criterion, or only adjacent?
    covers_all_subparts = Column(Boolean, nullable=True)  # concrete confidence factor: did the response cover all sub-parts of the criterion, or only some?
    near_band_boundary = Column(Boolean, nullable=True)  # concrete confidence factor: is the response genuinely near a boundary between two rubric bands?
    source_unreadable_or_missing = Column(Boolean, nullable=True)  # concrete confidence factor: was any part of the source material unreadable or missing?
    review_priority = Column(String(20), nullable=True)  # "low"/"medium"/"high" - computed deterministically in code from the four factors above (see compute_review_priority in submissions.py), never trusted as a bare self-reported percentage
    criterion_fully_addressed = Column(Boolean, nullable=True)  # false if this ONE submission's evidence (and, if given, the assignment brief) only covers PART of the criterion's full spec wording - not a fail on its own, since a criterion can legitimately be built up across multiple assessment opportunities, but it must be surfaced rather than silently marked as if fully met
    coverage_note = Column(Text, nullable=True)  # only set when criterion_fully_addressed is false: specifically which part of the criterion wasn't addressed, and whether that's because the brief never asked for it or because the response just didn't cover it
    final_mark = Column(String(100), nullable=True)
    final_comment = Column(Text, nullable=True)
    overridden = Column(Boolean, default=False)

    marking_run = relationship("MarkingRun", back_populates="criterion_marks")


# Fixed set of reason codes for the correct-then-approve feedback loop - a short controlled
# vocabulary (rather than free text only) so per-criterion correction history can actually be
# aggregated and surfaced ("this criterion has been corrected 4 of the last 10 times, always in
# the same direction") instead of being locked away in unstructured comment_diff text.
CORRECTION_REASON_CODES = [
    ("too_strict", "Too strict on command-word interpretation"),
    ("too_lenient", "Too lenient on command-word interpretation"),
    ("missed_evidence", "Missed evidence elsewhere in the submission"),
    ("misread_band_boundary", "Misread the rubric's band boundary"),
    ("wrong_descriptor_match", "Matched the wrong descriptor/band entirely"),
    ("factual_error", "Factual error in the AI's evidence or reasoning"),
    ("other", "Other (see comment)"),
]


class TeacherOverride(Base):
    __tablename__ = "teacher_overrides"
    id = Column(Integer, primary_key=True)
    criterion_mark_id = Column(Integer, ForeignKey("criterion_marks.id"), nullable=False)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    ai_mark = Column(String(100))
    final_mark = Column(String(100))
    reason_code = Column(String(50), nullable=True)  # one of CORRECTION_REASON_CODES - why the human corrected the AI, so the gap is auditable and aggregable per criterion, not just visible as a diff
    comment_diff = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class AuditLogEntry(Base):
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True)
    submission_id = Column(Integer, ForeignKey("submissions.id"), nullable=True)
    actor = Column(String(255), nullable=False)
    action = Column(String(255), nullable=False)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class IVRequest(Base):
    """
    Internal Verification. Two flows feed into this same model:
    1. In-tool: a submission already marked in TIGARSEC Assess gets sent to a colleague
       for IV (submission_id is set).
    2. External: a lecturer uploads work that was ALREADY marked outside this tool
       (submission_id is null; external_original_marks_summary holds what they typed
       describing the existing marks, external_file_path holds the marked document).
    """
    __tablename__ = "iv_requests"
    id = Column(Integer, primary_key=True)
    submission_id = Column(Integer, ForeignKey("submissions.id"), nullable=True)
    spec_version_id = Column(Integer, ForeignKey("spec_versions.id"), nullable=False)
    requested_by_teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    assigned_to_teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=True)
    iv_format_sample = Column(Text, nullable=True)
    external_file_path = Column(String(1000), nullable=True)
    external_original_marks_summary = Column(Text, nullable=True)
    status = Column(String(50), default="pending")  # pending, ai_assisting, ready_for_review, completed, failed
    error_detail = Column(Text, nullable=True)
    ai_overall_summary = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    submission = relationship("Submission")
    spec_version = relationship("SpecVersion")
    requested_by = relationship("Teacher", foreign_keys=[requested_by_teacher_id])
    assigned_to = relationship("Teacher", foreign_keys=[assigned_to_teacher_id])
    checks = relationship("IVCriterionCheck", back_populates="iv_request")


class IVCriterionCheck(Base):
    __tablename__ = "iv_criterion_checks"
    id = Column(Integer, primary_key=True)
    iv_request_id = Column(Integer, ForeignKey("iv_requests.id"), nullable=False)
    criterion_ref = Column(String(255), nullable=False)
    original_mark = Column(String(100), nullable=True)
    original_comment = Column(Text, nullable=True)
    ai_suggestion = Column(Text, nullable=True)
    ai_confidence = Column(Float, nullable=True)
    iv_agree = Column(Boolean, nullable=True)
    iv_comment = Column(Text, nullable=True)

    iv_request = relationship("IVRequest", back_populates="checks")


class SOWTemplate(Base):
    """
    A shared, institution-wide scheme-of-work template - same reasoning as SpecVersion:
    only admin/curriculum_manager can upload (SPEC_UPLOADER_ROLES, reused here rather than
    a new role list), everyone else picks from this list rather than each uploading their
    own copy of the same college template. A lecturer can still attach a one-off template
    for a single generation instead if they genuinely need to (SchemeOfWork.format_sample_*
    already supports that, unchanged) - this is an additional, easier option, not a
    replacement for it.
    """
    __tablename__ = "sow_templates"
    id = Column(Integer, primary_key=True)
    uploaded_by_teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    label = Column(String(255), nullable=False)
    file_path = Column(String(1000), nullable=False)
    original_filename = Column(String(500), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class ExamExemplar(Base):
    """
    A shared, institution-wide reference document showing what a REAL exam/Occupational
    Specialism task actually requires - built to close a real, confirmed gap: the spec
    alone doesn't always cover everything needed to pass (a real example given: the spec
    covers the core papers but doesn't itemize a Gantt chart requirement that's actually
    part of an ESP task). Uploaded by admin/curriculum_manager (SPEC_UPLOADER_ROLES,
    same gate as the spec library and SOW template library) - deliberately per UNIT, not
    all-at-once, since a college may only be delivering some of a spec's content areas and
    shouldn't need every exemplar uploaded before any of this is useful.

    Scoped to an exact spec + unit name(s) (matched against that spec's own structured_json
    unit names, not free text) so SOW generation can reliably find the right one(s) for
    whatever units were selected - a mismatch here would silently mean the exemplar never
    gets used, which matters enough to make it a real dropdown choice at upload time
    rather than something to get slightly wrong by typing it.

    unit_names is a LIST, not a single value - confirmed as a real requirement: a single
    real exemplar (e.g. a synoptic Occupational Specialism assessment) can genuinely span
    several content areas at once, not just one, so this needed to support several units
    per upload from the start rather than forcing the same file to be uploaded repeatedly.
    """
    __tablename__ = "exam_exemplars"
    id = Column(Integer, primary_key=True)
    uploaded_by_teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    spec_version_id = Column(Integer, ForeignKey("spec_versions.id"), nullable=False)
    unit_names = Column(JSON, nullable=False)
    unit_name = Column(String(500), nullable=True)  # legacy column from before the multiunit migration - the live DB table still has this as NOT NULL
    label = Column(String(255), nullable=False)
    file_path = Column(String(1000), nullable=False)
    original_filename = Column(String(500), nullable=False)
    extracted_text = Column(Text, nullable=True)  # extracted once at upload time, reused for every generation rather than re-extracting the same file repeatedly
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


ROOM_TYPES = [
    ("general_classroom", "General classroom"),
    ("computer_lab", "Computer lab"),
    ("practical_workshop", "Practical workshop"),
    ("lecture_hall", "Lecture hall"),
    ("other", "Other"),
]


class TeachingHoursBand(Base):
    """
    College-defined policy points mapping contracted hours to maximum teaching (contact)
    hours - deliberately a real, editable table rather than a fixed formula, since a real
    example given directly contradicted a simple fixed percentage: 35 contracted hours ->
    24 teaching hours (~68.6%) but 20 contracted hours -> 13 teaching hours (65%) - the
    ratio itself genuinely shifts with contract size, most likely because some admin/prep
    overhead doesn't shrink proportionally just because someone's contract does.

    Used to calculate a specific teacher's max teaching hours from their own contracted
    hours (see calculate_teaching_hours in the timetabling router) - exact matches use the
    band directly, values between two defined bands are interpolated, and values outside
    the whole defined range are extrapolated from the nearest band's own ratio and
    flagged as an estimate, since no explicit college policy exists for that range yet.
    """
    __tablename__ = "teaching_hours_bands"
    id = Column(Integer, primary_key=True)
    contracted_hours = Column(Float, nullable=False)
    max_teaching_hours = Column(Float, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Site(Base):
    """
    An admin-managed list of real sites/buildings/floors at the college - added directly
    from the UI, not hardcoded, since this genuinely varies per college and needs to be
    addable without a code change every time.

    Room.building and CourseUnitRequirement.preferred_building both stay as plain string
    columns rather than a foreign key here - no schema change needed, and no risk to
    existing data already saved as free text. What changes is the FORM: both now pick from
    this list rather than typing freely, so new values are consistent (a room tagged
    "Middle floor" here and a requirement preferring "Middle floor" there will actually
    match, rather than drifting apart from small wording differences over time).
    """
    __tablename__ = "sites"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Room(Base):
    """
    Timetabling foundation, Phase 1 (see the timetabling requirements analysis) - a shared,
    institution-wide list of physical teaching spaces. Room TYPE is tracked separately from
    capacity deliberately: a room being big enough is not the same as a room being SUITABLE
    - a cyber security practical needs a computer_lab, not just any room that happens to fit
    the group.

    supports_isolated_network flags a room as capable of running the sandboxed/isolated
    network setup the college cyber range project needs - a real, specific requirement
    identified during the requirements analysis, not every computer_lab necessarily has this,
    so it's tracked as its own flag rather than assumed from room_type alone.
    """
    __tablename__ = "rooms"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    capacity = Column(Integer, nullable=False)
    room_type = Column(String(50), default="general_classroom", nullable=False)
    building = Column(String(255), nullable=True)
    supports_isolated_network = Column(Boolean, default=False, nullable=False)
    accessibility_features = Column(Text, nullable=True)  # free text - step-free access, hearing loop, etc.
    is_active = Column(Boolean, default=True, nullable=False)  # soft-disable a room (e.g. out of use for refurbishment) without losing its history
    is_dummy = Column(Boolean, nullable=False, default=False)  # same reasoning as Teacher.is_dummy - explicit flag rather than name-matching, so bulk test data can be cleared with zero risk to a real room
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class StaffUnavailability(Base):
    """
    One-off, DATED unavailability - deliberately separate from Teacher.non_teaching_days
    above, which is a recurring weekly pattern. This is for specific dates (a training day,
    an appointment, planned leave), not a standing pattern.

    V1 simplification, deliberate and worth knowing: entered directly by whoever has access
    to this area (admin/curriculum_manager, via early access - see auth.py) on a staff
    member's behalf, rather than a full self-service staff request + approval workflow.
    The requirements analysis (see timetabling_requirements_analysis.md, section 8.2) notes
    real institutions run this as request-then-approve - that's a real, deliberately
    deferred refinement, not an oversight, since regular staff don't have UI access to this
    area at all yet at this stage of the project.
    """
    __tablename__ = "staff_unavailability"
    id = Column(Integer, primary_key=True)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    portion = Column(String(10), default="full_day", nullable=False)  # "full_day", "am", or "pm" - a morning-only absence still leaves the afternoon schedulable, a real distinction for timetabling even though it's a single date
    reason = Column(String(500), nullable=True)
    entered_by_teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    teacher = relationship("Teacher", foreign_keys=[teacher_id])


class TeacherUnitQualification(Base):
    """
    Which specific units a teacher is qualified/eligible to teach - the hard-constraint
    ELIGIBILITY gate from the requirements analysis (section 3, item 6): a session can only
    go to someone actually qualified for it. This is deliberately separate from the fuller
    multi-factor skill-RANKING score described in section 4 of the analysis - that ranking
    (qualification level, IV outcomes, CPD, etc.) is a later refinement; this table is just
    the basic yes/no eligibility floor it will eventually sit on top of.

    Scoped to an exact spec + unit name, same reasoning and same matching approach as
    ExamExemplar above - matched against that spec's own real unit names, not free text.
    """
    __tablename__ = "teacher_unit_qualifications"
    id = Column(Integer, primary_key=True)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    spec_version_id = Column(Integer, ForeignKey("spec_versions.id"), nullable=False)
    status = Column(String(20), nullable=False, default="confirmed")  # "confirmed" (admin-set, or an admin-approved request) vs "pending" (teacher self-requested via the self-service page, not yet trusted for real generator placement decisions) vs "pending_removal" (teacher-requested removal of a confirmed qualification, awaiting admin review - still trusted for scheduling until then)
    unit_name = Column(String(500), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    teacher = relationship("Teacher", foreign_keys=[teacher_id])


class TeacherSpecExperience(Base):
    """
    Years of experience and a discretionary admin rating (1-5) for a teacher - either
    scoped to one spec/qualification (spec_version_id set) or GENERAL (spec_version_id
    NULL), meaning it applies to every unit that teacher is qualified for as a baseline.

    Originally spec-only, but confirmed as a real remaining gap directly: a teacher deeply
    experienced in one subject (e.g. security, taught under a Cyber Security spec) may
    also be qualified for a DIFFERENT spec that happens to touch the same subject (e.g. a
    Software Development course with its own security content area) - under a purely
    spec-scoped model, that teacher would show as having no experience/rating standing
    at all for the second spec, understating someone genuinely well-suited to it. A fully
    global, un-scoped single figure was considered and deliberately rejected instead - it
    would let deep expertise in one narrow area silently inflate a teacher's ranking for
    completely unrelated subjects they've never actually worked in, the opposite failure
    from the one being fixed.

    A general (spec_version_id=NULL) figure resolves the tension: it's the teacher's
    overall standing, applied everywhere they're qualified by default - and a spec-specific
    row, where one exists, OVERRIDES the general figure for units under that particular
    spec only, letting genuinely deeper expertise in one qualification still be recorded
    without leaking into unrelated ones. See skill_ranking.py's caller in
    _build_generation_inputs for the override-resolution logic.

    These two factors are naturally coarser-grained judgments ("how experienced is this
    person, generally or at this qualification"), not something that meaningfully differs
    content-area to content-area within the same spec - confirmed as a real, unnecessary
    workload problem when this briefly lived on each individual TeacherUnitQualification
    row instead: a teacher qualified for four content areas under one spec needed the
    exact same figures re-entered four separate times.

    CPD and IV outcomes stay genuinely unit-specific (see TeacherCPDRecord and
    TeacherIVOutcome above) since those really can differ unit to unit - a CPD event on
    penetration testing techniques or an IV sample against one specific unit's marking
    decisions doesn't generalise the same way a teacher's overall experience level does.
    CPD does support its own separate general/specific split for the same reason (see
    TeacherCPDRecord).

    One row per (teacher, spec) INCLUDING the general NULL-spec row - enforced at the
    application level via lookup-then-update/insert, matching this project's existing
    pattern (e.g. SkillRankingWeights' own singleton-per-college row) rather than a
    DB-level unique constraint, since this schema-sync mechanism works from plain column
    definitions.
    """
    __tablename__ = "teacher_spec_experience"
    id = Column(Integer, primary_key=True)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    spec_version_id = Column(Integer, ForeignKey("spec_versions.id"), nullable=True)  # NULL = general, applies to every unit this teacher is qualified for; set = overrides the general figure for units under that spec only
    years_experience = Column(Float, nullable=True)
    admin_rating = Column(Integer, nullable=True)  # 1-5, deliberately simple and discretionary - real HR-sensitive data about a real person's job performance: only ever shown to admin/curriculum-manager access, never to other teachers
    updated_at = Column(DateTime, default=datetime.datetime.utcnow)

    teacher = relationship("Teacher", foreign_keys=[teacher_id])


class TeacherCPDRecord(Base):
    """
    A single continuing-professional-development entry for a teacher, optionally scoped to
    a specific unit/subject area. Kept as a log (a teacher accumulates many over time),
    unlike years_experience/admin_rating above which are single values. Recency matters a
    great deal for scoring - CPD from years ago counts for much less than CPD from this
    year, especially in fast-moving vocational subjects - see skill_ranking.py for the
    decay calculation. Deliberately not tied to a specific awarding body's own CPD
    categories, since colleges work with many different ones.

    General (unit_name/spec_version_id both NULL) is deliberately the home for CPD that
    isn't about any particular subject at all - safeguarding, Prevent duty, equality and
    diversity, GDPR, first aid - confirmed as a real, common case directly: a college
    still wants this recorded, but it shouldn't be forced under one specific unit just
    because the form technically requires picking something. General CPD counts toward
    every unit a teacher is qualified for, exactly like the general skill-ranking figure.

    status mirrors TeacherUnitQualification.status exactly, for the same reason: a
    teacher can submit their own CPD via self-service, landing as "pending" until an
    admin confirms it - not yet trusted for real generator scoring, since an
    unreviewed self-report could otherwise inflate a ranking with nothing behind it.
    Admin-entered CPD (via the Staff page) is created directly as "confirmed".
    """
    __tablename__ = "teacher_cpd_records"
    id = Column(Integer, primary_key=True)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    spec_version_id = Column(Integer, ForeignKey("spec_versions.id"), nullable=True)  # nullable - some CPD is general (e.g. safeguarding, assessment methods) rather than tied to one spec
    unit_name = Column(String(500), nullable=True)  # nullable for the same reason
    title = Column(String(300), nullable=False)
    date = Column(Date, nullable=False)
    note = Column(String(1000), nullable=True)
    status = Column(String(20), nullable=False, default="confirmed")  # "confirmed" (admin-entered, or an admin-approved self-service request) vs "pending" (teacher self-requested, not yet trusted for real generator scoring)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    teacher = relationship("Teacher", foreign_keys=[teacher_id])


class TeacherIVOutcome(Base):
    """
    A single Internal Verification / quality-assurance sampling outcome for a teacher on a
    unit. Deliberately normalised to a small, awarding-body-agnostic set of outcome levels
    rather than each awarding body's own specific wording, since colleges work with many
    different awarding bodies (Pearson, City & Guilds, NCFE, AQA, OCR, WJEC, and others)
    whose own IV terminology differs even though the underlying meaning converges: were
    standards met, or not. Recency matters here too - an old, since-resolved concern
    shouldn't permanently follow someone, see skill_ranking.py for the decay calculation.
    """
    __tablename__ = "teacher_iv_outcomes"
    id = Column(Integer, primary_key=True)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    spec_version_id = Column(Integer, ForeignKey("spec_versions.id"), nullable=False)
    unit_name = Column(String(500), nullable=False)
    date = Column(Date, nullable=False)
    outcome_level = Column(String(30), nullable=False)  # "no_concerns", "minor_action", or "significant_concern" - see note above on why this is normalised rather than awarding-body-specific
    note = Column(String(1000), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    teacher = relationship("Teacher", foreign_keys=[teacher_id])


class SkillRankingWeights(Base):
    """
    One editable row per college describing how heavily each ranking factor counts, and
    how quickly CPD/IV evidence decays with age - a real, college-owned policy table
    rather than a fixed formula, exactly the same principle already used for
    TeachingHoursBand. A college with no interest in a given factor sets its weight to 0
    rather than the system silently assuming a default it never agreed to.

    Singleton pattern (one row, id=1) exactly like TimetablingSettings - a college has one
    set of ranking priorities, not several.
    """
    __tablename__ = "skill_ranking_weights"
    id = Column(Integer, primary_key=True)
    weight_experience = Column(Float, nullable=False, default=1.0)
    weight_admin_rating = Column(Float, nullable=False, default=1.0)
    weight_cpd = Column(Float, nullable=False, default=1.0)
    weight_iv = Column(Float, nullable=False, default=1.0)
    cpd_half_life_years = Column(Float, nullable=False, default=3.0)  # a CPD entry this many years old counts for half as much as a brand new one
    iv_half_life_years = Column(Float, nullable=False, default=2.0)  # IV outcomes decay a bit faster than CPD by default - a resolved concern should stop mattering sooner than good CPD stops being relevant
    updated_at = Column(DateTime, nullable=True)
    updated_by = Column(String(200), nullable=True)


DAYS_OF_WEEK = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


class TimetableSession(Base):
    """
    Timetabling, Phase 2 (see the requirements analysis and Phase 1's Room/staff-
    availability foundation) - the actual schedule: a recurring weekly slot (day + time),
    not a specific calendar date, since a real timetable repeats the same pattern across a
    whole term rather than being entered occurrence by occurrence.

    Named TimetableSession, not just Session, deliberately - SQLAlchemy's own Session type
    is already imported by name in every router file in this app (the db dependency's
    type hint), and reusing that name here would shadow it.

    teacher_id and room_id are nullable deliberately - a session can exist "unassigned"
    while a timetable is still being built (clash detection can still run against whatever
    IS assigned so far), not just once every field is filled in.

    unit_name is scoped to the course's own spec, same matching approach as
    TeacherUnitQualification/ExamExemplar above - matched against real unit names from
    that spec, not free text. Nullable since not every session necessarily maps to one
    specific spec unit (a tutorial slot, a study session, etc).
    """
    __tablename__ = "timetable_sessions"
    id = Column(Integer, primary_key=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=True)
    day_of_week = Column(String(10), nullable=False)
    start_time = Column(String(5), nullable=False)  # "HH:MM", 24-hour, zero-padded - chosen deliberately so plain string comparison (</>) already matches chronological order, verified directly rather than assumed
    end_time = Column(String(5), nullable=False)
    unit_name = Column(String(500), nullable=True)
    label = Column(String(255), nullable=True)  # optional human label shown in listings, independent of whatever unit_name resolves to
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    course = relationship("Course")
    teacher = relationship("Teacher", foreign_keys=[teacher_id])
    room = relationship("Room")



class LessonImage(Base):
    """
    A shared, tag-searchable image library any teacher can contribute to. Used as the
    second-tier image source for lesson slides (Unsplash first if configured, then this
    library, then a generated fallback graphic) - see app/pptx_generator.py.
    """
    __tablename__ = "lesson_images"
    id = Column(Integer, primary_key=True)
    uploaded_by_teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    tags = Column(Text, nullable=False)  # comma-separated keywords, matched against slide image_query
    file_path = Column(String(1000), nullable=False)
    attribution_text = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class LessonDeck(Base):
    __tablename__ = "lesson_decks"
    id = Column(Integer, primary_key=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    content_area_name = Column(String(500), nullable=False)
    title = Column(String(255), nullable=False)
    duration_minutes = Column(Integer, default=60, nullable=False)
    previous_lesson_context = Column(Text, nullable=True)
    include_practicals = Column(Boolean, default=True, nullable=False)
    unit_index = Column(Integer, nullable=True)  # which spec unit this deck was built from - lets a follow-up lesson reuse the same selection
    selected_lo_indices = Column(JSON, nullable=True)  # which LOs within that unit were selected
    selected_criteria_by_lo = Column(JSON, nullable=True)  # {lo_index: [criterion_index, ...]} - selecting individual criteria within one flat/ungrouped LO rather than the whole thing (see presentations.py's _unit_content_text). Stored separately from selected_lo_indices above so "regenerate" can rebuild the exact same scoped content rather than falling back to the whole LO
    source_sow_id = Column(Integer, ForeignKey("schemes_of_work.id"), nullable=True)  # if generated from a specific scheme-of-work week, lets that week link straight back to this deck
    source_week = Column(Integer, nullable=True)
    fits_in_requested_time = Column(Boolean, nullable=True)  # the AI's own assessment - null until generation completes
    scope_note = Column(Text, nullable=True)  # explains what's covered vs what needs a follow-up session, if it doesn't fit
    outline_json = Column(JSON, nullable=True)
    pptx_path = Column(String(1000), nullable=True)
    status = Column(String(50), default="queued", nullable=False)  # queued, generating, ready, failed
    error_detail = Column(Text, nullable=True)
    generated_by_provider = Column(String(50), nullable=True)  # which AI provider actually generated this deck - drafts default to Ollama unless overridden
    marked_for_deletion_at = Column(DateTime, nullable=True)  # retention policy grace-period countdown start - see Submission's equivalent field for the full explanation
    purged_at = Column(DateTime, nullable=True)
    retention_exempt = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    course = relationship("Course")


class ResourceShare(Base):
    """
    A deliberate, tracked grant of access to one lesson deck for another named teacher -
    e.g. sending a week's PowerPoint/activities to a colleague covering an absence. This
    is a real access grant, not just a link: viewing/downloading a deck now checks
    ownership OR an active share here (see _can_access_deck in routers/presentations.py) -
    previously any authenticated teacher could open any deck by ID with no check at all,
    which this closes while also making sharing an explicit, visible, revocable action
    rather than an accident of missing authorization.
    """
    __tablename__ = "resource_shares"
    id = Column(Integer, primary_key=True)
    lesson_deck_id = Column(Integer, ForeignKey("lesson_decks.id"), nullable=False)
    shared_by_teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    shared_with_teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    note = Column(Text, nullable=True)  # e.g. "Off sick Tuesday - could you cover this one?"
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    lesson_deck = relationship("LessonDeck")
    shared_by_teacher = relationship("Teacher", foreign_keys=[shared_by_teacher_id])
    shared_with_teacher = relationship("Teacher", foreign_keys=[shared_with_teacher_id])


class Feedback(Base):
    """
    In-app feedback/feature-request box - deliberately simple (one free-text message plus
    a category) so it's a low-friction thing to fill in during or right after using a
    feature, rather than a formal form that discourages quick "this would help" notes.
    """
    __tablename__ = "feedback"
    id = Column(Integer, primary_key=True)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    category = Column(String(50), default="general", nullable=False)  # "bug" | "feature_request" | "general"
    message = Column(Text, nullable=False)
    page_context = Column(String(500), nullable=True)  # the page they were on when they submitted it, captured automatically
    status = Column(String(50), default="new", nullable=False)  # "new" | "acknowledged" | "planned" | "done" | "declined"
    admin_response = Column(Text, nullable=True)  # optional note back, shown to the person who submitted it
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, nullable=True)

    teacher = relationship("Teacher")


class SystemSettings(Base):
    """
    A single-row settings table (always id=1) for deployment-wide configuration that an
    admin controls, not something baked into code or a .env file a non-technical admin
    can't reach.
    - allowed_email_domains: which email domain(s) can self-register, so a DIFFERENT
      college adopting this same software configures it for THEIR OWN domain (e.g.
      wlv.ac.uk) without needing a code change or my involvement - comma-separated so one
      deployment can allow more than one real domain (e.g. staff + student subdomains).
      Empty/null means no restriction (the original, permissive default).
    - two_factor_mandatory: forces 2FA on for every account, overriding each person's own
      opt-in in Settings - an admin-level decision, not a per-user one.
    - smtp_*: same reasoning as AI provider settings - configurable here so email sending
      can be fixed from the UI, not only by editing .env on the server. If smtp_host here
      is empty, email_sender.py falls back to the .env values, so nothing breaks for a
      deployment that only ever configured this via .env.
    """
    __tablename__ = "system_settings"
    id = Column(Integer, primary_key=True)
    allowed_email_domains = Column(Text, nullable=True)
    two_factor_mandatory = Column(Boolean, default=False, nullable=False)
    smtp_host = Column(String(255), nullable=True)
    smtp_port = Column(Integer, nullable=True)
    smtp_user = Column(String(255), nullable=True)
    smtp_password = Column(Text, nullable=True)
    smtp_from = Column(String(255), nullable=True)
    smtp_use_tls = Column(Boolean, nullable=True)
    # Stage 6: the address students' phones use to reach CourseCraft (e.g. http://192.168.1.20:8000
    # or https://coursecraft.college.ac.uk) - put inside the live lesson QR code. Empty = the
    # address the lecturer's browser used (fine once CourseCraft has a proper network name).
    public_base_url = Column(String(255), nullable=True)
    updated_at = Column(DateTime, nullable=True)
    updated_by_teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=True)


class CourseUnitRequirement(Base):
    """
    The missing input the automated generator needs: what a specific unit within a course
    actually requires to be scheduled - hours per week, how many days to spread across (1
    means blocked into a single day, matching the real "whole day for one thing" pattern
    seen in an actual T-Level example timetable), which specific days are preferred if
    any, and what kind of room it needs.

    Scoped to a course + unit_name, not just a course - a real T-Level example showed
    different units within the same course needing genuinely different treatment (one
    blocked into a single day, another spread across two), so a single "this course needs
    N hours" figure wouldn't be enough for the generator to actually place things
    correctly. unit_name matched against the course's own spec, same approach as
    TeacherUnitQualification/ExamExemplar/TimetableSession above - real unit names, not
    free text. Nullable to also support a whole-course requirement (e.g. a tutorial slot
    that isn't tied to any specific spec unit).
    """
    __tablename__ = "course_unit_requirements"
    id = Column(Integer, primary_key=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    unit_name = Column(String(500), nullable=True)
    hours_per_week = Column(Float, nullable=False)
    preferred_days_count = Column(Integer, nullable=True)  # how many DAYS to spread hours_per_week across - 1 = block into a single day
    preferred_days = Column(JSON, nullable=True)  # specific weekday names, if particular days matter (not just how many)
    preferred_room_type = Column(String(50), nullable=True)  # matches ROOM_TYPES
    preferred_building = Column(String(255), nullable=True)  # free text, matched against Room.building - this is where a "keep DSS on the middle floor" preference plugs in
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    course = relationship("Course")


class TimetablingSettings(Base):
    """
    A single-row settings table (always id=1), same pattern as SystemSettings above - kept
    as its own table rather than added there, since that one already covers auth/email/SMTP
    and this is a genuinely separate subject.

    Deliberately a real, editable setting rather than a hardcoded assumption - confirmed as
    a real requirement: working hours and whether there's even a fixed lunch block both
    vary by college, and this needs to work for more than just the one college it started
    with.

    lunch_start/lunch_end are both nullable together - a college may have no fixed,
    college-wide lunch block at all, which is a real, valid choice, not a gap to fill in.

    min_break_minutes/break_after_hours: a real, described practice, not a legal-minimum
    placeholder (min_break_minutes was defined earlier but never actually wired into
    anything, so repurposing it here is safe) - any session of break_after_hours or more
    gets a mid-session break of min_break_minutes, which extends the session's clock-time
    span so it still delivers the full requested teaching hours (the same reasoning
    already applied to a lunch-spanning session: a 3-hour lesson with a 15-minute break in
    the middle needs 3 hours 15 minutes on the clock to still be 3 real teaching hours).
    """
    __tablename__ = "timetabling_settings"
    id = Column(Integer, primary_key=True)
    working_day_start = Column(String(5), nullable=False, default="09:00")
    working_day_end = Column(String(5), nullable=False, default="17:00")
    lunch_start = Column(String(5), nullable=True)
    lunch_end = Column(String(5), nullable=True)
    min_break_minutes = Column(Integer, nullable=False, default=15)
    break_after_hours = Column(Float, nullable=False, default=3.0)
    updated_at = Column(DateTime, nullable=True)
    updated_by_teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=True)


class AIProviderConfig(Base):
    """
    A saved, reusable AI provider configuration - lets an admin configure Anthropic,
    Ollama, Azure OpenAI, or any other OpenAI-compatible endpoint (a local server, a
    different vendor entirely, whatever's actually available on a given college's
    network) from the UI, with their own API key/endpoint, rather than only through
    .env + a server restart. Several can be saved at once (e.g. both Ollama for testing
    and Anthropic for real use) and switched between with one click.

    THREE task categories, each independently configurable, so activating a paid
    provider for marking doesn't silently switch everything else onto it too:
    - is_active: marking, and anything without a more specific category below.
    - is_active_for_spec_parsing: spec parsing specifically (defaults to is_active if unset).
    - is_active_for_draft_generation: scheme-of-work and lesson-plan generation - these
      are drafts a teacher reviews/edits before use, so cost matters more than for marking.
      Confirmed as a real requirement: this one does NOT fall back to is_active if unset -
      it defaults straight to Ollama (free/local), so activating a paid provider for
      marking never silently starts charging for draft generation too. An admin can still
      point this category at a paid provider explicitly if they want to.
    """
    __tablename__ = "ai_provider_configs"
    id = Column(Integer, primary_key=True)
    provider_type = Column(String(50), nullable=False)  # "anthropic" | "ollama" | "azure_openai" | "openai_compatible"
    label = Column(String(255), nullable=False)  # a friendly name the admin picks, e.g. "College Azure OpenAI"
    api_key = Column(Text, nullable=True)  # never re-rendered in the UI once saved - see the router's masking helper
    base_url = Column(String(500), nullable=True)  # Ollama host / Azure endpoint / OpenAI-compatible base URL
    model_name = Column(String(255), nullable=True)  # model string / Ollama model / Azure deployment name
    api_version = Column(String(50), nullable=True)  # Azure OpenAI specific (e.g. "2024-10-21")
    is_active = Column(Boolean, default=False, nullable=False)  # the provider used for marking (the highest-stakes task) and anything without a more specific category
    is_active_for_spec_parsing = Column(Boolean, default=False, nullable=False)  # optional separate override, same idea as the old SPEC_PARSING_LLM_PROVIDER env var
    is_active_for_draft_generation = Column(Boolean, default=False, nullable=False)  # optional override for scheme-of-work/lesson DRAFT generation - see llm_gateway.py's DRAFT_GENERATION_DEFAULT_PROVIDER for why this category defaults to Ollama even once a paid provider is active for marking
    created_by_teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, nullable=True)


class LoginOTP(Base):
    """
    A short-lived one-time code emailed at login when 2FA applies (personal opt-in via
    Settings, or forced on by SystemSettings.two_factor_mandatory). Only the HASH of the
    code is stored, the same principle as a password - if the database were ever read
    directly, no valid code should be recoverable from it. Expired/used rows are cleaned
    up lazily (see auth logic) rather than needing a separate scheduled job.
    """
    __tablename__ = "login_otps"
    id = Column(Integer, primary_key=True)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    code_hash = Column(String(255), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used = Column(Boolean, default=False, nullable=False)
    attempts = Column(Integer, default=0, nullable=False)  # locks the code out after too many wrong guesses - see otp.py
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class RetentionPolicy(Base):
    """
    One row per known data category (a fixed set defined in code, since each category
    needs real purge logic written for it - not an arbitrary admin-created list). The
    NUMBERS are what an admin configures here, matching the exam board / awarding body
    retention period they've actually been told to use - which varies by college and by
    qualification, so it was deliberately never hardcoded.

    Deliberately safe by default: enabled=False until an admin turns each category on -
    nothing is ever auto-deleted just because this table exists.

    Two-stage soft delete: once something exceeds retention_days, it's marked (not yet
    deleted) - grace_period_days later, it's actually purged, unless an admin exempted it
    first. This is a real, meaningful gap between "eligible" and "gone" specifically so a
    genuine mistake or an in-progress appeal can be caught before anything is irreversible.
    """
    __tablename__ = "retention_policies"
    id = Column(Integer, primary_key=True)
    category = Column(String(50), unique=True, nullable=False)  # "submission_files" | "lesson_decks" - see retention.py's RETENTION_CATEGORIES for what each one covers
    retention_days = Column(Integer, nullable=True)  # age (from submission/generation) before something becomes eligible to be marked for deletion. Null/disabled means no policy active for this category yet.
    grace_period_days = Column(Integer, default=7, nullable=False)  # days between being MARKED for deletion and actually PURGED - the window an admin can still exempt something in
    enabled = Column(Boolean, default=False, nullable=False)
    updated_at = Column(DateTime, nullable=True)
    updated_by_teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=True)


class DiscoveredSpecFormat(Base):
    """
    A parsing pattern the AI figured out once for a spec format neither hardcoded rule-set
    (BTEC, T-Level) recognised. Tried BEFORE falling back to full-document AI chunking on
    every future upload of a similarly-structured spec - this is the actual "learning"
    mechanism: the tool gets faster at new formats over time instead of needing manual
    regex work for each one.
    """
    __tablename__ = "discovered_spec_formats"
    id = Column(Integer, primary_key=True)
    label = Column(String(255), nullable=False)
    unit_header_regex = Column(Text, nullable=False)
    sub_heading_regex = Column(Text, nullable=True)
    criterion_regex = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    # Stage 5: a TABLE layout (header cells, column roles, unit heading rule) as JSON text.
    # When set, the regex columns above are empty strings and this is what gets used.
    layout_json = Column(Text, nullable=True)
    times_used = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class PracticeSet(Base):
    """
    Stage 6 (A): a set of exam-style practice questions for an exam-assessed course (A level,
    GCSE), written by AI from the spec's own statements and assessment objectives. Each
    question carries its marks, the objective(s) it tests (AO1-AO4) and a mark scheme. A
    lecturer reviews them before use - they are drafts, like lesson decks.
    """
    __tablename__ = "practice_sets"
    id = Column(Integer, primary_key=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    unit_name = Column(String(500), nullable=False)
    topic_titles = Column(JSON, nullable=True)          # the outcomes (topics) the questions are about
    question_count = Column(Integer, default=6, nullable=False)
    questions_json = Column(JSON, nullable=True)        # [{"question", "context", "marks", "ao": [...], "mark_scheme": [...], "levels": [...]}]
    status = Column(String(50), default="generating", nullable=False)  # generating | ready | failed
    error_detail = Column(Text, nullable=True)
    generated_by_provider = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class QuizSet(Base):
    """
    Stage 8: a live multiple-choice quiz (Kahoot-style) - written by AI from a lesson deck's
    content and checked by the lecturer, or typed in by hand. Run live: a countdown, points for
    right and quick answers, the answer revealed with how the class voted, and a leaderboard.
    """
    __tablename__ = "quiz_sets"
    id = Column(Integer, primary_key=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    lesson_deck_id = Column(Integer, ForeignKey("lesson_decks.id"), nullable=True)
    title = Column(String(255), nullable=False)
    questions_json = Column(JSON, nullable=True)        # [{"question", "choices": [...], "answer": index, "explanation"}]
    seconds_per_question = Column(Integer, default=20, nullable=False)
    status = Column(String(50), default="ready", nullable=False)  # generating | ready | failed
    error_detail = Column(Text, nullable=True)
    generated_by_provider = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class MarkingInsight(Base):
    """
    Stage 10: the AI's summary of the COMMON problems behind the criteria a class found hard in
    one unit's marked work (from the markers' comments, with names removed), plus a re-teaching
    tip per criterion. The counts themselves are always worked out fresh from the marks - only
    this summary is stored, so it can be shown without asking the AI again.
    """
    __tablename__ = "marking_insights"
    id = Column(Integer, primary_key=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False, index=True)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    unit_name = Column(String(500), nullable=False)
    status = Column(String(50), default="generating", nullable=False)  # generating | ready | failed
    insight_json = Column(JSON, nullable=True)       # {criterion code: {"common_problems": [...], "reteach_tip": str}}
    based_on_count = Column(Integer, nullable=True)  # how many students' marked work it was based on
    error_detail = Column(Text, nullable=True)
    generated_by_provider = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class LiveSession(Base):
    """
    Stage 6 (B): a live, interactive lesson - the lecturer presents a lesson deck (or a
    practice question set) and students join on their own phone or laptop with a short code,
    no account needed. They answer the deck's own questions (starter, check, plenary) and a
    confidence poll; the lecturer sees the answers arrive live and keeps a summary.
    """
    __tablename__ = "live_sessions"
    id = Column(Integer, primary_key=True)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    lesson_deck_id = Column(Integer, ForeignKey("lesson_decks.id"), nullable=True)
    practice_set_id = Column(Integer, ForeignKey("practice_sets.id"), nullable=True)
    quiz_set_id = Column(Integer, ForeignKey("quiz_sets.id"), nullable=True)   # Stage 8
    title = Column(String(255), nullable=False)
    join_code = Column(String(12), nullable=False, index=True)
    screens_json = Column(JSON, nullable=True)           # a snapshot of what was shown, taken at the start - the summary stays right even if the deck is later regenerated or deleted
    current_screen = Column(Integer, default=0, nullable=False)
    screen_started_at = Column(DateTime, nullable=True)  # Stage 8: when the current screen was shown - quiz countdown and speed points
    revealed_screens = Column(JSON, nullable=True)       # screens whose answers the lecturer has shown to the class
    state = Column(String(20), default="open", nullable=False)  # open | paused (a break - students wait) | ended (can be continued)
    started_at = Column(DateTime, nullable=True)  # when the lecturer actually started teaching - null while it's still the join screen
    groups_json = Column(JSON, nullable=True)     # small-group work while it's on: {"size": 3, "groups": {participant id: group number}}
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    ended_at = Column(DateTime, nullable=True)


class LiveParticipant(Base):
    """A student in a live session: only the first name / nickname they type, and a random
    token kept in their browser so their answers stay theirs. No account, no other data."""
    __tablename__ = "live_participants"
    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("live_sessions.id"), nullable=False, index=True)
    display_name = Column(String(40), nullable=False)
    token = Column(String(64), nullable=False, index=True)
    joined_at = Column(DateTime, default=datetime.datetime.utcnow)


class LiveResponse(Base):
    """One answer to one question in a live session (a student can change it while the
    question is open - the latest answer is kept)."""
    __tablename__ = "live_responses"
    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("live_sessions.id"), nullable=False, index=True)
    participant_id = Column(Integer, ForeignKey("live_participants.id"), nullable=False)
    screen_index = Column(Integer, nullable=False)
    question_key = Column(String(20), nullable=False)
    answer_text = Column(Text, nullable=True)
    answer_choice = Column(Integer, nullable=True)
    points = Column(Integer, nullable=True)  # Stage 8: quiz points (right + speed); empty for other questions
    group_no = Column(Integer, nullable=True)  # the student's group when this was answered during group work
    image_path = Column(String(500), nullable=True)  # a photo with the answer (re-saved, location data removed) - lecturer only
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class TermPlan(Base):
    """
    Plan the term: many weeks of a scheme of work turned into lessons (and quizzes) in one go,
    made one after another in the background. items_json lists the weeks and the lesson/quiz
    made for each - their progress is read from those rows, so this only records the plan.
    """
    __tablename__ = "term_plans"
    id = Column(Integer, primary_key=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False, index=True)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    sow_id = Column(Integer, ForeignKey("schemes_of_work.id"), nullable=False)
    options_json = Column(JSON, nullable=True)   # {"minutes", "differentiate", "quiz", "quiz_count", "practicals", "join_up"}
    items_json = Column(JSON, nullable=True)     # [{"week", "title", "deck_id", "quiz_id"}] in teaching order
    state = Column(String(20), default="running", nullable=False)  # running | stopped | done
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Homework(Base):
    """
    A quiz set as homework: students open a link on their own phone or laptop, find themselves
    on the class list and answer in their own time - no timer, no leaderboard. The questions
    are copied in when it is set, so editing the quiz later never changes marked results.
    """
    __tablename__ = "homeworks"
    id = Column(Integer, primary_key=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False, index=True)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    quiz_set_id = Column(Integer, ForeignKey("quiz_sets.id"), nullable=True)
    title = Column(String(255), nullable=False)
    code = Column(String(12), nullable=False, index=True)
    questions_json = Column(JSON, nullable=True)  # [{"question", "choices", "answer", "explanation"}]
    due_at = Column(DateTime, nullable=True)
    show_answers = Column(Boolean, default=True, nullable=False)  # right answers + explanations shown after handing in
    is_open = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class HomeworkAttempt(Base):
    """One student's go at a homework: their answers, score, and a random token kept in their
    browser so the attempt stays theirs. One per student - the lecturer can reset it."""
    __tablename__ = "homework_attempts"
    id = Column(Integer, primary_key=True)
    homework_id = Column(Integer, ForeignKey("homeworks.id"), nullable=False, index=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False, index=True)
    token = Column(String(64), nullable=False, index=True)
    answers_json = Column(JSON, nullable=True)  # {"0": choice index, ...}
    score = Column(Integer, nullable=True)
    total = Column(Integer, nullable=True)
    started_at = Column(DateTime, default=datetime.datetime.utcnow)
    submitted_at = Column(DateTime, nullable=True)


class LicenceRecord(Base):
    """A TIGARSEC licence installed on this copy (app/licence.py). Kept forever as history; the
    one in force is worked out from the dates each time. Only the TIGARSEC console (/vendor)
    shows these - never college admins."""
    __tablename__ = "licence_records"
    id = Column(Integer, primary_key=True)
    licence_id = Column(String(64), nullable=False, index=True)
    raw = Column(Text, nullable=False)  # the signed file exactly as issued - re-checked on every load
    installed_at = Column(DateTime, default=datetime.datetime.utcnow)
    installed_by = Column(String(100), nullable=False, default="TIGARSEC")  # "TIGARSEC console" or "file drop"


class LicenceMeta(Base):
    """One row (id=1): this installation's identity and licence bookkeeping."""
    __tablename__ = "licence_meta"
    id = Column(Integer, primary_key=True)
    install_id = Column(String(64), nullable=False)
    first_started_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    last_stage = Column(String(30), nullable=True)
    reminders_sent = Column(JSON, nullable=True)  # ["CC-2026-0007:60", ...] so each email goes once


class LicenceEvent(Base):
    """Licensing history (installs, stage changes, console sign-ins). Its own table, NOT the
    audit log, because college admins can read the audit log and licensing is TIGARSEC-only."""
    __tablename__ = "licence_events"
    id = Column(Integer, primary_key=True)
    action = Column(String(60), nullable=False)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)
