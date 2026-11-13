"""Can genanki2 tell a caller their output path is wrong before it does the work?

    uv run --with pytest pytest tests/test_writing_paths.py

Writing a package builds a collection in a temporary directory and streams a zip, so a
path that was never going to work should be refused before any of that, with a message
naming the path. The two mistakes worth catching are naming a file where a directory
belongs and naming somewhere the process cannot write.

The check is advisory and cannot be otherwise: it answers for one instant and for the
real user id, so it sees no ACL, no quota, no full disk, and no path another process
replaces a moment later. `OSError` from the write itself remains the thing that is
actually true, so these tests cover both what can be refused early and what the
destination holds when the write raises anyway: the package is built under a temporary
name and moved into place, so a failure leaves whatever was there untouched.
"""

import os
import sys
import zipfile
from pathlib import Path

import pytest

from genanki2.errors import Genanki2Error
from genanki2.io_utils import (
  atomic_replace,
  prepare_writing_dir,
  prepare_writing_file,
  prepare_writing_files,
  resolve_path,
)
from genanki2.models.collection import Collection
from genanki2.models.config import ConfigEntry
from genanki2.models.package import Package
from genanki2.models.package_metadata import PackageMetadata

if sys.version_info >= (3, 14):
  from compression import zstd  # pyright: ignore[reportUnreachable]
else:
  import zstandard as zstd

Version = PackageMetadata.Version

# Written collections carry it so that the writer's v1 scheduler warning stays out of
# the way of what these tests are about.
SCHEDULER_V2 = [ConfigEntry(key="schedVer", value=b"2")]

WRITERS = {
  Version.LEGACY_1: "_write_v18_legacy_1",
  Version.LEGACY_2: "_write_v18_legacy_2",
  Version.LATEST: "_write_v18_latest",
}


def a_package() -> Package:
  """The smallest package that writes without a word about its contents."""
  return Package(collection=Collection(config=SCHEDULER_V2))


def a_package_that_fails_halfway(directory: Path) -> Package:
  """A package that passes every check and then raises while the zip is being built."""
  media = directory / "media" / "audio.mp3"
  media.parent.mkdir(parents=True, exist_ok=True)
  media.write_bytes(b"ID3")
  package = Package(collection=Collection(config=SCHEDULER_V2), media=[str(media)])
  media.unlink()
  return package


@pytest.fixture
def unwritable_dir(tmp_path):
  """A directory the process may enter but not write into."""
  path = tmp_path / "readonly"
  path.mkdir()
  os.chmod(path, 0o500)
  yield path
  os.chmod(path, 0o700)


# ------------------------------- Is this a place to write? ------------------------------


NOT_A_WRITABLE_DIRECTORY = "is not a directory that can be written into"


def test_an_existing_directory_is_returned_as_it_is(tmp_path):
  assert prepare_writing_dir(tmp_path) == tmp_path.resolve()


def test_a_directory_that_does_not_exist_yet_is_created(tmp_path):
  """Extraction creates the directory it was given, so the question is whether it can
  be created, not whether it is already there."""
  path = tmp_path / "new" / "deeper"

  assert prepare_writing_dir(path) == path.resolve()
  assert path.is_dir()


@pytest.mark.parametrize("inside", [False, True])
def test_a_file_is_not_a_directory(tmp_path, inside: bool):
  path = tmp_path / "deck.apkg"
  path.write_bytes(b"")

  with pytest.raises(Genanki2Error, match=NOT_A_WRITABLE_DIRECTORY):
    prepare_writing_dir(path / "inside" if inside else path)


@pytest.mark.parametrize("name", [None, "new"])
def test_a_directory_that_cannot_be_written_into_is_refused(
  unwritable_dir, name: str | None
):
  """Creating an entry inside a directory needs write and traverse permission both."""
  path = unwritable_dir / name if name else unwritable_dir

  with pytest.raises(Genanki2Error, match=NOT_A_WRITABLE_DIRECTORY):
    prepare_writing_dir(path)

  assert not list(unwritable_dir.iterdir())


def test_an_empty_path_is_the_working_directory(tmp_path, monkeypatch):
  monkeypatch.chdir(tmp_path)

  assert prepare_writing_dir("") == tmp_path.resolve()


# ------------------------- A directory where the file belongs -------------------------


@pytest.mark.parametrize("overwrite", [True, False])
def test_a_directory_is_refused_as_the_file_to_write(tmp_path, overwrite: bool):
  """The directory check comes before the `overwrite` one, so the message names the
  real mistake rather than saying the file already exists."""
  path = tmp_path / "deck.apkg"
  path.mkdir()

  with pytest.raises(Genanki2Error, match="is a directory"):
    prepare_writing_file(path, overwrite)


