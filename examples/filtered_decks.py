"""Create filtered decks: saved searches that gather cards from the normal decks.

    python examples/filtered_decks.py filtered.colpkg

A filtered deck holds no notes of its own. Anki fills it when the user builds or
rebuilds it. Each one takes a search in the browser's syntax, with a limit and an
order, and optionally a second one. Anki never gathers suspended or buried cards, or
cards already in another filtered deck.

With `reschedule=False`, answers in the filtered deck don't affect a card's schedule
from its home deck, so they return with the same schedule as when they were collected.

This example writes a collection package (.colpkg), not a deck package (.apkg),
assuming this is the more typical use case for filtered decks. A colpkg carries every
deck, empty or not, and importing one replaces the user's whole collection. It suits
setting up a new profile or repairing a collection, not sharing decks with others.
After the import, the user builds each filtered deck in Anki.

An apkg import on the other hand only imports the decks that hold at least one of the
package's cards. In other words, empty (filtered) decks are not imported by Anki from
an apkg. To ensure a filtered deck is imported, one has to also create the `Card`
instances and set the `deck` attribute to the filtered deck and the `original_*`
attributes to the home deck. Even then, it arrives as a filtered deck only when the
user ticks "Import any learning progress" while importing. Without it, Anki moves the
cards back to their home decks and imports the filtered deck as an empty normal one.
"""

import sys
from pathlib import Path

from genanki2 import (
  BASIC_NOTETYPE,
  Collection,
  CollectionPackage,
  DeckFilteredKind,
  deck,
  filtered_deck,
  note,
)

Order = DeckFilteredKind.SearchTerm.Order


def main(path: Path) -> None:
  collection = Collection.anki_default()
  pkg = CollectionPackage(collection=collection)

  vocabulary = deck("Italian::Vocabulary")
  notes = [
    note(BASIC_NOTETYPE, ["mangiare", "to eat"], tags=["verb"]),
    note(BASIC_NOTETYPE, ["la casa", "the house"], tags=["noun"]),
  ]
  collection.add_notes(notes, vocabulary)

  verbs = filtered_deck(
    "Italian::Cram verbs",
    search='deck:"Italian::Vocabulary" tag:verb',
    limit=50,
    order=Order.RANDOM,
    reschedule=False,
  )
  catch_up = filtered_deck(
    "Italian::Catch up",
    search="deck:Italian is:due",
    limit=100,
    order=Order.RELATIVE_OVERDUENESS,
    search2="deck:Italian is:new",
    limit2=10,
    order2=Order.ADDED,
  )
  # A filtered deck has to be manually added to the decks list (nothing references it
  # to be automatically picked up by the writing process).
  collection.decks.extend([verbs, catch_up])

  # Anki cannot study a collection without the `Default` deck, so a colpkg needs it.
  # If it wasn't added manually, no cards reference it, or the collection was not
  # created using `Collection.anki_default()`, use:
  collection.collect_default_deck()

  pkg.write_colpkg(path)


if __name__ == "__main__":
  main(Path(sys.argv[1]))
