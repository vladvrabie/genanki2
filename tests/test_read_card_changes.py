"""Does writing back a read card warn when an apkg import would skip the change?

    uv run --with anki --with pytest pytest tests/test_read_card_changes.py

Anki's importer maps an incoming card's note to the user's note by guid
(`map_to_imported_note`) and adds the card only when that note has no card with the same
ordinal yet (`card_ordinal_already_exists`, both in
`rslib/src/import_export/package/apkg/import/cards.rs:100-133`). A card the user has is
never updated, so no change to it reaches them: not a deck move, a flag or its schedule,
whether or not "Import any learning progress" is ticked. A card under a new guid or
ordinal is added, so it is not compared.

The last section checks those claims against the anki wheel.
"""

import warnings
from collections.abc import Callable
from pathlib import Path

import pytest
from anki.collection import Collection as AnkiCollection
from anki.collection import ImportAnkiPackageRequest
from anki.import_export_pb2 import ImportAnkiPackageOptions

from genanki2.builtins import BASIC_NOTETYPE, DEFAULT_DECK
from genanki2.errors import Genanki2Warning
from genanki2.models.card import Card, CardFlag, CardQueue
from genanki2.models.collection import Collection
from genanki2.models.config import ConfigEntry
from genanki2.models.deck import Deck, DeckFilteredKind, DeckNormalKind
from genanki2.models.note import Note
from genanki2.models.package import CollectionPackage, Package
from genanki2.models.serialization_context import SerializationContext

SCHEDULER_V2 = [
  ConfigEntry(key="schedVer", value=b"2"),
  ConfigEntry(key="sched2021", value=b"true"),
]
APKG = SerializationContext(output=SerializationContext.Output.APKG)
MATCH = "Card: card "

HOME_DECK = "Italian::Verbs"
OTHER_DECK = "Italian::Nouns"
FRONT = "parlare"


def read_card(deck: Deck | None = None, **values) -> Card:
  """A card written to a row and read back, as last release's file."""
  deck = deck if deck is not None else Deck(name=HOME_DECK)
  note = Note(notetype=BASIC_NOTETYPE, fields=[FRONT, "to speak"])
  card = Card(note=note, deck=deck, due=1, **values)
  with warnings.catch_warnings():
    warnings.simplefilter("ignore", Genanki2Warning)
    return Card.from_row(card.to_row(), note=note, deck=deck)


def change_warnings(
  card: Card, context: SerializationContext | None = APKG
) -> list[str]:
  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always", Genanki2Warning)
    card.warn_unimported_changes(context)
  return [str(w.message) for w in caught]


# ------------------------------- What the import skips --------------------------------


def move_to_another_deck(card: Card) -> None:
  card.deck = Deck(name=OTHER_DECK)


def flag(card: Card) -> None:
  card.flags = CardFlag.RED


def suspend(card: Card) -> None:
  card.queue = CardQueue.SUSPENDED


def reposition(card: Card) -> None:
  card.due = 2


def set_custom_data(card: Card) -> None:
  card.data.custom_data = {"k": "v"}


@pytest.mark.parametrize(
  "edit",
  [
    pytest.param(move_to_another_deck, id="deck"),
    pytest.param(flag, id="flag"),
    pytest.param(suspend, id="queue"),
    pytest.param(reposition, id="position"),
    pytest.param(set_custom_data, id="data"),
  ],
)
def test_an_edit_under_the_read_identity_warns(edit: Callable[[Card], None]):
  card = read_card()
  edit(card)
  [message] = change_warnings(card)
  assert message.startswith(
    f'Card: card {card.id} of note "{card.note.guid}" (template ordinal 0) changed'
  )
  assert "An apkg import skips a card" in message


def test_the_time_the_sync_marker_and_the_note_id_are_not_compared():
  """The import matches the note by guid, so a new note id changes nothing for it."""
  card = read_card()
  card.modification_time_s = 1_600_000_000
  card.update_sequence_n = 5
  card.note.id += 1
  assert change_warnings(card) == []


def test_clearing_an_original_deck_the_card_cannot_explain_is_not_a_change():
  """A card read from a filtered deck turned normal has its original deck and due
  cleared by `Card.prepare()` at write time, and that repair is not the author's."""
  filtered = Deck(name=HOME_DECK, kind=DeckFilteredKind())
  home = Deck(name=OTHER_DECK)
  card = read_card(filtered, original_deck=home, original_due=7)
  filtered.kind = DeckNormalKind(config_id=1)
  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always", Genanki2Warning)
    row = card.to_row()
  assert (row["odid"], row["odue"]) == (0, 0)
  assert [w for w in caught if str(w.message).startswith(MATCH)] == []


# -------------------------------- What is not compared --------------------------------


def test_an_untouched_read_does_not_warn():
  assert change_warnings(read_card()) == []


def test_a_card_built_in_code_is_never_compared():
  card = Card(
    note=Note(notetype=BASIC_NOTETYPE, fields=[FRONT, "x"]), deck=DEFAULT_DECK
  )
  flag(card)
  assert change_warnings(card) == []


def test_a_card_whose_note_has_a_new_guid_is_not_compared():
  """The import adds the note as a new one, and the card with it."""
  card = read_card()
  card.note.guid = "a new guid"
  flag(card)
  assert change_warnings(card) == []


