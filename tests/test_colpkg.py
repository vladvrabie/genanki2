"""Does a colpkg genanki2 writes restore in Anki, and does it read what Anki writes?

    uv run --with anki --with pytest pytest tests/test_colpkg.py

A restore replaces the user's collection with the file (`import_colpkg`,
`rslib/src/import_export/package/colpkg/import.rs:31-61`). It merges nothing and repairs
nothing, so what the file holds is what the user gets: every table has to come back as
Anki exported it, and a collection built from models has to be one the scheduler can
study. The database check is not evidence of the second, since it passes collections
that cannot be studied, such as one without deck 1 or on the v1 scheduler (measured
against anki 26.08.1), so the tests here open the restored collection and ask the
scheduler for its decks.

A restore runs on a backend with no collection open: take `col._backend`, close the
collection, then call `import_collection_package`. With the collection open, the call
fails with `CollectionAlreadyOpen`.

Desktop Anki decides between a restore and a merge by the file name alone
(`ColpkgImporter.can_import`, `qt/aqt/import_export/importing.py:46-51`), so the name
guards on both writers are tested here too.

anki is a test-only dependency, never a dependency of genanki2.
"""

import hashlib
import warnings
from pathlib import Path
from typing import Any

import pytest
from anki.collection import Collection as AnkiCollection
from anki.scheduler.v3 import CardAnswer

from genanki2.builtins import (
  BASIC_NOTETYPE,
  DEFAULT_DECK_CONFIG,
  default_config_entries,
  default_deck,
)
from genanki2.errors import CollectionReadError, Genanki2Error, Genanki2Warning
from genanki2.models.card import Card, CardQueue, CardType
from genanki2.models.collection import Collection
from genanki2.models.deck import Deck
from genanki2.models.media_entries import MediaEntries
from genanki2.models.note import Note
from genanki2.models.package import CollectionPackage, Package
from genanki2.sqlite import AnkiSQLite

SOUND = ("sound.mp3", b"ID3 sound bytes")

# The `col` columns a restore sets on its own, measured against anki 26.08.1: it marks
# the collection and its schema modified (`import.rs:63-79`).
SET_BY_THE_RESTORE = {"mod", "scm"}


def table_rows(path: Path) -> dict[str, list[dict[str, Any]]]:
  with AnkiSQLite(path) as db:
    return {table: db.read_table(table) for table in AnkiSQLite.Table}


def restore(colpkg: Path, profile: Path) -> Path:
  """Restore `colpkg` into a fresh profile folder and return the collection's path."""
  profile.mkdir()
  col = AnkiCollection(str(profile / "collection.anki2"))
  backend, path, media_folder = col._backend, col.path, col.media.dir()
  col.close()
  backend.import_collection_package(
    col_path=path,
    backup_path=str(colpkg),
    media_folder=media_folder,
    media_db=str(profile / "collection.media.db2"),
  )
  return Path(path)


def answer_first_card(col: AnkiCollection) -> None:
  queued = col.sched.get_queued_cards().cards[0]
  card = col.get_card(queued.card.id)
  card.start_timer()
  col.sched.answer_card(
    col.sched.build_answer(card=card, states=queued.states, rating=CardAnswer.EASY)
  )


def an_anki_collection(directory: Path) -> AnkiCollection:
  """A collection holding what only a whole-collection export carries.

  Four notes: two answered, one deleted, and one pulled into a filtered deck. Tags in
  both collapsed states, and a media file the notes reference.
  """
  col = AnkiCollection(str(directory / "collection.anki2"))
  col.media.write_data(*SOUND)
  basic = col.models.by_name("Basic")
  assert basic is not None
  note_ids = []
  for position in range(4):
    note = col.new_note(basic)
    note["Front"] = f"front {position} [sound:{SOUND[0]}]"
    note["Back"] = "back"
    note.tags = ["open", "shut"]
    col.add_note(note, 1)
    note_ids.append(note.id)
  col.tags.set_collapsed("open", False)  # Anki registers a new tag collapsed
  answer_first_card(col)
  answer_first_card(col)
  col.remove_notes([note_ids[3]])

  filtered = col.sched.get_or_create_filtered_deck(deck_id=0)
  filtered.name = "Filtered"
  filtered.config.search_terms[0].search = "deck:Default is:new"
  col.sched.add_or_update_filtered_deck(filtered)
  return col