@pytest.mark.parametrize("overwrite", [True, False])
def test_a_directory_anywhere_in_a_batch_refuses_the_batch(tmp_path, overwrite: bool):
  """Extraction checks every file before writing any, so a media file named like an
  existing folder is caught before the files ahead of it are written."""
  (tmp_path / "b.mp3").mkdir()

  with pytest.raises(Genanki2Error, match="is a directory"):
    prepare_writing_files([tmp_path / "a.mp3", tmp_path / "b.mp3"], overwrite)


def test_an_empty_path_is_the_working_directory_and_no_file(tmp_path, monkeypatch):
  monkeypatch.chdir(tmp_path)

  with pytest.raises(Genanki2Error, match="is a directory"):
    prepare_writing_file("")


def test_writing_a_package_over_a_directory_is_refused_before_the_zip_is_built(
  tmp_path,
):
  """Without the check, the whole zip is built beside the directory and only the final
  rename fails, with an `IsADirectoryError` that names the temporary file."""
  path = tmp_path / "deck.apkg"
  path.mkdir()

  with pytest.raises(Genanki2Error, match="is a directory"):
    _ = a_package().write_apkg(path)

  assert [entry.name for entry in tmp_path.iterdir()] == ["deck.apkg"]
  assert path.is_dir()


# ------------------------------- Paths starting with ~ --------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
  """A home folder of its own, and a separate working directory to start from."""
  home = tmp_path / "home"
  home.mkdir()
  (tmp_path / "cwd").mkdir()
  monkeypatch.setenv("HOME", str(home))
  monkeypatch.chdir(tmp_path / "cwd")
  return home.resolve()


@pytest.mark.parametrize("as_path", [False, True])
def test_a_tilde_expands_to_the_home_folder(home: Path, as_path: bool):
  assert resolve_path(Path("~/x") if as_path else "~/x") == home / "x"


def test_a_relative_path_is_made_absolute_against_the_working_directory(home: Path):
  assert resolve_path("x") == Path.cwd() / "x"


def test_a_file_under_a_tilde_is_prepared_in_the_home_folder(home: Path):
  """Without the expansion, the directory is created as `./~/decks` in the working
  directory, and nothing reports it."""
  assert prepare_writing_file("~/decks/deck.apkg") == home / "decks" / "deck.apkg"
  assert (home / "decks").is_dir()
  assert not (Path.cwd() / "~").exists()


def test_a_package_written_under_a_tilde_lands_in_the_home_folder(home: Path):
  a_package().write_apkg("~/deck.apkg")

  assert zipfile.is_zipfile(home / "deck.apkg")
  assert not (Path.cwd() / "~").exists()


def test_a_package_read_into_a_tilde_directory_is_extracted_in_the_home_folder(
  home: Path,
):
  a_package().write_apkg(home / "deck.apkg")

  package = Package.read_apkg(home / "deck.apkg", extract_dir="~/extracted")

  assert (home / "extracted" / Version.LATEST.collection_entry).is_file()
  assert not (Path.cwd() / "~").exists()
  assert package.media == []


# ---------------------------------- Writing a package -----------------------------------


def test_a_package_is_written_under_a_directory_that_did_not_exist(tmp_path):
  """Writing into `build/decks/` should not ask the caller to create it first."""
  path = tmp_path / "build" / "decks" / "deck.apkg"

  a_package()._write_v18_legacy_1(path)

  assert zipfile.is_zipfile(path)


def test_writing_into_an_unwritable_directory_is_refused_before_any_work(
  unwritable_dir,
):
  with pytest.raises(Genanki2Error, match=NOT_A_WRITABLE_DIRECTORY):
    _ = a_package().write_apkg(unwritable_dir / "deck.apkg")

  assert not list(unwritable_dir.iterdir())


def test_rebuilding_over_yesterdays_package_is_the_default(tmp_path):
  """Reading last release's package, changing it and writing it back to the same path
  is the flow the library is built around."""
  path = tmp_path / "deck.apkg"
  a_package().write_apkg(path)

  a_package().write_apkg(path)

  assert zipfile.is_zipfile(path)


def test_a_caller_can_refuse_to_replace_an_existing_package(tmp_path):
  path = tmp_path / "deck.apkg"
  a_package().write_apkg(path)
  written = path.read_bytes()

  with pytest.raises(Genanki2Error, match="already exists"):
    _ = a_package().write_apkg(path, overwrite=False)

  assert path.read_bytes() == written


