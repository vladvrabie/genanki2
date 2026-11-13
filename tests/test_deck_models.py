"""Does a `Deck` survive a trip through the row layer, in both directions?

    uv run --with pytest pytest tests/test_deck_models.py

A deck name is stored one way and shown another: components are joined by `\\x1f` in the
`decks.name` column and by `::` everywhere a person reads one. `Deck` holds the human
form, so `from_row` and `to_row` convert on every trip, and a package read and written
back has to come out with the name it went in with.

`_default_rows` is a real empty collection dumped by Anki, so `Default` is compared
against the row Anki itself wrote rather than one written here.

Two cases below pin behaviour that looks wrong on sight. A `::` inside a native component
does not survive, because the conversion back out splits on it, and an empty native
component is refused rather than repaired. The first matches Anki, which converts to the
human form by a bare separator substitution with no per-component work; the second does
not, and is a deliberate divergence.

The models carry no anki dependency and neither does this file.
"""

import warnings

import pydantic as pyd
import pytest

from genanki2 import _default_rows
from genanki2.builtins import DEFAULT_DECK_CONFIG
from genanki2.errors import Genanki2Warning
from genanki2.models.deck import Deck, DeckFilteredKind, DeckNormalKind
from genanki2.models.deck_config import DeckConfig

DEFAULT_ROW = _default_rows.table_decks[0]

HUMAN_NAMES = [
  "Test",
  "Test::Nested",
  "Test::A::B",
  "Italian::Verbi::Presente",
]

NATIVE_NAMES = [
  "Default",
  "Test\x1fNested",
  "a\x1fb\x1fc",
  "blank\x1fx",
]


def a_row(**overrides) -> dict:
  return DEFAULT_ROW | overrides


def a_deck(name: str, **overrides) -> Deck:
  return Deck(name=name, **overrides)


# --------------------------------- Round trips ----------------------------------------


def test_the_default_row_survives_a_round_trip():
  assert Deck.from_row(DEFAULT_ROW).to_row() == DEFAULT_ROW


def test_reading_a_row_does_not_write_to_it():
  row = a_row()
  Deck.from_row(row)
  assert row == DEFAULT_ROW


@pytest.mark.parametrize("native", NATIVE_NAMES)
def test_a_row_written_by_anki_survives_a_round_trip(native):
  assert Deck.from_row(a_row(name=native)).to_row()["name"] == native


@pytest.mark.parametrize("human", HUMAN_NAMES)
def test_a_human_name_survives_a_round_trip(human):
  assert Deck.from_row(a_deck(human).to_row()).name == human


@pytest.mark.parametrize("human", HUMAN_NAMES)
def test_the_row_holds_the_native_name(human):
  assert a_deck(human).to_row()["name"] == human.replace("::", "\x1f")


def test_only_the_name_changes_on_the_way_to_a_row():
  row = a_deck("Test::Nested", id=7, mtime_secs=123, usn=4).to_row()
  assert row == a_row(name="Test\x1fNested", id=7, mtime_secs=123, usn=4)


# ------------------------------- Names that do not survive ----------------------------


def test_a_separator_inside_a_native_component_does_not_survive():
  """Anki does the same: it converts a native name to a human one by substituting the
  separator, without looking at what the components already contain, and converts back
  by splitting on `::`."""
  assert Deck.from_row(a_row(name="A::B")).name == "A::B"
  assert Deck.from_row(a_row(name="A::B")).to_row()["name"] == "A\x1fB"


def test_an_empty_native_component_is_refused():
  """Anki repairs this instead, substituting `blank` for a component that normalizes to
  nothing (rslib/src/decks/name.rs, normalized_deck_name_component). genanki2 raises,
  because the repair renames a deck the caller did not ask to rename, and a name arriving
  in this shape was not written by Anki in the first place."""
  with pytest.raises(pyd.ValidationError, match="empty part"):
    Deck.from_row(a_row(name="a\x1f\x1fb"))


@pytest.mark.parametrize("human", ["", "::", "a::", "a::::b", "a::  ::b"])
def test_an_empty_human_component_is_refused(human):
  with pytest.raises(pyd.ValidationError):
    a_deck(human)


# ------------------------------- Names that are repaired ------------------------------


@pytest.mark.parametrize(
  ("given", "kept"),
  [
    ("  Test  ::  Nested  ", "Test::Nested"),
    ("Test:::Nested", "Test::Nested"),
    ("Te\x01st::Nes\nted", "Test::Nested"),
    ("café::x", "café::x"),  # decomposed, so NFC has something to do
  ],
)
def test_a_component_is_normalized_before_it_is_stored(given, kept):
  assert a_deck(given).name == kept
  assert a_deck(given).to_row()["name"] == kept.replace("::", "\x1f")


