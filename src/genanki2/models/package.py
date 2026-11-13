import contextlib
import dataclasses as dc
import json
import sys
import tempfile
import warnings
import zipfile
from collections.abc import Iterator, Mapping
from collections.abc import Set as AbstractSet
from importlib import resources
from pathlib import Path
from typing import ClassVar

import pydantic as pyd
from typing_extensions import Self, override

from genanki2.builtins import BUILTIN_NOTETYPES, BUILTIN_NOTETYPES_VERSIONS
from genanki2.errors import Genanki2Error, Genanki2Warning, PackageReadError
from genanki2.io_utils import (
  atomic_replace,
  open_apkg,
  prepare_writing_files,
  resolve_path,
  zstd_decompress,
)
from genanki2.models.base import CheckedModel
from genanki2.models.collection import Collection
from genanki2.models.media_entries import (
  ExtractedEntry,
  MediaEntries,
  MediaEntry,
  extract_zipfile_media,
  read_zipfile_media_entries,
)
from genanki2.models.note import Note
from genanki2.models.notetype import Notetype
from genanki2.models.package_metadata import NamedEntry, PackageMetadata
from genanki2.models.serialization_context import SerializationContext
from genanki2.models.template import Template
from genanki2.text import RE_REMOTE_MEDIA_REF, normalize_media_name
from genanki2.utils import duplicates

if sys.version_info >= (3, 14):
  from compression import zstd  # pyright: ignore[reportUnreachable]
else:
  import zstandard as zstd


