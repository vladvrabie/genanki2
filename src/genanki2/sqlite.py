"""A SQLite connection to an Anki collection, carrying the collation its schema needs.

SQLite stores only a collation's name in the file, never its implementation, so a
comparator has to be registered under the name Anki uses before the collection's DDL
will execute or its indexes can be used.
"""

import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Protocol, TypeAlias, overload

from frozendict import frozendict
from typing_extensions import Self

import genanki2._typed_schema as _ts
from genanki2._schema import SCHEMA
from genanki2.errors import CollectionReadError, Genanki2Error
from genanki2.io_utils import resolve_path
from genanki2.utils import StrEnum

__all__ = [
  "AnkiSQLite",
  "Row",
  "RowSchema",
  "Rows",
  "dict_row_factory",
  "unicase",
]

# A row keyed by column name. A column's type is decided by the schema, not here.
Row: TypeAlias = Mapping[str, Any]
Rows: TypeAlias = Sequence[Row]
# Note: Starting with Python 3.12 use type statement


class RowSchema(Protocol):
  """Shape of a `TypedDict`, modeling a table row with column names and their types.

  Needed by the type checker because it rejects `type[TypedDict]`.
  """

  __required_keys__: frozenset[str]


def unicase(a: str, b: str) -> int:
  """Order two strings case-insensitively, as Anki's `unicase` collation does.

  SQLite orders index entries physically with the collation, so a comparator that
  disagrees with Anki's builds indexes through which Anki's own lookups miss rows.
  """
  return (a.casefold() > b.casefold()) - (a.casefold() < b.casefold())


def dict_row_factory(cursor: sqlite3.Cursor, row: tuple[Any, ...]) -> dict[str, Any]:
  """Pair `row`'s values with their column names, as a `sqlite3` row factory.

  Rows come out of the database as dicts, typed generically as
  `Row = Mapping[str, Any]`. The built-in `sqlite3.Row` doesn't support typical dict
  operations in the same way, such as unpacking and containment checks.
  """
  return {column[0]: value for column, value in zip(cursor.description, row)}


