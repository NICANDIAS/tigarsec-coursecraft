"""
Data retention: soft-delete with an admin-configurable grace period, disabled by default
per category. See RetentionPolicy in models.py for the full design reasoning.

RETENTION_CATEGORIES defines what each category covers and its age anchor. Adding a new
category means adding both an entry here and real purge/query logic below - not something
an admin can improvise through the UI, since each category needs code that knows exactly
what to delete and what to keep.

Decision functions (find_*) are kept pure/query-only so they can be tested against a real
session without needing to actually touch the filesystem or send email. Side-effecting
functions (purge_*, send_*) are separate.
"""
import os
import datetime

from sqlalchemy.orm import Session

from .models import RetentionPolicy, Submission, LessonDeck, Teacher, AuditLogEntry
from .email_sender import send_email, is_configured as email_is_configured

WARN_WITHIN_DAYS = 7  # how far ahead of the actual purge date daily reminder emails start

RETENTION_CATEGORIES = {
    "submission_files": {
        "label": "Student submission files",
        "description": (
            "The uploaded files themselves (and any annotated copy) - not the marks, "
            "grades, or feedback, which are always kept as the permanent academic record "
            "regardless of this setting. Age is counted from when the submission was "
            "uploaded."
        ),
    },
    "lesson_decks": {
        "label": "Generated lesson PowerPoints",
        "description": (
            "Generated teaching materials - these don't contain student data, so this is "
            "purely storage housekeeping, not a compliance requirement. Age is counted "
            "from when the deck was generated."
        ),
    },
}


def get_or_create_policies(db: Session) -> dict:
    """Returns {category: RetentionPolicy row}, creating a disabled default row (7-day
    grace period, no retention period set) for any known category that doesn't have one
    yet - so nothing is ever active until an admin deliberately turns it on."""
    existing = {p.category: p for p in db.query(RetentionPolicy).all()}
    changed = False
    for category in RETENTION_CATEGORIES:
        if category not in existing:
            policy = RetentionPolicy(category=category, retention_days=None, grace_period_days=7, enabled=False)
            db.add(policy)
            existing[category] = policy
            changed = True
    if changed:
        db.commit()
    return existing


def find_newly_eligible(db: Session, model, policy: RetentionPolicy, now: datetime.datetime) -> list:
    """Rows old enough to be marked for deletion - not already marked, not already purged,
    not exempted. Works for both Submission and LessonDeck since they share the same three
    field names (created_at, marked_for_deletion_at, purged_at, retention_exempt)."""
    if not policy.enabled or policy.retention_days is None:
        return []
    cutoff = now - datetime.timedelta(days=policy.retention_days)
    return (
        db.query(model)
        .filter(
            model.created_at < cutoff,
            model.marked_for_deletion_at.is_(None),
            model.purged_at.is_(None),
            model.retention_exempt == False,  # noqa: E712
        )
        .all()
    )


def find_purge_ready(db: Session, model, policy: RetentionPolicy, now: datetime.datetime) -> list:
    """Rows already marked for deletion whose grace period has fully elapsed."""
    if not policy.enabled:
        return []
    cutoff = now - datetime.timedelta(days=policy.grace_period_days)
    return (
        db.query(model)
        .filter(
            model.marked_for_deletion_at.isnot(None),
            model.marked_for_deletion_at < cutoff,
            model.purged_at.is_(None),
        )
        .all()
    )


def find_upcoming_purges(db: Session, model, policy: RetentionPolicy, now: datetime.datetime, warn_within_days: int = WARN_WITHIN_DAYS) -> list:
    """Rows marked for deletion with a purge date inside the warning window - returns
    (row, purge_date, days_until_purge) tuples, used to build the daily reminder digest."""
    if not policy.enabled:
        return []
    rows = db.query(model).filter(model.marked_for_deletion_at.isnot(None), model.purged_at.is_(None)).all()
    result = []
    for row in rows:
        purge_date = row.marked_for_deletion_at + datetime.timedelta(days=policy.grace_period_days)
        days_until = (purge_date - now).days
        if 0 <= days_until <= warn_within_days:
            result.append((row, purge_date, days_until))
    return result


def purge_submission_files(db: Session, submission: Submission, now: datetime.datetime) -> None:
    """Removes the uploaded files from disk and clears their paths. The Submission row,
    its marks, and its grade are ALL kept intact - this purges evidence FILES, never the
    academic record itself."""
    removed_names = []
    for f in list(submission.files):
        if f.stored_path and os.path.exists(f.stored_path):
            try:
                os.remove(f.stored_path)
                removed_names.append(f.original_filename)
            except OSError:
                pass
        db.delete(f)
    if submission.stored_path and os.path.exists(submission.stored_path):
        try:
            os.remove(submission.stored_path)
        except OSError:
            pass
    if submission.annotated_path and os.path.exists(submission.annotated_path):
        try:
            os.remove(submission.annotated_path)
        except OSError:
            pass
    submission.stored_path = None
    submission.annotated_path = None
    submission.purged_at = now
    db.add(AuditLogEntry(
        submission_id=submission.id, actor="retention_policy", action="submission_files_purged",
        detail=f"Files removed per retention policy ({len(removed_names)} file(s): "
               f"{', '.join(removed_names) or 'none found on disk'}). The submission record, "
               f"marks, and grade are unaffected and remain queryable.",
    ))