def a_studyable_collection() -> Collection:
  """A collection built from models: deck 1, one deck holding a new and a review card."""
  deck = Deck(name="Italian")
  collection = Collection(
    config=default_config_entries(),
    deck_configs=[DEFAULT_DECK_CONFIG],
    decks=[default_deck(), deck],
  )
  collection.cards += [
    Card(note=Note(notetype=BASIC_NOTETYPE, fields=["uno", "one"]), deck=deck),
    Card(
      note=Note(notetype=BASIC_NOTETYPE, fields=["due", "two"]),
      deck=deck,
      type=CardType.REVIEW,
      queue=CardQueue.REVIEW,
      due=0,
      ivl=1,
      factor=2500,
    ),
  ]
  return collection


@pytest.fixture(scope="module")
def exported(tmp_path_factory) -> tuple[Path, Path]:
  """Anki's collection file and the colpkg Anki exported from it."""
  directory = tmp_path_factory.mktemp("source")
  col = an_anki_collection(directory)
  colpkg = directory / "export.colpkg"
  col.export_collection_package(str(colpkg), include_media=True, legacy=False)
  path = Path(col.path)
  col.close()
  return path, colpkg


@pytest.fixture(scope="module")
def restored(exported, tmp_path_factory) -> Path:
  """The export read with `read_colpkg`, written with `write_colpkg`, and restored.

  `keep_source_media=True` is the path for a backup holding thousands of files, since
  the writer copies the entries without extracting them.
  """
  directory = tmp_path_factory.mktemp("restored")
  rewritten = directory / "rewritten.colpkg"
  package = CollectionPackage.read_colpkg(exported[1], keep_source_media=True)
  with warnings.catch_warnings():
    # Anki's stock Image Occlusion notetype has a field no template shows.
    warnings.simplefilter("ignore", Genanki2Warning)
    package.write_colpkg(rewritten)
  return restore(rewritten, directory / "profile")


# ------------------------------- The round trip through Anki -------------------------------


def test_the_source_carries_what_the_trip_is_about(exported):
  rows = table_rows(exported[0])
  assert len(rows["revlog"]) == 2
  assert any(card["odid"] for card in rows["cards"])
  assert {tag["tag"]: tag["collapsed"] for tag in rows["tags"]} == {
    "open": False,
    "shut": True,
  }
  assert {grave["type"] for grave in rows["graves"]} == {0, 1}  # a card and a note


@pytest.mark.parametrize("table", list(AnkiSQLite.Table))
def test_every_table_comes_back_after_a_restore(exported, restored, table):
  source, result = table_rows(exported[0])[table], table_rows(restored)[table]
  if table == AnkiSQLite.Table.COL:
    source = [
      {k: v for k, v in row.items() if k not in SET_BY_THE_RESTORE} for row in source
    ]
    result = [
      {k: v for k, v in row.items() if k not in SET_BY_THE_RESTORE} for row in result
    ]
  assert result == source


def test_the_restore_forces_a_full_sync(exported, restored):
  """A schema modified after the last sync is what makes the next sync a full upload."""
  [source] = table_rows(exported[0])["col"]
  [result] = table_rows(restored)["col"]
  assert result["scm"] > source["scm"]


def test_the_media_folder_holds_every_file_after_a_restore(restored):
  folder = restored.with_name("collection.media")
  files = {file.name: file.read_bytes() for file in folder.iterdir() if file.is_file()}
  assert files == dict([SOUND])


# ------------------------------ A collection built from models -----------------------------


def test_a_collection_built_from_models_restores_and_studies(tmp_path):
  colpkg = tmp_path / "built.colpkg"
  CollectionPackage(collection=a_studyable_collection()).write_colpkg(colpkg)
  col = AnkiCollection(str(restore(colpkg, tmp_path / "profile")))
  try:
    tree = col.sched.deck_due_tree()
    # Deck 1 is empty, and the tree leaves an empty default deck out.
    counts = {deck.name: (deck.new_count, deck.review_count) for deck in tree.children}
    assert counts == {"Italian": (1, 1)}
    assert col.fix_integrity()[1], "the database check found something to fix"
  finally:
    col.close()


