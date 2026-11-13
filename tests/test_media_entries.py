"""Does `MediaEntries` hold both envelopes' media maps and say where each file goes?

    uv run --with pytest pytest tests/test_media_entries.py

The two envelopes carry the same mapping in different shapes. The current one stores a
protobuf and puts a file under its position in the list; the legacy one stores a JSON
object whose keys are the zip entries, numbers its own entries, and may leave gaps. One
model holds both, which is why an entry from a legacy map carries the number it was
given and an entry from the current one does not.

A package is untrusted input, so the map is where a malformed or hostile name has to be
caught: the key has to be a number the zip could use, and the name has to be one file
rather than a path out of the directory it will be written into.

`extraction_entries()` then decides the name each file lands under. Anki repairs a name it
cannot store and rewrites the notes referencing it; genanki2 repairs the name but hands
the rewriting back to the caller, so every rename it performs has to be reported, by
`warn_renames_in_extraction_entries()`.
"""

import hashlib
import io
import json
import sys
import warnings
import zipfile
from collections.abc import Sequence

import pydantic as pyd
import pytest

from genanki2 import _proto
from genanki2.errors import Genanki2Error, Genanki2Warning, PackageReadError
from genanki2.models.media_entries import ExtractedEntry, MediaEntries
from genanki2.models.package_metadata import PackageMetadata

if sys.version_info >= (3, 14):
  from compression import zstd  # pyright: ignore[reportUnreachable]
else:
  import zstandard as zstd

MediaEntry = MediaEntries.MediaEntry


def legacy_map(mapping: dict[str, str]) -> MediaEntries:
  """The entries a legacy `media` entry holding `mapping` reads back as."""
  return MediaEntries.from_legacy_bytes(json.dumps(mapping).encode())


def positional(*names: str) -> MediaEntries:
  """Entries as the current envelope holds them, numbered by position alone."""
  return MediaEntries(
    entries=[MediaEntry(name=name, size=1, sha1=b"\x00" * 20) for name in names]
  )


# ------------------------------------ Reading a map -------------------------------------


def test_a_legacy_map_keeps_the_number_it_gave_each_entry():
  """The key is the zip entry, and it is the only way back to the file's bytes."""
  entries = legacy_map({"5": "b.mp3", "2": "a.mp3"}).entries

  assert [(entry.legacy_zip_filename, entry.name) for entry in entries] == [
    (2, "a.mp3"),
    (5, "b.mp3"),
  ]


def test_a_legacy_map_carries_no_size_or_checksum():
  """Both fields exist for the current envelope, which stores them so an import can
  skip a file the target already has. A legacy map holds names only."""
  entry = legacy_map({"0": "a.mp3"}).entries[0]

  assert (entry.size, entry.sha1) == (0, b"")


def test_a_package_with_no_media_entry_reads_as_an_empty_map():
  """Old AnkiDroid exports omit the entry, and Anki reads the absence as `{}`."""
  assert MediaEntries.from_legacy_bytes(b"{}").entries == []


@pytest.mark.parametrize(
  ("data", "reason"),
  [
    (b"not json at all", "does not parse as JSON"),
    (b"[]", "not as an object"),
    (b'{"0": 3}', "malformed"),
  ],
)
def test_a_map_that_is_not_an_object_of_names_is_refused(data: bytes, reason: str):
  with pytest.raises(PackageReadError, match=reason):
    _ = MediaEntries.from_legacy_bytes(data)


def test_a_map_the_model_rejects_reaches_the_caller_as_a_validation_error():
  """The entry parses, so the package is intact and the fault is in a value. Here one
  entry claims a legacy zip number and the other does not, which the model refuses."""
  message = _proto.MediaEntries(
    entries=[
      _proto.MediaEntry(name="a.mp3", size=1, sha1=b"\x00" * 20),
      _proto.MediaEntry(name="b.mp3", size=1, sha1=b"\x00" * 20, legacy_zip_filename=1),
    ]
  )
  buffer = io.BytesIO()
  with zipfile.ZipFile(buffer, "w") as zfile:
    zfile.writestr("media", zstd.compress(message.SerializeToString()))
  with (
    zipfile.ZipFile(buffer) as zfile,
    pytest.raises(pyd.ValidationError, match="legacy zip filename"),
  ):
    MediaEntries.from_zipfile(zfile, PackageMetadata.Version.LATEST)


