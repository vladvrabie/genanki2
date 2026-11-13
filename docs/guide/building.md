# Building a deck

## Building and writing a package

### 1. Create a package holding a collection

=== "API"

    ```python
    from genanki2 import package

    pkg, collection = package()
    ```

=== "Models"

    ```python
    from genanki2 import Collection, Package

    # default() adds the config entries a new Anki collection starts with
    collection = Collection.default()
    pkg = Package(collection=collection)
    ```

See [Package and collection](concepts.md#package-and-collection) for what they hold.

### 2. Create the notetypes

A notetype takes a name, fields and templates. A template is a (name, front, back) tuple:

=== "API"

    ```python
    from genanki2 import deck, guid_for, note, notetype

    word = notetype(
      "Italian word",
      ["Italian", "English", "Audio"],
      [
        ("Recognition", "{{Italian}}", "{{FrontSide}}<hr id=answer>{{English}}"),
        ("Recall", "{{English}}", "{{FrontSide}}<hr id=answer>{{Italian}}"),
        ("Listening", "{{Audio}}", "{{FrontSide}}<hr id=answer>{{Italian}}"),
      ],
      id=1_759_600_000_000,
      css=".card { font-family: sans-serif; font-size: 28px; text-align: center; }",
    )
    ```

=== "Models"

    ```python
    from genanki2 import Field, Notetype, NotetypeConfig, Template, TemplateConfig

    word = Notetype(
      id=1_759_600_000_000,
      name="Italian word",
      fields=[Field(name="Italian"), Field(name="English"), Field(name="Audio")],
      templates=[
        Template(
          name="Recognition",
          config=TemplateConfig(
            q_format="{{Italian}}", a_format="{{FrontSide}}<hr id=answer>{{English}}"
          ),
        ),
        Template(
          name="Recall",
          config=TemplateConfig(
            q_format="{{English}}", a_format="{{FrontSide}}<hr id=answer>{{Italian}}"
          ),
        ),
        Template(
          name="Listening",
          config=TemplateConfig(
            q_format="{{Audio}}", a_format="{{FrontSide}}<hr id=answer>{{Italian}}"
          ),
        ),
      ],
      config=NotetypeConfig(
        css=".card { font-family: sans-serif; font-size: 28px; text-align: center; }"
      ),
    )
    ```

Use `cloze=True` to make a cloze notetype (see [Cloze notetypes](#cloze-notetypes)). Use `sort_field="Italian"` to set which field Anki's card browser "Sort Field" column shows.

Any extra keyword arguments (such as `css` above) set options of the notetype's `NotetypeConfig`, which holds everything a notetype carries besides its name, fields and templates: its CSS, its sort field, the LaTeX preamble and postamble, and whether it is a cloze notetype. You cannot use `kind`, since `cloze=True` sets it.

### 3. Make the notes

A note takes its notetype and one value per field, in the notetype's field order:

=== "API"

    ```python
    casa = note(word, ["casa", "house", ""], tags=["noun"], guid=guid_for(word.id, "casa"))
    mangiare = note(
      word, ["mangiare", "to eat", ""], tags=["verb"], guid=guid_for(word.id, "mangiare")
    )
    ```

=== "Models"

    ```python
    from genanki2 import Note, guid_for

    casa = Note(
      notetype=word,
      fields=["casa", "house", ""],
      tags=["noun"],
      guid=guid_for(word.id, "casa"),
    )
    mangiare = Note(
      notetype=word,
      fields=["mangiare", "to eat", ""],
      tags=["verb"],
      guid=guid_for(word.id, "mangiare"),
    )
    ```

The notes above have no audio, so their "Listening" front is empty and they get two cards each, "Recognition" and "Recall".

If you don't know the order of the fields and/or your fields are in a dictionary (for example, data read from a CSV file), you can use `Notetype.field_values(mapping)` to get them in the order the notetype expects:

=== "API"

    ```python
    # For fields missing from the map, the default value is an empty string
    values = word.field_values({"Italian": "finestra", "English": "window"})
    finestra = note(word, values, tags=["noun"], guid=guid_for(word.id, "finestra"))
    ```

=== "Models"

    ```python
    # For fields missing from the map, the default value is an empty string
    values = word.field_values({"Italian": "finestra", "English": "window"})
    finestra = Note(
      notetype=word, fields=values, tags=["noun"], guid=guid_for(word.id, "finestra")
    )
    ```

The notetype `id` and the note guids are explained in [Identifiers that keep a deck updatable](concepts.md#identifiers-that-keep-a-deck-updatable).

### 4. Create the decks

`deck()` builds a normal deck:

=== "API"

    ```python
    vocabulary = deck("Italian::Vocabulary", description="Common words.")
    ```

=== "Models"

    ```python
    from genanki2 import DEFAULT_DECK_CONFIG, Deck, DeckNormalKind

    vocabulary = Deck(
      name="Italian::Vocabulary",
      kind=DeckNormalKind(description="Common words."),
      config=DEFAULT_DECK_CONFIG,  # deck() gives a deck the default preset
    )
    ```

`::` in a deck name makes a subdeck.

Use the `config` parameter to set the deck preset. See [Deck presets](#deck-presets).

Any extra keyword arguments set options of the deck's `DeckNormalKind`, which holds what a normal deck carries besides its name and preset: its description (and whether to render it as markdown), and the daily limits that override its preset's, such as `new_limit` and `review_limit`.

### 5. Add the notes to the collection

`collection.add_notes()` makes the notes' cards in the deck you pass and then adds them in the collection:

```python
collection.add_notes([casa, mangiare], vocabulary)
```

Add all the notes for a deck in one `add_notes()` call. Each call reads the whole collection, so a call per note becomes slow on a large deck. The notes are studied in the order they are added.

Despite the name, `add_notes()` does not add notes in the `collection.notes` list. It only builds the cards and appends them in the `collection.cards` list. The method raises an error if notes that are already present in the collection have some cards (for those use `collection.generate_missing_cards()`).

The deck, the notetype and the notes do not have to be added manually to the lists: the cards refer to them, and the write process collects everything that the cards refer to using `collection.collect_references()` method. A manual call to any `collection.collect_*()` method will update the respective list/lists (although for a large collection, it can be expensive to repeatedly collect all references, that is why it is deferred until the write process needs it).

> Decks, notetypes and notes referenced by cards created with `add_notes()` and not already present in the collection will not be found by the collection's look up methods (`get_deck()`, `get_notetype()`, etc.). For look ups to work, you need to either append them manually or collect the references after adding cards (the recommended way):

```python
collection.add_notes([casa, mangiare], vocabulary)
collection.collect_decks()  # or collection.collect_references() for everything
assert collection.get_deck(name="Italian::Vocabulary") is not None
```

### 6. Add media

Media files are added to the package by path and a field refers to them by file name:

=== "API"

    ```python
    pkg.media.append("audio/it_acqua.mp3")
    acqua = note(
      word,
      ["acqua", "water", "[sound:it_acqua.mp3]"],
      tags=["noun"],
      guid=guid_for(word.id, "acqua"),
    )
    collection.add_notes([acqua], vocabulary)  # three cards, Listening included
    ```

=== "Models"

    ```python
    pkg.media.append("audio/it_acqua.mp3")
    acqua = Note(
      notetype=word,
      fields=["acqua", "water", "[sound:it_acqua.mp3]"],
      tags=["noun"],
      guid=guid_for(word.id, "acqua"),
    )
    collection.add_notes([acqua], vocabulary)  # three cards, Listening included
    ```

The write process raises an error if a note refers to a file missing from the `pkg.media` list and warns about unused files. `pkg.trim_unused_media()` drops those.

To rename a file and every reference to it, see [Renaming media](updating.md#renaming-media).

A build can also reuse the media files of another package without extracting them:

```python
pkg.add_source_media("italian.apkg")
```

`pkg.add_source_media(path)` copies that package's files into this one when it is written.
See [Source packages](reading.md#source-packages) for details.

### 7. Write the package

```python
pkg.write_apkg("italian.apkg")
```

Before writing, genanki2 runs its checks and warnings over everything in the package. A check raises `Genanki2Error` for something Anki would reject or quietly change. A warning (`Genanki2Warning`) points at something Anki accepts but that is probably a mistake. [Checks and warnings](checks-and-warnings.md) lists each one and shows how to run them before the write, and [Settings](settings.md) shows how to turn them off.

`pkg.write_apkg(path)` replaces a file that already exists at path. With `overwrite=False` it raises `Genanki2Error` instead. `CollectionPackage.write_colpkg()` and `collection.write_sqlite()` take the same argument.

## Field and template options

A field that needs options can be built using the `field()` function and analogously a template with `template()`:

=== "API"

    ```python
    from genanki2 import field, template

    notes_field = field("Notes", collapsed=True, plain_text=True)
    translate_template = template(
      "Translate",
      "{{Italian}}",
      "{{FrontSide}}<hr id=answer>{{English}}",
      a_format_browser="{{English}}",
      browser_font_size=18,
    )
    phrase = notetype(
      "Italian phrase",
      ["Italian", "English", notes_field],
      [translate_template],
      id=1_759_600_000_001,
      sort_field="English",
    )
    ```

=== "Models"

    ```python
    from genanki2 import (
      Field,
      FieldConfig,
      Notetype,
      NotetypeConfig,
      Template,
      TemplateConfig,
    )

    notes_field = Field(name="Notes", config=FieldConfig(collapsed=True, plain_text=True))
    translate_template = Template(
      name="Translate",
      config=TemplateConfig(
        q_format="{{Italian}}",
        a_format="{{FrontSide}}<hr id=answer>{{English}}",
        a_format_browser="{{English}}",
        browser_font_size=18,
      ),
    )
    phrase = Notetype(
      id=1_759_600_000_001,
      name="Italian phrase",
      fields=[Field(name="Italian"), Field(name="English"), notes_field],
      templates=[translate_template],
      config=NotetypeConfig(sort_field_idx=1),  # the position of "English"
    )
    ```

Any extra keyword arguments set options of the field's `FieldConfig` or of the template's `TemplateConfig`. A field's options are about Anki's note editor: its font, whether its value stays after a note is added (`sticky`), plain-text editing, and placeholder text (`description`). A template's options are about the card browser: the formats it shows instead of the card's sides (`q_format_browser`, `a_format_browser`) and its font.

## Cloze notetypes

A cloze notetype makes one card per cloze number in the note, rather than one per template. Pass `cloze=True`, and use the `cloze:` filter on both sides of its one template:

=== "API"

    ```python
    cloze = notetype(
      "Italian cloze",
      ["Text", "Translation"],
      [("Cloze", "{{cloze:Text}}", "{{cloze:Text}}<br>{{Translation}}")],
      id=1_759_600_000_002,
      cloze=True,
    )
    fields = [
      "{{c1::Roma}} è la capitale d'{{c2::Italia}}.",
      "Rome is the capital of Italy.",
    ]
    collection.add_notes([note(cloze, fields)], vocabulary)  # two cards
    ```

=== "Models"

    ```python
    cloze = Notetype(
      id=1_759_600_000_002,
      name="Italian cloze",
      fields=[Field(name="Text"), Field(name="Translation")],
      templates=[
        Template(
          name="Cloze",
          config=TemplateConfig(
            q_format="{{cloze:Text}}", a_format="{{cloze:Text}}<br>{{Translation}}"
          ),
        ),
      ],
      # The cloze kind, with Anki's cloze styling
      config=NotetypeConfig.new_cloze_config(),
    )
    fields = [
      "{{c1::Roma}} è la capitale d'{{c2::Italia}}.",
      "Rome is the capital of Italy.",
    ]
    collection.add_notes([Note(notetype=cloze, fields=fields)], vocabulary)  # two cards
    ```

You can check the kind of a notetype using `Notetype.is_cloze()`/`Notetype.is_normal()`.

## Builtins

genanki2 ships Anki's six stock notetypes:

- `BASIC_NOTETYPE`
- `BASIC_AND_REVERSED_NOTETYPE`
- `BASIC_OPTIONAL_REVERSED_NOTETYPE`
- `BASIC_TYPE_IN_THE_ANSWER_NOTETYPE`
- `CLOZE_NOTETYPE`
- `IMAGE_OCCLUSION_NOTETYPE`

=== "API"

    ```python
    from genanki2 import BASIC_NOTETYPE

    rome = note(BASIC_NOTETYPE, ["Capital of Italy?", "Rome"])
    collection.add_notes([rome], vocabulary)
    ```

=== "Models"

    ```python
    from genanki2 import BASIC_NOTETYPE

    rome = Note(notetype=BASIC_NOTETYPE, fields=["Capital of Italy?", "Rome"])
    collection.add_notes([rome], vocabulary)
    ```

Every package that the program writes shares these constants, so they are read only: assigning to one raises an error. To change a builtin, change a copy instead: `builtin_notetype("Basic (genanki2)", id=...)` returns a new one on every call. Give the copy an id of its own that stays the same on every rebuild, since an import merges a notetype that keeps the stock id into the one the user already has.

genanki2 also provides the default deck as `DEFAULT_DECK` and the default deck preset as `DEFAULT_DECK_CONFIG`. For example, you can add notes to the default deck:

=== "API"

    ```python
    from genanki2 import BASIC_NOTETYPE, DEFAULT_DECK

    paris = note(BASIC_NOTETYPE, ["Capital of France?", "Paris"])
    collection.add_notes([paris], DEFAULT_DECK)
    ```

=== "Models"

    ```python
    from genanki2 import BASIC_NOTETYPE, DEFAULT_DECK

    paris = Note(notetype=BASIC_NOTETYPE, fields=["Capital of France?", "Paris"])
    collection.add_notes([paris], DEFAULT_DECK)
    ```

These constants are also immutable. You can build your own copies using `default_deck()` and `default_deck_config()`.

Two more functions provide builtins:

- `default_collection_metadata()`: the default collection metadata that an empty collection contains.
- `default_config_entries()`: the default configurations of an empty collection.

## Deck presets

A preset holds the study options, such as the number of new cards a day, and several decks can use the same one. Decks without one use Anki's default preset (which has `id=1`). Pass a preset to a deck as `config`:

=== "API"

    ```python
    from genanki2 import deck_config

    slow = deck_config("Italian, slow", id=1_759_600_000_100, new_per_day=5)
    grammar = deck("Italian::Grammar", config=slow)
    ```

=== "Models"

    ```python
    from genanki2 import DeckConfig, DeckConfigInner

    slow = DeckConfig(
      id=1_759_600_000_100, name="Italian, slow", config=DeckConfigInner(new_per_day=5)
    )
    grammar = Deck(name="Italian::Grammar", config=slow)
    ```

Anki only imports presets when the user ticks “Import any deck presets”. The import does not update the ones that the user already has. Anki matches presets by id, so give each one a fixed id.

Any extra keyword arguments (such as `new_per_day` above) sets an option of the preset's `DeckConfigInner`, which holds how the preset schedules the decks that use it: the daily limits (`new_per_day`, `reviews_per_day`), the learning steps in minutes (`learn_steps`, `relearn_steps`), `maximum_review_interval`, `desired_retention` and the burying options. An option that is not given keeps the value that Anki gives a new preset.

## Filtered decks

A filtered deck gathers cards from normal decks by a search, written in the syntax of Anki's browser. `filtered_deck()` builds one:

=== "API"

    ```python
    from genanki2 import filtered_deck

    cram = filtered_deck("Italian::Cram verbs", search="deck:Italian tag:verb", limit=50)
    ```

=== "Models"

    ```python
    from genanki2 import DeckFilteredKind

    SearchTerm = DeckFilteredKind.SearchTerm
    cram = Deck(
      name="Italian::Cram verbs",
      kind=DeckFilteredKind(
        search_terms=[
          SearchTerm(
            search="deck:Italian tag:verb", limit=50, order=SearchTerm.Order.RANDOM
          ),
        ],
      ),
    )
    ```

Up to `limit` cards are gathered using `search`, ordered by `order` (random by default). `search2` adds a second search, with its own `limit2` and `order2`. With `reschedule=False`, the deck is a preview: answering a card there does not change its schedule. A filtered deck has no preset, and `collection.add_notes()` refuses one, because a card belongs to a normal deck first.

Anki fills a filtered deck when the user builds it and an `.apkg` import drops empty decks. So a filtered deck written to an `.apkg` reaches the user only when it holds a card and it stays filtered only when the user ticks "Import any learning progress". Otherwise the import turns it into a normal deck. A card goes into a filtered deck with the filtered deck as `Card.deck` and its home deck as `Card.original_deck`, assigned in that order, since a card in a normal deck has its home deck cleared:

=== "API"

    ```python
    bere = note(
      word, ["bere", "to drink", ""], tags=["verb"], guid=guid_for(word.id, "bere")
    )
    recognition, recall = collection.add_notes([bere], vocabulary)[0]
    recognition.deck, recognition.original_deck = cram, recognition.deck
    ```

=== "Models"

    ```python
    bere = Note(
      notetype=word,
      fields=["bere", "to drink", ""],
      tags=["verb"],
      guid=guid_for(word.id, "bere"),
    )
    recognition, recall = collection.add_notes([bere], vocabulary)[0]
    recognition.deck, recognition.original_deck = cram, recognition.deck
    ```

A collection package carries filtered decks as they are. See [Collection packages](reading.md#collection-packages).

## Customizing cards

`collection.add_notes()` returns the new cards, grouped per note, in the order of the notes. Use them to change a card before it is written:

=== "API"

    ```python
    from genanki2 import CardFlag

    notes = [note(word, ["cane", "dog", ""], guid=guid_for(word.id, "cane"))]
    for recognition, recall in collection.add_notes(notes, vocabulary):
      recognition.flags = CardFlag.RED
      recall.suspend()
    ```

=== "Models"

    ```python
    from genanki2 import CardFlag

    notes = [
      Note(notetype=word, fields=["cane", "dog", ""], guid=guid_for(word.id, "cane"))
    ]
    for recognition, recall in collection.add_notes(notes, vocabulary):
      recognition.flags = CardFlag.RED
      recall.suspend()
    ```

`card.flags` holds the colour flag that Anki's browser shows, one of the `CardFlag` values from `RED` to `PURPLE`, or `NONE`. `suspend()` keeps a card out of the reviews until the user unsuspends it. `bury()` keeps it out for the rest of the day instead: Anki unburies it at the next day rollover.

A card's state reaches the user only when they tick "Import any learning progress" while importing. Without it, which is the default, Anki imports every card as a new card, unsuspended and without flags, keeping only its position.

A note gets the cards that Anki would give it: one for each template whose front is not empty. A note whose fronts are all empty still gets the first template's card, with a warning. The full rules are in [Card generation](../reference/card-generation.md).
