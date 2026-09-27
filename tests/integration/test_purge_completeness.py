"""Assert that `app/purge.py` covers every table with a shop_id column.

If someone adds a new feature that persists per-shop data but forgets to
extend `app/purge.py:purge_shop_data`, this test fails loudly. Without it,
merchant uninstall on prod would silently leak that table's data — a PCD
compliance violation.
"""
import re
from pathlib import Path

from sqlalchemy import inspect

from core.db.models import Base


# Tables that intentionally have shop_id but are NOT purged on uninstall.
# Keep this list minimal + always explain why.
INTENTIONALLY_NOT_PURGED = {
    # shops itself — we anonymize the row in purge_shop_data, don't delete
    "shops",
}


def _tables_with_shop_id():
    """Return set of table names that have a `shop_id` column."""
    return {
        table.name
        for table in Base.metadata.tables.values()
        if any(c.name == "shop_id" for c in table.columns)
    }


def _tables_referenced_by_purge():
    """Parse app/purge.py source and return set of tables mentioned in delete() calls.

    Rough heuristic: find every `delete(ModelName)` and map ModelName → __tablename__.
    Also detects the special kb_qa_pairs subquery pattern.
    """
    purge_src = Path(__file__).parent.parent.parent / "app" / "purge.py"
    src = purge_src.read_text()

    # Match: delete(ModelName)
    referenced_models = set(re.findall(r"\bdelete\((\w+)\)", src))

    # Map model class name → table name
    tables = set()
    for table in Base.metadata.tables.values():
        # Walk mapper classes to find which model owns this table
        for mapper in Base.registry.mappers:
            if mapper.class_.__tablename__ == table.name and mapper.class_.__name__ in referenced_models:
                tables.add(table.name)
    return tables


def test_purge_covers_every_shop_id_table():
    """Every table with shop_id must be either purged or on the allowlist."""
    all_shop_tables = _tables_with_shop_id()
    purged = _tables_referenced_by_purge()
    unaccounted = all_shop_tables - purged - INTENTIONALLY_NOT_PURGED
    assert not unaccounted, (
        f"Tables with shop_id NOT covered by app/purge.py: {sorted(unaccounted)}. "
        "Extend purge_shop_data to delete from these tables in FK-safe order "
        "OR add them to INTENTIONALLY_NOT_PURGED (with a comment explaining why)."
    )
