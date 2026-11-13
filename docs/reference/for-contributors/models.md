# Model shapes

Why the pydantic models under `src/genanki2/models/` hold what they hold.

## SQLite rows

An SQLite row becomes a model through `cls.model_validate(row)`, with each field aliased to its column. Defaults, ignored extra columns such as `ntid`, and error messages that name the column all come from pydantic. The alternative, a hand-written mapping per model, would spell every default twice.

## Notetypes

### Ordinals are list positions

A field's or template's ordinal is its position in the notetype's list, and nothing else. `Field.to_row(notetype_id, ordinal)` takes one, `to_rows()` supplies it from `enumerate`, and `from_rows()` sorts by the `ord` column and then forgets it. Anki derives ordinals the same way when it saves a notetype's fields and templates (`update_notetype_fields`, `update_notetype_templates`).

An `ordinal` attribute would store the same fact twice, and `reverse()` on the list would make them disagree without firing any validator, `validate_assignment` included.

### One notetype, three tables

A notetype spans the `notetypes`, `fields` and `templates` tables. That is why `from_rows()` and `to_rows()` exist next to `from_row()` and `to_row()`: they are the only place that knows the three tables must agree on the notetype id and on the ordinals.

### Duplicate and empty lists raise

- **Two fields or two templates with the same name raise.** Both tables have a unique index on the name, so a duplicate would otherwise surface as a bare `IntegrityError` naming neither the field nor the notetype. Renaming the duplicate is no fix: Anki's own repair adds a `+` suffix, which breaks every `{{Front}}` the user wrote in the templates.
- **An empty `fields` or `templates` list raises.** Without a template, a note gets no cards. Without a field, `flds`, `sfld` and `csum` mean nothing. There is no partial result worth writing.

### `field_values()` is a notetype method

`Notetype.field_values(mapping)` builds a note's field list from a dict. It cannot be a `Note` validator: pydantic builds `__init__` from the annotation, so passing a mapping to `fields: list[str]` is a type error at the call site, and a before-validator sees `notetype` before it is validated. A `Note` classmethod would have to redeclare every other `Note` field. `field_index()` and `field_names` already live on the notetype.

## Notes

### Random guids by default

Without `guid=`, a note gets random bits. Stability is opt-in, through `guid_for(notetype_id, *key)`.

A random default says what is true of a note with no identity of its own: it is a new note, and a rebuild adds it rather than pretending to update something. The other common default, a hash of the note's fields, mints a new guid at every edit, so the note forks on import instead of updating, and two notes with equal fields under different notetypes collide. `guid_for()` takes the identity from the caller instead, from whatever names the row in the source data, and mixes in the notetype id.

## Decks and presets

### Deck names use `::`

`Deck` holds the human name, with `::` between components, and converts it at the row boundary. Anki stores `\x1f` in its place, and `native_deck_name` is not idempotent, since `\x1f` is an ASCII control character that the normalization strips: a subdeck read back and rewritten would collapse `Test\x1fNested` into `TestNested`.

### A deck's preset is an object and an id

The row holds only the id (`kind.config_id`), so the object cannot be required. An id alone is what a caller reading a package back gets, and dropping the id would make writing one impossible. Keeping both lets `Deck.config` be collected into `Collection.deck_configs` while the id still stands on its own.

The cost is two places that can disagree. `sync_normal_kind_config_id` copies the object's id into the blob on every validation, and `check_normal_kind_config_id` refuses a pair that still disagrees at write time, since Anki reads the blob and would schedule the deck by whatever id it holds.

### Presets are matched by id, not by name

`deck_config.name` compares case-insensitively like deck and notetype names, but it has no unique index and the importer never compares it. Two presets may share a name, so `check_unique_names` covers decks and notetypes only.

The id does not guarantee much either. The importer imports presets only when the user ticks "Import any deck presets" (`with_deck_configs`, off by default). It then adds an incoming preset only when the user has none with that id (`add_deck_config_if_unique_undoable`), never updates one, and keeps the id as it is, unlike deck, note and notetype ids, which it remaps.

So a deck has to be usable on preset 1, and shipping a preset is a request rather than a guarantee. Shipping preset 1 never changes an existing deck's scheduling, since a deck moves to the incoming preset only when that preset is not 1. An export carries only the presets its decks name, and an export without scheduling empties their FSRS parameters (see [Changing decks and presets](../../guide/updating.md#changing-decks-and-presets)).

## Cards

### A card's original deck is an id or a deck

`odid` is the one reference in the library allowed to dangle. A package exported with scheduling can name an original deck that no longer exists; Anki's exporter documents this, and answering the card treats the deck as not found. So the field is a union of an id and a `Deck`:

