import enum
import hashlib
import itertools as it
import random
import re
import warnings
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from typing import Annotated, Any, ClassVar

import pydantic as pyd
from google.protobuf.message import Message
from typing_extensions import Self, override

import genanki2._proto as _proto  # noqa: PLR0402, import cycle via __init__
from genanki2._typed_schema import FieldRows, NotetypeRow, TemplateRows
from genanki2.errors import CollectionReadError, Genanki2Error, Genanki2Warning
from genanki2.models.annotations import (
  BytesDecoder,
  Id,
  Int32,
  Int64,
  LtRUnion,
  MonotonicId,
  MTimeSecsAlias,
  NotEmpty,
  TimeS,
  UInt32,
  UInt63,
  UsnField,
)
from genanki2.models.base import ProtobufModel, TimestampedRowModel
from genanki2.models.field import Field
from genanki2.models.serialization_context import SerializationContext
from genanki2.models.template import Template
from genanki2.settings import _checks_enabled
from genanki2.sqlite import Row
from genanki2.text import (
  SectionReference,
  cloze_referenced_field_names,
  is_template_reference_reachable,
  normalize_field_name,
  normalize_notetype_name,
  normalize_template_name,
  notetype_underscored_css_imports,
  replace_notetype_underscored_css_imports,
  template_referenced_field_names,
  template_references,
)
from genanki2.time import time_s
from genanki2.utils import duplicates

DEFAULT_CSS: str = """.card {
    font-family: arial;
    font-size: 20px;
    line-height: 1.5;
    text-align: center;
    color: black;
    background-color: white;
}
"""
DEFAULT_CLOZE_CSS: str = """.cloze {
    font-weight: bold;
    color: blue;
}
.nightMode .cloze {
    color: lightblue;
}
"""
DEFAULT_LATEX_HEADER: str = """\\documentclass[12pt]{article}
\\special{papersize=3in,5in}
\\usepackage[utf8]{inputenc}
\\usepackage{amssymb,amsmath}
\\pagestyle{empty}
\\setlength{\\parindent}{0in}
\\begin{document}
"""
DEFAULT_LATEX_FOOTER: str = "\\end{document}"
SPECIAL_FIELDS: tuple[str, ...] = (
  "FrontSide",
  "Card",
  "CardFlag",
  "CardID",
  "Deck",
  "Subdeck",
  "Tags",
  "Type",
)
RE_CARD_REFERENCE: re.Pattern[str] = re.compile(r"\{\{\s*Card\s*\}\}")


