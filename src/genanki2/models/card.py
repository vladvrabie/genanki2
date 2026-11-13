import enum
import json
import warnings
from typing import Annotated, Any, ClassVar

import pydantic as pyd
from typing_extensions import Self, override

from genanki2._typed_schema import CardRow
from genanki2.errors import CollectionReadError, Genanki2Error, Genanki2Warning
from genanki2.models.annotations import (
  Bounded,
  Id,
  Int64,
  JSONDecoder,
  ModAlias,
  MonotonicId,
  Rounded,
  TimeS,
  UInt8,
  UInt16,
  UInt32,
  UInt63,
  UsnField,
)
from genanki2.models.base import CheckedModel, FingerprintedRowModel
from genanki2.models.deck import Deck
from genanki2.models.note import Note
from genanki2.models.notetype import Notetype
from genanki2.models.serialization_context import SerializationContext
from genanki2.settings import _dbcheck_repairs_enabled, dbcheck_repairs_disabled
from genanki2.time import time_s


class CardType(enum.IntEnum):
  """What the scheduler is doing with the card.

  - `LEARNING` is a new card working through its steps
  - `RELEARNING` is for a review card that lapsed and is working through the steps
      again
  """

  NEW = 0
  LEARNING = 1
  REVIEW = 2
  RELEARNING = 3


class CardQueue(enum.IntEnum):
  """Where the card sits in the study queues, and what its `due` counts.

  - `NEW` counts a position
  - `REVIEW` and `DAY_LEARNING` count a day number
  - `LEARNING` and `PREVIEW_REPEAT` count a unix timestamp
  - `SUSPENDED`, `SCHEDULER_BURIED` and `USER_BURIED` are for cards out of rotation
  """

  NEW = 0
  LEARNING = 1
  REVIEW = 2
  DAY_LEARNING = 3
  PREVIEW_REPEAT = 4
  SUSPENDED = -1
  SCHEDULER_BURIED = -2
  USER_BURIED = -3


def _dump_custom_data(data: dict[str, Any]) -> str:
  """Encode custom scheduling data in the same way as Anki: compact, non-ASCII raw."""
  return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


