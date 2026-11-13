import enum
import zipfile
from pathlib import Path
from typing import ClassVar

import pydantic as pyd
from google.protobuf.message import DecodeError as ProtobufDecodeError
from google.protobuf.message import Message
from typing_extensions import Self

import genanki2._proto as _proto  # noqa: PLR0402, import cycle via __init__
from genanki2.errors import Genanki2Error, PackageReadError
from genanki2.io_utils import open_apkg, resolve_path
from genanki2.models.base import ProtobufModel
from genanki2.utils import StrEnum


class NamedEntry(StrEnum):
  """A zip entry name the envelopes give a fixed meaning."""

  COLLECTION_ANKI2 = "collection.anki2"
  COLLECTION_ANKI21 = "collection.anki21"
  COLLECTION_ANKI21B = "collection.anki21b"
  MEDIA = "media"
  META = "meta"


class PackageMetadata(ProtobufModel):
  """The `meta` zip entry, saying how the rest of the package is encoded.

  A reader takes this entry first. It is stored uncompressed so that the reader knows
  how to process the rest. The version it declares decides the collection's filename,
  whether the collection and the media files are zstd-compressed, and whether the
  `media` map is a protobuf `MediaEntries` or a JSON object. A package that declares
  one version and encodes another does not import.

  A package with no `meta` entry is a legacy one and its version is worked out from
  which collection filename is present: `VERSION_LEGACY_2` for `collection.anki21`,
  `VERSION_LEGACY_1` for `collection.anki2` or `VERSION_UNKNOWN` if not present.

  `VERSION_UNKNOWN` is the protobuf zero, so a `meta` omitting the field reads back as
  unknown and Anki rejects the package as too new.

  Attributes:
    version: The envelope the package uses. Defaults to `VERSION_LATEST`, the one
      genanki2 writes. The default applies only to a model built in Python; reading a
      blob always takes the value the blob carries.
  """

  # ----------------------------------- Nested types -----------------------------------

  class Version(enum.IntEnum):
    """The package envelope: its collection entry and media map format."""

    UNKNOWN = 0
    LEGACY_1 = 1  # `meta` missing, collection.anki2 file
    LEGACY_2 = 2  # `meta` missing, collection.anki21 file
    LATEST = 3  # MediaEntry media map, collection.21b file, zstd-compression

    # --------------------------------- Derived values ---------------------------------

    @property
    def collection_entry(self) -> NamedEntry:
      """The collection's zip entry corresponding to the version.

      A package declaring one version and using another version's filename does not
      import.

      Raises:
        Genanki2Error: the version is unknown, no name can be returned.
      """
      if self is PackageMetadata.Version.LATEST:
        return NamedEntry.COLLECTION_ANKI21B
      elif self is PackageMetadata.Version.LEGACY_2:
        return NamedEntry.COLLECTION_ANKI21
      elif self is PackageMetadata.Version.LEGACY_1:
        return NamedEntry.COLLECTION_ANKI2
      raise Genanki2Error("Unknown package version: cannot return a collection entry.")

    @property
    def compressed_entries(self) -> bool:
      """Whether this envelope zstd-compresses the collection and the media files.

      `meta` is always stored plain for a reader to inspect first.
      """
      return self is PackageMetadata.Version.LATEST

    # ------------------------------------- Checks -------------------------------------

    def raise_if_unknown(
      self, path: str | Path | None = None
    ) -> "PackageMetadata.Version":
      """Raise if version is `VERSION_UNKNOWN`, since nothing about the package is known.

      A zip reads back as unknown when:
      - its `meta` entry omits the `version` field.
      - it doesn't carry a `meta` entry.
      - it doesn't carry a known collection entry.

      Args:
        path: The package the version was read from, named in the error. Default: `None`.

      Raises:
        PackageReadError: the version is unknown.
      """
      if self is PackageMetadata.Version.UNKNOWN:
        path_str: str = f' ("{resolve_path(path)}")' if path is not None else ""
        raise PackageReadError(
          f"Cannot read the package{path_str}: it does not carry a `meta` entry "
          "saying how it is encoded, or a collection entry (`collection.anki2`, "
          "`collection.anki21`) to be read without `meta`. Either the file is not an "
          "Anki package or its `meta` entry omits the version field."
        )
      return self

  # ------------------------------------ Attributes ------------------------------------

  MessageType: ClassVar[type[Message]] = _proto.PackageMetadata

  version: Version = Version.LATEST

  # ---------------------------------- Serialization -----------------------------------

  @classmethod
  def zipfile_version(cls, zfile: zipfile.ZipFile) -> Version:
    """Return the envelope `zfile` is encoded in, reading its `meta` entry first.

    A `meta` entry is checked first and the version inside is returned. Otherwise:
    - the collection filename is checked: `VERSION_LEGACY_2` for `collection.anki21`,
      `VERSION_LEGACY_1` for `collection.anki2`.
    - `VERSION_UNKNOWN` when the `meta` entry has no version field, or the zip
      carries no collection filename.

    Raises:
      PackageReadError: bad zip file.
      PackageReadError: protobuf failed to parse the entry bytes.
      PackageReadError: `meta` declares a version genanki2 does not know.
    """
    contents: set[str] = set(zfile.namelist())
    if NamedEntry.META in contents:
      try:
        return cls.from_bytes(zfile.read(NamedEntry.META)).version
      except (zipfile.BadZipFile, ProtobufDecodeError) as error:
        msg = f"Corrupt package entries: cannot read the `meta` entry. Error: {error}"
        raise PackageReadError(msg) from error
      except pyd.ValidationError as error:
        raise PackageReadError(
          "Cannot read the package: its `meta` entry declares an envelope genanki2 "
          "does not know. The package was written by a newer Anki than this version "
          "of genanki2 supports."
        ) from error
    elif NamedEntry.COLLECTION_ANKI21 in contents:
      return PackageMetadata.Version.LEGACY_2
    elif NamedEntry.COLLECTION_ANKI2 in contents:
      return PackageMetadata.Version.LEGACY_1
    return PackageMetadata.Version.UNKNOWN

  @classmethod
  def from_apkg(cls, path: str | Path) -> Self:
    """Return the metadata of the package at `path`, whichever envelope it uses.

    A legacy package carries no `meta` entry, so the version comes from its collection
    filename. See `PackageMetadata.zipfile_version()` for more details.

    Raises:
      PackageReadError: the file is not a zip, or two of its entries share a name.
      PackageReadError: the `meta` entry cannot be read, or declares a version
        genanki2 does not know.
      OSError: the file cannot be opened.
    """
    with open_apkg(path) as apkg_file:
      return cls(version=PackageMetadata.zipfile_version(apkg_file))
