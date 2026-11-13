"""Does a colpkg write refuse a collection Anki would restore and then fail to study?

    uv run --with pytest pytest tests/test_collection_package_checks.py

A restore replaces the user's collection with the file and repairs nothing, so the file
has to hold what Anki assumes every collection holds. Deck 1 is one of those things. A
restored collection without it opens, but every scheduler call raises `No such deck:
'1'`, whichever deck is current, and the database check does not recreate it (measured
against anki 26.08.1). Anki never writes such a file, since deleting deck 1 resets it
instead (`decks/remove.rs`), so only a collection built from models can lack it. A merge
needs no deck 1 of its own, since it lands in a collection that already has one.

`Collection.check_present_default_deck()` raises only for a colpkg write. An apkg write
says nothing, and any other write warns that the problem applies to a restore.

The check reads `decks`, and a deck a card names reaches that list only when the
references are collected, so the writer collects before it checks.
"""

import warnings

import pytest

from genanki2.builtins import BASIC_NOTETYPE, DEFAULT_DECK, default_deck
from genanki2.errors import Genanki2Error, Genanki2Warning
from genanki2.models.card import Card
from genanki2.models.collection import Collection
from genanki2.models.config import ConfigEntry
from genanki2.models.deck import Deck
from genanki2.models.note import Note
from genanki2.models.package import CollectionPackage, Package
from genanki2.models.serialization_context import SerializationContext
from genanki2.settings import checks_disabled

# Written collections carry it so that the writer's v1 scheduler warning stays out of
# the way of what these tests are about.
SCHEDULER_V2 = [ConfigEntry(key="schedVer", value=b"2")]

APKG = SerializationContext(output=SerializationContext.Output.APKG)
COLPKG = SerializationContext(output=SerializationContext.Output.COLPKG)


def a_collection(**lists) -> Collection:
  return Collection(config=list(SCHEDULER_V2), **lists)


def a_card(deck: Deck) -> Card:
  return Card(note=Note(notetype=BASIC_NOTETYPE, fields=["front", "back"]), deck=deck)


# ------------------------------------- The check --------------------------------------


def test_a_collection_without_deck_1_is_refused():
  collection = a_collection(decks=[Deck(name="Italian")])
  with pytest.raises(Genanki2Error, match="no deck with id 1"):
    collection.check_present_default_deck(COLPKG)


def test_the_refusal_names_the_repair():
  collection = a_collection(decks=[Deck(name="Italian")])
  with pytest.raises(Genanki2Error, match=r"Collection\.collect_default_deck\(\)"):
    collection.check(context=COLPKG)


def test_a_collection_holding_deck_1_passes():
  collection = a_collection(decks=[default_deck()])
  assert collection.check(context=COLPKG) is collection


def test_deck_1_passes_under_any_name():
  """Anki looks the deck up by id, and a user can rename deck 1."""
  collection = a_collection(decks=[Deck(id=1, name="Mine")])
  assert collection.check(context=COLPKG) is collection


def test_a_direct_check_reads_the_decks_as_they_stand():
  """Only the writer collects first. A deck 1 no list holds yet is not seen here."""
  collection = a_collection(cards=[a_card(DEFAULT_DECK)])
  with pytest.raises(Genanki2Error, match="no deck with id 1"):
    collection.check_present_default_deck(COLPKG)


@pytest.mark.parametrize(
  "context", [pytest.param(None, id="no context"), pytest.param(APKG, id="apkg")]
)
def test_only_a_colpkg_is_refused(context):
  collection = a_collection(decks=[Deck(name="Italian")])
  assert collection.check_present_default_deck(context) is collection


# ------------------------------------- The writer -------------------------------------


def test_the_writer_sees_a_deck_1_only_a_card_names(tmp_path):
  path = tmp_path / "collection.colpkg"
  package = CollectionPackage(collection=a_collection(cards=[a_card(DEFAULT_DECK)]))
  package.write_colpkg(path)
  assert path.is_file()


def test_a_refused_collection_writes_nothing(tmp_path):
  path = tmp_path / "collection.colpkg"
  package = CollectionPackage(
    collection=a_collection(cards=[a_card(Deck(name="Italian"))])
  )
  with pytest.raises(Genanki2Error, match="no deck with id 1"):
    package.write_colpkg(path)
  assert not path.exists()


def test_collecting_the_default_deck_lets_the_write_through(tmp_path):
  path = tmp_path / "collection.colpkg"
  collection = a_collection(cards=[a_card(Deck(name="Italian"))])
  CollectionPackage(collection=collection.collect_default_deck()).write_colpkg(path)
  assert path.is_file()


def test_a_deck_package_needs_no_deck_1(tmp_path):
  path = tmp_path / "deck.apkg"
  package = Package(collection=a_collection(cards=[a_card(Deck(name="Italian"))]))
  package.write_apkg(path)
  assert path.is_file()


# ------------------------------------- The warning ------------------------------------


def write_colpkg_with_checks_disabled(collection: Collection, directory) -> None:
  with checks_disabled():
    CollectionPackage(collection=collection).write_colpkg(
      directory / "collection.colpkg"
    )


@pytest.mark.parametrize(
  ("write", "expected"),
  [
    pytest.param(
      lambda col, d: Package(collection=col).write_apkg(d / "deck.apkg"),
      None,
      id="apkg",
    ),
    pytest.param(write_colpkg_with_checks_disabled, "After a restore", id="colpkg"),
    pytest.param(
      lambda col, d: col.write_sqlite(d / "collection.anki2"),
      "If restored as a collection package",
      id="sqlite",
    ),
    pytest.param(
      lambda col, d: col.warn(), "If restored as a collection package", id="no context"
    ),
  ],
)
def test_a_missing_deck_1_is_reported_for_what_the_write_produces(
  tmp_path, write, expected
):
  """A colpkg write raises unless checks are disabled, and then warns instead."""
  collection = a_collection(cards=[a_card(Deck(name="Italian"))])
  collection.collect_references()
  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always", Genanki2Warning)
    write(collection, tmp_path)

  messages = [
    str(w.message)
    for w in caught
    if str(w.message).startswith("Collection: no deck has id 1")
  ]
  if expected is None:
    assert messages == []
  else:
    assert len(messages) == 1
    assert expected in messages[0]
    assert "Collection.collect_default_deck()" in messages[0]


def test_a_collection_holding_deck_1_warns_about_nothing(recwarn):
  a_collection(decks=[default_deck()]).warn_missing_default_deck()
  assert not [w for w in recwarn if "no deck has id 1" in str(w.message)]
