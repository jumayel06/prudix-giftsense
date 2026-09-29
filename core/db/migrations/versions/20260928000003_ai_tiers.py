"""AI tiers: shops.selected_model now stores "fast" / "balanced" / "premium"
instead of a model ID, plus shops.model_pins for per-shop admin pins.

Model IDs become tiers (Haiku 4.5, retired, goes to fast with GPT-6 Luna).
From here on, swapping or retiring a model is a change to app/ai_models.py
only: no data migration.

Revision ID: 20260928000003
Revises: 20260928000002
"""
import sqlalchemy as sa
from alembic import op

revision = "20260928000003"
down_revision = "20260928000002"
branch_labels = None
depends_on = None

_TIERS = {
    "fast": ("gpt-6-luna", "gpt-4o-mini", "claude-haiku-4-5"),
    "balanced": ("gpt-6-sol", "gpt-4.1"),
    "premium": ("claude-sonnet-5", "claude-sonnet-5-5"),
}
_DOWN = {"fast": "gpt-6-luna", "balanced": "gpt-6-sol", "premium": "claude-sonnet-5"}


def upgrade() -> None:
    for tier, models in _TIERS.items():
        ids = ", ".join(f"'{m}'" for m in models)
        op.execute(f"UPDATE shops SET selected_model = '{tier}' WHERE selected_model IN ({ids})")
    op.execute("UPDATE shops SET selected_model = 'fast' WHERE selected_model NOT IN ('fast', 'balanced', 'premium')")
    # batch mode: SQLite (tests) can't ALTER COLUMN ... DEFAULT in place.
    with op.batch_alter_table("shops") as batch:
        batch.alter_column("selected_model", server_default="fast", existing_type=sa.String(),
                           existing_nullable=False)
        batch.add_column(sa.Column("model_pins", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("shops") as batch:
        batch.drop_column("model_pins")
        batch.alter_column("selected_model", server_default="claude-haiku-4-5", existing_type=sa.String(),
                           existing_nullable=False)
    for tier, model in _DOWN.items():
        op.execute(f"UPDATE shops SET selected_model = '{model}' WHERE selected_model = '{tier}'")
