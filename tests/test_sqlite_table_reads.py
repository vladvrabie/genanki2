"""Does `AnkiSQLite.read_table` hand back the whole table, in one order, or refuse?

    uv run --with pytest pytest tests/test_sqlite_table_reads.py

Every row model is built from a dict keyed by column name, and most of their fields
carry a default. A column that never reaches the dict is therefore not an error anywhere
downstream: it becomes the default, and the collection comes back holding a value
nothing in the file put there. `read_table` is the only place that sees the table's
columns rather than one row's keys, so the refusal belongs there.

The column sets in `genanki2._typed_schema` were generated from a real Anki collection,
which makes them the thing a file is measured against. `tests/fixtures/real_tables.anki2`
is a trimmed collection Anki wrote; `AnkiSQLite.create` builds one from genanki2's own
DDL. Both are checked here, so the generated schema, Anki's schema and genanki2's DDL
are pinned to each other.
"""

import pathlib
import sqlite3

import pytest

from genanki2 import _typed_schema
from genanki2.errors import CollectionReadError, Genanki2Error
from genanki2.sqlite import AnkiSQLite

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "real_tables.anki2"

TABLES = list(AnkiSQLite.Table)

# The key set each table is read against, taken from the generated schema the way
# `read_table` takes it.
ROW_KEYS = {
  table: frozenset(
    getattr(
      _typed_schema, f"{table.title().rstrip('s').replace('_', '')}Row"
    ).__required_keys__
  )
  for table in TABLES
}


@pytest.fixture
def scratch(tmp_path) -> pathlib.Path:
  """A writable copy of the fixture collection, for tests that damage the schema."""
  path = tmp_path / "scratch.anki2"
  path.write_bytes(FIXTURE.read_bytes())
  return path


def alter(path: pathlib.Path, statement: str) -> None:
  """Run one DDL statement against the collection at `path`.

  It goes through `AnkiSQLite` rather than a plain connection because altering a table
  rebuilds its indexes, and `tags.tag` is indexed `COLLATE unicase`, which stdlib
  sqlite3 refuses to execute without the comparator registered.
  """
  with AnkiSQLite(path, writable=True) as db:
    db.connection.execute(statement)


# ------------------------------------ What comes back -----------------------------------


@pytest.mark.parametrize("table", TABLES)
def test_a_row_carries_exactly_the_columns_the_schema_declares(table):
  """A collection Anki wrote holds no column the generated schema misses, and misses
  none it declares. Were that untrue, every other test here would be measuring the
  fixture against the wrong key set."""
  with AnkiSQLite(FIXTURE) as db:
    rows = db.read_table(table)
  assert rows, f"the fixture lost its {table} rows"
  for row in rows:
    assert frozenset(row) == ROW_KEYS[table]


@pytest.mark.parametrize("table", TABLES)
def test_every_row_of_the_table_comes_back(table):
  with AnkiSQLite(FIXTURE) as db:
    assert (
      len(db.read_table(table))
      == db.connection.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
    )


@pytest.mark.parametrize(
  ("table", "key"),
  [
    (AnkiSQLite.Table.COL, ("id",)),
    (AnkiSQLite.Table.CONFIG, ("KEY",)),  # Anki names this column in upper case
    (AnkiSQLite.Table.FIELDS, ("ntid", "ord")),
    (AnkiSQLite.Table.TEMPLATES, ("ntid", "ord")),
    (AnkiSQLite.Table.NOTETYPES, ("id",)),
    (AnkiSQLite.Table.NOTES, ("id",)),
    (AnkiSQLite.Table.TAGS, ("tag",)),
    (AnkiSQLite.Table.DECK_CONFIG, ("id",)),
    (AnkiSQLite.Table.DECKS, ("id",)),
    (AnkiSQLite.Table.CARDS, ("id",)),
    (AnkiSQLite.Table.REVLOG, ("id",)),
    (AnkiSQLite.Table.GRAVES, ("oid", "type")),
  ],
)
def test_rows_come_back_sorted_by_the_columns_the_table_is_keyed_on(table, key):
  """Insertion order is what SQLite gives back without an ORDER BY, and an apkg carries
  whatever order the exporter happened to write. Grouping fields and templates by `ntid`
  needs them adjacent, and a round trip that reorders a table is a diff for no reason."""
  with AnkiSQLite(FIXTURE) as db:
    rows = db.read_table(table)
  keys = [tuple(row[column] for column in key) for row in rows]
  assert keys == sorted(keys)


def test_the_tags_order_is_the_collation_anki_indexed_them_with():
  """`tags.tag` is indexed `COLLATE unicase`, so SQLite orders it case-insensitively.
  Sorting the same names in Python would disagree wherever case differs."""
  with AnkiSQLite(FIXTURE) as db:
    tags = [row["tag"] for row in db.read_table(AnkiSQLite.Table.TAGS)]
  assert tags == sorted(tags, key=str.casefold)


