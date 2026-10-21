"""Checks for the public-demo protection in app/security.py (no database needed).
Run: docker compose run --rm --no-deps -v "$PWD":/app --entrypoint python app tools/test_public_demo.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("security", os.path.join(os.path.dirname(__file__), "..", "app", "security.py"))
security = importlib.util.module_from_spec(spec)
spec.loader.exec_module(security)

passed = 0


def check(name, condition):
    global passed
    if not condition:
        print(f"FAIL: {name}")
        sys.exit(1)
    passed += 1
    print(f"ok   {name}")


# --- registration switch
os.environ.pop("REGISTRATION", None)
check("registration open by default (college LAN unchanged)", security.registration_open())
os.environ["REGISTRATION"] = "closed"
check("REGISTRATION=closed closes it", not security.registration_open())
os.environ["REGISTRATION"] = " Closed "
check("closed is not case/space sensitive", not security.registration_open())
os.environ.pop("REGISTRATION")

# --- cookies
check("SameSite added, no Secure by default",
      security.harden_set_cookie("access_token=x; HttpOnly; Path=/", False) == "access_token=x; HttpOnly; Path=/; SameSite=Lax")
check("Secure added when asked",
      security.harden_set_cookie("a=1; Path=/", True).endswith("; SameSite=Lax; Secure"))
check("existing SameSite/Secure left alone",
      security.harden_set_cookie("a=1; SameSite=strict; Secure", True) == "a=1; SameSite=strict; Secure")
check("a value containing 'secure' isn't mistaken for the flag",
      security.harden_set_cookie("note=secure", True).endswith("; Secure"))

# --- login throttle with a fake clock
now = [1000.0]
t = security.LoginThrottle(max_fails=3, lock_minutes=15, max_fails_per_ip=5, clock=lambda: now[0])
for _ in range(2):
    t.failed("Teacher@X.ac.uk", "1.1.1.1")
check("under the limit: allowed", t.blocked_for("teacher@x.ac.uk", "1.1.1.1") == 0)
t.failed("teacher@x.ac.uk ", "1.1.1.1")
wait = t.blocked_for("TEACHER@x.ac.uk", "9.9.9.9")
check("3rd failure locks that email from any address (case/space ignored)", 0 < wait <= 15 * 60 + 1)
check("another email from the same address still allowed", t.blocked_for("other@x.ac.uk", "1.1.1.1") == 0)
now[0] += 15 * 60 + 1
check("lock lifts after the window", t.blocked_for("teacher@x.ac.uk", "1.1.1.1") == 0)

for i in range(5):
    t.failed(f"user{i}@x.ac.uk", "6.6.6.6")
check("spraying many emails from one address locks that address", t.blocked_for("new@x.ac.uk", "6.6.6.6") > 0)
check("...but not other addresses", t.blocked_for("new@x.ac.uk", "7.7.7.7") == 0)

t2 = security.LoginThrottle(max_fails=3, lock_minutes=15, clock=lambda: now[0])
t2.failed("a@x", "1.1.1.1"); t2.failed("a@x", "1.1.1.1")
t2.succeeded("a@x")
t2.failed("a@x", "1.1.1.1"); t2.failed("a@x", "1.1.1.1")
check("a successful login resets that email's count", t2.blocked_for("a@x", "2.2.2.2") == 0)

t3 = security.LoginThrottle(max_fails=3, lock_minutes=15, clock=lambda: now[0])
for i in range(10050):
    t3.failed(f"spray{i}@x", f"10.0.{i // 250}.{i % 250}")
check("memory stays bounded under a spray", len(t3._fails) <= 10002)

check("wait message in minutes", "wait 15 minutes" in security.wait_message(15 * 60))
check("wait message singular", "wait 1 minute " in security.wait_message(20))


class Req:
    class client:
        host = "203.0.113.5"


check("client_ip reads request.client", security.client_ip(Req()) == "203.0.113.5")
check("client_ip copes with no client", security.client_ip(object()) == "unknown")


# --- middleware
async def run_middleware(secure):
    os.environ["COOKIE_SECURE"] = "true" if secure else "false"
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/html"), (b"set-cookie", b"access_token=x; HttpOnly"),
                                (b"x-frame-options", b"DENY")]})
        await send({"type": "http.response.body", "body": b"hi"})

    async def send(message):
        sent.append(message)

    await security.SecurityHeadersMiddleware(app)({"type": "http"}, None, send)
    return dict((k.decode().lower(), v.decode()) for k, v in sent[0]["headers"]), sent[1]


headers, body = asyncio.run(run_middleware(True))
check("nosniff header added", headers.get("x-content-type-options") == "nosniff")
check("app's own X-Frame-Options kept", headers.get("x-frame-options") == "DENY")
check("HSTS when COOKIE_SECURE=true", "max-age" in headers.get("strict-transport-security", ""))
check("cookie made Secure + SameSite", headers["set-cookie"] == "access_token=x; HttpOnly; SameSite=Lax; Secure")
check("body passes through untouched", body["body"] == b"hi")
headers, _ = asyncio.run(run_middleware(False))
check("no HSTS / Secure on a plain-HTTP LAN install", "strict-transport-security" not in headers and "Secure" not in headers["set-cookie"])
os.environ.pop("COOKIE_SECURE")

print(f"\nALL PUBLIC DEMO CHECKS PASSED ({passed})")
