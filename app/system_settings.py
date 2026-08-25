from sqlalchemy.orm import Session

from .models import SystemSettings

_DEFAULT_SETTINGS = SystemSettings(id=1, allowed_email_domains=None, two_factor_mandatory=False)


def get_system_settings(db: Session) -> SystemSettings:
    """
    Gets the single settings row (always id=1), creating it with permissive defaults
    (no email restriction, 2FA not mandatory) if it doesn't exist yet - so a fresh install
    behaves exactly as before until an admin actively changes something here.

    Defensive against the table itself not existing yet (main.py now syncs schema
    automatically on startup, so this should be rare - but this is what a real deployment
    lockout looked like: this table missing broke a page that's needed to fix it. Never
    let that repeat - fall back to safe, permissive in-memory defaults rather than raising,
    so nothing that calls this can become a new single point of failure the same way.
    """
    try:
        settings = db.get(SystemSettings, 1)
        if settings is None:
            settings = SystemSettings(id=1, allowed_email_domains=None, two_factor_mandatory=False)
            db.add(settings)
            db.commit()
            db.refresh(settings)
        return settings
    except Exception:
        db.rollback()
        return _DEFAULT_SETTINGS


def parse_allowed_domains(raw: str | None) -> list[str]:
    """Comma-separated domains -> a clean list, lowercased, no leading '@', empty entries
    dropped. Returns [] for "no restriction" (matches the empty/null convention used
    everywhere this is read)."""
    if not raw or not raw.strip():
        return []
    domains = []
    for part in raw.split(","):
        d = part.strip().lower().lstrip("@")
        if d:
            domains.append(d)
    return domains


def email_domain_allowed(email: str, allowed_domains: list[str]) -> bool:
    """No restriction configured = everything allowed (the original, permissive default).
    Domain match is case-insensitive and exact (the part after the last '@')."""
    if not allowed_domains:
        return True
    if "@" not in email:
        return False
    email_domain = email.strip().lower().rsplit("@", 1)[-1]
    return email_domain in allowed_domains
