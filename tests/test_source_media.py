"""Does a package keep a handle on the media files of the package it was read from?

    uv run --with pytest pytest tests/test_source_media.py

A rebuild reads last release's package and writes it back. Its media files are usually
the bulk of the zip and usually unchanged, so instead of extracting them to disk the
package can remember where they are: the source's path, its envelope, and the name each
file is stored under after repair. The writer copies from there, so the source has to
still be on disk, and unchanged, when the write happens.

These tests cover what is held after `read_apkg(keep_source_media=True)` and
`add_source_media()`, what is refused, and what the writers pack once a source is set.
"""

import hashlib
import json
import warnings
import zipfile
from pathlib import Path

import pytest

from genanki2.builtins import BASIC_NOTETYPE
from genanki2.errors import Genanki2Error, Genanki2Warning, PackageReadError
from genanki2.models.collection import Collection
from genanki2.models.config import ConfigEntry
from genanki2.models.media_entries import ExtractedEntry, MediaEntries
from genanki2.models.note import Note
from genanki2.models.package import CollectionPackage, Package
from genanki2.models.package_metadata import PackageMetadata

Version = PackageMetadata.Version

# Keeps the writer's v1 scheduler warning out of the way of what these tests are about.
SCHEDULER_V2 = [ConfigEntry(key="schedVer", value=b"2")]

AUDIO = {"front.mp3": b"ID3-front", "back.mp3": b"ID3-back"}

WRITERS = {
  Version.LEGACY_1: "_write_v18_legacy_1",
  Version.LEGACY_2: "_write_v18_legacy_2",
  Version.LATEST: "_write_v18_latest",
}


def write_package(directory: Path, version: Version, audio: dict[str, bytes]) -> Path:
  """Write a package carrying `audio` in the envelope `version`, skipping the checks."""
  media_dir = directory / "media"
  media_dir.mkdir(parents=True, exist_ok=True)
  for name, payload in audio.items():
    (media_dir / name).write_bytes(payload)
  package = Package(
    collection=Collection(config=SCHEDULER_V2),
    media=[str(media_dir / name) for name in audio],
  )
  path = directory / "deck.apkg"
  getattr(package, WRITERS[version])(path)
  return path


def with_legacy_media_map(path: Path, media_map: dict[str, str]) -> Path:
  """Rewrite the legacy package at `path` so its `media` entry holds `media_map`."""
  rewritten = path.with_name("rewritten.apkg")
  with zipfile.ZipFile(path) as source, zipfile.ZipFile(rewritten, "w") as target:
    for info in source.infolist():
      if info.filename != "media":
        target.writestr(info, source.read(info))
    target.writestr("media", json.dumps(media_map))
  return rewritten


def a_source_with_notes(
  directory: Path, media_map: dict[str, str], *references: str
) -> Path:
  """A legacy package whose `media` entry holds `media_map` and whose notes reference
  `references`, one note each.

  The keys of `media_map` have to be `"0"`, `"1"` and so on, the entries the writer
  numbers its files with.
  """
  media_dir = directory / "media"
  media_dir.mkdir(parents=True, exist_ok=True)
  paths: list[str] = []
  for key in media_map:
    path = media_dir / f"file{key}.mp3"
    path.write_bytes(b"ID3")
    paths.append(str(path))
  notes = [
    Note(notetype=BASIC_NOTETYPE, fields=[f"[sound:{name}]", "back"])
    for name in references
  ]
  collection = Collection(config=SCHEDULER_V2, notes=notes).collect_notetypes()
  path = directory / "deck.apkg"
  Package(collection=collection, media=paths)._write_v18_legacy_1(path)
  return with_legacy_media_map(path, media_map)


def front_fields(package: Package) -> list[str]:
  return [note.fields[0] for note in package.collection.notes]


def without_entry(path: Path, entry: str) -> Path:
  """Rewrite the package at `path` without the zip entry `entry`."""
  stripped = path.with_name("stripped.apkg")
  with zipfile.ZipFile(path) as source, zipfile.ZipFile(stripped, "w") as target:
    for info in source.infolist():
      if info.filename != entry:
        target.writestr(info, source.read(info))
  return stripped


