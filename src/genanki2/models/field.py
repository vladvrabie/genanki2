import warnings
from typing import Annotated, ClassVar

import pydantic as pyd
from google.protobuf.message import Message
from typing_extensions import Self, override

import genanki2._proto as _proto  # noqa: PLR0402, import cycle via __init__
from genanki2._typed_schema import FieldRow
from genanki2.errors import Genanki2Warning
from genanki2.models.annotations import BytesDecoder, Int64, NotEmpty, UInt32
from genanki2.models.base import ProtobufModel, RowModel
from genanki2.models.serialization_context import SerializationContext
from genanki2.text import normalize_field_name


class FieldConfig(ProtobufModel):
  """The `fields.config` blob: everything a field carries beyond its name and position.

  The defaults match Anki's constructor. They are not the protobuf zeros, which is what
  an empty blob decodes to (e.g. font name and size decoded to 0 would render to
  invisible text). Serialization therefore sets every attribute from/to the
  message and never lets a default replace a value.

  Attributes:
    sticky: Keep the value in the editor after a note is added. Default: `False`.
    right_to_left: Render the field right to left. Default: `False`.
    font_name: The editor's font. A card is rendered with the notetype's CSS instead.
      Default: `"Arial"`.
    font_size: The editor's font size, in pixels, clamped to [5, 300] on save.
      Default: 20.
    description: Placeholder text shown in the editor while the field is empty.
      Default: `""`.
    plain_text: Edit as plain text rather than as rich text. Default: `False`.
    collapsed: Start collapsed in the editor. Default: `False`.
    exclude_from_search: Skip this field for a search term that names no field.
      Default: `False`.
    id: What makes a renamed field recognisable: two fields match on id and fall back
      to comparing names otherwise. Anki mints one in the 64-bit range. It never
      back-fills a missing one. Default is `None` (meaning unset in protobuf) because
      an id that changes from build to build is worse: the two ids differ, name
      matching is skipped and the notetype forks instead of updating. Use
      `Notetype.fill_missing_ids()` to set a stable id across builds.
    tag: Identifies a field by its role, independently of name and position, which is
      how a stock notetype's code finds its own fields after a user renames them. The
      cloze notetype tags both of its fields and image occlusion tags its first five.
      It means nothing on a field genanki2 invents, so it is carried rather than
      interpreted, and customizing a builtin has to preserve it or the notetype arrives
      structurally intact and functionally broken. Anki's database check restores the
      tags by position, but only on an image occlusion notetype, and only for a field
      that does not carry `prevent_deletion`. A cloze notetype that lost them is not
      repaired at all. Default: `None`.
    prevent_deletion: Refuse to remove the field from the notetype editor. Image
      occlusion sets it on the first four of its five tagged fields, and the cloze
      notetype on its Text field. It also decides which fields the tag repair
      above reaches, since a field carrying it is left as it is. Default: `False`.
    other: Schema 11 JSON keys with no protobuf fields, kept as raw bytes. They have to
      decode as a JSON object, otherwise Anki replaces anything that does not parse
      with an empty map. Default: `b""`.
  """

  MessageType: ClassVar[type[Message]] = _proto.FieldConfig

  sticky: bool = False
  right_to_left: Annotated[bool, pyd.Field(alias="rtl")] = False
  font_name: str = "Arial"
  font_size: Annotated[int, UInt32] = 20
  description: str = ""
  plain_text: bool = False
  collapsed: bool = False
  exclude_from_search: bool = False
  id: Annotated[int | None, Int64] = None
  tag: Annotated[int | None, UInt32] = None
  prevent_deletion: bool = False
  other: bytes = b""

  # ----------------------------- Pre-serialization steps ------------------------------

  def warn_out_of_range_font_size(self) -> Self:
    """Warn about a too large or too small font size."""
    if not (5 <= self.font_size <= 300):
      warnings.warn(
        f"FieldConfig: id {self.id} has a font size of {self.font_size}. Anki "
        "clamps the value to [5, 300] on save. A font size of 0 makes the the text "
        "invisible in the UI.",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  @override
  def warn(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Warn about a field configuration Anki accepts and the editor cannot present.

    Warnings can be turned off using `with genanki2.warnings_disabled():`.

    Args:
      recursive: Whether to warn about every model reachable from this one too, each
        once. If false, it only runs this model's own warnings. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.warn_out_of_range_font_size()
    return super().warn(recursive=recursive, context=context)


class Field(RowModel[FieldRow]):
  """One named slot of a notetype, which a note fills in with a value.

  A field carries no ordinal and no notetype id. Both are given by the notetype: the
  position in `Notetype.fields` is the ordinal, and `Notetype.to_rows()` supplies the id.

  Attributes:
    name: The name a template refers to, as `{{Front}}`. Repaired by
      `text.normalize_field_name()` before reaching Anki, so a collision created by the
      repair surfaces as a duplicate-name error from `Notetype.to_rows()`. Anki's own
      repair silently appends `+` until the name is free. `{{Front}}` may still resolve
      to whichever field kept the name, but only if it matches exactly. The renamed
      field becomes unreachable from every template, since none references `Front+`.
    config: The `fields.config` protobuf. Holds the font and size, the sticky, RTL and
      plain-text flags, the description shown in the editor, and the id Anki follows a
      rename by. Defaults to Anki's default values.
  """

  name: Annotated[str, NotEmpty, pyd.AfterValidator(normalize_field_name)]
  config: Annotated[
    FieldConfig, BytesDecoder(FieldConfig), pyd.PlainSerializer(FieldConfig._bytes)
  ] = pyd.Field(default_factory=FieldConfig)

  # ---------------------------------- Serialization -----------------------------------

  @override
  @classmethod
  def from_row(cls, row: FieldRow) -> Self:
    """Build a field from a `fields` table row.

    The `ntid` and `ord` columns are ignored: use `Notetype.from_rows()` to read and
    validate them.

    Field name is normalized and repaired by the model.
    """
    return super().from_row(row)

  @override
  def _row(self, notetype_id: int, ordinal: int) -> FieldRow:  # pyright: ignore[reportIncompatibleMethodOverride]
    """Serialize the field as a `fields` row, running no phase.

    Both arguments are the caller's responsibility, and neither is checked: the
    notetype is not looked up, and the ordinal is not compared with the field's
    position in it. The `fields` table declares no foreign key, so a `notetype_id`
    naming no notetype still writes a row.
    """
    return {**super()._row(), "ntid": notetype_id, "ord": ordinal}

  @override
  def to_row(  # pyright: ignore[reportIncompatibleMethodOverride]
    self, notetype_id: int, ordinal: int, *, context: SerializationContext | None = None
  ) -> FieldRow:
    """Return a `fields` row for this field, owned by `notetype_id` at `ordinal`.

    Both arguments are the caller's responsibility, and neither is checked: the
    notetype is not looked up, and the ordinal is not compared with the field's
    position in it. The `fields` table declares no foreign key, so a `notetype_id`
    naming no notetype still writes a row.

    Use `Notetype.to_rows()` to generate correct `fields` rows.

    If no context is given, one is created with output
    `SerializationContext.Output.ROW` and passed to the phases.
    """
    if context is None:
      context = SerializationContext(output=SerializationContext.Output.ROW)
    return self._run_phases(context=context)._row(notetype_id, ordinal)
