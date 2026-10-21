"""Protection for running CourseCraft where strangers can reach it (e.g. a public demo).

Three switches, all read from .env so a college LAN install behaves exactly as before:
  REGISTRATION=open|closed   closed = only the admin creates accounts (Admin -> Users).
                             The very first account can always register, so a fresh
                             install can still make its admin.
  COOKIE_SECURE=true         login cookies are only ever sent over HTTPS.
  LOGIN_MAX_FAILS / LOGIN_LOCK_MINUTES   slow down password guessing.

Kept free of database/FastAPI imports so the logic can be tested on its own
(tools/test_public_demo.py).
"""
import os
import threading
import time

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), payment=()",
}


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def registration_open() -> bool:
    return os.getenv("REGISTRATION", "open").strip().lower() != "closed"


def cookie_secure() -> bool:
    return _env_bool("COOKIE_SECURE")


def harden_set_cookie(value: str, secure: bool) -> str:
    """Add SameSite=Lax to every cookie that has none, and Secure when asked."""
    parts = [p.strip() for p in value.split(";")]
    names = {p.split("=", 1)[0].strip().lower() for p in parts[1:]}
    if "samesite" not in names:
        value += "; SameSite=Lax"
    if secure and "secure" not in names:
        value += "; Secure"
    return value


class LoginThrottle:
    """Counts failed logins per email and per client address in a sliding window.
    After too many failures the key is locked until the window passes. One uvicorn
    worker runs CourseCraft, so keeping this in memory is enough (a restart clears it)."""

    def __init__(self, max_fails=None, lock_minutes=None, max_fails_per_ip=None, clock=time.monotonic):
        self.max_fails = max_fails or int(os.getenv("LOGIN_MAX_FAILS", "5") or 5)
        self.window = 60 * (lock_minutes or int(os.getenv("LOGIN_LOCK_MINUTES", "15") or 15))
        self.max_fails_per_ip = max_fails_per_ip or self.max_fails * 6
        self.clock = clock
        self._fails = {}
        self._lock = threading.Lock()

    def _recent(self, key):
        now = self.clock()
        times = [t for t in self._fails.get(key, []) if now - t < self.window]
        if times:
            self._fails[key] = times
        else:
            self._fails.pop(key, None)
        return times

    @staticmethod
    def _keys(email, ip):
        return ("email:" + (email or "").strip().lower(), "ip:" + (ip or "unknown"))

    def blocked_for(self, email, ip) -> int:
        """Seconds until this email/address may try again (0 = allowed now)."""
        email_key, ip_key = self._keys(email, ip)
        with self._lock:
            waits = []
            for key, limit in ((email_key, self.max_fails), (ip_key, self.max_fails_per_ip)):
                times = self._recent(key)
                if len(times) >= limit:
                    waits.append(int(self.window - (self.clock() - times[-limit])) + 1)
            return max(waits, default=0)

    def failed(self, email, ip):
        with self._lock:
            for key in self._keys(email, ip):
                self._fails.setdefault(key, []).append(self.clock())
            if len(self._fails) > 10000:  # someone spraying random emails: forget the oldest
                for key in sorted(self._fails, key=lambda k: self._fails[k][-1])[:5000]:
                    self._fails.pop(key, None)

    def succeeded(self, email):
        with self._lock:
            self._fails.pop(self._keys(email, None)[0], None)


login_throttle = LoginThrottle()


def wait_message(seconds: int) -> str:
    minutes = max(1, (seconds + 59) // 60)
    return (f"Too many failed attempts. For your security, please wait {minutes} minute"
            f"{'s' if minutes != 1 else ''} before trying again.")


def client_ip(request) -> str:
    """The visitor's address. Behind a proxy, uvicorn's --proxy-headers already put the real
    address in request.client (see docker-entrypoint.sh)."""
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"


class SecurityHeadersMiddleware:
    """Pure ASGI middleware: safe default headers on every response, SameSite on every
    cookie, Secure on cookies (and HSTS) when COOKIE_SECURE=true."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        secure = cookie_secure()

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = []
                present = set()
                for name, value in message.get("headers", []):
                    lname = name.decode("latin-1").lower()
                    present.add(lname)
                    if lname == "set-cookie":
                        value = harden_set_cookie(value.decode("latin-1"), secure).encode("latin-1")
                    headers.append((name, value))
                extra = dict(SECURITY_HEADERS)
                if secure:
                    extra["Strict-Transport-Security"] = "max-age=31536000"
                for name, value in extra.items():
                    if name.lower() not in present:
                        headers.append((name.encode("latin-1"), value.encode("latin-1")))
                message = dict(message, headers=headers)
            await send(message)

        return await self.app(scope, receive, send_wrapper)
