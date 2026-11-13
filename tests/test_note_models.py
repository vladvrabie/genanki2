"""What does `Note` do with field values Anki cannot lay out?

    uv run --with pytest pytest tests/test_note_models.py

The first field is what `sfld` and `csum` are derived from, and `csum` is the only thing
Anki's duplicate detection looks at. An empty one is a real checksum over an empty string,
shared by every other note in the same state, so it answers "duplicate" for notes that
have nothing in common. Anki treats this the same way, reporting it in the editor
(`NoteFieldsState::Empty`) and importing the note anyway, so it warns here rather than
raising.

The count is the other way the values can be wrong. Anki lays a note out against its
notetype by position, so a row holding more or fewer values than the notetype has
fields is one it cannot render. `from_row` warns and builds the note anyway, since
padding the missing values and dropping the extra ones both cost content and the
choice belongs to the caller, and `to_row` refuses to write it until the counts agree.
Reading or setting a value by field name refuses it too, since the name finds its value
by position.
"""

import re

import pytest

from genanki2 import checks_disabled
from genanki2.builtins import BASIC_NOTETYPE, CLOZE_NOTETYPE, builtin_notetype
from genanki2.errors import CollectionReadError, Genanki2Error
from genanki2.models.field import Field
from genanki2.models.note import Note
from genanki2.models.notetype import Notetype
from genanki2.models.template import Template

EMPTY_FIRST_FIELDS = [
  "",
  "   ",
  "<br>",  # markup only, so nothing survives the strip
]


# --------------------------------- Empty first field ----------------------------------


@pytest.mark.parametrize("field", EMPTY_FIRST_FIELDS)
def test_an_empty_first_field_warns(field):
  note = Note(notetype=BASIC_NOTETYPE, fields=[field, "back"])
  with pytest.warns(UserWarning, match="first field is empty"):
    note.to_row()


def test_a_first_field_with_text_does_not_warn(recwarn):
  note = Note(notetype=BASIC_NOTETYPE, fields=["<b>front</b>", "back"])
  note.to_row()
  assert recwarn.list == []


# ------------------------------------ Field count -------------------------------------


def a_note_row(*values: str) -> dict:
  """A `notes` row for the Basic notetype, holding `values` in the `flds` column."""
  row = Note(notetype=BASIC_NOTETYPE, fields=["front", "back"]).to_row()
  return row | {"flds": "\x1f".join(values)}


def test_a_row_holding_one_value_per_field_does_not_warn(recwarn):
  Note.from_row(a_note_row("front", "back"), BASIC_NOTETYPE)
  assert recwarn.list == []


@pytest.mark.parametrize("values", [("front",), ("front", "back", "extra")])
def test_a_row_holding_the_wrong_number_of_values_warns(values):
  with pytest.warns(UserWarning, match="does not match the number of fields"):
    Note.from_row(a_note_row(*values), BASIC_NOTETYPE)


def test_a_row_holding_the_wrong_number_of_values_is_read_and_not_written():
  """The read is what the caller asked for and the repair is theirs to pick, so the note
  is built and only the write refuses."""
  with pytest.warns(UserWarning):
    note = Note.from_row(a_note_row("front"), BASIC_NOTETYPE)
  assert note.fields == ["front"]
  with pytest.raises(ValueError, match="does not match the number of fields"):
    note.to_row()


# ----------------------------- Cloze in a normal notetype -----------------------------

# Each agrees with pylib's `Note.fields_check()` on a Basic note in anki 26.8.
CLOZE_MARKUP = [
  "{{c1::a}}",
  "{{c1,0::a}}",  # a list holding a number above 0
  "{{c1::}}",
  "x {{c0::a}} {{c1::b}}",
]
NOT_CLOZE_MARKUP = [
  "{{c0::a}}",
  "{{c1::a",  # never closed
  "{{c70000::a}}",  # above 65535
  "{{C1::a}}",
]


