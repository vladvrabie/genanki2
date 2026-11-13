# Reading and collection packages

The reasons behind how the reader and the two writers behave. What they do is in [Reading a package](../../guide/reading.md).

## Reading a package

The reader exists for read-update-write: read the package shipped last time, change what changed, write it back. So every decision below leans towards handing back what Anki wrote, and towards failing where it cannot, rather than guessing.

### The envelope names the entry, `col.ver` names the schema

The `meta` entry, or the collection filename when there is none, decides three things: which zip entry holds the collection, whether it is compressed, and which media map format the package uses. It says nothing about the schema.

So the reader opens the entry and refuses a `col.ver` other than 18. Anki accepts 11 through 18 and upgrades on open; genanki2 models one schema.

Picking the entry by a name the reader recognises would go wrong: every package in the latest format also carries a schema 11 `collection.anki2` stub beside `collection.anki21b`, holding one "please update Anki" note. A reader that took it would read a valid collection with the wrong contents.

### An exported apkg holds a minimal collection

Anki builds a fresh collection for the package and inserts the exported rows into it. So the package carries the default config entries, deck 1 and preset 1, even when the export did not need them. Only `crt` and `creationOffset` are copied from the source.

### Read errors are raised outside validators

`PackageReadError` and `CollectionReadError` (see [Read errors](../../guide/reading.md#read-errors)) are raised only from plain methods and classmethods. Pydantic wraps any `ValueError` a validator raises into a `ValidationError`, so raising them inside a validator would lose their type.

### Orphan rows are kept raw

A field, template, note, card or revlog row whose parent is missing goes to the matching `Collection.orphan_*` list, and is written back unchanged. A model cannot be built without the object its row points at, and inventing that object or dropping the row would each alter what Anki wrote.

Anki deletes such rows in Check Database, and an apkg never carries them because the exporter gathers rows by reference. A collection copied off disk can hold them, though, such as revlog rows whose card was deleted.

### A model that raises fails the whole read

There is no lenient mode, for the same reason the models have [no strictness setting](validation.md#permissive-while-building-strict-at-the-write). The recovery path is the database that `extract_dir` writes before parsing it.

### A deck naming a missing preset keeps the id

Such a deck reads with `config = None` and keeps its preset id. Anki schedules it on preset 1 and never rewrites the id, so the reader invents no preset and repairs nothing. The write warns, and the repair is opt-in (see [Decks and presets](../../guide/repairs.md#decks-and-presets)).

### Ids in the future are reported, not renumbered

An id more than a day in the future stays as read. Renumbering it would rewrite identifiers the round trip owes back, and would not spare the user anything:

- The merge importer renumbers such ids on its temporary copy of every package it reads, resolving any clash with the ids the user's collection already holds.
- A restore renumbers nothing. The rows keep their ids until the user runs the database check, which renumbers them the same way.

### A kept media source is checked before writing

`Package.media` holds paths on disk, so a read has to give the media files a home (see [Managing media files](../../guide/reading.md#managing-media-files)). A kept source is checked before the destination zip is opened:

- It has to still exist.
- It has to hold every entry the read found.
- It has to declare the same envelope, since the envelope decides whether an entry is a file or a zstd frame of one.
- Its `media` entry has to be the one the read found, compared by the CRC-32.
    - In the latest envelope the map holds each file's size and SHA-1, so a changed file changes the map.
    - A legacy map holds only names, so a file replaced there under the same name is copied as it now stands.
    - In the latest envelope the CRC covers the map's zstd frame, so a source rewritten with the same files but another compression level is refused too.

The destination may not be the source itself, a hard link to it, or a spelling of it that a filesystem ignoring case resolves to the same file. The package would go on copying entries by a numbering the new file no longer has, and a file the new package drops would lose its only copy:

```python
pkg = Package.read_apkg("italian.apkg", keep_source_media=True)
pkg.media.append("extra.txt")
pkg.write_apkg("italian.apkg")  # Correct write, but because of "extra.txt",
                                # after the write the tracked media files data is
                                # out-of-sync with the actual file on disk
pkg.write_apkg("italian.apkg")  # Second write reads the wrong bytes from the source
```

### Media names are repaired, references are not

The read warns with the rename mapping instead of applying it (see [Media references](../../guide/repairs.md#media-references)), because applying it rewrites note fields, templates and CSS, which is data Anki wrote. The writer refuses a package whose references name a file it does not carry, so a caller who skips the step gets an error, not a broken deck.

Anki handles the two envelopes differently: a name that is not normalized fails the whole import in the latest format, while in the legacy format it is normalized and the notes are rewritten to match. genanki2 repairs both alike on read, and leaves the strict half to the writer, where `Package.check_media_paths()` refuses any name that `normalize_media_name()` would change.

## Collection packages

What a restore does and which file names each writer accepts are in [Collection packages](../../guide/reading.md#collection-packages).

### Every media entry carries a SHA-1

A restore skips a file the media folder already holds, and decides that from the SHA-1 in the media entry. So `write_colpkg()` fills it on every entry.

### Each import is a class

`Package` and `CollectionPackage` share their fields, media handling and envelope. They differ in the file name they accept and the checks they run. `read_apkg()` and `read_colpkg()` read the same files, since nothing inside a package tells the two apart.

The class the caller reads into decides how the file is written back, so no flag says which import a file is for. Moving between the two is an explicit construction from the other's fields.

### File names are matched the way Anki's client does

Both writers match a name case-sensitively, on the exact name, the prefix and the suffix, as Anki's desktop client does. How AnkiDroid routes a file has not been checked.

### Checks that differ between a merge and a restore

The checks that a restore needs differ from a merge's wherever the verdict depends on what the importer does. Each writer passes the phases a `SerializationContext` naming its output, and the model that owns the data decides from it whether a finding is an error or a warning, and what the message says. Without a context nothing is skipped: a finding only a restore refuses is a warning, and the message names what happens under each import.

| Check                           | `Package`                                  | `CollectionPackage`                       | Why they differ                                                                                                                                                                                                        |
| ------------------------------- | ------------------------------------------ | ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Builtin notetype versions       | raises                                     | skipped                                   | A merge replaces a notetype only when the incoming one is newer, so a stamped builtin overwrites the user's copy. A restore replaces everything, and a user's collection can hold a builtin the user has since edited. |
| Media references without a file | raises                                     | warns                                     | Real collections carry them routinely (Anki's "Check Media" lists them), and writing back a backup must not fail on data the user already has. A restore removes no file, so the reference may still resolve.          |
| Ids in the future               | warns: Anki renumbers them on import       | warns: they stay until the database check | The merge renumbers on its temporary copy. A restore keeps the ids, and the database check later renumbers them and moves the added dates and review times with them.                                                  |
| `schedVer` other than 2         | warns, when a card is out of the new queue | raises                                    | A merge upgrades a package to v2 when any card is out of the new queue, which rewrites the review history. A restore upgrades nothing, so the collection opens on a scheduler Anki no longer runs, or not at all.      |
| No deck 1                       | silent                                     | raises                                    | A merge lands in a collection that has deck 1. A restored collection without it opens, but every scheduler call fails, and the database check does not recreate it. `Collection.collect_default_deck()` adds it.       |
