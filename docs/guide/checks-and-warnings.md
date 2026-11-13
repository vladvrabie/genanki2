# Checks and warnings

genanki2 validates a package (and any model) before writing it, so a problem is raised in your script, not when importing in Anki or when reviewing cards. It does so in two ways:

- A check raises `Genanki2Error` for something that Anki would reject or quietly change, or that would break later, when reviewing cards.
- A warning (`Genanki2Warning`) points at something that Anki accepts but which is probably a mistake.

This page explains when they run and how to call them by hand, then lists every check and warning by model.

## How they work

When a serialization method is called on a model, three phases run before the actual serialization:

1. **Prepare** makes the model ready to be written:
    - [Timestamping](settings.md#timestamping) edited notes and notetypes;
    - updating the collection's list with all the referenced decks, notes, notetypes, etc.;
    - running some of Anki's database check repairs;
2. **Check** runs the checks. Some checks raise if the preparation phase is skipped.
3. **Warn** runs the warnings.

> The serialization methods include: `Package.write_apkg()`, `CollectionPackage.write_colpkg()`, `Collection.write_sqlite()` and a model's `to_row()`, `to_rows()` or `to_bytes()`.

Each phase is recursive by default, reaching every model inside the serialized model and running on each of them once. Use `recursive=False` to limit them to the model's own steps. Example: a card reaches its note and its deck, `card.check()` calls all of its checks, then calls `note.check()` and `deck.check()` if `recursive` is `True`.

`checks_disabled()` and `warnings_disabled()` skip the checks and the warnings of a write (see [Settings](settings.md)). The preparation always runs, except on a frozen model because `freeze()` already prepared it.

The phases can also be run by hand:

```python
from genanki2 import Package, guid_for

pkg = Package.read_apkg("italian.apkg", keep_source_media=True)
word = pkg.collection.get_notetype(name="Italian word")
casa = pkg.collection.get_note(guid=guid_for(word.id, "casa"))

pkg.prepare().check()  # the package and every model it holds
word.check(recursive=False)  # the notetype's own checks, not its fields' or templates'
word.check_unique_fronts()  # a single check
casa.warn()  # the note's warnings, and its notetype's
```

Each check and warning in the tables below is also a public method that runs alone. A manual call to a phase or a specific method runs regardless of the settings.

A few details:

- Call `collection.prepare()` or `collection.collect_references()` before calling `check()` by hand on a collection (or the package holding it). The collection's lists hold only what was read or added to them. If some referenced objects are missing from the lists, `collection.check_referential_integrity()` will raise.

- `pkg.write_apkg()` and `colpkg.write_colpkg()` tell the phases which kind of file they write (using a `SerializationContext` parameter) because some checks and warnings apply only to one of them, as the tables indicate. During a manual call without context, the checks that apply only to a `.colpkg` are skipped, and the warnings that depend on the kind of file written describe both outcomes.

- Some values are repaired rather than refused (see [Repairs](repairs.md)). Several warnings below report a value that a repair would have changed and are emitted only when the repairs are off (inside `dbcheck_repairs_disabled()`).

## Package and media

### Package

| Check/Warning                        | Description                                                                                                                                                                           |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `check_media_paths`                  | Each media file exists, is readable, and has a name that Anki can store as it is and that no other file in `media` has. Also runs when `media` is assigned.                           |
| `check_builtin_notetype_versions`    | A builtin notetype keeps the version Anki gave it as its modification time. A larger value would overwrite the user's copy of that builtin on import.                                 |
| `check_media_references_are_present` | Every file that a note, a notetype's CSS or a template refers to is in the package. Lists the missing ones, and the renames that fix references to a source package's repaired names. |
| `warn_unused_media_files`            | A file in the package that nothing refers to. It imports, but takes space. Names beginning with `_` are exempt.                                                                       |

### CollectionPackage

| Check/Warning                   | Description                                                                                                                                                             |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `check_media_paths`             | As for `Package`.                                                                                                                                                       |
| `warn_unused_media_files`       | As for `Package`.                                                                                                                                                       |
| `warn_missing_media_references` | A file that a note, a notetype's CSS or a template refers to is not in the package. Only a warning, since a restore keeps the files already in the user's media folder. |

### MediaEntries

The media map that a package writes, built by genanki2 itself.

| Check/Warning               | Description                                                                  |
| --------------------------- | ---------------------------------------------------------------------------- |
| `check_homogeneous_entries` | The map does not mix entries from the legacy and the latest package formats. |
| `check_checksum_lengths`    | Each checksum has the length its package format requires.                    |

## Collection and its objects

### Collection

| Check/Warning                         | Description                                                                                                                                                              | File      |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------- |
| `check_unique_ids`                    | No id is used twice within one list: presets, decks, notetypes, notes, cards or reviews.                                                                                 | both      |
| `check_unique_note_guids`             | No two notes share a guid. The import matches notes by guid, so the second one would never be added.                                                                     | both      |
| `check_unique_names`                  | No two decks, and no two notetypes, have names that differ only in case. Anki would rename one of them.                                                                  | both      |
| `check_unique_tags`                   | No two tags differ only in case.                                                                                                                                         | both      |
| `check_unique_card_producers`         | No two cards come from the same note and template. Anki would delete one of them.                                                                                        | both      |
| `check_unique_graves`                 | No two graves delete the same object.                                                                                                                                    | both      |
| `check_unique_config_keys`            | No two config entries share a key.                                                                                                                                       | both      |
| `check_referential_integrity`         | Every object that another one refers to by id is in the collection, such as a card's note and deck.                                                                      | both      |
| `check_present_default_deck`          | The collection has deck 1, without which Anki cannot study it.                                                                                                           | `.colpkg` |
| `check_card_original_deck_is_normal`  | A card's original deck, given by id, is not a filtered deck.                                                                                                             | both      |
| `check_scheduler_version`             | The `schedVer` config entry is the v2 scheduler. Otherwise the restored collection cannot be studied until Anki updates it, or cannot be opened at all.                  | `.colpkg` |
| `warn_future_ids`                     | A note, card or review id more than a day in the future. Anki renumbers them, which moves the dates they stand for.                                                      | both      |
| `warn_duplicate_deck_config_names`    | Two presets with the same name. The FSRS optimizer finds a preset by name, so it optimizes all of them with the cards of one.                                            | both      |
| `warn_missing_deck_configs`           | A normal deck naming a preset the collection does not have. Anki schedules it with the default preset.                                                                   | both      |
| `warn_missing_default_deck`           | The collection has no deck 1. Only reached when the checks are off, since `check_present_default_deck` refuses it.                                                       | `.colpkg` |
| `warn_nonleaf_filtered_decks`         | A filtered deck with subdecks, which it cannot hold. An import moves them, and a restore keeps them in a state Anki cannot edit.                                         | both      |
| `warn_filtered_decks_become_normal`   | Filtered decks that an import turns into normal ones.                                                                                                                    | `.apkg`   |
| `warn_read_notetype_schemas_mismatch` | A notetype read from a package whose fields or templates were added, removed or reordered. The import adds it as a second copy and does not update the notes users have. | `.apkg`   |
| `warn_import_skips_unused`            | A deck with no cards, or a notetype with no notes. The import never creates them.                                                                                        | `.apkg`   |
| `warn_tag_spelling_mismatch`          | A tag spelled two ways, such as `Verb` and `verb`. Anki keeps the spelling it registers first.                                                                           | both      |
| `warn_scheduler_version`              | A `schedVer` config entry that is not the v2 scheduler. Only reached when the checks are off, since `check_scheduler_version` refuses it.                                | `.colpkg` |
| `warn_v3_scheduler_off`               | A collection with the v3 scheduler switched off. A restored collection cannot be studied until the user accepts Anki's update.                                           | `.colpkg` |

### Notetype

The notetype's options, `NotetypeConfig`, are checked along with it.

| Check/Warning                                 | Description                                                                                                            |
| --------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `check_non_empty_fields`                      | The notetype has at least one field.                                                                                   |
| `check_non_empty_templates`                   | The notetype has at least one template.                                                                                |
| `check_unique_ids`                            | No two fields, and no two templates, share an id. Anki follows a rename by id.                                         |
| `check_unique_names`                          | No two fields, and no two templates, have names that differ only in case.                                              |
| `check_unique_fronts`                         | No two templates have the same front, which would make two identical cards. A front referring to `{{Card}}` is exempt. |
| `check_templates_field_references`            | Every name a template refers to is a field or one of Anki's special names.                                             |
| `check_cloze_template_uses_filter`            | A cloze notetype's template uses the `cloze:` filter on both sides.                                                    |
| `check_mod_time_not_in_future`                | The modification time has already happened. A future one blocks updates until it passes.                               |
| `warn_empty_card_generation`                  | A front that can never make a card, for any note.                                                                      |
| `warn_unused_fields`                          | A field that no template shows. The sort field is exempt.                                                              |
| `warn_sort_field_idx_out_of_range`            | A sort field index past the last field. Anki clamps it on import.                                                      |
| `warn_multi_template_cloze`                   | A cloze notetype with more than one template. The extra ones make no cards.                                            |
| `warn_unimported_changes`                     | A change to a read notetype that an import skips, because its modification time is not newer than the read one.        |
| `NotetypeConfig.warn_int_value_in_enum_field` | An option holding a number its enum does not name.                                                                     |

### Field

| Check/Warning                             | Description                                        |
| ----------------------------------------- | -------------------------------------------------- |
| `FieldConfig.warn_out_of_range_font_size` | A font size too large or too small for the editor. |

### Template

| Check/Warning                    | Description                                                                                           |
| -------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `check_templates_parse`          | Each side parses: every conditional that is opened is closed, under the same name.                    |
| `check_min_front_references`     | The front refers to at least one field. Anki refuses a front that shows the same text for every note. |
| `warn_misleading_target_deck_id` | A deck override that an import breaks, since it gives decks new ids. Only for an `.apkg`.             |

### Note

| Check/Warning                  | Description                                                                                                          |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------- |
| `check_fields_len`             | The note has one value per field of its notetype.                                                                    |
| `warn_empty_checksum_field`    | An empty first field. Anki cannot detect duplicates of such a note.                                                  |
| `warn_normal_note_cloze_field` | Cloze markup in a note whose notetype is not a cloze one. It shows as written.                                       |
| `warn_cloze_on_wrong_field`    | Cloze markup in a field that the cloze template does not pass through `cloze:`. It makes a card with nothing hidden. |
| `warn_mod_time_in_future`      | A modification time in the future, which blocks updates until it passes. Only with the repairs off.                  |
| `warn_unimported_changes`      | A change to a read note that an import skips, because its modification time is not newer than the read one.          |

### Deck

The deck's kind, `DeckNormalKind` or `DeckFilteredKind`, is checked along with it.

| Check/Warning                                 | Description                                                                                                 |
| --------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `check_normal_kind_config_id`                 | A normal deck's `kind.config_id` is its preset's id. `sync_normal_kind_config_id()` copies it.              |
| `check_mod_time_not_in_future`                | The modification time has already happened. A future one blocks updates until it passes.                    |
| `warn_unimported_changes`                     | A change to a read deck that an import never applies, such as emptying its description or clearing a limit. |
| `DeckNormalKind.warn_bad_desired_retention`   | A desired retention outside 0.7 to 0.99, which Anki uses as given.                                          |
| `DeckFilteredKind.warn_too_many_search_terms` | More than two searches. Anki reads only the first two.                                                      |
| `SearchTerm.warn_int_value_in_enum_field`     | A search order that its enum does not name. Anki falls back to the first order.                             |

### DeckConfig

The preset's options, `DeckConfigInner`, are checked along with it.

| Check/Warning                                  | Description                                                                                                       |
| ---------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `check_mod_time_not_in_future`                 | The modification time has already happened. A future one blocks updates until it passes.                          |
| `warn_unsearchable_name`                       | A name that the FSRS optimizer cannot search for, when the preset sets no search of its own.                      |
| `warn_unimported_changes`                      | A change to a read preset that an import never applies, since Anki does not update a preset the user already has. |
| `DeckConfigInner.check_learning_steps`         | Each learning step is between 0 and 2\*\*31 // 60 minutes.                                                        |
| `DeckConfigInner.check_fsrs_params_length`     | The FSRS parameters have a length FSRS can take. Otherwise no deck using the preset can be studied.               |
| `DeckConfigInner.check_date`                   | `ignore_revlogs_before_date` is empty or a date.                                                                  |
| `DeckConfigInner.check_easy_days_percentages`  | `easy_days_percentages` is empty or has 7 values. Otherwise no deck can be studied.                               |
| `DeckConfigInner.warn_int_value_in_enum_field` | An option holding a number its enum does not name. Anki falls back to the option's first value.                   |

### Card

The card's `data` (a `CardData`) is checked along with it.

| Check/Warning                                 | Description                                                                                                                     |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `check_template_ordinal`                      | The card's template ordinal is one of its notetype's templates. Not checked on a cloze notetype, where it names a cloze number. |
| `check_due_not_a_timestamp`                   | A `due` that counts days does not hold a timestamp. An import would shift it centuries away.                                    |
| `check_original_due_not_a_timestamp`          | The same, for `original_due`.                                                                                                   |
| `check_original_deck_is_normal`               | The card's original deck is not a filtered deck.                                                                                |
| `check_mod_time_not_in_future`                | The modification time has already happened. A future one blocks updates until it passes.                                        |
| `warn_due_above_ceiling`                      | A new card position of 2,000,000 or more. Only with the repairs off.                                                            |
| `warn_graduated_card_without_ease_factor`     | A review or relearning card with no ease factor. With SM-2, its interval barely grows.                                          |
| `warn_unexplained_original_due`               | An original due with no original deck. Only with the repairs off.                                                               |
| `warn_unexplained_original_data`              | An original deck and due on a card that is not in a filtered deck. Only with the repairs off.                                   |
| `warn_template_ord_above_ceiling`             | A template ordinal above 30,000. Only with the repairs off.                                                                     |
| `warn_interval_days_above_ceiling`            | An interval above 2**31 - 1 days. Only with the repairs off.                                                                    |
| `warn_unimported_changes`                     | A change to a read card that an import never applies, since Anki never updates a card the user has.                             |
| `CardData.check_custom_data`                  | Custom data that Anki can store. Anki checks it only when the card is answered.                                                 |
| `CardData.warn_unpaired_stability_difficulty` | An FSRS stability without a difficulty, or the other way round. The scheduler ignores it.                                       |

## History and settings

### Review

| Check/Warning                        | Description                                                                                     |
| ------------------------------------ | ----------------------------------------------------------------------------------------------- |
| `warn_rescheduled_typed_as_learning` | A learning review with no answer and no time. Anki's database check retypes it as a reschedule. |
| `warn_review_type_and_ease_disagree` | A review type and a button that Anki never writes together. FSRS reads the button.              |
| `warn_time_above_ceiling`            | An answer time above 2**31 - 1. Only with the repairs off.                                      |
| `warn_int_value_in_enum_field`       | A type or button that its enum does not name.                                                   |

### Grave

| Check/Warning                       | Description                                                               |
| ----------------------------------- | ------------------------------------------------------------------------- |
| `check_default_deck_is_not_deleted` | No grave deletes deck 1. A sync would delete the default deck everywhere. |

### CollectionMetadata

| Check/Warning                       | Description                                                                                    |
| ----------------------------------- | ---------------------------------------------------------------------------------------------- |
| `check_creation_time_not_in_future` | The collection's creation time has already happened. Review cards count their due day from it. |

### ConfigEntry

| Check/Warning           | Description                                                              |
| ----------------------- | ------------------------------------------------------------------------ |
| `warn_unloadable_value` | A config value that is not JSON. Anki uses the built-in default instead. |

## Checks outside the phases

A few checks run at another time, and raise at once:

- **When a value is assigned:** a deck with id 1 cannot be filtered, a filtered deck cannot have a preset, and `media` is checked by `check_media_paths`. Every attribute is also validated for its type and range.
- **When a package is read:** each row has to agree with the objects it is read with (a card with its note and deck, a note with its notetype, a review with its card), a notetype's fields and templates have to be numbered 0 to n-1, the collection has to be schema v18, and the media map has to name files the package carries. The read also warns about two media files with the same name, and about files stored under a repaired name.
