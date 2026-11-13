"""Do the readers that take a package apart one piece at a time agree with `read_apkg`?

    uv run --with pytest pytest tests/test_apkg_readers.py

`Package.read_apkg` is assembled from smaller readers: opening the zip, deciding the
envelope, reading the collection, reading the media map and extracting the files. Each
of them also has a variant taking a path, for a caller who wants one piece of a package
without the rest. These tests run each reader against a package written in every
envelope genanki2 can write.
"""

import warnings
import zipfile
from pathlib import Path

import pytest

from genanki2.errors import Genanki2Error, Genanki2Warning, PackageReadError
from genanki2.io_utils import open_apkg
from genanki2.models.collection import Collection
from genanki2.models.config import ConfigEntry
from genanki2.models.media_entries import (
  ExtractedEntry,
  MediaEntries,
  extract_apkg_media,
  extract_zipfile_media,
  read_zipfile_media_entries,
)
from genanki2.models.package import Package
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


def a_zip(path: Path, *entries: tuple[str, bytes]) -> Path:
  """A zip holding `entries` in order, duplicate names included."""
  with warnings.catch_warnings():
    warnings.simplefilter("ignore", UserWarning)  # zipfile warns on a duplicate name
    with zipfile.ZipFile(path, "w") as zfile:
      for name, data in entries:
        zfile.writestr(name, data)
  return path


@pytest.fixture(params=list(WRITERS))
def version(request) -> Version:
  return request.param


@pytest.fixture
def written_package(tmp_path, version: Version) -> Path:
  """A package carrying two audio files, in each envelope genanki2 can write."""
  return write_package(tmp_path, version, AUDIO)


@pytest.fixture
def not_a_package(tmp_path) -> Path:
  """A valid zip naming neither `meta` nor a collection entry."""
  return a_zip(tmp_path / "deck.apkg", ("0", b"ID3"))


# ------------------------------------- open_apkg --------------------------------------


def test_opening_returns_the_zip_open(written_package):
  with open_apkg(written_package) as zfile:
    assert "media" in zfile.namelist()


def test_opening_a_file_that_is_not_a_zip_is_refused(tmp_path):
  path = tmp_path / "deck.apkg"
  path.write_bytes(b"not a zip")

  with pytest.raises(PackageReadError, match="invalid zip file"):
    open_apkg(path)


def test_opening_a_zip_with_two_entries_under_one_name_is_refused(tmp_path):
  """Reading that name would pick one of the two arbitrarily."""
  path = a_zip(tmp_path / "deck.apkg", ("media", b"{}"), ("media", b"{}"))

  with pytest.raises(PackageReadError, match="Ambiguous package entries"):
    open_apkg(path)


def test_a_refused_zip_is_closed(tmp_path, monkeypatch):
  """The raise hands the zip to nobody, so only `open_apkg` can close it. Left to the
  garbage collector, the file stays open for as long as the traceback lives."""
  path = a_zip(tmp_path / "deck.apkg", ("media", b"{}"), ("media", b"{}"))
  opened: list[zipfile.ZipFile] = []
  init = zipfile.ZipFile.__init__

  def recording_init(self: zipfile.ZipFile, *args, **kwargs) -> None:
    opened.append(self)
    init(self, *args, **kwargs)

  monkeypatch.setattr(zipfile.ZipFile, "__init__", recording_init)

  with pytest.raises(PackageReadError):
    open_apkg(path)

  assert [zfile.fp for zfile in opened] == [None]


@pytest.mark.parametrize("name", ["missing.apkg", "a-directory"])
def test_opening_a_missing_file_or_a_directory_is_refused(tmp_path, name: str):
  (tmp_path / "a-directory").mkdir()

  with pytest.raises(Genanki2Error, match="is missing or not a file"):
    open_apkg(tmp_path / name)


@pytest.mark.parametrize("as_path", [False, True])
def test_a_path_starting_with_a_tilde_opens_the_package_in_the_home_folder(
  tmp_path, monkeypatch, as_path: bool
):
  """Without the expansion, `~` is a folder of that name under the working directory,
  and the file is not found."""
  home = tmp_path / "home"
  home.mkdir()
  a_zip(home / "deck.apkg", ("media", b"{}"))
  monkeypatch.setenv("HOME", str(home))
  monkeypatch.chdir(tmp_path)

  with open_apkg(Path("~/deck.apkg") if as_path else "~/deck.apkg") as zfile:
    assert Path(str(zfile.filename)) == (home / "deck.apkg").resolve()


def test_a_symlink_to_a_package_is_opened_through(tmp_path):
  target = a_zip(tmp_path / "deck.apkg", ("media", b"{}"))
  link = tmp_path / "link.apkg"
  link.symlink_to(target)

  with open_apkg(link) as zfile:
    assert zfile.namelist() == ["media"]


