"""Do the card repairs match Anki's, and do they terminate?

    uv run --with pytest pytest tests/test_card_models.py

`due` means a different thing in each queue (`rslib/src/card/mod.rs:44-63`): a position for
a new card, a day count for a review card, a unix timestamp for a learning or preview card.
Anki's db check has one repair per meaning, and each is guarded by the columns that say
which meaning applies, so a repair aimed at one queue cannot reach the others. Dropping
those guards is how a learning card's timestamp gets rewritten as a position.

`fix_due_new.sql` wraps a new card's position of 2,000,000 or more into the range from
1,000,000 to 1,999,999. Its guard reads `due >= 1000000`, but the wrap leaves every
position in that range unchanged, so the ceiling is 2,000,000. `fix_odue.sql` clears an `odue` that no `odid` explains:
the pair records where a card came from before a filtered deck took it, so one without the
other is orphaned. `fix_due_other.sql` resets a review card holding a timestamp to today,
which needs the collection's day count and so cannot happen here; the card rejects the
value instead. The check's filtered-card pass (`rslib/src/dbcheck.rs:208-231`) clears the
pair from the other side, for a card carrying an `odid` while sitting in a deck that is
not filtered. That one reads the card's current deck, which the card does not own, so
`Card.prepare()` runs it a second time.

`fix_ordinal.sql` clamps `ord` to 30,000 and `fix_ivl.sql` clamps `ivl` to 2**31 - 1,
while the card reader takes both up to the limit of their types, a u16 and a u32
(`rslib/src/card/mod.rs:84-90`). So a value between the two is clamped here, and one past
the type is refused, since Anki cannot read the row.

The repairs mutate the model from a validator, and `validate_assignment` sends every such
mutation back through validation. That terminates only if the repaired value no longer
matches the repair's guard: a wrapped position is under the ceiling and a cleared original
due is 0, so the second pass assigns nothing.
"""

import warnings

import pydantic as pyd
import pytest

from genanki2 import dbcheck_repairs_disabled
from genanki2.builtins import BASIC_NOTETYPE, DEFAULT_DECK
from genanki2.errors import CollectionReadError, Genanki2Warning
from genanki2.models.card import Card, CardQueue, CardType
from genanki2.models.deck import Deck, DeckFilteredKind, DeckNormalKind
from genanki2.models.note import Note

# The position from which Anki wraps, and a position several multiples past it.
POSITION_CEILING = 2_000_000
A_POSITION_PAST_THE_CEILING = 2_500_005
THE_SAME_POSITION_WRAPPED = 1_500_005

A_TIMESTAMP_IN_2025 = 1_750_000_000

A_NOTE = Note(notetype=BASIC_NOTETYPE, fields=["front", "back"])


def a_card(deck: Deck = DEFAULT_DECK, **overrides) -> Card:
  return Card(note=A_NOTE, deck=deck, **overrides)


def a_filtered_deck(name: str = "Filtered") -> Deck:
  """A new deck on each call, so a test can turn it normal without touching a shared one."""
  return Deck(name=name, kind=DeckFilteredKind())


# ----------------------------------- Due, by queue ------------------------------------


def test_a_new_card_position_past_the_ceiling_wraps():
  assert a_card(due=A_POSITION_PAST_THE_CEILING).due == THE_SAME_POSITION_WRAPPED


def test_wrapping_a_position_on_assignment_terminates():
  card = a_card()
  card.due = A_POSITION_PAST_THE_CEILING  # re-validates, so the wrap has to settle
  assert card.due == THE_SAME_POSITION_WRAPPED


def test_a_wrapped_position_is_left_alone():
  assert a_card(due=THE_SAME_POSITION_WRAPPED).due == THE_SAME_POSITION_WRAPPED


def test_a_position_below_the_ceiling_is_left_alone():
  assert a_card(due=POSITION_CEILING - 1).due == POSITION_CEILING - 1


def test_the_ceiling_itself_wraps_to_one_million():
  assert a_card(due=POSITION_CEILING).due == 1_000_000


def test_the_default_position_is_left_alone():
  assert a_card().due == 0


def test_a_preview_card_keeps_its_timestamp():
  card = a_card(
    type=CardType.NEW, queue=CardQueue.PREVIEW_REPEAT, due=A_TIMESTAMP_IN_2025
  )
  assert card.to_row()["due"] == A_TIMESTAMP_IN_2025


def test_a_learning_card_keeps_its_timestamp():
  card = a_card(
    type=CardType.LEARNING, queue=CardQueue.LEARNING, due=A_TIMESTAMP_IN_2025
  )
  assert card.to_row()["due"] == A_TIMESTAMP_IN_2025


