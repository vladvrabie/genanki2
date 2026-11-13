import contextlib
import os
import sys
import tempfile
import zipfile
from collections.abc import Generator, Iterable
from pathlib import Path

from genanki2.errors import Genanki2Error, PackageReadError
from genanki2.utils import duplicates

__all__ = [
  "atomic_replace",
  "open_apkg",
  "prepare_reading_file",
  "prepare_writing_dir",
  "prepare_writing_file",
  "prepare_writing_files",
  "resolve_path",
  "zstd_decompress",
]


def resolve_path(path: str | Path) -> Path:
  """Return `path` as an absolute path, with `~` expanded and symlinks followed.

  `~` and `~user` expand the way a shell expands them, so a folder literally named `~`
  is reached as `./~`. Environment variables such as `$HOME` are not expanded.

  Following a symlink can change the final component, so the returned name is not
  always the name the caller gave.
  """
  return Path(path).expanduser().resolve()


def prepare_reading_file(path: str | Path) -> Path:
  """Return `path` resolved (see `resolve_path()`), once it is checked to be a file.

  Checking before opening gives one clear error for a missing file or a directory,
  where the readers would otherwise fail in their own ways, or in SQLite's case create
  an empty database. A symlink to a file passes.

  Raises:
    Genanki2Error: `path` is missing or is not a file.
  """
  path = resolve_path(path)
  if not path.is_file():
    raise Genanki2Error(f'Cannot read from disk: "{path}" is missing or not a file.')
  return path


def prepare_writing_dir(path: str | Path) -> Path:
  """Return `path` resolved, created if missing, once it can be written into.

  A missing directory is judged by its nearest existing parent, which has to be a
  directory the process can write into and enter. `""` means the working directory.

  The check answers for one instant and for the process's user, so it is no guarantee:
  an ACL, a quota or a full disk still surfaces as an `OSError` from the write itself.

  Args:
    path: The directory to write into, created along with any missing parent.

  Raises:
    Genanki2Error: `path`, or its nearest existing parent, is not a directory that can
      be written into.
    OSError: the directory cannot be created, the environment refused.
  """
  path = resolve_path(path)
  existing: Path | None = next((p for p in (path, *path.parents) if p.exists()), None)
  if not (
    existing is not None
    and existing.is_dir()
    and os.access(existing, os.W_OK | os.X_OK)
  ):
    raise Genanki2Error(
      f'Cannot write to disk: "{path}" is not a directory that can be written into.'
    )
  path.mkdir(parents=True, exist_ok=True)
  return path


def prepare_writing_file(path: str | Path, overwrite: bool = True) -> Path:
  """Return `path` resolved, with a directory ready to hold the file.

  Args:
    path: The file to write.
    overwrite: Whether writing over an existing file is accepted. Default: `True`.

  Raises:
    Genanki2Error: `path` is a directory, the file exists and `overwrite` is false, or
      its directory cannot be written into.
    OSError: the directory cannot be written, the environment refused.
  """
  path = resolve_path(path)
  if path.is_dir():
    raise Genanki2Error(f'Cannot write to disk: "{path}" is a directory.')
  if path.exists() and not overwrite:
    raise Genanki2Error(f'Cannot write to disk: "{path}" already exists.')
  prepare_writing_dir(path.parent)
  return path


def prepare_writing_files(
  paths: Iterable[str | Path], overwrite: bool = True
) -> list[Path]:
  """Return each path resolved, in order, once every file in the batch can be written.

  Checking the whole batch first means a refusal leaves no partial set of files behind.
  The directory of each file is created if missing, so a refusal can still leave empty
  directories.

  Args:
    paths: The files to write.
    overwrite: Whether writing over an existing file is accepted. Default: `True`.

  Raises:
    Genanki2Error: a path is a directory, a file exists and `overwrite` is false, or a
      directory cannot be written into.
    OSError: a directory cannot be written, the environment refused.
  """
  parents: set[Path] = set()
  resolved_paths: list[Path] = []
  for path in paths:
    path = resolve_path(path)
    if path.is_dir():
      raise Genanki2Error(f'Cannot write to disk: "{path}" is a directory.')
    if path.exists() and not overwrite:
      raise Genanki2Error(f'Cannot write to disk: "{path}" already exists.')
    if (parent := path.parent) not in parents:
      prepare_writing_dir(parent)
      parents.add(parent)
    resolved_paths.append(path)
  return resolved_paths


