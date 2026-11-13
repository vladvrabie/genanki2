"""Do the media files genanki2 packs reach the collection a real Anki imports into?

    uv run --with anki --with pytest pytest tests/test_apkg_media_roundtrip.py

The media map is the only thing tying a numbered zip entry to a file name, and the
importer is the only authority on whether that pairing is right. A package holding a
wrong one still opens and still imports its notes, leaving every one of them pointing
at a file the collection does not have. The current envelope adds a second way to be
wrong that no reader catches: it refuses the whole import over a single unnormalized
name, where the legacy one repairs the name and rewrites the notes that referenced it.

A rebuild is what makes this worth checking against the real thing. A package read with
`keep_source_media=True` never puts its files on disk, so the bytes the importer
receives were copied from one zip into another, under names the read repaired, with
sizes and checksums genanki2 computed rather than read back.

The rebuild replaces the collection it read with one built in Python, as a release
replaces last release's notes, so everything about the media is the rebuild's own.

anki is a test-only dependency, never a dependency of genanki2.
"""

import json
import zipfile
from pathlib import Path

import pytest
from anki.collection import Collection as AnkiCollection
from anki.collection import ImportAnkiPackageRequest

from genanki2.builtins import (
  BASIC_NOTETYPE,
  DEFAULT_DECK_CONFIG,
  default_config_entries,
)
from genanki2.errors import Genanki2Error, Genanki2Warning
from genanki2.models.card import Card
from genanki2.models.collection import Collection
from genanki2.models.deck import Deck
from genanki2.models.note import Note
from genanki2.models.package import Package
from genanki2.models.package_metadata import PackageMetadata

Version = PackageMetadata.Version

WRITERS = {
  Version.LEGACY_1: "_write_v18_legacy_1",
  Version.LEGACY_2: "_write_v18_legacy_2",
  Version.LATEST: "_write_v18_latest",
}

DECK_NAME = "Media roundtrip"
AUDIO = {"front.mp3": b"ID3front", "back.mp3": b"ID3back"}


def media_files(directory: Path, audio: dict[str, bytes]) -> list[str]:
  """Write each file under `directory` and return the paths `Package.media` takes."""
  directory.mkdir(parents=True, exist_ok=True)
  for name, payload in audio.items():
    (directory / name).write_bytes(payload)
  return [str(directory / name) for name in audio]


def a_collection(audio: dict[str, bytes]) -> Collection:
  """One deck holding one note per file, each note referencing its own."""
  collection = Collection(
    config=default_config_entries(), deck_configs=[DEFAULT_DECK_CONFIG]
  )
  deck = Deck(name=DECK_NAME)
  collection.decks.append(deck)
  for position, name in enumerate(audio, start=1):
    note = Note(notetype=BASIC_NOTETYPE, fields=[f"[sound:{name}]", name])
    collection.notes.append(note)
    collection.cards.append(Card(note=note, deck=deck, due=position))
  return collection.collect_notetypes()


def a_package(directory: Path, audio: dict[str, bytes] = AUDIO) -> Package:
  """A package whose notes reference the files it carries, all of them on disk."""
  return Package(
    collection=a_collection(audio), media=media_files(directory / "media", audio)
  )


def a_source(
  directory: Path, version: Version, audio: dict[str, bytes] = AUDIO
) -> Path:
  """A package in the envelope `version`, standing in for last release's file."""
  path = directory / "source.apkg"
  getattr(a_package(directory, audio), WRITERS[version])(path)
  return path


def rebuilt_from(source: Path, audio: dict[str, bytes] = AUDIO) -> Package:
  """The package a rebuild produces: this release's collection, last release's files.

  `audio` names what the notes reference, which is the source's own names until a test
  is about a name the read repaired.
  """
  package = Package.read_apkg(source, keep_source_media=True)
  package.collection = a_collection(audio)
  return package


def a_source_naming_a_file_the_current_envelope_refuses(directory: Path) -> Path:
  """A legacy package whose map spells its one file `a?b.mp3`, which a read repairs."""
  return with_legacy_media_map(
    a_source(directory, Version.LEGACY_1, {"ab.mp3": b"ID3"}), {"0": "a?b.mp3"}
  )


def with_legacy_media_map(path: Path, media_map: dict[str, str]) -> Path:
  """Rewrite the legacy package at `path` so its `media` entry holds `media_map`."""
  rewritten = path.with_name("rewritten.apkg")
  with zipfile.ZipFile(path) as source, zipfile.ZipFile(rewritten, "w") as target:
    for info in source.infolist():
      if info.filename != "media":
        target.writestr(info, source.read(info))
    target.writestr("media", json.dumps(media_map))
  return rewritten


@pytest.fixture
def target(tmp_path):
  """An empty collection standing in for the user Anki is importing into."""
  col = AnkiCollection(str(tmp_path / "target.anki2"))
  yield col
  col.close()


def imported_media(col, path: Path) -> dict[str, bytes]:
  """Import the package at `path`, then read back the target's media folder by name."""
  col.import_anki_package(ImportAnkiPackageRequest(package_path=str(path)))
  folder = Path(col.media.dir())
  return {file.name: file.read_bytes() for file in folder.iterdir() if file.is_file()}