def test_an_empty_table_reads_as_an_empty_list(scratch):
  """A collection Anki exported carries no `graves` and no `tags`, so this is the state
  the reader meets most often, not an edge case."""
  alter(scratch, "DELETE FROM graves")
  with AnkiSQLite(scratch) as db:
    assert db.read_table(AnkiSQLite.Table.GRAVES) == []


# ------------------------------------ What is refused -----------------------------------


def test_a_table_missing_a_column_is_refused_by_name(scratch):
  """The column the model would have defaulted is named, so the message says which
  collection is at fault rather than which field ended up wrong."""
  alter(scratch, "ALTER TABLE tags DROP COLUMN usn")
  with AnkiSQLite(scratch) as db, pytest.raises(CollectionReadError) as caught:
    db.read_table(AnkiSQLite.Table.TAGS)
  assert "tags" in str(caught.value)
  assert "usn" in str(caught.value)


def test_a_table_the_database_does_not_hold_is_a_read_error_not_a_sqlite_error(scratch):
  """`PRAGMA table_info` answers an absent table with no rows instead of raising, so a
  dropped table arrives as every column missing. A caller catching `CollectionReadError`
  should not also have to catch `sqlite3.OperationalError` to cover a truncated file."""
  alter(scratch, "DROP TABLE graves")
  with AnkiSQLite(scratch) as db, pytest.raises(CollectionReadError):
    db.read_table(AnkiSQLite.Table.GRAVES)


def test_the_check_runs_before_the_rows_are_read(scratch):
  """A column is missing whether or not the table holds anything, and a collection with
  an empty table would otherwise be accepted and refused by turns."""
  alter(scratch, "DELETE FROM tags")
  alter(scratch, "ALTER TABLE tags DROP COLUMN usn")
  with AnkiSQLite(scratch) as db, pytest.raises(CollectionReadError):
    db.read_table(AnkiSQLite.Table.TAGS)


def test_a_column_the_schema_does_not_declare_is_read_and_passed_on(scratch):
  """Modelling v18 is the goal, so a column from outside it is not grounds for refusing
  a file. It reaches the row models, which ignore it, and is dropped on write-back.
  Carrying such a column through is what `todos/schema-extensibility.md` is about."""
  alter(scratch, "ALTER TABLE tags ADD COLUMN unknown TEXT NOT NULL DEFAULT 'x'")
  with AnkiSQLite(scratch) as db:
    rows = db.read_table(AnkiSQLite.Table.TAGS)
  assert rows
  assert all(row["unknown"] == "x" for row in rows)


# --------------------------- genanki2's own DDL against the schema ----------------------


@pytest.mark.parametrize("table", TABLES)
def test_a_collection_genanki2_creates_passes_its_own_column_check(table, tmp_path):
  """`_schema.py` holds the DDL and `_typed_schema.py` was generated from a collection
  Anki wrote. Nothing but this keeps the two from drifting apart, and a table genanki2
  writes without a column would be refused on the way back in."""
  path = tmp_path / "created.anki2"
  AnkiSQLite.create(path)
  with AnkiSQLite(path) as db:
    db.read_table(table)  # refusing here is the failure this test exists to catch


# -------------------------- Opening and creating a database ---------------------------


def test_opening_a_missing_database_creates_nothing(tmp_path):
  """`sqlite3.connect` creates a missing file by default, which left an empty database
  behind and turned a mistyped path into a missing `col` table."""
  with pytest.raises(sqlite3.OperationalError), AnkiSQLite(tmp_path / "missing.anki2"):
    pass

  assert list(tmp_path.iterdir()) == []


def test_a_read_only_connection_refuses_to_write(scratch):
  before = scratch.read_bytes()

  with (
    pytest.raises(sqlite3.OperationalError, match="readonly"),
    AnkiSQLite(scratch) as db,
  ):
    db.connection.execute("DELETE FROM cards")

  assert scratch.read_bytes() == before


@pytest.mark.parametrize("name", ["a b.anki2", "x?mode=rwc.anki2", "x#y.anki2"])
def test_a_file_name_cannot_change_how_the_database_is_opened(tmp_path, name: str):
  """The connection is opened by URI, so a `?` or `#` in the name would otherwise start
  the query string and a space would end the path."""
  path = tmp_path / name
  path.write_bytes(FIXTURE.read_bytes())

  with AnkiSQLite(path) as db:
    assert db.read_table(AnkiSQLite.Table.COL)
  AnkiSQLite.create(tmp_path / f"new {name}")

  assert sorted(entry.name for entry in tmp_path.iterdir()) == sorted(
    [name, f"new {name}"]
  )


def test_creating_over_an_existing_database_is_refused(scratch):
  """The schema would otherwise be added beside whatever the file holds."""
  before = scratch.read_bytes()

  with pytest.raises(Genanki2Error, match="already holds a database"):
    AnkiSQLite.create(scratch)

  assert scratch.read_bytes() == before


def test_a_connection_can_be_reopened_after_a_block_that_raised(scratch):
  db = AnkiSQLite(scratch)
  with pytest.raises(ValueError), db:
    raise ValueError

  with db:
    assert db.read_table(AnkiSQLite.Table.COL)
