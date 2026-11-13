import hashlib
import json
import sys
import warnings
import zipfile
from collections.abc import Iterator, Sequence
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import Annotated, Any, ClassVar, NamedTuple, TypeAlias, cast

import pydantic as pyd
from google.protobuf.message import DecodeError as ProtobufDecodeError
from google.protobuf.message import Message
from typing_extensions import Self, override

import genanki2._proto as _proto  # noqa: PLR0402, import cycle via __init__
from genanki2.errors import Genanki2Error, Genanki2Warning, PackageReadError
from genanki2.io_utils import (
  open_apkg,
  prepare_writing_files,
  resolve_path,
  zstd_decompress,
)
from genanki2.models.annotations import UInt32
from genanki2.models.base import ProtobufModel
from genanki2.models.package_metadata import NamedEntry, PackageMetadata
from genanki2.models.serialization_context import SerializationContext
from genanki2.text import free_normalized_media_name, is_media_name_safe
from genanki2.utils import duplicates, is_uint32

if sys.version_info >= (3, 14):
  from compression import zstd  # pyright: ignore[reportUnreachable]
else:
  import zstandard as zstd


class ExtractedEntry(NamedTuple):
  """One media file of a package: the zip entry holding it and the names it goes by.

  The two names differ when the name in the package had to be repaired or suffixed to
  be stored on disk. The references in the collection still use the name in the
  package.

  Attributes:
    zip_index: The zip entry holding the file's bytes.
    package_name: The name the media map gives the file, which references use.
    disk_name: The name the file is stored under on disk.
  """

  zip_index: str
  package_name: str
  disk_name: str


