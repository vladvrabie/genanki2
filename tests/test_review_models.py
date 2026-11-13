"""Does a review survive the trip to the `revlog` table, and which of its columns are
readable data rather than something Anki refuses?

    uv run --with pytest pytest tests/test_review_models.py

The review history is what FSRS recomputes a card's memory state from, and what the
statistics screen reads. Nothing during study reads it back, so a collection can hold
entries no card explains: deleting a card leaves its reviews behind, and the only
`delete from revlog` in rslib is by id, for undo (`rslib/src/storage/revlog/mod.rs:110`).
An orphaned `cid` is an ordinary state of a real collection, not damage, which is why
nothing here checks an entry against the cards list.

`row_to_revlog_entry` (`rslib/src/storage/revlog/mod.rs:33-44`) reads every column with a
plain `?` except `time` and `type`, which use `unwrap_or_default()`. So a value out of
range makes the row unreadable for the first group, and a bound that raises matches. The
other two are read as 0 and `LEARNING` instead. A `time` outside the u32 range is
repaired to the same 0. A `time` from 2**31 to 2**32 - 1 is read as it is and clamped to
2**31 - 1 by the database check only (`rslib/src/storage/revlog/fix_props.sql`), so it is
an opt-out repair. An unknown `type` is carried through and warned about.

The models carry no anki dependency and neither does this file.
"""

import warnings

import pydantic as pyd
import pytest

from genanki2 import dbcheck_repairs_disabled
from genanki2.builtins import BASIC_NOTETYPE, DEFAULT_DECK, DEFAULT_DECK_CONFIG
from genanki2.errors import CollectionReadError, Genanki2Warning
from genanki2.models.card import Card
from genanki2.models.collection import Collection
from genanki2.models.config import ConfigEntry
from genanki2.models.note import Note
from genanki2.models.review import Review, ReviewRating, ReviewType
from genanki2.sqlite import AnkiSQLite

# Written collections carry it so that the writer's v1 scheduler warning stays out of
# the way of what these tests are about.
SCHEDULER_V2 = [ConfigEntry(key="schedVer", value=b"2")]

A_NOTE = Note(notetype=BASIC_NOTETYPE, fields=["front", "back"])
A_CARD = Card(note=A_NOTE, deck=DEFAULT_DECK)


def a_review(card: Card = A_CARD, **overrides) -> Review:
  defaults = {"ease": ReviewRating.GOOD, "type": ReviewType.REVIEW}
  return Review(card=card, **(defaults | overrides))


def a_collection(**overrides) -> Collection:
  """The chain a review sits at the end of, with the card it was given for."""
  defaults = {
    "deck_configs": [DEFAULT_DECK_CONFIG],
    "decks": [DEFAULT_DECK],
    "notetypes": [BASIC_NOTETYPE],
    "notes": [A_NOTE],
    "cards": [A_CARD],
    "config": SCHEDULER_V2,
  }
  return Collection(**(defaults | overrides))


# ----------------------------------- Row round trips ------------------------------------


def test_the_row_survives_a_round_trip():
  row = a_review(ivl=30, lastIvl=10, factor=2500, time=4200, usn=6).to_row()
  assert Review.from_row(row, card=A_CARD).to_row() == row


def test_the_card_id_comes_from_the_card_and_not_from_the_caller():
  """`Review` drops the `cid` column and holds the card, so the id cannot drift from
  the object the collection writes."""
  assert a_review().to_row()["cid"] == A_CARD.id


def test_a_row_belonging_to_another_card_is_rejected():
  """Accepting it would move the entry onto the given card's history, and `to_row()`
  would then write that card's id, so nothing downstream could tell the two apart."""
  row = a_review().to_row() | {"cid": A_CARD.id + 1}
  with pytest.raises(CollectionReadError, match="card id"):
    Review.from_row(row, card=A_CARD)


def test_every_column_the_model_renames_is_read_under_its_column_name():
  row = a_review(ivl=30, lastIvl=10, factor=2500, time=4200).to_row()
  review = Review.from_row(row, card=A_CARD)
  assert (review.interval, review.last_interval) == (30, 10)
  assert (review.ease_factor, review.time_ms) == (2500, 4200)


