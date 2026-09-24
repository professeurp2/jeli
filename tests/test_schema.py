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


def test_the_app_only_replays_what_its_own_role_may_run():
    """Measured 24 September in production: "permission denied for database postgres". Creating the
    extension and the application's role needs rights on the database itself, which that role does
    not have — and one refused statement rolls back every other one with it, so the whole file did
    nothing. Those statements are fenced off; a human with the rights runs them once, at setup."""
    from app.kb.store import the_app_can_run

    mine = the_app_can_run(SQL)
    runnable = [l for l in mine.split("\n") if l.strip() and not l.strip().startswith("--")]
    for line in runnable:
        low = line.lower()
        for privileged in ("create extension", "create role", "grant ", "alter role", "create schema"):
            assert privileged not in low, f"{privileged} is not the app's to run: {line}"
    # And what is left is still the schema: the tables and the columns the code expects.
    assert mine.lower().count("create table if not exists") >= 15
    assert "asked_in" in mine and "number" in mine and "embedding_backup" in mine


def test_the_fence_is_closed_on_both_ends():
    """An unclosed marker would silently swallow the rest of the file — every table with it."""
    assert SQL.count("-- >>> owner only") == SQL.count("-- <<< owner only") > 0
    from app.kb.store import the_app_can_run

    # Nothing outside the fences is lost: the last table of the file survives the strip.
    assert "jeli.voice_quota" in the_app_can_run(SQL)
