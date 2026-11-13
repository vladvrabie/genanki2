# Reading a package

genanki2 reads packages that use the latest database schema (v18) and any package format. It does not read Anki's "Support older Anki versions" export, which uses the old collection format (v11). Reading a package validates every table and repairs a few values the way Anki does when it saves them; [Repairs](repairs.md) lists them.

```python
from genanki2 import Package

pkg = Package.read_apkg("italian.apkg", keep_source_media=True)
collection = pkg.collection
```

The collection contains lists for everything it holds (decks, presets, notetypes, notes, etc.), either read or added manually. See [Package and collection](concepts.md#package-and-collection) for the full details.

> It also has a few lists for orphaned objects (fields, templates, notes, cards and reviews). These are objects referencing other objects which are not present in the database and cannot be modeled.

The collection has lookup methods to find single objects:

- `collection.get_config_entry(key)`
- `collection.get_deck_config()`, by id or by name
- `collection.get_deck()`, by id or by name
- `collection.get_notetype()`, by id or by name
- `collection.get_note()`, by id or by guid

> In case a lookup method cannot find what you are looking for, try a manual search in the corresponding orphan list (`collection.orphan_*`).

Example:

```python
vocabulary = collection.get_deck(name="Italian::Vocabulary")
word = collection.get_notetype(name="Italian word")
casa = collection.get_note(guid=guid_for(word.id, "casa"))
for note in collection.notes:
  print(note["Italian"], note.tags)
```

`examples/read_package.py` prints a summary of a package.

## Managing media files

Media files can be read in 2 ways:

1. Using `extract_dir="..."` writes all the media files to a folder. This allows inspecting the media files and replacing them easily on the disk. As a consequence, `pkg.media` holds their paths after the reading finished.

    An extracted file replaces a file of the same name already in the directory. With `overwrite=False`, the read raises `Genanki2Error` instead and extracts nothing.

    > `extract_dir` has a secondary effect: it also extracts the collection database in the same directory. It is an SQLite file named `collection.anki21b` (according to the latest package format; `.anki2` or `.anki21` for the older package formats). This makes it convenient to explore the database using specialized software or alter it using scripts (for example, using genanki2's `Collection.read_sqlite()` and `collection.write_sqlite()`). The SQLite file is ignored by the write process.

2. Using `keep_source_media=True` leaves them in the source file, but keeps track of them. The next write process copies them from the source file into the newly written package. This makes it easy to correct some notes or add some new ones, without having to extract them and occupy disk space. Of course, this behaviour only works if the source file is still present during the write and hasn't changed since the read. The package is written to another path: writing over the source file is refused (details [here](../reference/for-contributors/packages.md#a-kept-media-source-is-checked-before-writing)).

With neither, the package is read without its media. A warning is shown that media files have been ignored and that writing the package back refuses any note that refers to a file (in this case, `pkg.media` is an empty list).

## Source packages

A source package is a package whose media files are copied when this package is written. Reading with `keep_source_media=True` makes the package being read the source. `pkg.add_source_media(path)` sets one on any package, read or newly built, for example to reuse the media files of the last release in a fresh build without extracting them:

=== "API"

    ```python
    from genanki2 import package

    pkg, collection = package()
    pkg.add_source_media("italian.apkg")
    ```

=== "Models"

    ```python
    from genanki2 import Collection, Package

    pkg = Package(collection=Collection.default())
    pkg.add_source_media("italian.apkg")
    ```

Notes can refer to the source's files as to any file in `pkg.media`, and the write counts them as carried. The files stay in the source until the write, so it has to still be present and unchanged then. A file in `pkg.media` with the same name as a file in the source is written instead of it (`pkg.media` has priority over the source's media). An unused source file is warned about and `pkg.trim_unused_media()` drops it as it drops the unused files in `pkg.media`. Names that Anki would not store are repaired as on a read, with the same warning.

A package has a **single** source, so adding one replaces the previous one. `add_source_media()` raises if the package at `path` carries no media files.

> If you want to combine media from multiple packages, either extract all the media files, then write one containing all, or perform multiple steps of read->add_source_media()->write, each step reading the package from the last step and adding media from another package.

`pkg.remove_source_media()` removes the source, so the next write copies nothing from it.

## Collection packages

An `.apkg` is a shared deck (or decks) and merges into the collection of whoever imports it.

A `.colpkg` is an export of a user's entire collection. When imported, it replaces their whole collection. This is what Anki writes for "Export collection" and for its backups.

genanki2 models this with `CollectionPackage`. Its methods `CollectionPackage.read_colpkg()` and `colpkg.write_colpkg()` read and write one, for changing your own collection or backup.

- **A restore doesn't repair anything.** Anki replaces the collection and marks the schema as modified, so the next sync is a one-way full upload.
- **Media files are added, never removed.** Anki saves to disk each file that the `.colpkg` carries unless the media folder already holds an equal one. A backup can hold thousands of files: `read_colpkg(path, keep_source_media=True)` followed by `write_colpkg()` to a new path copies them without extracting them.
- **The file name decides the import.** Anki restores any file ending in `.colpkg` or named `collection.apkg` or `backup-*.apkg`, and merges every other `.apkg`. `write_apkg()` refuses those three names, since a deck written under one of them would replace the collection of whoever imports it, and `write_colpkg()` refuses a name without the `.colpkg` suffix.
- **`write_colpkg()` writes the latest package format only.** Anki's "Support older Anki versions" collection export uses schema 11, which genanki2 does not read.

The API does not provide a method to create a collection package. One built from scratch needs all the defaults that Anki adds to an empty collection. Use this recipe:

```python
from genanki2 import Collection, CollectionPackage

colpkg = CollectionPackage(collection=Collection.anki_default())
```

## Read errors

A failed read raises one of two errors, both subclasses of `Genanki2Error`:

- `PackageReadError`: the file is not a package genanki2 can make sense of. It is not a zip file, it carries neither a `meta` entry nor a collection entry, an entry does not decompress, or its media map does not parse. A write raises it too when the [source package](#source-packages) of its media is gone or has changed since it was added.
- `CollectionReadError`: the package is intact, but the database inside is not a collection genanki2 can read. It uses another schema version (such as the v11 that "Support older Anki versions" exports), misses a table or a column, or holds rows that do not fit together, such as a notetype whose fields are not numbered from 0.

A collection that reads but holds a value out of its range raises pydantic's `ValidationError` instead.

To recover a package that genanki2 cannot read, import it into Anki and export it again; Anki rewrites the collection in the current format. If genanki2 still cannot read the export, try to fix the values in Anki. If Anki also refuses the import, extract the package and edit the database by hand. With `extract_dir`, the database is written to the directory before it is read, so it is still there to inspect after a `CollectionReadError`.
