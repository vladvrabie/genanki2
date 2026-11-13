"""Does an apkg import skip exactly the decks and notetypes genanki2 reports as unused?

    uv run --with anki --with pytest pytest tests/test_unused_decks_and_notetypes_match_anki.py

The importer gathers from the package only the decks holding its cards, their parents
found by name, and, with scheduling, the home decks of cards in a filtered deck
(`gather_decks`, `rslib/src/import_export/gather.rs:261`). It gathers only the
notetypes of its notes (`gather_notetypes`). `Collection.unused_decks` and
`Collection.unused_notetypes` report the rest against an import without scheduling.

A skipped parent still appears in the target, since Anki creates the missing parents of
a deck it adds, so each package deck carries its name as its description and arriving
means arriving with it. The `Default` deck is left out: every target has deck 1.

anki is a test-only dependency, never a dependency of genanki2.
"""

import pytest
from anki.collection import Collection as AnkiCollection
from anki.collection import ImportAnkiPackageRequest
from anki.import_export_pb2 import ImportAnkiPackageOptions

from genanki2 import BASIC_NOTETYPE, deck, filtered_deck, note, package
from genanki2.builtins import builtin_notetype
from genanki2.models.card import Card
from genanki2.models.collection import Collection
from genanki2.models.deck import Deck
from genanki2.models.package import Package


def described_deck(name: str) -> Deck:
  normal = deck(name)
  normal.kind.description = name
  return normal


def move_to_filtered_deck(card: Card, name: str) -> None:
  """Move `card` into a new filtered deck, keeping its deck as the home deck."""
  home, due = card.deck, card.due
  card.deck = filtered_deck(name, search="deck:*")
  card.original_deck, card.original_due = home, due


def build_package() -> tuple[Package, Collection, set[str]]:
  """A package covering each way a deck is used or not, and the home deck names."""
  pkg, collection = package()
  collection.notetypes.append(builtin_notetype("Cloze (genanki2)"))
  collection.decks.append(described_deck("Empty"))

  # `Gap::Missing` is absent, so the walk up from the leaf stops before `Gap`.
  collection.decks.append(described_deck("Gap"))
  leaf = described_deck("Gap::Missing::Leaf")
  collection.decks.append(leaf)
  collection.add_notes([note(BASIC_NOTETYPE, ["gap", "x"])], leaf)

  # `Home` holds no card once its card moves to a top-level filtered deck.
  home = described_deck("Home")
  collection.decks.append(home)
  [[home_card]] = collection.add_notes([note(BASIC_NOTETYPE, ["home", "x"])], home)

  # `A::B` is only the card's home deck, held by no list, and still leads up to `A`.
  # The cards move after the last `add_notes()`, which collects the decks of every
  # card it finds, so that `A::B` stays out of `decks`.
  collection.decks.append(described_deck("A"))
  [[a_card]] = collection.add_notes(
    [note(BASIC_NOTETYPE, ["a", "x"])], described_deck("A::B")
  )
  move_to_filtered_deck(home_card, "Cram")
  move_to_filtered_deck(a_card, "A::B::Cram")
  assert all(deck.name != "A::B" for deck in collection.decks)

  return pkg, collection, {"A::B", "Home"}


@pytest.mark.parametrize(
  "with_scheduling", [False, True], ids=["default", "scheduling"]
)
def test_an_import_skips_exactly_the_unused_decks_and_notetypes(
  tmp_path, with_scheduling
):
  pkg, collection, home_deck_names = build_package()
  unused_decks = {deck.name for deck in collection.unused_decks} - {"Default"}
  unused_notetypes = {notetype.name for notetype in collection.unused_notetypes}
  assert unused_decks == {"Empty", "Gap", "Home"}
  assert unused_notetypes == {"Cloze (genanki2)"}

  path = tmp_path / "package.apkg"
  pkg.write_apkg(path)
  target = AnkiCollection(str(tmp_path / "target.anki2"))
  options = ImportAnkiPackageOptions(with_scheduling=with_scheduling)
  target.import_anki_package(
    ImportAnkiPackageRequest(package_path=str(path), options=options)
  )

  def arrived(deck: Deck) -> bool:
    anki_deck = target.decks.by_name(deck.name)
    return anki_deck is not None and anki_deck.get("desc") == deck.name

  described = [
    deck
    for deck in collection.collect_decks().decks
    if deck.is_normal() and deck.id != 1
  ]
  skipped = {deck.name for deck in described if not arrived(deck)}
  missing_notetypes = {
    notetype.name
    for notetype in collection.notetypes
    if target.models.by_name(notetype.name) is None
  }
  target.close()

  # Scheduling brings the home decks in too, which the default import leaves out.
  expected = unused_decks - home_deck_names if with_scheduling else unused_decks
  assert skipped == expected
  assert missing_notetypes == unused_notetypes
