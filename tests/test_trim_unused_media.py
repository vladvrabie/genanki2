"""Does `Package.trim_unused_media()` drop what Anki's exporter would leave out?

    uv run --with pytest pytest tests/test_trim_unused_media.py

Anki's exporter packs the files a note's fields reference, plus the names beginning with
`_` that a notetype's CSS or templates reference (`gather_media_names` in
`rslib/src/import_export/gather.rs` and `notetype/mod.rs`). Everything else stays behind,
an unreferenced `_` file included. The trim applies the same rule to `media` and to the
files kept from a source package, which is what a rebuild read from last release's file
holds.
"""

import warnings
from pathlib import Path

import pytest

from genanki2.builtins import builtin_notetype, default_deck
from genanki2.errors import Genanki2Error, Genanki2Warning
from genanki2.models.card import Card
from genanki2.models.collection import Collection
from genanki2.models.config import ConfigEntry
from genanki2.models.note import Note
from genanki2.models.notetype import Notetype
from genanki2.models.package import Package
from genanki2.text import field_value_latex_image_names

# Keeps the writer's v1 scheduler warning out of the way of what these tests are about.
SCHEDULER_V2 = [ConfigEntry(key="schedVer", value=b"2")]


def a_notetype(css: str = "", front_extra: str = "") -> Notetype:
  notetype = builtin_notetype("Basic (genanki2)")
  notetype.config.css = css
  notetype.templates[0].config.q_format += front_extra
  return notetype


def a_collection(front: str = "", notetype: Notetype | None = None) -> Collection:
  """A collection reaching its note only through a card, so the trim has to collect."""
  note = Note(notetype=notetype or a_notetype(), fields=[front or "front", "back"])
  return Collection(config=SCHEDULER_V2, cards=[Card(note=note, deck=default_deck())])


def files(directory: Path, *names: str) -> list[str]:
  directory.mkdir(parents=True, exist_ok=True)
  for name in names:
    (directory / name).write_bytes(name.encode())
  return [str(directory / name) for name in names]


def names(package: Package) -> set[str]:
  return {Path(path).name for path in package.media}


def trimmed_quietly(package: Package) -> Package:
  with warnings.catch_warnings():
    warnings.simplefilter("ignore", Genanki2Warning)
    return package.trim_unused_media()


# ------------------------------------- media --------------------------------------


def test_a_file_a_note_references_is_kept(tmp_path):
  front = '<img src="used.png">[sound:used.mp3]'
  media = files(tmp_path, "used.png", "used.mp3")
  package = trimmed_quietly(Package(collection=a_collection(front), media=media))
  assert names(package) == {"used.png", "used.mp3"}


def test_a_file_nothing_references_is_dropped(tmp_path):
  media = files(tmp_path, "used.png", "unused.png")
  package = Package(collection=a_collection('<img src="used.png">'), media=media)
  assert names(trimmed_quietly(package)) == {"used.png"}


def test_an_underscored_file_the_css_references_is_kept(tmp_path):
  notetype = a_notetype(css='@import "_style.css";')
  media = files(tmp_path, "_style.css")
  package = Package(collection=a_collection(notetype=notetype), media=media)
  assert names(trimmed_quietly(package)) == {"_style.css"}


def test_an_underscored_file_a_template_references_is_kept(tmp_path):
  notetype = a_notetype(front_extra='<script src="_script.js"></script>')
  media = files(tmp_path, "_script.js")
  package = Package(collection=a_collection(notetype=notetype), media=media)
  assert names(trimmed_quietly(package)) == {"_script.js"}


def test_an_underscored_file_nothing_references_is_dropped(tmp_path):
  """Anki's exporter leaves it out too. Only Check Media exempts `_` names, because it
  reads note fields alone."""
  media = files(tmp_path, "_unused.js")
  package = Package(collection=a_collection(), media=media)
  assert names(trimmed_quietly(package)) == set()


def test_a_template_naming_a_file_without_an_underscore_does_not_keep_it(tmp_path):
  """A template reference counts only for a name beginning with `_`, Anki's rule."""
  notetype = a_notetype(front_extra='<img src="plain.png">')
  media = files(tmp_path, "plain.png")
  package = Package(collection=a_collection(notetype=notetype), media=media)
  assert names(trimmed_quietly(package)) == set()


def test_the_notes_and_notetypes_are_collected_first(tmp_path):
  media = files(tmp_path, "used.png")
  package = Package(collection=a_collection('<img src="used.png">'), media=media)
  trimmed_quietly(package)
  assert len(package.collection.notes) == 1
  assert len(package.collection.notetypes) == 1


def test_the_package_is_returned(tmp_path):
  package = Package(collection=a_collection(), media=files(tmp_path, "unused.png"))
  assert trimmed_quietly(package) is package


def test_a_package_without_media_is_left_alone():
  package = Package(collection=a_collection())
  with warnings.catch_warnings():
    warnings.simplefilter("error", Genanki2Warning)
    package.trim_unused_media()
  assert package.collection.notes == []


