"""Move shops off retired models: gpt-4o-mini → gpt-6-luna, gpt-4.1 → gpt-6-sol.

The 2026-09-28 six-model eval replaced them in every plan (app/config.py
RETIRED_MODELS). Data only; no schema change.

Revision ID: 20260928000002
Revises: 20260928000001
"""
from alembic import op

revision = "20260928000002"
down_revision = "20260928000001"
branch_labels = None
depends_on = None

_MAP = {"gpt-4o-mini": "gpt-6-luna", "gpt-4.1": "gpt-6-sol"}


def upgrade() -> None:
    for old, new in _MAP.items():
        op.execute(f"UPDATE shops SET selected_model = '{new}' WHERE selected_model = '{old}'")


def downgrade() -> None:
    for old, new in _MAP.items():
        op.execute(f"UPDATE shops SET selected_model = '{old}' WHERE selected_model = '{new}'")