def dropped_media_warnings(record: list[warnings.WarningMessage]) -> list[str]:
  """The warnings saying a read left the package's media files behind."""
  return [
    str(warning.message)
    for warning in record
    if issubclass(warning.category, Genanki2Warning)
    and "`Package.media` is empty" in str(warning.message)
  ]


def media_in(path: Path, extract_dir: Path) -> dict[str, bytes]:
  """The name and the content of every media file the package at `path` carries."""
  package = Package.read_apkg(path, extract_dir=extract_dir)
  return {Path(file).name: Path(file).read_bytes() for file in package.media}


@pytest.fixture(params=list(WRITERS))
def version(request) -> Version:
  return request.param


@pytest.fixture
def written_package(tmp_path, version: Version) -> Path:
  """A package carrying two audio files, in each envelope genanki2 can write."""
  return write_package(tmp_path, version, AUDIO)


@pytest.fixture
def opened_zips(monkeypatch) -> list[zipfile.ZipFile]:
  """Every `ZipFile` opened from here on, to be looked at for a handle left behind."""
  opened: list[zipfile.ZipFile] = []
  init = zipfile.ZipFile.__init__

  def recording_init(self: zipfile.ZipFile, *args, **kwargs) -> None:
    opened.append(self)
    init(self, *args, **kwargs)

  monkeypatch.setattr(zipfile.ZipFile, "__init__", recording_init)
  return opened


# ----------------------------------- read_apkg ----------------------------------------


def test_keeping_the_source_holds_its_path_envelope_and_names(written_package, version):
  """The writer needs all three: where to reopen, how to read what it finds there, and
  which zip entry holds each file under which name."""
  package = Package.read_apkg(written_package, keep_source_media=True)

  source = package._source_metadata
  assert source is not None
  assert source.path == written_package.resolve()
  assert source.version == version
  assert source.extraction_entries == (
    ExtractedEntry("0", "front.mp3", "front.mp3"),
    ExtractedEntry("1", "back.mp3", "back.mp3"),
  )


def test_keeping_the_source_leaves_media_empty_without_warning(written_package):
  """The files are not on disk, so `media` stays empty; the warning about dropped media
  would be wrong, since the source still holds them."""
  with warnings.catch_warnings(record=True) as record:
    warnings.simplefilter("always")
    package = Package.read_apkg(written_package, keep_source_media=True)

  assert package.media == []
  assert dropped_media_warnings(record) == []


def test_the_source_path_is_resolved(written_package, monkeypatch):
  """A relative path would point somewhere else once the working directory moves
  between the read and the write."""
  monkeypatch.chdir(written_package.parent)

  package = Package.read_apkg(written_package.name, keep_source_media=True)

  assert package._source_metadata is not None
  assert package._source_metadata.path == written_package.resolve()


def test_a_source_under_a_tilde_is_kept_as_the_file_in_the_home_folder(
  tmp_path, written_package, monkeypatch
):
  """The stored path is reopened at the write, after the working directory has moved,
  so it has to be the home folder's file rather than a `./~` that never existed."""
  monkeypatch.setenv("HOME", str(written_package.parent))
  monkeypatch.chdir(tmp_path)
  package = Package.read_apkg("~/" + written_package.name, keep_source_media=True)
  (tmp_path / "elsewhere").mkdir()
  monkeypatch.chdir(tmp_path / "elsewhere")
  destination = tmp_path / "rebuilt" / "deck.apkg"

  package.write_apkg(destination)

  assert media_in(destination, tmp_path / "extracted") == AUDIO


def test_by_default_no_source_is_kept(written_package):
  with pytest.warns(Genanki2Warning, match="keep_source_media=True"):
    package = Package.read_apkg(written_package)

  assert package._source_metadata is None


def test_a_package_without_media_keeps_no_source(tmp_path):
  """With no files to copy, a source would only be a path the write has to find."""
  path = write_package(tmp_path, Version.LATEST, {})

  package = Package.read_apkg(path, keep_source_media=True)

  assert package._source_metadata is None


