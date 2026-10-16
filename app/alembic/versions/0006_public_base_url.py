"""Stage 6: the address students use to join live lessons (QR code)

Revision ID: 0006_public_base_url
Revises: 0005_practice_and_live
Create Date: 2026-10-16

Adds one NULLABLE column, system_settings.public_base_url. Only added if missing (a new
install already has it from models.py). Empty means "use the address the lecturer's browser
used" - exactly how the join link worked before.
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_public_base_url"
down_revision = "0005_practice_and_live"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {row[1] for row in bind.execute(sa.text("PRAGMA table_info(system_settings)"))}
    if columns and "public_base_url" not in columns:
        bind.execute(sa.text("ALTER TABLE system_settings ADD COLUMN public_base_url VARCHAR(255)"))


def downgrade():
    pass