- A `Deck`-only field would force `from_row()` to invent a deck, or to refuse a row that Anki opens without complaint.
- An `int`-only field would make a caller who builds a card in a filtered deck remember to append the home deck to the collection. A `Deck` object is collected into `Collection.decks` like every other reference.

The union has two costs. No use site can assume the deck is there, and the unresolved id is what real collections produce. A caller also has two ways to write the same card, and every check accepts both. `original_deck_id` is what the rest of the library reads and what `to_row()` writes.

The bound sits on the int arm, `Annotated[Annotated[int, UInt63] | Deck, ...]`. A constraint on the whole union raises `TypeError` from pydantic's constraint machinery when a `Deck` arrives, instead of the `ValidationError` a caller catches with everything else.

### `last_review_time` is passed through

A `last_review_time` that disagrees with the revlog is not checked, by either `CardData` or `Collection`:

- Anki's only repair for it is in the database check (`fix_card_properties`), which no import calls, and it walks only cards that have revlog entries.
- An import without scheduling resets the card and clears its memory state, but leaves `lrt` in place. Without a memory state, the scheduler ignores the day count `lrt` feeds (`next_states`), and a card that keeps one overwrites `lrt` at its next answer.

What is left is a package carrying both a revlog and a card that contradicts it, imported with scheduling. genanki2 can only have read such a package, not written it.

## Builtin notetypes

### Frozen constants and a customization function

The constants such as `BASIC_NOTETYPE` are the frozen reference copies, and `builtin_notetype()` is the way to customize one. The constants are cached calls of the function, and the function reads the reference rows again on every call.

Documenting the function as the entry point makes "change a builtin" mean "change an object nobody else holds", while the constants stay the shared, unmodified object a package points at. The freeze enforces it: assigning to a constant or changing a list it holds raises, and a copy made with `model_copy()` is frozen too, so the function is the only way to get a builtin that can change. `BUILTIN_NOTETYPES` is a tuple of the constants, for iterating or keying, not for changing.

`default_deck()`, `default_deck_config()`, `default_collection_metadata()` and `default_config_entries()` follow the same shape for the stock deck, preset, `col` row and config entries, each reading the reference rows again on every call.

### Builtins ship with modification time 0

An import stores the incoming modification time as it is, and replaces a notetype the collection already holds only when the incoming one is newer. Zero never is, so the builtins survive every rebuild of a package in whatever state the user has edited them into. A current timestamp would overwrite their fields and templates at every import.

A timestamp is also the value a writer has the most reason to set on a shared constant, and one stamp would reach every package the process writes, including those already built. The freeze refuses it as an assignment, but a write straight into the constant's `__dict__` goes around the guard.

So `Package.check_builtin_notetype_versions()` refuses to write anything when a constant's modification time is not the version declared in `BUILTIN_NOTETYPES_VERSIONS`.

The cost is that a correction to a builtin cannot reach a collection that already holds it. `scripts/dump_empty_collection_rows.py` owns the modification time and raises it when the reference data changes.

### Builtin ids

`builtin_notetype(name, *, id=None)` defaults to the stock id, fixed at 1 to 6 by the dump script and so stable across every package genanki2 writes. A caller who changes anything supplies an id of their own, from the package they wrote last time.

Anki matches an incoming notetype by id alone, then compares its fields and templates to decide whether it is the same notetype (`get_target_notetype`, `equal_schema`). A rename does not change the fields or templates, so a renamed builtin that kept id 1 would merge into whatever the user holds under id 1 instead of landing beside it. Minting a fresh id inside the function would fork the deck on every rebuild instead.

A builtin always arrives as a notetype of its own: the importer never reads `original_stock_kind`, so no package can make it adopt the user's own stock notetype. The ` (genanki2)` suffix keeps Anki from making the name unique with a `+`, which would put `Basic+` beside the user's `Basic`.

The fixed ids cannot meet an id Anki allocated, since those are millisecond timestamps. Two genanki2 decks that ship one id with different fields both arrive intact, unless the user merges notetypes on import: the importer records each incoming id as the notetype's original id, adds the second deck's notetype under a new id, and finds that copy again when the deck is imported once more (`update_or_duplicate_notetype`, `get_previously_duplicated_notetype`).

## Card generation

An apkg import never generates cards: it brings in the cards the package carries and nothing else. So genanki2 makes the cards Anki would make, when the note is added (`Collection.add_notes()`) and after an edit (`Collection.generate_missing_cards()`). `Note.card_ordinals` decides which cards exist and `CardMaker` builds them, since a card's deck and position depend on the collection. `tests/test_card_ordinals_match_anki.py` compares `card_ordinals` with the cards Anki's own `add_note` creates.

The rules are in [Card generation](../card-generation.md).
