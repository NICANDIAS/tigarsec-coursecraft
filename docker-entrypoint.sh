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

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
