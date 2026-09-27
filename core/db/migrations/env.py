import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from core.config import settings
from core.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Migrations run over a dedicated, session-pinned connection — NOT the app's
# transaction-mode pooler (port 6543), which breaks DDL and the boot-time
# `alembic upgrade head`. Falls back to database_url for local dev where there's
# no separate pooler.
MIGRATION_URL = settings.direct_database_url or settings.database_url


def run_migrations_offline() -> None:
    url = MIGRATION_URL
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(MIGRATION_URL)
    async with engine.begin() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