@pytest.mark.parametrize("key", ["x", "", "1.0", "-1", "²", "1_0", str(2**32)])
def test_a_key_that_is_not_a_zip_entry_number_is_refused(key: str):
  """Anki parses the key as an unsigned integer. Python's `int()` is looser than that,
  taking surrounding space, a sign, underscores and non-ASCII digits, so a key like
  `1_0` would silently read entry 10."""
  with pytest.raises(PackageReadError, match="Keys that are not a decimal number"):
    _ = legacy_map({key: "a.mp3"})


@pytest.mark.parametrize("name", ["../../.bashrc", "/etc/passwd", "C:evil.exe", ".."])
def test_a_name_that_is_not_one_file_is_refused(name: str):
  """The name is joined to the directory the caller names, so it decides where a file
  is written. See tests/test_media_name_safety.py for the rule itself."""
  with pytest.raises(PackageReadError, match="Invalid file names"):
    _ = legacy_map({"0": name})


# --------------------------------- Which envelope it is ---------------------------------


def test_entries_from_a_legacy_map_know_they_are_legacy():
  assert legacy_map({"0": "a.mp3"}).are_legacy()
  assert not positional("a.mp3").are_legacy()
  assert not MediaEntries().are_legacy()


def test_entries_cannot_mix_the_two_envelopes():
  """One list, two numbering rules: a legacy entry numbered 0 and whatever sits at
  position 0 would both claim zip entry `"0"`."""
  with pytest.raises(pyd.ValidationError, match="legacy zip filename"):
    _ = MediaEntries(
      entries=[
        MediaEntry(name="a.mp3", size=1, sha1=b"\x00" * 20),
        MediaEntry(name="b.mp3", size=1, sha1=b"\x00" * 20, legacy_zip_filename=0),
      ]
    )


def test_a_list_mixed_after_construction_is_caught_by_the_check():
  """The validator sees a list being built, not one mutated in place afterwards, and
  `entries` is a plain list that any caller can append to."""
  entries = positional("a.mp3")
  entries.entries.append(
    MediaEntry(name="b.mp3", size=1, sha1=b"", legacy_zip_filename=0)
  )

  with pytest.raises(Genanki2Error, match="legacy zip filename"):
    _ = entries.check()


# ----------------------------------- Checksum lengths -----------------------------------


def test_a_latest_envelope_entry_carries_a_twenty_byte_digest():
  entries = MediaEntries(
    entries=[MediaEntry(name="a.mp3", size=3, sha1=hashlib.sha1(b"ID3").digest())]
  )

  assert entries.check() is entries


@pytest.mark.parametrize(
  "sha1", [b"", b"too short", b"\x00" * 21], ids=["empty", "short", "long"]
)
def test_a_digest_of_any_other_length_is_refused(sha1: bytes):
  """Anki reads the digest into a fixed 20 byte array and refuses the whole package
  when it does not fit, so an empty one is no better than a truncated one."""
  entries = MediaEntries(entries=[MediaEntry(name="a.mp3", size=3, sha1=sha1)])

  with pytest.raises(Genanki2Error, match="expected length"):
    _ = entries.check()


def test_a_legacy_entry_carries_no_digest_at_all():
  """The legacy map holds names only, so there is nothing to read one from."""
  entries = legacy_map({"0": "a.mp3"})

  assert entries.check() is entries


def test_a_legacy_entry_given_a_digest_is_refused():
  """Nothing reads it, so a digest there is a caller believing something untrue about
  which envelope the entries belong to."""
  entries = MediaEntries(
    entries=[MediaEntry(name="a.mp3", size=3, sha1=b"\x00" * 20, legacy_zip_filename=0)]
  )

  with pytest.raises(Genanki2Error, match="expected length"):
    _ = entries.check()


def test_writing_the_blob_runs_the_check():
  """`ProtobufModel.to_bytes()` checks first, so the writer cannot emit a media map
  Anki would refuse."""
  entries = MediaEntries(entries=[MediaEntry(name="a.mp3", size=3, sha1=b"")])

  with pytest.raises(Genanki2Error, match="expected length"):
    _ = entries.to_bytes()


# ------------------------------------ Where files go ------------------------------------


def disk_names(
  extraction_entries: Sequence[ExtractedEntry],
) -> list[tuple[str, str]]:
  """Each zip entry and the name its file is stored under, in the map's order."""
  return [(item.zip_index, item.disk_name) for item in extraction_entries]


def test_the_current_envelope_numbers_a_file_by_its_position():
  assert positional("a.mp3", "b.mp3").extraction_entries() == (
    ExtractedEntry("0", "a.mp3", "a.mp3"),
    ExtractedEntry("1", "b.mp3", "b.mp3"),
  )