def test_a_name_is_normalized_again_on_assignment():
  deck = a_deck("Test")
  deck.name = "  Renamed  ::  Deck  "
  assert deck.name == "Renamed::Deck"


# ------------------------------ A preset on a filtered deck ---------------------------


def test_a_filtered_deck_cannot_carry_a_deck_config():
  """A preset is reachable only through the normal kind blob's `config_id`, so on a
  filtered deck the attribute would name one the deck cannot store."""
  with pytest.raises(pyd.ValidationError, match="deck config set on it"):
    Deck(name="Filtered", kind=DeckFilteredKind(), config=DEFAULT_DECK_CONFIG)


def test_converting_a_deck_to_filtered_needs_the_preset_cleared_first():
  """The check runs on assignment too, so the two fields cannot be flipped in the order
  that leaves them contradicting each other."""
  deck = a_deck("Test")
  deck.config = DEFAULT_DECK_CONFIG
  with pytest.raises(pyd.ValidationError, match="deck config set on it"):
    deck.kind = DeckFilteredKind()
  deck.config = None
  deck.kind = DeckFilteredKind()
  assert deck.is_filtered()


# ------------------------------- The default deck's id --------------------------------


def test_the_default_deck_id_cannot_be_filtered():
  """Anki creates deck 1 normal and neither deletes nor converts it, which is what lets
  `Collection.check_card_home_deck()` pass over an original deck id of 1."""
  with pytest.raises(pyd.ValidationError, match="reserves"):
    a_deck("Filtered", id=1, kind=DeckFilteredKind())


def test_a_normal_deck_keeps_the_default_id():
  assert a_deck("Default", id=1).id == 1


def test_taking_the_default_id_is_rejected_on_assignment():
  deck = a_deck("Filtered", kind=DeckFilteredKind())
  with pytest.raises(pyd.ValidationError, match="reserves"):
    deck.id = 1


def test_filtering_the_default_deck_is_rejected_on_assignment():
  """The other order of the same pair, since either assignment re-runs the validator."""
  deck = a_deck("Default", id=1)
  with pytest.raises(pyd.ValidationError, match="reserves"):
    deck.kind = DeckFilteredKind()


# ---------------------------------- The preset's id -----------------------------------


def test_a_deck_with_a_preset_names_the_preset_id():
  slow = DeckConfig(id=1_759_600_000_100, name="Slow")
  assert a_deck("Test", config=slow).config_id == 1_759_600_000_100


def test_a_deck_without_a_preset_names_the_kind_blob_id():
  assert a_deck("Test").config_id == 1
  assert a_deck("Test", kind=DeckNormalKind(config_id=7)).config_id == 7


def test_a_preset_id_changed_after_assignment_is_the_one_named():
  """Changing the preset's id runs no deck validator, so the kind blob still holds the
  old id until it is synced, and the preset is the authority."""
  slow = DeckConfig(id=1_759_600_000_100, name="Slow")
  deck = a_deck("Test", config=slow)
  slow.id = 1_759_600_000_101
  assert deck.kind.config_id == 1_759_600_000_100
  assert deck.config_id == 1_759_600_000_101


def test_a_filtered_deck_names_no_preset():
  assert a_deck("Filtered", kind=DeckFilteredKind()).config_id is None


def test_a_deck_read_without_its_preset_names_the_row_id():
  row = a_row(kind=Deck(name="x", kind=DeckNormalKind(config_id=7)).to_row()["kind"])
  assert Deck.from_row(row).config_id == 7


# --------------------------- The desired retention override ---------------------------


@pytest.mark.parametrize("retention", [0.7, 0.99])
def test_a_retention_on_the_end_of_the_range_read_back_does_not_warn(retention):
  """The override is a 32-bit float, which 0.7 rounds below and 0.99 above, and Anki
  compares it in 32 bits."""
  read_back = DeckNormalKind.from_bytes(
    DeckNormalKind(desired_retention=retention).to_bytes()
  )
  with warnings.catch_warnings():
    warnings.simplefilter("error", Genanki2Warning)
    read_back.warn()


@pytest.mark.parametrize("retention", [0.69, 0.995])
def test_a_retention_outside_the_range_warns(retention):
  with pytest.warns(Genanki2Warning, match="desired retention"):
    DeckNormalKind(desired_retention=retention).warn()
