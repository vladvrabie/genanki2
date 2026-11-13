from typing import Annotated

import pydantic as pyd
from typing_extensions import Self, override

from genanki2._typed_schema import TagRow
from genanki2.models.annotations import UsnField
from genanki2.models.base import RowModel
from genanki2.models.serialization_context import SerializationContext
from genanki2.text import normalize_note_tag


class Tag(RowModel[TagRow]):
  """One registered tag name, as a row of the `tags` table.

  The table backs the browser's tag sidebar. Nothing during study reads it, and a
  note carries its own tags as text and doesn't point here.

  The table is not the union of the notes' tags. It holds some system tags and
  unused tags until a database check rebuilds the table. Parents are not registered
  either.

  An Anki export never writes this table and an import doesn't read it, rebuilding it
  from the notes.

  Attributes:
    tag: The name, primary key under `COLLATE unicase`. Repaired by
      `text.normalize_note_tag()`.
    collapsed: Whether the sidebar hides this tag's children. Anki's own tag object
      holds the opposite of it, `expanded`. Default: `True`.
    config: Reserved column, without a protobuf message. Anki doesn't read or write it.
      Default: `None`.
    update_sequence_n: The sync marker. Defaults to `-1`, which means the tag carries
      changes the server has not seen; a non-negative value is the number of the sync
      that last carried it. A package holds no tags, so an import registers the tag
      itself and stamps its own value.
  """

  tag: Annotated[str, pyd.AfterValidator(normalize_note_tag)]
  collapsed: bool = True
  config: bytes | None = None

  # Sync
  update_sequence_n: Annotated[int, UsnField] = -1

  # ---------------------------------- Serialization -----------------------------------

  @override
  @classmethod
  def from_row(cls, row: TagRow) -> Self:
    """Build a tag from a `tags` row."""
    return super().from_row(row)

  @override
  def to_row(self, *, context: SerializationContext | None = None) -> TagRow:
    """Return the `tags` row for this tag.

    If no context is given, one is created with output
    `SerializationContext.Output.ROW` and passed to the phases.
    """
    return super().to_row(context=context)
