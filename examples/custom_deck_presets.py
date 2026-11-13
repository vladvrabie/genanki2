"""Create custom deck configs / presets.

    python examples/custom_deck_presets.py presets.apkg

A preset holds the study options that Anki shows under a deck's Options: daily limits,
learning steps, FSRS. Decks share a preset by holding the same one. A deck without
one uses the collection's default preset, `DEFAULT_DECK_CONFIG`.
"""

import sys
from pathlib import Path

from genanki2 import BASIC_NOTETYPE, deck, deck_config, note, package


def main(path: Path) -> None:
  pkg, collection = package()

  # Keyword arguments are the options of `DeckConfigInner`.
  slow = deck_config("Italian, slow", new_per_day=5, reviews_per_day=100)
  fast = deck_config("Italian, fast", new_per_day=30, desired_retention=0.85)

  grammar = deck("Italian::Grammar", config=slow, description="One rule a day.")
  vocabulary = deck("Italian::Vocabulary", config=fast)
  phrases = deck("Italian::Phrases", config=fast)

  collection.add_notes([note(BASIC_NOTETYPE, ["essere: io ...", "sono"])], grammar)
  collection.add_notes([note(BASIC_NOTETYPE, ["il cane", "the dog"])], vocabulary)
  collection.add_notes([note(BASIC_NOTETYPE, ["Come stai?", "How are you?"])], phrases)

  pkg.write_apkg(path)


if __name__ == "__main__":
  main(Path(sys.argv[1]))
