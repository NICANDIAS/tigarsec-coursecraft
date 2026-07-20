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
    pass_threshold_pct = Column(Float, nullable=True)
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
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    courses = relationship("Course", back_populates="teacher")


class Course(Base):
    __tablename__ = "courses"
    id = Column(Integer, primary_key=True)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    name = Column(String(255), nullable=False)
    # A course attaches to ONE spec from the shared library at a time (can be changed).
    # This replaces v1's per-course spec upload - specs are now uploaded once, centrally,
    # and any teacher selects from the library rather than re-uploading the same document.
    spec_version_id = Column(Integer, ForeignKey("spec_versions.id"), nullable=True)
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
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class SchemeOfWork(Base):
    __tablename__ = "schemes_of_work"
    id = Column(Integer, primary_key=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    spec_version_id = Column(Integer, ForeignKey("spec_versions.id"), nullable=True)
    title = Column(String(255), nullable=False)
    content_area = Column(Text, nullable=True)
    selected_unit_names = Column(JSON, nullable=True)  # stored so "Retry" can rebuild the same request
    format_sample = Column(Text, nullable=True)
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    weeks_requested = Column(Integer, nullable=True)  # the actual (capped) week count used
    planned_assessments = Column(JSON, nullable=True)  # teacher-specified checkpoints: [{"week": int, "label": str}, ...] - optional, generation still works fine without any
    include_practicals = Column(Boolean, default=False, nullable=False)
    practicals_notes = Column(Text, nullable=True)
    design_assignments = Column(Boolean, default=False, nullable=False)
    extra_notes = Column(Text, nullable=True)
    weeks_json = Column(JSON, nullable=True)
    status = Column(String(50), default="generating", nullable=False)  # generating, ready, failed
    error_detail = Column(Text, nullable=True)
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
    section_results = Column(JSON, nullable=True)  # computed after marking: {"MCQ": {"total":.., "max":.., "pct":.., "passed":..}, ...} plus "overall_passed"
    rubric_file_path = Column(String(1000), nullable=True)  # the ACTUAL uploaded rubric file - filled in place, never rebuilt as a new document
    similarity_warning = Column(Text, nullable=True)  # set automatically at upload time if this matches another student's submission for the same assignment above the threshold
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
    final_mark = Column(String(100), nullable=True)
    final_comment = Column(Text, nullable=True)
    overridden = Column(Boolean, default=False)

    marking_run = relationship("MarkingRun", back_populates="criterion_marks")


class TeacherOverride(Base):
    __tablename__ = "teacher_overrides"
    id = Column(Integer, primary_key=True)
    criterion_mark_id = Column(Integer, ForeignKey("criterion_marks.id"), nullable=False)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    ai_mark = Column(String(100))
    final_mark = Column(String(100))
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
    source_sow_id = Column(Integer, ForeignKey("schemes_of_work.id"), nullable=True)  # if generated from a specific scheme-of-work week, lets that week link straight back to this deck
    source_week = Column(Integer, nullable=True)
    fits_in_requested_time = Column(Boolean, nullable=True)  # the AI's own assessment - null until generation completes
    scope_note = Column(Text, nullable=True)  # explains what's covered vs what needs a follow-up session, if it doesn't fit
    outline_json = Column(JSON, nullable=True)
    pptx_path = Column(String(1000), nullable=True)
    status = Column(String(50), default="queued", nullable=False)  # queued, generating, ready, failed
    error_detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    course = relationship("Course")


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
    times_used = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
