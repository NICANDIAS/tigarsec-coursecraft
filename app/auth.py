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
