"""Do the functions in `genanki2.api` refuse the arguments that would build the wrong
model, and pass the rest through to it?

    uv run --with pytest pytest tests/test_api.py

A single string where a list belongs is iterable, so without a check it builds a field,
a template or a tag per character. Two arguments that set the same value would let one
win without a word.
"""

from typing import Any

import pytest

from genanki2.api import deck, deck_config, filtered_deck, note, notetype, template
from genanki2.builtins import BASIC_NOTETYPE
from genanki2.errors import Genanki2Error
from genanki2.models.deck import DeckFilteredKind, DeckNormalKind
from genanki2.models.field import Field
from genanki2.models.notetype import NotetypeConfig

TEMPLATE = ("Card 1", "{{Front}}", "{{Back}}")


@pytest.mark.parametrize(
  "fields, templates",
  [
    ("Front", [TEMPLATE]),
    (Field(name="Front"), [TEMPLATE]),
    (["Front", "Back"], TEMPLATE),
    (["Front", "Back"], template(*TEMPLATE)),
    (["Front", "Back"], [("Card 1", "{{Front}}")]),
  ],
  ids=["bare str field", "bare Field", "bare tuple", "bare Template", "short tuple"],
)
def test_notetype_refuses_a_single_item_where_a_list_belongs(
  fields: Any, templates: Any
):
  with pytest.raises(Genanki2Error, match="Wrap a single"):
    notetype("Mine", fields, templates)


def test_notetype_takes_templates_from_a_generator():
  """The template check reads the iterable before building, so it must not use it up."""
  built = notetype("Mine", ["Front", "Back"], (t for t in [TEMPLATE]))
  assert [t.name for t in built.templates] == ["Card 1"]


@pytest.mark.parametrize(
  "kwargs",
  [{"kind": NotetypeConfig.Kind.CLOZE}, {"sort_field": "Back", "sort_field_idx": 1}],
  ids=["kind", "sort_field twice"],
)
def test_notetype_refuses_two_ways_of_setting_one_value(kwargs: dict[str, Any]):
  with pytest.raises(Genanki2Error):
    notetype("Mine", ["Front", "Back"], [TEMPLATE], **kwargs)


@pytest.mark.parametrize(
  "kwargs", [{"fields": "front"}, {"fields": ["front", "back"], "tags": "verb"}]
)
def test_note_refuses_a_single_string_where_a_list_belongs(kwargs: dict[str, Any]):
  name = "tags" if "tags" in kwargs else "fields"
  with pytest.raises(Genanki2Error, match=f"`{name}`"):
    note(BASIC_NOTETYPE, **kwargs)


def test_deck_refuses_a_preset_and_a_preset_id():
  with pytest.raises(Genanki2Error):
    deck("Italian", deck_config("Mine"), config_id=5)


def test_deck_without_a_preset_object_names_it_by_id():
  built = deck("Italian", None, config_id=5)
  assert built.config is None
  assert isinstance(built.kind, DeckNormalKind)
  assert built.kind.config_id == 5


def test_a_filtered_deck_without_a_second_search_has_one_term():
  built = filtered_deck("Cram", "tag:verb", limit2=5)
  assert isinstance(built.kind, DeckFilteredKind)
  assert [term.search for term in built.kind.search_terms] == ["tag:verb"]


def test_a_filtered_deck_with_a_second_search_has_two_terms():
  Order = DeckFilteredKind.SearchTerm.Order
  built = filtered_deck(
    "Cram", "is:due", search2="is:new", limit2=5, order2=Order.ADDED
  )
  assert isinstance(built.kind, DeckFilteredKind)
  second = built.kind.search_terms[1]
  assert (second.search, second.limit, second.order) == ("is:new", 5, Order.ADDED)
