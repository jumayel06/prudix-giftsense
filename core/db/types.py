"""Custom column types."""
from sqlalchemy import JSON
from sqlalchemy.types import TypeDecorator, UserDefinedType


class _PgVector(UserDefinedType):
    """Postgres `vector(n)` (pgvector). Values travel as the text form
    '[0.1,0.2,…]', so no driver-level adapter registration is needed."""
    cache_ok = True

    def __init__(self, dims: int):
        self.dims = dims

    def get_col_spec(self, **kw) -> str:
        return f"vector({self.dims})"

    def bind_processor(self, dialect):
        def process(value):
            if value is None:
                return None
            return "[" + ",".join(repr(float(x)) for x in value) + "]"
        return process

    def result_processor(self, dialect, coltype):
        def process(value):
            if value is None or isinstance(value, list):
                return value
            return [float(x) for x in str(value).strip("[]").split(",") if x.strip()]
        return process


class Embedding(TypeDecorator):
    """A float vector: pgvector `vector(dims)` on Postgres (so similarity
    search runs in the database), JSON everywhere else (SQLite tests)."""
    impl = JSON
    cache_ok = True

    def __init__(self, dims: int, *args, **kwargs):
        self.dims = dims
        super().__init__(*args, **kwargs)

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(_PgVector(self.dims))
        return dialect.type_descriptor(JSON())
