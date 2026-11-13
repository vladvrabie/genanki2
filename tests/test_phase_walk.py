"""Which models do the phases reach, and how often?

    uv run --with pytest pytest tests/test_phase_walk.py

A phase called with `recursive=True` gathers every model reachable from where it was
called, each object once, and runs the phase on each with `recursive=False`. A note
reaches its notetype, a card its note and decks, a deck its preset and a review its
card, so a check from any of them covers what it points at. From a collection, a
notetype shared by every note is walked once rather than once per note.

Only the fields whose annotation could hold a model are read. Reading every field has to
reach the same objects, or a field's annotation hides a model from the phases.
"""

import warnings

import pytest

from genanki2.builtins import DEFAULT_DECK, DEFAULT_DECK_CONFIG, builtin_notetype
from genanki2.errors import Genanki2Error, Genanki2Warning
from genanki2.models.base import CheckedModel
from genanki2.models.card import Card, CardData
from genanki2.models.collection import Collection
from genanki2.models.deck import Deck, DeckFilteredKind
from genanki2.models.field import Field
from genanki2.models.note import Note
from genanki2.models.notetype import Notetype, NotetypeConfig
from genanki2.models.package import Package
from genanki2.models.review import Review, ReviewRating, ReviewType
from genanki2.time import time_s
from genanki2.utils import nested_instances

NOTETYPE_ID = 1_700_000_000_000
NO_TEMPLATES = "notetype has no templates"


def a_notetype() -> Notetype:
  return builtin_notetype("Basic (genanki2)", id=NOTETYPE_ID)


def a_notetype_without_templates() -> Notetype:
  notetype = a_notetype()
  notetype.templates.clear()
  return notetype


def a_notetype_modified_in_the_future() -> Notetype:
  notetype = a_notetype()
  notetype.modification_time_s = time_s() + 3600
  return notetype


def a_notetype_with_an_unused_field() -> Notetype:
  notetype = a_notetype()
  notetype.fields.append(Field(name="Unused"))
  return notetype


def a_note(notetype: Notetype) -> Note:
  return Note(notetype=notetype, fields=["front"] * len(notetype.fields))


def count_calls(monkeypatch, cls: type, phase: str) -> list[int]:
  """Count the calls to `cls.<phase>`, which `CheckedModel` reaches on every model."""
  calls: list[int] = [0]
  original = getattr(cls, phase)

  def counting(self, *args, **kwargs):
    calls[0] += 1
    return original(self, *args, **kwargs)

  monkeypatch.setattr(cls, phase, counting)
  return calls


# ------------------------------------ Reachability ------------------------------------


def test_a_notetype_held_only_by_a_note_is_checked_on_write(tmp_path):
  """The collection's list does not hold it, so only the note leads to it."""
  collection = Collection(
    notes=[a_note(a_notetype_without_templates())], decks=[DEFAULT_DECK]
  )
  with pytest.raises(Genanki2Error, match=NO_TEMPLATES):
    collection.write_sqlite(tmp_path / "collection.anki2")


def test_a_note_checks_its_notetype():
  with pytest.raises(Genanki2Error, match=NO_TEMPLATES):
    a_note(a_notetype_without_templates()).check()


def test_a_review_checks_down_to_the_notetype():
  """Review to card to note to notetype: the walk follows every step."""
  note = a_note(a_notetype_modified_in_the_future())
  card = Card(note=note, deck=DEFAULT_DECK)
  review = Review(card=card, ease=ReviewRating.GOOD, review_type=ReviewType.REVIEW)
  with pytest.raises(Genanki2Error, match="modification time"):
    review.check()


def test_a_deck_checks_its_preset():
  preset = DEFAULT_DECK_CONFIG.model_copy(
    update={"modification_time_s": time_s() + 3600}, deep=True
  )
  deck = Deck(name="Future preset", config=preset)
  with pytest.raises(Genanki2Error, match="modification time"):
    deck.check()


