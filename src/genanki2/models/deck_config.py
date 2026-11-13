import enum
import math
import warnings
from datetime import datetime
from typing import Annotated, ClassVar

import pydantic as pyd
from google.protobuf.message import Message
from typing_extensions import Self, override

import genanki2._proto as _proto  # noqa: PLR0402, import cycle via __init__
from genanki2._typed_schema import DeckConfigRow
from genanki2.errors import Genanki2Error, Genanki2Warning
from genanki2.models.annotations import (
  Bounded,
  BytesDecoder,
  Clamped,
  Id,
  Int32,
  LtRUnion,
  MonotonicId,
  MTimeSecsAlias,
  TimeS,
  UInt32,
  UInt63,
  UsnField,
)
from genanki2.models.base import FingerprintedRowModel, ProtobufModel
from genanki2.models.serialization_context import SerializationContext
from genanki2.time import time_s


class DeckConfigInner(ProtobufModel):
  """The `deck_config.config` blob: how a preset schedules the decks that use it.

  The defaults are the ones Anki uses to create a preset. Eighteen of the scalars are
  bounds-checked by `annotations.Bounded()`, which replaces a value outside its range
  with the default rather than clamping it (Anki repairs them on a read too).

  Attributes:
    learn_steps: The delays, in minutes, a new card works through. The first applies to
      `Again` on a new card and the next to `Good`; a card past the last one becomes a
      review card. A value is clamped in [0, 2**31 // 60] because Anki stores it as an
      Int32 in seconds at some point. Default: `[1.0, 10.0]`.
    relearn_steps: The delays, in minutes, a lapsed review card works through. With
      none, `Again` shortens the card's interval without sending it back to relearning.
      A value is clamped in [0, 2**31 // 60] because Anki stores it as an Int32 in
      seconds at some point. Default: `[10.0]`.
    fsrs_params_4: The FSRS 4.x parameters. Default: `[]`.
    fsrs_params_5: The FSRS 5.0 parameters. Default: `[]`.
    fsrs_params_6: The FSRS 6.0 parameters. Default: `[]`.
    new_per_day: New cards to introduce in a day, when any are available. Anki accepts
      0 to 9999. Default: 20.
    reviews_per_day: Review cards to show in a day, when any are due. Anki accepts 0 to
      9999. Default: 200.
    new_per_day_minimum: Currently read by nothing. Anki accepts 0 to 9999. Default: 0.
    initial_ease: The ease multiplier a card leaves learning with, applied by `Good`.
      Anki accepts 1.31 to 5.0. Default: 2.5.
    easy_multiplier: The extra multiplier applied to a review interval answered `Easy`.
      Anki accepts 1.0 to 5.0. Default: 1.3.
    hard_multiplier: The multiplier applied to a review interval answered `Hard`. Anki
      accepts 0.5 to 1.3. Default: 1.2.
    lapse_multiplier: The multiplier applied to a review interval answered `Again`.
      Anki accepts 0.0 to 1.0. Default: 0.0.
    interval_multiplier: A multiplier applied to every review interval. Anki accepts
      0.5 to 2.0. Default: 1.0.
    maximum_review_interval: The most days a review card waits. At the limit, `Hard`,
      `Good` and `Easy` all give the same delay. Anki accepts 1 to 36500.
      Default: 36500.
    minimum_lapse_interval: The fewest days a review card waits after `Again`. Anki
      accepts 1 to 36500. Default: 1.
    graduating_interval_good: Days to wait after `Good` on the last learning step. Anki
      accepts 1 to 36500. Default: 1.
    graduating_interval_easy: Days to wait after `Easy` takes a card straight out of
      learning. Anki accepts 1 to 36500. Default: 4.
    new_card_insert_order: How new cards are positioned as they are added. Changing it
      renumbers the new cards already in the decks the preset covers.
      Default: `NewCardInsertOrder.DUE`.
    new_card_gather_priority: Which new cards are gathered for the day, and in what
      order. Default: `NewCardGatherPriority.DECK`.
    new_card_sort_order: How the gathered new cards are sorted before being shown.
      Default: `NewCardSortOrder.TEMPLATE`.
    new_mix: When new cards are shown relative to reviews.
      Default: `ReviewMix.MIX_WITH_REVIEWS`.
    review_order: The order in which to show due review cards.
      Default: `ReviewCardOrder.DAY`.
    interday_learning_mix: When to show and the order of learning cards that cross a
      day boundary relative to reviews. Default: `ReviewMix.MIX_WITH_REVIEWS`.
    leech_action: What happens to a card that reaches `leech_threshold`.
      Default: `LeechAction.TAG_ONLY`.
    leech_threshold: How many `Again` answers are accepted on a review card before it
      counts as a leech. Anki accepts 1 to 9999. Default: 8.
    disable_autoplay: Do not play a card's audio on its own, leaving it to the replay
      action. Default: `False`.
    cap_answer_time_to_secs: The longest a single review is recorded as taking, so a
      card left on screen does not distort the statistics. Anki accepts 1 to 9999.
      Default: 60.
    show_timer: Show a timer on the study screen counting the current card.
      Default: `False`.
    stop_timer_on_answer: Stop that timer when the answer is revealed. Statistics are
      unaffected. Default: `False`.
    seconds_to_show_question: With auto advance on, seconds to wait before applying
      `question_action`. `0.0` disables it. Anki accepts any positive value.
      Default: 0.0.
    seconds_to_show_answer: With auto advance on, seconds to wait before applying
      `answer_action`. `0.0` disables it. Anki accepts any positive value.
      Default: 0.0.
    question_action: What auto advance does once `seconds_to_show_question` has passed.
      Default: `QuestionAction.SHOW_ANSWER`.
    answer_action: What auto advance does once `seconds_to_show_answer` has passed.
      Default: `AnswerAction.BURY_CARD`.
    wait_for_audio: Let a card's audio finish before auto advance acts.
      Default: `True`.
    skip_question_when_replaying_answer: Leave the question's audio out when the replay
      action is used on the answer side. Default: `False`.
    bury_new: Delay the note's other new cards until the next day. Default: `False`.
    bury_reviews: Delay the note's other review cards until the next day.
      Default: `False`.
    bury_interday_learning: Delay the note's other learning cards whose interval is
      over a day. Default: `False`.
    desired_retention: The chance of recall FSRS schedules for. Anki accepts 0.7 to
      0.99. Default: 0.9.
    ignore_revlogs_before_date: A `YYYY-MM-DD` date whose earlier reviews FSRS leaves
      out when optimizing. Empty means no cutoff. A string that does not parse is
      reported when the optimizer runs, not when it is stored. Default: `""`.
    easy_days_percentages: A workload multiplier per weekday, Monday first, for FSRS
      load balancing. `0.0` means `Minimum` workload, `1.0` means `Normal` and any
      other value means `Reduced`, which is `0.5` (even a value > 1.0). Anki wants
      exactly seven entries, or none, which it reads as seven normal days. Any other
      length stops the review queue from being built at all. The error is
      collection-wide and no deck can be studied until it is fixed.
      Default: seven `1.0`s.
    historical_retention: The recall rate FSRS assumes for reviews it cannot see, which
      are the ones before `ignore_revlogs_before_date` and the ones lost to a purged or
      imported history. Anki accepts 0.7 to 0.97. Default: 0.9.
    param_search: A browser search limiting the cards the FSRS optimizer trains on.
      Empty means the cards of the decks this preset covers. Not validated.
      Default: `""`.
    other: Schema 11 JSON keys with no protobuf fields, kept as raw bytes. They have to
      decode as a JSON object, otherwise Anki replaces anything that does not parse
      with an empty map. Default: `b""`.
  """

  # ----------------------------------- Nested types -----------------------------------

  class NewCardInsertOrder(enum.IntEnum):
    """The position a new card is added at, which decides the order it is shown in."""

    DUE = 0
    RANDOM = 1

  class NewCardGatherPriority(enum.IntEnum):
    """Which new cards are gathered for the day and in what order.

    - `DECK`: decks in alphabetical order, then ascending position
    - `DECK_THEN_RANDOM_NOTES`: deck in alphabetical order, then random within a deck
    - `LOWEST_POSITION`: ascending position order
    - `HIGHEST_POSITION`: descending position order
    - `RANDOM_NOTES`: random, keeps cards grouped
    - `RANDOM_CARDS`: random
    """

    DECK = 0
    DECK_THEN_RANDOM_NOTES = 5
    LOWEST_POSITION = 1
    HIGHEST_POSITION = 2
    RANDOM_NOTES = 3
    RANDOM_CARDS = 4

  class NewCardSortOrder(enum.IntEnum):
    """How the gathered new cards are sorted before being shown.

    - `TEMPLATE`: ascending template ordinal
    - `TEMPLATE_THEN_RANDOM`: ascending template ordinal, then random
    - `NO_SORT`: gather order
    """

    TEMPLATE = 0
    NO_SORT = 1
    TEMPLATE_THEN_RANDOM = 2
    RANDOM_NOTE_THEN_TEMPLATE = 3
    RANDOM_CARD = 4

  class ReviewCardOrder(enum.IntEnum):
    """The order for due review cards.

    The default puts the longest-waiting cards first.
    """

    DAY = 0
    DAY_THEN_DECK = 1
    DECK_THEN_DAY = 2
    INTERVALS_ASCENDING = 3
    INTERVALS_DESCENDING = 4
    EASE_ASCENDING = 5
    EASE_DESCENDING = 6
    RETRIEVABILITY_ASCENDING = 7
    RETRIEVABILITY_DESCENDING = 11
    RELATIVE_OVERDUENESS = 12
    RANDOM = 8
    ADDED = 9
    REVERSE_ADDED = 10

  class ReviewMix(enum.IntEnum):
    """When a class of card is shown relative to review cards."""

    MIX_WITH_REVIEWS = 0
    AFTER_REVIEWS = 1
    BEFORE_REVIEWS = 2

  class LeechAction(enum.IntEnum):
    """What happens to a card that reaches the leech threshold.

    - `TAG_ONLY`: tag the note `leech` and show a pop-up
    - `SUSPEND`: tag and hide the card until it is unsuspended by hand
    """

    SUSPEND = 0
    TAG_ONLY = 1

  class AnswerAction(enum.IntEnum):
    """What auto advance does once the answer has been shown for long enough."""

    BURY_CARD = 0
    ANSWER_AGAIN = 1
    ANSWER_GOOD = 2
    ANSWER_HARD = 3
    SHOW_REMINDER = 4

  class QuestionAction(enum.IntEnum):
    """What auto advance does once the question has been shown for long enough."""

    SHOW_ANSWER = 0
    SHOW_REMINDER = 1

  # ------------------------------------ Attributes ------------------------------------

  MessageType: ClassVar[type[Message]] = _proto.DeckConfigConfig

  learn_steps: list[Annotated[float, Clamped(0, 2**31 // 60)]] = pyd.Field(
    default=[1.0, 10.0]
  )
  relearn_steps: list[Annotated[float, Clamped(0, 2**31 // 60)]] = pyd.Field(
    default=[10.0]
  )

  fsrs_params_4: list[float] = pyd.Field(default_factory=list)
  fsrs_params_5: list[float] = pyd.Field(default_factory=list)
  fsrs_params_6: list[float] = pyd.Field(default_factory=list)

  new_per_day: Annotated[int, UInt32, Bounded(0, 9999, 20)] = 20
  reviews_per_day: Annotated[int, UInt32, Bounded(0, 9999, 200)] = 200

  new_per_day_minimum: Annotated[int, UInt32, Bounded(0, 9999, 0)] = 0

  initial_ease: Annotated[float, Bounded(1.31, 5.0, 2.5, float32=True)] = 2.5
  easy_multiplier: Annotated[float, Bounded(1.0, 5.0, 1.3, float32=True)] = 1.3
  hard_multiplier: Annotated[float, Bounded(0.5, 1.3, 1.2, float32=True)] = 1.2
  lapse_multiplier: Annotated[float, Bounded(0.0, 1.0, 0.0, float32=True)] = 0.0
  interval_multiplier: Annotated[float, Bounded(0.5, 2.0, 1.0, float32=True)] = 1.0

  maximum_review_interval: Annotated[int, UInt32, Bounded(1, 36500, 36500)] = 36500
  minimum_lapse_interval: Annotated[int, UInt32, Bounded(1, 36500, 1)] = 1

  graduating_interval_good: Annotated[int, UInt32, Bounded(1, 36500, 1)] = 1
  graduating_interval_easy: Annotated[int, UInt32, Bounded(1, 36500, 4)] = 4

  new_card_insert_order: Annotated[NewCardInsertOrder | int, LtRUnion, Int32] = (
    NewCardInsertOrder.DUE
  )
  new_card_gather_priority: Annotated[NewCardGatherPriority | int, LtRUnion, Int32] = (
    NewCardGatherPriority.DECK
  )
  new_card_sort_order: Annotated[NewCardSortOrder | int, LtRUnion, Int32] = (
    NewCardSortOrder.TEMPLATE
  )
  new_mix: Annotated[ReviewMix | int, LtRUnion, Int32] = ReviewMix.MIX_WITH_REVIEWS

  review_order: Annotated[ReviewCardOrder | int, LtRUnion, Int32] = ReviewCardOrder.DAY

  interday_learning_mix: Annotated[ReviewMix | int, LtRUnion, Int32] = (
    ReviewMix.MIX_WITH_REVIEWS
  )

  leech_action: Annotated[LeechAction | int, LtRUnion, Int32] = LeechAction.TAG_ONLY
  leech_threshold: Annotated[int, UInt32, Bounded(1, 9999, 8)] = 8

  disable_autoplay: bool = False
  cap_answer_time_to_secs: Annotated[int, UInt32, Bounded(1, 9999, 60)] = 60
  show_timer: bool = False
  stop_timer_on_answer: bool = False
  seconds_to_show_question: Annotated[
    float, Bounded(0.0, math.inf, 0.0, float32=True)
  ] = 0.0
  seconds_to_show_answer: Annotated[
    float, Bounded(0.0, math.inf, 0.0, float32=True)
  ] = 0.0
  question_action: Annotated[QuestionAction | int, LtRUnion, Int32] = (
    QuestionAction.SHOW_ANSWER
  )
  answer_action: Annotated[AnswerAction | int, LtRUnion, Int32] = AnswerAction.BURY_CARD
  wait_for_audio: bool = True
  skip_question_when_replaying_answer: bool = False

  bury_new: bool = False
  bury_reviews: bool = False
  bury_interday_learning: bool = False

  # FSRS
  desired_retention: Annotated[float, Bounded(0.7, 0.99, 0.9, float32=True)] = 0.9
  ignore_revlogs_before_date: str = ""
  easy_days_percentages: list[float] = pyd.Field(default=[1.0] * 7)

  historical_retention: Annotated[float, Bounded(0.7, 0.97, 0.9, float32=True)] = 0.9
  param_search: str = ""

  other: bytes = b""

  # ---------------------------------- Derived values ----------------------------------

  @property
  def fsrs_params(self) -> tuple[float, ...]:
    """The parameters Anki schedules with.

    Returns first non-empty of `DeckConfigInner`'s `fsrs_params_6`, `fsrs_params_5` or
    `fsrs_params_4`. Anki falls back on its own built-in parameters when all three are
    empty.
    """
    return tuple(self.fsrs_params_6 or self.fsrs_params_5 or self.fsrs_params_4)

  # ----------------------------- Pre-serialization steps ------------------------------

  def check_learning_steps(self) -> Self:
    """Reject a learning step outside of [0, 2**31 // 60] minutes.

    A step is clamped by `annotations.Clamped()` when the list is validated, which
    mutating the list in place skips. Assigning the list back to itself runs the
    validators again and clamps.

    Raises:
      Genanki2Error: a step is negative, NaN, or over `2**31 // 60` minutes.
    """
    for source in ("learn_steps", "relearn_steps"):
      steps: list[float] = getattr(self, source)
      if not all(0 <= step <= 2**31 // 60 for step in steps):
        raise Genanki2Error(
          f"Check failed: {source} {steps} holds a value outside [0, {2**31 // 60}] "
          f"minutes. Assign the list back to `genanki2.DeckConfigInner.{source}` to "
          "clamp it."
        )
    return self

  def check_fsrs_params_length(self) -> Self:
    """Reject an FSRS parameter list of a length FSRS cannot take.

    Anki cannot build a scheduler with an invalid list, making all decks with this
    preset impossible to study. The newest non-empty list is checked. An empty list is
    valid and tells Anki to use built-in parameters.

    Raises:
      Genanki2Error: a list is neither empty nor the length its FSRS version takes.
    """
    msg: str = (
      "Check failed: fsrs_params_{0} holds {1} values, but FSRS {0} takes {2}. "
      "Leave the list empty for Anki's built-in parameters."
    )
    if self.fsrs_params_6 and len(self.fsrs_params_6) != 21:
      raise Genanki2Error(msg.format("6", len(self.fsrs_params_6), "21"))
    elif self.fsrs_params_5 and len(self.fsrs_params_5) != 19:
      raise Genanki2Error(msg.format("5", len(self.fsrs_params_5), "19"))
    elif self.fsrs_params_4 and len(self.fsrs_params_4) != 17:
      raise Genanki2Error(msg.format("4", len(self.fsrs_params_4), "17"))
    return self

  def check_date(self) -> Self:
    """Reject an `ignore_revlogs_before_date` that doesn't parse.

    Anki stores the string and parses it during a review. This catches it much sooner.

    An empty string is valid and says there is no cutoff.

    Raises:
      Genanki2Error: the date is not `YYYY-MM-DD`.
    """
    try:
      if self.ignore_revlogs_before_date:
        _ = datetime.strptime(self.ignore_revlogs_before_date, "%Y-%m-%d")
    except ValueError as error:
      raise Genanki2Error(
        "Check failed: ignore_revlogs_before_date is "
        f'"{self.ignore_revlogs_before_date}", which is not a YYYY-MM-DD date.'
      ) from error
    return self

  def check_easy_days_percentages(self) -> Self:
    """Reject an `easy_days_percentages` that is neither empty nor of size 7.

    Anki parses all the presets in the collection at once. One list of the wrong length
    stops every deck from being studied.

    An empty list is valid and marks that every day is a normal day.

    Raises:
      Genanki2Error: the list holds a number of days other than 0 or 7.
    """
    if len(self.easy_days_percentages) not in (0, 7):
      raise Genanki2Error(
        f"Check failed: easy_days_percentages holds {len(self.easy_days_percentages)} "
        "values. Anki wants seven, one per weekday from Monday, or none at all. This "
        "breaks Anki collection-wide preset parsing, making every deck impossible to "
        "study."
      )
    return self

  @override
  def check(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Reject a preset Anki stores without complaint and then trips over.

    `ProtobufModel.to_bytes()` calls this.

    Args:
      recursive: Whether to check every model reachable from this one too, each once.
        If false, it only runs this model's own checks. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.check_learning_steps()
    self.check_fsrs_params_length()
    self.check_date()
    self.check_easy_days_percentages()
    return super().check(recursive=recursive, context=context)

  @override
  def warn(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Warn about a preset with unknown enum values.

    Warnings can be turned off using `with genanki2.warnings_disabled():`.

    Args:
      recursive: Whether to warn about every model reachable from this one too, each
        once. If false, it only runs this model's own warnings. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.warn_int_value_in_enum_field()
    return super().warn(recursive=recursive, context=context)


class DeckConfig(FingerprintedRowModel[DeckConfigRow]):
  """One deck config: the named preset that schedules a deck.

  A deck refers to it through `DeckNormalKind.config_id`.

  Every collection holds a `Default` preset (id 1), which is also the fallback for an
  invalid config_id in a deck.

  Important: in Anki, presets are matched by id on import. On a match, the incoming
  preset is dropped and the decks arriving with it are scheduled by whatever the target
  collection holds under that id.

  Attributes:
    id: Primary key, used during import to match. Defaults to a new monotonic id.
    name: Title in the UI. Stored as given, no normalization or uniqueness condition.
    config: The `deck_config.config` blob, holding the scheduling itself. Defaults to
      what Anki writes for a preset it creates.
    modification_time_s: Seconds since the epoch. Not read by an import, which drops an
      incoming preset whose id already exists rather than updating it, but read by a
      sync: an unsynced preset keeps its local copy when that copy is the newer of the
      two, so a time in the future overrides what the server sends until the clock
      catches up. Defaults to the current time.
    update_sequence_n: The sync marker. Defaults to `-1`, which means the preset
      carries changes the server has not seen; a non-negative value is the number of
      the sync that last carried it. The server sets it, so a client only writes `-1`.
      Not preserved across an import, which stamps the target collection's own value.
  """

  id: Annotated[int, Id] = MonotonicId
  name: str
  config: Annotated[
    DeckConfigInner,
    BytesDecoder(DeckConfigInner),
    pyd.PlainSerializer(DeckConfigInner._bytes),
  ] = pyd.Field(default_factory=DeckConfigInner)

  # Sync
  modification_time_s: Annotated[int, UInt63, MTimeSecsAlias] = TimeS
  update_sequence_n: Annotated[int, UsnField] = -1

  _FINGERPRINT_EXCLUDED: ClassVar[frozenset[str]] = frozenset(["mtime_secs", "usn"])

  # Read data
  _read_id: int | None = None

  # ------------------------------------- Repairs --------------------------------------

  def repair_unsearchable_name(self) -> Self:
    """Rewrite a name the FSRS optimizer cannot search for.

    Drops every double quote and any trailing backslashes, which is what
    `DeckConfig.warn_unsearchable_name()` warns about. The repair is a no-op if the new
    name is empty, because an empty name is also unsearchable.

    Nothing calls this; a caller opts in.
    """
    if not self.name:
      warnings.warn(
        "DeckConfig: nothing to repair, the name is empty.",
        Genanki2Warning,
        stacklevel=2,
      )
    elif name := self.name.replace('"', "").rstrip("\\"):
      self.name = name
    else:
      warnings.warn(
        "DeckConfig: repair aborted because the repair would empty the name, which is "
        "also unsearchable.",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  # ----------------------------- Pre-serialization steps ------------------------------

  def check_mod_time_not_in_future(self) -> Self:
    """Reject a modification time that has not happened yet because it blocks updates.

    Raises:
      Genanki2Error: `modification_time_s` is in the future.
    """
    if self.modification_time_s > (now := time_s()):
      raise Genanki2Error(
        f"Check failed: modification time {self.modification_time_s} is in the future "
        f"(now is {now}). Anki reads it on a sync: an unsynced preset keeps its local "
        "copy when that copy is newer than the server's, so this preset would override "
        "the server's until that time arrives."
      )
    return self

  @override
  def check(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Reject a preset that cannot be written.

    `DeckConfig.to_row()` calls this, so a preset is validated at the point it is
    written instead of inside Anki, and a half-built one can be passed around freely.

    Args:
      recursive: Whether to check every model reachable from this one too, each once.
        If false, it only runs this model's own checks. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.check_mod_time_not_in_future()
    return super().check(recursive=recursive, context=context)

  def warn_unsearchable_name(self) -> Self:
    """Warn about a name the FSRS optimizer cannot search for.

    When `DeckConfigInner.param_search` is not set, the preset's cards are searched by
    name, but the name has to have correct search syntax.
    """
    if not self.name or '"' in self.name or self.name.endswith("\\"):
      warnings.warn(
        f'DeckConfig: "{self.name}" has a name Anki cannot search for and the search '
        "will return nothing or fail.",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  def warn_unimported_changes(
    self, context: SerializationContext | None = None
  ) -> Self:
    """Warn about a change to a read preset that does not reach a user who has it.

    Anki's importer adds a preset only when the user doesn't have one with this id, and
    Anki doesn't update the existing one with the incoming one's values. So a change
    does not reach a user who already has a preset with this id. Under a new id, the
    preset is added and a deck has to move to it. Both need the user to tick "Import
    any deck presets" while importing.

    A colpkg restore replaces the whole collection, so the warning is skipped.

    Args:
      context: Extra information from the serialization process. Default: `None`.
    """
    match context:
      case SerializationContext(output=SerializationContext.Output.COLPKG):
        return self
      case SerializationContext(output=SerializationContext.Output.APKG):
        message: str = "An apkg import"
      case _:
        message = "If imported from an apkg, the import"

    if self.id == self._read_id and self.is_changed_since_read():
      warnings.warn(
        f'DeckConfig: "{self.name}" (id {self.id}) changed since it was read. '
        f"{message} keeps the preset that a user already has under this id, so the "
        "change does not reach that user. Give it a new id (using `genanki2.monotonic_id()`) to have it "
        "added; a deck the user already has needs to move to it.",
        Genanki2Warning,
        stacklevel=2,
      )

    return self

  @override
  def warn(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Warn about a preset Anki trips over later, or a change that does not reach a user.

    Warnings can be turned off using `with genanki2.warnings_disabled():`.

    Args:
      recursive: Whether to warn about every model reachable from this one too, each
        once. If false, it only runs this model's own warnings. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.warn_unsearchable_name()
    self.warn_unimported_changes(context)
    return super().warn(recursive=recursive, context=context)

  # ---------------------------------- Serialization -----------------------------------

  @override
  @classmethod
  def from_row(cls, row: DeckConfigRow) -> Self:
    """Build a deck config from a `deck_config` row, recording its content and id."""
    deck_config: Self = super().from_row(row)
    deck_config._read_id = deck_config.id
    return deck_config

  @override
  def to_row(self, *, context: SerializationContext | None = None) -> DeckConfigRow:
    """Return a `deck_config` row for this deck config.

    If no context is given, one is created with output
    `SerializationContext.Output.ROW` and passed to the phases.
    """
    return super().to_row(context=context)
