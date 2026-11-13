"""Build a package from scratch: two decks, two builtin notetypes, a few notes.

    python examples/new_package.py capitals.apkg

`package()` returns the package and its collection. The package holds the media and
gets read and written to disk. The collection holds the decks, notetypes and notes.

Generally, there is no need to manually keep track of decks, notes and/or notetypes
added to the collection. `Collection.add_notes()` creates cards that reference them,
and the writing process collects every object referenced by another object.
`Collection.add_notes()` will not touch the decks, notes and notetypes lists in case
they are added manually.

Add all the notes going to one deck in a single `add_notes()` call. Each call reads the
whole collection, so calling it once per note gets slow on a large deck.

To include empty decks or unused notetypes in the package, one has to add them manually
to their respective list. Even so, Anki ignores them when importing an apkg: Anki
creates only the non-empty decks that hold the package's cards and the notetypes of the
notes in the package. From a collection package (.colpkg), Anki always imports them
(see examples/filtered_decks.py).

Notes are studied in the order they are added.
"""

import sys
from pathlib import Path

from genanki2 import (
  BASIC_AND_REVERSED_NOTETYPE,
  BASIC_NOTETYPE,
  Note,
  deck,
  note,
  package,
)

CAPITALS: list[tuple[str, str]] = [
  ("France", "Paris"),
  ("India", "New Delhi"),
  ("Brazil", "Brasília"),
]


def main(path: Path) -> None:
  pkg, collection = package()

  geography = deck("Geography")
  capitals = deck("Geography::Capitals")

  note1 = note(BASIC_NOTETYPE, ["What is the capital of Germany?", "Berlin"])
  collection.add_notes([note1], geography)

  notes: list[Note] = []
  for country, capital in CAPITALS:
    fields = [f"Capital of {country}", capital]
    notes.append(note(BASIC_AND_REVERSED_NOTETYPE, fields, tags=["capitals"]))

  # The cards come back grouped per note, in the order of `notes`.
  for cards in collection.add_notes(notes, capitals):
    # Suspend the reversed card. The suspension reaches the user only when they tick
    # "Import any learning progress"; by default Anki imports every card as new.
    front2back, back2front = cards
    back2front.suspend()

  pkg.write_apkg(path)


if __name__ == "__main__":
  main(Path(sys.argv[1]))
