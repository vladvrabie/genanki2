"""Does a package genanki2 wrote in the legacy envelope read back as it was written?

    uv run --with pytest pytest tests/test_legacy_envelope_roundtrip.py

The legacy envelope is the one with no `meta` entry: an uncompressed collection under
`collection.anki2`, a JSON media map, and one numbered entry per file. Reading it is
the half of read-update-write that lets a rebuild take last release's package as its
input, so the writer and the reader have to agree on every part of the zip without a
`meta` entry to tell either of them what to expect.

`Package.media` holds paths on disk, so media only come back when the read is given a
directory to put them in. That is what these assertions are about: the bytes in the zip
reach the disk under the name the map gave them, and the paths handed back point at
them.
"""

import itertools
from pathlib import Path

import pytest

from genanki2.builtins import (
  BASIC_AND_REVERSED_NOTETYPE,
  BASIC_NOTETYPE,
  DEFAULT_DECK_CONFIG,
  default_config_entries,
)
from genanki2.errors import Genanki2Warning
from genanki2.models.card import Card
from genanki2.models.collection import Collection
from genanki2.models.deck import Deck
from genanki2.models.note import Note
from genanki2.models.notetype import Notetype
from genanki2.models.package import Package

DECK_NAME = "Legacy"
SUBDECK_NAME = "Legacy::Nested"
SUBDECK_TAG = "Capitals"

AUDIO = {"front.mp3": b"ID3-front", "back.mp3": b"ID3-back"}

NOTES = [
  (BASIC_NOTETYPE, ["What is the capital of Germany?[sound:front.mp3]", "Berlin"]),
  (BASIC_AND_REVERSED_NOTETYPE, ["Capital of France[sound:back.mp3]", "Paris"]),
]

NOTE_COUNT = len(NOTES)
CARD_COUNT = sum(len(notetype.templates) for notetype, _ in NOTES)
DECK_COUNT = 2  # Legacy, Legacy::Nested: no card sits in Default, so it is not gathered


def add_note(
  collection: Collection,
  deck: Deck,
  notetype: Notetype,
  fields: list[str],
  position: int,
  tags: list[str] | None = None,
) -> Note:
  """Add a note and one card per template, all sharing the note's queue position."""
  note = Note(notetype=notetype, fields=fields, tags=tags or [])
  collection.notes.append(note)
  collection.cards.extend(
    Card(note=note, deck=deck, template_ord=ordinal, due=position)
    for ordinal in range(len(notetype.templates))
  )
  return note


@pytest.fixture
def media_dir(tmp_path):
  """The files the package carries, written where a caller would have them."""
  path = tmp_path / "source_media"
  path.mkdir()
  for name, payload in AUDIO.items():
    (path / name).write_bytes(payload)
  return path


@pytest.fixture
def written_package(tmp_path, media_dir):
  """A legacy package holding two decks, two notes and the audio they reference."""
  collection = Collection(
    config=default_config_entries(), deck_configs=[DEFAULT_DECK_CONFIG]
  )
  positions = itertools.count(1)

  deck = Deck(name=DECK_NAME)
  collection.decks.append(deck)
  add_note(collection, deck, *NOTES[0], next(positions))

  subdeck = Deck(name=SUBDECK_NAME)
  collection.decks.append(subdeck)
  add_note(collection, subdeck, *NOTES[1], next(positions), tags=[SUBDECK_TAG])

  package = Package(
    collection=collection.collect_notetypes(),
    media=[str(media_dir / name) for name in AUDIO],
  )
  path = tmp_path / "deck.apkg"
  package.check().warn()._write_v18_legacy_1(path)
  return path


def test_a_legacy_package_reads_back_with_its_media(written_package, tmp_path):
  """The zip names its files by position and the map alone ties `"0"` to `front.mp3`,
  so a reader that loses the map writes the right bytes under the wrong names."""
  extract_dir = tmp_path / "extracted"

  package = Package.read_apkg(written_package, extract_dir)

  assert [Path(path).name for path in package.media] == list(AUDIO)
  assert {path.name: path.read_bytes() for path in extract_dir.iterdir()} == {
    "collection.anki2": (extract_dir / "collection.anki2").read_bytes(),
    **AUDIO,
  }
  for path in package.media:
    assert Path(path).read_bytes() == AUDIO[Path(path).name]


def test_reading_without_a_directory_leaves_the_media_behind(written_package):
  """`Package.media` is paths on disk, so with nowhere to put them the files stay in
  the zip and writing the package back would drop every one."""
  with pytest.warns(Genanki2Warning):
    package = Package.read_apkg(written_package)

  assert package.media == []


def test_the_collection_comes_back_as_it_was_written(written_package, tmp_path):
  """What the round trip is ultimately for: the notes, cards and decks a rebuild has
  to start from."""
  package = Package.read_apkg(written_package, tmp_path / "extracted")
  collection = package.collection

  assert len(collection.notes) == NOTE_COUNT
  assert len(collection.cards) == CARD_COUNT
  assert len(collection.decks) == DECK_COUNT
  assert {deck.name for deck in collection.decks} >= {DECK_NAME, SUBDECK_NAME}
  assert [note.fields for note in collection.notes] == [fields for _, fields in NOTES]
  assert any(SUBDECK_TAG in note.tags for note in collection.notes)