def test_a_shallow_check_leaves_the_notetype_alone():
  """`recursive=False` runs the note's own rules only."""
  a_note(a_notetype_without_templates()).check(recursive=False)


# ------------------------------------- Each once --------------------------------------


def test_a_notetype_shared_by_every_note_is_checked_once(monkeypatch):
  notetype = a_notetype()
  collection = Collection(
    notetypes=[notetype], notes=[a_note(notetype) for _ in range(50)]
  )
  calls = count_calls(monkeypatch, Notetype, "check")

  collection.check()

  assert calls == [1]


def test_a_card_answered_twice_is_prepared_once(monkeypatch):
  note = a_note(a_notetype())
  card = Card(note=note, deck=DEFAULT_DECK)
  reviews = [
    Review(card=card, ease=ReviewRating.GOOD, review_type=ReviewType.REVIEW)
    for _ in range(2)
  ]
  collection = Collection(notes=[note], cards=[card], reviews=reviews)
  calls = count_calls(monkeypatch, Card, "prepare")

  collection.prepare()

  assert calls == [1]


def test_a_shallow_prepare_collects_but_prepares_nothing_held(monkeypatch):
  """`Collection.prepare(recursive=False)` still runs its own step: collecting."""
  note = a_note(a_notetype())
  collection = Collection(cards=[Card(note=note, deck=DEFAULT_DECK)])
  calls = count_calls(monkeypatch, Card, "prepare")

  collection.prepare(recursive=False)

  assert calls == [0]
  assert collection.notes == [note]


def test_a_notetype_warning_fires_once_on_write(tmp_path):
  """It used to fire from the list and again from every note carrying the notetype."""
  notetype = a_notetype_with_an_unused_field()
  collection = Collection(
    notes=[a_note(notetype) for _ in range(3)], decks=[DEFAULT_DECK]
  )
  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always", Genanki2Warning)
    collection.write_sqlite(tmp_path / "collection.anki2")

  assert sum("has unused fields" in str(w.message) for w in caught) == 1


def test_a_nested_message_is_warned_about_once_on_write(monkeypatch):
  """The walk reaches each card requirement inside the notetype's config, and encoding
  the config must not run the phases on them again."""
  notetype = a_notetype()
  assert notetype.config.requirements
  calls = count_calls(monkeypatch, NotetypeConfig.CardRequirement, "warn")

  notetype.to_rows()

  assert calls == [len(notetype.config.requirements)]


# ----------------------------------- Fields walked ------------------------------------


def every_field(self: CheckedModel):
  """`CheckedModel._iter_checked_models()` reading every field, scalars included."""
  yield from nested_instances(
    (getattr(self, name) for name in type(self).model_fields), CheckedModel
  )


def test_the_fields_walked_reach_what_every_field_reaches(monkeypatch):
  """A package holding one model of each kind, some reachable only by reference: the
  home deck and its preset through the card, the card through the review."""
  home = Deck(name="Home", config=DEFAULT_DECK_CONFIG)
  filtered = Deck(name="Filtered", kind=DeckFilteredKind())
  note = a_note(a_notetype())
  card = Card(
    note=note,
    deck=filtered,
    original_deck=home,
    original_due=1,
    data=CardData(custom_data={"seen": 1}),
  )
  review = Review(card=card, ease=ReviewRating.GOOD, review_type=ReviewType.REVIEW)
  package = Package(
    collection=Collection(notes=[note], decks=[filtered], reviews=[review])
  )

  walked = {id(model): model for model in package._iter_unique_checked_models()}
  monkeypatch.setattr(CheckedModel, "_iter_checked_models", every_field)
  reached = {id(model): model for model in package._iter_unique_checked_models()}

  missed = [type(model).__name__ for key, model in reached.items() if key not in walked]
  assert missed == []
  assert {type(model).__name__ for model in walked.values()} >= {
    "CardData",
    "DeckConfig",
    "DeckFilteredKind",
    "Field",
    "Notetype",
    "Review",
    "SearchTerm",
    "Template",
  }
