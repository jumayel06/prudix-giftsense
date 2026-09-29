"""Rename AI tiers: fast → standard, balanced → advanced (premium unchanged).

"Fast" read as the best choice, so merchants would pick the entry option;
the new names read as a quality ladder.

Revision ID: 20260928000004
Revises: 20260928000003
"""
import sqlalchemy as sa
from alembic import op

revision = "20260928000004"
down_revision = "20260928000003"
branch_labels = None
depends_on = None

_RENAMES = {"fast": "standard", "balanced": "advanced"}


def upgrade() -> None:
    for old, new in _RENAMES.items():
        op.execute(f"UPDATE shops SET selected_model = '{new}' WHERE selected_model = '{old}'")
    with op.batch_alter_table("shops") as batch:
        batch.alter_column("selected_model", server_default="standard", existing_type=sa.String(),
                           existing_nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("shops") as batch:
        batch.alter_column("selected_model", server_default="fast", existing_type=sa.String(),
                           existing_nullable=False)
    for old, new in _RENAMES.items():
        op.execute(f"UPDATE shops SET selected_model = '{old}' WHERE selected_model = '{new}'")