class NotetypeConfig(ProtobufModel):
  """The `notetypes.config` blob: what a notetype carries beyond fields and templates.

  The defaults match Anki's constructor. They are not the protobuf zeros, which is what
  an empty blob decodes to (e.g. CSS styling and LaTeX skeleton would be dropped).
  Serialization therefore sets every attribute from/to the message and never lets a
  default replace a value.

  Attributes:
    kind: Normal or cloze. `Notetype.is_cloze()` reads it, and it decides how cards are
      generated, whether a card ordinal has to name a template, and whether a merge on
      import touches the templates at all. Default: `Kind.NORMAL`.
    sort_field_idx: Which field's value is written to `notes.sfld`, the column the
      browser's Sort Field sorts on and `dupes` searches. Default: 0.
    css: The styling every card of this notetype is rendered with. Underscored `@import`
      rules in it pull files into a package that no note mentions.
      Default: `DEFAULT_CSS`.
    target_deck_id_unused: Dead. Anki doesn't read it. Default: 0.
    latex_pre: The preamble prepended to a LaTeX fragment before it is rendered.
      Default: `DEFAULT_LATEX_HEADER`.
    latex_post: The postamble appended to a LaTeX fragment before it is rendered.
      Default: `DEFAULT_LATEX_FOOTER`.
    latex_svg: When enabled, render LaTeX to SVG rather than to a bitmap.
      Default: `False`.
    requirements: Which fields a card of each ordinal needs before it is generated.
      Never worth computing: Anki recomputes it on every add and update, import
      included, so whatever a package ships is overwritten. Defaults to an empty list.
    original_stock_kind: Which stock notetype this was copied from, recorded by Anki
      2.1.62 and later. Anki keys its image occlusion repair on it. The importer does
      not consult it when matching notetypes. Default: `OriginalStockKind.UNKNOWN`.
    original_id: The importer's own bookkeeping, the id the notetype had in the
      collection it came from, used to recognise a copy it made on an earlier import.
      It doesn't survive an import because Anki overwrites it. Default: `None`.
    other: Schema 11 JSON keys with no protobuf fields, kept as raw bytes. They have to
      decode as a JSON object, otherwise Anki replaces anything that does not parse
      with an empty map. Default: `b""`.
  """

  # ----------------------------------- Nested types -----------------------------------

  class Kind(enum.IntEnum):
    """Whether cards are generated per template or per cloze number."""

    NORMAL = 0
    CLOZE = 1

  class OriginalStockKind(enum.IntEnum):
    """The stock notetype this one was copied from, if any."""

    UNKNOWN = 0
    BASIC = 1
    BASIC_AND_REVERSED = 2
    BASIC_OPTIONAL_REVERSED = 3
    BASIC_TYPING = 4
    CLOZE = 5
    IMAGE_OCCLUSION = 6

  class CardRequirement(ProtobufModel):
    """The fields one card ordinal needs before Anki generates that card.

    Attributes:
      card_ord: The template ordinal this applies to.
      kind: Whether the card needs none of the fields, any one of them, or all of them.
      field_ords: The field ordinals `kind` is applied to.
    """

    class Kind(enum.IntEnum):
      """Whether a card needs any or all of its required fields to be non-empty."""

      NONE = 0
      ANY = 1
      ALL = 2

    MessageType: ClassVar[type[Message]] = _proto.CardRequirement

    card_ord: Annotated[int, UInt32]
    kind: Annotated[Kind | int, LtRUnion, Int32]
    field_ords: list[Annotated[int, UInt32]]

    # ---------------------------- Pre-serialization steps -----------------------------

    @override
    def warn(
      self, *, recursive: bool = True, context: SerializationContext | None = None
    ) -> Self:
      """Warn about a requirement kind neither Anki nor genanki2 recognises.

      Warnings can be turned off using `with genanki2.warnings_disabled():`.

      Args:
        recursive: Whether to warn about every model reachable from this one too, each
          once. If false, it only runs this model's own warnings. Default: `True`.
        context: Extra information from the serialization process, passed to every model
          during the walk. Default: `None`.
      """
      self.warn_int_value_in_enum_field()
      return super().warn(recursive=recursive, context=context)

  # ------------------------------------ Attributes ------------------------------------

  MessageType: ClassVar[type[Message]] = _proto.NotetypeConfig

  kind: Annotated[Kind | int, LtRUnion, Int32] = Kind.NORMAL
  sort_field_idx: Annotated[int, UInt32] = 0
  css: str = DEFAULT_CSS
  target_deck_id_unused: Annotated[int, Int64] = 0
  latex_pre: str = DEFAULT_LATEX_HEADER
  latex_post: str = DEFAULT_LATEX_FOOTER
  latex_svg: bool = False
  requirements: Annotated[list[CardRequirement], pyd.Field(alias="reqs")] = pyd.Field(
    default_factory=list
  )
  original_stock_kind: Annotated[OriginalStockKind | int, LtRUnion, Int32] = (
    OriginalStockKind.UNKNOWN
  )
  original_id: Annotated[int | None, Int64] = None
  other: bytes = b""

  @classmethod
  def new_cloze_config(cls, **kwargs: Any) -> Self:
    """Return the configuration Anki gives a new cloze notetype.

    This sets the stock styling with cloze styling and `KIND_CLOZE`. Keyword arguments
    set any other option, or replace the styling and the kind if passed.
    """
    kwargs = {"kind": cls.Kind.CLOZE, "css": DEFAULT_CSS + DEFAULT_CLOZE_CSS, **kwargs}
    return cls(**kwargs)

  # ----------------------------- Pre-serialization steps ------------------------------

  @override
  def warn(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Warn about a notetype or stock kind Anki does not recognise.

    Warnings can be turned off using `with genanki2.warnings_disabled():`.

    Args:
      recursive: Whether to warn about every model reachable from this one too, each
        once. If false, it only runs this model's own warnings. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.warn_int_value_in_enum_field()
    return super().warn(recursive=recursive, context=context)


class Notetype(TimestampedRowModel[NotetypeRow]):
  """The shape of a note: the fields it carries and the templates that render its cards.

  A notetype spans three tables, `notetypes` plus a row per field and per template, so
  `Notetype.from_rows()` and `Notetype.to_rows()` are the complete conversions.
  `Notetype.from_row()` and `Notetype.to_row()` handle the `notetypes` row alone.

  Attributes:
    id: Primary key. Anki uses it to match notetypes. A re-imported package keeps the
      same id and updates the notetype in place. Changing it forks the notetype.
      Defaults to a new monotonic id.
    name: Unique across the collection, compared case-insensitively. Repaired by
      `text.normalize_notetype_name()` before reaching Anki, so a collision created by
      the repair surfaces as a duplicate-name error from
      `Collection.check_unique_names()`.
      Left uncaught, Anki appends `+` until the name is free, unless the notetype
      already holding it has the same id, which is an update and not a collision.
      Nothing dangles, since a note binds to its notetype by id.
    fields: Named placeholders, in ordinal order. Defaults to an empty list.
    templates: HTML recipes, in ordinal order. Defaults to an empty list.
    config: The `notetypes.config` protobuf. Holds the CSS, the LaTeX preamble and
      postamble, `NotetypeConfig.sort_field_idx`, whether the notetype is cloze, and
      the original id the importer falls back to. Defaults to Anki's default values.
    modification_time_s: Seconds since the epoch. An existing notetype is updated only
      when the incoming one is newer. A time in the future is rejected because, if
      it lands in the collection, any later import with a time older than it is
      silently skipped. A notetype read with `Notetype.from_rows()` and changed
      afterwards, in its own row, a field or a template, gets the current time when
      it is written or frozen, unless the caller set a time. Defaults to the current
      time.
    update_sequence_n: The sync marker. Defaults to `-1`, which means the notetype
      carries changes the server has not seen; a non-negative value is the number of
      the sync that last carried it. The server sets it, so a client only writes `-1`.
      Stamped along with `modification_time_s` and therefore not preserved across an
      import.
  """

  id: Annotated[int, Id] = MonotonicId
  name: Annotated[str, NotEmpty, pyd.AfterValidator(normalize_notetype_name)]
  fields: Annotated[list[Field], pyd.Field(exclude=True)] = pyd.Field(
    default_factory=list
  )
  templates: Annotated[list[Template], pyd.Field(exclude=True)] = pyd.Field(
    default_factory=list
  )
  config: Annotated[
    NotetypeConfig,
    BytesDecoder(NotetypeConfig),
    pyd.PlainSerializer(NotetypeConfig._bytes),
  ] = pyd.Field(default_factory=NotetypeConfig)

  # Sync
  modification_time_s: Annotated[int, UInt63, MTimeSecsAlias] = TimeS
  update_sequence_n: Annotated[int, UsnField] = -1

  _FINGERPRINT_EXCLUDED: ClassVar[frozenset[str]] = frozenset(["mtime_secs", "usn"])

  # ---------------------------------- Derived values ----------------------------------

  # Idea: get_field(name), get_template(name)

  @property
  def field_names(self) -> tuple[str, ...]:
    """The field names, in list position order.

    Anki treats them as case-insensitive and requires them to be unique. Neither is
    checked here: the names come back as they are, and serialization checks enforce
    uniqueness.
    """
    return tuple(field.name for field in self.fields)

  def field_index(self, name: str) -> int:
    """Return the position of the field called `name`, casefolded as SQLite compares it.

    `name` is normalized like a field name is. The first match wins.

    Raises:
      Genanki2Error: `name` is normalized to an empty string.
      Genanki2Error: no field carries `name`.
    """
    normalized: str = normalize_field_name(name).casefold()
    for idx, field in enumerate(self.fields):
      if field.name.casefold() == normalized:
        return idx
    names: str = ", ".join(f'"{field.name}"' for field in self.fields)
    raise Genanki2Error(
      f'Could not find a field with name "{name}". Current field names: {names}.'
    )

  def field_values(self, mapping: Mapping[str, str]) -> list[str]:
    """Return the positional list `Note.fields` takes, from a mapping of names to values.

    The default value for fields not present in the mapping is `""`, so a partial
    mapping gives a note with empty fields.

    The map's keys are normalized for name-lookup.

    Returned values are not normalized.

    Raises:
      Genanki2Error: a key is normalized to an empty string.
      Genanki2Error: a key names a field that is not present in the notetype.
      Genanki2Error: two keys name the same field, so one of their values would be lost.
    """
    values: list[str] = [""] * len(self.fields)
    claimed_by: dict[int, str] = {}
    for name, value in mapping.items():
      idx: int = self.field_index(name)
      if idx in claimed_by:
        raise Genanki2Error(
          f'Keys "{claimed_by[idx]}" and "{name}" both name the field '
          f'"{self.fields[idx].name}", so one of the two values would be lost. Names '
          "are repaired and compared case-insensitively, the way Anki compares them."
        )
      claimed_by[idx] = name
      values[idx] = value
    return values

  @property
  def sort_field(self) -> Field | None:
    """The field by which notes/cards are sorted in the browser.

    Returns the last field when the sort field index is invalid (larger or equal to the
    field list length), as Anki clamps it when it saves the notetype. Returns `None`
    when the notetype has no fields.
    """
    if self.fields:
      sort_field_idx: int = min(self.config.sort_field_idx, len(self.fields) - 1)
      return self.fields[sort_field_idx]
    return None

  def set_sort_field(self, field: str | Field) -> Self:
    """Make `field` the sort field, given by name or as a `Field`.

    The name is matched case-sensitively.

    Passing a `Field` object does not modify the `Notetype.fields` list in any way.
    Only the `name` attribute is used to find a matching field in the list.

    Raises:
      Genanki2Error: the notetype has no field with this name.
    """
    field = field if isinstance(field, str) else field.name
    try:
      field_idx: int = self.field_names.index(field)
    except ValueError:
      names: str = ", ".join(f'"{name}"' for name in self.field_names)
      raise Genanki2Error(
        f'Could not find a field with name "{field}". Current field names: {names}.'
      ) from None
    self.config.sort_field_idx = field_idx
    return self

  def is_cloze(self) -> bool:
    """Return whether the notetype’s cards are numbered by cloze deletion.

    A cloze notetype has one template and as many cards per note as the note has cloze
    numbers, so a card ordinal names a cloze, not a template.
    """
    return self.config.kind == NotetypeConfig.Kind.CLOZE

  def is_normal(self) -> bool:
    """Return whether the notetype’s cards are numbered by template."""
    return self.config.kind == NotetypeConfig.Kind.NORMAL

  @property
  def template_names(self) -> tuple[str, ...]:
    """The template names, in list position order.

    Anki treats them as case-insensitive and requires them to be unique. Neither is
    checked here: the names come back as they are, and serialization checks enforce
    uniqueness.
    """
    return tuple(template.name for template in self.templates)

  def template_index(self, name: str) -> int:
    """Return the position of the template called `name`, casefolded as SQLite treats it.

    `name` is normalized like a template name is. The first match wins.

    Raises:
      Genanki2Error: `name` is normalized to an empty string.
      Genanki2Error: no template carries `name`.
    """
    normalized: str = normalize_template_name(name).casefold()
    for idx, template in enumerate(self.templates):
      if template.name.casefold() == normalized:
        return idx
    names: str = ", ".join(f'"{template.name}"' for template in self.templates)
    raise Genanki2Error(
      f'Could not find a template with name "{name}". Current template names: {names}.'
    )

  @property
  def underscored_css_imports(self) -> tuple[str, ...]:
    """The underscored media filenames that the CSS pulls in."""
    return notetype_underscored_css_imports(self.config.css)

  @override
  def _get_fingerprint(self) -> bytes:
    """Return a digest of the notetype, field and template rows, without the times.

    The notetype's and each template's modification time and sync marker are left
    out: Anki sets only the notetype's time when it saves an edit, and the template
    times are never compared. The columns of each row are sorted by name, while the
    fields and templates stay in ordinal order, so reordering them is a change.
    """

    def filter_pairs(
      row: Row, excluded: AbstractSet[str] | None = None
    ) -> list[tuple[str, Any]]:
      excluded = excluded or set()
      return sorted((k, v) for k, v in row.items() if k not in excluded)

    notetype_row, field_rows, template_rows = self._rows()
    notetype_pairs: list[tuple[str, Any]] = filter_pairs(
      notetype_row, self._FINGERPRINT_EXCLUDED
    )
    field_pairs: list[list[tuple[str, Any]]] = [filter_pairs(row) for row in field_rows]
    template_pairs: list[list[tuple[str, Any]]] = [
      filter_pairs(row, Template._FINGERPRINT_EXCLUDED) for row in template_rows
    ]
    pairs = notetype_pairs, field_pairs, template_pairs
    return hashlib.sha256(repr(pairs).encode()).digest()

  # ------------------------------------- Repairs --------------------------------------

  def fill_missing_ids(self) -> Self:
    """Give every field and template without one an id for Anki to match renames by.

    An id lets a rename be recognised as the same field or template. An id that changes
    between two package builds of the same deck is worse than no id because then name
    matching is skipped and the notetype forks instead of updating.

    Nothing calls this; a caller opts in. Calling it twice changes nothing because a
    config that already has an id is skipped.

    The builtin constants are skipped. They ship without ids so that an import
    matches them by name, and they are rebuilt in every process, so ids filled in one
    build would differ in the next. A notetype from `genanki2.builtin_notetype()` is
    filled like any other.

    Raises:
      AttributeError: the notetype is frozen and has ids to fill.
    """
    from genanki2.builtins import BUILTIN_NOTETYPES

    if any(self is constant for constant in BUILTIN_NOTETYPES):
      return self
    for items in (self.fields, self.templates):
      ids: set[int] = {item.config.id for item in items if item.config.id is not None}
      for model in items:
        if model.config.id is None:
          new_id: int = random.randrange(-(2**63), 2**63)
          while new_id in ids:
            new_id = random.randrange(-(2**63), 2**63)
          model.config.id = new_id
          ids.add(new_id)
    return self

  def repair_sort_field_idx(self) -> Self:
    """Clamp `NotetypeConfig.sort_field_idx` to the last field, the way Anki does.

    Anki repairs an out-of-range index and recomputes `notes.sfld`. Applying the
    same clamp here makes the stored value agree with the one Anki would settle on.

    Nothing calls this; a caller opts in. Reading leaves the index alone so that a
    package written by another tool survives a read-update-write cycle unchanged.
    Writing leaves it alone so that a notetype is never silently mutated.

    The index is unchanged when the notetype has no fields.
    """
    if self.fields:
      self.config.sort_field_idx = min(self.config.sort_field_idx, len(self.fields) - 1)
    return self

  def replace_underscored_css_imports(self, renames: Mapping[str, str]) -> Self:
    """Rewrite the CSS's imports, renaming every file the mapping names.

    This method helps a caller automate the process of repairing media file names that
    are rejected by Anki. `Package.check_media_paths()` names the files a caller has to
    rename on disk, but renaming them leaves the references to the old names dangling.
    This repairs all those references.

    A name is matched literally, without decoding HTML entities: an import of
    `_a&amp;b.css` is renamed under that spelling.

    Anki's detection rule applies: only names beginning with "_" are repaired,
    in the same way `Notetype.underscored_css_imports` finds them.

    The renames apply in mapping order and they compose.

    Nothing calls this; a caller opts in.

    Args:
      renames: The old name of each renamed file, mapped to its new name.
    """
    new_css: str = self.config.css
    for old, new in renames.items():
      new_css = replace_notetype_underscored_css_imports(new_css, old, new)
    if self.config.css != new_css:
      self.config.css = new_css
    return self

  # ----------------------------- Pre-serialization steps ------------------------------

  @classmethod
  def check_row_consistent_notetype_id(
    cls, row: NotetypeRow, field_rows: FieldRows, template_rows: TemplateRows
  ) -> None:
    """Reject rows that do not all name the same notetype.

    Fields and templates refer to their notetype by the `ntid` column. Because their
    models drop it, they need to agree on a read, otherwise `Notetype.to_rows()` will
    mark them as belonging to the notetype being constructed from `row`.

    No error is raised when none of the rows carry a notetype id. A `notetypes` row
    without `id` takes a new one from the model default.

    Args:
      row: The `notetypes` row.
      field_rows: The `fields` rows.
      template_rows: The `templates` rows.

    Raises:
      CollectionReadError: the ids present in the rows name more than one notetype.
    """
    ntids: set[int] = {row["id"]} if "id" in row else set()
    rows: it.chain[Row] = it.chain(field_rows, template_rows)
    ntids.update(row["ntid"] for row in rows if "ntid" in row)
    if len(ntids) > 1:
      raise CollectionReadError(
        f"Row check failed: fields and templates disagree with the notetype id. Ids: "
        f"{sorted(ntids)}"
      )

  @classmethod
  def check_row_contiguous_ordinals(
    cls, source: str, rows: FieldRows | TemplateRows
  ) -> None:
    """Reject field or template rows whose ordinals are not 0 to n-1, each used once.

    An ordinal is not decoration: for a field it is its position in the note's value
    list, and for a card it names the template that renders it. A gap or a repeat
    therefore misreads every note of the notetype.

    Rows read from the database always carry `ord`, so a row without one is an error.
    This can be avoided by building the notetype from `Field` and `Template`
    objects, in which case `ord` is given by list position.

    Args:
      source: "field" or "template".
      rows: field or template rows to validate.

    Raises:
      CollectionReadError: a row is missing `ord` or the ordinals are not contiguous
        from 0.
    """
    if len(rows) == 0:
      return
    ords: list[int] = sorted(row["ord"] for row in rows if "ord" in row)
    if not ords:
      raise CollectionReadError(
        f"Row check failed: {source}s are missing ordinals. A row read from the "
        f"database always carries `ord`. To order by list position instead, build the "
        f"notetype from `genanki2.{source.title()}` objects."
      )
    if len(ords) != len(rows):
      raise CollectionReadError(f"Row check failed: not all {source}s have an ordinal.")
    if ords != list(range(len(rows))):
      raise CollectionReadError(
        f"Row check failed: {source}s don't have contiguous ordinals from 0 to "
        f"{len(rows) - 1}. Values: {ords}"
      )

  def check_non_empty_fields(self) -> Self:
    """Reject a notetype with no fields.

    Anki requires at least one.

    Raises:
      Genanki2Error: the notetype has no fields.
    """
    if not self.fields:
      msg: str = "Check failed: notetype has no fields; Anki requires at least one."
      raise Genanki2Error(msg)
    return self

  def check_non_empty_templates(self) -> Self:
    """Reject a notetype with no templates.

    Anki requires at least one.

    Raises:
      Genanki2Error: the notetype has no templates.
    """
    if not self.templates:
      msg: str = "Check failed: notetype has no templates; Anki requires at least one."
      raise Genanki2Error(msg)
    return self

  def check_unique_ids(self) -> Self:
    """Reject an id carried by more than one field, or by more than one template.

    An id is what lets Anki follow a rename. The match takes the first id it finds, so
    a repeated one is dropped and field values are put in the wrong field.

    An id of `None` takes part in no match, so it is skipped rather than counted as a
    collision.

    Raises:
      Genanki2Error: two fields, or two templates, carry the same id.
    """
    for source, items in (("field", self.fields), ("template", self.templates)):
      if ids := duplicates(
        item.config.id for item in items if item.config.id is not None
      ):
        raise Genanki2Error(
          f"Check failed: {source} ids are not unique. First {source} encountered "
          "will be added to the collection (or dropped), while the rest will be "
          f"dropped. Duplicates: {ids}"
        )
    return self

  def check_unique_names(self) -> Self:
    """Reject field or template names that collide case-insensitively.

    Fields and templates are separate namespaces, so a field and a template may share a
    name.

    Anki repairs a collision instead of reporting it, appending `+` until it is free.
    Raising is deliberate: the repaired name is the second one seen in the list and
    every template referencing the repaired name now resolves to the unrepaired one.

    Raises:
      Genanki2Error: two fields, or two templates, share a name after casefolding.
    """
    for source, items in (("field", self.fields), ("template", self.templates)):
      if names := duplicates(item.name.casefold() for item in items):
        raise Genanki2Error(
          f"Check failed: {source} names are not unique. They are treated as case-"
          f'insensitive, so "Text" and "text" are identical. Duplicates: {names}'
        )
    return self

  def check_unique_fronts(self) -> Self:
    """Reject two templates with the same front side, unless it references `{{Card}}`.

    Two identical front sides generate two identical cards, which is the copy-paste
    mistake this exists to catch.

    `{{Card}}` expands to the name of the template being rendered, so a front
    containing it renders differently for every template that shares it. That is a
    deliberate Anki design. The reference is matched literally, tolerating whitespace
    inside the braces and nothing else.

    Raises:
      Genanki2Error: two templates share a front that does not reference `{{Card}}`.
    """
    format_to_names: dict[str, list[str]] = {}
    for template in self.templates:
      format_to_names.setdefault(template.config.q_format, []).append(template.name)
    if names := [
      format_to_names[format]
      for format in duplicates(template.config.q_format for template in self.templates)
      if not RE_CARD_REFERENCE.search(format)
    ]:
      raise Genanki2Error(
        f"Check failed: template fronts are not unique. Duplicate groups by template "
        f"name: {names}"
      )
    return self

  def check_templates_field_references(self) -> Self:
    """Reject a template side referencing a name that is neither a field nor special.

    An unknown reference renders as an error on the card and on a front it also stops
    the card from being generated because a name never counts as filled when the note
    has no value for it. The apkg importer builds notetypes with its checks skipped,
    so nothing catches this and the deck imports broken.

    Two kinds of name are accepted without being fields: the special names Anki fills
    in and the empty name, which add-ons use. Conditionals count as references, so
    `{{#Foo}}` needs a field `Foo`.

    Names are compared exactly. Field names are stored NFC-normalized while template
    text is left as written, so a reference spelled with decomposed characters does not
    match the field it names.

    Raises:
      Genanki2Error: a side references a name the notetype does not carry.
      Genanki2Error: a side does not parse. Call `Template.check_templates_parse()`
        first for an error that names the side.
    """
    known_fields: set[str] = {*self.field_names, *SPECIAL_FIELDS, ""}
    for template in self.templates:
      q_format, a_format = template.config.q_format, template.config.a_format
      for side, format in (("front", q_format), ("back", a_format)):
        if unknown_references := [
          reference
          for reference in template_referenced_field_names(format)
          if reference not in known_fields
        ]:
          raise Genanki2Error(
            f'Check failed: the {side} side of template "{template.name}" contains '
            f"unknown field references. Unknown: {unknown_references}"
          )
    return self

  def check_cloze_template_uses_filter(self) -> Self:
    """Reject a cloze notetype whose first template lacks the `cloze` filter.

    A cloze notetype needs at least one `cloze:` reference on both sides of its first
    template, otherwise the cards render as an error. Only the first template is
    checked because a cloze notetype assumes one is enough (cards are numbered by cloze
    deletion, not by template).

    Raises:
      Genanki2Error: the notetype has no templates.
      Genanki2Error: the first template is missing a cloze reference on either side.
    """
    if self.is_normal():
      return self
    self.check_non_empty_templates()
    q_format: str = self.templates[0].config.q_format
    front_references: frozenset[str] = cloze_referenced_field_names(q_format)
    a_format: str = self.templates[0].config.a_format
    back_references: frozenset[str] = cloze_referenced_field_names(a_format)
    if not front_references or not back_references:
      raise Genanki2Error(
        "Check failed: a cloze notetype needs at least one cloze reference on both "
        "sides of its first template or its cards render as an error."
      )
    return self

  def check_mod_time_not_in_future(self) -> Self:
    """Reject a modification time that has not happened yet because it blocks updates.

    Raises:
      Genanki2Error: `modification_time_s` is in the future.
    """
    if self.modification_time_s > (now := time_s()):
      raise Genanki2Error(
        f"Check failed: modification time {self.modification_time_s} is in the future "
        f"(now is {now}). Anki updates an existing notetype only when the incoming one "
        "is newer, so once this time is in the collection, an import made before the "
        "clock catches up compares older and its fields and templates silently stop "
        "updating."
      )
    return self

  @override
  def check(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Reject a notetype that cannot be written.

    `Notetype.to_rows()` calls this, so a notetype is validated at the point it is
    written instead of inside Anki, and a half-built one can be passed around freely.

    Order matters only in that template parsing comes before anything that reads
    references, so a syntax error is reported as one.

    Args:
      recursive: Whether to check every model reachable from this one too, each once.
        If false, it only runs this model's own checks. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.check_non_empty_fields()
    self.check_non_empty_templates()
    self.check_unique_ids()
    self.check_unique_names()
    self.check_unique_fronts()
    for template in self.templates:
      template.check_templates_parse()
    # Some checks assume templates parse (*)
    self.check_templates_field_references()  # *
    self.check_cloze_template_uses_filter()  # *
    self.check_mod_time_not_in_future()
    return super().check(recursive=recursive, context=context)

  def warn_empty_card_generation(self) -> Self:
    """Warn about a front that can never generate a card.

    Anki accepts these, the note imports and the card never appears. The template looks
    correct while doing nothing.

    A card is generated when the front holds at least one plain replacement whose name
    is filled for that note and whose enclosing conditionals are satisfied. A
    conditional's own name never counts, only a replacement inside its body counts. A
    front is dead when no note can do that. `FrontSide`, the empty name and cloze
    numbers such as `c1` are never filled, and the other special names except `Tags`
    always are, so `{{#c1}}` and `{{^Deck}}` are never satisfied. A field with a
    special name is an ordinary field. `Tags` is filled only for a tagged note, and a
    front that depends on it alone is reported separately as the weaker case.

    The first template is exempt when every front is dead: Anki creates card 0 for a
    note that would otherwise get none.

    A cloze notetype is skipped. Its cards come from the cloze numbers in the note's
    fields and the front is not consulted.
    """
    if self.is_cloze() or not self.templates:
      return self

    field_names: set[str] = set(self.field_names)
    always_filled: set[str] = set(SPECIAL_FIELDS) - {"FrontSide", "Tags"} - field_names
    fillable: set[str] = field_names | always_filled | {"Tags"}

    statuses: list[str] = []
    RENDERABLE: str = "renderable"
    NO_FILLED_FIELDS: str = "no_filled_fields"
    ONLY_TAGS: str = "only_tags"

    for template in self.templates:
      filled_fields: set[str] = {
        ref.key
        for ref in template_references(template.config.q_format)
        if not ref.is_conditional
        and is_template_reference_reachable(
          (*ref.sections, SectionReference(ref.key, True)), fillable, always_filled
        )
      }
      if not filled_fields:
        statuses.append(NO_FILLED_FIELDS)
      elif filled_fields == {"Tags"} and "Tags" not in field_names:
        statuses.append(ONLY_TAGS)
      else:
        statuses.append(RENDERABLE)

    if all(status == NO_FILLED_FIELDS for status in statuses):
      statuses[0] = RENDERABLE

    for template, status in zip(self.templates, statuses):
      if status == NO_FILLED_FIELDS:
        warnings.warn(
          f'Notetype: "{self.name}" template "{template.name}" has a front side with '
          "no field references or ones that cannot be replaced. Anki generates no card "
          "for it, even though the notes get imported.",
          Genanki2Warning,
          stacklevel=2,
        )
      elif status == ONLY_TAGS:
        warnings.warn(
          f'Notetype: "{self.name}" template "{template.name}" has a front side '
          "containing only the special {{Tags}} field. Anki generates its card for a "
          "tagged note, but no card for an untagged one.",
          Genanki2Warning,
          stacklevel=2,
        )

    return self

  def warn_unused_fields(self) -> Self:
    """Warn about fields that are not referenced by any template.

    Anki does not object. The fields are stored on every note and never rendered.

    Two fields are exempt. The sort field is in use without a template, since the
    browser's Sort Field column shows it. Image Occlusion's Comments field is in no
    template by design, and Anki lets the user delete it, unlike the notetype's other
    fields. It is found by the tag Anki gives it, so it stays exempt when renamed.
    """
    unused: set[str] = set(self.field_names)
    if len(unused) <= 1:  # empty or holding the sort field, which gets removed
      return self
    # self.fields cannot be empty now
    unused.discard(self.sort_field.name)  # pyright: ignore[reportOptionalMemberAccess]
    # Remove Image Occlusion's Comments field because it is in no template by design
    OriginalStockKind = NotetypeConfig.OriginalStockKind
    if self.config.original_stock_kind == OriginalStockKind.IMAGE_OCCLUSION:
      comment_field_tag: int = 4
      unused -= {
        field.name for field in self.fields if field.config.tag == comment_field_tag
      }
    for template in self.templates:
      unused -= template_referenced_field_names(template.config.q_format)
      unused -= template_referenced_field_names(template.config.a_format)
      unused -= template_referenced_field_names(template.config.q_format_browser)
      unused -= template_referenced_field_names(template.config.a_format_browser)
    if unused:
      msg: str = f'Notetype: "{self.name}" has unused fields: {tuple(sorted(unused))}'
      warnings.warn(msg, Genanki2Warning, stacklevel=2)
    return self

  def warn_sort_field_idx_out_of_range(self) -> Self:
    """Warn when `NotetypeConfig.sort_field_idx` points past the last field.

    Anki clamps it when it saves the notetype, which an import does before writing any
    note, so the deck arrives with a different sort field than the package asked for.

    Use `Notetype.repair_sort_field_idx()` to clamp it as Anki does.
    """
    if self.fields and self.config.sort_field_idx >= len(self.fields):
      warnings.warn(
        f'Notetype: "{self.name}" has a sort field index ({self.config.sort_field_idx}) '
        f"that exceeds the number of fields ({len(self.fields)}). Whenever out of "
        "range, a genanki2.Notetype will use the highest correct value, but will not "
        "change the stored value.",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  def warn_multi_template_cloze(self) -> Self:
    """Warn about a cloze notetype carrying more than one template.

    A cloze note's cards are numbered by the cloze deletions in its fields, not by
    template, so a card ordinal names a cloze number and no card ever names a template
    after the first. The extra templates are inert.
    """
    if self.is_cloze() and len(self.templates) > 1:
      warnings.warn(
        f'Notetype: cloze "{self.name}" has more than one template. The extra ones '
        "after the first are not used by Anki to generate cards.",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  # Possible warning: invalid field references in browser templates

  @override
  def warn(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Warn about a notetype Anki accepts, but which has unwanted behaviour.

    Order matters only in that template parsing comes before anything that reads
    references, so a syntax error is reported as one.

    Warnings can be turned off using `with genanki2.warnings_disabled():`.

    Args:
      recursive: Whether to warn about every model reachable from this one too, each
        once. If false, it only runs this model's own warnings. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    # Some checks assume templates parse (*)
    self.warn_empty_card_generation()  # *
    self.warn_unused_fields()  # *
    self.warn_sort_field_idx_out_of_range()
    self.warn_multi_template_cloze()
    return super().warn(recursive=recursive, context=context)

  # ---------------------------------- Serialization -----------------------------------

  @override
  @classmethod
  def from_row(cls, row: NotetypeRow) -> Self:
    """Build a notetype from a `notetypes` row, without its fields and templates.

    The result cannot be written to the database, since `Notetype.to_rows()` requires
    at least one field and template. Use `Notetype.from_rows()` to read a complete
    notetype.

    The notetype takes its read time and content as its last stamp, to detect later
    changes to itself, its fields or its templates. Fields and templates added
    afterwards count as a change.
    """
    return super().from_row(row)

  @classmethod
  def from_rows(
    cls, notetype_row: NotetypeRow, field_rows: FieldRows, template_rows: TemplateRows
  ) -> Self:
    """Build a notetype from its `notetypes`, `fields` and `templates` rows.

    Rows are sorted by `ord`, which becomes list position and is then discarded to
    avoid ambiguous and contradicting information.

    The notetype takes its read time and content as its last stamp, to detect later
    changes to itself, its fields or its templates. On change, it updates the
    modification time to now when it is written or frozen, and that write becomes the
    last stamp.

    Raises:
      CollectionReadError: the rows name more than one notetype.
      CollectionReadError: a field or template row is missing `ord`, or the ordinals
        are not contiguous from 0.
    """
    cls.check_row_consistent_notetype_id(notetype_row, field_rows, template_rows)
    cls.check_row_contiguous_ordinals("field", field_rows)
    cls.check_row_contiguous_ordinals("template", template_rows)
    field_rows = sorted(field_rows, key=lambda row: row["ord"])
    template_rows = sorted(template_rows, key=lambda row: row["ord"])
    return super().from_row(
      {**notetype_row, "fields": field_rows, "templates": template_rows}
    )

  @override
  def to_row(self, *, context: SerializationContext | None = None) -> NotetypeRow:
    """Return the `notetypes` row, without the fields and templates.

    Those live in their own tables. `Notetype.to_rows()` writes all three together.

    `Notetype.check()` and `Notetype.warn()` are not called. Only row-local checks are
    called. A frozen notetype is not prepared again, since `freeze()` prepared it.

    If no context is given, one is created with output
    `SerializationContext.Output.ROW` and passed to the phases.
    """
    if context is None:
      context = SerializationContext(output=SerializationContext.Output.ROW)
    if not self._frozen:
      self.prepare(context=context)
    if _checks_enabled.get():
      self.check_mod_time_not_in_future()
    return self._row()

  def _rows(self) -> tuple[NotetypeRow, FieldRows, TemplateRows]:
    """Serialize the notetype as its `notetypes`, `fields` and `templates` rows.

    No phase runs. Ordinals for fields and templates come from list position.
    """
    field_rows: FieldRows = tuple(
      field._row(self.id, ord) for ord, field in enumerate(self.fields)
    )
    template_rows: TemplateRows = tuple(
      template._row(self.id, ord) for ord, template in enumerate(self.templates)
    )
    return self._row(), field_rows, template_rows

  def to_rows(
    self, *, context: SerializationContext | None = None
  ) -> tuple[NotetypeRow, FieldRows, TemplateRows]:
    """Return the `notetypes`, `fields` and `templates` rows, in that order.

    The completeness check lives here, at the point the notetype claims to be finished.
    See each `Notetype.check_*()` method documentation for details.

    Ordinals for fields and templates come from list position.

    If no context is given, one is created with output
    `SerializationContext.Output.ROW` and passed to the phases.
    """
    if context is None:
      context = SerializationContext(output=SerializationContext.Output.ROW)
    return self._run_phases(context=context)._rows()
