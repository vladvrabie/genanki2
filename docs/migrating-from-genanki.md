# Migrating from genanki

genanki writes packages using an old collection format (schema v11). genanki2 writes the latest collection format (schema v18) and it can also read packages.

Migration from genanki to genanki2 depends on whether you have the package build script or not. If you do, simply translate it: see [Translating a build script](#translating-a-build-script). It is recommended to use stable ids so that you can rerun the build script. See [Identifiers that keep a deck updatable](guide/concepts.md#identifiers-that-keep-a-deck-updatable).

If you don't have a build script, but you have a shared package (`.apkg` file), try to use genanki2 to read it. If the package was written directly by genanki, the read will fail, telling you that genanki2 cannot read a v11 package. You will need to [re-export a genanki-written deck](#re-export-a-genanki-written-deck) using Anki. From then on, use a [read-update-write](guide/updating.md) flow with genanki2 to maintain your package.

## Re-export a genanki-written deck

Import the package into Anki and export it again:

1. In Anki, choose File > Export. Pick **Anki Deck Package** and your deck.
    - Tick **Include media**.
    - Untick **Include scheduling information**, so that your own review history does not ship with the deck.
    - Untick **Support older Anki versions**.
2. Read the export with `Package.read_apkg(path, keep_source_media=True)`, then change it and write a new version as described in [Updating a package](guide/updating.md).

Reimporting the updated package does not create duplicates in your collection (apart as consequences of your updates) because the package contains all the identifiers that genanki wrote. See [Identifiers that keep a deck updatable](guide/concepts.md#identifiers-that-keep-a-deck-updatable) for details.

### Finding notes

`genanki2.guid_for()` hashes differently from `genanki.guid_for()`, so if your update script uses this pattern, it will fail:

```python
# The original build script:
import genanki

word = genanki.Model(1607392319, "Italian word", fields=[...], templates=[...])
casa = genanki.Note(
  model=word, fields=["casa", "house"], guid=genanki.guid_for(word.model_id, "casa")
)
print(casa.guid)  # MM8Z-D@is/
deck = genanki.Deck(2059400110, "Italian")
deck.add_note(casa)
genanki.Package(deck).write_to_file("italian.apkg")

# Import italian.apkg into Anki and export it again

# The genanki2 update script:
from genanki2 import Package, guid_for

pkg = Package.read_apkg("italian.apkg", keep_source_media=True)
word = pkg.collection.get_notetype(name="Italian word")
print(word.id)  # 1607392319
print(guid_for(word.id, "casa"))  # BMv|*`)wXK
print(pkg.collection.get_note(guid=guid_for(word.id, "casa")))  # None
```

Solutions:

1. **Find by fields.** Build a dictionary from fields that identify the note, as `examples/read_update_write.py` does with `{note["Italian"]: note for note in collection.notes}`.

2. **Find by genanki's guid.** If your script called `genanki.guid_for(*key)` with stable values, compute the same guid and look it up with `collection.get_note(guid=...)`. Here is a recipe:

    ```python
    import hashlib

    BASE91 = (
      "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
      "!#$%&()*+,-./:;<=>?@[]^_`{|}~"
    )

    def genanki_guid_for(*values: object) -> str:
      digest = hashlib.sha256("__".join(str(v) for v in values).encode()).digest()
      n = int.from_bytes(digest[:8], "big")
      out = ""
      while n > 0:
        n, r = divmod(n, len(BASE91))
        out = BASE91[r] + out
      return out
    ```

    Then later you can use `pkg.collection.get_note(guid=genanki_guid_for(word.id, "casa"))`.

**genanki's unstable default:** A note built without `guid=` got genanki's default guid, a hash of all its field values, which changes when a field's value changes. Find those notes by a stable set of fields (solution 1).

### Field and template ids

genanki writes fields and templates without ids, so Anki matches them by name, and renaming one makes the next import add a second notetype. Call `Notetype.fill_missing_ids()` on each notetype in the first version you ship with genanki2. The import gives the user's notetype those ids, and from then on a rename is matched by id. See [Changing a notetype's fields or templates](guide/updating.md#changing-a-notetypes-fields-or-templates).

## Translating a build script

The overall shape stays the same: notetypes, notes, a deck, a package.

| Step     | genanki                                                          | genanki2                                                                      |
| -------- | ---------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| Start    |                                                                  | `pkg, col = package()`                                                        |
| Notetype | `m = Model(1607392319, "Simple", fields=[...], templates=[...])` | `m = notetype("Simple", ["Q", "A"], [("Card 1", qfmt, afmt)], id=1607392319)` |
| Note     | `n = Note(model=m, fields=["q", "a"])`                           | `n = note(m, ["q", "a"])`                                                     |
| Deck     | `d = Deck(2059400110, "Capitals")`                               | `d = deck("Capitals")`                                                        |
| Add      | `d.add_note(n)`                                                  | `col.add_notes([n], d)`                                                       |
| Write    | `Package(d).write_to_file("out.apkg")`                           | `pkg.write_apkg("out.apkg")`                                                  |

`package()` returns two objects. The package holds the media and is read from and written to disk; the collection holds the decks, notetypes and notes. Notes belong to the collection, and each card points at its deck.

`col.add_notes()` reads the whole collection on every call, so add all the notes for a deck in one call rather than one call per note.

### Notetypes

| genanki                                               | genanki2                                        |
| ----------------------------------------------------- | ----------------------------------------------- |
| `Model(model_id, name, ...)`                          | `notetype(name, ..., id=model_id)`              |
| `fields=[{"name": "Q"}]`                              | `["Q"]`                                         |
| `fields=[{"name": "Q", "font": "Arial", "size": 20}]` | `[field("Q", font_name="Arial", font_size=20)]` |
| `templates=[{"name": n, "qfmt": q, "afmt": a}]`       | `[(n, q, a)]` or `[template(n, q, a)]`          |
| `css=...`, `latex_pre=...`, `latex_post=...`          | the same keyword arguments on `notetype()`      |
| `sort_field_index=1`                                  | `sort_field="A"` or `sort_field_idx=1`          |
| `model_type=Model.CLOZE`                              | `cloze=True`                                    |
| `m.set_fields(...)`, `m.set_templates(...)`           | `m.fields = [...]`, `m.templates = [...]`       |

The other keys of a field or template dictionary become keyword arguments of `field()` and `template()`:

| genanki key      | genanki2 keyword                         |
| ---------------- | ---------------------------------------- |
| `font`, `size`   | `font_name`, `font_size`                 |
| `rtl`, `sticky`  | `right_to_left`, `sticky`                |
| `bqfmt`, `bafmt` | `q_format_browser`, `a_format_browser`   |
| `bfont`, `bsize` | `browser_font_name`, `browser_font_size` |
| `did`            | `target_deck_id`                         |
| `media`          | none, Anki no longer stores it           |

- genanki also accepts `fields` and `templates` as a YAML string. genanki2 takes Python lists only.
- Without `id`, `notetype()` makes up a new id each run, and every rebuild then arrives as a new notetype. Pass a fixed `id` (see [Identifiers that keep a deck updatable](guide/concepts.md#identifiers-that-keep-a-deck-updatable)), or read the notetype from the previous package.
- genanki works out a note's cards by approximating Anki's rules and the approximation is wrong for some templates. genanki2 applies Anki's rules (see [Card generation](reference/card-generation.md)), so a note can get more or fewer cards than genanki wrote. genanki's errors:

    | Note                                                           | genanki           | genanki2 and Anki            |
    | -------------------------------------------------------------- | ----------------- | ---------------------------- |
    | front `{{#A}}{{B}}{{/A}}{{C}}`, only `A` filled                | no card           | card 0, through the fallback |
    | front `{{text:A}}`, `A` filled                                 | no card           | card 0                       |
    | a field holding only a space                                   | counted as filled | counted as empty             |
    | a cloze in a field the template does not pass through `cloze:` | ignored           | gives a card                 |
    | `{{c1,2::…}}`                                                  | card 0            | cards 0 and 1                |
    | `{{c1::{{c2::…}}}}`                                            | card 0            | cards 0 and 1                |
    | `{{c600::…}}`                                                  | card 599          | card 499                     |

    genanki writes a note without any card when its requirements fail, where Anki would give it card 0.

### Notes

| genanki                       | genanki2                                                     |
| ----------------------------- | ------------------------------------------------------------ |
| `Note(model=m, fields=[...])` | `note(m, [...])`                                             |
| `tags=["a", "b"]`             | `tags=["a", "b"]`                                            |
| `guid=...`                    | `guid=...`                                                   |
| `guid_for(*values)`           | `guid_for(notetype_id, *key)`, which gives other guids       |
| `sort_field=...`              | none, Anki computes it from the notetype's sort field        |
| `due=5`                       | `card.due = 5` on the cards `col.add_notes()` returns        |
| `n.cards[1].suspend = True`   | `cards[1].suspend()`, on the cards `col.add_notes()` returns |
| `n.fields[0]`                 | `n.fields[0]`, or `n["Q"]` by field name                     |

- Without `guid`, genanki hashes all the field values, so editing a field turns the note into a new one on the user's side. genanki2 picks a random guid instead, which is not better for a rebuild: pass a guid (see [Identifiers that keep a deck updatable](guide/concepts.md#identifiers-that-keep-a-deck-updatable)), or read the note from the previous package.
- `genanki2.guid_for()` gives other guids than `genanki.guid_for()` for the same values. For a deck people have already imported, keep genanki's guids: use `genanki_guid_for()` from [Finding notes](#finding-notes) and call it where the script called `genanki.guid_for()`.
- genanki gives every card position 0. genanki2 numbers new cards in the order that notes are added, which is the order they are studied in.
- A tag cannot contain a space. genanki2 also cleans each part of a tag (separated by `::`) the way Anki does.
- `col.add_notes()` returns the cards grouped per note, in the order of the notes:

    ```python
    for front, back in col.add_notes(notes, d):
      back.suspend()
    ```

- A suspension reaches the user only when they tick "Import any learning progress" while importing. Without it, which is the default, Anki imports every card as a new card and keeps only its position.

### Decks

| genanki                 | genanki2                                   |
| ----------------------- | ------------------------------------------ |
| `Deck(deck_id, name)`   | `deck(name)`                               |
| `description="..."`     | `description="..."`                        |
| `d.add_note(n)`         | `col.add_notes([n], d)`                    |
| `d.add_model(m)`        | not needed, the notes bring their notetype |
| `d.write_to_file(path)` | `pkg.write_apkg(path)`                     |

- Anki recognises a deck by its name, so genanki2 has no deck id to keep stable.
- An import creates only the decks and notetypes that the package's cards use. An empty deck is not imported by Anki.

### Packages and media

| genanki                             | genanki2                                             |
| ----------------------------------- | ---------------------------------------------------- |
| `Package([d1, d2])`                 | one `pkg, col = package()`, with every deck in `col` |
| `Package(d, media_files=["a.mp3"])` | `pkg.media.append("a.mp3")`                          |
| `p.write_to_file(path)`             | `pkg.write_apkg(path)`                               |

- genanki packs media in no fixed order, and two paths with the same file name both get packed under that one name. genanki2 keeps the order you append in and refuses two paths with the same file name.
- `pkg.trim_unused_media()` drops every media file that no note, template or stylesheet refers to.

### Builtin notetypes

| genanki                              | genanki2                            |
| ------------------------------------ | ----------------------------------- |
| `BASIC_MODEL`                        | `BASIC_NOTETYPE`                    |
| `BASIC_AND_REVERSED_CARD_MODEL`      | `BASIC_AND_REVERSED_NOTETYPE`       |
| `BASIC_OPTIONAL_REVERSED_CARD_MODEL` | `BASIC_OPTIONAL_REVERSED_NOTETYPE`  |
| `BASIC_TYPE_IN_THE_ANSWER_MODEL`     | `BASIC_TYPE_IN_THE_ANSWER_NOTETYPE` |
| `CLOZE_MODEL`                        | `CLOZE_NOTETYPE`                    |
|                                      | `IMAGE_OCCLUSION_NOTETYPE`          |

These have different ids from genanki's, so to Anki they are different notetypes. A deck that used `BASIC_MODEL` and is rebuilt with `BASIC_NOTETYPE` gives its users a second notetype and a second copy of every note. For a deck people have already imported, read the notetype from the export instead, as described in [Re-export a genanki-written deck](#re-export-a-genanki-written-deck).

### What genanki2 does not have

- `write_to_file(path, timestamp=...)`, which genanki uses to make ids and modification times repeatable between runs.
- `write_to_collection_from_addon()` and `write_to_db()`, which write into an open Anki collection from an add-on.