class _PackageBase(CheckedModel):
  """A collection and its media files.

  A package is written to/read from the file system as a zip file. The zip holds the
  collection database, a `media` map, and one entry per media file named after its
  position in that map.

  How those entries are named and compressed is the package envelope, chosen by the
  writer. `Package.write_apkg()` writes the latest one.

  Attributes:
    collection: The tables to write.
    media: Unique paths to the files to pack, in map order. Only the final component of
      each path is stored, so two paths ending in the same name collide. The name is
      taken from the path as written, so a symlink is stored under its own name with
      its target's contents, and `~` expands to the home folder. Defaults to an empty
      list.
  """

  # ----------------------------------- Nested types -----------------------------------

  class _SourceMetadata(pyd.BaseModel):
    """The package from where to copy media files when this package is written.

    Attributes:
      path: The source package.
      version: The source envelope version.
      media_crc: The CRC-32 stored in the zip for the `media` entry when the source was
        read, compared before writing to tell whether the map changed.
      extraction_entries: Each file's zip entry, name in the package and repaired name
        on disk (see `MediaEntries.extraction_entries()`).
    """

    # Pydantic
    model_config: ClassVar[pyd.ConfigDict] = pyd.ConfigDict(
      arbitrary_types_allowed=True, frozen=True
    )

    path: Path
    version: PackageMetadata.Version
    media_crc: int
    extraction_entries: tuple[ExtractedEntry, ...]

    def open_and_check(self) -> zipfile.ZipFile:
      """Open the source package and check if it matches the one that was read.

      The caller owns the returned zip and must close it.

      Because media files are copied when the package is written, this checks if the
      source package still matches the one that was read. What is checked: it exists,
      has the same version, holds the same media map and carries every entry the map
      names.

      The media map is compared by the CRC-32 stored by the zip for its entry, so
      nothing is read or decompressed. In the latest envelope, the map holds each
      file's size and SHA-1, so a changed file changes the map too. A legacy map holds
      only names.

      Raises:
        PackageReadError: missing file, different version, different media map, or
          missing entries.
        PackageReadError: the file is not a package genanki2 can read, or it declares
          an envelope genanki2 does not know.
        Genanki2Error: the path is no longer a file.
        OSError: the file cannot be opened.
      """
      # extra checks can be optionally added later: hashes of each media file, etc.
      path: Path = resolve_path(self.path)
      if not path.exists():
        raise PackageReadError(f'Missing source package: "{path}".')
      with contextlib.ExitStack() as stack:
        source_apkg: zipfile.ZipFile = stack.enter_context(open_apkg(path))
        version: PackageMetadata.Version = PackageMetadata.zipfile_version(source_apkg)
        version.raise_if_unknown(path)
        if version != self.version:
          raise PackageReadError(
            f'Source version mismatch: "{path}" declares version {version.name}, '
            f"which doesn't match version {self.version.name} when it was read."
          )
        source_namelist: set[str] = set(source_apkg.namelist())
        if (
          NamedEntry.MEDIA not in source_namelist
          or self.media_crc != source_apkg.getinfo(NamedEntry.MEDIA).CRC
        ):
          raise PackageReadError(
            f'Source media map changed: "{path}" no longer holds the media map it held '
            "when it was read, so its entries can't be matched to the files they were "
            "read as. Read the package again."
          )
        missing_entries: list[str] = [
          zip_index
          for zip_index, _, _ in self.extraction_entries
          if zip_index not in source_namelist
        ]
        if missing_entries:
          raise PackageReadError(
            f'Missing source entries: "{path}" no longer carries every entry its '
            "media map named when it was read, so they can't be copied. Missing "
            f"entries: {sorted(missing_entries)}"
          )
        stack.pop_all()
        return source_apkg

    def iter_media(
      self, zfile: zipfile.ZipFile, skip: AbstractSet[str] | None = None
    ) -> Iterator[tuple[str, bytes, bytes | None]]:
      """Yield the name, the content and the stored frame of each file from source.

      The first value is the file name in the extraction map, which should be repaired
      and free of collisions. The second value is the raw file bytes. If the source is
      a latest version package, the third value is the file's zstd-compressed bytes,
      otherwise `None`.

      Args:
        zfile: The source package, open for reading.
        skip: Names of files in the source package that should be skipped. This avoids
          name collisions when the written package contains in its media list files
          with the same name (the media list has priority). Default: `None`.

      Raises:
        zipfile.BadZipFile: an entry cannot be read out of the source.
        ZstdError: an entry does not decompress, although the envelope the source
          declares says it is compressed.
      """
      skip = skip or set()
      for zip_index, _, disk_name in self.extraction_entries:
        if disk_name not in skip:
          entry_bytes: bytes = zfile.read(zip_index)
          if self.version.compressed_entries:
            yield disk_name, zstd_decompress(entry_bytes), entry_bytes
          else:
            yield disk_name, entry_bytes, None

    # --------------------------------- Serialization ----------------------------------

    @classmethod
    def from_path(cls, path: str | Path) -> Self:
      """Read the media map of the package at `path`, leaving its files in the zip.

      Raises:
        PackageReadError: the file is not a package genanki2 can read.
        Genanki2Error: `path` is missing or is not a file.
        Genanki2Error: the package carries no media files.
        OSError: the file cannot be opened.
      """
      path = resolve_path(path)
      with open_apkg(path) as apkg_file:
        version: PackageMetadata.Version = PackageMetadata.zipfile_version(apkg_file)
        version.raise_if_unknown(path)
        _, extraction_entries = read_zipfile_media_entries(apkg_file, version)
        if not extraction_entries:
          raise Genanki2Error(
            f'No source media: the package at "{path}" carries no media files, so '
            "there is nothing to copy from it."
          )
      return cls(
        path=path,
        version=version,
        media_crc=apkg_file.getinfo(NamedEntry.MEDIA).CRC,
        extraction_entries=extraction_entries,
      )

  # ------------------------------------ Attributes ------------------------------------

  collection: Collection = pyd.Field(default_factory=Collection)
  media: list[str] = pyd.Field(default_factory=list)
  _source_metadata: _SourceMetadata | None = pyd.PrivateAttr(default=None)

  # ---------------------------------- Derived values ----------------------------------

  @property
  def media_filenames(self) -> frozenset[str]:
    """The name of every media file the package will write.

    Only the file name from each path in `media` is returned. If a source package is
    set, this returns its media file names too (repaired and free of collisions).

    These names can be used as media references.
    """
    media: frozenset[str] = frozenset(Path(path).name for path in self.media)
    if self._source_metadata is not None:
      media = media.union(
        item.disk_name for item in self._source_metadata.extraction_entries
      )
    return media

  def _dangling_media_references(
    self,
  ) -> tuple[
    list[tuple[Note, str]], list[tuple[Notetype, str]], list[tuple[Template, str]]
  ]:
    """Collect every media reference missing its file, grouped by location.

    The three groups are the three places where references appear: a note's fields,
    a notetype's CSS imports and the sides of a template. The three lists contain pairs
    of object plus dangling reference.

    In notetypes, Anki's convention is to gather only reference names beginning
    with `_`, so a stylesheet or a template naming a file any other way is omitted.

    Whether a dangling reference is an error or a warning depends on the importer of
    the package.
    """
    media: frozenset[str] = self.media_filenames

    note_references: list[tuple[Note, str]] = []
    for note in self.collection.notes:
      for reference in note.media_references:
        if not RE_REMOTE_MEDIA_REF.match(reference) and reference not in media:
          note_references.append((note, reference))

    css_references: list[tuple[Notetype, str]] = []
    for notetype in self.collection.notetypes:
      for reference in notetype.underscored_css_imports:
        if reference not in media:
          css_references.append((notetype, reference))

    template_references: list[tuple[Template, str]] = []
    for notetype in self.collection.notetypes:
      for template in notetype.templates:
        for reference in template.underscored_references:
          if reference not in media:
            template_references.append((template, reference))

    return note_references, css_references, template_references

  # --------------------------------- Media management ---------------------------------

  def add_source_media(self, path: str | Path) -> Self:
    """Copy the media files of the package at `path` when this package is written.

    The files stay in the source until the write, so the source has to still be there
    then. A source set earlier is replaced.

    Raises:
      PackageReadError: the file is not a package genanki2 can read.
      Genanki2Error: `path` is missing or is not a file.
      Genanki2Error: the package carries no media files.
      OSError: the file cannot be opened.
    """
    self._source_metadata = self._SourceMetadata.from_path(path)
    return self

  def remove_source_media(self) -> Self:
    """Stop copying media files from a source package when this package is written."""
    self._source_metadata = None
    return self

  # ------------------------------------- Repairs --------------------------------------

  def replace_media_references(self, renames: Mapping[str, str]) -> Self:
    """Rename the media references the collection holds, wherever they are written.

    After a file name is rejected by `Package.check_media_paths()` and a caller renames
    it, it leaves every reference to the old name dangling. This is the repair for
    those references, using a mapping from the old to the new names.

    All three sites that name a file are repaired: every note's fields, every
    notetype's CSS and both formats of every one of its templates. The two notetype
    sites follow Anki's convention and repair only names beginning with `_`, so a
    stylesheet or a template naming a file any other way is left as written.

    `media` is not read or modified here, so updating it is the caller's responsibility.

    An empty map is a no-op. When `renames` is not empty, this method runs
    `Collection.collect_notes()` and `Collection.collect_notetypes()` first, so a note
    or a notetype reachable only through a card is repaired too.

    Nothing calls this unless a caller opts in, directly or by reading a package with
    `repair_media_references=True`.

    Args:
      renames: The old name of each renamed file, mapped to its new name. A note field
        is matched on the decoded name, so the key `a&b.jpg` renames a reference
        spelled `a&amp;b.jpg`; a notetype's CSS and templates are matched literally, so
        the key is the spelling the file carries.

    Raises:
      Genanki2Error: two notes or two notetypes with different content share an id.
    """
    if not renames:
      return self
    self.collection.collect_notes().collect_notetypes()
    for note in self.collection.notes:
      note.replace_media_references(renames)
    for notetype in self.collection.notetypes:
      notetype.replace_underscored_css_imports(renames)
      for template in notetype.templates:
        template.replace_underscored_references(renames)
    return self

  # ----------------------------- Pre-serialization steps ------------------------------

  @pyd.field_validator("media", mode="after")
  @classmethod
  def check_media_paths(cls, media: list[str]) -> list[str]:
    """Check that each file exists, is readable, and can be stored under its own name.

    Each file name has to be unique because that is what the media map stores. Also,
    the name has to be normalized and a single path component valid on every platform.

    Raises:
      Genanki2Error: two paths end in the same file name.
      Genanki2Error: a path has no final component.
      Genanki2Error: a file cannot be opened.
      Genanki2Error: a name is not the one `text.normalize_media_name()` gives it.
    """
    paths: list[tuple[str, Path, str]] = [
      (Path(path).name, resolve_path(path), path) for path in media
    ]

    if dupes := duplicates(name for name, _, _ in paths):
      msg: str = f"Check failed: not all media paths are unique. Duplicates: {dupes}"
      raise Genanki2Error(msg)

    unnormalized_paths: list[tuple[str, str]] = []
    for name, path, media_path in paths:
      if not name:
        raise Genanki2Error(f"Check failed: file name is empty. Path: {media_path}")
      try:
        open(path, "rb").close()
      except OSError as error:
        raise Genanki2Error(
          f"Check failed: cannot read the media file at {media_path!r} ({path}). "
          f"Error: {error}"
        ) from error
      if (normalized := normalize_media_name(name)) != name:
        unnormalized_paths.append((media_path, normalized))

    if unnormalized_paths:
      listing: str = "\n".join(
        f"  {media_path!r} -> {normalized!r}"
        for media_path, normalized in unnormalized_paths
      )
      raise Genanki2Error(
        "Check failed: not all media file names are normalized. Anki rejects a package "
        "that carries any other form. Rename each file to the name on its right "
        "(generated using `genanki2.text.normalize_media_name()`):\n"
        f"{listing}"
      )

    return media

  def warn_unused_media_files(self) -> Self:
    """Warn about a packed file with no note references.

    An unused file imports, but is bloat in the collection. A name beginning with `_`
    is exempt.

    False positive warnings are possible because media filename normalization is
    stricter than note field normalization. Consider normalizing the references in the
    same way (see `text.normalize_media_name()`) to remove false positives.
    """
    media_references: set[str] = {
      reference for note in self.collection.notes for reference in note.media_references
    }
    latex_references: set[str] = {
      image for note in self.collection.notes for image in note.latex_image_references
    }
    unused_files: frozenset[str] = (
      self.media_filenames - media_references - latex_references
    )
    unused_media: list[str] = [
      f'"{unused}"' for unused in unused_files if not unused.startswith("_")
    ]
    if unused_media:
      name: str = self.__class__.__name__
      warnings.warn(
        f"{name}: no note references media files {', '.join(sorted(unused_media))}. "
        "Investigate for false positives: media filename normalization removes more "
        "characters than note field normalization does. Normalize the references in "
        "the same way (see `genanki2.text.normalize_media_name()`) to surface true "
        "unused files.",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  def _dangling_media_references_listings(self) -> str:
    """List by location the media references without a file in the package.

    One block per location (notes, notetype CSS, templates), with a line per object and
    reference. Empty when every reference resolves.
    """
    listings: list[str] = []
    note_references, css_references, template_references = (
      self._dangling_media_references()
    )

    lines: str = "\n".join(
      f"  Note id {note.id} -> {reference!r}" for note, reference in note_references
    )
    if note_references:
      listings.append(f"Notes:\n{lines}")

    lines = "\n".join(
      f"  Notetype id {notetype.id} -> {reference!r}"
      for notetype, reference in css_references
    )
    if css_references:
      listings.append(f"Notetypes, in the css:\n{lines}")

    lines = "\n".join(
      f'  Template "{template.name}" -> {reference!r}'
      for template, reference in template_references
    )
    if template_references:
      listings.append(f"Templates:\n{lines}")

    return "\n".join(listings)

  def _dangling_source_renames_listing(self) -> str:
    """List the source renames that fix a dangling media reference, one per line.

    A source package can store a file under a repaired name, and the writer copies it
    under that name, so a reference to the name the package gives it dangles. Empty
    when there is no source or no dangling reference names a renamed file.
    """
    if (source := self._source_metadata) is None:
      return ""
    dangling_refs: set[str] = {
      ref for group in self._dangling_media_references() for _, ref in group
    }
    return "\n".join(
      f"  {item.package_name!r}: {item.disk_name!r},"
      for item in MediaEntries.extraction_renames(source.extraction_entries)
      if item.package_name in dangling_refs
    )

  # ---------------------------------- Serialization -----------------------------------

  @classmethod
  def _read_v18_latest(
    cls,
    path: str | Path,
    extract_dir: str | Path | None = None,
    overwrite: bool = True,
    keep_source_media: bool = False,
    repair_media_references: bool = False,
  ) -> Self:
    """Read the Anki package at `path`.

    The `meta` entry or the collection filename decides the version (see
    `PackageMetadata.zipfile_version()`). An unknown version raises.

    Because `Package.media` holds complete paths, media files are:
    - extracted to `extract_dir` when given.
    - kept in the source for the writer to copy when `keep_source_media=True`.
    - ignored, and `Package.media` stays empty, when both are not set.

    The media map is parsed, validated and repaired in every case, raising errors or
    emitting warnings.

    A file whose name the filesystem cannot take is repaired and extracted or later
    written under the repaired name. Not repairing and writing back a broken file name
    results in a package Anki rejects and doesn't repair in the latest envelope.
    Therefore, references inside the collection still need to be updated. A warning
    lists the renames to pass to `Package.replace_media_references()`, or
    `repair_media_references=True` applies them before returning, without the warning.

    Parameters `extract_dir` and `keep_source_media` cannot be used together.

    Args:
      path: The package to read.
      extract_dir: A directory to write the collection and its media, created if
        missing. Default: `None`.
      overwrite: Whether writing over an existing file is accepted. Default: `True`.
      keep_source_media: Whether to copy the media files from `path` when the package
        is written, instead of extracting them. Ignored for a package with no media
        files. Default: `False`.
      repair_media_references: Whether to rename the references to every file stored
        under a repaired name. A duplicate name in the media map is left as written.
        Default: `False`.

    Raises:
      PackageReadError: the file is not a package genanki2 can read, or it declares an
        envelope genanki2 does not know.
      Genanki2Error: `path` is missing or is not a file.
      Genanki2Error: both `extract_dir` and `keep_source_media` are given.
      Genanki2Error: a file to extract is a directory, it exists and `overwrite` is
        false, or `extract_dir` cannot be written into.
      CollectionReadError: the entry read out of the package is not a collection
        genanki2 can read.
      CollectionReadError: the fields or templates of a notetype do not have ordinals
        running contiguously from 0.
      pydantic.ValidationError: a value in the collection or the media map failed a
        field annotation check.
      OSError: the file cannot be opened, or a write to `extract_dir` failed.
    """
    if extract_dir is not None and keep_source_media:
      raise Genanki2Error(
        "Invalid arguments: `extract_dir` and `keep_source_media=True` are mutually "
        "exclusive. Extracting writes the media files to disk, keeping the source "
        "leaves them in the package."
      )
    with open_apkg(path) as apkg_file:
      version: PackageMetadata.Version = PackageMetadata.zipfile_version(apkg_file)
      version.raise_if_unknown(path)
      media_entries, extraction_entries = read_zipfile_media_entries(
        apkg_file,
        version,
        for_extraction=extract_dir is not None,
        warn_renames=not repair_media_references,
      )

      if extract_dir is not None:
        extract_dir = Path(extract_dir)
        paths: list[Path] = [extract_dir / version.collection_entry]
        paths.extend(extract_dir / item.disk_name for item in extraction_entries)
        _ = prepare_writing_files(paths, overwrite)

      source_metadata: _PackageBase._SourceMetadata | None = None
      if media_entries.entries and keep_source_media:
        source_metadata = cls._SourceMetadata(
          path=resolve_path(path),
          version=version,
          media_crc=apkg_file.getinfo(NamedEntry.MEDIA).CRC,
          extraction_entries=extraction_entries,
        )

      collection: Collection = Collection.from_zipfile(
        apkg_file, version, extract_dir, overwrite
      )
      media: list[str] = []
      if extract_dir is not None:
        media = extract_zipfile_media(
          apkg_file, version, extract_dir, extraction_entries, overwrite
        )
      elif media_entries.entries and not keep_source_media:
        warnings.warn(
          f"Package: the package carries {len(media_entries.entries)} media files. "
          "Source media are not copied over and no extraction directory is given, so "
          "`genanki2.Package.media` is empty. Writing this package back either raises "
          "(see `genanki2.Package.check_media_references_are_present()`) or drops "
          "every file. Pass `extract_dir` or set `keep_source_media=True` to keep "
          "them.",
          Genanki2Warning,
          stacklevel=2,
        )

    package: Self = cls(collection=collection, media=media)
    package._source_metadata = source_metadata
    if repair_media_references:
      package.replace_media_references(
        {
          item.package_name: item.disk_name
          for item in MediaEntries.extraction_renames(extraction_entries)
        }
      )
    return package

  def _open_source(
    self, destination: str | Path
  ) -> contextlib.AbstractContextManager[zipfile.ZipFile | None]:
    """Return the source package open for reading, or a context yielding `None`.

    The source is opened and checked, raising before the writer starts writing.

    Args:
      destination: Where the package is being written.

    Raises:
      Genanki2Error: `destination` is the source package.
    """
    source: _PackageBase._SourceMetadata | None = self._source_metadata
    if source is None:
      return contextlib.nullcontext()
    destination = Path(destination).expanduser()
    if (
      destination.exists()
      and source.path.exists()
      and source.path.samefile(destination)
    ):
      raise Genanki2Error(
        f'Cannot write to "{destination}": the package copies its media files from '
        "it, and writing over it would leave the package copying from a file that no "
        "longer holds them. Write to another path, or read the package with "
        "`extract_dir` to keep its media files on disk."
      )
    return source.open_and_check()

  def _iter_media(
    self, source: zipfile.ZipFile | None = None
  ) -> Iterator[tuple[str, bytes, bytes | None]]:
    """Yield the name, bytes and zstd frame (if available) of every file to pack.

    The files in `media` come first, read from disk, then the retained source's
    files. If both carry a file with the same name, the `media` list has priority.

    Args:
      source: The source package open for reading. Default: `None`.
    """
    media_names: set[str] = set()
    for media_file in self.media:
      name: str = Path(media_file).name
      media_names.add(name)
      yield name, resolve_path(media_file).read_bytes(), None

    if self._source_metadata is not None and source is not None:
      yield from self._source_metadata.iter_media(source, media_names)

  def _write_v18_legacy(
    self, path: str | Path, collection_entry: str, overwrite: bool = True
  ) -> Self:
    """Write the package to `path` as a legacy envelope.

    Args:
      path: Path where the package is stored.
      collection_entry: Entry name under which to store the collection in the zip.
      overwrite: Whether writing over an existing package is allowed. Default: `True`.
    """
    with (
      atomic_replace(path, overwrite) as temp_path,
      self._open_source(path) as source_apkg,
      tempfile.TemporaryDirectory() as tmp,
    ):
      sqlite_path: Path = Path(tmp) / collection_entry
      self.collection._sqlite(sqlite_path)
      with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as apkg_file:
        apkg_file.write(sqlite_path, collection_entry)
        media_map: dict[str, str] = {}
        for idx, (name, file_bytes, _) in enumerate(self._iter_media(source_apkg)):
          media_map[str(idx)] = name
          apkg_file.writestr(str(idx), file_bytes)
        apkg_file.writestr(NamedEntry.MEDIA, json.dumps(media_map))
    return self

  def _write_v18_legacy_1(self, path: str | Path, overwrite: bool = True) -> Self:
    """Write the package to `path` as the legacy 1 envelope.

    This envelope is kept for testing purposes only. Nothing in genanki2 writes it
    by default.

    Legacy zip entries:
      - `collection.anki2`: the collection database
      - `media`: a JSON map of indices to filenames
      - `<N>`: every media file named after its position in that map
    """
    return self._write_v18_legacy(path, NamedEntry.COLLECTION_ANKI2, overwrite)

  def _write_v18_legacy_2(self, path: str | Path, overwrite: bool = True) -> Self:
    """Write the package to `path` as the legacy 2 envelope.

    This envelope is kept for testing purposes only. Nothing in genanki2 writes it
    by default.

    Legacy zip entries:
      - `collection.anki21`: the collection database
      - `media`: a JSON map of indices to filenames
      - `<N>`: every media file named after its position in that map
    """
    return self._write_v18_legacy(path, NamedEntry.COLLECTION_ANKI21, overwrite)

  def _write_v18_latest(self, path: str | Path, overwrite: bool = True) -> Self:
    """Write the package to `path` as the latest envelope.

    Zip entries:
      - `meta`: an uncompressed `PackageMetadata` blob. The importer reads it first.
        Declaring `VERSION_LATEST` commits the package to this envelope, otherwise it's
        rejected.
      - `collection.anki21b`: the zstd-compressed collection.
      - `collection.anki2`: a schema 11 collection with a single note asking the user
        to update. It's intended for an old client to show it and not fail.
      - `<N>`: every zstd-compressed media file; `N` is the positional index.
      - `media`: a zstd-compressed `MediaEntries` protobuf.

    Media names need to be normalized, otherwise Anki rejects the import.
    """
    with (
      atomic_replace(path, overwrite) as temp_path,
      self._open_source(path) as source_apkg,
      tempfile.TemporaryDirectory() as tmp,
    ):
      sqlite_path: Path = Path(tmp) / NamedEntry.COLLECTION_ANKI21B
      self.collection._sqlite(sqlite_path)
      with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_STORED) as apkg_file:
        apkg_file.writestr(NamedEntry.META, PackageMetadata()._bytes())
        apkg_file.writestr(
          NamedEntry.COLLECTION_ANKI21B, zstd.compress(sqlite_path.read_bytes())
        )
        # A v11 collection with 1 note whose Front says "this file needs a newer Anki"
        apkg_file.writestr(
          NamedEntry.COLLECTION_ANKI2,
          resources.files("genanki2").joinpath("_dummy_collection.anki2").read_bytes(),
          compress_type=zipfile.ZIP_DEFLATED,
        )

        media_entries: MediaEntries = MediaEntries()
        for idx, (name, file_bytes, frame) in enumerate(self._iter_media(source_apkg)):
          if frame is None:
            frame = zstd.compress(file_bytes)
          apkg_file.writestr(str(idx), frame)
          media_entries.entries.append(MediaEntry.from_file_bytes(name, file_bytes))
        apkg_file.writestr(NamedEntry.MEDIA, zstd.compress(media_entries._bytes()))
    return self