class AnkiSQLite:
  """A connection to a collection database.

  Constructing one only records the path. The connection is opened on entering a `with`
  block and closed on leaving it, so `connection` raises outside of one. Rows come back
  as plain dicts, keyed by column name.

  The connection is read-only unless `writable` is true, so reading neither creates a
  missing file nor changes an existing one.

  Not thread safe, since sqlite3 refuses to share a connection between threads.
  """

  class Table(StrEnum):
    """A table of the v18 collection schema.

    The order is suggested, to respect references when populating.
    """

    COL = "col"
    CONFIG = "config"
    ##
    FIELDS = "fields"
    TEMPLATES = "templates"
    NOTETYPES = "notetypes"
    NOTES = "notes"
    TAGS = "tags"
    ##
    DECK_CONFIG = "deck_config"
    DECKS = "decks"
    ##
    CARDS = "cards"
    REVLOG = "revlog"
    GRAVES = "graves"

  ORDERING_CLAUSE: frozendict[Table, str] = frozendict(
    {
      Table.COL: "ORDER BY id",
      Table.CONFIG: "ORDER BY KEY",
      Table.FIELDS: "ORDER BY ntid, ord",
      Table.TEMPLATES: "ORDER BY ntid, ord",
      Table.NOTETYPES: "ORDER BY id",
      Table.NOTES: "ORDER BY id",
      Table.TAGS: "ORDER BY tag",
      Table.DECK_CONFIG: "ORDER BY id",
      Table.DECKS: "ORDER BY id",
      Table.CARDS: "ORDER BY id",
      Table.REVLOG: "ORDER BY id",
      Table.GRAVES: "ORDER BY oid, type",
    }
  )

  ROW_SCHEMA: frozendict[Table, RowSchema] = frozendict(
    {
      Table.COL: _ts.ColRow,
      Table.CONFIG: _ts.ConfigRow,
      Table.FIELDS: _ts.FieldRow,
      Table.TEMPLATES: _ts.TemplateRow,
      Table.NOTETYPES: _ts.NotetypeRow,
      Table.NOTES: _ts.NoteRow,
      Table.TAGS: _ts.TagRow,
      Table.DECK_CONFIG: _ts.DeckConfigRow,
      Table.DECKS: _ts.DeckRow,
      Table.CARDS: _ts.CardRow,
      Table.REVLOG: _ts.RevlogRow,
      Table.GRAVES: _ts.GraveRow,
    }
  )

  @classmethod
  def create(cls, path: str | Path) -> Self:
    """Create a database at `path` and execute the collection schema on it.

    `path` is either missing or an empty file, such as the temporary file
    `atomic_replace()` hands out. The returned object is closed and writable: enter it
    again to write to the new database.

    Raises:
      Genanki2Error: the file at `path` already holds a database.
      sqlite3.Error: the database cannot be created.
    """
    with cls(path, writable=True) as db:
      query: str = "SELECT EXISTS (SELECT 1 FROM sqlite_master) AS has_schema"
      result: dict[str, int] = db.connection.execute(query).fetchone()
      if result["has_schema"]:
        msg: str = f'Cannot create a collection: "{db.path}" already holds a database.'
        raise Genanki2Error(msg)
      db.connection.executescript(SCHEMA)
      return db

  def __init__(self, path: str | Path, *, writable: bool = False) -> None:
    """Record `path`, resolved (see `resolve_path()`), and whether writing is allowed.

    Nothing is opened or created until the `with` block is entered.

    Args:
      path: The database file.
      writable: Whether the connection may write, and create a missing file. Default:
        `False`.
    """
    self.path: Path = resolve_path(path)
    self.writable: bool = writable
    self._connection: sqlite3.Connection | None = None

  @property
  def connection(self) -> sqlite3.Connection:
    """The open connection.

    Raises:
      RuntimeError: no connection is open, which is the case outside a `with` block.
    """
    if self._connection is None:
      raise RuntimeError("No connection to the database has been created.")
    return self._connection

  def __enter__(self) -> Self:
    """Open the connection, registering the collation. Not reentrant.

    Raises:
      RuntimeError: a connection is already open.
      sqlite3.OperationalError: the file is missing and the connection is read-only, or
        the file cannot be opened.
    """
    if self._connection is not None:
      raise RuntimeError("A connection to the database already exists.")
    uri: str = self.path.as_uri()
    if not self.writable:
      uri = f"{uri}?mode=ro"
    self._connection = sqlite3.connect(uri, uri=True)
    self._connection.create_collation("unicase", unicase)
    self._connection.row_factory = dict_row_factory
    return self

  def __exit__(
    self,
    exc_type: type[BaseException] | None,
    exc_val: BaseException | None,
    exc_tb: TracebackType | None,
  ) -> None:
    """Commit, or roll back if the block raised, then close the connection."""
    try:
      self.connection.__exit__(exc_type, exc_val, exc_tb)
    finally:
      self.connection.close()
      self._connection = None

  ### One read_table overload per table:

  @overload
  def read_table(self, table: Literal[Table.COL]) -> _ts.ColRows: ...

  @overload
  def read_table(self, table: Literal[Table.CONFIG]) -> _ts.ConfigRows: ...

  @overload
  def read_table(self, table: Literal[Table.FIELDS]) -> _ts.FieldRows: ...

  @overload
  def read_table(self, table: Literal[Table.TEMPLATES]) -> _ts.TemplateRows: ...

  @overload
  def read_table(self, table: Literal[Table.NOTETYPES]) -> _ts.NotetypeRows: ...

  @overload
  def read_table(self, table: Literal[Table.NOTES]) -> _ts.NoteRows: ...

  @overload
  def read_table(self, table: Literal[Table.TAGS]) -> _ts.TagRows: ...

  @overload
  def read_table(self, table: Literal[Table.DECK_CONFIG]) -> _ts.DeckConfigRows: ...

  @overload
  def read_table(self, table: Literal[Table.DECKS]) -> _ts.DeckRows: ...

  @overload
  def read_table(self, table: Literal[Table.CARDS]) -> _ts.CardRows: ...

  @overload
  def read_table(self, table: Literal[Table.REVLOG]) -> _ts.RevlogRows: ...

  @overload
  def read_table(self, table: Literal[Table.GRAVES]) -> _ts.GraveRows: ...

  def read_table(self, table: Table) -> Rows:
    """Read every row of `table` as a dict, in a fixed order.

    The order is by id where available or by the columns it is keyed on.

    The columns are checked against the collection schema before the rows are read. Any
    missing column raises. An extra column is passed to the row model, but it is
    ignored until modelled.

    Raises:
      CollectionReadError: the database does not contain `table` or it is missing one
        or more columns from the collection schema.
      sqlite3.Error: the database cannot be read.
    """
    query: str = "SELECT name FROM pragma_table_info(?)"
    table_columns: set[str] = {
      column_info["name"] for column_info in self.connection.execute(query, (table,))
    }
    if not table_columns:
      raise CollectionReadError(
        f"Cannot read the collection: the database does not hold a {table} table. "
        "Only schema version 18 is supported."
      )

    if missing_columns := self.ROW_SCHEMA[table].__required_keys__ - table_columns:
      raise CollectionReadError(
        f"Cannot read the collection: the {table} table is missing the columns "
        f"{', '.join(sorted(missing_columns))}. Only schema version 18 is supported."
      )

    query = f"SELECT * FROM {table} {self.ORDERING_CLAUSE[table]}"
    return self.connection.execute(query).fetchall()

  def insert_table(self, table: Table, rows: Rows) -> Self:
    """Insert `rows` into `table` and commit. An empty `rows` does nothing.

    The column list is taken from the table schema, so a missing column raises.

    Raises:
      sqlite3.ProgrammingError: a row is missing a table column.
      sqlite3.OperationalError: the connection is read-only.
    """
    if not rows:
      return self
    columns: frozenset[str] = self.ROW_SCHEMA[table].__required_keys__
    placeholders: str = ", ".join(f":{col}" for col in columns)
    query: str = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    self.connection.executemany(query, rows)
    self.connection.commit()
    return self
