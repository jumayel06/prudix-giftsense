"""Alembic migrations must produce exactly the schema in core/db/models.py.

Runs `alembic upgrade head` against a scratch SQLite file, then diffs it
against Base.metadata with Alembic's autogenerate comparator. Catches a model
change shipped without a migration (or the reverse) before it reaches Supabase.
Postgres-only steps (the `vector` extension) are skipped on SQLite.
"""
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from core.db.models import Base

ROOT = Path(__file__).resolve().parents[2]


def test_migrations_match_models(tmp_path, monkeypatch):
    from core import config as core_config

    db_file = tmp_path / "migrations.db"
    monkeypatch.setattr(core_config.settings, "database_url", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setattr(core_config.settings, "direct_database_url", "")

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "core" / "db" / "migrations"))
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_file}")
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": False})
        diff = compare_metadata(ctx, Base.metadata)
    engine.dispose()

    assert diff == [], f"Models and migrations disagree: {diff}"
