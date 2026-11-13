"""Do rows Anki wrote survive a trip through the models unchanged?

    uv run --with pytest pytest tests/test_real_collection_rows.py

Every other test in the suite builds its rows from the models and reads them back, so
what it pins is that genanki2 agrees with itself. These three tables are the ones
genanki2 never writes from scratch: an apkg carries no `tags` and no `graves`, and an
import rebuilds the registry and drops the deletion log, so the only way any of them
reaches the library is a collection somebody else wrote. Rows out of such a collection
are the only input that can show a model dropping a column, reordering a list or
repairing a value that did not need it.

`tests/fixtures/real_tables.anki2` is a trimmed copy of a real AnkiDroid collection,
exported as a colpkg because that is the only export carrying all twelve tables. The
trim keeps all 28 tags, graves of all three kinds (some naming a row the fixture still
holds, some naming nothing), and revlog rows chosen to cover every review kind and
button the collection contains, both interval signs, both sync states, and ten entries
whose card was deleted. The notes, cards, decks and notetypes are the ones those revlog
rows reference, since a `Review` cannot be built without the card it names.

The copy is anonymized. The note text is placeholder words, saved through Anki so that
`sfld` and `csum` are Anki's own. Media names, guids and tags are replaced, as are the
names of the decks, the presets and the custom notetypes, and those notetypes' fields,
templates, markup and CSS. Every time, and every id that is a time, went through one
order-preserving map into 2026, in the columns, the protobuf blobs and the config alike,
so each reference still finds its row. The timezone offsets, the decks' study
statistics and the preset options are replaced too, and so is every scheduling number:
the reviews' durations, intervals and ease factors, and the cards' positions, due days,
intervals, ease factors, review counts and lapse counts. What the tests read is
unchanged: the review kinds and buttons, the interval signs, the sync states, the
graves and the orphans.

The collapsed state is the one thing the fixture cannot vary: all 28 tags in the source
collection are collapsed, so the expanded case is covered by a constructed tag in
`test_tag_models.py` instead.

The models carry no anki dependency and neither does this file.
"""

import pathlib

import pytest

from genanki2.models.card import Card
from genanki2.models.deck import Deck
from genanki2.models.grave import Grave
from genanki2.models.note import Note
from genanki2.models.notetype import Notetype
from genanki2.models.review import Review
from genanki2.models.tag import Tag
from genanki2.sqlite import AnkiSQLite

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "real_tables.anki2"


@pytest.fixture(scope="session")
def rows() -> dict[str, list[dict]]:
  """Every row of the trimmed collection, keyed by table name."""
  with AnkiSQLite(FIXTURE) as db:
    return {table: db.read_table(table) for table in AnkiSQLite.Table}


@pytest.fixture(scope="session")
def notetypes(rows) -> dict[int, Notetype]:
  return {
    row["id"]: Notetype.from_rows(
      row,
      [field for field in rows["fields"] if field["ntid"] == row["id"]],
      [template for template in rows["templates"] if template["ntid"] == row["id"]],
    )
    for row in rows["notetypes"]
  }


@pytest.fixture(scope="session")
def decks(rows) -> dict[int, Deck]:
  return {row["id"]: Deck.from_row(row) for row in rows["decks"]}


@pytest.fixture(scope="session")
def notes(rows, notetypes) -> dict[int, Note]:
  return {row["id"]: Note.from_row(row, notetypes[row["mid"]]) for row in rows["notes"]}


@pytest.fixture(scope="session")
def cards(rows, notes, decks) -> dict[int, Card]:
  return {
    row["id"]: Card.from_row(row, note=notes[row["nid"]], deck=decks[row["did"]])
    for row in rows["cards"]
  }


# ----------------------------------- What the fixture holds -----------------------------


def test_the_fixture_covers_every_review_kind_the_collection_recorded(rows):
  """Filtered reviews are the one kind a real collection can lack, and this one does.
  Losing any of the other four to a re-trim would leave a column untested."""
  assert {row["type"] for row in rows["revlog"]} == {0, 1, 2, 4}


def test_the_fixture_covers_every_button(rows):
  assert {row["ease"] for row in rows["revlog"]} == {0, 1, 2, 3, 4}


def test_the_fixture_covers_both_interval_units(rows):
  intervals = [row["ivl"] for row in rows["revlog"]]
  assert min(intervals) < 0 < max(intervals)


def test_the_fixture_covers_both_sync_states(rows):
  """A pending marker sits in the same table as synced values in a real collection."""
  usns = {row["usn"] for row in rows["revlog"]}
  assert -1 in usns and max(usns) >= 0


def test_the_fixture_covers_every_grave_kind(rows):
  assert {row["type"] for row in rows["graves"]} == {0, 1, 2}


# ------------------------------------- Row round trips ----------------------------------


def test_every_registered_tag_survives_a_round_trip(rows):
  for row in rows["tags"]:
    assert Tag.from_row(row).to_row() == row


def test_every_grave_survives_a_round_trip(rows):
  for row in rows["graves"]:
    assert Grave.from_row(row).to_row() == row


def test_every_review_survives_a_round_trip(rows, cards):
  written = 0
  for row in rows["revlog"]:
    if row["cid"] not in cards:
      continue  # an entry whose card was deleted, covered below
    assert Review.from_row(row, card=cards[row["cid"]]).to_row() == row
    written += 1
  assert written, "the fixture lost the rows whose card it keeps"


def test_the_chain_a_review_hangs_off_survives_a_round_trip(rows, notetypes, cards):
  """A review round trip is only worth as much as the card behind it, since `to_row()`
  takes `cid` off the object rather than off the row it was read from."""
  for row in rows["notetypes"]:
    assert notetypes[row["id"]].to_rows()[0] == row
  for row in rows["cards"]:
    assert cards[row["id"]].to_row() == row


# --------------------------------- The rows no model holds ------------------------------


def test_an_entry_whose_card_was_deleted_cannot_become_a_review(rows, cards):
  """Nothing cascades when a card is deleted, so a real collection carries these: ten
  of them here, 242 in the untrimmed source. `Review.card` is a required `Card`, and
  `Card.from_row` needs a note and a deck that are equally gone, so the whole chain is
  unreachable and no model can hold the row. `Collection.read_sqlite()` keeps it raw in
  `orphan_reviews` and writes it back, which `test_collection_roundtrip.py` covers.
  """
  orphans = [row for row in rows["revlog"] if row["cid"] not in cards]
  assert orphans
  for row in orphans:
    assert row["cid"] not in {card_row["id"] for card_row in rows["cards"]}


def test_a_grave_naming_a_row_the_collection_still_holds_is_ordinary(rows):
  """Most graves in the source name a live row, which is what deleting a deck and
  re-importing it under the same ids produces. Nothing rejects them."""
  note_ids = {row["id"] for row in rows["notes"]}
  buried_notes = [row for row in rows["graves"] if row["type"] == 1]
  assert any(row["oid"] in note_ids for row in buried_notes)
  assert any(row["oid"] not in note_ids for row in buried_notes)