def test_a_legacy_map_numbers_a_file_by_its_key_gaps_included():
  assert legacy_map({"5": "b.mp3", "2": "a.mp3"}).extraction_entries() == (
    ExtractedEntry("2", "a.mp3", "a.mp3"),
    ExtractedEntry("5", "b.mp3", "b.mp3"),
  )


def test_a_name_anki_would_not_store_is_repaired():
  """The file lands under a name the filesystem takes, and the entry keeps the old one,
  which is the name the notes reference."""
  assert legacy_map({"0": "a?b.mp3"}).extraction_entries() == (
    ExtractedEntry("0", "a?b.mp3", "ab.mp3"),
  )


def test_working_out_the_names_warns_about_nothing():
  """Only the reader knows whether a rename is applied for the caller, recommended or
  required, so the warnings are its own."""
  with warnings.catch_warnings():
    warnings.simplefilter("error")
    _ = legacy_map({"0": "a?b.mp3", "1": "a?b.mp3"}).extraction_entries()


def test_two_names_repairing_to_one_both_keep_a_file():
  """Anki writes both into its media folder and the second overwrites the first."""
  placed = legacy_map({"0": "a?b.mp3", "1": "a*b.mp3"}).extraction_entries()

  assert disk_names(placed) == [("0", "ab.mp3"), ("1", "ab-1.mp3")]


def test_a_name_needing_no_repair_keeps_it_when_an_earlier_one_repairs_to_it():
  """`replace_media_references()` applies the renames in order, so `a?b.mp3 -> ab.mp3`
  followed by `ab.mp3 -> ab-1.mp3` would move both sets of references to `ab-1.mp3`.
  No name on disk may also be the package name of another entry."""
  placed = legacy_map({"0": "a?b.mp3", "1": "ab.mp3"}).extraction_entries()

  assert disk_names(placed) == [("0", "ab-1.mp3"), ("1", "ab.mp3")]
  package_names = {item.package_name for item in placed}
  renamed_to = {
    item.disk_name for item in placed if item.disk_name != item.package_name
  }
  assert not package_names & renamed_to


def test_the_map_is_in_zip_entry_order_counted_as_numbers():
  """The repaired first entry is placed last, and sorting the keys as strings would put
  `"10"` before `"2"`."""
  names = ["a?b.mp3", *(f"{i}.mp3" for i in range(1, 11))]

  placed = positional(*names).extraction_entries()

  assert [item.zip_index for item in placed] == [str(i) for i in range(11)]


def test_a_name_already_taken_is_placed_beside_it():
  """The collection is copied into the same directory, so its own entry name is passed
  in and a media file claiming it does not overwrite it."""
  placed = legacy_map({"0": "collection.anki2"}).extraction_entries(
    taken={"collection.anki2"}
  )

  assert placed == (ExtractedEntry("0", "collection.anki2", "collection-1.anki2"),)


def test_the_caller_s_set_of_taken_names_is_left_alone():
  taken = {"collection.anki2"}

  _ = legacy_map({"0": "collection.anki2"}).extraction_entries(taken=taken)

  assert taken == {"collection.anki2"}


def test_names_differing_only_in_case_each_keep_a_file():
  """The macOS and Windows defaults ignore case, so the second file would land on the
  first. Names are compared casefolded on every platform, which keeps an extraction
  directory portable."""
  placed = positional("A.png", "a.png").extraction_entries()

  assert disk_names(placed) == [("0", "A.png"), ("1", "a-1.png")]


def test_a_name_keeps_its_case():
  """Only the comparison folds case. The file is stored under the name the notes
  reference, and `ß` stays `ß` although it folds to `ss`."""
  placed = positional("Photo.PNG", "Straße.mp3").extraction_entries()

  assert disk_names(placed) == [("0", "Photo.PNG"), ("1", "Straße.mp3")]


def test_a_name_taken_in_another_case_is_placed_beside_it():
  placed = legacy_map({"0": "COLLECTION.anki2"}).extraction_entries(
    taken={"collection.anki2"}
  )

  assert placed == (ExtractedEntry("0", "COLLECTION.anki2", "COLLECTION-1.anki2"),)


def test_a_name_that_repairs_to_nothing_is_refused():
  """There is no name left to store the file under, so it cannot be extracted at all."""
  with pytest.raises(PackageReadError, match="normalizes to nothing"):
    _ = legacy_map({"0": "\x07"}).extraction_entries()


# --------------------------------- Checking the zip -------------------------------------


def test_a_map_whose_entries_the_zip_carries_passes():
  entries = legacy_map({"0": "a.mp3", "3": "b.mp3"})

  assert entries.check_zip_carries_entries({"0", "3", "media"}) is entries


