"""Does `Package` refuse a media list it cannot pack?

    uv run --with pytest pytest tests/test_package.py

The media map is keyed by position and holds only the file's name, so the name is the
one part of a path that has to be right: it is what the note references, and what Anki
resolves on import. Two failures are worth catching before the zip exists. A name that
repeats means one file silently loses its entry, and a name Anki refuses to normalize
fails the whole import with `Corrupt` rather than dropping one file.

The same list also has to agree with the collection, in both directions. A reference no
file backs imports without a word and breaks when the card renders; a file nothing names
is dead weight in the zip. Three things hold a reference: a note's fields, a notetype's
styling, and both formats of every template. Nothing in the format ties any of them to
the map, since a reference is text inside a field or a format and the map is built from
the path list, so the cross-check is the only place they meet.

What the writer produces is asserted here as far as the media half goes: the map, the
numbered entries and the agreement between them. The collection member is the subject of
`test_apkg_roundtrip.py`, which has a real Anki to ask.
"""

import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

import pydantic as pyd
import pytest

from genanki2.builtins import (
  BASIC_NOTETYPE,
  DEFAULT_DECK,
  builtin_notetype,
)
from genanki2.errors import Genanki2Error
from genanki2.models.card import Card
from genanki2.models.collection import Collection
from genanki2.models.media_entries import MediaEntries
from genanki2.models.note import Note
from genanki2.models.notetype import Notetype
from genanki2.models.package import Package

if sys.version_info >= (3, 14):
  from compression import zstd  # pyright: ignore[reportUnreachable]
else:
  import zstandard as zstd

skip_if_root = pytest.mark.skipif(
  hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads unreadable files"
)
skip_on_windows = pytest.mark.skipif(
  os.name == "nt", reason="the name is illegal on this filesystem"
)


def media_file(
  directory: Path, name: str = "audio.mp3", content: bytes = b"ID3"
) -> str:
  """Create a real, readable file: the validator opens every path it is given."""
  directory.mkdir(parents=True, exist_ok=True)
  path = directory / name
  path.write_bytes(content)
  return str(path)


# ------------------------------- One name, one entry --------------------------------


def test_files_with_different_names_are_accepted(tmp_path):
  media = [media_file(tmp_path, "front.mp3"), media_file(tmp_path, "back.mp3")]
  assert Package(media=media).media == media


def test_one_name_in_two_folders_is_rejected(tmp_path):
  """The folder disappears in the map, so both files would claim the same entry."""
  media = [media_file(tmp_path / "it"), media_file(tmp_path / "en")]
  with pytest.raises(pyd.ValidationError):
    Package(media=media)


def test_the_same_file_twice_is_rejected(tmp_path):
  path = media_file(tmp_path)
  with pytest.raises(pyd.ValidationError):
    Package(media=[path, path])


def test_two_spellings_of_one_path_are_one_file(tmp_path, monkeypatch):
  """The map is keyed by file name, so `./audio.mp3` and the absolute path claim one
  entry."""
  path = media_file(tmp_path)
  monkeypatch.chdir(tmp_path)
  with pytest.raises(pyd.ValidationError):
    Package(media=["audio.mp3", path])


def test_a_symlink_is_its_own_entry_beside_its_target(tmp_path):
  """The name comes from the path as written and only the contents from the target, so
  a link is one more name for the same bytes rather than a duplicate of its target."""
  target = media_file(tmp_path / "cache", "a.mp3")
  link = tmp_path / "b.mp3"
  link.symlink_to(target)

  assert Package(media=[target, str(link)]).media == [target, str(link)]


# ---------------------------- Names the importer refuses ----------------------------


@skip_on_windows
def test_a_backslash_in_the_name_is_rejected(tmp_path):
  """One component here is two components on Windows, and Anki fails the import with
  `Corrupt` instead of skipping the file. The package would build and import on this
  machine, which is what makes the check worth having."""
  with pytest.raises(pyd.ValidationError):
    Package(media=[media_file(tmp_path, "verbo\\irregolare.mp3")])


def test_a_path_with_no_final_component_is_rejected():
  """A root path has an empty name, which cannot key the map."""
  with pytest.raises(pyd.ValidationError):
    Package(media=["/"])


