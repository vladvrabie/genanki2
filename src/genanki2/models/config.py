import json
import warnings
from typing import Annotated, Any, TypeVar, overload

import pydantic as pyd
from typing_extensions import Self, override

from genanki2._typed_schema import ConfigRow
from genanki2.errors import Genanki2Warning
from genanki2.models.annotations import MTimeSecsAlias, TimeS, UInt63, UsnField
from genanki2.models.base import RowModel
from genanki2.models.serialization_context import SerializationContext

T = TypeVar("T")


class ConfigEntry(RowModel[ConfigRow]):
  """One setting of the collection, as a row of the key-value `config` table.

  An import does not copy this table into the target collection, so a setting written
  here never becomes a setting of the user's collection. What does reach: the
  scheduling keys decide the day count derived from the collection, and that count
  rebases the due date of every review card on the way in.

  Attributes:
    key: The setting's name, primary key.
    value: A JSON-encoded value. Anki falls back to the default value for that setting
      if it cannot decode this value. Default: `b"null"`.
    modification_time_s: Seconds since the epoch. Written through as given. A sync
      replaces the whole config table and stamps it with its own time, so this value
      is never compared. Defaults to the current time.
    update_sequence_n: The sync marker. Defaults to `-1`, which means the entry carries
      changes the server has not seen; a non-negative value is the number of the sync
      that last carried it. The server sets it, so a client only writes `-1`.
      Stamped along with `modification_time_s` and therefore not preserved across an
      import.
  """

  key: Annotated[str, pyd.Field(alias="KEY")]
  value: Annotated[bytes, pyd.Field(alias="val")] = b"null"

  # Sync
  # no not_in_future: Anki never reads it back
  modification_time_s: Annotated[int, UInt63, MTimeSecsAlias] = TimeS
  update_sequence_n: Annotated[int, UsnField] = -1

  # ---------------------------------- Derived values ----------------------------------

  @overload
  def value_or(self) -> Any | None: ...

  @overload
  def value_or(self, default_value: T) -> Any | T: ...

  def value_or(self, default_value: Any = None) -> Any:
    """Decode the value or return `default_value` if it cannot be decoded.

    Anki reads a setting the same way: a value that fails to parse is not an error but
    a setting that was never written and the default is returned.
    """
    try:
      return json.loads(self.value)
    except (json.JSONDecodeError, UnicodeDecodeError):
      return default_value

  def set_value(self, value: Any) -> Self:
    """Store `value` encoded as JSON, the way Anki writes a setting.

    The encoding is UTF-8 and compact, matching Anki.

    Raises:
      TypeError: `value` has no JSON form.
      ValueError: `value` holds NaN or an infinity. JSON has no spelling for them, and
        Anki would read the setting as never written.
    """
    self.value = json.dumps(
      value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()
    return self

  # ----------------------------- Pre-serialization steps ------------------------------

  def warn_unloadable_value(self) -> Self:
    """Warn about a config value that is not JSON.

    Anki decodes a setting on demand and falls back to the built-in default when the
    decode fails.
    """
    try:
      _ = json.loads(self.value)
    except (json.JSONDecodeError, UnicodeDecodeError):
      warnings.warn(
        f"ConfigEntry: {self.key!r} does not hold valid JSON, so Anki uses its default.",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  @override
  def warn(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Warn about a config value Anki cannot decode.

    Warnings can be turned off using `with genanki2.warnings_disabled():`.

    Args:
      recursive: Whether to warn about every model reachable from this one too, each
        once. If false, it only runs this model's own warnings. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.warn_unloadable_value()
    return super().warn(recursive=recursive, context=context)

  # ---------------------------------- Serialization -----------------------------------

  @override
  @classmethod
  def from_row(cls, row: ConfigRow) -> Self:
    """Build a config entry from a `config` row."""
    return super().from_row(row)

  @override
  def to_row(self, *, context: SerializationContext | None = None) -> ConfigRow:
    """Return the `config` row for this entry.

    If no context is given, one is created with output
    `SerializationContext.Output.ROW` and passed to the phases.
    """
    return super().to_row(context=context)