def test_a_repaired_name_is_the_one_kept(tmp_path):
  """A legacy map can carry a name Anki would not store; the writer has to store the
  repaired one, or the package it writes does not import."""
  path = write_package(tmp_path, Version.LEGACY_1, {"ab.mp3": b"ID3"})
  path = with_legacy_media_map(path, {"0": "a?b.mp3"})

  with pytest.warns(Genanki2Warning, match="'a\\?b.mp3': 'ab.mp3'"):
    package = Package.read_apkg(path, keep_source_media=True)

  source = package._source_metadata
  assert source is not None
  assert source.extraction_entries == (ExtractedEntry("0", "a?b.mp3", "ab.mp3"),)


def test_extracting_and_keeping_the_source_are_refused_together(tmp_path):
  """Refused before the zip is opened: the path does not exist and the directory is not
  created."""
  extract_dir = tmp_path / "extracted"

  with pytest.raises(Genanki2Error, match="mutually exclusive"):
    Package.read_apkg(tmp_path / "missing.apkg", extract_dir, keep_source_media=True)

  assert not extract_dir.exists()


# ------------------------ read_apkg(repair_media_references=True) ---------------------


def test_the_repair_renames_each_reference_to_the_stored_name(tmp_path):
  """The read is the one moment the renames are known to match the collection, so the
  references are rewritten there. A name that needs no repair is left alone."""
  source = a_source_with_notes(
    tmp_path, {"0": "a?b.mp3", "1": "c.mp3"}, "a?b.mp3", "c.mp3"
  )

  package = Package.read_apkg(
    source, keep_source_media=True, repair_media_references=True
  )

  assert front_fields(package) == ["[sound:ab.mp3]", "[sound:c.mp3]"]


def test_the_repair_takes_the_place_of_the_rename_warning(tmp_path):
  """The warning hands the caller renames to apply, and they are already applied."""
  source = a_source_with_notes(tmp_path, {"0": "a?b.mp3"}, "a?b.mp3")

  with warnings.catch_warnings(record=True) as record:
    warnings.simplefilter("always")
    Package.read_apkg(source, keep_source_media=True, repair_media_references=True)

  assert not [w for w in record if "would not store" in str(w.message)]


def test_by_default_the_references_are_left_as_written(tmp_path):
  """The repair rewrites data Anki wrote, so the caller opts in."""
  source = a_source_with_notes(tmp_path, {"0": "a?b.mp3"}, "a?b.mp3")

  with pytest.warns(Genanki2Warning, match="would not store"):
    package = Package.read_apkg(source, keep_source_media=True)

  assert front_fields(package) == ["[sound:a?b.mp3]"]


def test_a_repaired_package_is_written_back_under_the_stored_name(tmp_path):
  """The writer copies the file under the repaired name, and the repaired references
  point at it, so nothing dangles."""
  source = a_source_with_notes(tmp_path, {"0": "a?b.mp3"}, "a?b.mp3")
  package = Package.read_apkg(
    source, keep_source_media=True, repair_media_references=True
  )
  destination = tmp_path / "rebuilt" / "deck.apkg"

  package._write_v18_latest(destination)

  assert media_in(destination, tmp_path / "extracted") == {"ab.mp3": b"ID3"}


def test_the_repair_applies_to_extracted_files(tmp_path):
  """Extraction stores the file under the same repaired name as the source does."""
  source = a_source_with_notes(tmp_path, {"0": "a?b.mp3"}, "a?b.mp3")
  extract_dir = tmp_path / "extracted"

  package = Package.read_apkg(source, extract_dir, repair_media_references=True)

  assert front_fields(package) == ["[sound:ab.mp3]"]
  assert [Path(path).name for path in package.media] == ["ab.mp3"]


def test_a_name_on_two_entries_is_not_repaired(tmp_path):
  """The two copies are stored under two names, and a rename keyed by the shared name
  can send its references to only one of them."""
  source = a_source_with_notes(tmp_path, {"0": "a?b.mp3", "1": "a?b.mp3"}, "a?b.mp3")

  with pytest.warns(Genanki2Warning, match="duplicate filenames"):
    package = Package.read_apkg(
      source, keep_source_media=True, repair_media_references=True
    )

  assert front_fields(package) == ["[sound:a?b.mp3]"]


# --------------------------------- add_source_media -----------------------------------


