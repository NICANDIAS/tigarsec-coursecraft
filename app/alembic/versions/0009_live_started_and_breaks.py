"""Live lessons: "not started yet" and breaks

Revision ID: 0009_live_started_and_breaks
Revises: 0008_marking_insights
Create Date: 2026-10-19

Adds one NULLABLE column, live_sessions.started_at (when the lecturer pressed "Start the
lesson"). Sessions from before this keep working: one that has moved past its first screen
counts as started. The new "paused" state needs no schema change. Only added if missing.
"""
from alembic import op
import sqlalchemy as sa

revision = "0009_live_started_and_breaks"
down_revision = "0008_marking_insights"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {row[1] for row in bind.execute(sa.text("PRAGMA table_info(live_sessions)"))}
    if columns and "started_at" not in columns:
        bind.execute(sa.text("ALTER TABLE live_sessions ADD COLUMN started_at DATETIME"))
        # earlier sessions that clearly got going count as started
        bind.execute(sa.text("UPDATE live_sessions SET started_at = created_at WHERE current_screen > 0 OR state = 'ended'"))


def downgrade():
    pass