def test_the_working_directory_has_no_name_of_its_own(tmp_path, monkeypatch):
  """`.` resolves to a folder with a name, but the name as written is empty, and the
  message names `.` rather than the folder the caller never typed."""
  monkeypatch.chdir(tmp_path)

  with pytest.raises(pyd.ValidationError, match=r"file name is empty\. Path: \.") as e:
    Package(media=["."])

  assert str(tmp_path) not in str(e.value)


@skip_on_windows
def test_a_name_to_normalize_is_listed_as_the_path_written(tmp_path):
  """The file to rename is the link, not its target, and `repr` shows the characters
  the normalization drops, which plain quotes would print invisibly."""
  target = media_file(tmp_path / "cache", "a.mp3")
  link = tmp_path / "a\tb.mp3"
  link.symlink_to(target)

  with pytest.raises(pyd.ValidationError) as e:
    Package(media=[str(link)])

  assert f"{str(link)!r} -> 'ab.mp3'" in str(e.value)
  assert target not in str(e.value)


# ------------------------------ Files that cannot be read ------------------------------

# The OS errors are re-raised as `ValueError`, so every way of failing this validator
# reaches the caller as one `ValidationError`. The original errno stays on `__cause__`.


def test_a_missing_file_is_rejected(tmp_path):
  with pytest.raises(pyd.ValidationError):
    Package(media=[str(tmp_path / "absent.mp3")])


def test_a_directory_is_not_a_media_file(tmp_path):
  with pytest.raises(pyd.ValidationError):
    Package(media=[str(tmp_path)])


@skip_if_root
def test_an_unreadable_file_is_rejected(tmp_path):
  """Readable at build time is not the same question as present at build time: the zip
  reads the bytes, so a file that only `stat`s is still a failed write."""
  path = Path(media_file(tmp_path))
  path.chmod(0o000)
  with pytest.raises(pyd.ValidationError):
    Package(media=[str(path)])


def test_a_broken_symlink_names_the_link_and_where_it_points(tmp_path):
  link = tmp_path / "audio.mp3"
  link.symlink_to(tmp_path / "gone.mp3")

  with pytest.raises(pyd.ValidationError) as e:
    Package(media=[str(link)])

  assert f"{str(link)!r} ({tmp_path / 'gone.mp3'})" in str(e.value)


def test_the_reason_a_file_cannot_be_read_is_not_lost(tmp_path):
  """`FileNotFoundError` and `PermissionError` are worth telling apart, and wrapping
  them keeps the distinction on the cause rather than in the message alone."""
  with pytest.raises(pyd.ValidationError) as failure:
    Package(media=[str(tmp_path / "absent.mp3")])
  assert isinstance(
    failure.value.errors()[0]["ctx"]["error"].__cause__, FileNotFoundError
  )


def test_replacing_the_list_is_checked_too(tmp_path):
  """Without `validate_assignment` a list is only checked when it is passed to the
  constructor, and a caller building the package field by field is never checked."""
  package = Package(media=[media_file(tmp_path)])
  with pytest.raises(pyd.ValidationError):
    package.media = [str(tmp_path / "absent.mp3")]
  assert package.media == [media_file(tmp_path)]


# ------------------------- Notes and media files, cross-checked -------------------------


def package_with(tmp_path, fields: list[str], names: tuple[str, ...] = ()) -> Package:
  """One Basic note holding `fields`, and one packed file per name in `names`."""
  return Package(
    collection=Collection(notes=[Note(notetype=BASIC_NOTETYPE, fields=fields)]),
    media=[media_file(tmp_path, name) for name in names],
  )


def test_a_reference_backed_by_a_packed_file_passes(tmp_path):
  package = package_with(
    tmp_path, ['<img src="front.jpg">', "[sound:back.mp3]"], ("front.jpg", "back.mp3")
  )
  assert package.check_media_references_are_present() is package


def test_a_sound_reference_with_no_file_is_rejected(tmp_path):
  package = package_with(tmp_path, ["ciao", "[sound:back.mp3]"], ("front.jpg",))
  with pytest.raises(ValueError, match="back.mp3"):
    package.check_media_references_are_present()


def test_an_image_reference_with_no_file_is_rejected(tmp_path):
  """Both syntaxes reach the check, so an image is as much a claim as a sound tag."""
  package = package_with(tmp_path, ['<img src="front.jpg">', "ciao"])
  with pytest.raises(ValueError, match="front.jpg"):
    package.check_media_references_are_present()