# ------------------------------------ What Anki writes -------------------------------------


def test_an_automatic_backup_reads(tmp_path):
  col = AnkiCollection(str(tmp_path / "collection.anki2"))
  note = col.new_note(col.models.by_name("Basic"))  # pyright: ignore[reportArgumentType]
  note["Front"] = "backed up"
  col.add_note(note, 1)
  col.create_backup(backup_folder=str(tmp_path), force=True, wait_for_completion=True)
  col.close()
  [backup] = tmp_path.glob("backup-*.colpkg")

  package = CollectionPackage.read_colpkg(backup)
  assert [note.fields for note in package.collection.notes] == [["backed up", ""]]


def test_the_legacy_colpkg_is_refused_by_its_schema(tmp_path):
  """`legacy=True` downgrades the collection to schema 11 (`colpkg/export.rs:62-66`)."""
  path = tmp_path / "legacy.colpkg"
  col = AnkiCollection(str(tmp_path / "collection.anki2"))
  col.export_collection_package(str(path), include_media=True, legacy=True)
  col.close()
  with pytest.raises(CollectionReadError, match="declares 11"):
    CollectionPackage.read_colpkg(path)


# ------------------------------------- The media map ---------------------------------------


def test_every_entry_carries_the_size_and_sha1_of_its_file(tmp_path):
  """What lets a restore skip a file the media folder already holds."""
  audio = tmp_path / SOUND[0]
  audio.write_bytes(SOUND[1])
  collection = a_studyable_collection()
  collection.cards[0].note.fields[1] = f"[sound:{SOUND[0]}]"
  colpkg = tmp_path / "built.colpkg"
  CollectionPackage(collection=collection, media=[str(audio)]).write_colpkg(colpkg)

  [entry] = MediaEntries.from_apkg(colpkg).entries
  assert (entry.name, entry.size, entry.sha1) == (
    SOUND[0],
    len(SOUND[1]),
    hashlib.sha1(SOUND[1]).digest(),
  )


def test_entries_copied_from_the_source_carry_size_and_sha1(exported, tmp_path):
  rewritten = tmp_path / "rewritten.colpkg"
  package = CollectionPackage.read_colpkg(exported[1], keep_source_media=True)
  with warnings.catch_warnings():
    warnings.simplefilter("ignore", Genanki2Warning)
    package.write_colpkg(rewritten)

  [entry] = MediaEntries.from_apkg(rewritten).entries
  assert (entry.size, entry.sha1) == (len(SOUND[1]), hashlib.sha1(SOUND[1]).digest())


# -------------------------------------- File names -----------------------------------------


@pytest.mark.parametrize(
  "name", ["deck.colpkg", "collection.apkg", "backup-2026-09-24-18.32.08.apkg"]
)
def test_write_apkg_refuses_a_name_anki_restores(tmp_path, name):
  path = tmp_path / name
  package = Package(collection=a_studyable_collection())
  with pytest.raises(Genanki2Error, match="Invalid file name"):
    package.write_apkg(path)
  assert not path.exists()


@pytest.mark.parametrize("name", ["Collection.apkg", "my-backup-1.apkg", "backup.apkg"])
def test_write_apkg_takes_a_name_anki_merges(tmp_path, name):
  """Anki matches the names case-sensitively, by exact name, prefix and suffix."""
  path = tmp_path / name
  Package(collection=a_studyable_collection()).write_apkg(path)
  assert path.is_file()


@pytest.mark.parametrize("name", ["deck.apkg", "collection.apkg", "export.zip"])
def test_write_colpkg_refuses_a_name_without_the_suffix(tmp_path, name):
  path = tmp_path / name
  package = CollectionPackage(collection=a_studyable_collection())
  with pytest.raises(Genanki2Error, match="Invalid file name"):
    package.write_colpkg(path)
  assert not path.exists()
