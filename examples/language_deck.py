"""Assuming a CSV file and media files, (re)build an Italian vocabulary deck.

    python examples/language_deck.py italian.apkg

The script provides some fake data: the list `ROWS` stands in for a CSV and the
function `synthesize()` stands in for a TTS engine.

The script showcases how to build a package that can update a deck that already exists
in a user's collection. The script uses constants so that every identifier that Anki
matches on stays the same between runs:

- Notetypes are matched by id, so the id is a constant chosen once.
- Notes are matched by guid. `guid_for()` derives it from the word alone, so a
  corrected translation updates the note, while a guid derived from the whole row
  would fork it.
- Decks are matched by name.

A word without audio gets no listening card, because that template's front is empty
for it.
"""

import sys
from pathlib import Path

from genanki2 import Note, deck, guid_for, note, notetype, package

###### Stable IDs / Names
# Any number works, as long as it never changes
WORD_NOTETYPE_ID = 1_759_600_000_000  # This one is a time in milliseconds
ITALIAN_VOCABULARY_DECK = "Italian::Vocabulary"

WORD_CSS = ".card { font-family: sans-serif; font-size: 28px; text-align: center; }"

###### Fake data
# word, translation, part of speech, whether TTS produced audio
ROWS: list[tuple[str, str, str, bool]] = [
  ("casa", "house", "noun", True),
  ("mangiare", "to eat", "verb", True),
  ("rapidamente", "quickly", "adverb", False),
]


def synthesize(text: str, path: Path) -> Path:
  """Stand in for a TTS engine writing an mp3."""
  path.write_bytes(f"audio for {text}".encode())
  return path


def main(path: Path) -> None:
  pkg, collection = package()

  word = notetype(
    "Italian word",
    ["Italian", "English", "Audio"],
    [
      ("Recognition", "{{Italian}}", "{{FrontSide}}<hr id=answer>{{English}}"),
      ("Recall", "{{English}}", "{{FrontSide}}<hr id=answer>{{Italian}}"),
      ("Listening", "{{Audio}}", "{{FrontSide}}<hr id=answer>{{Italian}}"),
    ],
    id=WORD_NOTETYPE_ID,  # Needs to be the same between runs
    css=WORD_CSS,
  )
  vocabulary = deck(
    ITALIAN_VOCABULARY_DECK,  # Needs to be the same between runs
    description="Common words, with recordings.",
  )

  # Add to the package all media files (in this example fake files are created)
  audio_dir = path.parent / "audio"
  audio_dir.mkdir(exist_ok=True)
  for italian, _, _, has_audio in ROWS:
    if has_audio:
      mp3_path = synthesize(italian, audio_dir / f"it_{italian}.mp3")
      pkg.media.append(str(mp3_path))

  # Create all the notes
  notes: list[Note] = []
  for italian, english, part_of_speech, has_audio in ROWS:
    audio = f"[sound:it_{italian}.mp3]" if has_audio else ""
    fields = [italian, english, audio]
    guid = guid_for(word.id, italian)  # Needs to be the same between runs
    notes.append(note(word, fields, tags=[part_of_speech], guid=guid))

  collection.add_notes(notes, vocabulary)
  pkg.write_apkg(path)


if __name__ == "__main__":
  main(Path(sys.argv[1]))
