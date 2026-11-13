"""Look inside a package without changing it.

    python examples/read_package.py italian.apkg

Reading parses and validates every table. The checks and warnings that run on a write
can be run on their own, to learn whether the package would write back unchanged.

Reading a package can extract all of its media files to a folder: pass the
`extract_dir` parameter to the reading methods. To skip extraction and tell genkanki2
to still keep track of the media files in the package, set `keep_source_media=True`.
Without either, the media files are not read at all.
"""

import sys
from collections import Counter
from pathlib import Path

from genanki2 import Genanki2Error, Package


def main(path: Path) -> None:
  # Without `keep_source_media` or `extract_dir`, the media are not read at all.
  pkg = Package.read_apkg(path, keep_source_media=True)
  collection = pkg.collection

  cards_per_deck = Counter(card.deck.name for card in collection.cards)
  for deck in collection.decks:
    print(f"{deck.name}: {cards_per_deck[deck.name]} cards")

  for notetype in collection.notetypes:
    fields = ", ".join(notetype.field_names)
    print(f"{notetype.name} (id {notetype.id}): {fields}")

  print(f"{len(collection.notes)} notes, {len(pkg.media_filenames)} media files")
  print(f"A note added now is studied at position {collection.next_new_card_position}")

  try:
    # Run all the checks. Write methods call this unless disabled (see
    # examples/settings.py).
    pkg.check()
  except Genanki2Error as error:
    print(f"Would not write back: {error}")

  # Print all the warnings. Write methods call this unless disabled (see
  # examples/settings.py).
  pkg.warn()


if __name__ == "__main__":
  main(Path(sys.argv[1]))
