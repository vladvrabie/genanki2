"""Which rows with future ids does a collection name, and what does each write say?

    uv run --with pytest pytest tests/test_future_ids.py

A note, card or review id doubles as the millisecond the row was created, and Anki calls
an id more than a day ahead of the current time invalid. Both of its repairs run
`maybe_fix_invalid_ids` (`rslib/src/dbcheck.rs`), which renumbers every such row from
the current time onwards and rewrites the ids its cards and reviews name. What differs
is when. A merge runs it on its copy of the package before reading anything, so the
dates the user ends up with are not the ones the package carries. A restore repairs
nothing, so the rows keep their ids until the user runs the database check.

`Collection.warn_future_ids()` lists the rows and words the outcome by the write's
output: the import for an apkg, the database check for a colpkg, both without a context.
Neither is an error: the file imports either way, and the ids belong to the caller.
"""

import warnings

import pytest

from genanki2.builtins import BASIC_NOTETYPE, DEFAULT_DECK
from genanki2.errors import Genanki2Warning
from genanki2.models.card import Card
from genanki2.models.collection import Collection
from genanki2.models.config import ConfigEntry
from genanki2.models.note import Note
from genanki2.models.package import CollectionPackage, Package
from genanki2.models.review import Review, ReviewRating, ReviewType
from genanki2.time import time_ms

DAY_MS = 24 * 60 * 60 * 1000
PAST_TOMORROW = 2 * DAY_MS
INSIDE_THE_DAY = 23 * 60 * 60 * 1000

PREFIX = "Collection: some ids are more than a day in the future."
APKG_OUTCOME = "An apkg import renumbers them from the import time onwards, so"
COLPKG_OUTCOME = "A collection restore keeps them until the user runs the database"
BOTH_OUTCOMES = ("An apkg import renumbers them", "A restored colpkg keeps them")

# Written collections carry it so that the writer's v1 scheduler warning stays out of
# the way of what these tests are about.
SCHEDULER_V2 = [ConfigEntry(key="schedVer", value=b"2")]


def a_collection(note=0, card=0, review=0, **overrides) -> Collection:
  """One note, its card and a review of it, each id offset from now by its argument."""
  the_note = Note(
    notetype=BASIC_NOTETYPE, fields=["front", "back"], id=time_ms() + note
  )
  the_card = Card(note=the_note, deck=DEFAULT_DECK, id=time_ms() + card)
  the_review = Review(
    card=the_card,
    ease=ReviewRating.GOOD,
    type=ReviewType.REVIEW,
    id=time_ms() + review,
  )
  return Collection(
    config=list(SCHEDULER_V2),
    notes=[the_note],
    cards=[the_card],
    reviews=[the_review],
    **overrides,
  )


def an_orphan_review_row(offset: int) -> dict:
  """A raw revlog row naming a card no collection holds, which is how the reader keeps
  the reviews of a deleted card."""
  return {
    "id": time_ms() + offset,
    "cid": 1,
    "usn": -1,
    "ease": 3,
    "ivl": 1,
    "lastIvl": 1,
    "factor": 2500,
    "time": 1000,
    "type": 1,
  }


def future_id_warnings(action) -> list[str]:
  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always", Genanki2Warning)
    action()
  return [str(w.message) for w in caught if str(w.message).startswith(PREFIX)]


# ------------------------------------ What is named -----------------------------------


@pytest.mark.parametrize("kind", ["notes", "cards", "reviews"])
def test_a_row_dated_past_tomorrow_is_named_under_its_kind(kind):
  collection = a_collection(**{kind[:-1]: PAST_TOMORROW})
  row_id = getattr(collection, kind)[0].id
  with pytest.warns(Genanki2Warning, match=f"{kind}: {row_id}"):
    collection.warn_future_ids()


def test_every_kind_is_listed_in_one_warning():
  """One warning carries all three, so a caller sees the whole renumbering at once."""
  collection = a_collection(
    note=PAST_TOMORROW, card=PAST_TOMORROW, review=PAST_TOMORROW
  )
  messages = future_id_warnings(collection.warn_future_ids)

  assert len(messages) == 1
  assert all(f"{kind}:" in messages[0] for kind in ("notes", "cards", "reviews"))


def test_a_kind_holding_nothing_future_is_left_out_of_the_listing():
  collection = a_collection(note=PAST_TOMORROW)
  with pytest.warns(Genanki2Warning, match="notes:") as caught:
    collection.warn_future_ids()
  assert "cards:" not in str(caught.list[0].message)


def test_ids_inside_the_day_warn_about_nothing(recwarn):
  """The day is Anki's own allowance: its importer and its database check both compare
  against the current time plus twenty-four hours."""
  collection = a_collection(
    note=INSIDE_THE_DAY, card=INSIDE_THE_DAY, review=INSIDE_THE_DAY
  )
  assert collection.warn_future_ids() is collection
  assert recwarn.list == []


def test_an_orphan_row_dated_past_tomorrow_is_not_named(recwarn):
  """An orphan is a raw row, with no model to name in a listing. A merge never carries
  one, and a restore carries it but the database check renumbers it along with the rest,
  so neither importer leaves the user with the id the package holds."""
  collection = a_collection(orphan_reviews=[an_orphan_review_row(PAST_TOMORROW)])
  collection.warn_future_ids()
  assert recwarn.list == []


# ------------------------------ What each write is told -------------------------------


@pytest.mark.parametrize(
  ("write", "expected"),
  [
    pytest.param(
      lambda col, d: Package(collection=col).write_apkg(d / "deck.apkg"),
      (APKG_OUTCOME,),
      id="apkg",
    ),
    pytest.param(
      lambda col, d: CollectionPackage(collection=col).write_colpkg(
        d / "collection.colpkg"
      ),
      (COLPKG_OUTCOME,),
      id="colpkg",
    ),
    pytest.param(
      lambda col, d: col.write_sqlite(d / "collection.anki2"),
      BOTH_OUTCOMES,
      id="sqlite",
    ),
    pytest.param(lambda col, d: col.warn(), BOTH_OUTCOMES, id="no context"),
  ],
)
def test_the_warning_names_what_happens_to_the_write(tmp_path, write, expected):
  """A restore repairs nothing, so a message about the import time would name a moment
  at which nothing happens. Through the package writers, the context has to cross the
  package's override to reach the collection."""
  collection = a_collection(note=PAST_TOMORROW)
  messages = future_id_warnings(lambda: write(collection, tmp_path))

  assert len(messages) == 1
  assert all(outcome in messages[0] for outcome in expected)
  assert f"notes: {collection.notes[0].id}" in messages[0]