class Package(_PackageBase):
  """A typical Anki package, used for creating and sharing decks.

  A package is written to/read from the file system as a zip file. The zip holds the
  collection database, a `media` map, and one entry per media file named after its
  position in that map. Use `Package.read_apkg()` and `Package.write_apkg()` for that.

  Reading a package may involve extracting the media files. This is the recommended
  workflow because it allows more flexibility in checking and repairing references.

  `Package.write_apkg()` writes the latest package format, which includes compression
  of the collection and of the media files.

  Attributes:
    collection: The tables to write.
    media: Unique paths to the files to pack, in map order. Only the final component of
      each path is stored, so two paths ending in the same name collide. The name is
      taken from the path as written, so a symlink is stored under its own name with
      its target's contents, and `~` expands to the home folder. Defaults to an empty
      list.
  """

  # ------------------------------------- Repairs --------------------------------------

  def trim_unused_media(self) -> Self:
    """Drop every packed file that no note, notetype CSS or template references.

    Deleting notes does not delete the media files they reference. If the media files
    are not referenced by anything, they become unnecessary weight in the package.

    References are gathered the way Anki's exporter gathers files: every media
    reference and rendered LaTeX image in a note's fields and names beginning with `_`
    in a notetype's CSS and templates.

    The notes and notetypes are collected first. If the package knows of a source
    package for media, the source's tracked media is also trimmed and not copied back
    on write.

    A warning lists the files that were dropped.

    Nothing calls this; a caller opts in.

    Raises:
      Genanki2Error: two paths in `media` end in the same file name.
    """
    source: Package._SourceMetadata | None = self._source_metadata
    extraction_entries: tuple[ExtractedEntry, ...] | None = (
      source.extraction_entries if source is not None else None
    )

    if not self.media and not extraction_entries:
      return self

    self.collection.collect_notes().collect_notetypes()
    media_references: set[str] = {
      reference for note in self.collection.notes for reference in note.media_references
    }
    latex_references: set[str] = {
      image for note in self.collection.notes for image in note.latex_image_references
    }
    css_references: set[str] = {
      reference
      for notetype in self.collection.notetypes
      for reference in notetype.underscored_css_imports
    }
    template_references: set[str] = {
      reference
      for notetype in self.collection.notetypes
      for template in notetype.templates
      for reference in template.underscored_references
    }
    all_references: set[str] = (
      media_references | latex_references | css_references | template_references
    )
    removed: list[str] = []

    if self.media:
      filenames: list[str] = [Path(path).name for path in self.media]
      if duplicate_names := duplicates(filenames):
        raise Genanki2Error(
          "Trim failed: not all media paths end in a unique file name, so a reference "
          f"cannot tell them apart. Duplicates: {duplicate_names}"
        )
      media_map: dict[str, str] = dict(zip(filenames, self.media))
      used_media: set[str] = all_references.intersection(media_map)
      trimmed_media: list[str] = []
      for filename, path in media_map.items():
        if filename in used_media:
          trimmed_media.append(path)
        else:
          removed.append(filename)
      self.media: list[str] = trimmed_media

    if source is not None and extraction_entries:
      used_media = all_references.intersection(
        item.disk_name for item in extraction_entries
      )
      trimmed_extraction_entries: list[ExtractedEntry] = []
      for item in extraction_entries:
        if item.disk_name in used_media:
          trimmed_extraction_entries.append(item)
        else:
          removed.append(item.disk_name)
      self._source_metadata: _PackageBase._SourceMetadata | None = source.model_copy(
        update={"extraction_entries": tuple(trimmed_extraction_entries)}
      )

    if removed:
      listing: str = "\n".join(f"  {name!r}" for name in sorted(removed))
      warnings.warn(
        f"Package: removed {len(removed)} media files that no note, notetype CSS or "
        f"template references:\n{listing}",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  # ----------------------------- Pre-serialization steps ------------------------------

  @staticmethod
  def check_builtin_notetype_versions() -> None:
    """Reject a builtin notetype with modification time unequal to the declared version.

    A builtin carries a version number in its modification time rather than a timestamp,
    because that value is what an import compares to decide whether to replace a
    notetype the target collection already holds. An unchanged version leaves the
    existing notetype in the user's collection unchanged across package rebuilds (user
    edits included); any larger value overwrites their fields and templates on import.
    The check is here to keep a package from replacing the builtin notetypes the user
    already has.

    Two causes for the raise:
      1. A modification time stamped by a writer. While this is correct writer behaviour
        when defining new notetypes or when updating them, builtin notetype constants
        cannot be changed because changes will be present in every package the process
        writes, including the already built ones.
      2. `builtins.BUILTIN_NOTETYPES_VERSIONS` has drifted from what
        `scripts/dump_empty_collection_rows.py` wrote, which would be a genanki2 bug.

    Raises:
      Genanki2Error: a builtin notetype's modification time differs from its version in
        `BUILTIN_NOTETYPES_VERSIONS`.
    """
    mismatched: list[str] = [
      f'"{nt.name}" at version {nt.modification_time_s} instead of {version}'
      for nt in BUILTIN_NOTETYPES
      if nt.modification_time_s != (version := BUILTIN_NOTETYPES_VERSIONS[nt.name])
    ]
    if mismatched:
      raise Genanki2Error(
        "Check failed: a builtin notetype does not carry the version it declares "
        f"({'; '.join(mismatched)}). The version decides whether an import replaces the "
        "notetype the user already has, so it moves only when the content of a builtin "
        "changes."
      )

  def check_media_references_are_present(self) -> Self:
    """Reject a package with missing media files referenced by notes/notetypes/templates.

    Note fields, notetype CSS files and template formats reference files and they must
    be present in the package. A reference to a remote address is skipped (only note
    fields can hold them).

    Media filenames and note fields are normalized by different rules. Media
    normalization is much stricter (see `text.normalize_media_name()`), so
    consider normalizing the references in the same way. CSS and template formats are
    read exactly as written and can suffer from the same issue.

    Every reference the package does not carry is listed, grouped by where it is
    written. A source package (see `Package.add_source_media()`) can store a file under
    a repaired name, and the writer copies it under that name. The renames that fix the
    dangling references to such files are listed too.

    Raises:
      Genanki2Error: a note, a notetype's CSS or a template references a file the
        package does not carry.
    """
    if listings := self._dangling_media_references_listings():
      message: str = (
        "Check failed: some notes, notetypes or templates reference media files the "
        "package does not carry. Add each file to `media` or correct the reference "
        "using the same normalization method as the media filenames (see "
        f"`genanki2.text.normalize_media_name()`).\n{listings}"
      )
      if renames := self._dangling_source_renames_listing():
        message += (
          "\nThe source package stores some of these files under a repaired name. "
          "Rename the references by passing this mapping to "
          "`genanki2.Package.replace_media_references()`, or read the package with "
          f"`repair_media_references=True`:\n{{\n{renames}\n}}"
        )
      raise Genanki2Error(message)
    return self

  @override
  def check(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Reject a package that cannot be written.

    The order is not significant: no check repairs anything, so none of them can
    influence each other.

    Args:
      recursive: Whether to check every model reachable from this one too, each once.
        If false, it only runs this model's own checks. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.check_media_paths(self.media)
    self.check_builtin_notetype_versions()
    self.check_media_references_are_present()
    return super().check(recursive=recursive, context=context)

  @override
  def warn(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Warn about future ids, skipped decks or notetypes or bloat.

    Warnings can be turned off using `with genanki2.warnings_disabled():`.

    Args:
      recursive: Whether to warn about every model reachable from this one too, each
        once. If false, it only runs this model's own warnings. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.warn_unused_media_files()
    return super().warn(recursive=recursive, context=context)

  # ---------------------------------- Serialization -----------------------------------

  @classmethod
  def read_apkg(
    cls,
    path: str | Path,
    extract_dir: str | Path | None = None,
    overwrite: bool = True,
    keep_source_media: bool = False,
    repair_media_references: bool = False,
  ) -> Self:
    """Read the Anki package at `path`.

    The `meta` entry or the collection filename decides the version (see
    `PackageMetadata.zipfile_version()`). An unknown version raises.

    Because `Package.media` holds complete paths, media files are:
    - extracted to `extract_dir` when given.
    - kept in the source for the writer to copy when `keep_source_media=True`.
    - ignored, and `Package.media` stays empty, when both are not set.

    The two parameters cannot be used together. The media map is parsed, validated and
    repaired in every case, raising errors or emitting warnings.

    A file whose name the filesystem cannot take is repaired and extracted or later
    written under the repaired name. Not repairing and writing back a broken file name
    results in a package Anki rejects and doesn't repair in the latest envelope.
    Therefore, references inside the collection still need to be updated. A warning
    lists the renames to pass to `Package.replace_media_references()`, or
    `repair_media_references=True` applies them before returning, without the warning.

    Note: A ".colpkg" file has the same structure as a ".apkg" file, so, technically,
    this method can read ".colpkg" files too without any issues. The difference is that
    the `genanki2.Package` class operates on different assumptions and runs different
    checks when writing a package than `genanki2.CollectionPackage`.

    Args:
      path: The package to read.
      extract_dir: A directory to write the collection and its media into, created if
        missing. Default: `None`.
      overwrite: Whether writing over an existing file is accepted. Default: `True`.
      keep_source_media: Whether to copy the media files from `path` when the package
        is written, instead of extracting them. Ignored for a package with no media
        files. Default: `False`.
      repair_media_references: Whether to rename the references to every file stored
        under a repaired name. A duplicate name in the media map is left as written.
        Default: `False`.

    Raises:
      PackageReadError: the file is not a package genanki2 can read, or it declares an
        envelope genanki2 does not know.
      Genanki2Error: `path` is missing or is not a file.
      Genanki2Error: both `extract_dir` and `keep_source_media` are given.
      Genanki2Error: a file to extract is a directory, it exists and `overwrite` is
        false, or `extract_dir` cannot be written into.
      CollectionReadError: the entry read out of the package is not a collection
        genanki2 can read.
      CollectionReadError: the fields or templates of a notetype do not have ordinals
        running contiguously from 0.
      pydantic.ValidationError: a value in the collection or the media map failed a
        field annotation check.
      OSError: the file cannot be opened, or a write to `extract_dir` failed.
    """
    return cls._read_v18_latest(
      path, extract_dir, overwrite, keep_source_media, repair_media_references
    )

  def write_apkg(
    self,
    path: str | Path,
    overwrite: bool = True,
    *,
    context: SerializationContext | None = None,
  ) -> Self:
    """Write the package to `path`.

    Writes the latest envelope: a `meta` entry declaring `VERSION_LATEST`, the
    collection as a zstd-compressed `collection.anki21b`, a dummy v11 collection for
    old clients, a zstd-compressed protobuf `MediaEntries` media map, and one
    zstd-compressed entry per media file.

    Collects all references and runs all checks before writing. Media names are held to
    a stricter standard than on the legacy envelope, where an unnormalized name is
    repaired on import and the notes referencing it are rewritten to match. A v18 entry
    carrying such a name fails the whole import instead, so the name a `MediaEntry`
    stores has to be normalized before this method is called.

    Anki picks the import behaviour by file name. `Package.write_apkg()` refuses every
    name Anki treats as a whole collection ("*.colpkg", "collection.apkg",
    "backup-*.apkg"); use `CollectionPackage` for those.

    A context's output attribute is replaced with `SerializationContext.Output.APKG`.
    If no context is given, one is created and passed to the phases.

    Args:
      path: Location and file name where to write the package.
      overwrite: Whether writing over an existing file is accepted. Default: `True`.
      context: Extra information from the serialization process, passed to the phases.
        Default: `None`.

    Raises:
      Genanki2Error: `path` uses a filename reserved for a collection export.
      Genanki2Error: `path` is a directory, it exists and `overwrite` is false, or its
        directory cannot be written into.
      Genanki2Error: the package or its collection fails a check.
      Genanki2Error: `path` is the retained source package.
      PackageReadError: the retained source package is gone, declares a different
        version, holds a different media map, or no longer carries an entry from its
        media map.
      zipfile.BadZipFile: an entry cannot be read out of the retained source.
      ZstdError: an entry of the retained source does not decompress.
      OSError: `path` cannot be written, or a media file cannot be read.
    """
    path = Path(path)
    if (
      path.suffix == ".colpkg"
      or path.name == "collection.apkg"
      or (path.stem.startswith("backup-") and path.suffix == ".apkg")
    ):
      raise Genanki2Error(
        f'Invalid file name: Anki reads "{path.name}" as a whole collection and '
        "importing it replaces the user's collection instead of merging into it. "
        "Use something different than: `*.colpkg`, `collection.apkg` or "
        "`backup-*.apkg`. For a package representing a whole collection, use "
        "`genanki2.CollectionPackage`."
      )
    context = dc.replace(
      context or SerializationContext(), output=SerializationContext.Output.APKG
    )
    return self._run_phases(context=context)._write_v18_latest(path, overwrite)


class CollectionPackage(_PackageBase):
  """A package containing a whole collection.

  This class is intended for collection exports or when dealing with automatic backups.
  Importing one replaces the user's collection instead of merging into it, forces the
  next sync to be a full upload, and adds all the media files it carries without
  removing any. Nothing in the file is repaired on import.

  Important: This is the wrong class for building a shared deck (or multiple decks)
  for others to add to their collection. `genanki2.Package` is the intended class for
  that use case (and it can read ".colpkg" files too).

  A package is written to/read from the file system as a zip file. The zip holds the
  collection database, a `media` map, and one entry per media file named after its
  position in that map. Use `CollectionPackage.read_colpkg()` and
  `CollectionPackage.write_colpkg()` for that.

  Reading a package may involve extracting the media files. This is the recommended
  workflow because it allows more flexibility in checking and repairing references.

  `CollectionPackage.write_colpkg()` writes the latest package format, which includes
  compression of the collection and of the media files.

  Attributes:
    collection: The tables to write.
    media: Unique paths to the files to pack, in map order. Only the final component of
      each path is stored, so two paths ending in the same name collide. The name is
      taken from the path as written, so a symlink is stored under its own name with
      its target's contents, and `~` expands to the home folder. Defaults to an empty
      list.
  """

  # ----------------------------- Pre-serialization steps ------------------------------

  @override
  def check(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Reject a collection package that cannot be written.

    Args:
      recursive: Whether to check every model reachable from this one too, each once.
        If false, it only runs this model's own checks. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.check_media_paths(self.media)
    return super().check(recursive=recursive, context=context)

  def warn_missing_media_references(self) -> Self:
    """Warn about a media reference for which the package has no file.

    A collection read out of a backup may reference files that are already gone from
    the user's media folder. A restore adds the files inside the package to the media
    folder and doesn't remove the ones already there, so a reference may still resolve
    after the import.

    A source package (see `CollectionPackage.add_source_media()`) can store a file under
    a repaired name, and the writer copies it under that name. The renames that fix the
    dangling references to such files are listed too.
    """
    if listings := self._dangling_media_references_listings():
      message: str = (
        "CollectionPackage: some notes, notetypes or templates reference media files the "
        "package does not carry. Add the missing files to `media` or correct the "
        "references using the same normalization method as the media filenames (see "
        f"`genanki2.text.normalize_media_name()`).\n{listings}"
      )
      if renames := self._dangling_source_renames_listing():
        message += (
          "\nThe source package stores some of these files under a repaired name. "
          "Rename the references by passing this mapping to "
          "`genanki2.CollectionPackage.replace_media_references()`, or read the "
          f"package with `repair_media_references=True`:\n{{\n{renames}\n}}"
        )
      warnings.warn(message, Genanki2Warning, stacklevel=2)
    return self

  @override
  def warn(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Warn about future ids or unused or missing media.

    Warnings can be turned off using `with genanki2.warnings_disabled():`.

    Args:
      recursive: Whether to warn about every model reachable from this one too, each
        once. If false, it only runs this model's own warnings. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.warn_unused_media_files()
    self.warn_missing_media_references()
    return super().warn(recursive=recursive, context=context)

  # ---------------------------------- Serialization -----------------------------------

  @classmethod
  def read_colpkg(
    cls,
    path: str | Path,
    extract_dir: str | Path | None = None,
    overwrite: bool = True,
    keep_source_media: bool = False,
    repair_media_references: bool = False,
  ) -> Self:
    """Read the collection package at `path`.

    A collection export carries every table in full, including the rows a deck export
    leaves out: graves, a review whose card is gone, filtered decks with the cards
    sitting in them, every config key and the collection's sync columns.

    An automatic backup is also a collection package, named after the time it was taken
    (usually `backup-%Y-%m-%d-%H.%M.%S.colpkg`).

    The `meta` entry or the collection filename decides the version (see
    `PackageMetadata.zipfile_version()`). An unknown version raises.

    Because `CollectionPackage.media` holds complete paths, media files are:
    - extracted to `extract_dir` when given.
    - kept in the source for the writer to copy when `keep_source_media=True`.
    - ignored, and `CollectionPackage.media` stays empty, when both are not set.

    The two parameters cannot be used together. The media map is parsed, validated and
    repaired in every case, raising errors or emitting warnings.

    A file whose name the filesystem cannot take is repaired and extracted or later
    written under the repaired name. Not repairing and writing back a broken file name
    results in a package Anki rejects and doesn't repair in the latest envelope.
    Therefore, references inside the collection still need to be updated. A warning
    lists the renames to pass to `CollectionPackage.replace_media_references()`, or
    `repair_media_references=True` applies them before returning, without the warning.

    Note: A ".apkg" file has the same structure as a ".colpkg" file, so, technically,
    this method can read ".apkg" files too without any issues. The difference is that
    the `genanki2.CollectionPackage` class operates on different assumptions and runs
    different checks when writing a package than `genanki2.Package`.

    Args:
      path: The package to read.
      extract_dir: A directory to write the collection and its media into, created if
        missing. Default: `None`.
      overwrite: Whether writing over an existing file is accepted. Default: `True`.
      keep_source_media: Whether to copy the media files from `path` when the package
        is written, instead of extracting them. Ignored for a package with no media
        files. Default: `False`.
      repair_media_references: Whether to rename the references to every file stored
        under a repaired name. A duplicate name in the media map is left as written.
        Default: `False`.

    Raises:
      PackageReadError: the file is not a package genanki2 can read, or it declares an
        envelope genanki2 does not know.
      Genanki2Error: `path` is missing or is not a file.
      Genanki2Error: both `extract_dir` and `keep_source_media` are given.
      Genanki2Error: a file to extract is a directory, it exists and `overwrite` is
        false, or `extract_dir` cannot be written into.
      CollectionReadError: the entry read out of the package is not a collection
        genanki2 can read.
      CollectionReadError: the fields or templates of a notetype do not have ordinals
        running contiguously from 0.
      pydantic.ValidationError: a value in the collection or the media map failed a
        field annotation check.
      OSError: the file cannot be opened, or a write to `extract_dir` failed.
    """
    return cls._read_v18_latest(
      path, extract_dir, overwrite, keep_source_media, repair_media_references
    )

  def write_colpkg(
    self,
    path: str | Path,
    overwrite: bool = True,
    *,
    context: SerializationContext | None = None,
  ) -> Self:
    """Write the collection package to `path`.

    Writes the latest envelope: a `meta` entry declaring `VERSION_LATEST`, the
    collection as a zstd-compressed `collection.anki21b`, a dummy v11 collection for
    old clients, a zstd-compressed protobuf `MediaEntries` media map, and one
    zstd-compressed entry per media file.

    Anki's legacy collection export is schema 11, which genanki2 doesn't read or write.

    Collects all references and runs all checks before writing. Media names are held to
    a stricter standard than on the legacy envelope, where an unnormalized name is
    repaired on import and the notes referencing it are rewritten to match. A v18 entry
    carrying such a name fails the whole import instead, so the name a `MediaEntry`
    stores has to be normalized before this method is called.

    Anki picks the import behaviour by file name. The ".colpkg" extension tells Anki
    this package is a collection export. `CollectionPackage.write_colpkg()` refuses a
    name that doesn't end in ".colpkg".

    A context's output attribute is replaced with `SerializationContext.Output.COLPKG`.
    If no context is given, one is created and passed to the phases.

    Args:
      path: Location and file name where to write the package.
      overwrite: Whether writing over an existing file is accepted. Default: `True`.
      context: Extra information from the serialization process, passed to the phases.
        Default: `None`.

    Raises:
      Genanki2Error: `path` does not end in ".colpkg".
      Genanki2Error: `path` is a directory, it exists and `overwrite` is false, or its
        directory cannot be written into.
      Genanki2Error: the package or its collection fails a check.
      Genanki2Error: `path` is the retained source package.
      PackageReadError: the retained source package is gone, declares a different
        version, holds a different media map, or no longer carries an entry from its
        media map.
      zipfile.BadZipFile: an entry cannot be read out of the retained source.
      ZstdError: an entry of the retained source does not decompress.
      OSError: `path` cannot be written, or a media file cannot be read.
    """
    if Path(path).suffix != ".colpkg":
      raise Genanki2Error(
        f'Invalid file name: "{Path(path).name}" does not end in ".colpkg". '
        'The ".colpkg" extension tells Anki this package is an export and should '
        "replace the existing collection. Use `genanki2.Package.write_apkg()` if "
        "the file is meant to be a normal deck to be shared."
      )
    context = dc.replace(
      context or SerializationContext(), output=SerializationContext.Output.COLPKG
    )
    return self._run_phases(context=context)._write_v18_latest(path, overwrite)
