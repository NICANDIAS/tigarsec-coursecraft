"""Stage 8: live quizzes (multiple choice, countdown, points, leaderboard)

Revision ID: 0007_live_quizzes
Revises: 0006_public_base_url
Create Date: 2026-10-17

Adds one NEW table (quiz_sets) and three NULLABLE columns: live_sessions.quiz_set_id,
live_sessions.screen_started_at and live_responses.points. Each is only added if missing (a
new install already has them from models.py). Nothing existing is changed or removed.
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_live_quizzes"
down_revision = "0006_public_base_url"
branch_labels = None
depends_on = None

NEW_COLUMNS = {
    "live_sessions": [("quiz_set_id", "INTEGER REFERENCES quiz_sets(id)"), ("screen_started_at", "DATETIME")],
    "live_responses": [("points", "INTEGER")],
}


def upgrade():
    from app.models import Base  # the table definition lives in one place: models.py
    bind = op.get_bind()
    if "quiz_sets" not in set(sa.inspect(bind).get_table_names()):
        Base.metadata.create_all(bind=bind, tables=[Base.metadata.tables["quiz_sets"]])
    for table, columns in NEW_COLUMNS.items():
        existing = {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}
        for name, kind in columns:
            if existing and name not in existing:
                bind.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {name} {kind}"))


def downgrade():
    # New table and nullable columns: older code simply never uses them, so rolling the CODE back is enough.
    pass