# --------------------------- Writing a collection database ----------------------------


def a_collection_that_fails_a_check() -> Collection:
  """A collection holding a repeated config key, which collecting does not mend."""
  return Collection(
    config=[*SCHEDULER_V2, ConfigEntry(key="rollover"), ConfigEntry(key="rollover")]
  )


def test_a_database_is_written_under_a_tilde_in_a_folder_that_did_not_exist(
  home: Path,
):
  Collection(config=SCHEDULER_V2).write_sqlite("~/build/collection.anki2")

  assert (home / "build" / "collection.anki2").is_file()
  assert not (Path.cwd() / "~").exists()


def test_rewriting_a_database_replaces_it(tmp_path):
  """Writing the schema into the existing file failed with `table cards already
  exists`."""
  path = tmp_path / "collection.anki2"
  Collection(config=SCHEDULER_V2).write_sqlite(path)

  Collection(config=SCHEDULER_V2).write_sqlite(path)

  assert Collection.read_sqlite(path).config == SCHEDULER_V2
  assert [entry.name for entry in tmp_path.iterdir()] == ["collection.anki2"]


def test_a_caller_can_refuse_to_replace_an_existing_database(tmp_path):
  path = tmp_path / "collection.anki2"
  Collection(config=SCHEDULER_V2).write_sqlite(path)
  written = path.read_bytes()

  with pytest.raises(Genanki2Error, match="already exists"):
    _ = Collection(config=SCHEDULER_V2).write_sqlite(path, overwrite=False)

  assert path.read_bytes() == written


def test_a_database_is_refused_over_a_directory(tmp_path):
  path = tmp_path / "collection.anki2"
  path.mkdir()

  with pytest.raises(Genanki2Error, match="is a directory"):
    _ = Collection(config=SCHEDULER_V2).write_sqlite(path)


def test_a_failed_check_leaves_the_earlier_database_in_place(tmp_path):
  path = tmp_path / "collection.anki2"
  Collection(config=SCHEDULER_V2).write_sqlite(path)
  written = path.read_bytes()

  with pytest.raises(ValueError):
    _ = a_collection_that_fails_a_check().write_sqlite(path)

  assert path.read_bytes() == written
  assert [entry.name for entry in tmp_path.iterdir()] == ["collection.anki2"]


def test_a_failed_check_creates_no_folder(tmp_path):
  """The checks run before the destination is prepared, so a refused collection
  leaves the disk as it found it."""
  with pytest.raises(ValueError):
    _ = a_collection_that_fails_a_check().write_sqlite(tmp_path / "build" / "c.anki2")

  assert list(tmp_path.iterdir()) == []


# ---------------------------- Is the destination replaced? ----------------------------


@pytest.mark.parametrize("version", list(WRITERS))
def test_a_failed_write_leaves_the_earlier_package_in_place(tmp_path, version):
  """A rebuild writes over last release's package, so a failure partway through has to
  leave the caller with the old package rather than with neither."""
  path = tmp_path / "deck.apkg"
  getattr(a_package(), WRITERS[version])(path)
  written = path.read_bytes()

  with pytest.raises(OSError):
    _ = getattr(a_package_that_fails_halfway(tmp_path), WRITERS[version])(path)

  assert path.read_bytes() == written


@pytest.mark.parametrize("version", list(WRITERS))
def test_a_failed_write_leaves_no_half_written_package_behind(tmp_path, version):
  """Nothing appears under the destination until there is a package to put there, so a
  failed write is not a file the caller has to know to delete."""
  path = tmp_path / "deck.apkg"

  with pytest.raises(OSError):
    _ = getattr(a_package_that_fails_halfway(tmp_path), WRITERS[version])(path)

  assert not path.exists()


@pytest.mark.parametrize("version", list(WRITERS))
def test_the_new_package_is_built_beside_the_destination(
  tmp_path, monkeypatch, version
):
  """A rename is what puts the package in place and a rename cannot cross filesystems,
  so a zip built in the system temporary directory could not be moved onto a
  destination anywhere else: `OSError: [Errno 18] Invalid cross-device link`. Building
  it in the destination's own directory is also what makes the replacement atomic."""
  built: list[Path] = []
  init = zipfile.ZipFile.__init__

  def recording_init(self: zipfile.ZipFile, *args, **kwargs) -> None:
    init(self, *args, **kwargs)
    if self.mode == "w" and self.filename is not None:
      built.append(Path(self.filename))

  monkeypatch.setattr(zipfile.ZipFile, "__init__", recording_init)
  path = tmp_path / "deck.apkg"

  getattr(a_package(), WRITERS[version])(path)

  assert [zip_path.parent for zip_path in built] == [path.parent]


