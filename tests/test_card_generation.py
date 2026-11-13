"""Which cards does a note get from its notetype?

    uv run --with pytest pytest tests/test_card_generation.py

Anki generates a template's card when the front renders for the note
(`renders_with_fields`, `rslib/src/template.rs:312-360`): the front must hold a plain
replacement of a name that is filled for the note, and the note must satisfy every
conditional around it. The filled names are the note's non-empty fields plus the
special names (`rslib/src/notetype/cardgen.rs:130-140`).

Every front below was added to a real collection on a Front/Back notetype, with a note
whose Front is filled and Back is empty, once untagged and once tagged. The expectations
are the cards Anki generated.

A cloze notetype has one template, and Anki numbers a cloze note's cards from the cloze
numbers in all of its fields instead (`new_cards_required_cloze`,
`rslib/src/notetype/cardgen.rs:160-183`): `c1` is card 0, and every number from 500 up
is card 499. Each cloze case below was added to Anki's stock Cloze notetype the same way.

The models carry no anki dependency and neither does this file.
"""

import pytest

from genanki2.builtins import BASIC_NOTETYPE, builtin_notetype
from genanki2.models.field import Field
from genanki2.models.note import Note
from genanki2.models.notetype import Notetype
from genanki2.models.template import Template

# front: (renders untagged, renders tagged)
FRONTS = {
  "{{Front}}": (True, True),
  "{{Back}}": (False, False),
  "{{#Front}}text{{/Front}}": (False, False),
  "{{#Back}}{{Front}}{{/Back}}": (False, False),
  "{{^Back}}{{Front}}{{/Back}}": (True, True),
  "{{^Front}}{{Back}}{{/Front}}": (False, False),
  "{{#Front}}{{^Back}}{{Front}}{{/Back}}{{/Front}}": (True, True),
  "{{^Front}}{{Front}}{{/Front}}": (False, False),
  "{{Back}}{{Front}}": (True, True),
  "{{text:Front}}": (True, True),
  "{{#c1}}{{Front}}{{/c1}}": (False, False),
  "{{Deck}}": (True, True),
  "{{FrontSide}}": (False, False),
  "{{Tags}}": (False, True),
  "{{#Tags}}{{Front}}{{/Tags}}": (False, True),
  "{{^Tags}}{{Front}}{{/Tags}}": (True, False),
}


def template(front: str, name: str = "Card 1") -> Template:
  return Template(name=name, config={"q_format": front, "a_format": "{{Back}}"})


def note(*fronts: str, tags: list[str]) -> Note:
  """A note with Front filled and Back empty, on a notetype with one template per
  front."""
  notetype = Notetype(
    name="Generating",
    fields=[Field(name="Front"), Field(name="Back")],
    templates=[
      template(front, name=f"Card {ordinal}")
      for ordinal, front in enumerate(fronts, start=1)
    ],
  )
  return Note(notetype=notetype, fields=["front", ""], tags=tags)


# ------------------------------------ One front -------------------------------------


@pytest.mark.parametrize("tagged", [False, True], ids=["untagged", "tagged"])
@pytest.mark.parametrize(("front", "renders"), FRONTS.items(), ids=list(FRONTS))
def test_a_front_renders_as_anki_generates_its_card(front, renders, tagged):
  filled_fields = note(front, tags=["tag"] if tagged else []).filled_fields
  assert template(front).is_front_renderable(filled_fields) is renders[tagged]


def test_text_alone_does_not_render():
  """Anki refuses such a template when the notetype is saved, so no collection can
  answer for it, but nothing in it can render."""
  assert not template("text").is_front_renderable({"Front"})


# -------------------------------------- A note --------------------------------------


@pytest.mark.parametrize("tagged", [False, True], ids=["untagged", "tagged"])
def test_a_note_gets_the_ordinals_of_the_fronts_that_render(tagged):
  fronts: list[str] = list(FRONTS)
  expected = tuple(
    ordinal for ordinal, front in enumerate(fronts) if FRONTS[front][tagged]
  )
  tags = ["tag"] if tagged else []
  assert note(*fronts, tags=tags).card_ordinals == expected


def test_a_note_no_front_renders_for_gets_no_ordinals():
  """Anki still gives such a note card 0, which is the caller's to add, so the result
  tells it apart from a note whose first front renders."""
  assert note("{{Back}}", "{{FrontSide}}", tags=[]).card_ordinals == ()


# ----------------------------------- A cloze note -----------------------------------

# Text, Back Extra: the card ordinals Anki generated
CLOZE_FIELDS = {
  "one cloze": (("{{c1::a}}", ""), (0,)),
  "a gap in the numbers": (("{{c1::a}} {{c3::b}}", ""), (0, 2)),
  "numbers out of order": (("{{c2::a}} {{c1::b}}", ""), (0, 1)),
  "the last ordinal": (("{{c500::a}}", ""), (499,)),
  "a cloze in every field": (("{{c1::a}}", "{{c2::b}}"), (0, 1)),
  "a cloze only outside the cloze field": (("", "{{c1::b}}"), (0,)),
}


def cloze_note(*fields: str) -> Note:
  return Note(notetype=builtin_notetype("Cloze (genanki2)"), fields=list(fields))


@pytest.mark.parametrize(
  ("fields", "ordinals"), CLOZE_FIELDS.values(), ids=list(CLOZE_FIELDS)
)
def test_a_cloze_note_gets_a_card_per_cloze_number(fields, ordinals):
  assert cloze_note(*fields).card_ordinals == ordinals


def test_numbers_past_the_last_ordinal_share_one_card():
  """Anki gives `c600` and `c700` a card 499 each, and "Check Database" then deletes
  one as a duplicate ordinal (`remove_duplicate_card_ordinals`,
  `rslib/src/dbcheck.rs:360`), so only one is generated here."""
  assert cloze_note("{{c600::a}} {{c700::b}}", "").card_ordinals == (499,)


def test_a_cloze_note_without_a_cloze_gets_no_ordinals():
  """Anki gives it card 0 regardless, which is the caller's to add."""
  assert cloze_note("a", "").card_ordinals == ()


def test_a_value_set_in_place_is_normalized_before_it_is_parsed():
  """Anki strips control characters on add, so a control character inside the marker
  does not stop it numbering a card."""
  note = cloze_note("", "")
  note.fields[0] = "{{c\x011::a}}"
  assert note.card_ordinals == (0,)


def test_cloze_markup_on_a_normal_notetype_numbers_no_card():
  """The front decides there, and `{{Front}}` renders, so the note gets card 0 only."""
  note = Note(notetype=BASIC_NOTETYPE, fields=["{{c2::a}}", "b"])
  assert note.card_ordinals == (0,)