# ------------------------------------- The intervals ------------------------------------


@pytest.mark.parametrize("interval", [-600, 0, 30])
def test_an_interval_carries_two_units_in_one_column(interval):
  """Negative counts seconds for a learning step and positive counts days for a review
  interval, so neither sign is a repair to make."""
  assert a_review(ivl=interval).interval == interval


@pytest.mark.parametrize("column", ["ivl", "lastIvl"])
def test_an_interval_outside_an_i32_is_refused(column):
  """`fix_props.sql` clamps both into an `i32`, and Anki cannot read the row until it
  does."""
  with pytest.raises(pyd.ValidationError):
    a_review(**{column: 2**31})


def test_an_ease_factor_above_the_sm2_range_is_accepted():
  """Under FSRS the column carries difficulty instead, and a real collection reaches
  well past the 2500 an SM-2 card starts at."""
  assert a_review(factor=2950).ease_factor == 2950


# -------------------------------- The two swallowed columns -----------------------------


def test_a_review_type_the_enum_does_not_name_is_carried_through():
  """Anki reads an unrecognized value as `LEARNING` rather than failing, so rejecting
  one would refuse a collection Anki opens."""
  review = a_review(type=99)
  assert review.review_type == 99
  with pytest.warns(Genanki2Warning, match="ReviewType"):
    review.warn()


@pytest.mark.parametrize("time_ms", [-1, 2**32])
def test_a_duration_outside_u32_is_read_as_zero(time_ms):
  """Anki's u32 read fails and falls back to 0, so 0 is what every Anki code path sees.
  The repair is mandatory and silent."""
  with warnings.catch_warnings():
    warnings.simplefilter("error", Genanki2Warning)
    assert a_review(time=time_ms).time_ms == 0


def test_a_duration_above_the_ceiling_is_clamped_with_a_warning():
  with pytest.warns(Genanki2Warning, match="was clamped"):
    review = a_review(time=2**31)
  assert review.time_ms == 2**31 - 1


def test_a_duration_above_the_ceiling_is_kept_inside_dbcheck_repairs_disabled():
  with dbcheck_repairs_disabled():
    review = a_review(time=2**31)
  assert review.time_ms == 2**31
  with pytest.warns(Genanki2Warning, match="was not repaired"):
    review.warn_time_above_ceiling()


@pytest.mark.parametrize("time_ms", [0, 4200, 2**31 - 1])
def test_a_duration_inside_the_range_is_not_reported(time_ms):
  with warnings.catch_warnings():
    warnings.simplefilter("error", Genanki2Warning)
    a_review(time=time_ms).warn_time_above_ceiling()


# ------------------------------- The shape of a reschedule ------------------------------


def test_a_learning_review_that_records_no_answer_is_reported():
  """The database check reads that combination as a reschedule and rewrites the type,
  so the entry does not stay what it was written as."""
  entry = a_review(type=ReviewType.LEARNING, ease=ReviewRating.NONE, time=0)
  with pytest.warns(Genanki2Warning, match="reschedule"):
    entry.warn_rescheduled_typed_as_learning()


def test_a_reschedule_typed_as_one_is_not_reported():
  entry = a_review(type=ReviewType.MANUAL, ease=ReviewRating.NONE, time=0)
  with warnings.catch_warnings():
    warnings.simplefilter("error", Genanki2Warning)
    entry.warn_rescheduled_typed_as_learning()


def test_a_learning_review_that_records_an_answer_is_not_reported():
  entry = a_review(type=ReviewType.LEARNING, ease=ReviewRating.GOOD, time=4200)
  with warnings.catch_warnings():
    warnings.simplefilter("error", Genanki2Warning)
    entry.warn_rescheduled_typed_as_learning()


# ---------------------------- The kind against the button --------------------------------


