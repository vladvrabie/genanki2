#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["anki"]
# ///
"""The protobuf definitions Anki encodes its blob columns with.

    uv run scripts/dump_proto_descriptors.py

Reads the generated modules shipped by the official `anki` package and writes their
descriptors and every definition they import to src/genanki2/_proto/descriptors.binpb
as a `FileDescriptorSet`. genanki2 loads that file into a descriptor pool, so no
generated module has to be vendored.

Why vendoring one would not work:
A generated module registers its proto file in protobuf's process-wide default pool
under the file name and a second registration of the same file name is aliased to the
first, not refused. With `anki` installed as the test oracle, whichever import ran
first would win, and the vendored definitions would go unused with nothing reporting it.

Output is deterministic for a given Anki version. Re-running it after an Anki upgrade
can produce a diff: no diff means the definitions are unchanged, a diff is the list of
things genanki2 needs to update.
"""

import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

from anki import deck_config_pb2, decks_pb2, import_export_pb2, notetypes_pb2
from google.protobuf.descriptor import FileDescriptor
from google.protobuf.descriptor_pb2 import FileDescriptorSet
from google.protobuf.descriptor_pool import DescriptorPool

ROOTS: tuple[ModuleType, ...] = (
  deck_config_pb2,
  decks_pb2,
  import_export_pb2,
  notetypes_pb2,
)
"""The root modules to process."""

KEEP: dict[str, set[str]] = {
  "anki/generic.proto": {"UInt32"},  # Notetype.Field.ord is UInt32
  "anki/deck_config.proto": {"DeckConfig"},
  "anki/decks.proto": {"Deck"},  # Deck carries Common and KindContainer
  "anki/import_export.proto": {"MediaEntries", "PackageMetadata"},
  "anki/notetypes.proto": {"Notetype", "StockNotetype"},  # Notetype carries 3 configs
}
"""The messages genanki2 reads, per file. Nested messages come with their parent."""

NEEDS: dict[str, list[str]] = {"anki/notetypes.proto": ["anki/generic.proto"]}
"""The imports each kept file still makes, once the unkept messages are gone."""


def with_imports(
  descriptor: FileDescriptor, seen: set[str]
) -> Iterator[FileDescriptor]:
  """Yield `descriptor` and every file it imports. Imports first, `descriptor` last.

  A descriptor pool refuses a file whose imports it has not been given yet, so loading
  a dump is order dependent.

  Args:
    descriptor: File to walk, its dependencies are processed recursively.
    seen: Saves the names the files already yielded. It is updated in place, so one
      set carries across several roots and each file is emitted once.
  """
  if descriptor.name in seen:
    return
  seen.add(descriptor.name)
  for dependency in descriptor.dependencies:
    yield from with_imports(dependency, seen)
  yield descriptor


def trimmed(descriptor_set: FileDescriptorSet) -> FileDescriptorSet:
  """Return `descriptor_set` filtered by the messages named in `KEEP`.

  Services and messages `KEEP` does not name are dropped, and each file's import list
  is replaced with what `NEEDS` gives it.

  `NEEDS` records what a file uses, it does not control what it can reach. A pool
  resolves a type name against every file it has been given, so a file omitted from
  `NEEDS` still resolves. What has to be right is `KEEP`, which has to name every file
  a kept message refers to, or the pool cannot resolve the type at all.
  """
  out: FileDescriptorSet = FileDescriptorSet()
  for file in descriptor_set.file:
    if file.name not in KEEP:
      continue
    kept = out.file.add()
    kept.CopyFrom(file)
    # Remove all services; none needed
    del kept.service[:]
    # Save needed messages, remove all, then readd
    messages = [m for m in kept.message_type if m.name in KEEP[file.name]]
    del kept.message_type[:]
    for message in messages:
      kept.message_type.add().CopyFrom(message)
    # Replace dependencies, manually add only the necessary ones
    del kept.dependency[:]
    kept.dependency.extend(NEEDS.get(file.name, []))
  return out


def validate_descriptor_set(descriptor_set: FileDescriptorSet) -> None:
  """Check by loading `descriptor_set` in a pool.

  A pool refuses a type it cannot resolve, so loading the result is what proves KEEP
  holds every file the kept messages reach.
  """
  pool: DescriptorPool = DescriptorPool()
  for file_descriptor_proto in descriptor_set.file:
    pool.Add(file_descriptor_proto)


def main() -> int:
  """Dump every definition the roots reach and write `descriptors.binpb`."""
  descriptor_set: FileDescriptorSet = FileDescriptorSet()
  seen_descriptors: set[str] = set()
  for module in ROOTS:
    for file_descriptor in with_imports(module.DESCRIPTOR, seen_descriptors):
      file_descriptor.CopyToProto(descriptor_set.file.add())
  descriptor_set = trimmed(descriptor_set)
  # Validations
  validate_descriptor_set(descriptor_set)
  msg: str = "ROOTS is incomplete: a root module is missing from KEEP"
  assert {m.DESCRIPTOR.name for m in ROOTS} <= set(KEEP), msg
  msg = "KEEP needs revision: KEEP names a file no root reaches"
  assert set(KEEP) <= seen_descriptors, msg

  bincontent: bytes = descriptor_set.SerializeToString()

  project_path: Path = Path(__file__).resolve().parent.parent
  binfile: Path = project_path / "src" / "genanki2" / "_proto" / "descriptors.binpb"
  binfile.parent.mkdir(parents=True, exist_ok=True)
  changed: bool = not binfile.exists() or binfile.read_bytes() != bincontent
  binfile.write_bytes(bincontent)
  print(f"{'updated' if changed else 'unchanged'}: {binfile}")
  return 0


if __name__ == "__main__":
  sys.exit(main())