def test_the_failure_names_the_note_that_carries_the_reference(tmp_path):
  package = package_with(tmp_path, ["[sound:absent.mp3]", "ciao"])
  note_id = package.collection.notes[0].id
  with pytest.raises(ValueError, match=f"Note id {note_id} -> 'absent.mp3'"):
    package.check_media_references_are_present()


def test_a_note_only_its_card_names_is_checked_on_write(tmp_path):
  """The check reads `notes`, and a note a card names reaches that list only when the
  writer collects the references, which it does before it checks."""
  note = Note(notetype=BASIC_NOTETYPE, fields=["[sound:absent.mp3]", "ciao"])
  package = Package(collection=Collection(cards=[Card(note=note, deck=DEFAULT_DECK)]))
  with pytest.raises(ValueError, match="absent.mp3"):
    package.write_apkg(tmp_path / "deck.apkg")


@pytest.mark.parametrize(
  "reference",
  [
    "https://example.com/audio.mp3",
    "http://example.com/audio.mp3",
    "ftp://example.com/audio.mp3",
    "HTTPS://example.com/audio.mp3",  # Anki lowercases before testing the scheme
  ],
)
def test_a_remote_reference_claims_no_packed_file(tmp_path, reference):
  """Anki drops these before it looks for a file, so a hotlinked image is not missing."""
  package = package_with(tmp_path, [f'<img src="{reference}">', "ciao"])
  assert package.check_media_references_are_present() is package


def test_a_reference_that_needs_repair_names_no_packed_file(tmp_path):
  """`star*.mp3` cannot be a file name, so the file was packed as `star.mp3` and the
  note points at nothing. Anki resolves it no better, since its importer performs no
  repair on this envelope."""
  package = package_with(tmp_path, ["[sound:star*.mp3]", "ciao"], ("star.mp3",))
  with pytest.raises(ValueError, match=r"star\*.mp3"):
    package.check_media_references_are_present()


def notetype_with(
  css: str = "", q_format: str = "{{Front}}", a_format: str = ""
) -> Notetype:
  """A copy of the Basic notetype whose styling and only template are as given."""
  notetype = builtin_notetype(BASIC_NOTETYPE.name)
  notetype.config.css = css
  notetype.templates[0].config.q_format = q_format
  notetype.templates[0].config.a_format = a_format
  return notetype


def package_with_notetype(
  tmp_path, notetype: Notetype, names: tuple[str, ...] = ()
) -> Package:
  """The notetype, one note of it, and one packed file per name in `names`."""
  return Package(
    collection=Collection(
      notetypes=[notetype], notes=[Note(notetype=notetype, fields=["ciao", "hello"])]
    ),
    media=[media_file(tmp_path, name) for name in names],
  )


def test_a_css_import_backed_by_a_packed_file_passes(tmp_path):
  package = package_with_notetype(
    tmp_path, notetype_with(css="@import '_font.css'"), ("_font.css",)
  )
  assert package.check_media_references_are_present() is package


def test_a_css_import_with_no_file_is_rejected(tmp_path):
  """No note names a webfont, so the styling is the only place this file is asked for
  and the only place the omission can be caught."""
  notetype = notetype_with(css="@import '_font.css'")
  package = package_with_notetype(tmp_path, notetype)
  with pytest.raises(ValueError, match=f"Notetype id {notetype.id} -> '_font.css'"):
    package.check_media_references_are_present()


def test_a_template_reference_with_no_file_is_rejected(tmp_path):
  notetype = notetype_with(q_format='{{Front}}<img src="_logo.png">')
  package = package_with_notetype(tmp_path, notetype)
  with pytest.raises(ValueError, match="Template \"Card 1\" -> '_logo.png'"):
    package.check_media_references_are_present()


def test_both_template_formats_are_read(tmp_path):
  """Anki gathers the answer side too, and a file only the back names is as missing as
  one only the front names."""
  package = package_with_notetype(
    tmp_path, notetype_with(a_format='{{FrontSide}}<img src="_logo.png">')
  )
  with pytest.raises(ValueError, match="_logo.png"):
    package.check_media_references_are_present()


def test_a_file_no_underscore_names_is_not_a_notetype_reference(tmp_path):
  """A template can hold an ordinary name, and Anki never gathers it: the file belongs
  to whatever note field references it, or to nothing."""
  package = package_with_notetype(
    tmp_path, notetype_with(q_format='{{Front}}<img src="logo.png">')
  )
  assert package.check_media_references_are_present() is package


