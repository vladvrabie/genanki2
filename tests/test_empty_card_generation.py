"""Which fronts does `Notetype.warn_empty_card_generation` call dead?

    uv run --with pytest pytest tests/test_empty_card_generation.py

A front that generates no card is the one template mistake Anki never reports. The
notetype is accepted, the notes import, and the card is simply absent, so the deck looks
smaller than it should for no visible reason.

Anki decides by walking the front against the names that are non-empty for one note
(`template_is_empty`, `rslib/src/template.rs:324-360`). A plain replacement of a filled
name generates the card; a conditional only guards a body, and its own name never counts.
A body is walked only when its conditional is satisfied, so a replacement inside `{{#K}}`
on a never filled name, or inside `{{^K}}` on an always filled one, cannot generate the
card.
The non-empty set is the note's own fields plus the special names, except `FrontSide`,
which is never in it, and `Tags`, which is there only for a tagged note
(`rslib/src/notetype/cardgen.rs:130-140`). Every case below was run against a real
collection first, and the report agrees with what Anki generated.

The models carry no anki dependency and neither does this file.
"""

import pytest

from genanki2.builtins import builtin_notetype
from genanki2.models.field import Field
from genanki2.models.notetype import Notetype
from genanki2.models.template import Template

LIVE_FRONT = "{{Front}}"

DEAD_FRONTS = {
  "frontside": "{{FrontSide}}",
  "empty name": "{{}}",
  "conditional around text": "{{#Front}}text{{/Front}}",
  "conditional around text, then frontside": "{{#Front}}text{{/Front}}{{FrontSide}}",
  "conditional on a cloze number": "{{#c1}}{{Front}}{{/c1}}",
  "conditional on frontside": "{{#FrontSide}}{{Front}}{{/FrontSide}}",
  "negated conditional on an always filled name": "{{^Deck}}{{Front}}{{/Deck}}",
  "conditional inside its own negation": "{{^Back}}{{#Back}}{{Front}}{{/Back}}{{/Back}}",
  "replacement inside its own negation": "{{^Front}}{{Front}}{{/Front}}",
}

LIVE_FRONTS = {
  "plain replacement": LIVE_FRONT,
  "empty name beside a plain replacement": "{{}}{{Front}}",
  "special name that is always filled": "{{Deck}}",
  "conditional around a plain replacement": "{{#Back}}{{Front}}{{/Back}}",
  "negated conditional around a plain replacement": "{{^Back}}{{Front}}{{/Back}}",
  "conditional on its own replacement": "{{#Front}}{{Front}}{{/Front}}",
  "conditional on an always filled name": "{{#Deck}}{{Front}}{{/Deck}}",
  "negated conditional on tags": "{{^Tags}}{{Front}}{{/Tags}}",
}


def notetype(*fronts: str, fields: tuple[str, ...] = ("Front", "Back")) -> Notetype:
  """A notetype with one template per front, named after its position."""
  return Notetype(
    name="Reporting",
    fields=[Field(name=name) for name in fields],
    templates=[
      Template(
        name=f"Card {ordinal}",
        config={"q_format": front, "a_format": "{{FrontSide}}{{Back}}"},
      )
      for ordinal, front in enumerate(fronts, start=1)
    ],
  )


@pytest.mark.parametrize("front", DEAD_FRONTS.values(), ids=list(DEAD_FRONTS))
def test_a_dead_front_warns(front: str):
  """Beside a live template, so the card 0 exemption does not cover the dead one."""
  with pytest.warns(UserWarning, match="Card 1") as warnings:
    notetype(front, LIVE_FRONT).warn_empty_card_generation()
  assert len(warnings) == 1


@pytest.mark.parametrize("front", LIVE_FRONTS.values(), ids=list(LIVE_FRONTS))
def test_a_live_front_does_not_warn(front: str, recwarn):
  """Second, because a lone dead template is exempt and would pass this silently too."""
  notetype(LIVE_FRONT, front).warn_empty_card_generation()
  assert recwarn.list == []


@pytest.mark.parametrize(
  ("field", "front"),
  [
    ("FrontSide", "{{FrontSide}}"),
    ("Deck", "{{^Deck}}{{Front}}{{/Deck}}"),
    ("c1", "{{#c1}}{{Front}}{{/c1}}"),
  ],
  ids=["frontside", "deck", "cloze number"],
)
def test_a_field_claiming_a_special_name_makes_the_front_live(field, front, recwarn):
  """A special name is only special while no field of the notetype answers to it. The
  field can be empty or filled, so either conditional on it can be satisfied."""
  fields = (field, "Front")
  notetype(LIVE_FRONT, front, fields=fields).warn_empty_card_generation()
  assert recwarn.list == []


def test_the_first_template_is_exempt_when_every_front_is_dead():
  """Anki adds card 0 to a note that would otherwise get none, so that card does appear."""
  with pytest.warns(UserWarning, match="Card 2") as warnings:
    notetype("{{FrontSide}}", "{{}}").warn_empty_card_generation()
  assert len(warnings) == 1


def test_a_front_that_only_holds_tags_warns_about_untagged_notes():
  with pytest.warns(UserWarning, match="Tags"):
    notetype("{{Tags}}").warn_empty_card_generation()


def test_tags_inside_its_own_negation_is_dead_rather_than_tag_dependent():
  """Only an untagged note satisfies the conditional, and it renders `{{Tags}}` empty, so
  no note gets the card and the weaker Tags warning would misreport it."""
  with pytest.warns(UserWarning, match="no field references") as warnings:
    notetype("{{^Tags}}{{Tags}}{{/Tags}}", LIVE_FRONT).warn_empty_card_generation()
  assert len(warnings) == 1


def test_a_field_named_tags_is_an_ordinary_field(recwarn):
  notetype("{{Tags}}", fields=("Tags", "Back")).warn_empty_card_generation()
  assert recwarn.list == []


def test_a_cloze_notetype_is_skipped(recwarn):
  """Its cards come from the cloze numbers in the note's fields, not from the front."""
  cloze = builtin_notetype("Cloze (genanki2)")
  cloze.templates.append(
    Template(
      name="Card 2", config={"q_format": "{{FrontSide}}", "a_format": "{{Text}}"}
    )
  )
  cloze.warn_empty_card_generation()
  assert recwarn.list == []


def test_a_notetype_without_templates_is_skipped(recwarn):
  notetype().warn_empty_card_generation()
  assert recwarn.list == []