def test_a_card_moved_to_another_note_is_not_compared():
  card = read_card()
  card.note = Note(notetype=BASIC_NOTETYPE, fields=["mangiare", "to eat"])
  flag(card)
  assert change_warnings(card) == []


def test_a_card_under_a_new_ordinal_is_not_compared():
  """The user's note has no card for that template yet, so the import adds it."""
  card = read_card()
  card.template_ord = 1
  flag(card)
  assert change_warnings(card) == []


# ------------------------------ What the write produces -------------------------------


def read_back(tmp_path: Path) -> Collection:
  """Write a collection holding the card and read it back, as last release's file."""
  path = tmp_path / "collection.anki2"
  deck = Deck(name=HOME_DECK)
  note = Note(notetype=BASIC_NOTETYPE, fields=[FRONT, "to speak"])
  with warnings.catch_warnings():
    warnings.simplefilter("ignore", Genanki2Warning)
    Collection(
      decks=[DEFAULT_DECK, deck],
      notes=[note],
      cards=[Card(note=note, deck=deck, due=1)],
      config=list(SCHEDULER_V2),
    ).write_sqlite(path)
    return Collection.read_sqlite(path)


@pytest.mark.parametrize(
  ("write", "expected"),
  [
    pytest.param(
      lambda col, d: Package(collection=col).write_apkg(d / "deck.apkg"),
      "An apkg import skips a card",
      id="apkg",
    ),
    pytest.param(
      lambda col, d: CollectionPackage(collection=col).write_colpkg(
        d / "collection.colpkg"
      ),
      None,
      id="colpkg",
    ),
    pytest.param(
      lambda col, d: col.write_sqlite(d / "rewritten.anki2"),
      "If imported from an apkg, the import skips a card",
      id="sqlite",
    ),
    pytest.param(
      lambda col, d: col.warn(),
      "If imported from an apkg, the import skips a card",
      id="no context",
    ),
  ],
)
def test_a_skipped_edit_is_reported_for_what_the_write_produces(
  tmp_path, write, expected
):
  """A restore replaces the user's cards outright, so nothing is skipped."""
  collection = read_back(tmp_path)
  [card] = collection.cards
  flag(card)
  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always", Genanki2Warning)
    write(collection, tmp_path)

  messages = [str(w.message) for w in caught if str(w.message).startswith(MATCH)]
  if expected is None:
    assert messages == []
  else:
    assert len(messages) == 1
    assert expected in messages[0]


# --------------------------------- Against the wheel ----------------------------------


@pytest.fixture
def shipped(tmp_path) -> Path:
  """A package with one card in one of its two decks."""
  home, other = Deck(name=HOME_DECK), Deck(name=OTHER_DECK)
  note = Note(notetype=BASIC_NOTETYPE, fields=[FRONT, "to speak"])
  collection = Collection(
    config=list(SCHEDULER_V2),
    decks=[home, other],
    notes=[note],
    cards=[Card(note=note, deck=home, due=1)],
  )
  path = tmp_path / "shipped.apkg"
  Package(collection=collection).write_apkg(path)
  return path


@pytest.fixture
def target(tmp_path):
  """A collection that imports with every option on."""
  col = AnkiCollection(str(tmp_path / "target.anki2"))
  yield col
  col.close()


def import_package(target: AnkiCollection, path: Path) -> None:
  target.import_anki_package(
    ImportAnkiPackageRequest(
      package_path=str(path),
      options=ImportAnkiPackageOptions(with_deck_configs=True, with_scheduling=True),
    )
  )


def write_edited(
  source: Path, path: Path, edit: Callable[[Package], None]
) -> list[str]:
  """Read the package back, edit it and write it to `path`, returning the card warnings
  raised."""
  package = Package.read_apkg(source)
  edit(package)
  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always", Genanki2Warning)
    package.write_apkg(path)
  return [str(w.message) for w in caught if str(w.message).startswith(MATCH)]


def move_and_flag(package: Package) -> None:
  [card] = package.collection.cards
  other = package.collection.get_deck(name=OTHER_DECK)
  assert other is not None
  card.deck = other
  flag(card)


def test_a_skipped_edit_does_not_reach_a_user_who_has_the_card(
  target, shipped, tmp_path
):
  """The card keeps the deck and flag the first import gave it."""
  import_package(target, shipped)
  edited = tmp_path / "edited.apkg"
  assert len(write_edited(shipped, edited, move_and_flag)) == 1

  import_package(target, edited)
  [card_id] = target.find_cards(FRONT)
  card = target.get_card(card_id)
  assert target.decks.name(card.did) == HOME_DECK
  assert card.user_flag() == 0


def test_a_card_whose_note_has_a_new_guid_is_added(target, shipped, tmp_path):
  """The other side of the line the warning draws: the note arrives as a new one, and
  its card lands where the package put it."""
  import_package(target, shipped)

  def edit(package: Package) -> None:
    move_and_flag(package)
    package.collection.notes[0].guid = "a new guid"

  edited = tmp_path / "edited.apkg"
  assert write_edited(shipped, edited, edit) == []

  import_package(target, edited)
  cards = [target.get_card(card_id) for card_id in target.find_cards(FRONT)]
  assert sorted(target.decks.name(card.did) for card in cards) == [
    OTHER_DECK,
    HOME_DECK,
  ]
