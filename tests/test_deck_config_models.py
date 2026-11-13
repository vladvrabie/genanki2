"""Does a `DeckConfig` hold what Anki puts in a preset, and survive a trip through the
row layer?

    uv run --with pytest pytest tests/test_deck_config_models.py

The defaults are the assertion this file is built around. Anki has two default
configurations, and only one of them is ever stored: `DEFAULT_DECK_CONFIG_INNER`
(`rslib/src/deckconfig/mod.rs:41-86`) is a struct-update base whose own comment says it
does not set the learning steps, and `DeckConfig::default()` (`mod.rs:88-103`) fills
those in before anything writes a preset. The distance between them is three fields, all
of them empty in the constant, so a model that transcribed the constant would build a
preset with no learning steps and no error. Comparing against the blob Anki wrote for
preset 1, dumped in `_default_rows`, catches that without an oracle.

A preset is matched by id on import and by nothing else, so the name is free text: Anki
neither normalizes it, nor requires it to be unique, nor rejects the empty string. The
name cases below pin that genanki2 leaves it alone too, since repairing it would alter a
name read back out of a package Anki wrote.

The models carry no anki dependency and neither does this file.
"""

import struct

import pydantic as pyd
import pytest

from genanki2 import _default_rows, _proto
from genanki2.models.deck_config import DeckConfig, DeckConfigInner

DEFAULT_ROW = _default_rows.table_deck_config[0]

# Every enum the blob declares, against the protobuf enum it mirrors. Compared by value
# rather than by name, because the wire is what has to agree.
ENUMS = [
  pytest.param(model, name, id=name)
  for model, name in [
    (DeckConfigInner.NewCardInsertOrder, "NewCardInsertOrder"),
    (DeckConfigInner.NewCardGatherPriority, "NewCardGatherPriority"),
    (DeckConfigInner.NewCardSortOrder, "NewCardSortOrder"),
    (DeckConfigInner.ReviewCardOrder, "ReviewCardOrder"),
    (DeckConfigInner.ReviewMix, "ReviewMix"),
    (DeckConfigInner.LeechAction, "LeechAction"),
    (DeckConfigInner.AnswerAction, "AnswerAction"),
    (DeckConfigInner.QuestionAction, "QuestionAction"),
  ]
]

# The four floats whose default is a decimal a 64-bit Python float holds exactly and a
# 32-bit protobuf float does not.
NARROWED_FLOATS = [
  "easy_multiplier",
  "hard_multiplier",
  "desired_retention",
  "historical_retention",
]

# The ends of Anki's ranges that a 32-bit float rounds past: 0.7 and 1.31 round below
# the 64-bit literal, 0.97 and 0.99 above it.
RANGE_ENDS = [
  pytest.param("desired_retention", 0.7, id="desired_retention-minimum"),
  pytest.param("desired_retention", 0.99, id="desired_retention-maximum"),
  pytest.param("historical_retention", 0.7, id="historical_retention-minimum"),
  pytest.param("historical_retention", 0.97, id="historical_retention-maximum"),
  pytest.param("initial_ease", 1.31, id="initial_ease-minimum"),
]

NAMES = [
  pytest.param("Default", id="stock"),
  pytest.param("  Padded  ", id="padded"),
  pytest.param("Study::Italian", id="separator"),
  pytest.param("café", id="decomposed"),
  pytest.param("a\x1fb", id="field-separator"),
]


def a_row(**overrides) -> dict:
  return DEFAULT_ROW | overrides


# ---------------------------------- The defaults --------------------------------------


def test_a_new_configuration_is_the_one_anki_writes():
  assert DeckConfigInner().to_bytes() == DEFAULT_ROW["config"]


def test_the_steps_the_rust_constant_leaves_empty_are_filled_in():
  """The three fields `DeckConfig::default()` overrides. A preset without them still
  imports, and studies with no learning steps at all."""
  configuration = DeckConfigInner()
  assert configuration.learn_steps == [1.0, 10.0]
  assert configuration.relearn_steps == [10.0]
  assert configuration.easy_days_percentages == [1.0] * 7


def test_a_default_list_is_not_shared_between_configurations():
  first, second = DeckConfigInner(), DeckConfigInner()
  first.learn_steps.append(100.0)
  assert second.learn_steps == [1.0, 10.0]


# ---------------------------------- Round trips ---------------------------------------


def test_the_default_row_survives_a_round_trip():
  assert DeckConfig.from_row(DEFAULT_ROW).to_row() == DEFAULT_ROW


def test_reading_a_row_does_not_write_to_it():
  row = a_row()
  DeckConfig.from_row(row)
  assert row == DEFAULT_ROW


def test_a_row_holds_what_the_configuration_was_built_with():
  row = DeckConfig(id=7, name="Study", mtime_secs=123, usn=4).to_row()
  assert row == a_row(id=7, name="Study", mtime_secs=123, usn=4)


def test_a_value_at_its_protobuf_zero_is_dropped_from_the_blob():
  """`new_per_day` is 20 in the blob Anki wrote, so setting it to zero has to remove the
  field rather than write an explicit zero, or the bytes stop matching Anki's."""
  configuration = DeckConfigInner.from_bytes(DEFAULT_ROW["config"])
  configuration.new_per_day = 0
  written = configuration.to_bytes()
  message = _proto.DeckConfigConfig.FromString(written)
  assert "new_per_day" not in {field.name for field, _ in message.ListFields()}
  assert DeckConfigInner.from_bytes(written).new_per_day == 0


