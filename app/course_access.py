from sqlalchemy.orm import Session

from .models import Course, CourseTeacher


def get_course_for_teacher(db: Session, course_id: int, teacher) -> Course | None:
    """
    Returns the Course if this teacher manages it, else None - the single, shared
    replacement for the same "does this course belong to this teacher" check that used
    to be repeated by hand at 24 separate call sites across 8 files. Every one of those
    now calls this instead, so there is exactly one place this logic lives and one place
    to get it right.

    Deliberately strict: membership in CourseTeacher is the only path to access, no admin
    override case - matches the ownership model already used everywhere else in this app
    (submissions, students, schemes of work all check exact ownership with no admin
    exception), so this doesn't quietly introduce a broader one. An admin who needs access
    to a specific course needs to actually be assigned to it, the same as any other
    teacher.
    """
    return (
        db.query(Course)
        .join(CourseTeacher, CourseTeacher.course_id == Course.id)
        .filter(Course.id == course_id, CourseTeacher.teacher_id == teacher.id)
        .first()
    )


def get_courses_for_teacher(db: Session, teacher) -> list[Course]:
    """All courses this teacher manages, alphabetically - the shared replacement for the
    "list my courses" query that used to filter on Course.teacher_id directly."""
    return (
        db.query(Course)
        .join(CourseTeacher, CourseTeacher.course_id == Course.id)
        .filter(CourseTeacher.teacher_id == teacher.id)
        .order_by(Course.name)
        .all()
    )