@pytest.mark.parametrize("value", CLOZE_MARKUP)
def test_cloze_markup_in_a_normal_note_warns_naming_the_field(value):
  note = Note(notetype=BASIC_NOTETYPE, fields=["front", value])
  with pytest.warns(UserWarning, match="cloze markup in Back, "):
    note.to_row()


@pytest.mark.parametrize("value", NOT_CLOZE_MARKUP)
def test_markup_anki_does_not_read_as_a_cloze_does_not_warn(value, recwarn):
  Note(notetype=BASIC_NOTETYPE, fields=["front", value]).to_row()
  assert recwarn.list == []


def test_cloze_markup_in_a_cloze_note_does_not_warn(recwarn):
  Note(notetype=CLOZE_NOTETYPE, fields=["{{c1::a}}", ""]).to_row()
  assert recwarn.list == []


# ---------------------------- Cloze outside a cloze field -----------------------------


def a_cloze_note(front: str, text: str, back_extra: str) -> Note:
  """A note of the stock cloze notetype, with `front` on its only template."""
  notetype = builtin_notetype("Cloze (genanki2)")
  notetype.templates[0].config.q_format = front
  return Note(notetype=notetype, fields=[text, back_extra])


# Each warns exactly when pylib's `Note.fields_check()` returns `FIELD_NOT_CLOZE` for the
# same front and values in anki 26.8.
@pytest.mark.parametrize(
  ("front", "text", "back_extra", "warns"),
  [
    ("{{cloze:Text}}", "{{c1::a}}", "", False),
    ("{{cloze:Text}}", "{{c1::a}}", "{{c2::b}}", True),
    ("{{cloze:Text}}", "x", "{{c1::b}}", True),
    # a reference inside a conditional still makes a cloze field
    ("{{#Back Extra}}{{cloze:Text}}{{/Back Extra}}", "{{c1::a}}", "", False),
    ("{{#Back Extra}}{{cloze:Text}}{{/Back Extra}}", "{{c1::a}}", "{{c2::b}}", True),
    ("{{cloze:Text}}{{cloze:Back Extra}}", "{{c1::a}}", "{{c2::b}}", False),
    ("{{cloze:Text}}{{cloze:Back Extra}}", "x", "{{c1::b}}", False),
  ],
)
def test_cloze_markup_outside_the_fronts_cloze_fields_warns(
  front, text, back_extra, warns, recwarn
):
  note = a_cloze_note(front, text, back_extra)
  if warns:
    with pytest.warns(UserWarning, match="cloze markup in Back Extra, "):
      note.to_row()
  else:
    note.to_row()
    assert recwarn.list == []


def test_a_cloze_reference_differing_in_case_still_makes_a_cloze_field(recwarn):
  """Anki finds the cloze fields case-insensitively (`get_field_ord`,
  `rslib/src/notetype/mod.rs:639`), so `fields_check()` accepts this note. The reference
  is the notetype's problem: the renderer looks fields up by exact name, and
  `Notetype.check()` rejects it, so only a write with checks off gets this far."""
  with checks_disabled():
    a_cloze_note("{{cloze:text}}", "{{c1::a}}", "").to_row()
  assert recwarn.list == []


# ------------------------------------- Notetype -------------------------------------


def test_a_row_belonging_to_another_notetype_is_rejected():
  """The notetype passed in replaces the row's `mid`, so accepting it would move the note
  onto a notetype whose fields it was never laid out against."""
  row = a_note_row("front", "back") | {"mid": BASIC_NOTETYPE.id + 1}
  with pytest.raises(CollectionReadError, match="notetype id"):
    Note.from_row(row, BASIC_NOTETYPE)


# ----------------------------------- Field access -----------------------------------


def test_a_value_is_read_and_set_by_field_name():
  note = Note(notetype=BASIC_NOTETYPE, fields=["front", "back"])
  assert note["Back"] == "back"
  note["Back"] = "new back"
  assert note.fields == ["front", "new back"]


