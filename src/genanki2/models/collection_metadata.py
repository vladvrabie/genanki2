from typing import Annotated

import pydantic as pyd
from typing_extensions import Self, override

from genanki2._typed_schema import ColRow, ColRows
from genanki2.datetime import rollover_datetime
from genanki2.errors import CollectionReadError, Genanki2Error
from genanki2.models.annotations import ModAlias, TimeMs, UInt63, UsnField
from genanki2.models.base import RowModel
from genanki2.models.serialization_context import SerializationContext
from genanki2.sqlite import AnkiSQLite
from genanki2.time import time_s


class CollectionMetadata(RowModel[ColRow]):
  """The collection's own record, as the single row of the `col` table.

  An import doesn't copy it into the target collection, but it decides how the
  collection is read: the day count derived from `creation_time_s` and the scheduling
  settings rebases the due date of every review card, so a creation time that is
  wrong moves cards rather than being ignored.

  Only relevant table columns are held, the rest are constants or schema 11 remnants
  written by `CollectionMetadata.to_row()`.

  Attributes:
    creation_time_s: Seconds since the epoch, at the rollover hour on or before the
      moment of creation. Every relative due date counts days from it, so changing it
      without the review cards' `due`, or the reverse, moves every review card.
    modification_time_ms: Milliseconds since the epoch. Read only by sync. Defaults to
      the current time.
    schema_modification_time_ms: When the schema last changed. A value the server does
      not share forces a full sync. Read only by sync. Defaults to the current time.
    last_sync_time_ms: Last timestamp of a sync, read only by sync. `0` means
      "never synced". Default: 0.
    update_sequence_n: The last update sequence number the server sent. Default: 0
      (not -1).
  """

  creation_time_s: Annotated[int, UInt63, pyd.Field(alias="crt")] = pyd.Field(
    default_factory=lambda: time_s(rollover_datetime().timestamp())
  )
  modification_time_ms: Annotated[int, UInt63, ModAlias] = TimeMs
  schema_modification_time_ms: Annotated[int, UInt63, pyd.Field(alias="scm")] = TimeMs
  update_sequence_n: Annotated[int, UsnField] = 0
  last_sync_time_ms: Annotated[int, UInt63, pyd.Field(alias="ls")] = 0

  # ----------------------------- Pre-serialization steps ------------------------------

  @classmethod
  def check_row_schema_version(cls, row: ColRow) -> None:
    """Reject a `col` row if it doesn't declare version 18.

    Raises:
      CollectionReadError: the row declares a schema version other than 18.
    """
    if (version := row.get("ver")) != 18:
      raise CollectionReadError(
        f"Cannot read the collection: only schema version 18 is supported, but "
        f"row declares {version}."
      )

  def check_creation_time_not_in_future(self) -> Self:
    """Reject a creation time that has not happened yet as it would shift review cards.

    Raises:
      Genanki2Error: `creation_time_s` is in the future.
    """
    if self.creation_time_s > (now := time_s()):
      raise Genanki2Error(
        f"Check failed: creation time {self.creation_time_s} is in the future "
        f"(now is {now}). Anki counts due dates in days since the collection was "
        "created, so an import would shift the due date of every review card."
      )
    return self

  @override
  def check(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Reject metadata that negatively impacts the collection.

    `CollectionMetadata.to_row()` calls this, so metadata is checked at the point it is
    written instead of inside Anki.

    Args:
      recursive: Whether to check every model reachable from this one too, each once.
        If false, it only runs this model's own checks. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.check_creation_time_not_in_future()
    return super().check(recursive=recursive, context=context)

  # ---------------------------------- Serialization -----------------------------------

  @override
  @classmethod
  def from_row(cls, row: ColRow) -> Self:
    """Build the metadata from the `col` row, accepting only schema version 18.

    Raises:
      CollectionReadError: schema version is not 18.
    """
    cls.check_row_schema_version(row)
    return super().from_row(row)

  @classmethod
  def from_sqlite(cls, db: AnkiSQLite) -> Self:
    """Build the metadata from the single `col` row held by `db`.

    Raises:
      CollectionReadError: the `col` table does not hold exactly one row or declares a
        schema version different from 18.
      sqlite3.Error: the database cannot be read.
    """
    col_rows: ColRows = db.read_table(AnkiSQLite.Table.COL)
    if len(col_rows) != 1:
      raise CollectionReadError(
        "Cannot read the collection: the col table must have 1 row, but "
        f"{len(col_rows)} rows were found."
      )
    return cls.from_row(col_rows[0])

  @override
  def _row(self) -> ColRow:
    """Serialize the metadata as the `col` row with its constants, running no phase."""
    return {
      **super()._row(),
      "id": 1,
      "ver": 18,
      "dty": 0,
      "conf": "",
      "models": "",
      "decks": "",
      "dconf": "",
      "tags": "",
    }

  @override
  def to_row(self, *, context: SerializationContext | None = None) -> ColRow:
    """Return the `col` row for this metadata.

    Constant columns:
    - id: 1
    - version: 18
    - dty: 0
    - conf, models, decks, dconf, tags: "" (v11 remnants)

    If no context is given, one is created with output
    `SerializationContext.Output.ROW` and passed to the phases.
    """
    return super().to_row(context=context)
