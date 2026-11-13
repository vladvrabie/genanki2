# Updating a package

Read the package, change it, and write a new version. No need to hardcode ids: the identifiers come back out of the file, so the new version updates the notes that users already have.

The examples on this page start from the package that [Building a deck](building.md) writes:

```python
from genanki2 import Package, guid_for, note

pkg = Package.read_apkg("italian.apkg", keep_source_media=True)
collection = pkg.collection
word = collection.get_notetype(name="Italian word")
vocabulary = collection.get_deck(name="Italian::Vocabulary")
casa = collection.get_note(guid=guid_for(word.id, "casa"))
```

## Changing notes

Notes override the subscript operator `[]`, which allows editing field values using their name:

```python
casa["English"] = "house, home"
```

genanki2 keeps track of modifications applied to notes in order to update the modification time during the write process. Anki imports a note only when the incoming note's modification time is more recent than the one in the collection.

The following snippet illustrates how this works by updating the time manually:

```python
print(casa.modification_time_s)
casa["English"] = "house, home"
casa.update_modification_time()
print(casa.modification_time_s)
```

An unchanged note keeps the time it was read with, so a user's own edit survives the import. A changed note replaces the user's copy, overwriting their edits.

The same mechanism applies to a notetype whose template text, styling, or field and template names change. Changing which fields and templates it has is different, as the next section explains.

The time has a resolution of one second, so a change written in the same second as the read, or as an earlier write that stamped the note, cannot be newer, and the write warns about it. A later write stamps it. A `modification_time_s` you set yourself is written as set.

This mechanism can be disabled with the `timestamping_disabled()` context manager (see [Timestamping](settings.md#timestamping)):

```python
from genanki2 import timestamping_disabled

casa["English"] = "house, home"
with timestamping_disabled():
  pkg.write_apkg("italian-v2.apkg")
```

Automatic timestamping is on by default and can be reenabled using the `timestamping_enabled()` context manager.

## Changing a notetype's fields or templates

Once a notetype has shipped (your shared deck is imported by users), its lists of fields and templates are fixed. Adding, removing or reordering a field or a template makes the import add a second copy of the notetype. The notes that users already have stay on the old notetype and are not updated, and new notes go to the copy.

Users can avoid this, by ticking "Merge note types" while importing, which is off by default. genanki2's write process warns when a notetype read from the package changed this way.

Renaming a field or a template can be safe under certain circumstances: if they have been stamped with ids. `None` means they have no id. (Anki's import matches them by id and falls back to the name only when an id is missing.)

```python
for template in word.templates:
  print(template.name, template.config.id if template.config.id is not None else "None")
```

When first building a package, it is highly recommended to call `Notetype.fill_missing_ids()` on each notetype to set ids on fields and templates. Call it **only in the first build** and read the package from that point on to make updates! Rebuilding the package generates random ids again. Leave it out (by commenting the call or putting it behind a flag) in a script that rebuilds an already shipped deck because later imports duplicate the notetype.

```python
parser = argparse.ArgumentParser()
parser.add_argument(
  "--first-build", action="store_true", help="Give fields and templates ids."
)
args = parser.parse_args()

word = notetype(...)
if args.first_build:
  word.fill_missing_ids()
```

## Adding notes

New notes are added with `collection.add_notes()` and are studied after every card already in the deck:

```python
finestra = note(word, ["finestra", "window", ""], guid=guid_for(word.id, "finestra"))
collection.add_notes([finestra], vocabulary)
```

See [Building a deck - add notes to the collection](building.md#5-add-the-notes-to-the-collection) for details.

## Cards after an edit

A card is not generated when a template's front side is empty.

An edit (like adding a field value or editing the template itself) can make a template's front non-empty. Use `collection.generate_missing_cards()` to make the cards that notes did not produce before:

```python
pkg.media.append("audio/it_finestra.mp3")
finestra["Audio"] = "[sound:it_finestra.mp3]"
collection.generate_missing_cards()
```

Filling a field is a change on a note read from a package, so the note is written with the current time and the import brings in the field along with the new card.

Cards are not removed automatically if edits make a template's front empty (in Anki they stay until the user runs Tools > Empty Cards). Use `collection.remove_empty_cards()` to remove them from the package.

```python
finestra["Audio"] = ""
collection.remove_empty_cards()
```

## Changing cards

Anki's import never updates a card that a user already has: it skips them. Moving such a card to another deck, suspending, flagging or repositioning it reaches only users who import the card for the first time, and genanki2's write process warns about it.

## Changing decks and presets

An import matches a deck by name (a renamed deck arrives as a new one, see [Identifiers that keep a deck updatable](concepts.md#identifiers-that-keep-a-deck-updatable)) and updates the deck that the user already has, whatever the modification times:

- its description, unless the incoming one is empty;
- its preset, when the user ticks "Import any deck presets" and the incoming deck does not use the default preset;
- the daily limits that the incoming deck sets, when the user ticks "Import any learning progress".

```python
from genanki2 import deck_config

vocabulary.kind.description = "Common words and phrases."
vocabulary.config = deck_config("Italian, slow", id=1_759_600_000_100, new_per_day=5)
```

The import never changes a preset the user already has: a preset whose options changed reaches only users who do not have it yet (see [Deck presets](building.md#deck-presets)). To ship new options, give the deck a new preset with an id of its own.

## Removing notes

```python
note_to_remove = collection.get_note(guid=guid_for(word.id, "mangiare"))
collection.remove_notes([note_to_remove])
```

`collection.remove_notes([...])` removes notes with their cards and reviews (unlike `add_notes()`, this method alters 3 collection lists: `notes`, `cards` and `reviews`). An import never deletes anything, though: users who imported an earlier version keep the removed notes until they delete them themselves.

## Renaming media

`pkg.replace_media_references()` renames a file wherever the collection refers to it: in note fields, and in a notetype's CSS and templates for names beginning with `_`. The file on disk is not touched and the path in `pkg.media` is not updated:

```python
from pathlib import Path

Path("audio/it_acqua.mp3").rename("audio/acqua.mp3")
pkg.media.remove("audio/it_acqua.mp3")
pkg.media.append("audio/acqua.mp3")
pkg.replace_media_references({"it_acqua.mp3": "acqua.mp3"})
```

A package can hold a media name which Anki would not store. Reading it stores the file under a repaired name and emits a warning containing a map with renames to pass to this method. With `repair_media_references=True`, `read_apkg()` applies them itself. [Media references](repairs.md#media-references) lists the other calls that rename references.

## Writing the new version

```python
pkg.trim_unused_media()
pkg.write_apkg("italian-v2.apkg")
```

If using the read-update-write flow, use `italian-v2.apkg` for the next round of updates. `examples/read_update_write.py` illustrates the whole update seen here.
