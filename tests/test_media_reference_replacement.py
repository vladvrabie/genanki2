"""Does renaming a media file rewrite the references to it and nothing else?

    uv run --with anki --with pytest pytest tests/test_media_reference_replacement.py

`Package.check_media_paths()` names the file a caller has to rename, and renaming it
leaves every note that referenced the old name pointing at nothing. This is the other
half: the string rewrite that no caller can do from outside, because a reference is
matched on its decoded name and written back in the encoding the field already used.

The rename comes from the caller, so nothing here derives `new` from `old`.

Anki repairs an unnormalized media name on the legacy import path and rewrites the notes
that referenced it, so the last tests are differential against the real importer rather
than a transcription of what it is believed to do.

anki is a test-only dependency, never a dependency of genanki2.
"""

from pathlib import Path

import pytest
from anki.collection import Collection as AnkiCollection
from anki.collection import ImportAnkiPackageRequest

from genanki2.builtins import (
  BASIC_NOTETYPE,
  DEFAULT_DECK_CONFIG,
  builtin_notetype,
  default_config_entries,
)
from genanki2.models.card import Card
from genanki2.models.collection import Collection
from genanki2.models.deck import Deck
from genanki2.models.note import Note
from genanki2.models.package import Package
from genanki2.text import (
  field_value_media_refs,
  normalize_media_name,
  replace_field_value_media_refs,
)

RENAMES = [
  # Every spelling of the attribute the media pattern accepts.
  ('<img src="a.jpg">', "a.jpg", "z.jpg", '<img src="z.jpg">'),
  ("<img src='a.jpg'>", "a.jpg", "z.jpg", "<img src='z.jpg'>"),
  ("<img src=a.jpg>", "a.jpg", "z.jpg", "<img src=z.jpg>"),
  ('<audio src="a.ogg">', "a.ogg", "z.ogg", '<audio src="z.ogg">'),
  ('<object data="o.swf">', "o.swf", "z.swf", '<object data="z.swf">'),
  ("[sound:a.mp3]", "a.mp3", "z.mp3", "[sound:z.mp3]"),
  # Both syntaxes in one field, and only the named file moves.
  (
    'a<img src="x.jpg">b[sound:y.mp3]',
    "x.jpg",
    "z.jpg",
    'a<img src="z.jpg">b[sound:y.mp3]',
  ),
  (
    '<img src="a.jpg"><img src="b.jpg">',
    "b.jpg",
    "z.jpg",
    '<img src="a.jpg"><img src="z.jpg">',
  ),
  # Every reference to the file moves, not just the first.
  ("[sound:a.mp3] e [sound:a.mp3]", "a.mp3", "z.mp3", "[sound:z.mp3] e [sound:z.mp3]"),
  # Prose naming the file is not a reference.
  (
    'see a.jpg here <img src="a.jpg">',
    "a.jpg",
    "z.jpg",
    'see a.jpg here <img src="z.jpg">',
  ),
  # A tag the media pattern does not recognize holds no reference.
  ('<div src="c.jpg">', "c.jpg", "z.jpg", '<div src="c.jpg">'),
  # A field with nothing to rename comes back as it went in.
  ("[sound:a.mp3]", "b.mp3", "z.mp3", "[sound:a.mp3]"),
  ("ciao", "a.jpg", "z.jpg", "ciao"),
]

ENCODINGS = [
  # An escaped reference is matched on its decoded name and written back escaped.
  ('<img src="a&amp;b.jpg">', "a&b.jpg", "c&d.jpg", '<img src="c&amp;d.jpg">'),
  ("[sound:s&amp;t.mp3]", "s&t.mp3", "u&v.mp3", "[sound:u&amp;v.mp3]"),
  # A raw reference stays raw, so the field keeps the spelling it had.
  ('<img src="a.jpg">', "a.jpg", "c&d.jpg", '<img src="c&d.jpg">'),
  ("[sound:a.mp3]", "a.mp3", "u&v.mp3", "[sound:u&v.mp3]"),
  # An ampersand with no closing semicolon is not an entity, so nothing decodes.
  ('<img src="a&b.jpg">', "a&b.jpg", "z.jpg", '<img src="z.jpg">'),
]

REMOTE = '<img src="http://example.com/a.jpg">'


@pytest.mark.parametrize(("field", "old", "new", "renamed"), RENAMES + ENCODINGS)
def test_a_reference_is_renamed_where_it_stands(
  field: str, old: str, new: str, renamed: str
):
  assert replace_field_value_media_refs(field, old, new) == renamed


@pytest.mark.parametrize(("field", "old", "new", "renamed"), RENAMES + ENCODINGS)
def test_renaming_twice_changes_nothing_the_second_time(
  field: str, old: str, new: str, renamed: str
):
  """`old` no longer appears once the rename has run, whatever `new` is spelled like."""
  once: str = replace_field_value_media_refs(field, old, new)
  assert replace_field_value_media_refs(once, old, new) == renamed