def test_a_review_card_holding_a_timestamp_is_rejected():
  card = a_card(type=CardType.REVIEW, queue=CardQueue.REVIEW, due=A_TIMESTAMP_IN_2025)
  with pytest.raises(ValueError, match="counts due in days"):
    card.to_row()


def test_a_suspended_review_card_holding_a_timestamp_is_rejected():
  """The queue says nothing here, the type does: Anki reads a review card's due as a day
  count wherever the card sits, so an import shifts this one too."""
  card = a_card(
    type=CardType.REVIEW, queue=CardQueue.SUSPENDED, due=A_TIMESTAMP_IN_2025
  )
  with pytest.raises(ValueError, match="counts due in days"):
    card.to_row()


def test_a_day_learning_card_holding_a_timestamp_is_rejected():
  """A learning card that crossed a day boundary counts days like a review card does."""
  card = a_card(
    type=CardType.LEARNING, queue=CardQueue.DAY_LEARNING, due=A_TIMESTAMP_IN_2025
  )
  with pytest.raises(ValueError, match="counts due in days"):
    card.to_row()


def test_a_new_card_in_the_review_queue_is_wrapped_and_then_rejected():
  """The wrap runs first and leaves the position above the day threshold. Anki resets
  such a card to today, which needs the collection's day count, so the card is refused
  instead."""
  card = a_card(queue=CardQueue.REVIEW, due=A_POSITION_PAST_THE_CEILING)
  assert card.due == THE_SAME_POSITION_WRAPPED
  with pytest.raises(ValueError, match="counts due in days"):
    card.to_row()


def test_a_review_card_day_count_is_accepted():
  card = a_card(type=CardType.REVIEW, queue=CardQueue.REVIEW, due=120)
  assert card.to_row()["due"] == 120


def test_a_day_learning_card_day_count_is_accepted():
  card = a_card(type=CardType.LEARNING, queue=CardQueue.DAY_LEARNING, due=120)
  assert card.to_row()["due"] == 120


def test_a_review_card_waiting_in_a_preview_deck_keeps_its_timestamp():
  """Answering a card in a filtered deck that does not reschedule moves it to the
  preview queue with a timestamp and leaves its type alone (`apply_preview_state`,
  `rslib/src/scheduler/answering/preview.rs:23-29`). The queue decides what `due`
  means, and the type only for a card out of rotation."""
  card = a_card(
    deck=a_filtered_deck(),
    type=CardType.REVIEW,
    queue=CardQueue.PREVIEW_REPEAT,
    due=A_TIMESTAMP_IN_2025,
    odid=1_234,
    odue=120,
    ivl=10,
    factor=2500,
  )
  assert card.to_row()["due"] == A_TIMESTAMP_IN_2025


# -------------------------------- Due outside 32 bits ---------------------------------


@pytest.mark.parametrize("column", ["due", "odue"])
@pytest.mark.parametrize("value", [2**31, -(2**31) - 1])
def test_a_due_outside_32_bits_is_read_as_zero(column, value):
  """Anki reads both columns with a fallback to 0 (`rslib/src/storage/card/mod.rs:84`,
  `:90`), and old collections hold such values. Refusing the row would refuse the whole
  collection."""
  row = a_card().to_row() | {column: value}
  assert Card.from_row(row, note=A_NOTE, deck=DEFAULT_DECK).to_row()[column] == 0


@pytest.mark.parametrize("value", [-(2**31), 2**31 - 1])
def test_an_original_due_on_the_ends_of_32_bits_is_kept(value):
  """A new card's original due is left alone by every other repair."""
  assert a_card(odue=value).original_due == value


# -------------------------------- Original due and deck --------------------------------


def test_a_learning_cards_original_due_without_an_original_deck_is_cleared():
  assert a_card(type=CardType.LEARNING, odue=42, odid=0).original_due == 0


def test_a_review_cards_original_due_without_an_original_deck_is_cleared():
  assert a_card(queue=CardQueue.REVIEW, odue=42, odid=0).original_due == 0


def test_clearing_an_original_due_on_assignment_terminates():
  card = a_card(type=CardType.LEARNING)
  card.original_due = 42
  assert card.original_due == 0


def test_an_original_due_explained_by_an_original_deck_is_kept():
  card = a_card(deck=a_filtered_deck(), type=CardType.LEARNING, odue=42, odid=1_234)
  assert card.original_due == 42


def test_the_pair_is_kept_in_a_filtered_deck(recwarn):
  card = a_card(deck=a_filtered_deck(), type=CardType.LEARNING, odue=42, odid=1_234)
  assert (card.original_deck, card.original_due) == (1_234, 42)
  assert len(recwarn) == 0


