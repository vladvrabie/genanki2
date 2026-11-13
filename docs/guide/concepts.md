# Concepts

An Anki package (`.apkg`) is a zip file that holds a small Anki collection and the media files that notes refer to. This page describes the objects genanki2 uses to model it and the identifiers that make a new version of a deck import as an update and not make duplicates.

## Package and collection

A package holds a small Anki collection and genanki2 models it with the same objects that Anki uses. `package()` returns two objects:

- The package holds the media files and handles reading and writing to disk, through `Package.read_apkg()` and `pkg.write_apkg()`.
- The collection holds the decks, notetypes, notes and cards, among other things. It can also be read and written to disk, independently of the package, using `Collection.read_sqlite()` and `collection.write_sqlite()`.

=== "API"

    ```python
    from genanki2 import package

    pkg, collection = package()
    ```

=== "Models"

    ```python
    from genanki2 import Collection, Package

    collection = Collection() # An empty collection
    # Although Anki does not start from a completely empty collection.
    # default() adds the default config entries, which are required
    collection = Collection.default()
    # anki_default() also adds the default deck, preset and builtin notetypes, but are technically optional
    collection = Collection.anki_default()
    pkg = Package(collection=collection)
    ```

### Package

A package has two attributes:

- `pkg.collection`: the collection it writes.
- `pkg.media`: the paths of the media files it writes. A field refers to a file by its file name only, so two paths ending in the same name collide:
    ```python
    pkg.media.append("audio/it_casa.mp3")
    pkg.media.append("more_audio/it_casa.mp3")  # Raises on the media check
    ```
    A package (called "source") read with `keep_source_media=True` keeps track of media files in it and adds them to the written package, but in a separate list. See [Reading a package](reading.md).

Use `Package.read_apkg()` and `pkg.write_apkg()` for disk I/O.

