"""spec parsing Stage 1: honesty report, safe re-parse versions, kept source file

Revision ID: 0003_spec_quality_versions
Revises: 0002_catchup_columns
Create Date: 2026-10-15

Adds three NULLABLE columns to spec_versions. Runs automatically on app start-up
(docker-entrypoint.sh runs `alembic upgrade head` before the web server starts), so the
columns exist before any page can ask for them.

Safe on every database state, same pattern as 0002: it checks which columns already
exist and only adds the missing ones. A brand-new install already has them (0001 builds
tables straight from models.py), so on a new install this is a harmless no-op.
Existing rows get NULL, which the app reads as "spec from before Stage 1" - shown
exactly as before.
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_spec_quality_versions"
down_revision = "0002_catchup_columns"
branch_labels = None
depends_on = None

COLUMNS_TO_ENSURE = [
    ("spec_versions", "parse_quality", "TEXT"),          # JSON is stored as TEXT in SQLite
    ("spec_versions", "reparsed_from_id", "INTEGER"),
    ("spec_versions", "source_file_path", "VARCHAR(1000)"),
]


def _existing_columns(bind, table_name):
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table_name})"))}


def upgrade():
    bind = op.get_bind()
    for table, column, sql_type in COLUMNS_TO_ENSURE:
        if column not in _existing_columns(bind, table):
            bind.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))


def downgrade():
    # SQLite can't easily drop columns; the columns are nullable and unused by older code,
    # so rolling the CODE back is enough - old code simply ignores them.
    pass