def test_adding_a_source_holds_what_reading_with_it_holds(written_package):
  """A package built in Python can take its media from a package on disk, and ends up
  holding the same thing a read would have given it."""
  package = Package(collection=Collection(config=SCHEDULER_V2))

  assert package.add_source_media(written_package) is package

  read = Package.read_apkg(written_package, keep_source_media=True)
  assert package._source_metadata == read._source_metadata


def test_adding_a_source_replaces_the_previous_one(tmp_path):
  first = write_package(tmp_path / "first", Version.LATEST, AUDIO)
  second = write_package(tmp_path / "second", Version.LATEST, AUDIO)
  package = Package().add_source_media(first).add_source_media(second)

  assert package._source_metadata is not None
  assert package._source_metadata.path == second.resolve()


def test_a_source_added_under_a_tilde_is_the_file_in_the_home_folder(
  tmp_path, written_package, monkeypatch
):
  """The path is stored and reopened at the write, so it is kept resolved, as
  `read_apkg` keeps it."""
  monkeypatch.setenv("HOME", str(written_package.parent))
  monkeypatch.chdir(tmp_path)

  package = Package().add_source_media("~/" + written_package.name)

  assert package._source_metadata is not None
  assert package._source_metadata.path == written_package.resolve()


def test_removing_the_source_clears_it(written_package):
  package = Package().add_source_media(written_package)

  assert package.remove_source_media() is package
  assert package._source_metadata is None


def test_a_source_without_media_is_refused(tmp_path):
  """Asking for media from a package that has none is a mistake, not a no-op."""
  path = write_package(tmp_path, Version.LATEST, {})

  with pytest.raises(Genanki2Error, match="No source media"):
    Package().add_source_media(path)


def test_a_file_that_is_not_a_zip_is_refused(tmp_path):
  path = tmp_path / "deck.apkg"
  path.write_bytes(b"not a zip")

  with pytest.raises(PackageReadError, match="invalid zip file"):
    Package().add_source_media(path)


def test_a_zip_that_is_not_a_package_is_refused(tmp_path):
  path = tmp_path / "deck.apkg"
  with zipfile.ZipFile(path, "w") as zfile:
    zfile.writestr("0", b"ID3")

  with pytest.raises(PackageReadError, match="does not carry a `meta` entry"):
    Package().add_source_media(path)


def test_a_missing_source_is_refused(tmp_path):
  with pytest.raises(Genanki2Error, match="is missing or not a file"):
    Package().add_source_media(tmp_path / "missing.apkg")


# --------------------------------- What a write packs ---------------------------------


@pytest.fixture(params=list(WRITERS))
def destination_version(request) -> Version:
  """The envelope a rebuild is written in, whichever one it was read from."""
  return request.param


def test_the_source_files_are_packed_into_the_written_package(
  tmp_path, written_package, destination_version: Version
):
  """Every pair of envelopes. Two of the nine disagree about compression, and a copied
  file has to come out the same either way."""
  package = Package.read_apkg(written_package, keep_source_media=True)
  destination = tmp_path / "rebuilt" / "deck.apkg"

  getattr(package, WRITERS[destination_version])(destination)

  assert media_in(destination, tmp_path / "extracted") == AUDIO


def test_the_source_files_are_packed_after_the_media_paths(tmp_path, written_package):
  """One pass over both builds the map, so the order they are packed in is the order
  the map numbers them in."""
  extra = tmp_path / "new" / "extra.mp3"
  extra.parent.mkdir()
  extra.write_bytes(b"ID3-extra")
  package = Package.read_apkg(written_package, keep_source_media=True)
  package.media = [str(extra)]
  destination = tmp_path / "rebuilt" / "deck.apkg"

  package._write_v18_latest(destination)

  assert [entry.name for entry in MediaEntries.from_apkg(destination).entries] == [
    "extra.mp3",
    "front.mp3",
    "back.mp3",
  ]


