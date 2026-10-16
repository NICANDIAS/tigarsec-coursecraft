"""Stage 10: class marking report - stored AI summary of common problems

Revision ID: 0008_marking_insights
Revises: 0007_live_quizzes
Create Date: 2026-10-17

Adds one NEW table (marking_insights). Nothing existing is changed. Only created if it is not
there yet (a new install already has it from models.py).
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_marking_insights"
down_revision = "0007_live_quizzes"
branch_labels = None
depends_on = None


def upgrade():
    from app.models import Base  # the table definition lives in one place: models.py
    bind = op.get_bind()
    if "marking_insights" not in set(sa.inspect(bind).get_table_names()):
        Base.metadata.create_all(bind=bind, tables=[Base.metadata.tables["marking_insights"]])


def downgrade():
    # A new, separate table: older code simply never uses it, so rolling the CODE back is enough.
    pass