@pytest.mark.parametrize(("field", "old", "new", "renamed"), RENAMES + ENCODINGS)
def test_the_renamed_field_reports_the_new_name(
  field: str, old: str, new: str, renamed: str
):
  """The rewrite is only worth anything if the extractor agrees with it afterwards."""
  before = field_value_media_refs(field)
  after = field_value_media_refs(renamed)
  assert after == [new if ref == old else ref for ref in before]


def test_a_remote_reference_is_left_alone_when_only_the_filename_is_given():
  """A URL is captured whole, so the file name at its end is not the reference."""
  assert replace_field_value_media_refs(REMOTE, "a.jpg", "z.jpg") == REMOTE


def test_a_remote_reference_is_renamed_when_the_caller_names_the_url():
  """Nothing here decides what a reference means, so naming the URL renames it."""
  assert (
    replace_field_value_media_refs(REMOTE, "http://example.com/a.jpg", "z.jpg")
    == '<img src="z.jpg">'
  )


def test_the_whole_tag_is_searched_not_just_the_attribute():
  """Anki substitutes the name across the matched tag, so a second copy of it moves too.

  Pinned because it is a consequence of following Anki rather than a decision here.
  """
  assert (
    replace_field_value_media_refs('<img src="a.jpg" alt="a.jpg">', "a.jpg", "z.jpg")
    == '<img src="z.jpg" alt="z.jpg">'
  )


def test_a_name_spelled_like_an_entity_does_not_survive_a_raw_reference():
  """The escape follows the old spelling, so it cannot protect every new name.

  A file named `a&amp;b.jpg` renamed into a reference that carried no entity is written
  raw, and reading the field back gives `a&b.jpg`, a name no file in the package
  carries. Anki's importer writes the same field. `normalize_media_name()` keeps every
  character involved, so such a name is legal and the caller is the one that has to
  avoid it.
  """
  renamed = replace_field_value_media_refs('<img src="x.jpg">', "x.jpg", "a&amp;b.jpg")
  assert renamed == '<img src="a&amp;b.jpg">'
  assert field_value_media_refs(renamed) == ["a&b.jpg"]


# --------------------------------------- A note ---------------------------------------


def a_note(*fields: str) -> Note:
  return Note(notetype=BASIC_NOTETYPE, fields=list(fields))


def test_a_note_renames_across_every_field():
  note = a_note('<img src="a.jpg">', "[sound:b.mp3]")
  note.replace_media_references({"a.jpg": "z.jpg", "b.mp3": "y.mp3"})
  assert note.fields == ['<img src="z.jpg">', "[sound:y.mp3]"]


def test_a_note_keeps_its_field_count():
  """One field comes out per field that went in, however many renames are asked for.

  A note carrying more fields than its notetype declares renders nothing, and the count
  is only checked on the way to a row, so a mistake here surfaces at write time.
  """
  note = a_note('<img src="a.jpg">', "[sound:b.mp3]")
  note.replace_media_references({"a.jpg": "z.jpg", "b.mp3": "y.mp3", "c.png": "x.png"})
  assert len(note.fields) == 2


def test_renames_compose_in_mapping_order():
  """Renaming A to B and then B to C leaves the fields that held either name on C."""
  note = a_note("[sound:a.mp3][sound:b.mp3]", "back")
  note.replace_media_references({"a.mp3": "b.mp3", "b.mp3": "c.mp3"})
  assert note.fields[0] == "[sound:c.mp3][sound:c.mp3]"


def test_a_field_replaced_through_the_list_is_normalized():
  """Assigning into the list runs no validator, so the method normalizes what it reads.

  The row is written from normalized text, so a search over the raw field would rename
  a reference the package never carries.
  """
  note = a_note("front", "back")
  note.fields[0] = "[sound:bell\x07.mp3]"
  note.replace_media_references({})
  assert note.fields == ["[sound:bell.mp3]", "back"]


def test_a_name_no_field_references_leaves_the_note_alone():
  note = a_note('<img src="a.jpg">', "b.mp3 is prose, not a reference")
  assert note.replace_media_references({"b.mp3": "z.mp3"}).fields == [
    '<img src="a.jpg">',
    "b.mp3 is prose, not a reference",
  ]


# ------------------------------------- A package --------------------------------------


def test_a_package_renames_across_every_note():
  collection = Collection()
  collection.notes.extend(
    [a_note("[sound:a.mp3]", "back"), a_note('<img src="a.mp3">', "back")]
  )
  Package(collection=collection).replace_media_references({"a.mp3": "z.mp3"})
  assert [note.fields[0] for note in collection.notes] == [
    "[sound:z.mp3]",
    '<img src="z.mp3">',
  ]


def test_a_note_only_a_card_names_is_repaired_too():
  """The reason the method exists: a hand-written loop over `notes` misses this one.

  `collect_notes()` runs first, so a note the caller appended a card for and forgot to
  append itself is repaired instead of shipping a reference to a file that is gone.
  """
  collection = Collection()
  deck = Deck(name="Media")
  collection.decks.append(deck)
  note = a_note("[sound:a.mp3]", "back")
  collection.cards.append(Card(note=note, deck=deck, template_ord=0, due=1))
  assert collection.notes == []

  Package(collection=collection).replace_media_references({"a.mp3": "z.mp3"})
  assert note.fields[0] == "[sound:z.mp3]"