def test_a_media_path_replaces_the_source_file_of_the_same_name(
  tmp_path, written_package
):
  """Regenerating one file and packing it is the normal update, so the path wins and
  the source's copy is left behind, rather than both being packed under one name."""
  regenerated = tmp_path / "new" / "front.mp3"
  regenerated.parent.mkdir()
  regenerated.write_bytes(b"ID3-regenerated")
  package = Package.read_apkg(written_package, keep_source_media=True)
  package.media = [str(regenerated)]
  destination = tmp_path / "rebuilt" / "deck.apkg"

  package._write_v18_latest(destination)

  assert media_in(destination, tmp_path / "extracted") == {
    "front.mp3": b"ID3-regenerated",
    "back.mp3": AUDIO["back.mp3"],
  }


def test_a_repaired_name_is_the_one_written(tmp_path):
  """The name the read repaired to is the name the rebuilt package stores, or the write
  would hand the broken one back to Anki."""
  source = with_legacy_media_map(
    write_package(tmp_path, Version.LEGACY_1, {"ab.mp3": b"ID3"}), {"0": "a?b.mp3"}
  )
  with pytest.warns(Genanki2Warning, match="'a\\?b.mp3': 'ab.mp3'"):
    package = Package.read_apkg(source, keep_source_media=True)
  destination = tmp_path / "rebuilt" / "deck.apkg"

  package._write_v18_latest(destination)

  assert media_in(destination, tmp_path / "extracted") == {"ab.mp3": b"ID3"}


def test_a_compressed_source_is_copied_without_being_compressed_again(tmp_path):
  """Both sides zstd-compress their entries, so the frame the source holds is stored as
  it is. Compressing again is the expensive half of a rebuild that changed nothing."""
  source = write_package(tmp_path / "source", Version.LATEST, AUDIO)
  package = Package.read_apkg(source, keep_source_media=True)
  destination = tmp_path / "rebuilt" / "deck.apkg"

  package._write_v18_latest(destination)

  with zipfile.ZipFile(source) as before, zipfile.ZipFile(destination) as after:
    assert [after.read("0"), after.read("1")] == [before.read("0"), before.read("1")]


def test_a_copied_entry_describes_the_file_and_not_its_frame(tmp_path):
  """Anki compares the size and the checksum against the file in the media folder,
  which is not compressed, so a reused frame must not reach the entry describing it."""
  source = write_package(tmp_path / "source", Version.LATEST, AUDIO)
  package = Package.read_apkg(source, keep_source_media=True)
  destination = tmp_path / "rebuilt" / "deck.apkg"

  package._write_v18_latest(destination)

  entries = MediaEntries.from_apkg(destination).entries
  assert [(entry.name, entry.size) for entry in entries] == [
    (name, len(content)) for name, content in AUDIO.items()
  ]
  assert entries[0].sha1 == hashlib.sha1(AUDIO["front.mp3"]).digest()


def test_writing_over_the_source_is_refused(written_package, version: Version):
  """The package would go on copying entries by the source's numbering, which the new
  file no longer has, and a file the new package drops would lose its only copy. The
  write raises before anything replaces the source."""
  package = Package.read_apkg(written_package, keep_source_media=True)
  before = written_package.read_bytes()

  with pytest.raises(Genanki2Error, match="Cannot write to"):
    getattr(package, WRITERS[version])(written_package)

  assert written_package.read_bytes() == before


# --------------------------- Is the source still what it was? -------------------------


def test_a_source_that_is_gone_at_write_time_is_refused(tmp_path, written_package):
  """Nothing holds the files open between the read and the write, so the package can be
  moved or deleted in between."""
  package = Package.read_apkg(written_package, keep_source_media=True)
  written_package.unlink()
  destination = tmp_path / "rebuilt" / "deck.apkg"

  with pytest.raises(PackageReadError, match="Missing source package"):
    package._write_v18_latest(destination)

  assert not destination.exists()


def test_a_source_replaced_by_another_envelope_is_refused(tmp_path):
  """The envelope says whether the entries are compressed. Taking the one read earlier
  on trust would pack zstd frames as if they were the media files, which imports."""
  source = write_package(tmp_path / "source", Version.LATEST, AUDIO)
  package = Package.read_apkg(source, keep_source_media=True)
  replacement = write_package(tmp_path / "other", Version.LEGACY_1, AUDIO)
  source.write_bytes(replacement.read_bytes())
  destination = tmp_path / "rebuilt" / "deck.apkg"

  with pytest.raises(PackageReadError, match="Source version mismatch"):
    package._write_v18_latest(destination)

  assert not destination.exists()


