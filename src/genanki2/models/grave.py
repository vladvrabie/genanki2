import enum
from typing import Annotated

import pydantic as pyd
from typing_extensions import Self, override

from genanki2._typed_schema import GraveRow
from genanki2.errors import Genanki2Error
from genanki2.models.annotations import Id, UsnField
from genanki2.models.base import RowModel
from genanki2.models.serialization_context import SerializationContext


class GraveType(enum.IntEnum):
  """The kind of object a grave records as deleted."""

  CARD = 0
  NOTE = 1
  DECK = 2


class Grave(RowModel[GraveRow]):
  """One card, note or deck deleted since the last sync, as a row of the `graves` table.

  Only sync reads this table. It tells the server what to delete on its side and an
  upload clears it. Nothing in studying, importing or exporting looks at it.

  This model exists to enable reading a collection from a colpkg or a plain SQLite file.
  A caller should not add graves to a collection unless they want the server to delete
  data during the next sync.

  A grave models a deletion, but it doesn't guarantee the id is free. Most of the ids
  in a real collection's table name a row the collection currently holds (e.g. deleting
  a deck and re-importing it). Nothing is checked against the collection.

  Attributes:
    object_id: The id the deleted object had, half of the primary key.
    grave_type: What kind of object was deleted, half of the primary key.
    update_sequence_n: The sync marker. Defaults to `-1`, which means the deletion has
      not reached the server; a non-negative value is the number of the sync that last
      carried it. The two sides never agree on this table, and sync's own sanity check
      zeroes the client's count before it compares.
  """

  object_id: Annotated[int, Id, pyd.Field(alias="oid")]
  grave_type: Annotated[GraveType, pyd.Field(alias="type")]

  # Sync
  update_sequence_n: Annotated[int, UsnField] = -1

  # ----------------------------- Pre-serialization steps ------------------------------

  def check_default_deck_is_not_deleted(self) -> Self:
    """Reject a grave deleting deck 1, which Anki never writes and a sync acts on.

    "Deleting" the default deck empties it and resets its name, but Anki never
    creates a grave for it. Sync acts on a grave without checking, so the
    default deck gets deleted from the server and from every client.

    Raises:
      Genanki2Error: the grave names deck 1.
    """
    if self.object_id == 1 and self.grave_type == GraveType.DECK:
      raise Genanki2Error(
        "Check failed: the grave wants to delete deck 1, the default deck, which Anki "
        "refuses to delete. A sync would delete it on the server and on every client."
      )
    return self

  @override
  def check(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Reject a grave that cannot be written.

    `Grave.to_row()` calls this, so a grave is judged at the point it is written
    rather than by the sync it would otherwise reach.

    Args:
      recursive: Whether to check every model reachable from this one too, each once.
        If false, it only runs this model's own checks. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.check_default_deck_is_not_deleted()
    return super().check(recursive=recursive, context=context)

  # ---------------------------------- Serialization -----------------------------------

  @override
  @classmethod
  def from_row(cls, row: GraveRow) -> Self:
    """Build a grave from a `graves` row."""
    return super().from_row(row)

  @override
  def to_row(self, *, context: SerializationContext | None = None) -> GraveRow:
    """Return the `graves` row for this grave.

    If no context is given, one is created with output
    `SerializationContext.Output.ROW` and passed to the phases.
    """
    return super().to_row(context=context)
