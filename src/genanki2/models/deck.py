import enum
import warnings
from typing import Annotated, Any, ClassVar, NamedTuple

import pydantic as pyd
from google.protobuf.message import Message
from typing_extensions import Self, override

import genanki2._proto as _proto  # noqa: PLR0402, import cycle via __init__
from genanki2._typed_schema import DeckRow
from genanki2.errors import Genanki2Error, Genanki2Warning
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
from genanki2.models.base import ProtobufModel, RowModel
from genanki2.models.deck_config import DeckConfig
from genanki2.models.serialization_context import SerializationContext
from genanki2.text import human_deck_name, native_deck_name, normalize_deck_name
from genanki2.time import time_s
from genanki2.utils import float32


class DeckCommon(ProtobufModel):
  """The `decks.common` blob: what all decks carry about their study state.

  It is not configuration, it holds some display states and card counters for the
  current day.

  Attributes:
    study_collapsed: Hide this deck's subdecks in the deck list. Default: `True`.
    browser_collapsed: Hide this deck's subdecks in the browser's sidebar. Default:
      `True`.
    last_day_studied: The day the four counters below were last written, counted from
      the collection's creation. Anki zeroes them the first time the deck is studied on
      a later day, and reads them as zero meanwhile, so a stale day cannot add to
      the current day's numbers. Default: 0.
    new_studied: New cards answered on `last_day_studied`. When extending the limit,
      this number becomes negative. Default: 0.
    review_studied: Review cards answered on `last_day_studied`. Default: 0.
    learning_studied: Written by the v1 scheduler, currently read by nothing. Default: 0.
    milliseconds_studied: Time spent answering on `last_day_studied`. Default: 0.
    other: Schema 11 JSON keys with no protobuf fields, kept as raw bytes. They have to
      decode as a JSON object, otherwise Anki replaces anything that does not parse
      with an empty map. Default: `b""`.
  """

  MessageType: ClassVar[type[Message]] = _proto.DeckCommon

  study_collapsed: bool = True
  browser_collapsed: bool = True
  last_day_studied: Annotated[int, UInt32] = 0
  new_studied: Annotated[int, Int32] = 0
  review_studied: Annotated[int, Int32] = 0
  learning_studied: Annotated[int, Int32] = 0
  milliseconds_studied: Annotated[int, Int32] = 0
  other: bytes = b""