def test_a_source_that_lost_a_file_is_refused(tmp_path, written_package):
  """Every name the map holds is copied, so one the zip no longer carries would raise
  in the middle of the write instead of before it."""
  package = Package.read_apkg(written_package, keep_source_media=True)
  written_package.write_bytes(without_entry(written_package, "1").read_bytes())
  destination = tmp_path / "rebuilt" / "deck.apkg"

  with pytest.raises(PackageReadError, match="Missing source entries"):
    package._write_v18_latest(destination)

  assert not destination.exists()


def test_a_source_rewritten_with_another_file_is_refused(tmp_path):
  """The entry numbers still exist, but one of them now holds other bytes. The latest
  map records each file's SHA-1, so its CRC changes with the file."""
  source = write_package(tmp_path / "source", Version.LATEST, AUDIO)
  package = Package.read_apkg(source, keep_source_media=True)
  changed = AUDIO | {"front.mp3": b"ID3-changed"}
  replacement = write_package(tmp_path / "other", Version.LATEST, changed)
  source.write_bytes(replacement.read_bytes())
  destination = tmp_path / "rebuilt" / "deck.apkg"

  with pytest.raises(PackageReadError, match="Source media map changed"):
    package._write_v18_latest(destination)

  assert not destination.exists()


def test_a_source_that_lost_its_media_map_is_refused(tmp_path, written_package):
  """A package without a `media` entry reads as one without media files, so it is a
  valid replacement, and the comparison must not raise a bare `KeyError`."""
  package = Package.read_apkg(written_package, keep_source_media=True)
  written_package.write_bytes(without_entry(written_package, "media").read_bytes())
  destination = tmp_path / "rebuilt" / "deck.apkg"

  with pytest.raises(PackageReadError, match="Source media map changed"):
    package._write_v18_latest(destination)

  assert not destination.exists()


def test_the_source_is_closed_after_the_write(tmp_path, written_package, opened_zips):
  package = Package.read_apkg(written_package, keep_source_media=True)
  opened_zips.clear()

  package._write_v18_latest(tmp_path / "rebuilt" / "deck.apkg")

  assert opened_zips and all(zfile.fp is None for zfile in opened_zips)


def test_a_refused_source_is_closed(tmp_path, opened_zips):
  """The checks run on an open zip, and the raise hands it to nobody, so a refusal has
  to close it on the way out."""
  source = write_package(tmp_path / "source", Version.LATEST, AUDIO)
  package = Package.read_apkg(source, keep_source_media=True)
  source.write_bytes(
    write_package(tmp_path / "other", Version.LEGACY_1, AUDIO).read_bytes()
  )
  opened_zips.clear()

  with pytest.raises(PackageReadError):
    package._write_v18_latest(tmp_path / "rebuilt" / "deck.apkg")

  assert [zfile.fp for zfile in opened_zips] == [None]


# -------------------------------- What a writer reads ---------------------------------


def test_without_a_source_there_is_nothing_to_open(tmp_path):
  """A writer asks for the source whether or not one is set, so the answer when none is
  set has to be a context manager too."""
  with Package()._open_source(tmp_path / "rebuilt.apkg") as source:
    assert source is None


def test_the_media_paths_are_read_before_the_source_files(tmp_path, written_package):
  """What the writers loop over: the paths in order, then the source's files, each one
  as the bytes that go in the package the caller asked for."""
  extra = tmp_path / "new" / "extra.mp3"
  extra.parent.mkdir()
  extra.write_bytes(b"ID3-extra")
  package = Package.read_apkg(written_package, keep_source_media=True)
  package.media = [str(extra)]

  with package._open_source(tmp_path / "rebuilt.apkg") as source:
    packed = [(name, content) for name, content, _ in package._iter_media(source)]

  assert packed == [
    ("extra.mp3", b"ID3-extra"),
    ("front.mp3", AUDIO["front.mp3"]),
    ("back.mp3", AUDIO["back.mp3"]),
  ]