def test_the_pair_is_cleared_in_a_normal_deck():
  with pytest.warns(UserWarning, match="not filtered") as warnings:
    card = a_card(type=CardType.LEARNING, odue=42, odid=1_234)
  assert (card.original_deck, card.original_due) == (0, 0)
  # One warning, not two: the pair is written in one step, so the card is never seen
  # holding an original due with no original deck, which is the other repair's case.
  assert len(warnings) == 1


def test_clearing_the_pair_on_assignment_terminates():
  card = a_card(type=CardType.LEARNING)
  with pytest.warns(UserWarning, match="not filtered"):
    card.original_deck = 1_234  # re-validates, so the clear has to settle
  assert card.original_deck == 0


def test_a_deck_turned_normal_after_the_card_was_built_still_clears_the_pair():
  deck = a_filtered_deck()
  card = a_card(deck=deck, type=CardType.LEARNING, odue=42, odid=1_234)
  deck.kind = DeckNormalKind()  # the deck owns its kind, so no card validator fires
  assert (card.original_deck, card.original_due) == (1_234, 42)
  with pytest.warns(UserWarning, match="not filtered"):
    row = card.to_row()
  assert (row["odid"], row["odue"]) == (0, 0)


def test_an_original_deck_held_as_an_object_is_read_through_its_id():
  home = Deck(name="Home")
  card = a_card(deck=a_filtered_deck(), odid=home)
  assert card.original_deck_id == home.id
  assert card.to_row()["odid"] == home.id


def test_an_original_deck_held_as_an_object_is_cleared_like_an_id():
  with pytest.warns(UserWarning, match="not filtered"):
    card = a_card(type=CardType.LEARNING, odue=42, odid=Deck(name="Home"))
  assert (card.original_deck, card.original_due) == (0, 0)


def test_a_new_cards_original_due_is_kept():
  # Anki repairs only the learning and review states, so this one is left as it is.
  card = a_card(type=CardType.NEW, queue=CardQueue.NEW, odue=42, odid=0)
  assert card.original_due == 42


def test_a_review_cards_original_due_holding_a_timestamp_is_rejected():
  """Emptying the filtered deck copies `odue` into `due`, so the wrong value outlives
  the deck that explains it."""
  card = a_card(
    deck=a_filtered_deck(),
    odid=Deck(name="Home"),
    type=CardType.REVIEW,
    queue=CardQueue.REVIEW,
    due=120,
    odue=A_TIMESTAMP_IN_2025,
  )
  with pytest.raises(ValueError, match="counts original due in days"):
    card.to_row()


def test_a_learning_cards_original_due_keeps_its_timestamp():
  """The narrower condition: only a review card's `odue` counts days, so a learning
  card gathered into a filtered deck keeps the timestamp it came in with."""
  card = a_card(
    deck=a_filtered_deck(),
    odid=Deck(name="Home"),
    type=CardType.LEARNING,
    queue=CardQueue.LEARNING,
    due=A_TIMESTAMP_IN_2025,
    odue=A_TIMESTAMP_IN_2025,
  )
  assert card.to_row()["odue"] == A_TIMESTAMP_IN_2025


# ------------------------ Template ordinal and interval ceilings -------------------------

# Each row: the column alias, the attribute, the database check's ceiling, and the first
# value past the type Anki reads into.
CEILINGS = [
  pytest.param("ord", "template_ord", 30_000, 2**16, id="template_ord"),
  pytest.param("ivl", "interval_days", 2**31 - 1, 2**32, id="interval_days"),
]


@pytest.mark.parametrize(("alias", "attr", "ceiling", "past_type"), CEILINGS)
def test_a_value_above_the_ceiling_is_clamped_with_a_warning(
  alias, attr, ceiling, past_type
):
  for value in (ceiling + 1, past_type - 1):
    with pytest.warns(Genanki2Warning, match="was clamped"):
      card = a_card(**{alias: value})
    assert getattr(card, attr) == ceiling


@pytest.mark.parametrize(("alias", "attr", "ceiling", "past_type"), CEILINGS)
def test_clamping_on_assignment_terminates(alias, attr, ceiling, past_type):
  card = a_card()
  with pytest.warns(Genanki2Warning, match="was clamped"):
    setattr(card, attr, ceiling + 1)
  assert getattr(card, attr) == ceiling


@pytest.mark.parametrize(("alias", "attr", "ceiling", "past_type"), CEILINGS)
def test_the_ceiling_itself_is_left_alone(alias, attr, ceiling, past_type):
  with warnings.catch_warnings():
    warnings.simplefilter("error", Genanki2Warning)
    assert getattr(a_card(**{alias: ceiling}), attr) == ceiling