# ------------------------------------- Floats -----------------------------------------


@pytest.mark.parametrize("attribute", NARROWED_FLOATS)
def test_a_float_comes_back_narrowed_to_32_bits(attribute):
  """The blob is unaffected, so this never reaches a package. It reaches a comparison: a
  configuration built here is not equal to the same one read back."""
  built = DeckConfigInner()
  read_back = DeckConfigInner.from_bytes(built.to_bytes())
  written = getattr(built, attribute)
  assert getattr(read_back, attribute) != written
  assert (
    getattr(read_back, attribute) == struct.unpack("f", struct.pack("f", written))[0]
  )


def test_narrowing_does_not_change_the_blob():
  blob = DeckConfigInner().to_bytes()
  assert DeckConfigInner.from_bytes(blob).to_bytes() == blob


@pytest.mark.parametrize(("attribute", "value"), RANGE_ENDS)
def test_a_value_on_the_end_of_its_range_survives_a_round_trip(attribute, value):
  """Anki compares the 32-bit value with 32-bit bounds (`ensure_f32_valid`,
  `rslib/src/deckconfig/mod.rs:317`), so the end of the range is inside it. Compared
  with the 64-bit literal, the value read back falls outside and is reset to the
  default."""
  written = DeckConfigInner(**{attribute: value}).to_bytes()
  read_back = DeckConfigInner.from_bytes(written)
  assert getattr(read_back, attribute) == struct.unpack("f", struct.pack("f", value))[0]


@pytest.mark.parametrize(("attribute", "value"), RANGE_ENDS)
def test_a_value_set_on_the_end_of_its_range_is_kept_as_given(attribute, value):
  """Only the comparison is made in 32 bits. The value is narrowed when it is written."""
  assert getattr(DeckConfigInner(**{attribute: value}), attribute) == value


# ------------------------------------- Enums ------------------------------------------


@pytest.mark.parametrize(("model", "name"), ENUMS)
def test_an_enum_holds_the_values_its_protobuf_enum_holds(model, name):
  descriptor = _proto.DeckConfigConfig.DESCRIPTOR.enum_types_by_name[name]
  assert {member.value for member in model} == {
    value.number for value in descriptor.values
  }
  assert len(model) == len(descriptor.values)


# ------------------------------------- The name ---------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_a_name_is_stored_as_it_was_given(name):
  assert DeckConfig(name=name).to_row()["name"] == name


@pytest.mark.parametrize("name", NAMES)
def test_a_name_written_by_anki_survives_a_round_trip(name):
  assert DeckConfig.from_row(a_row(name=name)).to_row()["name"] == name


def test_an_empty_name_is_accepted():
  """Anki's own default preset is constructed with one, and its deck options screen
  stores a name the user clears, so a package can arrive holding it."""
  assert DeckConfig.from_row(a_row(name="")).name == ""
  assert DeckConfig(name="").name == ""


def test_a_name_that_another_preset_already_holds_is_accepted():
  """`deck_config.name` carries no unique index, and the importer's insert conflicts on
  the id primary key. Two presets of the same name are a legal collection."""
  assert DeckConfig(name="Study").name == DeckConfig(name="Study").name


# ---------------------------------- The sync fields -----------------------------------


def test_a_new_configuration_is_marked_unsynced():
  assert DeckConfig(name="Study").update_sequence_n == -1


def test_a_negative_modification_time_is_refused():
  with pytest.raises(pyd.ValidationError):
    DeckConfig(name="Study", mtime_secs=-1)


def test_a_modification_time_in_the_future_is_accepted():
  """Accepted here and refused by `to_row`, so reading a collection holding one works.
  `tests/test_timestamps.py` covers the write side, for this model and the five others."""
  assert DeckConfig(name="Study", mtime_secs=2**40).modification_time_s == 2**40


# ---------------------------------- The FSRS arrays -----------------------------------


@pytest.mark.parametrize(
  ("set_arrays", "expected"),
  [
    pytest.param({}, (), id="none"),
    pytest.param({"fsrs_params_4": [4.0]}, (4.0,), id="4"),
    pytest.param({"fsrs_params_5": [5.0]}, (5.0,), id="5"),
    pytest.param({"fsrs_params_6": [6.0]}, (6.0,), id="6"),
    pytest.param(
      {"fsrs_params_4": [4.0], "fsrs_params_5": [5.0]}, (5.0,), id="4-and-5"
    ),
    pytest.param(
      {"fsrs_params_4": [4.0], "fsrs_params_6": [6.0]}, (6.0,), id="4-and-6"
    ),
    pytest.param(
      {"fsrs_params_4": [4.0], "fsrs_params_5": [5.0], "fsrs_params_6": [6.0]},
      (6.0,),
      id="all",
    ),
  ],
)
def test_the_newest_array_holding_parameters_is_the_one_that_counts(
  set_arrays, expected
):
  """Anki reads version 6 first and falls back on 5, then 4, so a caller who fills in
  the wrong array gets a preset whose parameters are ignored rather than an error. All
  three empty is what leaves Anki on its own built-in parameters."""
  assert DeckConfigInner(**set_arrays).fsrs_params == expected