def test_the_media_paths_are_left_to_the_caller(tmp_path):
  """The files are renamed outside genanki2, so `media` is the caller's to update."""
  media_path = tmp_path / "a.mp3"
  media_path.write_bytes(b"not really an mp3")
  package = Package(collection=Collection(), media=[str(media_path)])

  package.replace_media_references({"a.mp3": "z.mp3"})
  assert package.media == [str(media_path)]


def test_a_notetype_css_and_its_templates_are_repaired_too():
  """A file an underscored asset names is renamed on disk like any other.

  Anki rewrites neither site, so this is the only thing that can put a notetype back in
  agreement with a media folder someone renamed.
  """
  notetype = builtin_notetype(BASIC_NOTETYPE.name)
  notetype.config.css += '@import "_a*.css";'
  notetype.templates[0].config.q_format += '<img src="_b*.png">'

  collection = Collection(notetypes=[notetype])
  Package(collection=collection).replace_media_references(
    {"_a*.css": "_a.css", "_b*.png": "_b.png"}
  )
  assert notetype.underscored_css_imports == ("_a.css",)
  assert notetype.templates[0].underscored_references == ("_b.png",)


def test_a_name_without_an_underscore_is_repaired_in_fields_only():
  """The two notetype sites follow Anki's convention, and a note field does not.

  A caller renaming a stylesheet asset that does not begin with `_` gets nothing back
  and no error, because neither pattern can see such a name.
  """
  notetype = builtin_notetype(BASIC_NOTETYPE.name)
  notetype.config.css += '@import "a*.css";'
  notetype.templates[0].config.q_format += '<img src="b*.png">'
  css, q_format = notetype.config.css, notetype.templates[0].config.q_format

  note = Note(notetype=notetype, fields=['<img src="a*.css">', ""])
  collection = Collection(notes=[note], notetypes=[notetype])
  Package(collection=collection).replace_media_references(
    {"a*.css": "a.css", "b*.png": "b.png"}
  )
  assert note.fields[0] == '<img src="a.css">'
  assert notetype.config.css == css
  assert notetype.templates[0].config.q_format == q_format


# ------------------------------ Differential against Anki ------------------------------

UNNORMALIZED_MEDIA = ["star*.mp3", "a&b*.jpg"]
"""Two names Anki refuses to store: `*` is stripped, and one of them keeps an `&`."""

UNNORMALIZED_FIELDS = [
  "[sound:star*.mp3]",
  '<img src="a&amp;b*.jpg"><img src="a&b*.jpg">',
]
"""The second field references one file twice, once escaped and once raw."""


@pytest.fixture
def package_anki_has_to_repair(tmp_path):
  """A legacy package whose media names Anki normalizes as it imports.

  `check_media_paths()` rejects these names, so the package is built past its validators.
  The legacy envelope is the one whose importer repairs a name; the current envelope
  fails the import instead.
  """
  media_dir = tmp_path / "media"
  media_dir.mkdir()
  for name in UNNORMALIZED_MEDIA:
    (media_dir / name).write_bytes(name.encode())

  collection = Collection(
    config=default_config_entries(), deck_configs=[DEFAULT_DECK_CONFIG]
  )
  deck = Deck(name="Media")
  collection.decks.append(deck)
  note = a_note(*UNNORMALIZED_FIELDS)
  collection.notes.append(note)
  collection.cards.append(Card(note=note, deck=deck, template_ord=0, due=1))
  collection.collect_notetypes()

  path = tmp_path / "legacy.apkg"
  Package.model_construct(
    collection=collection,
    media=[str(media_dir / name) for name in UNNORMALIZED_MEDIA],
  )._write_v18_legacy_1(path)
  return path


@pytest.fixture
def imported(tmp_path, package_anki_has_to_repair):
  """The collection Anki produced out of that package."""
  collection = AnkiCollection(str(tmp_path / "target.anki2"))
  collection.import_anki_package(
    ImportAnkiPackageRequest(package_path=str(package_anki_has_to_repair))
  )
  yield collection
  collection.close()


def test_anki_stores_every_file_under_its_normalized_name(imported):
  """The premise of the differential test: the files really were renamed on import."""
  assert sorted(path.name for path in Path(imported.media.dir()).iterdir()) == [
    normalize_media_name(name) for name in sorted(UNNORMALIZED_MEDIA)
  ]


def test_a_repaired_field_matches_the_one_anki_writes(imported):
  """Anki rewrites the notes that referenced a renamed file, and this is that rewrite.

  The escaped and the raw reference to the same file are in one field, so the run pins
  the encoding rule as well as the rename: Anki writes each one back in the spelling it
  found, and so does `replace_field_value_media_refs()`.
  """
  ours = list(UNNORMALIZED_FIELDS)
  for name in UNNORMALIZED_MEDIA:
    ours = [
      replace_field_value_media_refs(field, name, normalize_media_name(name))
      for field in ours
    ]

  (note_id,) = imported.find_notes("")
  assert ours == imported.get_note(note_id).fields