A `CollectionPackage` is basically the same thing (same attributes, mostly the same methods), but it is interpreted as a whole collection backup and uses a different file extension, `.colpkg`. See [Collection packages](reading.md#collection-packages).

### Collection

The collection holds a list per table, either what was read or added manually:

- `collection.deck_configs` for deck presets
- `collection.decks` for decks
- `collection.notetypes` for notetypes. A notetype contains all of its fields and templates inside it, so there are no dedicated lists for them.
- `collection.notes` for notes
- `collection.tags` for tags (this is ignored during import; it is present only for completeness' sake)
- `collection.cards` for cards
- `collection.reviews` for revision history
- `collection.graves` for deleted objects (also ignored)
- `collection.config` for configuration entries
- `collection.metadata` for metadata, like creation time (this is always a 1 row table)

The lists do not have to be filled by hand. `add_notes()` adds the cards to `collection.cards`, and the write process collects everything the cards refer to: the notes, their notetypes, the decks and the decks' presets. See [Building a deck](building.md).

Some relevant methods:

- `add_notes()` returns the new cards, so they can be changed before a write (see [Customizing cards](building.md#customizing-cards)). `generate_missing_cards()` makes the cards that an edit adds, and `remove_notes()` removes notes together with their cards (see [Cards after an edit](updating.md#cards-after-an-edit) and [Removing notes](updating.md#removing-notes)).
- To find objects, use the lookup methods such as `collection.get_deck(name=...)` and `collection.get_note(guid=...)` (see [Reading a package](reading.md)).
- `Collection.read_sqlite()` and `collection.write_sqlite()` for disk I/O. You can read only the collection inside a package using `Collection.from_apkg()`.

## A collection's objects

### Notetypes, fields and templates

A **notetype** says which fields a note has and which cards it produces. Each card type is a **template**, with a front and a back written in Anki's template syntax.

A notetype is either standard or cloze. A cloze notetype has a single template and makes one card per cloze number in the note (`{{c1::...}}`, `{{c2::...}}`). See [Cloze notetypes](building.md#cloze-notetypes).

=== "API"

    ```python
    from genanki2 import notetype

    # A template is a (name, front, back) tuple
    word = notetype(
      "Italian word",
      ["Italian", "English"],
      [("Recognition", "{{Italian}}", "{{FrontSide}}<hr id=answer>{{English}}")],
      id=1_759_600_000_000,
    )
    ```

=== "Models"

    ```python
    from genanki2 import Field, Notetype, Template, TemplateConfig

    word = Notetype(
      id=1_759_600_000_000,
      name="Italian word",
      fields=[Field(name="Italian"), Field(name="English")],
      templates=[
        Template(
          name="Recognition",
          config=TemplateConfig(
            q_format="{{Italian}}",  # the front
            a_format="{{FrontSide}}<hr id=answer>{{English}}",  # the back
          ),
        ),
      ],
    )
    ```

### Notes

A **note** is one entry: a value for each field of its notetype.

`note.fields` holds the values in the order of the notetype's fields, and `note["English"]` reads or sets one by name. The values are HTML, and a note refers to a media file by its name, as in `[sound:it_casa.mp3]` (see [Building a deck](building.md)).

=== "API"

    ```python
    from genanki2 import guid_for, note

    casa = note(word, ["casa", "house"], tags=["noun"], guid=guid_for(word.id, "casa"))
    ```

=== "Models"

    ```python
    from genanki2 import Note, guid_for

    casa = Note(
      notetype=word,
      fields=["casa", "house"],
      tags=["noun"],
      guid=guid_for(word.id, "casa"),
    )
    ```

### Decks and presets

A **deck** holds cards. Its study options come from a **preset** (in Anki's interface, the deck's Options). `::` in a name makes a subdeck: `Italian::Vocabulary` is `Vocabulary` inside `Italian`.

A deck is either normal or filtered:

- A normal deck owns its cards and has a preset.
- A filtered deck has no preset: it gathers cards from normal decks by a search, so they can be studied apart for a while, and they return to their home deck when it is emptied. A card in a filtered deck has the filtered deck as `card.deck` and its home deck as `card.original_deck`. Use `deck.is_normal()`/`deck.is_filtered()` to tell them apart. Filtered decks are usually built by users in Anki, so they mostly show up when reading someone's collection backup. See [Filtered decks](building.md#filtered-decks) on building one.

=== "API"

    ```python
    from genanki2 import deck, deck_config

    slow = deck_config("Italian, slow", id=1_759_600_000_100, new_per_day=5)
    vocabulary = deck("Italian::Vocabulary", config=slow)
    ```

=== "Models"

    ```python
    from genanki2 import Deck, DeckConfig, DeckConfigInner

    slow = DeckConfig(
      id=1_759_600_000_100, name="Italian, slow", config=DeckConfigInner(new_per_day=5)
    )
    vocabulary = Deck(name="Italian::Vocabulary", config=slow)
    ```

### Cards

A **card** is one thing to study. A note gets one card per template whose front is not empty, or one card per cloze number on a cloze notetype.

A card holds the study progress (`card_type`, `queue`, `due`, `interval_days` and others), so this is where a user's schedule lives. Every card in a newly built package is new.

=== "API"

    ```python
    # One tuple of cards per note, with a card for each template that renders
    cards = collection.add_notes([casa], vocabulary)
    recognition = cards[0][0]
    ```

=== "Models"

    ```python
    from genanki2 import Card

    # The template's position, and the new-card position add_notes() would give
    recognition = Card(note=casa, deck=vocabulary, template_ord=0, due=1)
    collection.cards.append(recognition)
    ```

## Identifiers that keep a deck updatable

When someone imports a new version of a deck that they already have, Anki decides what is an update by comparing identifiers:

- notetypes by `id` and then their fields and templates, by id (if present) or by name
- notes by `guid`
- decks by name
- presets by `id`

An object whose identifier changed between versions arrives as a second copy, next to the first. You can spot them because Anki appends a "+" to the name (something similar to "My Notetype" and "My Notetype+").

A deck that will be rebuilt needs identifiers that stay the same on every build:

- **Notetype ids.** Without `id=`, `notetype()` makes a new one each run using the `monotonic_id()` function. Pass a _fixed_ number, any integer chosen once. A millisecond timestamp is what Anki uses.
- **Field and template ids.** A notetype whose fields or templates change (added, removed, reordered, or renamed without an id) arrives as a copy even with the same notetype id. `Notetype.fill_missing_ids()` gives them ids, but random ones, so a rebuild script should call it only in the first build or should pass fixed ones (`field("Italian", id=...)`). See [Changing a notetype's fields or templates](updating.md#changing-a-notetypes-fields-or-templates).
- **Note guids.** Without `guid=`, a note gets a random one each run using the `guid()` function. `guid_for(notetype.id, key)` derives a guid from a _stable_ key that names the note in your data: a word, a row id, a file name. Pass several key parts when no single one is unique (`guid_for(notetype.id, key1, key2, ...)`). Do not derive it from the whole note: a corrected translation would then change the guid, and the corrected note would arrive as a duplicate beside the old one.
- **Deck names** have to stay the same. A renamed deck arrives as a new deck, and the cards that users already have stay in the old one.

> Alternatively, build a deck only **once**, the first time, and never rebuild it again. Any updates you need to make can be done by a **read-update-write** flow using genanki2 (see [Updating a package](updating.md)). Thus, you don't have to hardcode and keep track of unique ids or guids.

Cards have no identifier of their own: the import never updates a card that a user already has, as [Changing cards](updating.md#changing-cards) explains.