def purge_lesson_deck(db: Session, deck: LessonDeck, now: datetime.datetime) -> None:
    """Removes the generated PowerPoint from disk. Keeps the row itself as a lightweight
    historical trace (title, course, dates) rather than deleting it outright."""
    if deck.pptx_path and os.path.exists(deck.pptx_path):
        try:
            os.remove(deck.pptx_path)
        except OSError:
            pass
    deck.pptx_path = None
    deck.outline_json = None
    deck.status = "purged"
    deck.purged_at = now
    db.add(AuditLogEntry(
        actor="retention_policy", action="lesson_deck_purged",
        detail=f"Lesson deck '{deck.title}' (course {deck.course_id}) purged per retention policy.",
    ))


PURGE_FUNCTIONS = {
    "submission_files": (Submission, purge_submission_files),
    "lesson_decks": (LessonDeck, purge_lesson_deck),
}


def describe_row(category: str, row) -> str:
    if category == "submission_files":
        return f"Submission #{row.id} ({row.original_filename}, student ID {row.student_id})"
    return f"Lesson deck '{row.title}' (course ID {row.course_id})"


def send_daily_reminder(db: Session, upcoming_by_category: dict) -> None:
    """
    One digest email per day to every admin - a summary COUNT only, deliberately no
    filenames, submission IDs, or student references. Standing fix: this used to list
    every pending item by name, which put that detail on a channel with much weaker
    access control than the app itself - an email can be forwarded, sits in an inbox
    under different retention rules, and reaches every admin whether or not they had any
    reason to look at that specific submission. The full breakdown belongs behind the
    same admin-only login the rest of the app already requires, not broadcast over email.
    Only sent if there's actually something in the warning window.
    """
    total = sum(len(v) for v in upcoming_by_category.values())
    if total == 0 or not email_is_configured():
        return

    lines = [
        "Items are scheduled to be permanently purged soon, per your data retention policy.",
        "Nothing has been deleted yet - this is advance notice so there's time to review or",
        "exempt anything that shouldn't go. Details are only shown in the app, not here.",
        "",
    ]
    for category, items in upcoming_by_category.items():
        if not items:
            continue
        label = RETENTION_CATEGORIES[category]["label"]
        soonest = min(days_until for _, _, days_until in items)
        lines.append(f"- {label}: {len(items)} item(s), soonest in {soonest} day{'s' if soonest != 1 else ''}")
    lines.append("")
    lines.append("Review, or exempt anything that shouldn't be purged, from Admin: Data Retention.")
    body = "\n".join(lines)

    admins = db.query(Teacher).filter(Teacher.role == "admin", Teacher.is_active == True).all()  # noqa: E712
    for admin in admins:
        try:
            send_email(admin.email, f"CourseCraft: {total} item(s) scheduled for deletion soon", body)
        except Exception as e:
            print(f"[retention] Failed to send reminder to {admin.email}: {e}", flush=True)


def run_retention_check(db: Session) -> dict:
    """
    The full daily cycle: mark newly-eligible items, purge anything whose grace period has
    elapsed, and send the reminder digest for anything upcoming. Returns a summary dict
    (mainly for logging/testing) rather than anything the caller needs to act on.
    """
    now = datetime.datetime.utcnow()
    policies = get_or_create_policies(db)
    summary = {"marked": 0, "purged": 0, "upcoming": 0}
    upcoming_by_category = {}

    for category, (model, purge_fn) in PURGE_FUNCTIONS.items():
        policy = policies[category]

        newly_eligible = find_newly_eligible(db, model, policy, now)
        for row in newly_eligible:
            row.marked_for_deletion_at = now
            db.add(AuditLogEntry(
                submission_id=row.id if category == "submission_files" else None,
                actor="retention_policy", action=f"{category}_marked_for_deletion",
                detail=f"{describe_row(category, row)} exceeded the {policy.retention_days}-day "
                       f"retention period - scheduled for purge in {policy.grace_period_days} days "
                       f"unless exempted.",
            ))
        summary["marked"] += len(newly_eligible)

        ready = find_purge_ready(db, model, policy, now)
        for row in ready:
            purge_fn(db, row, now)
        summary["purged"] += len(ready)

        upcoming_by_category[category] = find_upcoming_purges(db, model, policy, now)
        summary["upcoming"] += len(upcoming_by_category[category])

    db.commit()
    send_daily_reminder(db, upcoming_by_category)
    return summary