# ------------------------------ Files the package names -------------------------------


@pytest.mark.parametrize("version", list(WRITERS))
def test_the_files_a_package_names_reach_the_collection(tmp_path, target, version):
  """Both envelopes number their entries and name them in a map the other cannot read,
  so each one has to be read by the importer to be known to work."""
  path = tmp_path / "deck.apkg"
  getattr(a_package(tmp_path), WRITERS[version])(path)

  assert imported_media(target, path) == AUDIO


def test_no_note_is_left_pointing_at_a_file_that_did_not_arrive(tmp_path, target):
  """A map naming files the package does not carry imports the notes anyway and leaves
  them dangling. Anki's own media check is what reads the references back against the
  folder, and it is the check a user would run to find the damage."""
  path = tmp_path / "deck.apkg"
  a_package(tmp_path).write_apkg(path)
  target.import_anki_package(ImportAnkiPackageRequest(package_path=str(path)))

  assert list(target.media.check().missing) == []


def test_a_file_no_note_references_does_not_cross(tmp_path, target):
  """The importer takes the files the imported notes name and leaves the rest in the
  package. An unreferenced file therefore costs the package its size and nothing in the
  collection, which is what `Package.warn_unused_media_files()` warns about."""
  path = tmp_path / "deck.apkg"
  package = a_package(tmp_path)
  package.media = media_files(tmp_path / "extra", {**AUDIO, "spare.mp3": b"ID3spare"})
  with pytest.warns(Genanki2Warning, match="spare.mp3"):
    package.write_apkg(path)

  assert imported_media(target, path) == AUDIO


# --------------------------- Files copied from the source -----------------------------


@pytest.mark.parametrize("version", list(WRITERS))
def test_a_rebuild_carries_over_every_file_of_the_source(tmp_path, target, version):
  """The files never touch the disk between the two packages: they are read out of the
  source zip and written into the destination, so what reaches the importer is what
  that copy produced, in whichever envelope the source was written in."""
  source = a_source(tmp_path / "last", version)
  rebuilt = tmp_path / "deck.apkg"

  rebuilt_from(source).write_apkg(rebuilt)

  assert imported_media(target, rebuilt) == AUDIO


def test_a_rebuild_leaves_no_note_pointing_at_a_missing_file(tmp_path, target):
  """The names come from the source's map and the references from this release's notes,
  so the two agreeing is what the rebuild has to get right."""
  source = a_source(tmp_path / "last", Version.LATEST)
  rebuilt = tmp_path / "deck.apkg"
  rebuilt_from(source).write_apkg(rebuilt)
  target.import_anki_package(ImportAnkiPackageRequest(package_path=str(rebuilt)))

  assert list(target.media.check().missing) == []


def test_a_file_on_disk_is_the_one_that_arrives(tmp_path, target):
  """A file replaced since last release is in `media` under a name the source also
  carries. The package writes one entry for that name, and it holds the new bytes."""
  source = a_source(tmp_path / "last", Version.LATEST)
  package = rebuilt_from(source)
  package.media = media_files(tmp_path / "new", {"front.mp3": b"ID3recorded-again"})
  rebuilt = tmp_path / "deck.apkg"

  package.write_apkg(rebuilt)

  assert imported_media(target, rebuilt) == {
    "front.mp3": b"ID3recorded-again",
    "back.mp3": AUDIO["back.mp3"],
  }


def test_a_name_the_source_cannot_keep_arrives_repaired(tmp_path, target):
  """A legacy package can carry a name the current envelope refuses, and an entry
  holding one fails the import outright rather than losing the one file. The read
  repairs the name and the rebuild stores the file under it."""
  source = a_source_naming_a_file_the_current_envelope_refuses(tmp_path / "last")
  package = rebuilt_from(source, {"ab.mp3": b"ID3"})
  rebuilt = tmp_path / "deck.apkg"

  package.write_apkg(rebuilt)

  assert imported_media(target, rebuilt) == {"ab.mp3": b"ID3"}


def test_a_reference_the_repair_left_behind_is_refused(tmp_path):
  """The notes still spell the name the source shipped, so the package would import
  with its file under one name and every reference to it under another. The write is
  refused instead, which is the whole reason the read reports the renames."""
  source = a_source_naming_a_file_the_current_envelope_refuses(tmp_path / "last")
  package = rebuilt_from(source, {"a?b.mp3": b"ID3"})

  with pytest.raises(Genanki2Error, match="a\\?b.mp3"):
    package.write_apkg(tmp_path / "deck.apkg")


def test_repaired_references_reach_files_that_are_there(tmp_path, target):
  """`replace_media_references()` is the repair the read's mapping is for. Taking it
  puts every note back on the name the file is stored under."""
  source = a_source_naming_a_file_the_current_envelope_refuses(tmp_path / "last")
  package = rebuilt_from(source, {"a?b.mp3": b"ID3"})
  package.replace_media_references({"a?b.mp3": "ab.mp3"})
  rebuilt = tmp_path / "deck.apkg"

  package.write_apkg(rebuilt)

  assert imported_media(target, rebuilt) == {"ab.mp3": b"ID3"}
  assert list(target.media.check().missing) == []
