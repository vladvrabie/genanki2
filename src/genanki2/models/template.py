import warnings
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from typing import Annotated, ClassVar

import pydantic as pyd
from google.protobuf.message import Message
from typing_extensions import Self, override

import genanki2._proto as _proto  # noqa: PLR0402, import cycle via __init__
from genanki2._typed_schema import TemplateRow
from genanki2.errors import Genanki2Error, Genanki2Warning
from genanki2.models.annotations import (
  BytesDecoder,
  Int64,
  MTimeSecsAlias,
  NotEmpty,
  TimeS,
  UInt32,
  UInt63,
  UsnField,
)
from genanki2.models.base import ProtobufModel, RowModel
from genanki2.models.serialization_context import SerializationContext
from genanki2.text import (
  SectionReference,
  is_template_reference_reachable,
  normalize_template_name,
  replace_template_underscored_references,
  template_referenced_field_names,
  template_references,
  template_underscored_references,
)


class TemplateConfig(ProtobufModel):
  """The `templates.config` blob: everything a card type carries beyond its name.

  The two main formats have no default because a template without them renders nothing.
  Everything else is at the protobuf zero, except for `id`.

  Attributes:
    q_format: The front format, rendered into a card's question side.
    a_format: The back format, rendered into a card's answer side.
    q_format_browser: Front format used in the card browser, falling back to `q_format`
      when empty. Default: `""`.
    a_format_browser: Back format used in the card browser, falling back to `a_format`
      when empty. Default: `""`.
    target_deck_id: A per-template deck override, used only when positive, so <=0 means
      unset. It has no effect on an import. It only applies to the cards Anki generates
      later, when a note is added or edited or the notetype gains a template. There it
      has precedence over the deck the note's existing cards are in, and the deck being
      added to is used only when the template has no override and the note has no cards
      to take one from, so this is the one value here that can put a card somewhere
      other than its siblings. Cloze notetypes ignore it. Default: 0.
    browser_font_name: The card browser's font for this template's rows. Default: `""`.
    browser_font_size: The card browser's font size, in pixels. `0` lets the browser
      use font size 12. Default: 0.
    id: What makes a renamed template recognisable: two templates match on id and fall
      back to comparing names otherwise. Anki mints one in the 64-bit range. It never
      back-fills a missing one. Default is `None` (meaning unset in protobuf) because
      an id that changes from build to build is worse: the two ids differ, name
      matching is skipped and the template forks instead of updating. Use
      `Notetype.fill_missing_ids()` to set a stable id across builds.

      A rename has 2 consequences. When merging is off and without template ids, a
      renamed template makes two schemas unequal and the incoming notetype is added
      under a fresh id, duplicating it. With merging enabled, both notetypes end up
      holding the old template and the renamed one, so Anki generates a card for the
      duplicate on every note. Cards keep their own templates, since their ordinals are
      remapped along with them. Cloze notetypes skip the second case: merging leaves
      their templates alone.
    other: Schema 11 JSON keys with no protobuf fields, kept as raw bytes. They have to
      decode as a JSON object, otherwise Anki replaces anything that does not parse
      with an empty map. Default: `b""`.
  """

  MessageType: ClassVar[type[Message]] = _proto.TemplateConfig

  q_format: str
  a_format: str
  q_format_browser: str = ""
  a_format_browser: str = ""
  target_deck_id: Annotated[int, Int64] = 0
  browser_font_name: str = ""
  browser_font_size: Annotated[int, UInt32] = 0
  id: Annotated[int | None, Int64] = None
  other: bytes = b""