@pytest.mark.parametrize("name", ["Missing", "front"])
def test_a_name_the_notetype_does_not_have_raises_key_error(name):
  """Names are matched case-sensitively, so a differently cased name is missing too."""
  note = Note(notetype=BASIC_NOTETYPE, fields=["front", "back"])
  message = re.escape(f"{BASIC_NOTETYPE.name!r} has no field {name!r}")
  with pytest.raises(KeyError, match=message):
    note[name]
  with pytest.raises(KeyError, match=message):
    note[name] = "value"


def test_a_note_holding_the_wrong_number_of_values_refuses_access_by_name():
  note = Note(notetype=BASIC_NOTETYPE, fields=["front"])
  with pytest.raises(Genanki2Error, match="does not match the number of fields"):
    note["Front"]
  with pytest.raises(Genanki2Error, match="does not match the number of fields"):
    note["Front"] = "value"


def test_in_tells_whether_the_notetype_has_a_field():
  """Pydantic's `__iter__` yields attribute pairs, so `in` would quietly be false for
  every name without `Note.__contains__`."""
  note = Note(notetype=BASIC_NOTETYPE, fields=["front", "back"])
  assert "Front" in note
  assert "front" not in note


# ------------------------------------ Filled names ------------------------------------

SPECIAL_NAMES_FILLED_FOR_EVERY_NOTE = {
  "Card",
  "CardFlag",
  "CardID",
  "Deck",
  "Subdeck",
  "Type",
}


def notetype_with_fields(*names: str) -> Notetype:
  return Notetype(
    name="Filled",
    fields=[Field(name=name) for name in names],
    templates=[
      Template(name="Card 1", config={"q_format": "{{Front}}", "a_format": "a"})
    ],
  )


def test_a_field_is_filled_unless_it_holds_only_whitespace_and_line_breaks():
  """A non-breaking space is content to Anki, unlike `<br>` and ASCII whitespace."""
  note = Note(notetype=BASIC_NOTETYPE, fields=[" <br> ", "\xa0"])
  assert "Front" not in note.filled_fields
  assert "Back" in note.filled_fields


@pytest.mark.parametrize(
  ("tags", "expected"),
  [
    ([], SPECIAL_NAMES_FILLED_FOR_EVERY_NOTE),
    (["tag"], SPECIAL_NAMES_FILLED_FOR_EVERY_NOTE | {"Tags"}),
  ],
  ids=["untagged", "tagged"],
)
def test_special_names_are_filled_except_frontside_and_tags_on_an_untagged_note(
  tags, expected
):
  """`FrontSide` is never added, and `Tags` only when the note has tags
  (rslib/src/notetype/cardgen.rs:130-140)."""
  note = Note(notetype=BASIC_NOTETYPE, fields=["", ""], tags=tags)
  assert note.filled_fields == expected


@pytest.mark.parametrize(
  ("field", "value", "tags", "filled"),
  [
    ("Deck", "", [], False),
    ("Tags", "", ["tag"], False),
    ("Tags", "value", [], True),
    ("FrontSide", "value", [], True),
  ],
  ids=["empty deck", "empty tags on a tagged note", "tags", "frontside"],
)
def test_a_field_named_like_a_special_name_is_filled_by_its_own_value(
  field, value, tags, filled
):
  """Anki skips a special name that a field of the notetype claims, whether or not the
  note's own value fills it."""
  note = Note(notetype=notetype_with_fields("Front", field), fields=["front", value])
  note.tags = tags
  assert (field in note.filled_fields) is filled


def test_a_value_set_in_place_is_normalized_before_it_is_tested():
  """Mutating the list runs no validator, and Anki strips control characters on add, so
  a value holding only one is empty by the time Anki tests it."""
  note = Note(notetype=BASIC_NOTETYPE, fields=["front", "back"])
  note.fields[1] = "\x1c"
  assert "Back" not in note.filled_fields


def test_a_field_with_no_value_is_not_filled():
  """Anki pairs values with fields by position and skips the ones that have no value."""
  note = Note(notetype=BASIC_NOTETYPE, fields=["front"])
  assert "Front" in note.filled_fields
  assert "Back" not in note.filled_fields
