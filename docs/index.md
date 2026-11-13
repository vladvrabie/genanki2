# genanki2

Generate, read and update Anki decks using Python.

genanki2 reads and writes `.apkg`/`.colpkg` packages using Anki's latest collection schema (v18). It is a spiritual successor to [`genanki`](https://github.com/kerrickstaley/genanki), which has been unmaintained for a few years and uses the old v11 schema.

Coming from genanki? See [Migrating from genanki](migrating-from-genanki.md).

#### Highlights:

⭐ Generate packages: simple-to-use API, inspired by genanki, while fixing many of its issues.

⭐ Read packages: inspect, update and fix your packages. Extract media files to disk for easy management.

⭐ No more hardcoded ids: build a package _once_, genanki2 generates unique ids for you. Then read, update and write again a new version: ids and guids come back out of the file, so your script doesn't have to hardcode them anymore. The next import updates the decks that users already have instead of adding a duplicate.

⭐ Comprehensive checks and repairs: genanki2 refuses to write what Anki would reject or change on import and warns about possible mistakes.

## Installation

```bash
pip install genanki2
# or
uv add genanki2
```

genanki2 needs Python 3.10 or newer. No Anki install required!

## Quick start

Build a deck and write it:

```python
from genanki2 import deck, guid_for, note, notetype, package

# The package holds the media files, the collection holds everything else
pkg, collection = package()

# A notetype: its fields, and a (name, front, back) template per card type
word = notetype(
  name="Italian word",
  fields=["Italian", "English", "Audio"],
  templates=[
    ("Recognition", "{{Italian}}", "{{FrontSide}}<hr id=answer>{{English}}"),
    ("Recall", "{{English}}", "{{FrontSide}}<hr id=answer>{{Italian}}"),
    ("Listening", "{{Audio}}", "{{FrontSide}}<hr id=answer>{{Italian}}"),
  ],
  id=1_759_600_000_000,  # a fixed id, so a rebuild updates the notetype
)

# guid_for() derives a note's guid from a key that stays the same on every build
notes = [
  note(word, ["casa", "house", ""], tags=["noun"], guid=guid_for(word.id, "casa")),
  note(word, ["cane", "dog", ""], tags=["noun"], guid=guid_for(word.id, "cane")),
]

# "::" makes a subdeck
vocabulary = deck("Italian::Vocabulary")

# Two cards per note: "Listening" is skipped because "Audio" is empty
collection.add_notes(notes, vocabulary)
pkg.write_apkg("italian.apkg")
```

Open `italian.apkg` with File > Import in Anki.

Later, update the deck by reading the package back:

```python
from genanki2 import Package, guid_for, note

pkg = Package.read_apkg("italian.apkg", keep_source_media=True)
collection = pkg.collection

# Find objects by name or identifier
word = collection.get_notetype(name="Italian word")
casa = collection.get_note(guid=guid_for(word.id, "casa"))
vocabulary = collection.get_deck(name="Italian::Vocabulary")

# Set fields by name
casa["English"] = "house, home"

# Update notes with new media files and generate missing cards
pkg.media.append("audio/it_casa.mp3")
casa["Audio"] = "[sound:it_casa.mp3]"
collection.generate_missing_cards()  # Adds the missing "Listening" card

# Add new notes
finestra = note(word, ["finestra", "window", ""], guid=guid_for(word.id, "finestra"))
collection.add_notes([finestra], vocabulary)

pkg.write_apkg("italian-v2.apkg")
```

Anyone who imports `italian-v2.apkg` over the first version gets the corrected note and three new cards, and keeps the schedule of the cards they were already studying.

## Guide

- [Concepts](guide/concepts.md): what is in a package and which identifiers matter to avoid duplicates when reimporting a deck.
- [Building a deck](guide/building.md): notetypes, notes, cards, media, builtins, cloze notetypes, deck presets and filtered decks.
- [Reading a package](guide/reading.md): reading a package with or without its media, finding what it holds, and using collection packages (aka backups).
- [Updating a package](guide/updating.md): changing, adding and removing notes, renaming media, and what an import does with each change.
- [Checks and warnings](guide/checks-and-warnings.md): how they run and what they report about.
- [Settings](guide/settings.md): turning the checks, warnings, repairs and timestamping off.
- [Repairs](guide/repairs.md): which values genanki2 repairs automatically, which repairs are opt in and what each warning asks for.

[Collection schema v18](reference/schema-v18.md) describes the database inside a package, table by table.