def test_a_symlink_to_nothing_is_refused_as_missing(tmp_path):
  link = tmp_path / "link.apkg"
  link.symlink_to(tmp_path / "gone.apkg")

  with pytest.raises(Genanki2Error, match="is missing or not a file"):
    open_apkg(link)


# ----------------------------- Version.raise_if_unknown -------------------------------


@pytest.mark.parametrize("known", [Version.LEGACY_1, Version.LEGACY_2, Version.LATEST])
def test_a_known_version_passes(known: Version):
  assert known.raise_if_unknown() is known


def test_an_unknown_version_is_refused_naming_the_package(tmp_path):
  path = tmp_path / "deck.apkg"

  with pytest.raises(PackageReadError, match=str(path.resolve())):
    _ = Version.UNKNOWN.raise_if_unknown(path)


def test_an_unknown_version_is_refused_without_a_path():
  with pytest.raises(PackageReadError, match=r"^Cannot read the package: "):
    _ = Version.UNKNOWN.raise_if_unknown()


# ----------------------------- PackageMetadata.from_apkg ------------------------------


def test_metadata_from_a_package_holds_its_envelope(written_package, version: Version):
  assert PackageMetadata.from_apkg(written_package).version == version


def test_metadata_from_a_zip_that_is_not_a_package_is_unknown(not_a_package):
  """Returned rather than raised: the caller decides through `raise_if_unknown()`."""
  assert PackageMetadata.from_apkg(not_a_package).version == Version.UNKNOWN


# ------------------------------ MediaEntries.from_zipfile -----------------------------


def test_the_media_map_names_every_file_in_order(written_package, version: Version):
  with open_apkg(written_package) as zfile:
    media_entries = MediaEntries.from_zipfile(zfile, version)

  assert [entry.name for entry in media_entries.entries] == list(AUDIO)


def test_a_latest_media_map_carries_sizes_and_digests(tmp_path):
  path = write_package(tmp_path, Version.LATEST, AUDIO)

  with open_apkg(path) as zfile:
    media_entries = MediaEntries.from_zipfile(zfile, Version.LATEST)

  expected = [MediaEntries.MediaEntry.from_file_bytes(n, b) for n, b in AUDIO.items()]
  assert media_entries.entries == expected
  assert not media_entries.are_legacy()


@pytest.mark.parametrize("legacy", [Version.LEGACY_1, Version.LEGACY_2])
def test_a_legacy_media_map_carries_zip_entries_only(tmp_path, legacy: Version):
  path = write_package(tmp_path, legacy, AUDIO)

  with open_apkg(path) as zfile:
    media_entries = MediaEntries.from_zipfile(zfile, legacy)

  assert media_entries.are_legacy()
  assert [e.legacy_zip_filename for e in media_entries.entries] == [0, 1]
  assert {(e.size, e.sha1) for e in media_entries.entries} == {(0, b"")}


def test_a_package_without_a_media_entry_has_no_media(tmp_path):
  path = a_zip(tmp_path / "deck.apkg", ("collection.anki2", b""))

  with open_apkg(path) as zfile:
    assert MediaEntries.from_zipfile(zfile, Version.LEGACY_1).entries == []


@pytest.mark.parametrize(
  ("declared", "media"),
  [(Version.LATEST, b"not zstd"), (Version.LEGACY_1, b"not json")],
)
def test_a_corrupt_media_entry_is_refused(tmp_path, declared: Version, media: bytes):
  path = a_zip(tmp_path / "deck.apkg", ("media", media))

  with open_apkg(path) as zfile, pytest.raises(PackageReadError):
    MediaEntries.from_zipfile(zfile, declared)


# ------------------------------- MediaEntries.from_apkg -------------------------------


def test_the_media_map_from_a_path_is_the_one_from_the_zip(
  written_package, version: Version
):
  with open_apkg(written_package) as zfile:
    expected = MediaEntries.from_zipfile(zfile, version)

  assert MediaEntries.from_apkg(written_package) == expected


def test_the_media_map_of_a_zip_that_is_not_a_package_is_refused(not_a_package):
  with pytest.raises(PackageReadError, match="does not carry a `meta` entry"):
    MediaEntries.from_apkg(not_a_package)


# ----------------------------- read_zipfile_media_entries -----------------------------


def test_reading_the_media_returns_the_map_and_where_each_file_goes(
  written_package, version: Version
):
  with open_apkg(written_package) as zfile:
    media_entries, extraction_entries = read_zipfile_media_entries(zfile, version)

  assert [entry.name for entry in media_entries.entries] == list(AUDIO)
  assert extraction_entries == (
    ExtractedEntry("0", "front.mp3", "front.mp3"),
    ExtractedEntry("1", "back.mp3", "back.mp3"),
  )