def test_a_file_only_the_styling_names_is_not_unused(tmp_path, recwarn):
  """It is exempt for its underscore rather than for being found, which is the same
  answer Anki reaches by never reporting an underscored file at all."""
  package = package_with_notetype(
    tmp_path, notetype_with(css="@import '_font.css'"), ("_font.css",)
  )
  package.warn_unused_media_files()
  assert recwarn.list == []


def test_a_packed_file_no_note_references_warns(tmp_path):
  package = package_with(tmp_path, ["ciao", "hello"], ("unused.mp3",))
  with pytest.warns(UserWarning, match="unused.mp3"):
    package.warn_unused_media_files()


def test_every_packed_file_referenced_warns_about_nothing(tmp_path, recwarn):
  package = package_with(
    tmp_path, ["[sound:a.mp3]", "[sound:b.mp3]"], ("a.mp3", "b.mp3")
  )
  assert package.warn_unused_media_files() is package
  assert recwarn.list == []


def test_an_underscored_file_is_never_unused(tmp_path, recwarn):
  """Anki's own media check never reports an underscored file, whatever references it,
  and its notetype gatherer collects underscored names only, so every file a stylesheet
  or a template can name is exempt before the report starts looking."""
  package = package_with(tmp_path, ["ciao", "hello"], ("_logo.png",))
  package.warn_unused_media_files()
  assert recwarn.list == []


def test_a_packed_latex_image_is_not_unused(tmp_path, recwarn):
  """No field names the file, but Anki renders it from the note's LaTeX. The field and
  the name are the second case of the Rust test in `rslib/src/latex.rs`."""
  name = "latex-060219fbf3ddb74306abddaf4504276ad793b029.png"
  package = package_with(tmp_path, ["[$]<b>hello</b>&nbsp; world[/$]", "hi"], (name,))
  package.warn_unused_media_files()
  assert recwarn.list == []


def test_every_unused_file_is_named_in_one_warning(tmp_path):
  """Sorted, so the text is the same on every run whatever order the set yields."""
  package = package_with(tmp_path, ["ciao", "hello"], ("b.mp3", "a.mp3"))
  with pytest.warns(UserWarning, match='"a.mp3", "b.mp3"'):
    package.warn_unused_media_files()


def test_both_directions_run_from_check(tmp_path):
  package = package_with(tmp_path, ["[sound:absent.mp3]", "ciao"], ("unused.mp3",))
  with pytest.raises(ValueError, match="absent.mp3"):
    package.check()


# ------------------------------- Media, as it is written -------------------------------


def package_with_media(tmp_path, names: tuple[str, ...]) -> Package:
  """A package whose one note references every packed file, so the write is clean.

  Each file holds different bytes, since the point of most of these is which entry a
  given file landed in.
  """
  fields = ["".join(f"[sound:{name}]" for name in names), "hello"]
  return Package(
    collection=Collection(
      notetypes=[BASIC_NOTETYPE], notes=[Note(notetype=BASIC_NOTETYPE, fields=fields)]
    ),
    media=[media_file(tmp_path, name, content=name.encode()) for name in names],
  )


def test_the_legacy_zip_holds_the_collection_the_map_and_one_entry_per_file(tmp_path):
  package = package_with_media(tmp_path, ("front.mp3", "back.mp3"))
  path = tmp_path / "deck.apkg"
  package._write_v18_legacy_1(path)
  with zipfile.ZipFile(path) as apkg:
    assert set(apkg.namelist()) == {"collection.anki2", "media", "0", "1"}


def test_the_legacy_map_names_every_file_in_list_order(tmp_path):
  """The name lives only in the map, so this is the only thing tying entry `"0"` to the
  name a note field references."""
  package = package_with_media(tmp_path, ("front.mp3", "back.mp3"))
  path = tmp_path / "deck.apkg"
  package._write_v18_legacy_1(path)
  with zipfile.ZipFile(path) as apkg:
    assert json.loads(apkg.read("media")) == {"0": "front.mp3", "1": "back.mp3"}


def test_a_legacy_entry_holds_the_bytes_of_the_file_the_map_gives_it(tmp_path):
  """Index and name have to agree. Swapping two entries imports without complaint and
  plays the wrong audio on every card."""
  package = package_with_media(tmp_path, ("front.mp3", "back.mp3"))
  path = tmp_path / "deck.apkg"
  package._write_v18_legacy_1(path)
  with zipfile.ZipFile(path) as apkg:
    assert apkg.read("0") == b"front.mp3"
    assert apkg.read("1") == b"back.mp3"


