import os
import datetime
import secrets as _secrets
import jwt
from passlib.context import CryptContext
from fastapi import Depends, Request
from sqlalchemy.orm import Session
from .database import get_db
from .models import Teacher

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 12

# The four roles you asked for. "admin" can allocate roles to others; the rest are
# just labels for now (see README - deeper permission differences between curriculum
# lead/manager/lecturer aren't built yet, this is the foundation for that).
ROLES = ["admin", "curriculum_manager", "curriculum_lead", "lecturer"]

_PLACEHOLDER_VALUES = {"", "change-me-in-production", "replace-this-with-a-long-random-string"}


def _get_or_create_secret_key() -> str:
    """
    Uses SECRET_KEY from the environment if it's a real value. Otherwise generates one
    automatically and persists it to disk so sessions survive container restarts -
    this is what lets the whole stack run with zero manual setup via `docker compose up`.
    """
    env_value = os.getenv("SECRET_KEY", "").strip()
    if env_value and env_value not in _PLACEHOLDER_VALUES:
        return env_value

    secret_path = os.path.join("data", ".generated_secret_key")
    if os.path.exists(secret_path):
        with open(secret_path, "r") as f:
            existing = f.read().strip()
            if existing:
                return existing

    generated = _secrets.token_hex(32)
    os.makedirs("data", exist_ok=True)
    with open(secret_path, "w") as f:
        f.write(generated)
    return generated


SECRET_KEY = _get_or_create_secret_key()

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class NotAuthenticated(Exception):
    """Raised by page routes when there's no valid session. Handled in main.py
    with a redirect to /login instead of a raw 401 (this is a browser UI, not a pure API)."""
    pass


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(teacher_id: int) -> str:
    expire = datetime.datetime.utcnow() + datetime.timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    payload = {"sub": str(teacher_id), "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


PENDING_2FA_EXPIRE_MINUTES = 10


def create_pending_2fa_token(teacher_id: int) -> str:
    """
    Issued right after a correct password but BEFORE the OTP is verified - deliberately
    uses a different claim ("pending_2fa" rather than "sub") than the real session token,
    so get_current_teacher (which only ever reads "sub") can never be tricked into
    accepting a pending token as a real logged-in session, even if it ended up in the
    wrong cookie by mistake. Short-lived (10 minutes) since it exists only to survive the
    one redirect to the code-entry page.
    """
    expire = datetime.datetime.utcnow() + datetime.timedelta(minutes=PENDING_2FA_EXPIRE_MINUTES)
    payload = {"pending_2fa": str(teacher_id), "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def read_pending_2fa_token(token: str | None) -> int | None:
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        teacher_id = payload.get("pending_2fa")
        return int(teacher_id) if teacher_id is not None else None
    except jwt.PyJWTError:
        return None


def get_current_teacher(request: Request, db: Session = Depends(get_db)) -> Teacher:
    token = request.cookies.get("access_token")
    if not token:
        raise NotAuthenticated()
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        teacher_id = payload.get("sub")
    except jwt.PyJWTError:
        raise NotAuthenticated()
    teacher = db.query(Teacher).filter(Teacher.id == int(teacher_id)).first()
    if not teacher or not teacher.is_active:
        raise NotAuthenticated()
    return teacher


class NotAuthorized(Exception):
    """Raised when a logged-in teacher tries to access an admin-only page."""
    pass


def require_admin(teacher: Teacher = Depends(get_current_teacher)) -> Teacher:
    if teacher.role != "admin":
        raise NotAuthorized()
    return teacher


# Item 11: only admin/curriculum_manager can add to the shared spec library, so
# lecturers don't each upload their own copy of the same document.
SPEC_UPLOADER_ROLES = {"admin", "curriculum_manager"}


def require_spec_uploader(teacher: Teacher = Depends(get_current_teacher)) -> Teacher:
    if teacher.role not in SPEC_UPLOADER_ROLES:
        raise NotAuthorized()
    return teacher


# Course creation moved here from any teacher self-serving their own course record -
# confirmed as a real, necessary change: two teachers covering different content areas of
# the same real cohort were each creating their own separate course, splitting students
# and progress tracking across disconnected copies of what should be one course. Same two
# roles as SPEC_UPLOADER_ROLES today, kept as its own named constant rather than reusing
# that one directly - these are conceptually distinct permissions (creating a course vs
# uploading a spec) even though they happen to share the same role set right now.
COURSE_MANAGER_ROLES = {"admin", "curriculum_manager"}


def require_course_manager(teacher: Teacher = Depends(get_current_teacher)) -> Teacher:
    if teacher.role not in COURSE_MANAGER_ROLES:
        raise NotAuthorized()
    return teacher


# Known in-development features that can be granted to a specific person early, while
# still showing "Coming Soon" to everyone else - a fixed, code-defined set (not
# admin-creatable) since each key needs an actual route/page decision behind it, same
# reasoning as RETENTION_CATEGORIES and PURGE_FUNCTIONS in retention.py. Add a new entry
# here when starting real work on a "Soon" item, then gate that feature's real route with
# has_early_access() - the nav link swap alone is not access control by itself, a direct
# URL guess must be blocked at the route too.
EARLY_ACCESS_FEATURES = {
    "timetabling": "Timetabling",
    "teams_integration": "Teams integration",
}


def has_early_access(teacher: Teacher, feature_key: str) -> bool:
    """Whether this specific person has been granted early access to an in-development
    feature. Deliberately per-person (checking the teacher's own granted list), not
    per-role - the point is inviting a specific reviewer in, not opening it to everyone
    who happens to share that role."""
    return feature_key in (teacher.early_access_features or [])


def require_timetabling_access(teacher: Teacher = Depends(get_current_teacher)) -> Teacher:
    """
    Admin always has access to manage an in-development feature they're actively
    building, regardless of early-access grants (gating the builder out of their own
    work-in-progress would be self-defeating) - plus anyone specifically granted early
    access via EARLY_ACCESS_FEATURES/has_early_access above (e.g. a curriculum manager
    reviewing progress).
    """
    if teacher.role == "admin" or has_early_access(teacher, "timetabling"):
        return teacher
    raise NotAuthorized()