def test_only_a_compressed_source_carries_a_frame_to_reuse(
  tmp_path, written_package, version: Version
):
  """The third value is the entry as the source stores it, there for a destination that
  compresses its entries too. A legacy source has nothing to hand over."""
  package = Package.read_apkg(written_package, keep_source_media=True)

  with package._open_source(tmp_path / "rebuilt.apkg") as source:
    frames = [frame for _, _, frame in package._iter_media(source)]

  if version.compressed_entries:
    assert all(frame is not None for frame in frames)
  else:
    assert frames == [None, None]


# -------------------------- What the source counts towards ----------------------------


def test_a_reference_to_a_source_file_is_backed(written_package):
  """The file is neither on disk nor in `media`, but the write puts it in the package,
  so a note naming it is not a dangling reference."""
  package = Package.read_apkg(written_package, keep_source_media=True)
  package.collection.notes = [
    Note(notetype=BASIC_NOTETYPE, fields=["[sound:front.mp3]", "[sound:back.mp3]"])
  ]

  assert package.check_media_references_are_present() is package


def test_a_source_file_no_note_names_is_reported_as_unused(written_package):
  """A file carried over from the source is as much dead weight as one just packed."""
  package = Package.read_apkg(written_package, keep_source_media=True)

  with pytest.warns(Genanki2Warning, match='"back.mp3", "front.mp3"'):
    package.warn_unused_media_files()


def test_a_reference_to_a_renamed_source_file_names_the_rename(tmp_path):
  """The writer copies the file under its repaired name, so the reference dangles, and
  the rename that fixes it is known only to the read."""
  source = a_source_with_notes(tmp_path, {"0": "a?b.mp3"}, "a?b.mp3")
  with pytest.warns(Genanki2Warning, match="would not store"):
    package = Package.read_apkg(source, keep_source_media=True)

  with pytest.raises(Genanki2Error, match="repair_media_references=True") as error:
    package.check_media_references_are_present()

  assert "'a?b.mp3': 'ab.mp3'" in str(error.value)


def test_only_the_renames_a_dangling_reference_needs_are_named(tmp_path):
  source = a_source_with_notes(tmp_path, {"0": "a?b.mp3", "1": "c*d.mp3"}, "a?b.mp3")
  with pytest.warns(Genanki2Warning, match="would not store"):
    package = Package.read_apkg(source, keep_source_media=True)

  with pytest.raises(Genanki2Error) as error:
    package.check_media_references_are_present()

  assert "'a?b.mp3': 'ab.mp3'" in str(error.value)
  assert "c*d.mp3" not in str(error.value)


def test_a_dangling_reference_to_no_renamed_file_names_no_rename(tmp_path):
  """The renames of the source fix nothing here, so offering them would mislead."""
  source = a_source_with_notes(tmp_path, {"0": "a?b.mp3"}, "missing.mp3")
  with pytest.warns(Genanki2Warning, match="would not store"):
    package = Package.read_apkg(source, keep_source_media=True)

  with pytest.raises(Genanki2Error, match="'missing.mp3'") as error:
    package.check_media_references_are_present()

  assert "repaired name" not in str(error.value)


def test_a_collection_package_names_the_rename_in_its_warning(tmp_path):
  """A restore keeps the files already in the media folder, so a dangling reference is
  a warning there, and it names the rename the same way."""
  source = a_source_with_notes(tmp_path, {"0": "a?b.mp3"}, "a?b.mp3")
  with pytest.warns(Genanki2Warning, match="would not store"):
    package = CollectionPackage.read_colpkg(source, keep_source_media=True)

  with pytest.warns(Genanki2Warning, match="does not carry") as record:
    package.warn_missing_media_references()

  message = str(record[0].message)
  assert "'a?b.mp3': 'ab.mp3'" in message
  assert "CollectionPackage.replace_media_references()" in message


def test_a_collection_package_names_no_rename_a_reference_does_not_need(tmp_path):
  source = a_source_with_notes(tmp_path, {"0": "a?b.mp3"}, "missing.mp3")
  with pytest.warns(Genanki2Warning, match="would not store"):
    package = CollectionPackage.read_colpkg(source, keep_source_media=True)

  with pytest.warns(Genanki2Warning, match="'missing.mp3'") as record:
    package.warn_missing_media_references()

  assert "repaired name" not in str(record[0].message)