@contextlib.contextmanager
def atomic_replace(
  path: str | Path, overwrite: bool = True, mode: int = 0o644
) -> Generator[Path, None, None]:
  """Return a temporary file path for writing and move it onto `path` on exit.

  This is a context manager and can be used in a `with` statement. It returns a
  path to a temporary file in the destination's directory. At the end of the block,
  the temporary replaces or creates the file at `path`.

  If a file at `path` exists, it isn't touched until the block ends. A block that
  raises leaves the existing file unchanged and the temporary file is deleted.

  Args:
    path: The file to replace, created if it is not there.
    overwrite: Whether replacing an existing file is accepted. Default: `True`.
    mode: The permissions the destination ends up with. A temporary file is created
      readable by its owner alone. Default: `0o644`.

  Yields:
    The temporary file path for writing, in the destination's directory. It is
    automatically removed if it still is there when the block ends.

  Raises:
    Genanki2Error: `path` is a directory, the file exists and `overwrite` is false, or
      its directory cannot be written into.
    OSError: the temporary file cannot be created, or the move failed.
  """
  path = prepare_writing_file(path, overwrite)
  handle, temp_path = tempfile.mkstemp(
    dir=path.parent, prefix=f"{path.name}.", suffix=".part"
  )
  os.close(handle)
  temp_path = Path(temp_path)
  try:
    yield temp_path
    temp_path.chmod(mode)
    os.replace(temp_path, path)
  finally:
    temp_path.unlink(missing_ok=True)


def open_apkg(path: str | Path) -> zipfile.ZipFile:
  """Open the package at `path` for reading and return it open.

  The caller owns the zip and closes it.

  Raises:
    PackageReadError: two zip entries share a name, so reading either is ambiguous.
      (The zipfile is also closed).
    PackageReadError: the file is not a zip.
    Genanki2Error: `path` is missing or is not a file.
    OSError: the file cannot be opened.
  """
  path = prepare_reading_file(path)
  try:
    apkg_file: zipfile.ZipFile = zipfile.ZipFile(path, "r")
  except zipfile.BadZipFile as error:
    msg: str = f'Cannot read the package ("{path}"): invalid zip file. Error: {error}'
    raise PackageReadError(msg) from error
  if duplicate_entries := duplicates(apkg_file.namelist()):
    apkg_file.close()
    raise PackageReadError(
      "Ambiguous package entries: the package carries more than one entry under a "
      f"name, so reading that name picks one of them arbitrarily. Names: "
      f"{duplicate_entries}"
    )
  return apkg_file


def zstd_decompress(data: bytes) -> bytes:
  """Decompress one zstd frame, whether or not its header records the original size.

  genanki2 compresses each entry in one call, which records the size in the frame
  header. Anki compresses as a stream, which leaves it out, and a
  `zstandard.decompress()` (used when Python <3.14) refuses to decompress such a frame
  because it cannot size its output in advance.

  Raises:
    ZstdError: `data` is not a zstd frame, or ends before the frame does.
  """
  if sys.version_info >= (3, 14):
    from compression import zstd  # pyright: ignore[reportUnreachable]

    return zstd.decompress(data)
  else:
    import zstandard as zstd

    decompressobj: zstd.ZstdDecompressionObj = zstd.ZstdDecompressor().decompressobj()
    result: bytes = decompressobj.decompress(data)
    if not decompressobj.eof:
      msg: str = "Truncated zstd frame: the data ends before the frame's end marker."
      raise zstd.ZstdError(msg)
    return result
