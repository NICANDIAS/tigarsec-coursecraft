"""
SMTP email sending - no third-party email service dependency, since the whole point
(matching the AI provider settings) is letting a college plug in whatever mail relay they
already have rather than requiring a specific vendor.

Configurable two ways, same priority as AI providers:
1. Admin: System Settings, from the UI - lets an admin fix email sending without needing
   server/terminal access, which matters a lot for the mandatory-2FA-lockout scenario:
   if SMTP breaks after being configured, an admin needs to be able to repair it from
   inside the app, not only by editing .env on a server they might not have access to.
2. Env vars (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM, SMTP_USE_TLS) -
   the fallback if nothing is configured via the UI, so a deployment that only ever used
   .env keeps working exactly as before.

If neither is configured, send_email raises a clear RuntimeError rather than failing
silently - a 2FA code that was never actually sent is exactly the kind of failure that
must be loud, not swallowed.
"""
import os
import smtplib
from email.mime.text import MIMEText

SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "").strip() or SMTP_USER
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").strip().lower() != "false"


def _resolve_config() -> dict:
    """DB-configured settings (Admin: System Settings) take priority if smtp_host is set
    there; otherwise falls back to env vars. Defensive against the table/column not
    existing yet (e.g. before Schema Check/the startup sync has run) - same pattern as
    llm_gateway.py's provider resolution, for the same reason: never let a missing-schema
    edge case become a crash in something as central as sending an email."""
    try:
        from .database import SessionLocal
        from .models import SystemSettings
        db = SessionLocal()
        try:
            settings = db.get(SystemSettings, 1)
            if settings and settings.smtp_host:
                return {
                    "host": settings.smtp_host,
                    "port": settings.smtp_port or 587,
                    "user": settings.smtp_user or "",
                    "password": settings.smtp_password or "",
                    "from_addr": settings.smtp_from or settings.smtp_user or "",
                    "use_tls": settings.smtp_use_tls if settings.smtp_use_tls is not None else True,
                }
        finally:
            db.close()
    except Exception:
        pass  # table/column missing, DB unreachable, etc. - fall through to env vars below

    return {
        "host": SMTP_HOST, "port": SMTP_PORT, "user": SMTP_USER,
        "password": SMTP_PASSWORD, "from_addr": SMTP_FROM, "use_tls": SMTP_USE_TLS,
    }


def is_configured() -> bool:
    config = _resolve_config()
    return bool(config["host"] and config["from_addr"])


def send_email(to_address: str, subject: str, body: str) -> None:
    config = _resolve_config()
    if not (config["host"] and config["from_addr"]):
        raise RuntimeError(
            "SMTP is not configured (neither Admin: System Settings nor SMTP_HOST in .env), "
            "so this email could not be sent. Two-factor login codes and any other app "
            "emails will not work until a mail relay is configured - ask IT which SMTP "
            "server/relay to use for this deployment."
        )
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = config["from_addr"]
    msg["To"] = to_address

    with smtplib.SMTP(config["host"], config["port"], timeout=20) as server:
        if config["use_tls"]:
            server.starttls()
        if config["user"]:
            server.login(config["user"], config["password"])
        server.sendmail(config["from_addr"], [to_address], msg.as_string())
