"""Does a grave survive the trip to the `graves` table, and is its key the pair?

    uv run --with pytest pytest tests/test_grave_models.py

`graves` is the deletion log, and only sync reads it: `pending_graves` hands the rows to
the server (`rslib/src/storage/graves/mod.rs:51-69`) and `before_upload` clears the table
outright (`rslib/src/collection/mod.rs:194`). No export writes it and no import reads it,
so a grave reaches genanki2 only from a collection read off a colpkg or a plain SQLite
file. Writing one that was not read there asks the server to delete what it names, which
is why nothing in the library adds one.

The primary key is `(oid, type)` rather than the id alone, so the same id buried under
two kinds is two rows. Anki inserts with `INSERT OR IGNORE` (`storage/graves/add.sql`)
and cannot produce a duplicate pair; a caller assembling the list can, and SQLite would
report it as an `IntegrityError` naming the table and no object.

The models carry no anki dependency and neither does this file.
"""

import pydantic as pyd
import pytest

from genanki2.builtins import BASIC_NOTETYPE, DEFAULT_DECK, DEFAULT_DECK_CONFIG
from genanki2.models.card import Card
from genanki2.models.collection import Collection
from genanki2.models.config import ConfigEntry
from genanki2.models.grave import Grave, GraveType
from genanki2.models.note import Note
from genanki2.sqlite import AnkiSQLite

A_DELETED_ID = 1_700_000_000_000

# Written collections carry it so that the writer's v1 scheduler warning stays out of
# the way of what these tests are about.
SCHEDULER_V2 = [ConfigEntry(key="schedVer", value=b"2")]


def a_grave(
  oid: int = A_DELETED_ID, type: GraveType = GraveType.NOTE, usn: int = -1
) -> Grave:
  return Grave(oid=oid, grave_type=type, usn=usn)


def written_rows(collection: Collection, table: str, tmp_path) -> list[dict]:
  """Write `collection` and read `table` back out of the file it wrote."""
  path = tmp_path / "collection.anki2"
  collection.write_sqlite(path)
  with AnkiSQLite(path) as db:
    return db.connection.execute(f"select * from {table}").fetchall()


# ----------------------------------- Row round trips ------------------------------------


def test_the_row_survives_a_round_trip():
  row = a_grave(usn=12).to_row()
  assert Grave.from_row(row).to_row() == row


def test_the_object_id_is_read_under_the_column_name():
  """The attribute is `object_id` and the column is `oid`, so a wrong alias would
  substitute the default, except there is none and the read fails instead."""
  assert Grave.from_row({"oid": 42, "type": 0, "usn": -1}).object_id == 42


def test_a_row_built_by_attribute_name_still_writes_the_column_name():
  assert Grave(object_id=42, grave_type=GraveType.CARD).to_row()["oid"] == 42


# -------------------------------------- The kinds ---------------------------------------


@pytest.mark.parametrize(
  ("kind", "value"), [(GraveType.CARD, 0), (GraveType.NOTE, 1), (GraveType.DECK, 2)]
)
def test_the_kinds_are_numbered_the_way_anki_numbers_them(kind, value):
  """`GraveKind` is `#[repr(u8)]` with no explicit discriminants and `Card, Note, Deck`
  in that order (`rslib/src/storage/graves/mod.rs:13-19`), so the order is the value."""
  assert a_grave(type=kind).to_row()["type"] == value


def test_a_kind_the_enum_does_not_name_is_refused():
  """Anki reads the column with `try_from` and fails the read on anything else
  (`rslib/src/storage/graves/mod.rs:60-61`), so carrying it through would write a row
  no sync can read."""
  with pytest.raises(pyd.ValidationError):
    a_grave(type=3)


# ------------------------------------- The object id ------------------------------------


@pytest.mark.parametrize("object_id", [0, -1])
def test_an_object_id_that_is_not_an_id_is_refused(object_id):
  with pytest.raises(pyd.ValidationError):
    a_grave(oid=object_id)


def test_an_object_id_naming_a_live_row_is_accepted():
  """Most of the ids in a real collection's table name a row it currently holds, which
  is what deleting a deck and re-importing it under the same ids produces. The table
  records that a deletion happened, not that the id came free."""
  note = Note(notetype=BASIC_NOTETYPE, fields=["front", "back"])
  collection = Collection(
    notetypes=[BASIC_NOTETYPE],
    notes=[note],
    graves=[Grave(oid=note.id, grave_type=GraveType.NOTE)],
    config=SCHEDULER_V2,
  )
  assert collection.check() is collection


# ---------------------------------- The default deck -------------------------------------


def test_a_grave_burying_the_default_deck_is_refused():
  """Anki empties the default deck and resets its name instead of removing the row, so
  it graves every other deck and never this one."""
  grave = a_grave(oid=1, type=GraveType.DECK)
  with pytest.raises(ValueError, match="default deck"):
    grave.to_row()


def test_the_default_deck_id_under_another_kind_is_accepted():
  """The pair is the key: card 1 and note 1 are other objects entirely."""
  assert a_grave(oid=1, type=GraveType.CARD).to_row()["oid"] == 1
  assert a_grave(oid=1, type=GraveType.NOTE).to_row()["oid"] == 1


def test_another_deck_is_accepted():
  assert a_grave(oid=2, type=GraveType.DECK).to_row()["oid"] == 2


# ------------------------------------- Sync columns -------------------------------------


def test_a_fresh_grave_has_not_reached_the_server():
  assert a_grave().update_sequence_n == -1


# ------------------------------------- Unique keys --------------------------------------


def test_two_graves_burying_the_same_object_are_refused():
  collection = Collection(graves=[a_grave(), a_grave(usn=7)])
  with pytest.raises(ValueError, match="graves"):
    collection.check_unique_graves()


def test_one_id_buried_under_two_kinds_is_two_graves():
  """The key is the pair, so an id that named both a card and a note is two rows."""
  collection = Collection(
    graves=[
      a_grave(type=GraveType.CARD),
      a_grave(type=GraveType.NOTE),
      a_grave(type=GraveType.DECK),
    ],
    config=SCHEDULER_V2,
  )
  assert collection.check() is collection


# ------------------------------------ Serialization -------------------------------------


def test_a_grave_reaches_the_table(tmp_path):
  grave = a_grave(usn=3)
  collection = Collection(graves=[grave], config=SCHEDULER_V2)
  rows = written_rows(collection, "graves", tmp_path)
  assert rows == [grave.to_row()]


def test_a_collection_invents_no_graves(tmp_path):
  """Nothing in the library adds a grave: the list is written and never filled."""
  note = Note(notetype=BASIC_NOTETYPE, fields=["front", "back"])
  collection = Collection(
    deck_configs=[DEFAULT_DECK_CONFIG],
    decks=[DEFAULT_DECK],
    notetypes=[BASIC_NOTETYPE],
    notes=[note],
    cards=[Card(note=note, deck=DEFAULT_DECK)],
    config=SCHEDULER_V2,
  )
  assert written_rows(collection, "graves", tmp_path) == []
