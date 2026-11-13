"""Does a package Anki exported survive being read and written back by genanki2?

    uv run --with anki --with pytest pytest tests/test_anki_package_roundtrip.py

The other round trips start from a collection genanki2 wrote, or from the tables of one
Anki wrote. These start from a whole package out of Anki's exporter, which is the file a
deck author holds when read-update-write begins. AnkiDroid exports through the same
backend, so its packages are this one.

The package is exported with scheduling, deck presets and media, so it carries a review,
a preset and a file on top of notes, cards and a nested deck.

anki is a test-only dependency, never a dependency of genanki2.
"""

from pathlib import Path
from typing import Any

import pytest
from anki.collection import Collection as AnkiCollection
from anki.collection import ImportAnkiPackageRequest
from anki.import_export_pb2 import ExportAnkiPackageOptions, ImportAnkiPackageOptions

from genanki2.errors import CollectionReadError
from genanki2.models.package import Package

AUDIO = {"front.mp3": b"ID3front"}


def export(tmp_path: Path, *, legacy: bool) -> Path:
  """Three notes of Basic (and reversed card) in `Top::Sub`, one card answered."""
  col = AnkiCollection(str(tmp_path / "author.anki2"))
  deck_id = col.decks.id("Top::Sub")
  notetype = col.models.by_name("Basic (and reversed card)")
  assert deck_id is not None and notetype is not None
  [(name, data)] = AUDIO.items()
  col.media.write_data(name, data)
  for i in range(3):
    note = col.new_note(notetype)
    note["Front"], note["Back"] = f"front {i} [sound:{name}]", f"back {i}"
    note.tags = ["plain", "parent::child"]
    col.add_note(note, deck_id)
  col.decks.select(deck_id)
  card = col.sched.getCard()
  assert card is not None
  col.sched.answerCard(card, 3)

  path = tmp_path / "anki.apkg"
  col.export_anki_package(
    out_path=str(path),
    options=ExportAnkiPackageOptions(
      with_scheduling=True, with_deck_configs=True, with_media=True, legacy=legacy
    ),
    limit=None,
  )
  col.close()
  return path


def rebuild(source: Path, target: Path) -> Path:
  Package.read_apkg(source, extract_dir=target.parent / "media").write_apkg(target)
  return target


@pytest.fixture(scope="module")
def packages(tmp_path_factory) -> tuple[Path, Path]:
  """The package Anki exported, and the one genanki2 wrote back from it."""
  directory = tmp_path_factory.mktemp("anki")
  source = export(directory, legacy=False)
  return source, rebuild(source, tmp_path_factory.mktemp("rebuilt") / "deck.apkg")


# ------------------------------------ Through genanki2 -----------------------------------


@pytest.mark.parametrize(
  "attribute",
  ["deck_configs", "decks", "notetypes", "notes", "tags", "cards", "reviews", "config"],
)
def test_reading_the_rebuild_gives_what_reading_the_export_gave(
  packages, tmp_path, attribute
):
  """Per attribute, so a failure names what the trip changed."""
  source, rebuilt = packages
  before = Package.read_apkg(source, extract_dir=tmp_path / "before").collection
  after = Package.read_apkg(rebuilt, extract_dir=tmp_path / "after").collection
  assert getattr(after, attribute) == getattr(before, attribute)


def test_the_rebuild_carries_the_exported_file(packages, tmp_path):
  _, rebuilt = packages
  package = Package.read_apkg(rebuilt, extract_dir=tmp_path)
  assert {Path(path).name: Path(path).read_bytes() for path in package.media} == AUDIO


# ------------------------------------ Through Anki -------------------------------------


def imported(package: Path, collection: Path) -> dict[str, Any]:
  """What a user importing `package` into an empty collection ends up with. Cards are
  keyed by note guid and template, since the ids of notes and cards are the importer's."""
  col = AnkiCollection(str(collection))
  col.import_anki_package(
    ImportAnkiPackageRequest(
      package_path=str(package),
      options=ImportAnkiPackageOptions(with_scheduling=True),
    )
  )
  assert col.db is not None
  notes = {col.get_note(nid).guid: col.get_note(nid) for nid in col.find_notes("")}
  cards = [col.get_card(cid) for cid in col.find_cards("")]
  result = {
    "notes": {guid: (note.fields, note.tags) for guid, note in notes.items()},
    "cards": {
      (col.get_note(card.nid).guid, card.ord): (
        col.decks.name(card.did),
        card.type,
        card.queue,
        card.due,
        card.ivl,
        card.factor,
        card.reps,
      )
      for card in cards
    },
    "decks": sorted(deck.name for deck in col.decks.all_names_and_ids()),
    "reviews": col.db.scalar("select count() from revlog"),
    "media": {
      file.name: file.read_bytes()
      for file in Path(col.media.dir()).iterdir()
      if file.is_file()
    },
    "missing media": list(col.media.check().missing),
  }
  col.close()
  return result


def test_anki_imports_the_rebuild_as_it_imports_the_export(packages, tmp_path):
  """The criterion read-update-write rests on: whatever genanki2 does to a package it did
  not write, the user importing it gets the notes, cards, decks and files they would
  have got from the original."""
  source, rebuilt = packages
  expected = imported(source, tmp_path / "from_export.anki2")
  assert imported(rebuilt, tmp_path / "from_rebuild.anki2") == expected


def test_the_import_is_not_empty(packages, tmp_path):
  """Guards the comparison above against two imports that both lost everything."""
  source, _ = packages
  result = imported(source, tmp_path / "collection.anki2")
  assert len(result["notes"]) == 3
  assert len(result["cards"]) == 6
  assert result["reviews"] == 1
  assert result["media"] == AUDIO


# ------------------------------ Anki's older-client export ------------------------------


def test_an_export_for_older_clients_is_refused_naming_its_version(tmp_path):
  """Anki's "support older Anki versions" export writes schema 11 in
  `collection.anki21`, which is outside what genanki2 reads."""
  with pytest.raises(CollectionReadError, match="declares 11"):
    Package.read_apkg(export(tmp_path, legacy=True))