class DeckNormalKind(ProtobufModel):
  """The `Normal` variant of the `decks.kind` blob: a deck that owns its cards.

  Scheduling is not configured here, but in `DeckConfig`. A normal deck holds an id to
  a `DeckConfig` object and some overriding values.

  Attributes:
    config_id: The deck config id which controls scheduling. Default: 1.
    extend_new: In custom study, by how much to increase today's new card limit.
      Remembered from the last time it was used. Default: 0.
    extend_review: In custom study, by how much to increase today's review limit.
      Default: 0.
    description: Text shown under the deck's name on the study screen. Default: `""`.
    markdown_description: Render `description` as markdown. When off, it is treated
      as HTML. Default: `False`.
    review_limit: When set, it overrides the deck config's daily review limit.
      Default: `None`.
    new_limit: When set, it overrides the deck config's daily new cards limit.
      Default: `None`.
    review_limit_today: When set, it overrides both the deck config's and
      `review_limit`'s daily review limit, but only for a specific day. Default: `None`.
    new_limit_today: When set, it overrides both the deck config's and
      `new_limit`'s daily new card limit, but only for a specific day. Default: `None`.
    desired_retention: When set, it overrides the deck config's FSRS desired retention.
      Anki accepts 0.7 to 0.99. Default: `None`.
  """

  # ----------------------------------- Nested types -----------------------------------

  class DayLimit(ProtobufModel):
    """A daily limit that expires on its own.

    Anki applies `limit` only while `today` is the current day and ignores it afterwards
    without clearing it, so the deck options can still show what was last entered.

    A day number belongs to the collection that produced it. Anki rewrites it against
    the target collection on import and clamps a limit that was already expired so it
    stays expired.

    Attributes:
      limit: Cards a day, while the limit is current.
      today: The day it was set on, counted from the collection's creation.
        `Collection.days_elapsed_since_creation` is the value that makes the limit
        arrive as active, while any other value is considered expired.
    """

    MessageType: ClassVar[type[Message]] = _proto.DayLimit

    limit: Annotated[int, UInt32]
    today: Annotated[int, UInt32]

  # ------------------------------------ Attributes ------------------------------------

  MessageType: ClassVar[type[Message]] = _proto.DeckNormal

  config_id: Annotated[int, Int64] = 1
  extend_new: Annotated[int, UInt32] = 0
  extend_review: Annotated[int, UInt32] = 0
  description: str = ""
  markdown_description: bool = False
  review_limit: Annotated[int | None, UInt32] = None
  new_limit: Annotated[int | None, UInt32] = None
  review_limit_today: DayLimit | None = None
  new_limit_today: DayLimit | None = None
  desired_retention: float | None = None

  # ----------------------------- Pre-serialization steps ------------------------------

  def warn_bad_desired_retention(self) -> Self:
    """Warn about an out of bounds `desired_retention` that Anki does not repair.

    `desired_retention` should be a probability between 0.7 and 0.99. Anki does
    not repair it in the protobuf message and uses it as given.
    """
    if self.desired_retention is not None and not (
      float32(0.7) <= float32(self.desired_retention) <= float32(0.99)
    ):
      msg: str = (
        f"DeckNormalKind: desired retention {self.desired_retention} is outside "
        "[0.7, 0.99]."
      )
      warnings.warn(msg, Genanki2Warning, stacklevel=2)
    return self

  @override
  def warn(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Warn about a bad value Anki accepts and then schedules with.

    Warnings can be turned off using `with genanki2.warnings_disabled():`.

    Args:
      recursive: Whether to warn about every model reachable from this one too, each
        once. If false, it only runs this model's own warnings. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.warn_bad_desired_retention()
    return super().warn(recursive=recursive, context=context)


class DeckFilteredKind(ProtobufModel):
  """The `Filtered` variant of the `decks.kind` blob: a temporary deck built by search.

  A filtered deck doesn't own cards. Anki gathers cards from the collection using the
  search terms below and moves them in. Each card remembers the deck it came from and
  returns to it when the filtered deck is emptied or deleted.

  It follows no deck config because its scheduling config is contained here.

  A filtered deck is exported when its scheduling is exported alongside every card it
  gathered.

  Attributes:
    reschedule: When enabled, it makes answers count and will schedule a card as its
      home deck would. When disabled, the deck is a preview: cards keep the due dates
      they arrived with, and the three `preview_*_secs` delays schedule them.
      Default: `True`.
    search_terms: What to gather. Anki reads the first two and ignores any others.
    delays: Learning steps for the v1 scheduler, currently read by nothing.
    preview_delay: The preview delay used by the v2 and early v3 schedulers, in minutes,
      superseded by the three `preview_*_secs` delays. Default: 0.
    preview_hard_secs: When rescheduling is off, it marks seconds until a card with a
      `Hard` answer is shown again. Setting it to `0` sends the card home instead of
      showing it again. Default: 600.
    preview_good_secs: When rescheduling is off, it marks seconds until a card with a
      `Good` answer is shown again. Setting it to `0` sends the card home instead of
      showing it again. Default: 0.
    preview_again_secs: When rescheduling is off, it marks seconds until a card with a
      `Again` answer is shown again. Setting it to `0` sends the card home instead of
      showing it again. Default: 60.
  """

  # ----------------------------------- Nested types -----------------------------------

  class SearchTerm(ProtobufModel):
    """One search a filtered deck gathers cards with.

    Attributes:
      search: The search, in the browser's syntax. Anki narrows it to cards that are
        not suspended, not buried and not already in a filtered deck, so the term can
        match them without gathering them. Not validated.
      limit: The maximum number of cards this term gathers.
      order: The order in which the gathered cards are studied.
    """

    class Order(enum.IntEnum):
      """The order in which the gathered cards are studied."""

      OLDEST_REVIEWED_FIRST = 0
      RANDOM = 1
      INTERVALS_ASCENDING = 2
      INTERVALS_DESCENDING = 3
      LAPSES = 4
      ADDED = 5
      DUE = 6
      REVERSE_ADDED = 7
      RETRIEVABILITY_ASCENDING = 8
      RETRIEVABILITY_DESCENDING = 9
      RELATIVE_OVERDUENESS = 10

    MessageType: ClassVar[type[Message]] = _proto.SearchTerm

    search: str
    limit: Annotated[int, UInt32]
    order: Annotated[Order | int, LtRUnion, Int32]

    @classmethod
    def defaults(cls) -> list[Self]:
      """Return Anki's two stock terms: 100 cards in random order, then 20 by due date."""
      return [
        cls(search="", limit=100, order=DeckFilteredKind.SearchTerm.Order.RANDOM),
        cls(search="", limit=20, order=DeckFilteredKind.SearchTerm.Order.DUE),
      ]

    # ---------------------------- Pre-serialization steps -----------------------------

    @override
    def warn(
      self, *, recursive: bool = True, context: SerializationContext | None = None
    ) -> Self:
      """Warn about a deck search term with unknown enum values.

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

  MessageType: ClassVar[type[Message]] = _proto.DeckFiltered

  reschedule: bool = True
  search_terms: Annotated[list[SearchTerm], NotEmpty] = pyd.Field(
    default_factory=SearchTerm.defaults
  )
  delays: list[float] = pyd.Field(default_factory=list)
  preview_delay: Annotated[int, UInt32] = 0
  preview_hard_secs: Annotated[int, UInt32] = 600
  preview_good_secs: Annotated[int, UInt32] = 0
  preview_again_secs: Annotated[int, UInt32] = 60

  # ------------------------------- Serialization checks -------------------------------

  def warn_too_many_search_terms(self) -> Self:
    """Warn about search terms Anki never reads.

    Only the first two build the deck, and the UI drops the rest on save.
    """
    if len(self.search_terms) > 2:
      warnings.warn(
        f"DeckFilteredKind: `search_terms` list has {len(self.search_terms)} "
        "elements. All elements after the second are discarded by Anki.",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  @override
  def warn(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Warn about search terms Anki accepts and does not use.

    Warnings can be turned off using `with genanki2.warnings_disabled():`.

    Args:
      recursive: Whether to warn about every model reachable from this one too, each
        once. If false, it only runs this model's own warnings. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.warn_too_many_search_terms()
    return super().warn(recursive=recursive, context=context)


def decode_kind(value: Any) -> Any:
  """Return the variant a `decks.kind` blob sets.

  The column holds a C-style union between the two variants.

  Raises:
    ValueError: the blob sets neither variant.
  """
  if not isinstance(value, bytes):
    return value
  container: Message = _proto.DeckKindContainer.FromString(value)
  variant: str | None = container.WhichOneof("kind")
  if variant is None:
    raise ValueError(
      'A deck kind sets neither "normal" nor "filtered": the blob is empty '
      "or it sets a variant added by a newer Anki."
    )
  cls = DeckNormalKind if variant == "normal" else DeckFilteredKind
  return cls.from_bytes(getattr(container, variant).SerializeToString())


def encode_kind(kind: DeckNormalKind | DeckFilteredKind) -> bytes:
  """Return `kind` as a `decks.kind` blob, wrapped in its container.

  A new container is constructed and returned. If a newer Anki adds a new field to
  the container itself, it is not carried through.
  """
  container: Message = _proto.DeckKindContainer()
  variant: str = "normal" if isinstance(kind, DeckNormalKind) else "filtered"
  getattr(container, variant).ParseFromString(kind._bytes())
  return container.SerializeToString()


class Deck(RowModel[DeckRow]):
  """One deck: the named container a card is studied from.

  genanki2 builds normal decks by default. To make a filtered deck, set `kind` to a
  `DeckFilteredKind` instance or use `filtered_deck()`.

  Decks can be organized hierarchically using `::` in their names: for `Deck` and
  `Deck::Subdeck`, the latter is a subdeck of the former.

  Important: in Anki, decks are matched by name, not by id. A deck's content is merged
  into the existing one (of the same type). Anki creates all necessary parent decks.

  Attributes:
    id: Primary key. Anki does not match by it. Defaults to a new monotonic id.
    name: Complete name in human form, parts separated by `::`. Repaired by
      `text.normalize_deck_name()` before reaching Anki, except that a part the repair
      empties is rejected instead of substituted with `blank`. A collision created by
      the repair surfaces as a duplicate-name error from
      `Collection.check_unique_names()`. Left uncaught, nothing gets renamed and Anki
      merges decks it matches by name.
    common: The `decks.common` blob: the deck's study state, not its configuration.
      Defaults to what Anki writes for a deck it creates.
    kind: The `decks.kind` blob, holding the normal or the filtered variant. Whichever
      is present makes the deck normal or filtered. Defaults to a normal deck.
    config: The scheduling preset a normal deck follows. When set, this is the
      authoritative object and its id is written into the `kind` blob. If `None`, the
      `DeckNormalKind.config_id` is the fallback authority. If `kind` holds a
      `DeckFilteredKind` object, the config is ignored. Default: `None`.
    modification_time_s: Seconds since the epoch. Not preserved across an import: Anki
      stamps an incoming deck with the target collection's own value, where a note
      keeps the mtime it arrived with. A sync does compare it, so a time in the future
      overrides what the server sends until the clock catches up. Defaults to the
      current time.
    update_sequence_n: The sync marker. Defaults to `-1`, which means the deck carries
      changes the server has not seen; a non-negative value is the number of the sync
      that last carried it. The server sets it, so a client only writes `-1`.
      Stamped along with `modification_time_s` and therefore not preserved across an
      import.
  """

  # ----------------------------------- Nested types -----------------------------------

  class _ReadData(NamedTuple):
    """What `Deck.from_row()` read for a normal deck."""

    name: str
    # Normal kind data
    config_id: int
    description: str
    review_limit: int | None = None
    new_limit: int | None = None
    review_limit_today: DeckNormalKind.DayLimit | None = None
    new_limit_today: DeckNormalKind.DayLimit | None = None
    desired_retention: float | None = None

  # ------------------------------------ Attributes ------------------------------------

  id: Annotated[int, Id] = MonotonicId
  name: Annotated[str, NotEmpty, pyd.AfterValidator(normalize_deck_name)]
  common: Annotated[
    DeckCommon, BytesDecoder(DeckCommon), pyd.PlainSerializer(DeckCommon._bytes)
  ] = pyd.Field(default_factory=DeckCommon)
  kind: Annotated[
    DeckNormalKind | DeckFilteredKind,
    pyd.BeforeValidator(decode_kind),
    pyd.PlainSerializer(encode_kind),
  ] = pyd.Field(default_factory=DeckNormalKind)
  config: Annotated[DeckConfig | None, pyd.Field(exclude=True)] = None

  # Sync
  modification_time_s: Annotated[int, UInt63, MTimeSecsAlias] = TimeS
  update_sequence_n: Annotated[int, UsnField] = -1

  # Read data
  _read_data: _ReadData | None = None

  # ------------------------------------ Validators ------------------------------------

  @pyd.model_validator(mode="after")
  def sync_normal_kind_config_id(self) -> Self:
    """For a normal deck, copy the config's id into `DeckNormalKind.config_id`."""
    if (
      isinstance(self.kind, DeckNormalKind)
      and self.config is not None
      and self.kind.config_id != self.config.id
    ):
      self.kind.config_id = self.config.id
    return self

  @pyd.model_validator(mode="after")
  def check_default_deck_is_normal(self) -> Self:
    """Reject a filtered deck with id=1, which Anki reserves for the default deck.

    Anki creates deck 1 as a normal deck along with the collection and refuses to
    delete it, resetting its name instead. Nothing can turn an existing deck into a
    filtered one.

    Raises:
      ValueError: the deck is filtered and its id is 1.
    """
    if self.id == 1 and self.is_filtered():
      raise ValueError(
        f"Check failed: filtered deck {self.name!r} uses id 1, which Anki reserves "
        "for the default normal deck. The default deck cannot be deleted or turned "
        f"into a filtered one, so {self.name!r} needs another id."
      )
    return self

  @pyd.model_validator(mode="after")
  def check_filtered_deck_has_config(self) -> Self:
    """Reject a preset on a filtered deck, which doesn't use or hold one.

    The association lives in `DeckNormalKind.config_id`. The `config` member is
    completely ignored on a filtered deck.

    Raises:
      ValueError: the deck is filtered and carries a preset.
    """
    if self.is_filtered() and self.config is not None:
      raise ValueError(
        f"Check failed: filtered deck {self.name!r} has a deck config set on it. "
        "To convert a normal deck to a filtered one, first set `config` to `None`, "
        "then set `kind`."
      )
    return self

  # ---------------------------------- Derived values ----------------------------------

  @property
  def config_id(self) -> int | None:
    """The id of the preset that schedules the deck or `None` for a filtered deck.

    It is `config`'s id when the deck has one and `DeckNormalKind.config_id` otherwise.
    `config` comes first: a change to the preset's own id reaches the kind blob at the
    next `Deck.sync_normal_kind_config_id()`.
    """
    if self.config is not None:
      return self.config.id
    if isinstance(self.kind, DeckNormalKind):
      return self.kind.config_id
    return None

  def is_normal(self) -> bool:
    """Return whether the deck owns the cards instead of gathering them by search."""
    return isinstance(self.kind, DeckNormalKind)

  def is_filtered(self) -> bool:
    """Return whether Anki gathered the cards by search, not by the deck owning them."""
    return isinstance(self.kind, DeckFilteredKind)

  # ----------------------------- Pre-serialization steps ------------------------------

  def check_normal_kind_config_id(self) -> Self:
    """Reject a normal deck whose `DeckNormalKind.config_id` doesn't match its preset.

    `Deck.sync_normal_kind_config_id()` fixes it.

    Raises:
      Genanki2Error: `DeckNormalKind.config_id` and `DeckConfig.id` are not equal.
    """
    if (
      isinstance(self.kind, DeckNormalKind)
      and self.config is not None
      and self.kind.config_id != self.config.id
    ):
      raise Genanki2Error(
        f'Check failed: deck "{self.name}" carries preset {self.config.id} '
        f'("{self.config.name}"), but its kind blob points at preset '
        f"{self.kind.config_id}. They need to agree, otherwise Anki will read the "
        "kind blob and use the preset indicated by it. Sync with "
        "`genanki2.Deck.sync_normal_kind_config_id()` before calling "
        "`genanki2.Deck.to_row()`."
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
        f"(now is {now}). Anki reads it on a sync: an unsynced deck keeps its local "
        "copy when that copy is newer than the server's, so this deck would override "
        "the server's until that time arrives."
      )
    return self

  @override
  def check(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Reject a deck that cannot be written.

    `Deck.to_row()` calls this, so a deck is validated at the point it is written
    instead of inside Anki, and a half-built one can be passed around freely.

    Args:
      recursive: Whether to check every model reachable from this one too, each once.
        If false, it only runs this model's own checks. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.check_normal_kind_config_id()
    self.check_mod_time_not_in_future()
    return super().check(recursive=recursive, context=context)

  def warn_unimported_changes(
    self, context: SerializationContext | None = None
  ) -> Self:
    """Warn about a change to a read deck that does not reach a user who has it.

    Anki matches an incoming deck by name case-insensitively and updates a deck with
    incoming values only partly: the description when it is not empty, the preset when
    it is not 1, and each limit that is set. It never copies the desired retention.
    So emptying a description, putting the deck back on preset 1, clearing a limit or
    changing the desired retention does not reach an importing user who already has
    the deck.

    A deck renamed since the read is added as a new deck, and a filtered deck has its
    settings replaced whole, so neither is compared. A colpkg restore replaces the
    whole collection, so the warning is skipped.

    Args:
      context: Extra information from the serialization process. Default: `None`.
    """
    read_data: Deck._ReadData | None = self._read_data
    if (
      read_data is None
      or not isinstance(self.kind, DeckNormalKind)
      or self.name.casefold() != read_data.name.casefold()
    ):
      return self

    match context:
      case SerializationContext(output=SerializationContext.Output.COLPKG):
        return self
      case SerializationContext(output=SerializationContext.Output.APKG):
        message: str = "An apkg import"
      case _:
        message = "If imported from an apkg, the import"

    kind: DeckNormalKind = self.kind
    changes: list[str] = []
    if read_data.description and not kind.description:
      changes.append("  description: emptied")
    if read_data.config_id != 1 and kind.config_id == 1:
      changes.append(f"  config_id: set back to 1 from {read_data.config_id}")
    for limit in ("review_limit", "new_limit", "review_limit_today", "new_limit_today"):
      if getattr(read_data, limit) is not None and getattr(kind, limit) is None:
        changes.append(f"  {limit}: cleared")
    # The read value comes back from a 32-bit float, so compare what will be written
    retention: float | None = kind.desired_retention
    if retention is not None:
      retention = float32(retention)
    if read_data.desired_retention != retention:
      before, after = (
        "unset" if value is None else f"{value:g}"
        for value in (read_data.desired_retention, retention)
      )
      changes.append(f"  desired_retention: changed from {before} to {after}")

    if changes:
      listing: str = "\n".join(changes)
      warnings.warn(
        f'Deck: "{self.name}" changed since it was read. {message} updates a deck that '
        "the user already has under this name with a description only when it is not "
        "empty, a preset only when it is not 1 and a limit only when it is set, and "
        "never with a desired retention, so these changes do not reach that user:\n"
        f"{listing}",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  @override
  def warn(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Warn about a change to a read deck that does not reach a user who has it.

    Warnings can be turned off using `with genanki2.warnings_disabled():`.

    Args:
      recursive: Whether to warn about every model reachable from this one too, each
        once. If false, it only runs this model's own warnings. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.warn_unimported_changes(context)
    return super().warn(recursive=recursive, context=context)

  # ---------------------------------- Serialization -----------------------------------

  @override
  @classmethod
  def from_row(cls, row: DeckRow, config: DeckConfig | None = None) -> Self:
    """Build a deck from a `decks` table row, holding `name` in human readable form."""
    deck: Self = super().from_row(
      {**row, "name": human_deck_name(row["name"]), "config": config}
    )
    if isinstance(deck.kind, DeckNormalKind):
      kind: DeckNormalKind = deck.kind
      today_limits: dict[str, DeckNormalKind.DayLimit] = {}
      if kind.review_limit_today is not None:
        today_limits["review_limit_today"] = kind.review_limit_today.model_copy()
      if kind.new_limit_today is not None:
        today_limits["new_limit_today"] = kind.new_limit_today.model_copy()
      deck._read_data = cls._ReadData(
        name=deck.name,
        config_id=kind.config_id,
        description=kind.description,
        review_limit=kind.review_limit,
        new_limit=kind.new_limit,
        desired_retention=kind.desired_retention,
        **today_limits,
      )
    return deck

  @override
  def _row(self) -> DeckRow:
    """Serialize the deck as its `decks` row, name in native form, running no phase."""
    return {**super()._row(), "name": native_deck_name(self.name)}

  @override
  def to_row(self, *, context: SerializationContext | None = None) -> DeckRow:
    """Return a `decks` row for this deck, with the name back in its native form.

    If no context is given, one is created with output
    `SerializationContext.Output.ROW` and passed to the phases.
    """
    return super().to_row(context=context)
