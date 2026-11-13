"""Does a note get the cards from genanki2 that Anki generates when it adds the note?

    uv run --with anki --with pytest pytest tests/test_card_ordinals_match_anki.py

`Note.card_ordinals` reimplements the card generation Anki runs on `add_note`
(`new_cards_required`, `rslib/src/notetype/cardgen.rs:95-183`): on a normal notetype a
card per template whose front renders for the note, on a cloze notetype a card per
cloze number in its fields. Each notetype below is mirrored into a real collection with
the same fields, fronts and kind, the same values are added to it, and the ordinals of
the cards Anki created are the expectation.

Two differences are by design. Anki gives a note that would get no card card 0, which
genanki2 leaves to the caller, so an empty result is compared as card 0. Anki also gives
every cloze number from 500 up its own card 499, a duplicate its "Check Database"
removes, so Anki's ordinals are compared without repeats.

anki is a test-only dependency, never a dependency of genanki2.
"""

import itertools
import random
from collections.abc import Callable, Sequence

import pytest
from anki.collection import Collection as AnkiCollection
from anki.models import MODEL_CLOZE

from genanki2.builtins import BUILTIN_NOTETYPES
from genanki2.models.field import Field
from genanki2.models.note import Note
from genanki2.models.notetype import Notetype
from genanki2.models.template import Template

# Field values chosen to be filled, empty while holding markup, filled by markup alone,
# and to carry cloze numbers below, at and past the last ordinal.
VALUES = [
  "",
  "x",
  " <br> ",
  "<p></p>",
  "{{c1::a}}",
  "{{c3::b}} {{c1::c}}",
  "{{c600::d}}",
]

TAGS = [[], ["tag"]]

# Every combination up to this many per notetype, a seeded sample beyond it.
MAX_COMBINATIONS = 1_000

CONDITIONALS_NOTETYPE = Notetype(
  name="Conditionals",
  fields=[Field(name="X"), Field(name="Y")],
  templates=[
    Template(name=name, config={"q_format": front, "a_format": "{{Y}}"})
    for name, front in [
      ("Plain", "{{Y}}"),
      ("Filled X", "{{#X}}{{Y}}{{/X}}"),
      ("Empty X", "{{^X}}{{Y}}{{/X}}"),
      ("Tags", "{{Tags}}"),
      ("Tags and filled X", "{{#X}}{{Tags}}{{/X}}"),
      ("Untagged", "{{^Tags}}{{X}}{{/Tags}}"),
    ]
  ],
)

NOTETYPES = [*BUILTIN_NOTETYPES, CONDITIONALS_NOTETYPE]

AnkiOrdinals = Callable[[Notetype, Sequence[str], list[str]], tuple[int, ...]]


@pytest.fixture(scope="module")
def anki_ordinals(tmp_path_factory):
  """The card ordinals Anki creates when it adds a note with these values and tags."""
  collection = AnkiCollection(str(tmp_path_factory.mktemp("oracle") / "oracle.anki2"))
  mirrored: dict[str, dict] = {}

  def mirror(notetype: Notetype) -> dict:
    if notetype.name not in mirrored:
      models = collection.models
      anki_notetype = models.new(notetype.name)
      if notetype.is_cloze():
        anki_notetype["type"] = MODEL_CLOZE
      for name in notetype.field_names:
        models.add_field(anki_notetype, models.new_field(name))
      for template in notetype.templates:
        anki_template = models.new_template(template.name)
        anki_template["qfmt"] = template.config.q_format
        anki_template["afmt"] = template.config.a_format
        models.add_template(anki_notetype, anki_template)
      models.add(anki_notetype)
      mirrored[notetype.name] = models.by_name(notetype.name)
    return mirrored[notetype.name]

  def ordinals(
    notetype: Notetype, values: Sequence[str], tags: list[str]
  ) -> tuple[int, ...]:
    note = collection.new_note(mirror(notetype))
    note.fields = list(values)
    note.tags = tags
    collection.add_note(note, 1)
    return tuple(sorted({card.ord for card in note.cards()}))

  yield ordinals
  collection.close()


def value_combinations(field_count: int) -> list[tuple[str, ...]]:
  combinations = list(itertools.product(VALUES, repeat=field_count))
  if len(combinations) <= MAX_COMBINATIONS:
    return combinations
  return random.Random(0).sample(combinations, MAX_COMBINATIONS)


def ordinals(
  notetype: Notetype, values: Sequence[str], tags: list[str]
) -> tuple[int, ...]:
  """genanki2's answer, with the card 0 Anki adds to a note that would get none."""
  note = Note(notetype=notetype, fields=list(values), tags=tags)
  return note.card_ordinals or (0,)


@pytest.mark.parametrize("notetype", NOTETYPES, ids=[nt.name for nt in NOTETYPES])
def test_a_note_gets_the_cards_anki_generates(
  notetype: Notetype, anki_ordinals: AnkiOrdinals
):
  disagreements = [
    (values, tags)
    for values in value_combinations(len(notetype.fields))
    for tags in TAGS
    if ordinals(notetype, values, tags) != anki_ordinals(notetype, values, tags)
  ]
  assert disagreements == []
