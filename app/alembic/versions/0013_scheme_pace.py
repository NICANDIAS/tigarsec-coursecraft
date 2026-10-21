"""Scheme of work: teaching at the class's pace (weeks carried on, weeks combined)

Revision ID: 0013_scheme_pace
Revises: 0012_scheme_week_anchor
Create Date: 2026-10-20

Adds one NULLABLE column, schemes_of_work.pace_json, only if missing. Nothing existing is changed.
"""
from alembic import op
import sqlalchemy as sa

revision = "0013_scheme_pace"
down_revision = "0012_scheme_week_anchor"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    existing = {row[1] for row in bind.execute(sa.text("PRAGMA table_info(schemes_of_work)"))}
    if existing and "pace_json" not in existing:
        bind.execute(sa.text("ALTER TABLE schemes_of_work ADD COLUMN pace_json JSON"))


def downgrade():
    pass  # a nullable column older code never reads
