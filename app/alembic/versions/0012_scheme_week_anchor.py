"""Scheme of work: the lecturer's "this is week N" correction

Revision ID: 0012_scheme_week_anchor
Revises: 0011_term_plans_homework
Create Date: 2026-10-20

Adds one NULLABLE column, schemes_of_work.week_anchor_json, only if missing. Nothing existing is changed.
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_scheme_week_anchor"
down_revision = "0011_term_plans_homework"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    existing = {row[1] for row in bind.execute(sa.text("PRAGMA table_info(schemes_of_work)"))}
    if existing and "week_anchor_json" not in existing:
        bind.execute(sa.text("ALTER TABLE schemes_of_work ADD COLUMN week_anchor_json JSON"))


def downgrade():
    pass  # a nullable column older code never reads
