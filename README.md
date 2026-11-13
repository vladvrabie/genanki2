# genanki2

![pypi](https://img.shields.io/badge/pypi-unreleased-lightgrey)
![versions](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14%20%7C%203.15-blue)
[![license](https://img.shields.io/badge/license-MIT-blue)](https://github.com/vladvrabie/genanki2/blob/main/LICENSE)
[![Pydantic v2](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/pydantic/pydantic/main/docs/badge/v2.json)](https://pydantic.dev)
[![anki schema](https://img.shields.io/badge/anki%20schema-v18-blue)](https://github.com/vladvrabie/genanki2/blob/main/docs/reference/schema-v18.md)

Generate, read and update Anki decks using Python.

genanki2 reads and writes `.apkg`/`.colpkg` packages using Anki's latest collection schema (v18). It is a spiritual successor to [`genanki`](https://github.com/kerrickstaley/genanki), which has been unmaintained for a few years and uses the old v11 schema. Short migration guide [here](#migrating-from-genanki).

Highlights:

⭐ Generate packages: simple-to-use API, inspired by genanki, while fixing many of its issues.

⭐ Read packages: inspect, update and fix your packages. Extract media files to disk for easy management.

⭐ No more hardcoded ids: build a package _once_, genanki2 generates unique ids for you. Then read, update and write again a new version: ids and guids come back out of the file, so your script doesn't have to hardcode them anymore. The next import updates the decks that users already have instead of adding a duplicate.

⭐ Comprehensive checks and repairs: genanki2 refuses to write what Anki would reject or change on import and warns about possible mistakes.

Create and write a package:

```python
from genanki2 import BASIC_NOTETYPE, deck, note, package

pkg, collection = package()
collection.add_notes(
  [note(BASIC_NOTETYPE, ["Capital of Italy?", "Rome"])],
  deck("Geography"),
)
pkg.write_apkg("geography.apkg")
```

Open `geography.apkg` with File > Import in Anki.

## Tutorial

Start with an empty package. `package()` returns two objects:

- the package (`pkg`) holds the media files and is used for reading/writing to disk;
- the collection (`collection`) holds the decks, notetypes, notes and cards.

```python
from genanki2 import deck, guid_for, note, notetype, package

pkg, collection = package()
```

Define the notetype (`name`, `fields` and `templates` are positional). A template can be defined as a tuple of a template name, a front and a back:

```python
word = notetype(
  name="Italian word",
  fields=["Italian", "English", "Audio"],
  templates=[
    ("Recognition", "{{Italian}}", "{{FrontSide}}<hr id=answer>{{English}}"),
    ("Recall", "{{English}}", "{{FrontSide}}<hr id=answer>{{Italian}}"),
    ("Listening", "{{Audio}}", "{{FrontSide}}<hr id=answer>{{Italian}}"),
  ],
  id=1_759_600_000_000,
)
# word.fill_missing_ids()  # First build only: later builds read back these ids
```

> `Notetype.fill_missing_ids()` lets Anki recognise a renamed field or template. Call it only in the first build and read the package from then on because it generates random ids. Leave it out in a script that rebuilds an already imported deck because later imports duplicate the notetype.

Create a deck and the notes. Use `::` in a deck name to make a subdeck and `guid_for` to derive a note's guid from a fixed key:

```python
vocabulary = deck("Italian::Vocabulary")
notes = [
  note(word, ["casa", "house", ""], tags=["noun"], guid=guid_for(word.id, "casa")),
  note(word, ["cane", "dog", ""], tags=["noun"], guid=guid_for(word.id, "cane")),
]
```

> Anki recognises a notetype by its id, a note by its guid and a deck by its name. Keep them the same on every build, otherwise reimporting it makes a second copy next to the first. Without `id=` and `guid=`, genanki2 creates new ones on each run.

Add the notes, then write the package. A note gets one card per template whose front has something to show, so these notes get two cards each and no "Listening" card until their "Audio" field is filled:

```python
collection.add_notes(notes, vocabulary)
pkg.write_apkg("italian.apkg")
```

To update the package, start from the last written file. The identifiers (ids, guids) are read out of it, so updating needs no constants. Correct a translation and add a word:

```python
from genanki2 import Package

pkg = Package.read_apkg("italian.apkg", keep_source_media=True)
collection = pkg.collection

# Find items using their identifiers or names
word = collection.get_notetype(name="Italian word")
vocabulary = collection.get_deck(name="Italian::Vocabulary")

casa = collection.get_note(guid=guid_for(word.id, "casa"))
# Use the subscript operator to update fields by name
casa["English"] = "house, home"

finestra = note(word, ["finestra", "window", ""], guid=guid_for(word.id, "finestra"))
collection.add_notes([finestra], vocabulary)

pkg.write_apkg("italian-v2.apkg")
```

Anyone who imports `italian-v2.apkg` over the first version gets the corrected note and two new cards, and keeps the schedule of the cards they were already studying.

## Installation

```bash
pip install genanki2
# or
uv add genanki2
```

genanki2 needs Python 3.10 or newer. No Anki install required!

## Reading and updating decks

A package you read carries the identifiers of its notetypes and notes, so what you write from it updates what users already have. The snippets in this section and in [Features](#features) continue from the tutorial.

### Reading packages

`Package.read_apkg()` reads packages, validating every table. It does not read Anki's "Support older Anki versions" export, which uses the old collection format.

Media files can be:

- extracted to a folder using `extract_dir="media/"`; the extracted media paths are added to the `Package.media` list.
- copied directly from the read package using `keep_source_media=True`; it leaves them in the source file and during the next write it copies them from there. Modifying or overwriting the read package is not allowed (the next write raises).

With neither, the package is read without its media, with a warning.

The collection's lists hold everything that was read, lookups find single objects, and a note's fields are read and set by name:

```python
coll = Package.read_apkg("italian.apkg", extract_dir="media/").collection

coll.get_deck(name="Italian::Vocabulary")
coll.get_notetype(name="Italian word")
for note_ in coll.notes:
  print(note_["Italian"], note_.tags)
```

### Managing the collection

These methods work the same on a package you read and on a new one:

| Method                                              | Result                                                                                                                                                                                                                                                                                        |
| --------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `get_note()`                                        | the note, or `None`, using `guid=` or `id=`                                                                                                                                                                                                                                                   |
| `get_notetype()`, `get_deck()`, `get_deck_config()` | the notetype, deck or preset, or `None`, using `name=` or `id=`                                                                                                                                                                                                                               |
| `add_notes(notes, deck)`                            | makes the notes' cards in the deck, adds them to `collection.cards` and returns them; the notes and the deck join the collection's lists during the write, or earlier with `collect_references()` (despite the name, `add_notes()` does not update `collection.notes` and `collection.decks`) |
| `remove_notes(notes)`                               | removes the notes, their cards and those cards' reviews                                                                                                                                                                                                                                       |
| `generate_missing_cards()`                          | makes the cards that edited notes now produce and did not before                                                                                                                                                                                                                              |
| `remove_empty_cards()`                              | removes the cards that notes no longer produce, like Tools > Empty Cards in Anki, and any note left with none                                                                                                                                                                                 |

### Updating decks

An import:

- replaces the user's copy of a changed note or notetype (overwriting the user's edits);
- adds new notes and cards;
- never deletes anything;
- never updates the cards a user already has; genanki2 emits a warning.

When updating a notetype:

- adding, removing or reordering a notetype's fields or templates makes a second copy of the notetype; genanki2 emits a warning.
- renaming is safe once fields and templates have ids, so call `Notetype.fill_missing_ids()` before the first release.

See [Updating a package](https://github.com/vladvrabie/genanki2/blob/main/docs/guide/updating.md) for more details.

```python
pkg.media.append("audio/it_casa.mp3")
casa["Audio"] = "[sound:it_casa.mp3]"
collection.generate_missing_cards()  # casa gains its Listening card

cane = collection.get_note(guid=guid_for(word.id, "cane"))
collection.remove_notes([cane])
pkg.trim_unused_media()
pkg.write_apkg("italian-v3.apkg")
```

## Features

- [Builtins](#builtins): Anki's six stock notetypes, the default deck and preset
- [Media](#media): audio and images
- [Renaming media](#renaming-media): change a file's name and every reference to it
- [Cloze notetypes](#cloze-notetypes): one card per cloze number
- [Field and template options](#field-and-template-options): collapse a field, pick the sort field
- [Cards](#cards): change a card before it is written
- [Presets](#presets): a deck's study options
- [Filtered decks](#filtered-decks): saved searches that gather cards
- [Collection packages](#collection-packages): a whole collection or backup

### Builtins

genanki2 ships:

- `BASIC_NOTETYPE`
- `BASIC_AND_REVERSED_NOTETYPE`
- `BASIC_OPTIONAL_REVERSED_NOTETYPE`
- `BASIC_TYPE_IN_THE_ANSWER_NOTETYPE`
- `CLOZE_NOTETYPE`
- `IMAGE_OCCLUSION_NOTETYPE`
- `DEFAULT_DECK`
- `DEFAULT_DECK_CONFIG`

These constants cannot be changed. To change one, build your own copy and give an id to notetypes:

```python
from genanki2 import builtin_notetype, default_deck, default_deck_config

my_basic = builtin_notetype("Basic (genanki2)", id=1_759_600_000_003)
my_basic.config.css = ".card { font-size: 28px; }"
my_default_deck = default_deck()
my_default_deck_config = default_deck_config()
```

### Media

Add a file to the package by path and refer to it from a field by file name:

```python
pkg.media.append("audio/it_acqua.mp3")
acqua = note(
  word,
  ["acqua", "water", "[sound:it_acqua.mp3]"],
  guid=guid_for(word.id, "acqua"),
)
collection.add_notes([acqua], vocabulary)  # three cards, Listening included
```

The write raises if a note refers to a file that the package does not have and warns about redundant files. Use `pkg.trim_unused_media()` to drop those.

### Renaming media

`pkg.replace_media_references()` renames a file wherever the collection refers to it: in note fields, and in a notetype's CSS and templates for names beginning with `_`. The file and `pkg.media` are yours to update:

```python
from pathlib import Path

Path("audio/it_acqua.mp3").rename("audio/acqua.mp3")
pkg.media.remove("audio/it_acqua.mp3")
pkg.media.append("audio/acqua.mp3")
pkg.replace_media_references({"it_acqua.mp3": "acqua.mp3"})
```

A package can hold a media name the filesystem cannot take. Reading it stores the file under a repaired name and warns with the renames to pass to this method. With `repair_media_references=True`, `read_apkg()` applies them itself.

### Cloze notetypes

A cloze notetype makes one card per cloze number in the note. Pass `cloze=True`, and use the `cloze:` filter on both sides of its single template:

```python
cloze = notetype(
  "Italian cloze",
  ["Text", "Translation"],
  [("Cloze", "{{cloze:Text}}", "{{cloze:Text}}<br>{{Translation}}")],
  id=1_759_600_000_002,
  cloze=True,
)
fields = ["{{c1::Roma}} è la capitale d'{{c2::Italia}}.", "Rome is the capital of Italy."]
collection.add_notes([note(cloze, fields)], vocabulary)  # two cards
```

### Field and template options

Use `field()` and `template()` for fine-grained customization. Their keyword arguments are Anki's options. Set `sort_field` to pick the field that the browser shows in its Sort Field column.

```python
from genanki2 import field, template

phrase = notetype(
  "Italian phrase",
  ["Italian", "English", field("Notes", collapsed=True, plain_text=True)],
  [
    template(
      "Translate",
      "{{Italian}}",
      "{{FrontSide}}<hr id=answer>{{English}}",
      a_format_browser="{{English}}",
      browser_font_size=18,
    )
  ],
  id=1_759_600_000_001,
  sort_field="English",
  css=".card { font-family: sans-serif; text-align: center; }",
)
```

### Cards

`add_notes` returns the new cards, grouped per note, in the order of the notes, and can be used to further customize them:

```python
cards = collection.add_notes(
  [note(word, ["gatto", "cat", ""], guid=guid_for(word.id, "gatto"))], vocabulary
)
for recognition, recall in cards:
  recall.suspend()
```

> A card's state reaches the user only when they tick "Import any learning progress" while importing. Without it, Anki imports every card as a new card, keeping only its position.

### Presets

A preset holds a deck's study options. Decks without one use Anki's default preset. Decks that should share options share one preset object:

```python
from genanki2 import deck_config

slow = deck_config("Italian, slow", id=1_759_600_000_100, new_per_day=5)
grammar = deck("Italian::Grammar", config=slow)
```

> Anki matches presets by id and only imports them when the user ticks "Import any deck presets" while importing.

### Filtered decks

A filtered deck is a saved search that gathers cards from normal decks when the user builds it in Anki. No card refers to it, so add it to the collection's decks yourself:

```python
from genanki2 import DeckFilteredKind, filtered_deck

cram = filtered_deck(
  "Italian::Cram nouns",
  search='deck:"Italian::Vocabulary" tag:noun',
  limit=50,
  order=DeckFilteredKind.SearchTerm.Order.RANDOM,
)
collection.decks.append(cram)
```

An `.apkg` import skips decks that hold no cards, so an empty filtered deck arrives only through a [collection package](#collection-packages). See [`examples/filtered_decks.py`](https://github.com/vladvrabie/genanki2/blob/main/examples/filtered_decks.py).

### Collection packages

An `.apkg` merges into the collection of whoever imports it. A `.colpkg` replaces their whole collection, and is what Anki writes for "Export collection" and for its backups. Use `CollectionPackage.read_colpkg()` and `write_colpkg()` to change your own collection or backup. To share a deck with other people, use an `.apkg`.

## Checks, warnings and repairs

Before writing, genanki2 runs its checks and warnings over everything in the package:

- A check raises `Genanki2Error` for something Anki would reject or change on import (such as two fields with the same name).
- A warning (`Genanki2Warning`) points at something Anki accepts but that is probably a mistake (such as a note whose first field is empty).

genanki2 also repairs values. It normalizes names, tags and field text the way Anki does. A value that only Anki's Check Database would fix is repaired with a warning. [Repairs](https://github.com/vladvrabie/genanki2/blob/main/docs/guide/repairs.md) lists them, along with the repairs to opt into and what each warning asks for.

A package someone else made can hold values Anki accepts and a check refuses. Use `checks_disabled()`, `warnings_disabled()` and `dbcheck_repairs_disabled()` to turn them off. An explicit `pkg.check()`, `pkg.warn()` or repair method call inside these blocks still runs:

```python
from genanki2 import Genanki2Error, checks_disabled

collection.collect_references()  # add_notes stores only the cards until then
try:
  pkg.check()
except Genanki2Error as error:
  print(f"Writing anyway: {error}")
with checks_disabled():
  pkg.write_apkg("copy.apkg")
```

## Migrating from genanki

The overall shape stays the same: notetypes, notes, a deck, a package. There is no genanki-compatible API, so moving means rewriting the build script. This genanki script:

```python
import genanki

model = genanki.Model(
  1607392319,
  "Simple",
  fields=[{"name": "Question"}, {"name": "Answer"}],
  templates=[
    {
      "name": "Card 1",
      "qfmt": "{{Question}}",
      "afmt": "{{FrontSide}}<hr id=answer>{{Answer}}",
    }
  ],
)
capitals = genanki.Deck(2059400110, "Capitals")
capitals.add_note(genanki.Note(model=model, fields=["Capital of Italy?", "Rome"]))
genanki.Package(capitals).write_to_file("capitals.apkg")
```

becomes:

```python
from genanki2 import deck, guid_for, note, notetype, package

pkg, collection = package()
model = notetype(
  "Simple",
  ["Question", "Answer"],
  [("Card 1", "{{Question}}", "{{FrontSide}}<hr id=answer>{{Answer}}")],
  id=1607392319,
)
italy = note(model, ["Capital of Italy?", "Rome"], guid=guid_for(model.id, "Italy"))
collection.add_notes([italy], deck("Capitals"))
pkg.write_apkg("capitals.apkg")
```

Most calls map one to one:

| genanki                                         | genanki2                                                                    |
| ----------------------------------------------- | --------------------------------------------------------------------------- |
| `Model(model_id, name, ...)`                    | `notetype(name, ..., id=model_id)`                                          |
| `fields=[{"name": "Q"}]`                        | `["Q"]` or `[field("Q", ...)]`                                              |
| `templates=[{"name": n, "qfmt": q, "afmt": a}]` | `[(n, q, a)]`, or `[template(n, q, a, ...)]`                                |
| `model_type=Model.CLOZE`                        | `notetype(..., cloze=True)` or `nt.config.kind = NotetypeConfig.Kind.CLOZE` |
| `Note(model=m, fields=[...])`                   | `note(m, [...])`                                                            |
| `n.cards[1].suspend = True`                     | `cards[1].suspend()`, on the cards `add_notes` returns                      |
| `Deck(deck_id, name)`                           | `deck(name)`                                                                |
| `d.add_note(n)`                                 | `collection.add_notes([n], d)`                                              |
| `Package(d, media_files=["a.mp3"])`             | `pkg.media.append("a.mp3")`                                                 |
| `Package([d1, d2])`                             | one `package()`, with every deck in its collection                          |
| `p.write_to_file(path)`                         | `pkg.write_apkg(path)`                                                      |
| `BASIC_MODEL`, `CLOZE_MODEL`, ...               | `BASIC_NOTETYPE`, `CLOZE_NOTETYPE`, ...                                     |

Some behaviour differs:

- Without `guid`, genanki hashes the field values, so editing a field turns the note into a new one on the user's side. genanki2 picks a random guid, which is not better for a rebuild: pass `guid_for(notetype.id, key)`.
- genanki2's builtin notetypes have different ids from genanki's, so to Anki they are different notetypes.
- `write_to_file(path, timestamp=...)`, `write_to_collection_from_addon()` and `write_to_db()` have no equivalent.

### Reading genanki built decks

genanki2 can't read the file genanki wrote, because it uses the old format, so go through Anki first. Import the deck, then choose File > Export and pick **Anki Deck Package**, with **Include scheduling information** and **Support older Anki versions** unticked and **Include media** ticked. Pass that export to `Package.read_apkg(path, keep_source_media=True)`, and from then on read the package each build wrote.

The documentation page [Migrating from genanki](https://github.com/vladvrabie/genanki2/blob/main/docs/migrating-from-genanki.md) has every mapping and behaviour difference, finding the note for each row, recomputing genanki's guids and renaming fields on a genanki-built notetype.

## Examples

Runnable scripts in [`examples/`](https://github.com/vladvrabie/genanki2/tree/main/examples):

- [`new_package.py`](https://github.com/vladvrabie/genanki2/blob/main/examples/new_package.py): two decks, two builtin notetypes, a few notes
- [`language_deck.py`](https://github.com/vladvrabie/genanki2/blob/main/examples/language_deck.py): rebuild a vocabulary deck from rows of data, with audio
- [`read_update_write.py`](https://github.com/vladvrabie/genanki2/blob/main/examples/read_update_write.py): update the package `language_deck.py` wrote
- [`read_package.py`](https://github.com/vladvrabie/genanki2/blob/main/examples/read_package.py): look inside a package without changing it
- [`custom_notetypes.py`](https://github.com/vladvrabie/genanki2/blob/main/examples/custom_notetypes.py): a styled notetype and a cloze one
- [`custom_deck_presets.py`](https://github.com/vladvrabie/genanki2/blob/main/examples/custom_deck_presets.py): daily limits, learning steps, FSRS
- [`filtered_decks.py`](https://github.com/vladvrabie/genanki2/blob/main/examples/filtered_decks.py): saved searches, in a collection package
- [`settings.py`](https://github.com/vladvrabie/genanki2/blob/main/examples/settings.py): when checks, warnings and database repairs run

## License

[MIT](https://github.com/vladvrabie/genanki2/blob/main/LICENSE). Bundled Unicode data is under the [Unicode License](https://github.com/vladvrabie/genanki2/blob/main/LICENSES/UNICODE-LICENSE-V3.txt). The protobuf descriptors are Anki's, Copyright Ankitects Pty Ltd and contributors, under the [GNU AGPL, version 3 or later](https://github.com/vladvrabie/genanki2/blob/main/LICENSES/AGPL-3.0-or-later.txt).
