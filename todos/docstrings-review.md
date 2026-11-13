# Docstring review

Every `.py` file under `src/` and `scripts/`, against PEP 257 mood, the Google style guide and the project conventions. Generated files (`_default_rows.py`, `_schema.py`, `_typed_schema.py`, `_unicode_ages.py`, `_proto/`) are excluded. Line numbers point at the quoted text. Mechanical checks found nothing for dashes, source citations, `Args:` entries naming a missing parameter, entries documenting a type, section order, or a `Raises:` missing an exception the function raises itself; every function named in backticks carries its parens.

## Convention breaches

Ordered from the text furthest from correct: wrong facts and names, person, editorial labels, intent, misspellings, then smaller wording.

- `Important:` as a label telling the reader what matters. Drop the label and keep the sentence:
  - `src/genanki2/api.py:86`, `:159`, `:186`
  - `src/genanki2/builtins.py:72`
  - `src/genanki2/datetime.py:67`
  - `src/genanki2/models/deck.py:329`
  - `src/genanki2/models/deck_config.py:440`
- "It is highly recommended", instead of stating what happens:
  - `src/genanki2/models/collection.py:459-464`: "Important: Producing the cards searches the collection for deck presets and decks. In case the notes have templates that override the target deck or the target deck uses a custom preset, it is highly recommended to ensure that those deck presets and decks are present in the collection (i.e. by adding them before calling `add_notes()`), otherwise the builtin defaults (id=1) are used as a fallback." → "Producing the cards reads the decks and presets the collection holds. A deck a template overrides with, or a preset the deck uses, that is not in the collection yet falls back to the builtin default (id 1), so add them before calling `add_notes()`."
  - `src/genanki2/models/_card_maker.py:23`: "It is highly recommended to collect decks and deck configs before creating cards." → "Decks and deck configs held only by other objects are found once the collection has collected them."
- `Note:` as a label on a sentence that stands on its own. Drop the label:
  - `src/genanki2/datetime.py:87`: "Note: This method assumes the v2 scheduler is used." `days_elapsed()` is a function, not a method. → "The count assumes the v2 scheduler."
  - `src/genanki2/idgen.py:75`
  - `src/genanki2/text.py:147` and `:544`
  - `src/genanki2/models/package.py:1023` and `:1315`. Both also read "the same structure of a "…" file", which should be "the same structure as".

## Placeholders

None.

## Missing

Twenty-nine modules in `src/` have no module docstring. Every script has one, and the functions are covered apart from the exempt cases below.

- No module docstring. `__init__.py` is the package's entry point and re-exports every public name, so it costs a reader the most:
  - `src/genanki2/`: `__init__.py`, `datetime.py`, `errors.py`, `freezable.py`, `idgen.py`, `io_utils.py`, `settings.py`, `text.py`, `time.py`, `utils.py`
  - `src/genanki2/models/`: `_card_maker.py`, `annotations.py`, `base.py`, `card.py`, `collection.py`, `collection_metadata.py`, `config.py`, `deck.py`, `deck_config.py`, `field.py`, `grave.py`, `media_entries.py`, `note.py`, `notetype.py`, `package.py`, `package_metadata.py`, `review.py`, `tag.py`, `template.py`
  - `api.py`, `builtins.py` and `sqlite.py` have one. `models/__init__.py` is an empty file.
- `src/genanki2/utils.py:71`, `:75`, `:79`, `:83`: `is_uint8()`, `is_uint16()`, `is_uint32()` and `uint32()` have no docstring. Four other modules import them, but `genanki2` does not export them, so Google's short-and-obvious exemption covers them unless `utils` is meant to be public.
- Exempt under Google's short-and-obvious rule, listed so a later pass does not reopen them:
  - the closures inside the documented factories in `src/genanki2/models/annotations.py`: `:49`, `:69`, `:100`, `:123`, `:146`;
  - the protocol stubs `DecodesBytes.from_bytes` (`:113`) and `DecodesJSON.from_json` (`:140`);
  - the closures `replace_field_value_media_refs.maybe_escaped_new` (`src/genanki2/text.py:808`) and `_replace_captured_name.get_replacement` (`:224`);
  - the private `_base91` (`src/genanki2/idgen.py:25`), and `_MonotonicId.__init__` and `next` (`:90`, `:94`).