@pytest.mark.parametrize("version", list(WRITERS))
def test_a_destination_named_after_the_collection_entry_still_holds_one(
  tmp_path, version
):
  """`deck.apkg` is a convention, not a rule. A writer builds the collection under the
  name the envelope stores it as, so a destination carrying that same name is a file
  the collection must not be built over."""
  path = tmp_path / str(version.collection_entry)

  getattr(a_package(), WRITERS[version])(path)

  with zipfile.ZipFile(path) as zfile:
    collection = zfile.read(version.collection_entry)
  if version.compressed_entries:
    collection = zstd.decompress(collection)
  assert collection.startswith(b"SQLite format 3")


# --------------------------- What replaces the destination? ---------------------------


def failing_replacement(path: Path) -> None:
  """A replacement that raises once part of the new file is written."""
  with atomic_replace(path) as temp_path:
    temp_path.write_bytes(b"half a deck")
    raise ValueError("the package could not be built")


def refused_replacement(path: Path) -> None:
  """A replacement of a file the caller said not to replace."""
  with atomic_replace(path, overwrite=False) as temp_path:
    temp_path.write_bytes(b"a deck")


def test_what_the_block_writes_ends_up_at_the_destination(tmp_path):
  path = tmp_path / "deck.apkg"

  with atomic_replace(path) as temp_path:
    temp_path.write_bytes(b"a deck")

  assert path.read_bytes() == b"a deck"


def test_the_destination_holds_the_old_file_until_the_block_ends(tmp_path):
  """Anki reading the package while it is being rebuilt finds last release's, rather
  than as much of this one as the writer has reached."""
  path = tmp_path / "deck.apkg"
  path.write_bytes(b"last release")

  with atomic_replace(path) as temp_path:
    temp_path.write_bytes(b"this release")
    assert path.read_bytes() == b"last release"

  assert path.read_bytes() == b"this release"


def test_the_file_to_write_is_created_beside_the_destination(tmp_path):
  """A rename cannot cross filesystems and the system temporary directory is usually
  on another one, so the file being written sits next to the one it replaces. The
  destination's directory is created for it if it is not there yet."""
  path = tmp_path / "build" / "deck.apkg"

  with atomic_replace(path) as temp_path:
    assert temp_path.parent == path.parent
    assert temp_path.read_bytes() == b""


def test_a_block_that_raises_leaves_the_destination_as_it_was(tmp_path):
  """The exception is not an `OSError`: a write fails on what it is given as often as
  on the filesystem, and either way the old package is what the caller still has."""
  path = tmp_path / "deck.apkg"
  path.write_bytes(b"last release")

  with pytest.raises(ValueError):
    failing_replacement(path)

  assert path.read_bytes() == b"last release"
  assert [entry.name for entry in tmp_path.iterdir()] == ["deck.apkg"]


def test_a_block_that_raises_leaves_nothing_behind(tmp_path):
  """With no file there to begin with, a failure leaves the directory as empty as it
  found it, rather than a half-written file the caller has to know to delete."""
  path = tmp_path / "deck.apkg"

  with pytest.raises(ValueError):
    failing_replacement(path)

  assert list(tmp_path.iterdir()) == []


def test_the_destination_is_readable_by_more_than_its_owner(tmp_path):
  """A temporary file is created private to the user who created it, which is not what
  a deck being handed around should end up as."""
  path = tmp_path / "deck.apkg"

  with atomic_replace(path) as temp_path:
    temp_path.write_bytes(b"a deck")

  assert path.stat().st_mode & 0o777 == 0o644


def test_a_caller_can_ask_for_other_permissions(tmp_path):
  path = tmp_path / "deck.apkg"

  with atomic_replace(path, mode=0o600) as temp_path:
    temp_path.write_bytes(b"a deck")

  assert path.stat().st_mode & 0o777 == 0o600


def test_refusing_to_replace_a_file_writes_nothing_beside_it(tmp_path):
  """The refusal comes from `prepare_writing_file`, before there is a file to write
  into, so there is nothing to remove afterwards."""
  path = tmp_path / "deck.apkg"
  path.write_bytes(b"last release")

  with pytest.raises(Genanki2Error, match="already exists"):
    refused_replacement(path)

  assert path.read_bytes() == b"last release"
  assert [entry.name for entry in tmp_path.iterdir()] == ["deck.apkg"]