@pytest.mark.parametrize(("alias", "attr", "ceiling", "past_type"), CEILINGS)
def test_a_value_past_the_type_anki_reads_is_refused(alias, attr, ceiling, past_type):
  with pytest.raises(pyd.ValidationError):
    a_card(**{alias: past_type})


@pytest.mark.parametrize(("alias", "attr", "ceiling", "past_type"), CEILINGS)
def test_a_value_above_the_ceiling_is_kept_inside_dbcheck_repairs_disabled(
  alias, attr, ceiling, past_type
):
  with dbcheck_repairs_disabled():
    card = a_card(**{alias: ceiling + 1})
  assert getattr(card, attr) == ceiling + 1
  with pytest.warns(Genanki2Warning, match="was not repaired"):
    getattr(card, f"warn_{attr}_above_ceiling")()


# ------------------------------------ The ease factor -----------------------------------


def test_a_review_card_without_an_ease_factor_is_reported():
  """Zero is the model default, so it is what a hand-built review card carries unless
  the caller supplies the factor the card would have graduated with."""
  card = a_card(type=CardType.REVIEW, queue=CardQueue.REVIEW, due=120, ivl=30)
  with pytest.warns(UserWarning, match="ease factor"):
    card.to_row()


def test_a_relearning_card_without_an_ease_factor_is_reported():
  """A relearning card is scheduled through a review state too, so it reads the same
  column."""
  card = a_card(
    type=CardType.RELEARNING, queue=CardQueue.LEARNING, due=A_TIMESTAMP_IN_2025
  )
  with pytest.warns(UserWarning, match="ease factor"):
    card.to_row()


def test_a_review_card_carrying_an_ease_factor_is_not_reported(recwarn):
  card = a_card(
    type=CardType.REVIEW, queue=CardQueue.REVIEW, due=120, ivl=30, factor=2500
  )
  card.to_row()
  assert len(recwarn) == 0


def test_a_learning_card_without_an_ease_factor_is_not_reported(recwarn):
  """Anki leaves the column at zero until the card graduates, so this is the ordinary
  state rather than a gap."""
  card = a_card(
    type=CardType.LEARNING, queue=CardQueue.LEARNING, due=A_TIMESTAMP_IN_2025
  )
  card.to_row()
  assert len(recwarn) == 0


def test_a_new_card_without_an_ease_factor_is_not_reported(recwarn):
  a_card().to_row()
  assert len(recwarn) == 0


# ----------------------------------- Serialization -------------------------------------


def test_a_row_round_trips():
  card = a_card(
    type=CardType.REVIEW, queue=CardQueue.REVIEW, due=120, ivl=30, factor=2500, reps=7
  )
  row = card.to_row()
  assert Card.from_row(row, note=A_NOTE, deck=DEFAULT_DECK).to_row() == row


def test_a_row_belonging_to_another_note_is_rejected():
  row = a_card().to_row() | {"nid": A_NOTE.id + 1}
  with pytest.raises(CollectionReadError, match="note id"):
    Card.from_row(row, note=A_NOTE, deck=DEFAULT_DECK)


def test_a_row_belonging_to_another_deck_is_rejected():
  row = a_card().to_row() | {"did": DEFAULT_DECK.id + 1}
  with pytest.raises(CollectionReadError, match="deck id"):
    Card.from_row(row, note=A_NOTE, deck=DEFAULT_DECK)


def test_a_card_without_a_template_is_rejected():
  # Anki deletes such a card outright (`rslib/src/dbcheck.rs:376-391`).
  card = a_card(ord=len(BASIC_NOTETYPE.templates))
  with pytest.raises(ValueError, match="template index"):
    card.to_row()


# ---------------------------------- The original deck ---------------------------------


def test_a_card_in_a_filtered_deck_with_no_original_deck_is_accepted():
  """A card the scheduler could not answer, and still not this model's to refuse: what
  Anki does with a package holding one is convert the deck, which
  `Collection.warn_filtered_decks_become_normal()` reports and only a collection sees."""
  card = a_card(deck=a_filtered_deck())
  assert card.to_row()["odid"] == 0


def test_a_card_in_a_filtered_deck_with_an_original_deck_is_accepted():
  """The state every card in a filtered deck is in, so the check has to leave it be."""
  card = a_card(deck=a_filtered_deck(), odid=Deck(name="Home"), odue=42)
  assert card.to_row()["odue"] == 42


def test_a_card_whose_original_deck_is_filtered_is_rejected():
  """The original deck is where the card returns to, so it has to be a normal one."""
  card = a_card(deck=a_filtered_deck(), odid=a_filtered_deck("Other"), odue=42)
  with pytest.raises(ValueError, match="names filtered deck 'Other'"):
    card.to_row()