class CardData(CheckedModel):
  """The card's `data` column: the scheduling values Anki keeps as JSON.

  Every key is left out when it is unset, so an ordinary card carries `{}`. Anki
  replaces a value it cannot read with the default instead of refusing the card, and
  `CardData.from_json()` does the same, so a column written elsewhere cannot be
  assumed sound.

  Attributes:
    original_queue_position: Where a new card sat before it was repositioned, kept so
      the position can be restored. Default: `None`.
    fsrs_stability: Stability, half of the FSRS memory state. Anki reads a memory
      state only when both stability and difficulty are present. Default: `None`.
    fsrs_dificulty: Difficulty, half of the FSRS memory state. Anki reads a memory
      state only when both stability and difficulty are present. Default: `None`.
    fsrs_desired_retention: Overrides the preset's desired retention for this card.
      Default: `None`.
    fsrs_decay: The decay of the card's forgetting curve. Default: `None`.
    last_review_time: Seconds since the epoch, the point FSRS measures elapsed days
      from. Default: `None`.
    custom_data: State a v3 custom scheduler keeps on the card. Written as a JSON
      object inside the column. Anki takes keys of at most 8 bytes. Maximum encoded
      size is 100 bytes. Default: `{}`.
  """

  # Pydantic
  model_config: ClassVar[pyd.ConfigDict] = pyd.ConfigDict(
    extra="ignore", serialize_by_alias=True
  )

  original_queue_position: Annotated[int | None, UInt32, pyd.Field(alias="pos")] = None
  fsrs_stability: Annotated[float | None, Rounded(4), pyd.Field(alias="s")] = None
  fsrs_dificulty: Annotated[float | None, Rounded(3), pyd.Field(alias="d")] = None
  fsrs_desired_retention: Annotated[float | None, Rounded(2), pyd.Field(alias="dr")] = (
    None
  )
  fsrs_decay: Annotated[float | None, Rounded(3), pyd.Field(alias="decay")] = None
  last_review_time: Annotated[int | None, Int64, pyd.Field(alias="lrt")] = None
  custom_data: Annotated[
    dict[str, Any],
    pyd.Field(exclude_if=lambda data: not data, alias="cd"),
    pyd.PlainSerializer(_dump_custom_data, return_type=str),
  ] = pyd.Field(default_factory=dict)

  # ----------------------------- Pre-serialization steps ------------------------------

  def check_custom_data(self) -> Self:
    """Reject custom data Anki refuses to store.

    Anki checks this when the card is updated or answered, not when it is imported, so
    a package can carry a value that fails at the user's next review.

    Raises:
      Genanki2Error: a key or value breaks one of the limits Anki enforces on custom
        data.
    """
    if not self.custom_data:
      return self
    bad_keys: list[str] = []
    for key in self.custom_data:
      if not isinstance(key, str) or len(key.encode()) > 8:  # pyright: ignore[reportUnnecessaryIsInstance]
        bad_keys.append(key)
    if bad_keys:
      raise Genanki2Error(
        "Check failed: custom data holds keys Anki refuses. A key has to be a string "
        f"of at most 8 bytes. Bad keys: {bad_keys}"
      )
    try:
      if (encoded_len := len(_dump_custom_data(self.custom_data).encode())) > 100:
        raise Genanki2Error(
          f"Check failed: custom data encodes to {encoded_len} bytes, but Anki accepts a "
          "maximum of 100 bytes."
        )
    except TypeError as error:
      raise Genanki2Error(
        "Check failed: custom data holds a value that is not JSON. The column stores "
        "the object as a JSON string, so every value has to be encodable."
      ) from error
    return self

  @override
  def check(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Reject card data that cannot be written.

    `CardData.to_json()` calls this, so card data is validated at the point it is
    written instead of inside Anki.

    Args:
      recursive: Whether to check every model reachable from this one too, each once.
        If false, it only runs this model's own checks. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.check_custom_data()
    return super().check(recursive=recursive, context=context)

  def warn_unpaired_stability_difficulty(self) -> Self:
    """Warn about a stability without a difficulty or vice-versa.

    Anki reads the two as a pair and ignores one if the other is missing. A lone value
    is carried by the column, but ignored by the scheduler.
    """
    missing_stability = self.fsrs_stability is None and self.fsrs_dificulty is not None
    missing_difficulty = self.fsrs_stability is not None and self.fsrs_dificulty is None
    if missing_stability or missing_difficulty:
      warnings.warn(
        f"CardData: stability {self.fsrs_stability} and difficulty "
        f"{self.fsrs_dificulty} are not both set, so the scheduler ignores them and "
        "FSRS rebuilds memory state from the review history.",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  @override
  def warn(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Warn about card data that Anki accepts and then reads as something else.

    Args:
      recursive: Whether to warn about every model reachable from this one too, each
        once. If false, it only runs this model's own warnings. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.warn_unpaired_stability_difficulty()
    return super().warn(recursive=recursive, context=context)

  # ---------------------------------- Serialization -----------------------------------

  @classmethod
  def from_json(cls, json_data: str) -> Self:
    """Build the model from the `data` column.

    Reading is forgiving, mirroring Anki:

    - if `json_data` is not a JSON object, return an instance with all keys unset
    - a key which doesn't pass type and validation checks is dropped
    - if custom data is not a string, return an instance of `CardData` with all keys
      unset (custom data causes all other keys to be dropped)
    """
    try:
      parsed_data: Any = json.loads(json_data)
    except json.JSONDecodeError:
      return cls()

    if not isinstance(parsed_data, dict):
      return cls()

    card_data: Self = cls()
    if "cd" in parsed_data:
      if isinstance(parsed_data["cd"], str):
        try:
          card_data.custom_data = json.loads(parsed_data["cd"])
        except (json.JSONDecodeError, pyd.ValidationError):
          warnings.warn(
            f"CardData: custom data {parsed_data['cd']!r} was dropped because it's "
            "not a JSON object. Anki stores it and then refuses the card at its next "
            "review.",
            Genanki2Warning,
            stacklevel=2,
          )
      else:
        return cls()

    for name, field_info in cls.model_fields.items():
      key: str = field_info.alias or name
      if key not in parsed_data or key == "cd":
        continue
      try:
        cls.__pydantic_validator__.validate_assignment(
          card_data, name, parsed_data[key], strict=True
        )
      except pyd.ValidationError:
        continue
    return card_data

  def _json(self) -> str:
    """Return the `data` column this model writes, running no phase.

    An unset key is left out, and the FSRS floats are rounded. `Card.data` serializes
    through this, so a card's write judges its data in the card's phases, once.
    """
    return self.model_dump_json(exclude_none=True)

  def to_json(self, *, context: SerializationContext | None = None) -> str:
    """Return the `data` column this model writes.

    `CardData.prepare()`, `CardData.check()` and `CardData.warn()` run first.
    An unset key is left out, and the FSRS floats are rounded.

    If no context is given, one is created with output
    `SerializationContext.Output.JSON` and passed to the phases.
    """
    if context is None:
      context = SerializationContext(output=SerializationContext.Output.JSON)
    return self._run_phases(context=context)._json()


class CardFlag(enum.IntEnum):
  """The colour flag shown in Anki's browser, held in the low three bits of `Card.flags`.

  Anki does not use the remaining five bits, and preserves them when the flag changes.
  """

  NONE = 0
  RED = 1
  ORANGE = 2
  GREEN = 3
  BLUE = 4
  PINK = 5
  TURQUOISE = 6
  PURPLE = 7


class Card(FingerprintedRowModel[CardRow]):
  """A note rendered through one of its notetype's templates and its schedule.

  Nothing renderable is held here: the note supplies the field values and the template
  supplies the layout. What the card carries is which template, which deck, and when it
  comes up next.

  The scheduling state, from `card_type` to `data` and the flags, reaches the user only
  when they tick "Import any learning progress" while importing. By default Anki
  imports every card as a new card, unsuspended and without flags, keeping only its
  position.

  Attributes:
    id: Primary key. Unique within the package is sufficient. The importer renumbers a
      colliding one and skips the card entirely when the note already has one for this
      template. Defaults to a new monotonic id.
    note: The content behind the card. Only its id reaches the row.
    deck: Where the card is studied. Only its id reaches the row.
    template_ord: Which template rendered the card, by position, or the cloze number
      for a cloze notetype. Anki deletes a card whose ordinal has no template.
      Default: 0.
    card_type: What the scheduler is doing with the card. Default: `CardType.NEW`.
    queue: Where the card sits in the study queues. Decides what `due` counts.
      Default: `CardQueue.NEW`.
    due: A position, a day number or a unix timestamp, depending on `queue`. Outside
      `genanki2.dbcheck_repairs_disabled()`, it is repaired by `Card.wrap_due()`.
      Default: 0.
    interval_days: Days the scheduler waits between reviews. Default: 0.
    ease_factor: The permille multiplier the interval grows by. Defaults to `0` until
      the card graduates, when the deck config supplies one.
    repetitions: How many times the card has been answered. Default: 0.
    lapses: How many times it has been forgotten. Default: 0.
    left: Learning steps remaining, in the low three digits. Default: 0.
    data: The card's JSON extras: FSRS memory state, original position, custom data.
      Defaults to a `CardData` object with all fields empty.
    original_due: The `due` the card had before moving to a filtered deck. Outside
      `genanki2.dbcheck_repairs_disabled()`, it is cleared by
      `Card.clear_unexplained_original_due()` when no `original_deck` explains it, and
      by `Card.repair_unexplained_original_data()` when `deck` does not explain the
      pair. Default: 0.
    original_deck: The deck the card belongs to before moving to a filtered deck.
      Either the deck itself or the id, `0` for a card that is not in one. Outside
      `genanki2.dbcheck_repairs_disabled()`, it is cleared by
      `Card.repair_unexplained_original_data()` when `deck` is not filtered.
      Default: 0.
    flags: The browser flag, a `CardFlag` in the low three bits.
      Default: `CardFlag.NONE`.
    modification_time_s: Seconds since the epoch. Not read by an import, which skips a
      card that already exists rather than updating it, but read by a sync: an unsynced
      card keeps its local copy when that copy is the newer of the two, so a time in
      the future overrides what the server sends until the clock catches up. Defaults
      to the current time.
    update_sequence_n: The sync marker. Defaults to `-1`, which means the card carries
      changes the server has not seen; a non-negative value is the number of the sync
      that last carried it. The server sets it, so a client only writes `-1`.
      Stamped along with `modification_time_s` and therefore not preserved across an
      import.
  """

  id: Annotated[int, Id] = MonotonicId
  note: Annotated[Note, pyd.Field(exclude=True)]
  deck: Annotated[Deck, pyd.Field(exclude=True)]
  template_ord: Annotated[int, UInt16, pyd.Field(alias="ord")] = 0

  # Scheduling
  card_type: Annotated[CardType, pyd.Field(alias="type")] = CardType.NEW
  queue: CardQueue = CardQueue.NEW
  due: Annotated[int, Bounded(-(2**31), 2**31 - 1, 0)] = 0
  interval_days: Annotated[int, UInt32, pyd.Field(alias="ivl")] = 0
  ease_factor: Annotated[int, UInt16, pyd.Field(alias="factor")] = 0
  repetitions: Annotated[int, UInt32, pyd.Field(alias="reps")] = 0
  lapses: Annotated[int, UInt32] = 0
  left: Annotated[int, UInt32] = 0
  data: Annotated[
    CardData, JSONDecoder(CardData), pyd.PlainSerializer(CardData._json)
  ] = pyd.Field(default_factory=CardData)

  # Filtered deck
  original_due: Annotated[
    int, Bounded(-(2**31), 2**31 - 1, 0), pyd.Field(alias="odue")
  ] = 0
  original_deck: Annotated[
    Annotated[int, UInt63] | Deck, pyd.Field(alias="odid", exclude=True)
  ] = 0

  # Anki's Browser window
  flags: Annotated[int, UInt8] = CardFlag.NONE

  # Sync
  modification_time_s: Annotated[int, UInt63, ModAlias] = TimeS
  update_sequence_n: Annotated[int, UsnField] = -1

  _FINGERPRINT_EXCLUDED: ClassVar[frozenset[str]] = frozenset(
    ["mod", "usn", "id", "nid", "odid", "odue"]
  )

  # Read data
  _read_note_guid: str | None = None
  _read_template_ord: int | None = None

  # ------------------------------------ Validators ------------------------------------

  @pyd.model_validator(mode="after")
  def clamp_template_ord(self) -> Self:
    """Clamp a template ordinal above 30,000."""
    if _dbcheck_repairs_enabled.get():
      self.repair_template_ord_above_ceiling()
    return self

  @pyd.model_validator(mode="after")
  def wrap_due(self) -> Self:
    """Wrap a new card's position that is past Anki's ceiling."""
    if _dbcheck_repairs_enabled.get():
      self.repair_due_above_ceiling()
    return self

  @pyd.model_validator(mode="after")
  def clamp_interval_days(self) -> Self:
    """Clamp an interval above 2**31 - 1 days."""
    if _dbcheck_repairs_enabled.get():
      self.repair_interval_days_above_ceiling()
    return self

  @pyd.model_validator(mode="after")
  def clear_unexplained_original_due(self) -> Self:
    """Clear an original due that no original deck explains."""
    if _dbcheck_repairs_enabled.get():
      self.repair_unexplained_original_due()
    return self

  @pyd.model_validator(mode="after")
  def clear_unexplained_original_data(self) -> Self:
    """Clear an original deck and due that the card's own deck does not explain."""
    if _dbcheck_repairs_enabled.get():
      self.repair_unexplained_original_data()
    return self

  # ---------------------------------- Derived values ----------------------------------

  @property
  def original_deck_id(self) -> int:
    """The id of the deck the card came from, `0` when no filtered deck took it."""
    match self.original_deck:
      case int():
        return self.original_deck
      case Deck():
        return self.original_deck.id

  # ------------------------------------- Setters --------------------------------------

  def suspend(self) -> Self:
    """Mark the card as suspended.

    This change reaches the user only when they tick "Import any learning progress";
    by default Anki imports every card as new.
    """
    self.queue = CardQueue.SUSPENDED
    return self

  def bury(self) -> Self:
    """Mark the card as user buried.

    This change reaches the user only when they tick "Import any learning progress";
    by default Anki imports every card as new.
    """
    self.queue = CardQueue.USER_BURIED
    return self

  # ------------------------------------- Repairs --------------------------------------

  def repair_template_ord_above_ceiling(self) -> Self:
    """Clamp a template ordinal above 30,000, as Anki's database check does.

    Anki reads an ordinal up to 65,535 as it is, and only its database check clamps one
    above 30,000. A larger ordinal makes the row unreadable, and `Card` refuses it.

    `Card.clamp_template_ord()` runs this while the database-check repairs are on.
    """
    if self.template_ord > 30_000:
      warnings.warn(
        f"Card: template ordinal {self.template_ord} is above 30,000 and was clamped "
        "to 30,000. Anki's database check makes the same clamp.",
        Genanki2Warning,
        stacklevel=2,
      )
      self.template_ord = 30_000
    return self

  def repair_due_above_ceiling(self) -> Self:
    """Wrap a new card's position back under 2,000,000, as Anki's database check does.

    A position >=2,000,000 keeps its last six digits and moves into range
    [1,000,000, 2,000,000), still behind every position under 1,000,000. Positions
    in that range are left alone. The wrap keeps consecutive positions in order unless
    they cross a multiple of 1,000,000, and two positions can land on the same value.

    `Card.wrap_due()` runs this while the database-check repairs are on.
    """
    if (
      self.card_type == CardType.NEW
      and self.queue != CardQueue.PREVIEW_REPEAT
      and self.due >= 2_000_000
    ):
      new_due: int = 1_000_000 + self.due % 1_000_000
      warnings.warn(
        f"Card: new card position {self.due} is at or above 2,000,000 and was wrapped "
        f"to {new_due}. Anki's database check makes the same wrap.",
        Genanki2Warning,
      )
      self.due = new_due
    return self

  def repair_interval_days_above_ceiling(self) -> Self:
    """Clamp an interval above 2**31 - 1 days, as Anki's database check does.

    Anki reads an interval up to 2**32 - 1 as it is, and only its database check clamps
    one above 2**31 - 1. A larger interval makes the row unreadable, and `Card` refuses
    it.

    `Card.clamp_interval_days()` runs this while the database-check repairs are on.
    """
    if self.interval_days > 2**31 - 1:
      warnings.warn(
        f"Card: interval of {self.interval_days} days is above 2**31 - 1 and was "
        "clamped to 2**31 - 1. Anki's database check makes the same clamp.",
        Genanki2Warning,
        stacklevel=2,
      )
      self.interval_days = 2**31 - 1
    return self

  def repair_unexplained_original_due(self) -> Self:
    """Clear an original due no original deck explains, as Anki's database check does.

    The value is the `due` a learning or review card had before a filtered deck took
    it, so without an original deck it records a filtered deck the card is not in. A
    new card is unchanged, as Anki leaves it.

    `Card.clear_unexplained_original_due()` runs this while the database-check repairs
    are on.
    """
    if (
      self.original_due > 0
      and self.original_deck_id == 0
      and (self.card_type == CardType.LEARNING or self.queue == CardQueue.REVIEW)
    ):
      warnings.warn(
        f"Card: original due {self.original_due} was cleared because the card carries "
        "no original deck, so the value records a filtered deck the card is not in. "
        "Anki's database check clears it in the same way.",
        Genanki2Warning,
      )
      self.original_due = 0
    return self

  def repair_unexplained_original_data(self) -> Self:
    """Clear the original deck and due of a card whose own deck is not filtered.

    The pair records where the card was before a filtered deck took it. They are reset
    to `0` when the card sits in a normal deck. Anki's database check clears both in
    the same case.

    `Card.clear_unexplained_original_data()` and `Card.prepare()` run this, while the
    database-check repairs are on, because the card cannot detect when a filtered deck
    turns into a normal one.
    """
    if self.original_deck_id > 0 and self.deck.is_normal():
      warnings.warn(
        f"Card: original deck {self.original_deck_id} and original due "
        f"{self.original_due} were cleared because the card is in deck "
        f"{self.deck.name!r}, which is not filtered, so the pair records a filtered "
        "deck the card is not in. Anki's database check clears them in the same way.",
        Genanki2Warning,
      )
      with dbcheck_repairs_disabled():
        self.original_deck = 0
        self.original_due = 0
    return self

  # ----------------------------- Pre-serialization steps ------------------------------

  @override
  def prepare(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Clear an original deck and due the card cannot explain, before it is judged.

    A card cannot tell when its filtered deck turns normal, so
    `Card.repair_unexplained_original_data()` runs again at write time, when the decks
    are final.

    Args:
      recursive: Whether to prepare every model reachable from this one too, each once.
        Each of them is called with `recursive=False`, so an override makes only its
        own changes and leaves the walk to the call it came from. A frozen model is
        skipped. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    if _dbcheck_repairs_enabled.get():
      self.repair_unexplained_original_data()
    return super().prepare(recursive=recursive, context=context)

  @classmethod
  def check_row_consistent_note_id(cls, row: CardRow, note: Note) -> None:
    """Reject a card row that names a different note than the one given.

    A `cards` row names its note in the `nid` column, and `Card` drops the column and
    holds the note itself. A mismatch would move the card onto the given note's
    fields instead of failing, and `Card.to_row()` would then write that note's id, so
    the card renders content it was never written for.

    Args:
      row: The `cards` row.
      note: The note the row's `nid` names.

    Raises:
      CollectionReadError: the row's `nid` and the note's id differ.
    """
    if row["nid"] != note.id:
      raise CollectionReadError(
        f"Row check failed: note id {note.id} does not match the card's note id "
        f"{row['nid']}."
      )

  @classmethod
  def check_row_consistent_deck_id(cls, row: CardRow, deck: Deck) -> None:
    """Reject a card row that names a different deck than the one given.

    A `cards` row names its deck in the `did` column, and `Card` drops the column and
    holds the deck itself. A mismatch would move the card into the given deck instead
    of failing, and `Card.to_row()` would then write that deck's id, so the card is
    studied under another deck's scheduling.

    Args:
      row: The `cards` row.
      deck: The deck the row's `did` names.

    Raises:
      CollectionReadError: the row's `did` and the deck's id differ.
    """
    if row["did"] != deck.id:
      raise CollectionReadError(
        f"Row check failed: deck id {deck.id} does not match the card's deck id "
        f"{row['did']}."
      )

  def check_template_ordinal(self) -> Self:
    """Check that the template ordinal is a valid template index in the notetype.

    The check ignores cloze notetypes because a card ordinal has a different meaning,
    it names a cloze.

    Raises:
      Genanki2Error: `template_ord` is out of range for the notetype's template list.
    """
    notetype: Notetype = self.note.notetype
    if not notetype.is_cloze() and self.template_ord >= len(notetype.templates):
      raise Genanki2Error(
        f"Check failed: card template ordinal {self.template_ord} is not a valid "
        f"template index (the notetype has {len(notetype.templates)}). Anki deletes a "
        "card without a template."
      )
    return self

  def check_due_not_a_timestamp(self) -> Self:
    """Reject a day-counting `due` that holds a unix timestamp.

    `due` counts days when the card waits in the review or day-learning queue, or is a
    review card that is suspended or buried, since Anki puts it back in the review
    queue from its type. In a learning or preview queue it holds a timestamp, whatever
    the type.

    An import subtracts the day delta between the two collections from every `due`
    that counts days, so a timestamp arrives shifted and lands centuries out. Anki's
    database check resets such a card to today. The threshold is 100,000 (days),
    while a timestamp is around 1.7 billion, so no real day count reaches it.

    Raises:
      Genanki2Error: `due` counts days and is above 100,000.
    """
    queue_counts_days: bool = self.queue in (CardQueue.REVIEW, CardQueue.DAY_LEARNING)
    out_of_rotation: tuple[CardQueue, ...] = (
      CardQueue.SUSPENDED,
      CardQueue.SCHEDULER_BURIED,
      CardQueue.USER_BURIED,
    )
    review_out_of_rotation: bool = (
      self.queue in out_of_rotation and self.card_type == CardType.REVIEW
    )
    if (queue_counts_days or review_out_of_rotation) and self.due > 100_000:
      raise Genanki2Error(
        f"Check failed: card {self.id} counts due in days since the collection was "
        f"created, but {self.due} holds what looks like a timestamp. Use "
        "`genanki2.Collection.days_elapsed_since_creation` to compute the day count."
      )
    return self

  def check_original_due_not_a_timestamp(self) -> Self:
    """Reject a day-counting `original_due` that holds a unix timestamp.

    An import subtracts the day delta between the two collections from every `due`
    that counts days, so a timestamp arrives shifted and lands centuries out.
    Nothing reads the value while the card is in the filtered deck, so the damage
    surfaces later: emptying the deck copies `original_due` into `due`, and the card
    returns home due on a date centuries out. The threshold is 100,000 (days),
    while a timestamp is around 1.7 billion, so no real day count reaches it.

    Raises:
      Genanki2Error: `original_due` counts days and is above 100,000.
    """
    if self.card_type == CardType.REVIEW and self.original_due > 100_000:
      raise Genanki2Error(
        f"Check failed: card {self.id} counts original due in days since the "
        f"collection was created, but {self.original_due} holds what looks like a "
        "timestamp. Emptying the filtered deck copies it into due, so the value "
        "outlives the deck that explains it."
      )
    return self

  def check_original_deck_is_normal(self) -> Self:
    """Reject a card whose original deck is filtered.

    Anki retrieves the scheduling preset from the deck, but a filtered deck doesn't
    have one and the card cannot be reviewed.

    Nothing in Anki repairs it.

    Raises:
      Genanki2Error: the card's original deck is a filtered one.
    """
    if isinstance(self.original_deck, Deck) and self.original_deck.is_filtered():
      raise Genanki2Error(
        f"Check failed: card {self.id} names filtered deck {self.original_deck.name!r} "
        "as its original deck. The original deck is where the card returns and "
        "gives it its scheduling, so it has to be a normal one."
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
        f"(now is {now}). Anki reads it on a sync: an unsynced card keeps its local "
        "copy when that copy is newer than the server's, so this card would override "
        "the server's until that time arrives."
      )
    return self

  @override
  def check(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Reject a card that cannot be written.

    `Card.to_row()` calls this, so a card is checked at the point it is written
    instead of inside Anki, and a half-built one can be passed around freely.

    Args:
      recursive: Whether to check every model reachable from this one too, each once.
        If false, it only runs this model's own checks. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.check_template_ordinal()
    self.check_due_not_a_timestamp()
    self.check_original_due_not_a_timestamp()
    self.check_original_deck_is_normal()
    self.check_mod_time_not_in_future()
    return super().check(recursive=recursive, context=context)

  def warn_template_ord_above_ceiling(self) -> Self:
    """Warn about a template ordinal above 30,000, which was not clamped.

    Anki keeps the ordinal until its database check clamps it.
    `Card.clamp_template_ord()` repairs it, but when repairs are skipped (inside
    `genanki2.dbcheck_repairs_disabled()`), this warning is emitted.
    """
    if self.template_ord > 30_000:
      warnings.warn(
        f"Card: template ordinal {self.template_ord} is above 30,000 and was not "
        "repaired. Anki's database check clamps it to 30,000. "
        "`Card.repair_template_ord_above_ceiling()` makes the same repair.",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  def warn_due_above_ceiling(self) -> Self:
    """Warn about a new card position of 2,000,000 or more, which was not wrapped.

    Anki keeps the position until its database check wraps it. `Card.wrap_due()`
    repairs it, but when repairs are skipped (inside
    `genanki2.dbcheck_repairs_disabled()`), this warning is emitted.
    """
    if (
      self.card_type == CardType.NEW
      and self.queue != CardQueue.PREVIEW_REPEAT
      and self.due >= 2_000_000
    ):
      new_due: int = 1_000_000 + self.due % 1_000_000
      warnings.warn(
        f"Card: new card position {self.due} is at or above 2,000,000 and was not "
        f"repaired. Anki's database check wraps the position to {new_due}. "
        "`Card.repair_due_above_ceiling()` makes the same repair.",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  def warn_interval_days_above_ceiling(self) -> Self:
    """Warn about an interval above 2**31 - 1 days, which was not clamped.

    Anki keeps the interval until its database check clamps it.
    `Card.clamp_interval_days()` repairs it, but when repairs are skipped (inside
    `genanki2.dbcheck_repairs_disabled()`), this warning is emitted.
    """
    if self.interval_days > 2**31 - 1:
      warnings.warn(
        f"Card: interval of {self.interval_days} days is above 2**31 - 1 and was not "
        "repaired. Anki's database check clamps it to 2**31 - 1. "
        "`Card.repair_interval_days_above_ceiling()` makes the same repair.",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  def warn_graduated_card_without_ease_factor(self) -> Self:
    """Warn about a review or relearning card that carries no ease factor.

    A card takes the preset's initial ease when it graduates, so Anki leaves the column
    at zero while the card is still in learning. Nothing repairs it later.
    SM-2 multiplies the interval by the factor, so a card at zero grows by about a day
    per review instead of by its ease. FSRS is not affected.

    An `interval_days` of zero is the same kind of gap and is not reported, because it
    recovers on its own: the card is scheduled at 1, 1, 2 and 3 days and multiplies
    normally from there.
    """
    if self.ease_factor == 0 and self.card_type in (
      CardType.REVIEW,
      CardType.RELEARNING,
    ):
      warnings.warn(
        f"Card: card {self.id} is typed as review or relearning but carries no ease "
        "factor. Anki leaves the factor at zero only until a card graduates. Nothing "
        "repairs it. SM-2 is affected by this, growing the interval by about a day per "
        "review instead of multiplying it. Set the factor to the one with which the "
        "card would have graduated, 2500 by default.",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  def warn_unexplained_original_due(self) -> Self:
    """Warn about an original due that no original deck explains.

    Anki keeps the value until its database check clears it.
    `Card.clear_unexplained_original_due()` repairs it, but when repairs are skipped
    (inside `genanki2.dbcheck_repairs_disabled()`), this warning is emitted.
    """
    if (
      self.original_due > 0
      and self.original_deck_id == 0
      and (self.card_type == CardType.LEARNING or self.queue == CardQueue.REVIEW)
    ):
      warnings.warn(
        f"Card: original due {self.original_due} should be cleared because the card "
        "carries no original deck, so the value records a filtered deck the card is "
        "not in. Anki's database check clears it. "
        "`Card.repair_unexplained_original_due()` makes the same repair.",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  def warn_unexplained_original_data(self) -> Self:
    """Warn about an original deck and due that the card's own deck does not explain.

    Anki keeps the pair until its database check clears it.
    `Card.clear_unexplained_original_data()` and `Card.prepare()` repair it, but when
    repairs are skipped (inside `genanki2.dbcheck_repairs_disabled()`), this warning is
    emitted.
    """
    if self.original_deck_id > 0 and self.deck.is_normal():
      warnings.warn(
        f"Card: original deck {self.original_deck_id} and original due "
        f"{self.original_due} should be cleared because the card is in deck "
        f"{self.deck.name!r}, which is not filtered, so the pair records a filtered "
        "deck the card is not in. Anki's database check clears them. "
        "`Card.repair_unexplained_original_data()` makes the same repair.",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  def warn_unimported_changes(
    self, context: SerializationContext | None = None
  ) -> Self:
    """Warn about a change to a read card that does not reach a user who has it.

    Anki's importer adds a card only when the user's copy of its note has no card for
    its template, and never updates a card the user has. So while the card's note keeps
    the guid it was read with and the card keeps its template ordinal, no change to it
    reaches a user who already has the note: not a move to another deck, a flag or its
    schedule. A card under another guid or ordinal is added, so it is not compared.

    A colpkg restore replaces the whole collection, so the warning is skipped.

    Args:
      context: Extra information from the serialization process. Default: `None`.
    """
    if self.note.guid != self._read_note_guid or (
      self.template_ord != self._read_template_ord
    ):
      return self

    match context:
      case SerializationContext(output=SerializationContext.Output.COLPKG):
        return self
      case SerializationContext(output=SerializationContext.Output.APKG):
        message: str = "An apkg import"
      case _:
        message = "If imported from an apkg, the import"

    if self.is_changed_since_read():
      warnings.warn(
        f'Card: card {self.id} of note "{self.note.guid}" (template ordinal '
        f"{self.template_ord}) changed since it was read. {message} skips a card "
        "whose note a user already has with a card for this template, so the change "
        "does not reach that user.",
        Genanki2Warning,
        stacklevel=2,
      )

    return self

  @override
  def warn(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Warn about a card with unwanted behaviour, or a change that does not reach a user.

    Warnings can be turned off using `with genanki2.warnings_disabled():`.

    Args:
      recursive: Whether to warn about every model reachable from this one too, each
        once. If false, it only runs this model's own warnings. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.warn_template_ord_above_ceiling()
    self.warn_due_above_ceiling()
    self.warn_interval_days_above_ceiling()
    self.warn_graduated_card_without_ease_factor()
    self.warn_unexplained_original_due()
    self.warn_unexplained_original_data()
    self.warn_unimported_changes(context)
    return super().warn(recursive=recursive, context=context)

  # ---------------------------------- Serialization -----------------------------------

  @override
  @classmethod
  def from_row(cls, row: CardRow, note: Note, deck: Deck) -> Self:  # pyright: ignore[reportIncompatibleMethodOverride]
    """Build a card from a `cards` row, with the note and deck its ids point at.

    Neither can come from the row, which carries only their ids, so both are passed in
    and checked against it. The scheduling columns are repaired on the way in, as
    Anki's database check repairs them.

    Raises:
      CollectionReadError: the card's note id and `note.id` don't match.
      CollectionReadError: the card's deck id and `deck.id` don't match.
    """
    cls.check_row_consistent_note_id(row, note)
    cls.check_row_consistent_deck_id(row, deck)
    card: Self = super().from_row({**row, "note": note, "deck": deck})
    card._read_note_guid = note.guid
    card._read_template_ord = card.template_ord
    return card

  @override
  def _row(self) -> CardRow:
    """Serialize the card as a `cards` row with note and deck ids, running no phase."""
    return {
      **super()._row(),
      "nid": self.note.id,
      "did": self.deck.id,
      "odid": self.original_deck_id,
    }

  @override
  def to_row(self, *, context: SerializationContext | None = None) -> CardRow:
    """Return the `cards` row for this card, with the referenced note and deck ids.

    If no context is given, one is created with output
    `SerializationContext.Output.ROW` and passed to the phases.
    """
    return super().to_row(context=context)
