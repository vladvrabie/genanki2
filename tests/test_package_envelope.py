"""Which envelope does a package declare, and what decides it?

    uv run --with pytest pytest tests/test_package_envelope.py

Everything else about reading a package follows from this answer: it names the zip
entry holding the collection, says whether entries are zstd-compressed, and says
whether the media map is JSON or a protobuf. Reading it wrong produces a package that
is valid and wrong, since a real export carries a schema 11 stub under
`collection.anki2` beside the modern collection, and a reader that picks the entry it
recognises by name reads the stub.

The `meta` entry decides alone when it is there, which is why these cases pit it
against a collection filename that says something else.
"""

import zipfile

import pytest

from genanki2 import _proto
from genanki2.errors import PackageReadError
from genanki2.models.package_metadata import PackageMetadata

Version = PackageMetadata.Version


def a_package(tmp_path, **entries: bytes) -> zipfile.ZipFile:
  """A zip holding exactly the entries named, with no collection inside any of them."""
  path = tmp_path / "deck.apkg"
  with zipfile.ZipFile(path, "w") as apkg:
    for name, data in entries.items():
      apkg.writestr(name, data)
  return zipfile.ZipFile(path)


def meta_declaring(version: int) -> bytes:
  """A `meta` blob declaring `version`, including one genanki2 does not know."""
  message = _proto.PackageMetadata()
  message.version = version
  return message.SerializeToString()


def test_a_meta_entry_names_the_envelope(tmp_path):
  apkg = a_package(tmp_path, meta=meta_declaring(Version.LATEST))

  assert PackageMetadata.zipfile_version(apkg) == Version.LATEST


def test_a_meta_entry_outranks_the_collection_filename(tmp_path):
  """A real export carries `collection.anki2` as a stub for old clients, so the
  filename present says nothing about which collection to read."""
  apkg = a_package(
    tmp_path,
    meta=meta_declaring(Version.LATEST),
    **{"collection.anki2": b"", "collection.anki21b": b""},
  )

  assert PackageMetadata.zipfile_version(apkg) == Version.LATEST


def test_a_meta_entry_omitting_the_version_is_unknown(tmp_path):
  """The field is the protobuf zero, so an empty blob declares nothing rather than
  falling back to the version genanki2 writes."""
  apkg = a_package(tmp_path, meta=b"")

  assert PackageMetadata.zipfile_version(apkg) == Version.UNKNOWN


def test_a_version_genanki2_does_not_know_is_refused(tmp_path):
  """Anki reads it as `Unknown` and rejects the package as too new. genanki2 cannot
  hold the value at all, so the refusal happens as the blob decodes."""
  apkg = a_package(tmp_path, meta=meta_declaring(7))

  with pytest.raises(PackageReadError, match="newer Anki"):
    _ = PackageMetadata.zipfile_version(apkg)


@pytest.mark.parametrize(
  ("collection_entry", "version"),
  [("collection.anki21", Version.LEGACY_2), ("collection.anki2", Version.LEGACY_1)],
)
def test_without_meta_the_collection_filename_decides(
  tmp_path, collection_entry: str, version: Version
):
  apkg = a_package(tmp_path, **{collection_entry: b""})

  assert PackageMetadata.zipfile_version(apkg) == version


def test_without_meta_the_newer_collection_filename_wins(tmp_path):
  apkg = a_package(tmp_path, **{"collection.anki2": b"", "collection.anki21": b""})

  assert PackageMetadata.zipfile_version(apkg) == Version.LEGACY_2


def test_a_zip_naming_no_collection_at_all_is_unknown(tmp_path):
  """The other way a package says nothing, and the one that catches a file that is
  not a package."""
  apkg = a_package(tmp_path, media=b"{}")

  assert PackageMetadata.zipfile_version(apkg) == Version.UNKNOWN