def test_the_zip_holds_one_numbered_entry_per_file(tmp_path):
  package = package_with_media(tmp_path, ("front.mp3", "back.mp3"))
  path = tmp_path / "deck.apkg"
  package.write_apkg(path)
  with zipfile.ZipFile(path) as apkg:
    assert set(apkg.namelist()) == {
      "meta",
      "collection.anki21b",
      "collection.anki2",
      "media",
      "0",
      "1",
    }


def test_a_numbered_entry_holds_the_compressed_bytes_of_its_file(tmp_path):
  package = package_with_media(tmp_path, ("front.mp3", "back.mp3"))
  path = tmp_path / "deck.apkg"
  package.write_apkg(path)
  with zipfile.ZipFile(path) as apkg:
    assert zstd.decompress(apkg.read("1")) == b"back.mp3"


def test_every_entry_describes_the_file_it_stands_for(tmp_path):
  """The importer trusts the size and the digest rather than measuring the file: a
  matching sha1 in the target skips the copy, so a wrong one keeps the old file."""
  package = package_with_media(tmp_path, ("front.mp3", "back.mp3"))
  path = tmp_path / "deck.apkg"
  package.write_apkg(path)
  with zipfile.ZipFile(path) as apkg:
    entries = MediaEntries.from_bytes(zstd.decompress(apkg.read("media"))).entries
  assert [entry.name for entry in entries] == ["front.mp3", "back.mp3"]
  assert [entry.size for entry in entries] == [len(b"front.mp3"), len(b"back.mp3")]
  assert [entry.sha1 for entry in entries] == [
    hashlib.sha1(b"front.mp3").digest(),
    hashlib.sha1(b"back.mp3").digest(),
  ]


def test_a_writer_never_sets_the_legacy_zip_filename(tmp_path):
  """It records the entry a file came from in a legacy map, which may number its entries
  with gaps. A map genanki2 writes never has one."""
  package = package_with_media(tmp_path, ("front.mp3",))
  path = tmp_path / "deck.apkg"
  package.write_apkg(path)
  with zipfile.ZipFile(path) as apkg:
    entries = MediaEntries.from_bytes(zstd.decompress(apkg.read("media"))).entries
  assert entries[0].legacy_zip_filename is None


def test_a_symlink_is_packed_under_its_own_name_with_its_targets_bytes(tmp_path):
  """The note references the link's name, so that is the name the map has to carry.
  Packing it under the target's name left the note's sound missing in Anki."""
  target = media_file(tmp_path / "cache", "0a1b2c.mp3", content=b"ID3-ciao")
  link = tmp_path / "ciao.mp3"
  link.symlink_to(target)
  package = package_with_media(tmp_path, ())
  package.collection.notes[0].fields[0] = "[sound:ciao.mp3]"
  package.media = [str(link)]
  path = tmp_path / "deck.apkg"

  package.write_apkg(path)

  with zipfile.ZipFile(path) as apkg:
    entries = MediaEntries.from_bytes(zstd.decompress(apkg.read("media"))).entries
    assert [entry.name for entry in entries] == ["ciao.mp3"]
    assert zstd.decompress(apkg.read("0")) == b"ID3-ciao"


def test_a_media_path_under_a_tilde_is_read_from_the_home_folder(tmp_path, monkeypatch):
  home = tmp_path / "home"
  media_file(home, "ciao.mp3", content=b"ID3-ciao")
  monkeypatch.setenv("HOME", str(home))
  monkeypatch.chdir(tmp_path)
  package = package_with_media(tmp_path, ())
  package.collection.notes[0].fields[0] = "[sound:ciao.mp3]"
  package.media = ["~/ciao.mp3"]
  path = tmp_path / "deck.apkg"

  package.write_apkg(path)

  with zipfile.ZipFile(path) as apkg:
    assert zstd.decompress(apkg.read("0")) == b"ID3-ciao"


def test_a_file_deleted_before_the_write_stops_it(tmp_path):
  """The paths are read when the package is built and again when it is written, because
  the file has to be there at the second moment, not the first. Without the second read
  the map gains an entry with no file behind it."""
  package = package_with_media(tmp_path, ("front.mp3",))
  Path(package.media[0]).unlink()
  path = tmp_path / "deck.apkg"
  with pytest.raises(Genanki2Error, match="cannot read the media file"):
    package.write_apkg(path)
  assert not path.exists()
