from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import settings

engine = create_async_engine(
    settings.database_url,
    # NullPool: don't keep a client-side pool of open connections. In prod we sit
    # behind Supabase's TRANSACTION-mode pooler (Supavisor, port 6543), which does
    # the real pooling — multiplexing many clients over a small set of Postgres
    # backends. A client-side QueuePool here would both fight Supavisor and, in
    # SESSION mode, exhaust the pooler's per-tenant client cap (the
    # `EMAXCONNSESSION: max clients ... pool_size: 15` 500s we hit 2026-09-11:
    # 2 web workers + 1 ARQ worker × pool_size 10 + overflow 20 ≈ 90 connections
    # into a 15-slot session pooler). With NullPool + transaction mode, each
    # request borrows a backend only for its query and hands it straight back, so
    # concurrency is bounded by Supavisor, not by us.
    poolclass=NullPool,
    echo=settings.sql_echo,
    # Supavisor (transaction mode, port 6543) hands each transaction a
    # potentially DIFFERENT Postgres backend, so any cached prepared statement
    # can reference one that doesn't exist on the current backend — surfacing as
    # `InvalidSQLStatementNameError: prepared statement "__asyncpg_stmt_N__" does
    # not exist` (prod PRUDIX-COMMERCE-PROD-5, 2026-09-11). There are TWO caches
    # to defeat, at two layers:
    #   1. statement_cache_size=0 — asyncpg's OWN cache (default 100), which the
    #      SQLAlchemy dialect does NOT touch. Covers asyncpg's internal
    #      introspection statements (the __asyncpg_stmt_N__ names) that broke prod.
    #   2. prepared_statement_cache_size=0 + a unique prepared_statement_name_func
    #      — SQLAlchemy's own statement cache + naming, so statements SQLAlchemy
    #      issues never collide across backends either.
    # All three together are the documented pgbouncer/Supavisor transaction-mode
    # workaround and are what make port-6543 pooling safe.
    connect_args={
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
        "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
    },
)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