def test_extracting_keeps_the_collection_name_free(tmp_path):
  """A media file named like the collection would overwrite the extracted database."""
  path = write_package(tmp_path, Version.LATEST, {"collection.anki21b": b"ID3"})

  with open_apkg(path) as zfile:
    _, without_dir = read_zipfile_media_entries(zfile, Version.LATEST)
    with pytest.warns(Genanki2Warning, match="collection.anki21b"):
      _, with_dir = read_zipfile_media_entries(
        zfile, Version.LATEST, for_extraction=True
      )

  assert without_dir == (
    ExtractedEntry("0", "collection.anki21b", "collection.anki21b"),
  )
  assert with_dir[0].disk_name != "collection.anki21b"


def test_an_entry_nothing_reads_is_warned_about(tmp_path):
  path = a_zip(
    tmp_path / "deck.apkg",
    ("collection.anki2", b""),
    ("media", b'{"0": "a.mp3"}'),
    ("0", b"ID3"),
    ("stray", b""),
  )

  with open_apkg(path) as zfile, pytest.warns(Genanki2Warning, match="'stray'"):
    read_zipfile_media_entries(zfile, Version.LEGACY_1)


def test_a_map_naming_an_entry_the_zip_does_not_carry_is_refused(tmp_path):
  path = a_zip(
    tmp_path / "deck.apkg",
    ("collection.anki2", b""),
    ("media", b'{"0": "a.mp3", "7": "b.mp3"}'),
    ("0", b"ID3"),
  )

  with open_apkg(path) as zfile, pytest.raises(PackageReadError, match="'b.mp3'"):
    read_zipfile_media_entries(zfile, Version.LEGACY_1)


def test_reading_warns_about_duplicate_names_and_renames(tmp_path):
  """`MediaEntries.extraction_entries()` warns about nothing, so the reader emits
  both."""
  path = a_zip(
    tmp_path / "deck.apkg",
    ("collection.anki2", b""),
    ("media", b'{"0": "a.mp3", "1": "a.mp3", "2": "a?b.mp3"}'),
    ("0", b"ID3"),
    ("1", b"ID3"),
    ("2", b"ID3"),
  )

  with (
    open_apkg(path) as zfile,
    pytest.warns(Genanki2Warning, match="duplicate filenames"),
    pytest.warns(Genanki2Warning, match=r"'a\?b\.mp3': 'ab\.mp3'"),
  ):
    read_zipfile_media_entries(zfile, Version.LEGACY_1)


def test_the_stray_entry_warning_points_at_the_direct_caller(tmp_path):
  path = a_zip(
    tmp_path / "deck.apkg",
    ("collection.anki2", b""),
    ("media", b"{}"),
    ("stray", b""),
  )

  with open_apkg(path) as zfile, pytest.warns(Genanki2Warning) as record:
    read_zipfile_media_entries(zfile, Version.LEGACY_1)

  assert [w.filename for w in record if "'stray'" in str(w.message)] == [__file__]


# ------------------------------- extract_zipfile_media --------------------------------


def test_extracting_writes_each_file_under_its_name(tmp_path, version: Version):
  path = write_package(tmp_path / "package", version, AUDIO)
  extract_dir = tmp_path / "extracted"
  extract_dir.mkdir()

  with open_apkg(path) as zfile:
    _, extraction_entries = read_zipfile_media_entries(
      zfile, version, for_extraction=True
    )
    media = extract_zipfile_media(zfile, version, extract_dir, extraction_entries)

  assert media == [str(extract_dir / name) for name in AUDIO]
  assert {Path(p).name: Path(p).read_bytes() for p in media} == AUDIO


def test_a_corrupt_media_file_is_refused_naming_its_entry(tmp_path):
  path = a_zip(tmp_path / "deck.apkg", ("0", b"not zstd"))

  extraction_entries = (ExtractedEntry("0", "a.mp3", "a.mp3"),)

  with open_apkg(path) as zfile, pytest.raises(PackageReadError, match="'0'"):
    extract_zipfile_media(zfile, Version.LATEST, tmp_path, extraction_entries)


# --------------------------------- extract_apkg_media ---------------------------------


def test_extracting_from_a_path_creates_the_directory(tmp_path, version: Version):
  path = write_package(tmp_path / "package", version, AUDIO)
  extract_dir = tmp_path / "missing" / "extracted"

  media = extract_apkg_media(path, extract_dir)

  assert {Path(p).name: Path(p).read_bytes() for p in media} == AUDIO
  assert all(Path(p).parent == extract_dir.resolve() for p in media)


