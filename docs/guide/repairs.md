# Repairs

genanki2 changes automatically some of the values it is given or it reads. Most of these changes would happen anyway: Anki silently updates the value on an import, on a save or when "Check Database" is run. Any genanki2 repair that mirrors what "Check Database" does emits a warning.

Other repairs available in genanki2 are opt in. Many warnings and errors name the call, or the edit, that makes them go away.

This page lists the automatic and opt in repairs and how to solve errors and warnings. The reasons behind each automatic repair, and where Anki makes the same one, are in [Repairs that alter what they read](../reference/for-contributors/repairs.md).

## Automatic repairs

### Normalized values on save

These run whenever a value is set, whether by the caller or by a read, and they **never warn**. Anki would make each change itself, so the package imports the same either way.

- **Normalizing names and text:** deck names, tags, notetype names, field names and template names;
- **`notes.sfld` and `csum`:** a note's sort field and checksum are recomputed from its fields on every write;
- **Out-of-range preset values:** 18 preset scalars go back to their default when set outside the range that Anki accepts. The learning and relearning steps are clamped to the nearest bound.
- **Unreadable dues and durations:** `Card.due` and `Card.original_due` outside the 32-bit signed range, and `Review.time_ms` outside the 32-bit unsigned range, become `0`, which is what Anki reads in their place.
- **A normal deck's preset id:** a deck's `Deck.config.id` (if set) has priority over `Deck.kind.config_id`, which gets overwritten;
- **Normalizing media file names:** extraction and `keep_source_media=True` store each file under the normalized name (using `normalize_media_name()`), with a suffix when two names repair to the same one. The references in notes, CSS and templates are not updated and a warning lists the renames to apply (see [Media references](#media-references) below).

A validator runs when a field is assigned, but there are some edge cases. One such case is mutable containers which don't trigger them, so a list changed in place is not repaired. Examples:

- `DeckConfig.config.learn_steps.append(...)` skips the clamp and the write refuses the steps with a message asking to assign the list back.
- Changing `Deck.kind.config_id` directly skips the preset id copy and the write refuses the deck until `Deck.sync_normal_kind_config_id()` runs.

### Anki's "Check Database"

Anki stores these values until the user runs Tools > Check Database. genanki2 repairs them when set or read and warns each time about it.

| Value                                                              | Repair                                | Method that makes the same repair           |
| ------------------------------------------------------------------ | ------------------------------------- | ------------------------------------------- |
| `Card.due >= 2 000 000` on a new card                              | wrapped into `[1 000 000, 1 999 999]` | `Card.repair_due_above_ceiling()`           |
| `Card.original_due` on a card with no original deck                | cleared                               | `Card.repair_unexplained_original_due()`    |
| `Card.original_deck` and `original_due` on a card in a normal deck | both cleared, again at write time     | `Card.repair_unexplained_original_data()`   |
| `Card.template_ord > 30 000`                                       | clamped to `30 000`                   | `Card.repair_template_ord_above_ceiling()`  |
| `Card.interval_days > 2 147 483 647`                               | clamped to `2 147 483 647`            | `Card.repair_interval_days_above_ceiling()` |
| `Note.modification_time_s` in the future                           | set to now                            | `Note.repair_modification_time()`           |
| `Review.time_ms > 2 147 483 647`                                   | clamped to `2 147 483 647`            | `Review.repair_time_above_ceiling()`        |

`dbcheck_repairs_disabled()` keeps the values unchanged. The block has to cover the read, the update and the write because a validator repairs a value on any assignment to its model and the original deck is cleared again at write time:

```python
from genanki2 import Package, dbcheck_repairs_disabled

with dbcheck_repairs_disabled():
  pkg = Package.read_apkg("italian.apkg", keep_source_media=True)
  ...  # the update
  pkg.write_apkg("italian-v2.apkg")
```

A kept value warns at write time and names the method in the table. A repair called inside the block works, so specific fixes can be applied. `dbcheck_repairs_enabled()` turns the repairs back on inside a disabled block.

### Repairs at write time

Writing a package or a collection first collects every object referenced by another object: notes, decks, notetypes, etc. When a deck references preset 1 and the collection does not hold it, the builtin `DEFAULT_DECK_CONFIG` is added. A card's original deck and due are checked again once the decks are final, as in the table above.

## Opt in repairs

Nothing in this section is called inside genanki2.

### Media references

Reading a package can rename media files (see above), and a write refuses a name Anki would not store, listing the name each file should have. In both cases the references to the old names need the same rename.

| Call                                                                                                                                              | What it changes                                                                                            |
| ------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `Package.replace_media_references(renames)`                                                                                                       | every reference to a renamed file: note fields, and names beginning with `_` in notetype CSS and templates |
| `Package.read_apkg(..., repair_media_references=True)`                                                                                            | applies the renames the read made before returning, instead of warning                                     |
| `Note.replace_media_references(renames)`, `Notetype.replace_underscored_css_imports(renames)`, `Template.replace_underscored_references(renames)` | the same rename for one note, stylesheet or template                                                       |
| `Package.trim_unused_media()`                                                                                                                     | drops every file nothing references, like Anki's Tools > Check Media                                       |

A read that stores a file under a new name prints the renames as a map that `replace_media_references()` can take:

```python
pkg = Package.read_apkg("shared.apkg", keep_source_media=True)
# Genanki2Warning: MediaEntries: media map contains filenames that Anki would not
# store. [...] Pass this mapping to `genanki2.Package.replace_media_references()`:
# {
#   'casa?.mp3': 'casa.mp3',
# }
pkg.replace_media_references({"casa?.mp3": "casa.mp3"})
```

`repair_media_references=True` makes the same call during the read, without the warning:

```python
pkg = Package.read_apkg(
  "shared.apkg", keep_source_media=True, repair_media_references=True
)
```

### Cards and notes

| Call                                                                                                                                                                                                                                                                                     | What it changes                                                                                                                    | Anki equivalent                           |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------- |
| `Collection.generate_missing_cards(deck=None)`                                                                                                                                                                                                                                           | adds the cards that a note renders and does not have yet, for example after filling a field that a template's front needs          | the cards Anki adds when a note is edited |
| `Collection.remove_empty_cards(keep_notes=False)`                                                                                                                                                                                                                                        | removes the cards that a note no longer renders, with their reviews, and a note whose cards are all empty unless `keep_notes=True` | Tools > Empty Cards                       |
| `Collection.repair_next_new_card_position()`                                                                                                                                                                                                                                             | moves the new-card position counter past the highest position a new card holds, so added notes are studied after the existing ones | Check Database                            |
| `Card.repair_due_above_ceiling()`, `Card.repair_unexplained_original_due()`, `Card.repair_unexplained_original_data()`, `Card.repair_template_ord_above_ceiling()`, `Card.repair_interval_days_above_ceiling()`, `Note.repair_modification_time()`, `Review.repair_time_above_ceiling()` | the Check Database repairs above, for one card, note or review                                                                     | Check Database                            |

### Decks and presets

| Call                                       | What it changes                                                                                                                           | Anki equivalent                                                           |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| `Collection.collect_default_deck()`        | adds the builtin `DEFAULT_DECK` when no deck has id 1                                                                                     | none: Anki cannot study a collection without it                           |
| `Collection.repair_missing_deck_configs()` | points every normal deck whose preset the collection does not hold at preset 1                                                            | the fallback Anki makes each time it reads such a deck, without saving it |
| `DeckConfig.repair_unsearchable_name()`    | drops the double quotes and trailing backslashes that keep the FSRS optimizer from finding the preset's cards, and warns on an empty name | none                                                                      |

### Notetypes and templates

| Call                               | What it changes                                                                                                 | Anki equivalent                                           |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| `Notetype.fill_missing_ids()`      | gives every field and template without an id a random one, so a later rename is matched by id                   | the id Anki gives a field or template when it creates one |
| `Notetype.repair_sort_field_idx()` | clamps a sort field index past the last field to the last field                                                 | the same clamp on save                                    |
| `Template.repair_target_deck_id()` | clears a deck override, so the template's cards go to the deck of their siblings; an override to deck 1 is kept | none                                                      |

`fill_missing_ids()` generates new ids on every run. Call it only in the first build and read the package from then on: a script that rebuilds an already imported deck and calls it each time duplicates the notetype on the next import.

## Solving warnings and errors

Each row indicates the class that raises the message, whether it is a warning or an error and how the message starts. The messages are raised during writing, if not mentioned otherwise. `N` stands for a number and `X` for a name or a list.

### Media

| Class                                                           | Type               | Message                                                                               | What to do                                                                                                                                                                                                                   |
| --------------------------------------------------------------- | ------------------ | ------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `MediaEntries`                                                  | warning, on a read | `media map contains filenames that Anki would not store`                              | pass the printed mapping to `Package.replace_media_references()`, or read with `repair_media_references=True`                                                                                                                |
| `MediaEntries`                                                  | warning, on a read | `media map contains duplicate filenames`                                              | rename the files, then rename their references with `Package.replace_media_references()`                                                                                                                                     |
| `Package`                                                       | warning, on a read | `the package carries N media files`                                                   | read with `extract_dir` or `keep_source_media=True`                                                                                                                                                                          |
| [`Package`](checks-and-warnings.md#package)                     | error              | `not all media file names are normalized`                                             | rename each file on disk to the name the listing gives, then rename the references with `Package.replace_media_references()`                                                                                                 |
| [`Package`](checks-and-warnings.md#package)                     | error              | `some notes, notetypes or templates reference media files the package does not carry` | add each file to `media`, or correct the reference; when the source package stores the file under a repaired name, the message prints the mapping for `Package.replace_media_references()` or `repair_media_references=True` |
| [`CollectionPackage`](checks-and-warnings.md#collectionpackage) | warning            | `some notes, notetypes or templates reference media files the package does not carry` | add each file to `media`, or correct the reference; when the source package stores the file under a repaired name, the message prints the mapping for `Package.replace_media_references()` or `repair_media_references=True` |
| [`Package`](checks-and-warnings.md#package)                     | warning            | `no note references media files X`                                                    | normalize the references with `normalize_media_name()` to see which files are unused, then `Package.trim_unused_media()`                                                                                                     |

### Cards, notes and reviews

| Class                                     | Type                                                       | Message                                                                                                | What to do                                                                    |
| ----------------------------------------- | ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------- |
| [`Card`](checks-and-warnings.md#card)     | warning                                                    | `new card position N is at or above 2,000,000 and was not repaired`                                    | `Card.repair_due_above_ceiling()`                                             |
| [`Card`](checks-and-warnings.md#card)     | warning                                                    | `original due N should be cleared`                                                                     | `Card.repair_unexplained_original_due()`                                      |
| [`Card`](checks-and-warnings.md#card)     | warning                                                    | `original deck N and original due N should be cleared`                                                 | `Card.repair_unexplained_original_data()`                                     |
| [`Card`](checks-and-warnings.md#card)     | warning                                                    | `template ordinal N is above 30,000 and was not repaired`                                              | `Card.repair_template_ord_above_ceiling()`                                    |
| [`Card`](checks-and-warnings.md#card)     | warning                                                    | `interval of N days is above 2**31 - 1 and was not repaired`                                           | `Card.repair_interval_days_above_ceiling()`                                   |
| [`Card`](checks-and-warnings.md#card)     | warning                                                    | `card N is typed as review or relearning but carries no ease factor`                                   | set the factor to the one the card would have graduated with, 2500 by default |
| [`Card`](checks-and-warnings.md#card)     | error                                                      | `card N counts due in days since the collection was created, but N holds what looks like a timestamp`  | compute the day count with `Collection.days_elapsed_since_creation`           |
| [`Note`](checks-and-warnings.md#note)     | warning                                                    | `modification time is in the future (N), now is N, and was not repaired`                               | `Note.repair_modification_time()`                                             |
| [`Note`](checks-and-warnings.md#note)     | warning                                                    | `note id=N has cloze markup in X, but note type "X" is not a cloze note type`                          | use a cloze notetype, or remove the markup                                    |
| [`Review`](checks-and-warnings.md#review) | warning                                                    | `entry N is typed as a learning review but took no time and pressed no button`                         | set `ReviewType.RESCHEDULED`, or add the time and the button                  |
| [`Review`](checks-and-warnings.md#review) | warning                                                    | `entry N took N ms, which is above 2**31 - 1, and was not repaired`                                    | `Review.repair_time_above_ceiling()`                                          |
| `Collection`                              | warning, from `add_notes()` and `generate_missing_cards()` | `the new-card position counter (N) is at or below the highest position an existing new card holds (N)` | call `Collection.repair_next_new_card_position()` before adding the notes     |

### Decks, presets and notetypes

| Class                                                                                | Type                                                       | Message                                                                                           | What to do                                                                                                                                    |
| ------------------------------------------------------------------------------------ | ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| [`Collection`](checks-and-warnings.md#collection)                                    | warning, not for an `.apkg`                                | `no deck has id 1`                                                                                | `Collection.collect_default_deck()`                                                                                                           |
| [`Collection`](checks-and-warnings.md#collection)                                    | error, for a `.colpkg`                                     | `the collection has no deck with id 1`                                                            | `Collection.collect_default_deck()`                                                                                                           |
| [`Collection`](checks-and-warnings.md#collection)                                    | warning                                                    | `a normal deck names a preset the collection does not hold`                                       | `Collection.repair_missing_deck_configs()`, or add the missing preset                                                                         |
| [`Deck`](checks-and-warnings.md#deck)                                                | error                                                      | `deck "X" carries preset N ("X"), but its kind blob points at preset N`                           | `Deck.sync_normal_kind_config_id()`                                                                                                           |
| [`DeckConfig`](checks-and-warnings.md#deckconfig)                                    | error                                                      | `learn_steps X holds a value outside [0, N] minutes` (or `relearn_steps`)                         | assign the list back to the preset's `learn_steps` or `relearn_steps`, which clamps it                                                        |
| [`DeckConfig`](checks-and-warnings.md#deckconfig)                                    | warning                                                    | `"X" (id N) changed since it was read`                                                            | give the preset a new id with `monotonic_id()` and move the decks to it, since an import keeps the preset a user already has under the old id |
| [`Note`](checks-and-warnings.md#note), [`Notetype`](checks-and-warnings.md#notetype) | warning                                                    | `changed since its last stamp, but its modification time (N) is not later than that stamp (N)`    | set `modification_time_s` to a later time                                                                                                     |
| [`DeckConfig`](checks-and-warnings.md#deckconfig)                                    | warning                                                    | `"X" has a name Anki cannot search for`                                                           | `DeckConfig.repair_unsearchable_name()`                                                                                                       |
| [`Notetype`](checks-and-warnings.md#notetype)                                        | warning                                                    | `"X" has a sort field index (N) that exceeds the number of fields (N)`                            | `Notetype.repair_sort_field_idx()`                                                                                                            |
| [`Template`](checks-and-warnings.md#template)                                        | warning                                                    | `"X" sets a deck override (target_deck_id=N)`                                                     | point the override at a normal deck in the collection, or `Template.repair_target_deck_id()`                                                  |
| `Collection`                                                                         | warning, from `add_notes()` and `generate_missing_cards()` | `Template "X" overrides the deck with id N, which is not in the collection or is a filtered deck` | point the override at a normal deck in the collection, or `Template.repair_target_deck_id()`                                                  |

### Collection settings and file names

| Class                                             | Type                         | Message                                                              | What to do                                                           |
| ------------------------------------------------- | ---------------------------- | -------------------------------------------------------------------- | -------------------------------------------------------------------- |
| [`Collection`](checks-and-warnings.md#collection) | warning, not for an `.apkg`  | ``the `schedVer` config entry is N``                                 | `ConfigEntry(key="schedVer", value=b"2")`                            |
| [`Collection`](checks-and-warnings.md#collection) | error, for a `.colpkg`       | ``the collection's `schedVer` config entry is N``                    | `ConfigEntry(key="schedVer", value=b"2")`                            |
| [`Collection`](checks-and-warnings.md#collection) | warning, not for an `.apkg`  | ``the `sched2021` config entry is missing or does not hold `true` `` | `ConfigEntry(key="sched2021", value=b"true")`                        |
| `Package`                                         | error, from `write_apkg()`   | `Invalid file name: Anki reads "X" as a whole collection`            | choose another name, or write a `CollectionPackage`                  |
| `CollectionPackage`                               | error, from `write_colpkg()` | `Invalid file name: "X" does not end in ".colpkg"`                   | add the extension, or use `Package.write_apkg()` for a deck to share |