class Template(RowModel[TemplateRow]):
  """One card type of a notetype: the formats that render a note into a card.

  A template carries no ordinal and no notetype id. Both are given by the notetype: the
  position in `Notetype.templates` is the ordinal, and `Notetype.to_rows()` supplies the
  id.

  Attributes:
    name: The name Anki lists the card type under, as `Card 1`, and the name a `card:`
      search matches. Repaired by `text.normalize_template_name()` before reaching
      Anki, so a collision the repair creates surfaces as a duplicate-name error from
      `Notetype.to_rows()`. Anki silently repairs by appending `+` until the name is
      free. Nothing dangles, since a card binds to its template by ordinal.
    config: The `templates.config` protobuf. Holds the question and answer formats,
      their browser variants, the browser font, the target deck id, and the id Anki
      follows a rename by.
    modification_time_s: Seconds since the epoch. Anki writes it through as given,
      never reads it back and sets it to 0 on save. Defaults to the current time.
    update_sequence_n: The sync marker. Anki writes it through as given, never reads
      it back and sets it to 0 on save. Default: -1.
  """

  name: Annotated[str, NotEmpty, pyd.AfterValidator(normalize_template_name)]
  config: Annotated[
    TemplateConfig,
    BytesDecoder(TemplateConfig),
    pyd.PlainSerializer(TemplateConfig._bytes),
  ]

  # Sync
  # no not_in_future: Anki never reads it back
  modification_time_s: Annotated[int, UInt63, MTimeSecsAlias] = TimeS
  update_sequence_n: Annotated[int, UsnField] = -1

  _FINGERPRINT_EXCLUDED: ClassVar[frozenset[str]] = frozenset(["mtime_secs", "usn"])

  # ---------------------------------- Derived values ----------------------------------

  def is_front_renderable(self, filled_fields: AbstractSet[str]) -> bool:
    """Tell whether the front renders for a note whose filled names are `filled_fields`.

    The front renders when it holds a plain replacement of a filled name and every
    conditional around it is satisfied: `{{#K}}` when `K` is filled, `{{^K}}` when it
    is empty. A conditional's own name never renders anything, and text alone does not
    either.

    `Note.filled_fields` gives the names for a note, special names included.
    """
    return any(
      not ref.is_conditional
      and is_template_reference_reachable(
        (*ref.sections, SectionReference(ref.key, True)), filled_fields, filled_fields
      )
      for ref in template_references(self.config.q_format)
    )

  @property
  def underscored_references(self) -> tuple[str, ...]:
    """The underscored media filenames that the template's two formats contain.

    Question format first, then answer format. Duplicates are kept.
    """
    return template_underscored_references(
      self.config.q_format
    ) + template_underscored_references(self.config.a_format)

  # ------------------------------------- Repairs --------------------------------------

  def repair_target_deck_id(self) -> Self:
    """Clear a deck override an import turns wrong, so the card joins its siblings.

    A target deck id of 1 is not modified, since telling cards to go to the default
    deck is the default behaviour and every collection has a default deck with id 1.

    Nothing calls this; a caller opts in. Without it, cards might go to the default
    deck instead of their original deck.
    """
    if self.config.target_deck_id > 1:
      self.config.target_deck_id = 0
    return self

  def replace_underscored_references(self, renames: Mapping[str, str]) -> Self:
    """Rewrite the references in template sides, renaming every file the mapping names.

    This method helps a caller automate the process of repairing media file names that
    are rejected by Anki. `Package.check_media_paths()` names the files a caller has to
    rename on disk, but renaming them leaves the references to the old names dangling.
    This repairs all those references.

    A name is matched literally, without decoding HTML entities: a reference of
    `_a&amp;b.png` is renamed under that spelling.

    Anki's detection rule applies: only names beginning with "_" are repaired,
    in the same way `Template.underscored_references` finds them.

    The renames apply in mapping order and they compose.

    Nothing calls this; a caller opts in.

    Args:
      renames: The old name of each renamed file, mapped to its new name.
    """
    q_format, a_format = self.config.q_format, self.config.a_format
    for old, new in renames.items():
      q_format = replace_template_underscored_references(q_format, old, new)
      a_format = replace_template_underscored_references(a_format, old, new)
    if self.config.q_format != q_format:
      self.config.q_format = q_format
    if self.config.a_format != a_format:
      self.config.a_format = a_format
    return self

  # ----------------------------- Pre-serialization steps ------------------------------

  def check_templates_parse(self) -> Self:
    """Reject a template side that does not parse.

    Template syntax errors come from unbalanced conditionals: opened and never
    closed, closed without being opened, or closed under a name other than the one it
    opened. Unclosed braces are not an error.

    For consistency, the browser sides are also checked when non-empty.

    Raises:
      Genanki2Error: the front or back of a template contains a syntax error.
    """
    q_format, a_format = self.config.q_format, self.config.a_format
    formats_to_check = [("front", q_format), ("back", a_format)]
    if self.config.q_format_browser:
      formats_to_check.append(("browser front", self.config.q_format_browser))
    if self.config.a_format_browser:
      formats_to_check.append(("browser back", self.config.a_format_browser))
    for side, format in formats_to_check:
      try:
        _ = template_referenced_field_names(format)
      except Genanki2Error as error:
        raise Genanki2Error(
          f'Check failed: the {side} side of template "{self.name}" contains a '
          f"syntax error. Error: {error}"
        ) from error
    return self

  def check_min_front_references(self) -> Self:
    """Reject a front that references nothing at all.

    Such a front renders the same static text for every note of the notetype, so Anki
    refuses it. A single reference satisfies this, including a conditional one.

    Raises:
      Genanki2Error: a template's front references nothing.
    """
    if not template_referenced_field_names(self.config.q_format):
      raise Genanki2Error(
        f'Check failed: the front side of template "{self.name}" contains no field '
        "references."
      )
    return self

  @override
  def check(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Reject a template that cannot be written.

    `Template.to_row()` calls this, so a template is validated at the point it is
    written instead of inside Anki, and a half-built one can be passed around freely.

    Args:
      recursive: Whether to check every model reachable from this one too, each once.
        If false, it only runs this model's own checks. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.check_templates_parse()
    # Some checks assume templates parse (*)
    self.check_min_front_references()  # *
    return super().check(recursive=recursive, context=context)

  def warn_misleading_target_deck_id(
    self, context: SerializationContext | None = None
  ) -> Self:
    """Warn about a template deck override that an apkg import turns wrong.

    An apkg import gives every incoming deck the id of the user's deck with the same
    name, or a new one, and doesn't update `TemplateConfig.target_deck_id`, which means
    that after the import the override references nothing or something unrelated.

    A target deck id of 1 is the exception, since telling cards to go to the default
    deck is the default behaviour and every collection has a default deck with id 1.

    A colpkg restore keeps every deck id, so a colpkg write gets no warning. Without a
    context, the warning says it applies to an apkg import.

    Args:
      context: Extra information from the serialization process. Default: `None`.
    """
    match context:
      case SerializationContext(output=SerializationContext.Output.COLPKG):
        return self
      case SerializationContext(output=SerializationContext.Output.APKG):
        message: str = (
          "An apkg import renumbers every deck and leaves the override as it is, so it "
          "references a deck the user does not have, or an unrelated one."
        )
      case _:
        message = (
          "If imported from an apkg, it references a deck the user does not have, or "
          "an unrelated one: the import renumbers every deck and leaves the override "
          "as it is."
        )

    if (target_id := self.config.target_deck_id) > 1:
      warnings.warn(
        f'Template: "{self.name}" sets a deck override (target_deck_id={target_id}). '
        f"{message} Call `Template.repair_target_deck_id()` to clear it.",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  @override
  def warn(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Warn about a template Anki accepts, but which has unwanted behaviour.

    Warnings can be turned off using `with genanki2.warnings_disabled():`.

    Args:
      recursive: Whether to warn about every model reachable from this one too, each
        once. If false, it only runs this model's own warnings. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.warn_misleading_target_deck_id(context)
    return super().warn(recursive=recursive, context=context)

  # ---------------------------------- Serialization -----------------------------------

  @override
  @classmethod
  def from_row(cls, row: TemplateRow) -> Self:
    """Build a template from a `templates` table row.

    The `ntid` and `ord` columns are ignored: use `Notetype.from_rows()` to read and
    validate them.

    Template name is normalized and repaired by the model.
    """
    return super().from_row(row)

  @override
  def _row(self, notetype_id: int, ordinal: int) -> TemplateRow:  # pyright: ignore[reportIncompatibleMethodOverride]
    """Serialize the template as a `templates` row, running no phase.

    Both arguments are the caller's responsibility, and neither is checked: the
    notetype is not looked up, and the ordinal is not compared with the template's
    position in it. The `templates` table declares no foreign key, so a `notetype_id`
    naming no notetype still writes a row.
    """
    return {**super()._row(), "ntid": notetype_id, "ord": ordinal}

  @override
  def to_row(  # pyright: ignore[reportIncompatibleMethodOverride]
    self, notetype_id: int, ordinal: int, *, context: SerializationContext | None = None
  ) -> TemplateRow:
    """Return a `templates` row for this template, owned by `notetype_id` at `ordinal`.

    Both arguments are the caller's responsibility, and neither is checked: the
    notetype is not looked up, and the ordinal is not compared with the template's
    position in it. The `templates` table declares no foreign key, so a `notetype_id`
    naming no notetype still writes a row.

    Use `Notetype.to_rows()` to generate correct `templates` rows.

    If no context is given, one is created with output
    `SerializationContext.Output.ROW` and passed to the phases.
    """
    if context is None:
      context = SerializationContext(output=SerializationContext.Output.ROW)
    return self._run_phases(context=context)._row(notetype_id, ordinal)