def test_every_entry_the_zip_does_not_carry_is_named():
  entries = legacy_map({"0": "a.mp3", "3": "b.mp3", "7": "c.mp3"})

  with pytest.raises(PackageReadError, match="does not carry") as error:
    _ = entries.check_zip_carries_entries({"0", "media"})

  assert "'b.mp3'" in str(error.value)
  assert "'c.mp3'" in str(error.value)
  assert "'a.mp3'" not in str(error.value)


# ------------------------------------ Warnings ------------------------------------------


def test_one_name_on_two_entries_is_reported_as_ambiguous():
  """Both files are kept, but a reference names one of them, and nothing says which."""
  with pytest.warns(Genanki2Warning, match="duplicate filenames"):
    _ = legacy_map({"0": "ab.mp3", "1": "ab.mp3"}).warn_duplicate_names()


def test_distinct_names_repairing_to_one_are_not_ambiguous():
  """The repair is keyed by the name as written, and these differ, so each reference
  finds its own file."""
  with warnings.catch_warnings():
    warnings.simplefilter("error")
    _ = legacy_map({"0": "a?b.mp3", "1": "a*b.mp3"}).warn_duplicate_names()


def test_a_rename_is_reported_with_the_mapping_to_apply():
  """The notes still reference the old name, so the rename has to reach the caller."""
  placed = legacy_map({"0": "a?b.mp3", "1": "a.mp3"}).extraction_entries()

  with pytest.warns(Genanki2Warning, match="would not store") as record:
    MediaEntries.warn_renames_in_extraction_entries(placed)

  message = str(record[0].message)
  assert "'a?b.mp3': 'ab.mp3'" in message
  assert "'a.mp3'" not in message


def test_a_name_on_two_entries_is_left_out_of_the_renames():
  """Its second copy is suffixed, and renaming the name would move every reference to
  that copy. The rename of the other name is still reported."""
  placed = legacy_map({"0": "a.mp3", "1": "a.mp3", "2": "a?b.mp3"}).extraction_entries()

  with pytest.warns(Genanki2Warning, match="would not store") as record:
    MediaEntries.warn_renames_in_extraction_entries(placed)

  message = str(record[0].message)
  assert "'a?b.mp3': 'ab.mp3'" in message
  assert "a-1.mp3" not in message


def test_only_duplicated_names_renamed_warns_about_nothing():
  placed = legacy_map({"0": "a.mp3", "1": "a.mp3"}).extraction_entries()

  with warnings.catch_warnings():
    warnings.simplefilter("error")
    MediaEntries.warn_renames_in_extraction_entries(placed)


# --------------------------------- Describing a file ------------------------------------


def test_an_entry_measures_and_hashes_the_bytes_it_is_given():
  """The digest lets an import skip a file the target collection already holds, and
  Anki never recomputes it, so a wrong one silently keeps the old file."""
  entry = MediaEntry.from_file_bytes("audio.mp3", b"ID3")

  assert (entry.name, entry.size) == ("audio.mp3", 3)
  assert entry.sha1 == hashlib.sha1(b"ID3").digest()
  assert entry.legacy_zip_filename is None


def test_an_entry_read_from_a_path_stores_only_the_file_name(tmp_path):
  """Note fields reference a file by its name, so the directory it was read from must
  not reach the entry."""
  path = tmp_path / "recordings" / "audio.mp3"
  path.parent.mkdir()
  path.write_bytes(b"ID3")

  assert MediaEntry.from_path(path) == MediaEntry.from_file_bytes("audio.mp3", b"ID3")


def test_an_entry_read_from_a_tilde_path_reads_the_file_in_the_home_folder(
  tmp_path, monkeypatch
):
  home = tmp_path / "home"
  home.mkdir()
  (home / "audio.mp3").write_bytes(b"ID3")
  monkeypatch.setenv("HOME", str(home))
  monkeypatch.chdir(tmp_path)

  assert MediaEntry.from_path("~/audio.mp3") == MediaEntry.from_file_bytes(
    "audio.mp3", b"ID3"
  )


def test_an_entry_read_through_a_symlink_takes_the_links_name(tmp_path):
  """A note references the link's name, so that is the name the entry has to carry,
  with the bytes of the file it points at."""
  target = tmp_path / "cache" / "0a1b2c.mp3"
  target.parent.mkdir()
  target.write_bytes(b"ID3")
  link = tmp_path / "audio.mp3"
  link.symlink_to(target)

  assert MediaEntry.from_path(link) == MediaEntry.from_file_bytes("audio.mp3", b"ID3")
