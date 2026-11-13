import enum
import warnings
from typing import Annotated

import pydantic as pyd
from typing_extensions import Self, override

from genanki2._typed_schema import RevlogRow
from genanki2.errors import CollectionReadError, Genanki2Warning
from genanki2.models.annotations import (
  Bounded,
  Id,
  Int32,
  Int64,
  LtRUnion,
  MonotonicId,
  UInt8,
  UInt32,
  UsnField,
)
from genanki2.models.base import RowModel
from genanki2.models.card import Card
from genanki2.models.serialization_context import SerializationContext
from genanki2.settings import _dbcheck_repairs_enabled


class ReviewRating(enum.IntEnum):
  """The button pressed when answering the card, or none."""

  NONE = 0
  AGAIN = 1
  HARD = 2
  GOOD = 3
  EASY = 4


class ReviewType(enum.IntEnum):
  """How the card was being studied when the entry was recorded."""

  LEARNING = 0
  REVIEW = 1
  RELEARNING = 2
  FILTERED = 3
  MANUAL = 4
  RESCHEDULED = 5


class Review(RowModel[RevlogRow]):
  """One answer given to a card, as a row of the `revlog` table.

  Nothing reads a review back during study: the card carries its own scheduling state.
  This history is for FSRS, which recomputes a card's memory state from it, and the
  statistics screen.

  A review outlives the card it belongs to. Deleting a card leaves the reviews behind,
  so a revlog row with a card id naming a card the collection no longer holds is an
  ordinary state, but a state that this class cannot model.

  Attributes:
    id: Primary key, the millisecond timestamp when the answer was given. An import
      drops an incoming review when the target already holds that id. Defaults to
      a new monotonic id.
    card: The card that was answered. Only its id reaches the row.
    ease: The button pressed, one of `ReviewRating`. `ReviewRating.NONE` means no
      button was pressed, so the entry records something other than an answer.
      Important: FSRS accepts a value larger than 4 and schedules wrongly from it, so
      the “invalid value for enum” warning on this field marks a real error.
    interval: How long the scheduler waits before showing the card again, as this
      answer set it. It has two units: a negative value counts seconds for a learning
      step, a positive value counts days for a review interval. Default: 0.
    last_interval: How long the scheduler waited the previous time before showing the
      card again. It has two units: a negative value counts seconds for a learning
      step, a positive value counts days for a review interval. Default: 0.
    ease_factor: The card's ease after the answer, as a permille multiplier, so `2500`
      grows the next interval to 2.5 times the last. It's `0` while the card is still
      in learning and SM-2 has not set one. Under FSRS the column carries difficulty
      normalized to the [100, 1100] range so that a difficulty of zero stays
      distinguishable from a card that never had a factor. Default: 0.
    time_ms: Milliseconds taken to answer. Read only by the statistics. Default: 0.
    review_type: What kind of review it was. An int the enum does not name is carried
      through because Anki reads an unrecognized value as `ReviewType.LEARNING` instead
      of failing.
    update_sequence_n: The sync marker. Defaults to `-1`, which means the review has
      not reached the server; a non-negative value is the number of the sync that last
      carried it. An import stamps the target collection's own value, so it is not
      preserved across one.
  """

  id: Annotated[int, Id] = MonotonicId
  card: Annotated[Card, pyd.Field(exclude=True)]
  ease: Annotated[ReviewRating | int, LtRUnion, UInt8]
  interval: Annotated[int, Int32, pyd.Field(alias="ivl")] = 0
  last_interval: Annotated[int, Int32, pyd.Field(alias="lastIvl")] = 0
  ease_factor: Annotated[int, UInt32, pyd.Field(alias="factor")] = 0
  time_ms: Annotated[int, Bounded(0, 2**32 - 1, 0), pyd.Field(alias="time")] = 0
  review_type: Annotated[ReviewType | int, LtRUnion, Int64, pyd.Field(alias="type")]

  # Sync
  update_sequence_n: Annotated[int, UsnField] = -1

  # ------------------------------------ Validators ------------------------------------

  @pyd.model_validator(mode="after")
  def clamp_time(self) -> Self:
    """Clamp an answer duration above 2**31 - 1."""
    if _dbcheck_repairs_enabled.get():
      self.repair_time_above_ceiling()
    return self

  # ------------------------------------- Repairs --------------------------------------

  def repair_time_above_ceiling(self) -> Self:
    """Clamp an answer duration above 2**31 - 1, as Anki's database check does.

    Anki reads a duration up to 2**32 - 1 as it is, and only its database check clamps
    one above 2**31 - 1. A larger or negative duration never reaches this repair: Anki
    reads it as 0, and so does `Review`.

    `Review.clamp_time()` runs this while the database-check repairs are on.
    """
    if self.time_ms >= 2**31:
      warnings.warn(
        f"Review: entry {self.id} took {self.time_ms} ms, which is above 2**31 - 1, "
        "and was clamped to 2**31 - 1. Anki's database check makes the same clamp.",
        Genanki2Warning,
        stacklevel=2,
      )
      self.time_ms = 2**31 - 1
    return self

  # ----------------------------- Pre-serialization steps ------------------------------

  @classmethod
  def check_row_consistent_card_id(cls, row: RevlogRow, card: Card) -> None:
    """Reject a review row that names a different card than the one given.

    A `revlog` row names its card in the `cid` column, and `Review` drops the column
    and holds the card itself. A mismatch would move the review onto the given card's
    history instead of failing, and `Review.to_row()` would then write that card's id,
    so nothing downstream can tell the two apart again.

    Args:
      row: The `revlog` row.
      card: The card the row's `cid` names.

    Raises:
      CollectionReadError: the row's `cid` and the card's id differ.
    """
    if row["cid"] != card.id:
      raise CollectionReadError(
        f"Row check failed: card id {card.id} does not match the review row's card id "
        f"{row['cid']}."
      )

  def warn_rescheduled_typed_as_learning(self) -> Self:
    """Warn about a learning review that records no answer, which Anki retypes.

    A learning review that took no time and pressed no button is not a review. Anki's
    database check reads that combination as a reschedule and rewrites the type to
    `ReviewType.RESCHEDULED`.
    """
    if (
      self.review_type == ReviewType.LEARNING
      and self.time_ms == 0
      and self.ease == ReviewRating.NONE
    ):
      warnings.warn(
        f"Review: entry {self.id} is typed as a learning review but took no time and "
        "pressed no button. Anki's database check rewrites that combination to a "
        "reschedule. Either set `genanki2.ReviewType.RESCHEDULED` or add the time and "
        "button.",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  def warn_review_type_and_ease_disagree(self) -> Self:
    """Warn about a kind and a button that Anki never writes together.

    Answering a card records the button pressed and sets a review type (any except
    `ReviewType.MANUAL` and `ReviewType.RESCHEDULED`). Setting a due date or resetting
    a card records no button and sets `ReviewType.MANUAL` or `ReviewType.RESCHEDULED`.
    FSRS reads the button, not the type, and counts every entry above zero as a review,
    so a button on a manual entry adds an answer that was never given and a missing one
    hides an answer that was given. The database check repairs neither.
    """
    manual_or_rescheduled: bool = self.review_type in (
      ReviewType.MANUAL,
      ReviewType.RESCHEDULED,
    )
    if manual_or_rescheduled and self.ease != ReviewRating.NONE:
      warnings.warn(
        f"Review: entry {self.id} is typed as manual or rescheduled but records button "
        f"{int(self.ease)}. Anki writes no button for either kind, and FSRS counts any "
        "entry whose button is above zero as a review, so this one adds an answer that "
        "was never given.",
        Genanki2Warning,
        stacklevel=2,
      )

    rescheduled_typed_as_learning: bool = (
      self.review_type == ReviewType.LEARNING
      and self.time_ms == 0
      and self.ease == ReviewRating.NONE
    )
    if (
      not rescheduled_typed_as_learning  # avoid double warning
      and not manual_or_rescheduled
      and self.ease == ReviewRating.NONE
    ):
      warnings.warn(
        f"Review: entry {self.id} records no button, which Anki writes only for a "
        "manual or rescheduled entry. FSRS skips an entry whose button is zero, so "
        "this answer is missing from the card's history.",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  def warn_time_above_ceiling(self) -> Self:
    """Warn about an answer duration above 2**31 - 1, which was not clamped.

    Anki keeps the duration until its database check clamps it. `Review.clamp_time()`
    repairs it, but when repairs are skipped (inside
    `genanki2.dbcheck_repairs_disabled()`), this warning is emitted.
    """
    if self.time_ms >= 2**31:
      warnings.warn(
        f"Review: entry {self.id} took {self.time_ms} ms, which is above 2**31 - 1, "
        "and was not repaired. Anki's database check clamps it to 2**31 - 1. "
        "`Review.repair_time_above_ceiling()` makes the same repair.",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  @override
  def warn(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Warn about a review Anki accepts but reads as something else.

    Warnings can be turned off using `with genanki2.warnings_disabled():`.

    Args:
      recursive: Whether to warn about every model reachable from this one too, each
        once. If false, it only runs this model's own warnings. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.warn_rescheduled_typed_as_learning()
    self.warn_review_type_and_ease_disagree()
    self.warn_time_above_ceiling()
    self.warn_int_value_in_enum_field()
    return super().warn(recursive=recursive, context=context)

  # ---------------------------------- Serialization -----------------------------------

  @override
  @classmethod
  def from_row(cls, row: RevlogRow, card: Card) -> Self:  # pyright: ignore[reportIncompatibleMethodOverride]
    """Build a review entry from a `revlog` table row.

    The card can't come from the row, which carries only its id, so it is passed in
    and checked against it.

    Raises:
      CollectionReadError: the review's card id and `card.id` don't match.
    """
    cls.check_row_consistent_card_id(row, card)
    return super().from_row({**row, "card": card})

  @override
  def _row(self) -> RevlogRow:
    """Serialize the review as its `revlog` row with the card id, running no phase."""
    return {**super()._row(), "cid": self.card.id}

  @override
  def to_row(self, *, context: SerializationContext | None = None) -> RevlogRow:
    """Return a `revlog` row for this review entry, with the referenced card id.

    If no context is given, one is created with output
    `SerializationContext.Output.ROW` and passed to the phases.
    """
    return super().to_row(context=context)
