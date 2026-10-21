#!/bin/sh
set -e

if [ "$LLM_PROVIDER" = "ollama" ]; then
  python wait_for_ollama.py
fi

# Applies any new migrations automatically on every startup - this is what replaces the
# old "rm -f data/tigarsec_assess.db" reset process. New installs get every table created
# fresh; existing installs get only whatever's new since their last start, with all
# existing data left untouched.
echo "Applying database migrations..."
alembic upgrade head

# Behind a proxy (e.g. Tailscale Funnel on a Raspberry Pi), trust its X-Forwarded-* headers so
# CourseCraft sees each visitor's real address (used to slow down password guessing).
# Only set FORWARDED_ALLOW_IPS when the app port is NOT reachable from the network directly
# (bound to 127.0.0.1) - otherwise anyone could fake their address.
if [ -n "$FORWARDED_ALLOW_IPS" ]; then
  exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "$FORWARDED_ALLOW_IPS"
fi
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