class MediaEntries(ProtobufModel):
  """The `media` zip entry, mapping each numbered zip entry to a file name.

  The filename appears only here and it is what note fields reference. Nothing else
  ties the two together.

  Both envelopes use this model during reading. The current one stores it as a protobuf
  and numbers a file by its list position. The legacy one carries the mapping as a JSON
  object holding only the names.

  Attributes:
    entries: One entry per media file, in zip entry order for the latest envelope.
      Defaults to an empty list.
  """

  # ----------------------------------- Nested types -----------------------------------

  class MediaEntry(ProtobufModel):
    """One media file, described by its name, size, checksum and legacy index.

    The file name is stored in the package as given. If not normalized, it fails the
    whole import on the latest envelope and gets repaired in the legacy one.

    Using the latest envelope, the size and the digest save work during an import. Anki
    compares the incoming file checksum to the target file checksum and skips the
    incoming file when they agree. The checksum is never recomputed.

    Attributes:
      name: The file name the note fields reference.
      size: The file's length in bytes, before compression. `0` on a legacy entry.
      sha1: The raw 20-byte digest of the file's bytes. `b""` on a legacy entry.
      legacy_zip_filename: The zip entry the file came from, set only when reading a
        legacy media map, since those may number their entries with gaps. A writer never
        sets it. Default: `None`.
    """

    MessageType: ClassVar[type[Message]] = _proto.MediaEntry

    name: str
    size: Annotated[int, UInt32]
    sha1: bytes
    legacy_zip_filename: Annotated[int | None, UInt32] = None

    # --------------------------------- Serialization ----------------------------------

    @classmethod
    def from_file_bytes(cls, name: str, file_bytes: bytes) -> Self:
      """Return the entry describing a media file, measured and hashed from its bytes.

      `name` is stored as given (it is not normalized). It is the name that note
      fields reference, not the path (see `Package.check_media_paths()`).
      """
      return cls(
        name=name, size=len(file_bytes), sha1=hashlib.sha1(file_bytes).digest()
      )

    @classmethod
    def from_path(cls, path: str | Path) -> Self:
      """Return the entry describing the media file at `path`.

      The file is read once, to measure and hash it. Only the final component of `path`
      is stored, since that is the name the note fields reference.

      Raises:
        OSError: the file cannot be read.
      """
      return cls.from_file_bytes(Path(path).name, resolve_path(path).read_bytes())

  # ------------------------------------ Attributes ------------------------------------

  MessageType: ClassVar[type[Message]] = _proto.MediaEntries

  entries: list[MediaEntry] = pyd.Field(default_factory=list)

  # ------------------------------- Checks & Validators --------------------------------

  @pyd.model_validator(mode="after")
  def validate_homogeneous_entries(self) -> Self:
    """Run the homogeneity check on the entries."""
    return self.check_homogeneous_entries()

  def check_zip_carries_entries(self, zip_namelist: AbstractSet[str]) -> Self:
    """Reject a map naming zip entries that the package does not carry.

    Args:
      zip_namelist: The package's zip entry names.

    Raises:
      PackageReadError: the map names a zip entry the package does not carry.
    """
    lines: list[str] = [
      f"  {{{zip_index!r}: {entry.name!r}}}"
      for zip_index, entry in self.enumerate_entries()
      if zip_index not in zip_namelist
    ]
    if lines:
      listing: str = "\n".join(lines)
      raise PackageReadError(
        "Missing package entries: the package does not carry some entries in the "
        f"media map. Missing entries:\n{listing}"
      )
    return self

  # ---------------------------------- Derived values ----------------------------------

  def are_legacy(self) -> bool:
    """Return whether these entries came from a legacy media map.

    Decided by checking if any entry carries `MediaEntry.legacy_zip_filename`, because
    `MediaEntries.check_homogeneous_entries()` ensures the list does not mix
    legacy and latest envelope entries.

    This decides how a file is numbered: from list position for the latest envelope or
    from `MediaEntry.legacy_zip_filename` for the legacy envelopes.
    """
    return any(entry.legacy_zip_filename is not None for entry in self.entries)

  def enumerate_entries(self) -> Iterator[tuple[str, MediaEntry]]:
    """Yield each entry with the zip entry holding its file, in list order.

    The zip entry is the list position in the latest envelope and
    `MediaEntry.legacy_zip_filename` in the legacy ones.
    """
    legacy: bool = self.are_legacy()
    for position, entry in enumerate(self.entries):
      idx: str = str(entry.legacy_zip_filename if legacy else position)
      yield idx, entry

  def extraction_entries(
    self, taken: AbstractSet[str] | None = None
  ) -> tuple[ExtractedEntry, ...]:
    """Return the name each entry is stored under on disk, in zip entry order.

    A name is repaired by `text.normalize_media_name()` before it is used, since a
    package can carry a name no filesystem accepts. A name already taken by another
    entry is suffixed using `text.free_normalized_media_name()`, which keeps two
    entries repairing to the same name from overwriting each other's file.

    An entry whose name needs no repair keeps that name, even when an earlier entry's
    name repairs to it, so no name on disk is the package name of another entry.

    A renamed file needs its references updated, and
    `MediaEntries.warn_renames_in_extraction_entries()` lists the renames to apply.

    Args:
      taken: Names already spoken for. The function case folds them (for
        case-insensitive OSes). Pass the collection's entry name when extracting
        to avoid overwriting it by a media file with the same name. Default: `None`.

    Raises:
      PackageReadError: a name repairs to nothing, so it cannot be stored.
    """
    extraction_entries: list[ExtractedEntry] = []
    taken = {name.casefold() for name in taken} if taken is not None else set()

    def add_to_entries(zip_index: str, name: str, free_name: str | None = None) -> None:
      """Store the entry under the first free normalized form of `name`."""
      if free_name is None:
        free_name = free_normalized_media_name(name, taken)
      if not free_name:
        raise PackageReadError(
          "Invalid normalized name: media map contains entry "
          f"{{{zip_index!r}: {name!r}}} whose name normalizes to nothing, making "
          "writing the file to disk impossible."
        )
      extraction_entries.append(
        ExtractedEntry(zip_index=zip_index, package_name=name, disk_name=free_name)
      )
      taken.add(free_name.casefold())

    deferred: list[tuple[str, MediaEntry]] = []
    for zip_index, entry in self.enumerate_entries():
      if free_normalized_media_name(entry.name, taken) == entry.name:
        add_to_entries(zip_index, entry.name, entry.name)
      else:
        deferred.append((zip_index, entry))

    for zip_index, entry in deferred:
      add_to_entries(zip_index, entry.name)

    return tuple(sorted(extraction_entries, key=lambda item: int(item.zip_index)))

  @staticmethod
  def extraction_renames(
    extraction_entries: Sequence[ExtractedEntry],
  ) -> tuple[ExtractedEntry, ...]:
    """Return the entries stored under a new name, whose references need renaming.

    A name given to more than one entry is left out, since its second copy is suffixed
    and renaming it would move every reference to that name.
    """
    duplicate_names: set[str] = set(
      duplicates(item.package_name for item in extraction_entries)
    )
    return tuple(
      item
      for item in extraction_entries
      if item.package_name not in duplicate_names
      and item.package_name != item.disk_name
    )

  # ----------------------------- Pre-serialization steps ------------------------------

  def check_homogeneous_entries(self) -> Self:
    """Reject a list mixing entries from a legacy media map with positional ones.

    The two number their files differently, so a mixed list has no single rule for
    which zip entry holds a file.

    Raises:
      Genanki2Error: only some of the entries carry a legacy zip filename.
    """
    all_latest: bool = all(entry.legacy_zip_filename is None for entry in self.entries)
    all_legacy: bool = all(
      entry.legacy_zip_filename is not None for entry in self.entries
    )
    if not (all_latest or all_legacy):
      raise Genanki2Error(
        "Check failed: some entries carry a legacy zip filename and some do not. They "
        "cannot mix because it makes it ambiguous in which kind of envelope they "
        "belong. A legacy media map numbers its own entries, the current envelope "
        "numbers a file by its position."
      )
    return self

  def check_checksum_lengths(self) -> Self:
    """Reject an entry checksum of invalid length, depending on the envelope.

    Anki reads a latest-envelope digest into a fixed 20 byte array, refusing the whole
    package for any other length. A legacy map holds names only, so its entries must
    carry no digest at all.

    Raises:
      Genanki2Error: an entry's checksum is not the length its envelope expects.
    """
    expected_len: int = 0 if self.are_legacy() else 20
    bad_entries: list[MediaEntry] = [
      entry for entry in self.entries if len(entry.sha1) != expected_len
    ]
    if bad_entries:
      listing: str = ", ".join(
        f'"{entry.name}" ({len(entry.sha1)})' for entry in bad_entries
      )
      raise Genanki2Error(
        "Check failed: not all checksums have the expected length. In the legacy "
        "envelope, they must be empty, while in the latest one 20 bytes. Names and "
        f"checksum lengths of entries: {listing}"
      )
    return self

  @override
  def check(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Reject entries that cannot be written.

    Args:
      recursive: Whether to check every model reachable from this one too, each once.
        If false, it only runs this model's own checks. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.check_homogeneous_entries()
    self.check_checksum_lengths()
    return super().check(recursive=recursive, context=context)

  def warn_duplicate_names(self) -> Self:
    """Warn when two entries share a name, since a reference cannot tell them apart.

    Both files are kept on extraction, the second under a suffixed name, but every
    reference resolves to the first.
    """
    if duplicate_names := duplicates(entry.name for entry in self.entries):
      warnings.warn(
        "MediaEntries: media map contains duplicate filenames. This makes matching "
        "references ambiguous. Consider renaming them, then repairing references. "
        f"Duplicates: {duplicate_names}",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  @staticmethod
  def warn_renames_in_extraction_entries(
    extraction_entries: Sequence[ExtractedEntry],
  ) -> None:
    """Warn about the files stored under a new name, listing the renames to apply.

    The renames are the ones `MediaEntries.extraction_renames()` returns.
    """
    renames: list[str] = [
      f"  {item.package_name!r}: {item.disk_name!r},"
      for item in MediaEntries.extraction_renames(extraction_entries)
    ]
    if renames:
      listing: str = "\n".join(renames)
      warnings.warn(
        "MediaEntries: media map contains filenames that Anki would not store. They "
        "can be repaired using the map below. Repairing also requires updating the "
        "references in notes, in notetype CSSes and in templates. Pass this mapping to "
        f"`genanki2.Package.replace_media_references()`:\n{{\n{listing}\n}}",
        Genanki2Warning,
        stacklevel=2,
      )

  # ---------------------------------- Serialization -----------------------------------

  @classmethod
  def from_legacy_bytes(cls, data: bytes) -> Self:
    """Read the legacy envelope's `media` entry.

    The legacy map holds `{"<zip entry>": "<file name>"}`, where the key is a UInt32
    string. A file name is validated: it must not redirect in any way.

    List position is not zip entry because the legacy map may number its entries with
    gaps. Entries come back ordered by the zip entry.

    File size and checksum are set to `0`.

    Args:
      data: The `media` entry's bytes.

    Raises:
      PackageReadError: the entry is not a JSON object mapping decimal numbers to file
        names.
    """
    try:
      media_map: dict[str, str] | Any = json.loads(data)
    except json.JSONDecodeError as error:
      raise PackageReadError(
        "Cannot read the package: its `media` entry does not parse as JSON. A package "
        "with no `meta` entry carries its media map as a JSON object."
      ) from error
    if not isinstance(media_map, dict):
      raise PackageReadError(
        "Cannot read the package: its `media` entry parses as JSON, but not as an "
        f"object mapping zip entries to file names (found {type(media_map).__name__})."
      )
    invalid_keys: list[Any] = []
    invalid_filenames: list[Any] = []
    for idx, filename in media_map.items():  # pyright: ignore[reportUnknownVariableType]
      int_parses_idx: bool = isinstance(idx, str) and idx.isascii() and idx.isdigit()
      if not (int_parses_idx and is_uint32(int(idx))):  # pyright: ignore[reportUnknownArgumentType]
        invalid_keys.append(idx)
      if not (isinstance(filename, str) and is_media_name_safe(filename)):
        invalid_filenames.append(filename)
    if invalid_keys or invalid_filenames:
      msg = (
        "Cannot read the package: its `media` entry is malformed. Keys must be decimal "
        "strings and file names must be a single path component with no drive:"
      )
      if invalid_keys:
        msg += f"\n  Keys that are not a decimal number: {invalid_keys}"
      if invalid_filenames:
        msg += f"\n  Invalid file names: {invalid_filenames}"
      raise PackageReadError(msg)

    media_map = cast(dict[str, str], media_map)
    entries: list[MediaEntry] = [
      cls.MediaEntry(name=filename, size=0, sha1=b"", legacy_zip_filename=int(idx))
      for idx, filename in media_map.items()
    ]
    entries.sort(key=lambda entry: entry.legacy_zip_filename)  # pyright: ignore[reportArgumentType]
    return cls(entries=entries)

  @classmethod
  def from_zipfile(
    cls, zfile: zipfile.ZipFile, version: PackageMetadata.Version
  ) -> Self:
    """Read the `media` entry `zfile` holds, in the format its envelope uses.

    The latest envelope stores a zstd-compressed protobuf, the legacy one a JSON
    object (see `MediaEntries.from_legacy_bytes()`). A package with no `media` entry
    has no media files and returns an instance with no entries.

    The map is not checked against the zip entries (see
    `MediaEntries.check_zip_carries_entries()`).

    Args:
      zfile: The package to read.
      version: Envelope version. Must not be `VERSION_UNKNOWN`.

    Raises:
      PackageReadError: bad zip file.
      PackageReadError: failed zstd decompression.
      PackageReadError: the `media` entry does not parse as its envelope's map.
      pydantic.ValidationError: a file name or size in the map fails its field check.
    """
    if NamedEntry.MEDIA not in zfile.namelist():
      return cls()

    try:
      media_bytes: bytes = zfile.read(NamedEntry.MEDIA)
      if version.compressed_entries:
        return cls.from_bytes(zstd_decompress(media_bytes))
      else:
        return cls.from_legacy_bytes(media_bytes)
    except (zipfile.BadZipFile, zstd.ZstdError, ProtobufDecodeError) as error:
      msg = f"Corrupt package entries: cannot read the `media` entry. Error: {error}"
      raise PackageReadError(msg) from error

  @classmethod
  def from_apkg(cls, path: str | Path) -> Self:
    """Return the media map of the package at `path`, leaving its files in the zip.

    Raises:
      PackageReadError: the file is not a package genanki2 can read.
      PackageReadError: the `media` entry does not parse as its envelope's map.
      pydantic.ValidationError: a file name or size in the map fails its field check.
      OSError: the file cannot be opened.
    """
    with open_apkg(path) as apkg_file:
      version: PackageMetadata.Version = PackageMetadata.zipfile_version(apkg_file)
      version.raise_if_unknown(path)
      return cls.from_zipfile(apkg_file, version)


MediaEntry: TypeAlias = MediaEntries.MediaEntry


def read_zipfile_media_entries(
  zfile: zipfile.ZipFile,
  version: PackageMetadata.Version,
  *,
  for_extraction: bool = False,
  warn_renames: bool = True,
) -> tuple[MediaEntries, tuple[ExtractedEntry, ...]]:
  """Return the media map `zfile` holds and its extraction entries.

  A package with no `media` entry has no media files. Warnings are emitted for
  duplicate names in the map, for files stored under a new name (unless `warn_renames`
  is false), and for zip entries that neither the map nor the envelope name, which
  are dropped.

  Args:
    zfile: The package to read.
    version: Envelope version.
    for_extraction: Whether the files are extracted next to the collection. If true,
      the collection's entry name is reserved, so no media file is written over it.
      Default: `False`.
    warn_renames: Whether to warn about the files stored under a new name. A caller
      that renames the references itself turns it off. Default: `True`.

  Raises:
    PackageReadError: bad zip file.
    PackageReadError: failed zstd decompression.
    PackageReadError: the `media` entry does not parse as its envelope's map.
    PackageReadError: the map names a zip entry the package does not carry, or a name
      that repairs to nothing.
  """
  zip_contents: set[str] = set(zfile.namelist())
  media_entries: MediaEntries = MediaEntries.from_zipfile(zfile, version)
  media_entries.check_zip_carries_entries(zip_contents)
  media_entries.warn_duplicate_names()
  taken: set[str] = {version.collection_entry} if for_extraction else set()
  extraction_entries: tuple[ExtractedEntry, ...] = media_entries.extraction_entries(
    taken
  )
  if warn_renames:
    MediaEntries.warn_renames_in_extraction_entries(extraction_entries)

  zip_indices: set[str] = {item.zip_index for item in extraction_entries}
  if stray_entries := zip_contents - set(NamedEntry) - zip_indices:
    warnings.warn(
      "Package: the package carries entries that are not named in the media map "
      "and that an envelope does not use, so nothing reads them. Writing back the "
      f"package leaves them out. Entries: {sorted(stray_entries)}",
      Genanki2Warning,
      stacklevel=2,
    )

  return media_entries, extraction_entries


def extract_zipfile_media(
  zfile: zipfile.ZipFile,
  version: PackageMetadata.Version,
  extract_dir: str | Path,
  extraction_entries: Sequence[ExtractedEntry],
  overwrite: bool = True,
) -> list[str]:
  """Write each media file in `extract_dir` under its name on disk.

  Args:
    zfile: The package to read.
    version: Envelope version.
    extraction_entries: The files to write. Only the zip entry and the name on disk are
      read. The names must be safe to join and free of collisions, so nothing is
      overwritten.
    extract_dir: The directory where to write.
    overwrite: Whether writing over an existing file is accepted. Default: `True`.

  Returns:
    The complete paths of the written media files.

  Raises:
    Genanki2Error: a file exists and `overwrite` is false, or `extract_dir` cannot be
      written into.
    OSError: a write to `extract_dir` failed.
    PackageReadError: the map names a zip entry the package does not carry.
    PackageReadError: bad zip file.
    PackageReadError: failed zstd decompression.
  """
  extract_dir = Path(extract_dir)
  paths: list[Path] = prepare_writing_files(
    (extract_dir / item.disk_name for item in extraction_entries), overwrite
  )
  zip_contents: set[str] = set(zfile.namelist())
  for zip_index, _, disk_name in extraction_entries:
    if zip_index not in zip_contents:
      raise PackageReadError(
        "Missing package entries: an extraction entry names zip entry "
        f"{{{zip_index!r}: {disk_name!r}}}, but the package carries no such entry."
      )

  for (zip_index, _, disk_name), path in zip(extraction_entries, paths):
    try:
      file_bytes: bytes = zfile.read(zip_index)
      if version.compressed_entries:
        file_bytes = zstd_decompress(file_bytes)
      path.write_bytes(file_bytes)
    except (zipfile.BadZipFile, zstd.ZstdError) as error:
      raise PackageReadError(
        "Corrupt package entries: cannot read the media file stored as entry "
        f"{zip_index!r} ({disk_name!r}). Error: {error}"
      ) from error
  return [str(path) for path in paths]


def extract_apkg_media(
  path: str | Path,
  extract_dir: str | Path,
  extraction_entries: Sequence[ExtractedEntry] | None = None,
  overwrite: bool = True,
) -> list[str]:
  """Write the media files of the package at `path` into `extract_dir`.

  If `extraction_entries` is not provided, they are computed using
  `MediaEntries.extraction_entries()`, which normalizes file names. The collection's
  entry name is reserved for it so that a later extraction of the collection into the
  same directory does not collide.

  Args:
    path: The package to read.
    extract_dir: The directory in which to write, created if missing.
    extraction_entries: The files to write (see `extract_zipfile_media()`). Default:
      `None`.
    overwrite: Whether writing over an existing file is accepted. Default: `True`.

  Returns:
    The complete paths of the written media files.

  Raises:
    PackageReadError: the file is not a package genanki2 can read, or its media map or
      a media file is corrupt.
    Genanki2Error: a file exists and `overwrite` is false, or `extract_dir` cannot be
      written into.
    OSError: the file cannot be opened, or a write to `extract_dir` failed.
  """
  with open_apkg(path) as apkg_file:
    version: PackageMetadata.Version = PackageMetadata.zipfile_version(apkg_file)
    version.raise_if_unknown(path)
    if extraction_entries is None:
      _, extraction_entries = read_zipfile_media_entries(
        apkg_file, version, for_extraction=True
      )
    return extract_zipfile_media(
      apkg_file, version, extract_dir, extraction_entries, overwrite
    )