def test_a_manual_entry_carrying_a_button_is_reported():
  """FSRS branches on the button and not on the kind, so this one is counted as an
  answer the user never gave."""
  entry = a_review(type=ReviewType.MANUAL, ease=ReviewRating.GOOD)
  with pytest.warns(Genanki2Warning, match="records button 3"):
    entry.warn_review_type_and_ease_disagree()


def test_a_rescheduled_entry_carrying_a_button_is_reported():
  entry = a_review(type=ReviewType.RESCHEDULED, ease=ReviewRating.AGAIN)
  with pytest.warns(Genanki2Warning, match="records button 1"):
    entry.warn_review_type_and_ease_disagree()


def test_an_answered_kind_recording_no_button_is_reported():
  """The other direction: FSRS skips the entry, so the answer is missing from the
  card's history."""
  entry = a_review(type=ReviewType.REVIEW, ease=ReviewRating.NONE, time=4200)
  with pytest.warns(Genanki2Warning, match="records no button"):
    entry.warn_review_type_and_ease_disagree()


def test_the_reschedule_typed_as_learning_is_left_to_its_own_report():
  """`warn_rescheduled_typed_as_learning()` names the repair Anki makes to exactly this
  combination, so reporting it twice would say the same thing about one row."""
  entry = a_review(type=ReviewType.LEARNING, ease=ReviewRating.NONE, time=0)
  with warnings.catch_warnings():
    warnings.simplefilter("error", Genanki2Warning)
    entry.warn_review_type_and_ease_disagree()


def test_a_manual_entry_recording_no_button_is_not_reported():
  entry = a_review(type=ReviewType.MANUAL, ease=ReviewRating.NONE)
  with warnings.catch_warnings():
    warnings.simplefilter("error", Genanki2Warning)
    entry.warn_review_type_and_ease_disagree()


def test_an_answered_kind_carrying_a_button_is_not_reported():
  entry = a_review(type=ReviewType.REVIEW, ease=ReviewRating.EASY)
  with warnings.catch_warnings():
    warnings.simplefilter("error", Genanki2Warning)
    entry.warn_review_type_and_ease_disagree()


# ------------------------------------- Sync columns -------------------------------------


def test_a_fresh_review_has_not_reached_the_server():
  assert a_review().update_sequence_n == -1


def test_a_synced_value_is_accepted_alongside_the_pending_marker():
  """A real table holds both: a pending marker is not a sign the collection was written
  by something other than Anki."""
  collection = a_collection(reviews=[a_review(usn=-1), a_review(usn=164)])
  assert collection.check() is collection


# ---------------------------------- The collection lists --------------------------------


def test_two_reviews_sharing_an_id_are_refused():
  """`id` is the primary key, and an import drops an incoming entry whose id is taken
  rather than renumbering it (`add_revlog_entry_if_unique_undoable` passes
  `uniquify: false`), so the second entry is history that quietly does not arrive."""
  collection = a_collection(reviews=[a_review(id=1), a_review(id=1)])
  with pytest.raises(ValueError, match="reviews"):
    collection.check_unique_ids()


def test_the_answered_card_reaches_the_collection_through_its_review():
  collection = Collection(reviews=[a_review()])
  assert collection.collect_cards().cards == [A_CARD]


def test_an_entry_whose_card_the_collection_does_not_hold_is_accepted():
  """A collection carrying entries for deleted cards is what a real one looks like, so
  refusing them would refuse a v18 collection Anki wrote."""
  collection = a_collection(cards=[], reviews=[a_review()])
  assert collection.check() is collection


# ------------------------------------ Serialization -------------------------------------


def test_a_review_reaches_the_table(tmp_path):
  review = a_review(ivl=30, factor=2500, time=4200)
  path = tmp_path / "collection.anki2"
  a_collection(reviews=[review]).write_sqlite(path)
  with AnkiSQLite(path) as db:
    assert db.connection.execute("select * from revlog").fetchall() == [review.to_row()]


def test_a_collection_invents_no_reviews(tmp_path):
  path = tmp_path / "collection.anki2"
  a_collection().write_sqlite(path)
  with AnkiSQLite(path) as db:
    assert db.connection.execute("select * from revlog").fetchall() == []
