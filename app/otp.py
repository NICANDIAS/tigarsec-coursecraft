"""
One-time code generation/verification for 2FA login. Kept separate from auth_router.py
so the actual logic (generate, hash, verify, expire) can be tested directly without
spinning up a request.
"""
import datetime
import secrets

from sqlalchemy.orm import Session
from passlib.context import CryptContext

from .models import LoginOTP

_otp_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

OTP_LENGTH = 6
OTP_VALID_MINUTES = 10
OTP_MAX_ATTEMPTS_PER_CODE = 5  # generous but bounded - stops brute-forcing a 6-digit code


def generate_code() -> str:
    """A random 6-digit numeric code, zero-padded (e.g. '004821') - cryptographically
    random, not just random.randint, since this is a real auth factor."""
    return f"{secrets.randbelow(1_000_000):06d}"


def create_and_store_otp(db: Session, teacher_id: int) -> str:
    """Creates a new OTP row for this teacher and returns the PLAIN code (to email) -
    only the hash is persisted. Any previous unused codes for this teacher are invalidated
    first, so only the most recently requested code ever works."""
    db.query(LoginOTP).filter(LoginOTP.teacher_id == teacher_id, LoginOTP.used == False).update({"used": True})  # noqa: E712

    code = generate_code()
    otp = LoginOTP(
        teacher_id=teacher_id,
        code_hash=_otp_context.hash(code),
        expires_at=datetime.datetime.utcnow() + datetime.timedelta(minutes=OTP_VALID_MINUTES),
        used=False,
    )
    db.add(otp)
    db.commit()
    return code


def verify_otp(db: Session, teacher_id: int, submitted_code: str) -> bool:
    """
    Checks the submitted code against this teacher's most recent unused, unexpired OTP.
    Marks it used on success (one-time, can't be replayed). Also tracks failed attempts
    per code and locks it out after OTP_MAX_ATTEMPTS_PER_CODE wrong guesses, regardless of
    whether the NEXT guess would have been correct - without this, a code stays guessable
    for its full validity window with no limit on attempts, which defeats the point of a
    6-digit code being hard to brute-force.
    """
    otp = (
        db.query(LoginOTP)
        .filter(LoginOTP.teacher_id == teacher_id, LoginOTP.used == False)  # noqa: E712
        .order_by(LoginOTP.id.desc())
        .first()
    )
    if not otp:
        return False
    if otp.expires_at < datetime.datetime.utcnow():
        otp.used = True
        db.commit()
        return False
    if otp.attempts >= OTP_MAX_ATTEMPTS_PER_CODE:
        otp.used = True
        db.commit()
        return False

    otp.attempts += 1
    is_correct = _otp_context.verify(submitted_code.strip(), otp.code_hash)
    if is_correct:
        otp.used = True
    db.commit()
    return is_correct
