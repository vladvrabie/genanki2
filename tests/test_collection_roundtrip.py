"""Does a collection read by genanki2 write back the rows it was read from?

    uv run --with pytest pytest tests/test_collection_roundtrip.py

The row-level round trips in `test_real_collection_rows.py` build each model from its
own rows. These go through `Collection.read_sqlite` and `Collection.write_sqlite`, so
they also cover what the reader assembles: which rows become models, which become
orphans, how the references between them are resolved, and what the writer's reference
collection adds on the way out. Read-update-write depends on that sum adding nothing and
losing nothing.

`tests/fixtures/real_tables.anki2` is described in `test_real_collection_rows.py`. It is
the only input in the suite carrying `tags`, `graves` and revlog rows whose card was
deleted, all three of which only reach genanki2 from a collection somebody else wrote.
"""

import pathlib
import warnings
from collections.abc import Sequence
from typing import Any

import pytest

from genanki2.builtins import BASIC_NOTETYPE, DEFAULT_DECK, DEFAULT_DECK_CONFIG
from genanki2.errors import CollectionReadError, Genanki2Warning
from genanki2.models.card import Card
from genanki2.models.collection import Collection
from genanki2.models.config import ConfigEntry
from genanki2.models.grave import Grave
from genanki2.models.note import Note
from genanki2.models.package import Package
from genanki2.models.tag import Tag
from genanki2.sqlite import AnkiSQLite

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "real_tables.anki2"
STUB = (
  pathlib.Path(__file__).parents[1] / "src" / "genanki2" / "_dummy_collection.anki2"
)

# Keeps the writer's v1 scheduler warning out of the way of what these tests are about.
SCHEDULER_V2 = [ConfigEntry(key="schedVer", value=b"2")]


def table_rows(path: pathlib.Path) -> dict[str, Sequence[Any]]:
  with AnkiSQLite(path) as db:
    return {table: db.read_table(table) for table in AnkiSQLite.Table}


def rewrite(source: pathlib.Path, target: pathlib.Path) -> Collection:
  """Read `source` and write it to `target`, silencing the warnings the data earns."""
  with warnings.catch_warnings():
    warnings.simplefilter("ignore", Genanki2Warning)
    return Collection.read_sqlite(source).write_sqlite(target)


def a_collection() -> Collection:
  """A collection genanki2 builds, carrying a tag of each collapsed state and a grave."""
  note = Note(notetype=BASIC_NOTETYPE, fields=["front", "back"], tags=["open", "shut"])
  return Collection(
    deck_configs=[DEFAULT_DECK_CONFIG],
    decks=[DEFAULT_DECK],
    notetypes=[BASIC_NOTETYPE],
    notes=[note],
    cards=[Card(note=note, deck=DEFAULT_DECK)],
    tags=[Tag(tag="open", collapsed=False), Tag(tag="shut", collapsed=True)],
    graves=[Grave.from_row({"oid": 1_500_000_000_000, "type": 1, "usn": 0})],
    config=SCHEDULER_V2,
  )


@pytest.fixture(scope="module")
def rewritten(tmp_path_factory) -> pathlib.Path:
  path = tmp_path_factory.mktemp("roundtrip") / "collection.anki2"
  rewrite(FIXTURE, path)
  return path


# ----------------------------------- A real collection ----------------------------------


@pytest.mark.parametrize("table", list(AnkiSQLite.Table))
def test_every_table_of_a_real_collection_comes_back_row_for_row(rewritten, table):
  """Equality per table, so a failure names the table. `read_table` sorts by the key,
  which makes the comparison independent of the order the writer inserts in."""
  assert table_rows(rewritten)[table] == table_rows(FIXTURE)[table]


def test_the_reviews_of_deleted_cards_are_read_as_orphans_and_written_back(rewritten):
  """Ten revlog rows in the fixture name a card it no longer holds. A `Review` cannot be
  built without its card, so the reader keeps them raw, and an exported colpkg is the
  only package that carries them (`import_export/package/colpkg/export.rs` copies the
  file rather than gathering by reference)."""
  collection = Collection.read_sqlite(FIXTURE)
  card_ids = {row["id"] for row in table_rows(FIXTURE)["cards"]}
  orphans = [row for row in table_rows(FIXTURE)["revlog"] if row["cid"] not in card_ids]

  assert len(orphans) == 10
  assert collection.orphan_reviews == orphans
  assert all(row in table_rows(rewritten)["revlog"] for row in orphans)


def test_a_real_collection_holds_no_other_orphans():
  """The fixture keeps every note, card and notetype its revlog rows reach, so any other
  orphan would be the reader failing to resolve a reference that is there."""
  collection = Collection.read_sqlite(FIXTURE)
  assert collection.orphan_fields == []
  assert collection.orphan_templates == []
  assert collection.orphan_notes == []
  assert collection.orphan_cards == []


def test_a_second_trip_changes_nothing(rewritten, tmp_path):
  """A rebuild reads the package the previous rebuild wrote, so the trip has to be a
  fixed point and not only equal to the source once."""
  again = tmp_path / "again.anki2"
  rewrite(rewritten, again)
  assert table_rows(again) == table_rows(rewritten)


# ------------------------------- A collection genanki2 wrote ------------------------------


def test_a_collection_genanki2_wrote_comes_back_row_for_row(tmp_path):
  first, second = tmp_path / "first.anki2", tmp_path / "second.anki2"
  a_collection().write_sqlite(first)
  rewrite(first, second)
  assert table_rows(second) == table_rows(first)


def test_both_collapsed_states_survive_the_trip(tmp_path):
  """Every tag in the real fixture is collapsed, so the expanded state is only covered by
  a collection built here."""
  path = tmp_path / "collection.anki2"
  a_collection().write_sqlite(path)
  collapsed = {tag.tag: tag.collapsed for tag in Collection.read_sqlite(path).tags}
  assert collapsed == {"open": False, "shut": True}


def test_a_grave_survives_the_trip(tmp_path):
  path = tmp_path / "collection.anki2"
  collection = a_collection().write_sqlite(path)
  assert Collection.read_sqlite(path).graves == collection.graves


# ------------------------------------ Schema version ------------------------------------


def test_a_schema_11_collection_is_refused_naming_its_version():
  """The stub every latest package carries next to its real collection is a schema 11
  file, so it is the collection a reader picking the wrong zip entry would meet. Its
  `col` row is read first, so the version is what refuses it, not a missing table."""
  with pytest.raises(CollectionReadError, match="declares 11"):
    Collection.read_sqlite(STUB)


# ------------------------------------ Two collections -----------------------------------


def test_a_package_carrying_the_stub_reads_the_real_collection(tmp_path):
  """A latest package holds the zstd `collection.anki21b` and the schema 11
  `collection.anki2` stub side by side, and only `meta` says which one is the package.
  The stub holds a single "please update Anki" note, so reading it would still produce
  a collection with one note in it."""
  path = tmp_path / "deck.apkg"
  written = a_collection()
  Package(collection=written).write_apkg(path)

  with warnings.catch_warnings():
    warnings.simplefilter("ignore", Genanki2Warning)  # no extract_dir, no media
    read = Package.read_apkg(path).collection

  assert [note.guid for note in read.notes] == [note.guid for note in written.notes]
  assert [note.fields for note in read.notes] == [["front", "back"]]
