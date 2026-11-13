import hashlib
import warnings
from collections.abc import Mapping
from typing import Annotated, ClassVar

import pydantic as pyd
from typing_extensions import Self, override

from genanki2._typed_schema import NoteRow
from genanki2.errors import CollectionReadError, Genanki2Error, Genanki2Warning
from genanki2.models.annotations import (
  Guid,
  Id,
  ModAlias,
  MonotonicId,
  NotEmpty,
  TimeS,
  UInt63,
  UsnField,
)
from genanki2.models.base import TimestampedRowModel
from genanki2.models.notetype import SPECIAL_FIELDS, Notetype
from genanki2.models.serialization_context import SerializationContext
from genanki2.models.tag import Tag
from genanki2.settings import _dbcheck_repairs_enabled
from genanki2.text import (
  FIELD_VALUE_SEPARATOR,
  cloze_referenced_field_names,
  field_value_cloze_latex_image_names,
  field_value_cloze_numbers,
  field_value_latex_image_names,
  field_value_media_refs,
  is_field_value_empty,
  normalize_field_value,
  normalize_note_fields,
  normalize_note_tags,
  replace_field_value_media_refs,
  split_note_tags,
  strip_field_value_html_preserving_media_names,
)
from genanki2.time import time_s


class Note(TimestampedRowModel[NoteRow]):
  """The content behind a card: one value per notetype field, plus the tags.

  The cards it produces are separate objects, one per template that renders, and are
  not held here.

  Attributes:
    id: Primary key. Unique within the package is sufficient. The importer renumbers an
      incoming note whose id is already taken in the target collection. Defaults to a
      new monotonic id.
    guid: The identifier the importer matches on. A re-imported package keeps the same
      guid and updates the note in place. Changing it adds a duplicate. Defaults to a
      new guid.
    notetype: The named prototype with named fields and templates.
    fields: One value per notetype field, in the notetype's field order. Repaired by
      `text.normalize_field_value()`. The count is not checked here, since it belongs
      to the notetype and the notetype stays mutable: `Note.from_row()` and
      `Note.to_row()` check it, and Anki fails the whole import when it disagrees.
      Defaults to an empty list.
    tags: Repaired by `text.normalize_note_tags()`. The list is deduplicated
      case-insensitively (first spelling seen wins) and sorted (the way Anki stores
      them). A tag holding a tag separator is rejected because Anki splits on it,
      quietly turning one tag into two. Defaults to an empty list.
    modification_time_s: Seconds since the epoch. An existing note is updated only when
      the incoming one is newer. A time in the future is clamped to now, outside
      `genanki2.dbcheck_repairs_disabled()`, because if it lands in the collection,
      any later import with a time older than it is silently skipped. A note read
      with `Note.from_row()` and changed afterwards gets the current time when it is
      written or frozen, unless the caller set a time. Defaults to the current time.
    update_sequence_n: The sync marker. Defaults to `-1`, which means the note carries
      changes the server has not seen; a non-negative value is the number of the sync
      that last carried it. The server sets it, so a client only writes `-1`.
      Stamped along with `modification_time_s` and therefore not preserved across an
      import.
  """

  id: Annotated[int, Id] = MonotonicId
  guid: Annotated[str, NotEmpty] = Guid
  notetype: Notetype
  fields: Annotated[list[str], NotEmpty, pyd.AfterValidator(normalize_note_fields)]
  tags: Annotated[list[str], pyd.AfterValidator(normalize_note_tags)] = pyd.Field(
    default_factory=list
  )

  # Sync
  modification_time_s: Annotated[int, UInt63, ModAlias] = TimeS
  update_sequence_n: Annotated[int, UsnField] = -1

  _FINGERPRINT_EXCLUDED: ClassVar[frozenset[str]] = frozenset(
    ["mod", "usn", "sfld", "csum"]
  )

  # ------------------------------------ Validators ------------------------------------

  @pyd.model_validator(mode="after")
  def clamp_modification_time(self) -> Self:
    """Replace a modification time in the future with now, warning that it changed.

    Anki updates an existing note only when the incoming one is newer, so a note kept
    at a future time stops updating until the clock catches up. Anki's database check
    makes the same repair, but only for notes.
    """
    if _dbcheck_repairs_enabled.get():
      self.repair_modification_time()
    return self

  # ----------------------------------- Field access -----------------------------------

  def __contains__(self, field: str) -> bool:
    """Return whether the notetype has a field with this name.

    The name is matched case-sensitively.
    """
    return field in self.notetype.field_names

  def __getitem__(self, field: str) -> str:
    """Return the value of the notetype field with this name.

    The name is matched case-sensitively.

    Raises:
      KeyError: the notetype has no field with this name.
      Genanki2Error: the note's value count differs from the notetype's field count,
        so a name cannot be matched to a value.
    """
    self.check_fields_len()
    try:
      return self.fields[self.notetype.field_names.index(field)]
    except ValueError:
      msg: str = f"Note type {self.notetype.name!r} has no field {field!r}."
      raise KeyError(msg) from None

  def __setitem__(self, field: str, value: str) -> None:
    """Set the value of the notetype field with this name.

    The name is matched case-sensitively.

    The value is repaired as any assignment to `fields` is.

    Raises:
      KeyError: the notetype has no field with this name.
      Genanki2Error: the note's value count differs from the notetype's field count,
        so a name cannot be matched to a value.
    """
    self.check_fields_len()
    try:
      index: int = self.notetype.field_names.index(field)
    except ValueError:
      msg: str = f"Note type {self.notetype.name!r} has no field {field!r}."
      raise KeyError(msg) from None
    fields: list[str] = list(self.fields)
    fields[index] = value
    self.fields = fields

  # ---------------------------------- Derived values ----------------------------------

  @property
  def checksum_field(self) -> str:
    """First field value, normalized and stripped of HTML."""
    return strip_field_value_html_preserving_media_names(
      normalize_field_value(self.fields[0])
    )

  @property
  def filled_fields(self) -> frozenset[str]:
    """The field names a card front can reference and get content for this note.

    Anki generates a card when its front references one of these names and the note
    satisfies every conditional around the reference.

    A field is filled unless its value holds only whitespace and line breaks. The
    special names are filled for every note, except `FrontSide`, which never is, and
    `Tags`, which is filled only when the note has tags. A field with a special name's
    name replaces it and is filled by its own value.
    """
    always_filled: set[str] = set(SPECIAL_FIELDS) - {"FrontSide"}
    if not self.tags:
      always_filled -= {"Tags"}
    always_filled -= set(self.notetype.field_names)
    filled: set[str] = {
      field
      for field, value in zip(self.notetype.field_names, self.fields)
      if not is_field_value_empty(normalize_field_value(value))
    }
    return frozenset(filled | always_filled)

  @property
  def sort_field(self) -> str:
    """The value the browser's Sort Field column sorts and searches on.

    One of the note's own field values. `NotetypeConfig.sort_field_idx` decides which
    field is given. Out of range yields `""` to match Anki's fallback when writing a
    note (Anki clamps the index when it saves the notetype).

    It is normalized and stripped of HTML.
    """
    sort_field_idx: int = self.notetype.config.sort_field_idx
    if sort_field_idx >= len(self.fields):
      return ""
    field: str = normalize_field_value(self.fields[sort_field_idx])
    return strip_field_value_html_preserving_media_names(field)

  @property
  def latex_image_references(self) -> tuple[str, ...]:
    """The images Anki renders from the LaTeX in the note's fields, in field order.

    A package carries them as ordinary media files. Anki renders a missing one when a
    card is shown only if LaTeX generation is turned on, and it is off by default, so
    most users see an image only if the package carries it. genanki2 cannot render
    LaTeX, so the files come from Anki's media folder.

    They are kept apart from `Note.media_references`: a missing image is not a dangling
    reference, since Anki's own export leaves out any image it never rendered, and
    `Note.replace_media_references()` cannot rename a name derived from the LaTeX.

    LaTeX holding a cloze also names the images its cards render, with each cloze
    hidden and revealed, after the name of the LaTeX as written.

    Each field is normalized before it is searched. Duplicates are kept.
    """
    svg: bool = self.notetype.config.latex_svg
    references: list[str] = []
    for field in self.fields:
      field = normalize_field_value(field)
      references.extend(field_value_latex_image_names(field, svg))
      references.extend(field_value_cloze_latex_image_names(field, svg))
    return tuple(references)

  @property
  def media_references(self) -> tuple[str, ...]:
    """The media filenames the note's fields refer to, in field order.

    These are files the package has to carry with the note. A missing file doesn't
    block an import.

    Each field is normalized before it is searched. Duplicates are kept.
    """
    return tuple(
      ref
      for field in self.fields
      for ref in field_value_media_refs(normalize_field_value(field))
    )

  @property
  def card_ordinals(self) -> tuple[int, ...]:
    """The ordinals of the cards Anki generates for this note, in ascending order.

    On a normal notetype, an ordinal is a template whose front renders for the note:
    the front holds a plain replacement of one of `filled_fields` and every conditional
    around it is satisfied, `{{#K}}` when `K` is filled and `{{^K}}` when it is empty.

    On a cloze notetype, every card uses the first template, and an ordinal comes from
    a cloze number in any of the note's fields: `{{c1::...}}` is card 0, and every
    number from 500 up shares card 499. Anki would give each of those numbers its own
    card 499 and remove all but one at the next "Check Database", so they appear here
    once.

    An empty result is possible. Anki then gives the note card 0 regardless, which is
    left to the caller.
    """
    if self.notetype.is_cloze():
      numbers: set[int] = set()
      for field in self.fields:
        numbers |= field_value_cloze_numbers(normalize_field_value(field))
      return tuple(sorted({min(n - 1, 499) for n in numbers}))
    else:
      filled_fields: frozenset[str] = self.filled_fields
      return tuple(
        template_ord
        for template_ord, template in enumerate(self.notetype.templates)
        if template.is_front_renderable(filled_fields)
      )

  @property
  def tags_to_register(self) -> tuple[Tag, ...]:
    """A table entry per note tag, holding the defaults a new tag gets."""
    return tuple(Tag(tag=tag) for tag in self.tags)

  # ------------------------------------- Repairs --------------------------------------

  def replace_media_references(self, renames: Mapping[str, str]) -> Self:
    """Rewrite the note's media references, renaming every file the mapping names.

    This method helps a caller automate the process of repairing media file names that
    are rejected by Anki. `Package.check_media_paths()` names the files a caller has to
    rename on disk, but renaming them leaves the references to the old names dangling.
    This repairs all those references.

    The renames apply in mapping order and they compose.

    Every field is normalized beforehand.

    Nothing calls this; a caller opts in.

    Args:
      renames: The old name of each renamed file, mapped to its new name. Names are
        spelled the way the media map holds them, so the key `a&b.jpg` renames a field
        referencing `a&amp;b.jpg`.
    """
    new_fields: list[str] = []
    for field in normalize_note_fields(self.fields):
      for old, new in renames.items():
        field = replace_field_value_media_refs(field, old, new)
      new_fields.append(field)
    self.fields = new_fields
    return self

  def repair_modification_time(self) -> Self:
    """Replace a modification time in the future with now, as Anki's database check does.

    `Note.clamp_modification_time()` runs this while the database-check repairs are on.
    """
    now: int = time_s()
    new_modification_time: int = min(self.modification_time_s, now)
    if new_modification_time != self.modification_time_s:
      warnings.warn(
        f"Note: modification time is in the future ({self.modification_time_s}) and "
        f"was corrected to now ({now}) (Anki makes the same repair). This ensures "
        "an import can update the note.",
        Genanki2Warning,
      )
      self.modification_time_s = new_modification_time
    return self

  # ----------------------------- Pre-serialization steps ------------------------------

  @classmethod
  def check_row_consistent_notetype_id(cls, row: NoteRow, notetype: Notetype) -> None:
    """Reject a note row that names a different notetype than the one given.

    A `notes` row refers to its notetype by the `mid` column, and `Note` drops it.
    A mismatch would move the note to another notetype instead of failing.

    Args:
      row: The `notes` row.
      notetype: The notetype the row's `mid` names.

    Raises:
      CollectionReadError: the row's `mid` and the notetype's id differ.
    """
    if row["mid"] != notetype.id:
      raise CollectionReadError(
        f"Row check failed: notetype id {notetype.id} does not match the note's "
        f"notetype id {row['mid']}."
      )

  @classmethod
  def warn_row_wrong_field_len(cls, row: NoteRow, notetype: Notetype) -> None:
    """Warn that a row holds a different number of values than its notetype has fields.

    Anki lays a note out against its notetype by position, so a row whose count
    disagrees is one Anki cannot render and the import fails on it. Because of this,
    `Note.check_fields_len()` refuses to write the note until the two counts agree.

    Args:
      row: The `notes` row.
      notetype: The notetype the row's `mid` references.
    """
    len_note_fields: int = len(row["flds"].split(FIELD_VALUE_SEPARATOR))
    if len_note_fields != max(len(notetype.fields), 1):
      warnings.warn(
        f"Note: row with guid={row['guid']} has {len_note_fields} fields, which does "
        f"not match the number of fields in note type {notetype.name!r} "
        f"({len(notetype.fields)}).",
        Genanki2Warning,
        stacklevel=2,
      )

  def check_fields_len(self) -> Self:
    """Reject a note with the wrong number of field values for its notetype.

    Raises:
      Genanki2Error: number of note values and number of notetype fields don't match.
    """
    if len(self.fields) != max(len(self.notetype.fields), 1):
      raise Genanki2Error(
        f"Check failed: the number of fields in the note ({len(self.fields)}) does not "
        f"match the number of fields in note type ({len(self.notetype.fields)})."
      )
    return self

  @override
  def check(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Reject a note that cannot be written.

    `Note.to_row()` calls this, so a note is checked at the point it is written
    instead of inside Anki, and a half-built one can be passed around freely.

    Args:
      recursive: Whether to check every model reachable from this one too, each once.
        If false, it only runs this model's own checks. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.check_fields_len()
    return super().check(recursive=recursive, context=context)

  def warn_empty_checksum_field(self) -> Self:
    """Warn about an empty first field, which no duplicate check can match.

    An empty first field disables duplicate detection of notes in Anki (which is based
    on the checksum), so duplicates (with different guids) can get in and stay
    unreported.
    """
    if not self.checksum_field.strip():
      warnings.warn(
        f"Note: first field is empty for note id={self.id}, so its checksum is useless "
        "for duplicate detection.",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  def warn_normal_note_cloze_field(self) -> Self:
    """Warn about cloze markup in a note whose notetype is normal.

    Only a cloze notetype turns `{{c1::...}}` into cards and hidden text. On a normal
    notetype the markup is plain text, shown as written wherever a template shows the
    field. Anki's Add dialog refuses such a note, but its import accepts one, so this
    warns rather than raises. As in Anki, `{{c0::...}}` is not a cloze.

    A cloze nested in another counts, as it does when Anki generates cards, so
    `{{c0::{{c1::...}}}}` warns too: its markup shows as written all the same. The Add
    dialog reads only the outermost clozes and accepts that note.
    """
    if self.notetype.is_normal():
      bad_fields: list[str] = [
        name
        for name, field in zip(self.notetype.field_names, self.fields)
        if field_value_cloze_numbers(normalize_field_value(field))
      ]
      if bad_fields:
        warnings.warn(
          f"Note: note id={self.id} has cloze markup in {', '.join(bad_fields)}, but "
          f"note type {self.notetype.name!r} is not a cloze note type, so the markup "
          "shows as plain text on the cards. Use a cloze note type, or remove the "
          "markup.",
          Genanki2Warning,
          stacklevel=2,
        )
    return self

  def warn_cloze_on_wrong_field(self) -> Self:
    """Warn about cloze markup in a field the cloze notetype does not cloze.

    Anki takes a cloze notetype's cloze fields from the `{{cloze:...}}` references on
    the front of its first template, matching names case-insensitively. A cloze number
    in any other field still gives the note a card, but nothing on the front hides it:
    a number found only there makes a card whose front is blank, and the markup shows
    as plain text wherever a template shows the field. Anki's Add dialog refuses such a
    note, but its import accepts one, so this warns rather than raises.

    A cloze nested in another counts, as it does when Anki generates cards, so
    `{{c0::{{c1::...}}}}` in such a field warns too: it still makes a card for `c1`
    that nothing hides. The Add dialog reads only the outermost clozes and accepts that
    note.
    """
    if not self.notetype.is_cloze() or not self.notetype.templates:
      return self

    front: str = self.notetype.templates[0].config.q_format
    cloze_references: set[str] = {
      ref.casefold() for ref in cloze_referenced_field_names(front)
    }
    bad_markup_fields: list[str] = [
      name
      for name, field in zip(self.notetype.field_names, self.fields)
      if field_value_cloze_numbers(normalize_field_value(field))
      and name.casefold() not in cloze_references
    ]
    if bad_markup_fields:
      warnings.warn(
        f"Note: note id={self.id} has cloze markup in {', '.join(bad_markup_fields)}, "
        f"which the front of template {self.notetype.templates[0].name!r} in note type "
        f"{self.notetype.name!r} does not show through a cloze filter. A cloze number "
        "found only there gives the note a card whose front is blank, and the markup "
        "shows as plain text wherever a template shows the field. Move the cloze into "
        "a field the front shows as `{{cloze:Field}}`.",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  def warn_mod_time_in_future(self) -> Self:
    """Warn about a modification time in the future, which blocks the note's updates.

    Anki updates an existing note only when the incoming one is newer.
    `Note.clamp_modification_time()` repairs the time, but when repairs are skipped
    (inside `genanki2.dbcheck_repairs_disabled()`), this warning is emitted.
    """
    if self.modification_time_s > (now := time_s()):
      warnings.warn(
        f"Note: modification time is in the future ({self.modification_time_s}), now "
        f"is {now}, and was not repaired. Anki updates an existing note only when the "
        "incoming one is newer, so the note stops updating until the clock catches up "
        "or Anki's database check clamps the time to now. "
        "`Note.repair_modification_time()` makes the same repair.",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  @override
  def warn(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Warn about a note Anki accepts, but which has unwanted behaviour.

    Warnings can be turned off using `with genanki2.warnings_disabled():`.

    Args:
      recursive: Whether to warn about every model reachable from this one too, each
        once. If false, it only runs this model's own warnings. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.warn_empty_checksum_field()
    self.warn_normal_note_cloze_field()
    self.warn_cloze_on_wrong_field()
    self.warn_mod_time_in_future()
    return super().warn(recursive=recursive, context=context)

  # ---------------------------------- Serialization -----------------------------------

  @override
  @classmethod
  def from_row(cls, row: NoteRow, notetype: Notetype) -> Self:  # pyright: ignore[reportIncompatibleMethodOverride]
    """Build a note from a `notes` row and the notetype the row's `mid` points at.

    The notetype cannot come from the row, which carries only its id. The two ids are
    checked against each other.

    Splitting is exact in both directions: `text.normalize_note_fields()` strips the
    field separator from every value, and `text.normalize_note_tags()` rejects a tag
    holding a tag separator, so neither column can hold a value that survives the join
    and not the split. Both are normalized again on the way in, which changes nothing
    for a row Anki wrote and repairs one written by something else.

    The note takes its read time and content as its last stamp, to detect later
    changes. On change, it updates the modification time to now when it is written or
    frozen, and that write becomes the last stamp.

    A modification time in the future is replaced with now, with a warning, as Anki's
    database check does. A mismatched number of fields issues a warning. `sfld` and
    `csum` are dropped because they are computed in `Note.to_row()`. `flags` and `data`
    are dropped because Anki does not read them.

    Raises:
      CollectionReadError: the row's `mid` and `notetype.id` don't match.
    """
    cls.check_row_consistent_notetype_id(row, notetype)
    cls.warn_row_wrong_field_len(row, notetype)
    fields: list[str] = row["flds"].split(FIELD_VALUE_SEPARATOR)
    tags: list[str] = split_note_tags(row["tags"])
    return super().from_row(
      {**row, "notetype": notetype, "fields": fields, "tags": tags}
    )

  @override
  def _row(self) -> NoteRow:
    """Serialize the note as its `notes` row, running no phase.

    Tags and fields are normalized, and the sort field and checksum are derived from
    the fields.
    """
    tags: list[str] = normalize_note_tags(self.tags)
    tags_str: str = "" if not tags else f" {' '.join(tags)} "
    fields_str: str = FIELD_VALUE_SEPARATOR.join(normalize_note_fields(self.fields))
    checksum: int = int.from_bytes(
      hashlib.sha1(self.checksum_field.encode()).digest()[:4], "big"
    )
    return {
      "id": self.id,
      "guid": self.guid,
      "mid": self.notetype.id,
      "mod": self.modification_time_s,
      "usn": self.update_sequence_n,
      "tags": tags_str,
      "flds": fields_str,
      "sfld": self.sort_field,
      "csum": checksum,
      "flags": 0,
      "data": "",
    }

  @override
  def to_row(self, *, context: SerializationContext | None = None) -> NoteRow:
    """Return the `notes` row for this note.

    Sort field and checksum are derived from the fields. Flags and data are hardcoded
    to default values because Anki does not use them. All are added for completeness.

    If no context is given, one is created with output
    `SerializationContext.Output.ROW` and passed to the phases.
    """
    return super().to_row(context=context)