def test_extracting_from_a_zip_that_is_not_a_package_is_refused(
  not_a_package, tmp_path
):
  extract_dir = tmp_path / "extracted"

  with pytest.raises(PackageReadError, match="does not carry a `meta` entry"):
    extract_apkg_media(not_a_package, extract_dir)

  assert not extract_dir.exists()


# ------------------------------ Collection.from_zipfile -------------------------------


def test_the_collection_is_read_from_its_envelope_entry(
  written_package, version: Version
):
  """The package holds an empty collection, so its one config entry is what tells it
  apart from anything else the zip carries."""
  with open_apkg(written_package) as zfile:
    collection = Collection.from_zipfile(zfile, version)

  assert collection.get_config_entry("schedVer") == SCHEDULER_V2[0]


def test_the_collection_is_copied_to_the_extraction_directory(
  written_package, version: Version, tmp_path
):
  extract_dir = tmp_path / "extracted"
  extract_dir.mkdir()

  with open_apkg(written_package) as zfile:
    Collection.from_zipfile(zfile, version, extract_dir)

  assert (extract_dir / version.collection_entry).is_file()


def test_a_missing_collection_entry_is_refused(tmp_path):
  """A latest package whose modern collection is missing still carries the stub, and
  the stub must not be read in its place."""
  path = a_zip(tmp_path / "deck.apkg", ("collection.anki2", b""))

  with open_apkg(path) as zfile, pytest.raises(PackageReadError, match="anki21b"):
    Collection.from_zipfile(zfile, Version.LATEST)


def test_a_corrupt_collection_entry_is_refused(tmp_path):
  path = a_zip(tmp_path / "deck.apkg", ("collection.anki21b", b"not zstd"))

  with open_apkg(path) as zfile, pytest.raises(PackageReadError, match="Corrupt"):
    Collection.from_zipfile(zfile, Version.LATEST)


# -------------------------------- Collection.from_apkg --------------------------------


def test_the_collection_is_read_from_a_path(written_package):
  """Only that the path resolves to a readable collection, for the reason given for
  `Collection.from_zipfile` above."""
  assert isinstance(Collection.from_apkg(written_package), Collection)


def test_the_collection_of_a_zip_that_is_not_a_package_is_refused(not_a_package):
  with pytest.raises(PackageReadError, match="does not carry a `meta` entry"):
    Collection.from_apkg(not_a_package)


# ------------------------------- Collection.read_sqlite -------------------------------


@pytest.mark.parametrize("name", ["missing.anki2", "a-directory"])
def test_reading_a_missing_database_or_a_directory_is_refused(tmp_path, name: str):
  """`sqlite3.connect` creates a missing file, so without the check a typo left an
  empty database behind and failed for a missing `col` table instead."""
  (tmp_path / "a-directory").mkdir()

  with pytest.raises(Genanki2Error, match="is missing or not a file"):
    Collection.read_sqlite(tmp_path / name)

  assert [entry.name for entry in tmp_path.iterdir()] == ["a-directory"]


def test_a_database_under_a_tilde_is_read_from_the_home_folder(tmp_path, monkeypatch):
  home = tmp_path / "home"
  home.mkdir()
  with warnings.catch_warnings():
    warnings.simplefilter("ignore", Genanki2Warning)
    Collection(config=SCHEDULER_V2).write_sqlite(home / "collection.anki2")
  monkeypatch.setenv("HOME", str(home))
  monkeypatch.chdir(tmp_path)

  collection = Collection.read_sqlite("~/collection.anki2")

  assert collection.get_config_entry("schedVer") == SCHEDULER_V2[0]


# ---------------------------- Package.read_apkg overwrite -----------------------------


def test_an_existing_media_file_refuses_the_read_before_anything_is_written(
  written_package, version: Version, tmp_path
):
  """The file clashing is the last one, so a check per file would write the collection
  and the first file before refusing."""
  extract_dir = tmp_path / "extracted"
  extract_dir.mkdir()
  (extract_dir / "back.mp3").write_bytes(b"mine")

  with pytest.raises(Genanki2Error, match="already exists"):
    Package.read_apkg(written_package, extract_dir=extract_dir, overwrite=False)

  assert {p.name: p.read_bytes() for p in extract_dir.iterdir()} == {
    "back.mp3": b"mine"
  }


def test_an_existing_collection_file_refuses_the_read_before_anything_is_written(
  written_package, version: Version, tmp_path
):
  extract_dir = tmp_path / "extracted"
  extract_dir.mkdir()
  (extract_dir / version.collection_entry).write_bytes(b"mine")

  with pytest.raises(Genanki2Error, match="already exists"):
    Package.read_apkg(written_package, extract_dir=extract_dir, overwrite=False)

  assert {p.name: p.read_bytes() for p in extract_dir.iterdir()} == {
    version.collection_entry: b"mine"
  }
