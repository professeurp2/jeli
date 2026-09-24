"""The schema the code expects, and the database it runs against, kept from drifting apart.

Measured 24 September: two columns added to db/schema.sql that morning never reached the database,
because nothing applied the file. The first anyone knew was a member's reminder crashing at three
in the morning, then every message failing to record who the member was. The file is now applied at
every start — which only works while every statement in it is safe to run again.
"""

import re

from app.kb.store import SCHEMA_FILE

SQL = SCHEMA_FILE.read_text(encoding="utf-8")
# Statements inside a do $$ … $$ block guard themselves; the rules below are for the plain ones.
PLAIN = re.sub(r"do \$\$.*?\$\$;", "", SQL, flags=re.DOTALL | re.IGNORECASE)
PLAIN = re.sub(r"^\s*--.*$", "", PLAIN, flags=re.MULTILINE)


def statements(verb: str) -> list[str]:
    return [" ".join(s.split()) for s in re.findall(rf"^\s*({verb}\b[^;]*);", PLAIN, re.MULTILINE | re.IGNORECASE)]


def test_the_schema_file_is_safe_to_run_again():
    """It runs at every start, so a statement that only works once breaks every restart after."""
    for statement in statements("create table"):
        assert "if not exists" in statement.lower(), statement
    for statement in statements("create index") + statements("create unique index"):
        assert "if not exists" in statement.lower(), statement
    for statement in statements("alter table"):
        low = statement.lower()
        if "add column" in low or "add constraint" in low:
            assert "if not exists" in low, statement


def test_nothing_in_the_schema_file_destroys_data():
    """Applied at every start, a drop is not a migration: it is a loss, once per deployment."""
    for forbidden in ("drop table", "truncate", "delete from", "drop schema"):
        assert forbidden not in PLAIN.lower(), forbidden
    # A generated column may be dropped and rebuilt by hand, never on the way in: it would be
    # recomputed over every passage at each boot.
    assert "drop column" not in PLAIN.lower()


def test_the_columns_the_code_writes_are_in_the_schema():
    """The two that were missing, and the ones added beside them."""
    for table, column in (
        ("members", "number"),
        ("reminders", "asked_in"),
        ("chunks", "embedding_backup"),
    ):
        assert re.search(rf"\b{column}\b", SQL), f"{table}.{column} is not in the schema file"


def test_the_schema_is_applied_when_the_database_opens():
    import inspect

    from app.kb.store import Store

    assert "await self.apply_schema()" in inspect.getsource(Store.open)
    applying = inspect.getsource(Store.apply_schema)
    # Never fatal: a Jeli missing the newest column still answers questions.
    assert "except Exception as error" in applying and "log.error" in applying
    assert "return problem" in applying  # and it says what went wrong, rather than going quiet


def test_the_schema_file_travels_with_the_code():
    """It is read from the repository at run time, so it must sit where the deployed tree puts it."""
    assert SCHEMA_FILE.exists() and SCHEMA_FILE.name == "schema.sql"
    assert SCHEMA_FILE.parent.name == "db"
    assert (SCHEMA_FILE.parent.parent / "app" / "kb" / "store.py").exists()