def test_two_paths_ending_in_the_same_name_are_refused(tmp_path):
  """The validator on `media` does not see an append, so the trim checks again."""
  package = Package(collection=a_collection(), media=files(tmp_path / "a", "x.png"))
  package.media.append(files(tmp_path / "b", "x.png")[0])
  with pytest.raises(Genanki2Error, match="Duplicates: \\('x.png',\\)"):
    package.trim_unused_media()


# `[$]<b>hello</b>&nbsp; world[/$]` from the Rust test in `rslib/src/latex.rs`.
LATEX_FIELD = "[$]<b>hello</b>&nbsp; world[/$]"
LATEX_IMAGE = "latex-060219fbf3ddb74306abddaf4504276ad793b029"


def test_the_image_a_note_s_latex_renders_is_kept(tmp_path):
  media = files(tmp_path, f"{LATEX_IMAGE}.png", "unused.png")
  package = Package(collection=a_collection(LATEX_FIELD), media=media)
  assert names(trimmed_quietly(package)) == {f"{LATEX_IMAGE}.png"}


def test_the_notetype_decides_which_latex_image_is_kept(tmp_path):
  """The same LaTeX names a `.svg` under `latex_svg`, so the `.png` is left over."""
  notetype = a_notetype()
  notetype.config.latex_svg = True
  media = files(tmp_path, f"{LATEX_IMAGE}.png", f"{LATEX_IMAGE}.svg")
  package = Package(collection=a_collection(LATEX_FIELD, notetype), media=media)
  assert names(trimmed_quietly(package)) == {f"{LATEX_IMAGE}.svg"}


def test_the_images_of_a_cloze_inside_latex_are_kept(tmp_path):
  """A cloze card renders the LaTeX with its cloze hidden on the front and revealed on
  the back. The exporter hashes the field as written and leaves both images behind;
  the trim keeps them, since the cards show them."""
  field = "[$]{{c1::x}}[/$]"
  hidden, shown, written = (
    field_value_latex_image_names(f"[$]{x}[/$]", False)[0]
    for x in ("[...]", "x", "{{c1::x}}")
  )
  media = files(tmp_path, hidden, shown, written, "unused.png")
  cloze = builtin_notetype("Cloze (genanki2)")
  package = Package(collection=a_collection(field, cloze), media=media)
  assert names(trimmed_quietly(package)) == {hidden, shown, written}


# ------------------------------------ warning -------------------------------------


def test_the_dropped_files_are_listed_in_a_warning(tmp_path):
  media = files(tmp_path, "used.png", "b.png", "a.png")
  package = Package(collection=a_collection('<img src="used.png">'), media=media)
  listing = "removed 2 media files .*:\n  'a.png'\n  'b.png'$"
  with pytest.warns(Genanki2Warning, match=listing):
    package.trim_unused_media()


def test_nothing_dropped_means_no_warning(tmp_path):
  media = files(tmp_path, "used.png")
  package = Package(collection=a_collection('<img src="used.png">'), media=media)
  with warnings.catch_warnings():
    warnings.simplefilter("error", Genanki2Warning)
    package.trim_unused_media()


# --------------------------------- source package ---------------------------------


@pytest.fixture
def source_package(tmp_path) -> Path:
  """A package whose note references `used.png` and carries `unused.png` as well."""
  media = files(tmp_path / "media", "used.png", "unused.png")
  package = Package(collection=a_collection('<img src="used.png">'), media=media)
  path = tmp_path / "source.apkg"
  with warnings.catch_warnings():
    warnings.simplefilter("ignore", Genanki2Warning)
    package.write_apkg(path)
  return path


def test_a_source_file_nothing_references_is_dropped(source_package):
  package = Package.read_apkg(source_package, keep_source_media=True)
  trimmed_quietly(package)
  assert package.media_filenames == {"used.png"}


def test_the_kept_source_entries_still_map_zip_entry_to_name(source_package):
  package = Package.read_apkg(source_package, keep_source_media=True)
  assert package._source_metadata is not None
  before = package._source_metadata.extraction_entries
  trimmed_quietly(package)
  assert package._source_metadata is not None
  after = package._source_metadata.extraction_entries
  assert after == tuple(item for item in before if item.disk_name == "used.png")


def test_the_source_path_and_envelope_are_kept(source_package):
  package = Package.read_apkg(source_package, keep_source_media=True)
  before = package._source_metadata
  trimmed_quietly(package)
  after = package._source_metadata
  assert before is not None and after is not None
  assert (after.path, after.version) == (before.path, before.version)


def test_a_trimmed_source_file_is_not_written_back(source_package, tmp_path):
  package = trimmed_quietly(Package.read_apkg(source_package, keep_source_media=True))
  written = tmp_path / "written.apkg"
  package.write_apkg(written)
  extracted = Package.read_apkg(written, extract_dir=tmp_path / "extracted")
  assert names(extracted) == {"used.png"}


def test_media_and_source_are_trimmed_together(source_package, tmp_path):
  package = Package.read_apkg(source_package, keep_source_media=True)
  package.media = files(tmp_path / "added", "added.png")
  with pytest.warns(Genanki2Warning, match="\n  'added.png'\n  'unused.png'$"):
    package.trim_unused_media()
  assert package.media_filenames == {"used.png"}
