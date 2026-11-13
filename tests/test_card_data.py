"""What survives the trip through the `cards.data` column, and what Anki drops on the way?

    uv run --with pytest pytest tests/test_card_data.py

The column is a JSON object of optional keys, each left out when unset, so an ordinary
card carries `{}`. Every key but `cd` deserializes with `default_on_invalid`
(`rslib/src/storage/card/data.rs:20-63`), so Anki replaces a value it cannot read with
the default and keeps the rest of the object. `cd` is a plain `String` with no such
wrapper, so a `cd` that is not a string fails the whole struct, and `CardData::from_str`
answers an unparseable column with `unwrap_or_default()`: every key goes.

What `cd` holds is a JSON object inside that string, written by the reviewer with
`JSON.stringify` (`ts/reviewer/answering.ts:37-40`). `validate_custom_data`
(`data.rs:135-149`) takes keys of at most 8 bytes and a serialized string of at most 100,
and it runs when a card is updated or answered rather than when it is imported, so a
package can carry custom data that fails at the user's next review.

The FSRS values are rounded as the column is written, not as they are set
(`convert_to_json`), and Rust rounds a half away from zero where Python's `round()`
rounds it to the nearest even digit.

The models carry no anki dependency and neither does this file.
"""

import pytest

from genanki2.builtins import BASIC_NOTETYPE, DEFAULT_DECK
from genanki2.errors import Genanki2Warning
from genanki2.models.card import Card, CardData
from genanki2.models.note import Note

A_NOTE = Note(notetype=BASIC_NOTETYPE, fields=["front", "back"])

# A column of the shape Anki writes, with every key set and custom data of its own.
AN_ANKI_COLUMN = '{"pos":123,"s":4.5,"d":2.1,"dr":0.9,"decay":0.5,"lrt":1700000000,"cd":"{\\"v\\":\\"a\\"}"}'


# ------------------------------------- Reading -----------------------------------------


def test_an_ordinary_cards_column_reads_as_empty():
  assert CardData.from_json("{}") == CardData()


@pytest.mark.parametrize("column", ["", "garbage", "[1,2]", "null", "5", '"{}"'])
def test_a_column_that_is_not_an_object_reads_as_empty(column):
  assert CardData.from_json(column) == CardData()


def test_a_value_that_does_not_fit_its_key_is_dropped():
  """`default_on_invalid` is per key, so the rest of the object survives."""
  data = CardData.from_json('{"s": 1.5, "d": "x"}')
  assert data.fsrs_stability == 1.5
  assert data.fsrs_dificulty is None


@pytest.mark.parametrize(
  "column",
  [
    '{"s": "1.5"}',  # serde reads no float from a JSON string
    '{"pos": 1.0}',  # nor an integer from a float
    '{"pos": true}',  # nor from a bool
    '{"pos": -5}',  # the position is a u32
  ],
)
def test_a_value_anki_would_refuse_is_dropped(column):
  assert CardData.from_json(column) == CardData()


def test_an_integer_reads_as_a_float():
  """The one coercion serde does perform, so reading has to make it too."""
  assert CardData.from_json('{"s": 1}').fsrs_stability == 1.0


def test_a_negative_last_review_time_is_kept():
  """`lrt` is an i64, unlike the position, so a negative one is a value Anki stores."""
  assert CardData.from_json('{"lrt": -3}').last_review_time == -3


def test_an_unknown_key_is_ignored():
  """The struct has no `deny_unknown_fields`, so a key from a newer Anki is not an
  error. It does not survive the trip, because Anki does not carry it either."""
  assert CardData.from_json('{"unknown": 7, "s": 1.5}').fsrs_stability == 1.5


def test_a_null_reads_as_an_unset_key():
  assert CardData.from_json('{"s": null}') == CardData()


def test_custom_data_that_is_not_a_string_drops_every_other_key():
  """`cd` has no `default_on_invalid`, so it takes the whole struct down with it."""
  assert CardData.from_json('{"cd": 5, "s": 1.5}') == CardData()


@pytest.mark.parametrize("custom_data", ["garbage", "[1,2]", "5", "null"])
def test_custom_data_that_is_not_an_object_is_dropped_with_a_warning(custom_data):
  """Anki stores it and then refuses the card at its next review, so this is the case
  genanki2 names instead of passing on."""
  with pytest.warns(Genanki2Warning, match="not a JSON object"):
    data = CardData.from_json(f'{{"cd": "{custom_data}", "lrt": 5}}')
  assert data.custom_data == {}
  assert data.last_review_time == 5


def test_custom_data_decodes_into_a_dict():
  assert CardData.from_json('{"cd": "{\\"a\\": 1}"}').custom_data == {"a": 1}


# ------------------------------------- Writing -----------------------------------------


def test_an_empty_model_writes_an_empty_object():
  assert CardData().to_json() == "{}"


def test_an_unset_key_is_left_out():
  assert CardData(pos=3).to_json() == '{"pos":3}'


def test_empty_custom_data_is_left_out():
  assert CardData(cd={}).to_json() == "{}"


def test_custom_data_is_written_as_a_json_string():
  assert CardData(cd={"a": 1}).to_json() == '{"cd":"{\\"a\\":1}"}'


def test_custom_data_keeps_its_text_unescaped():
  """`JSON.stringify` leaves non-ASCII raw, and the 100-byte limit counts those bytes,
  so escaping them would both change the string and miscount it."""
  assert CardData(cd={"a": "é"}).to_json() == '{"cd":"{\\"a\\":\\"é\\"}"}'


def test_the_fsrs_values_are_rounded_where_anki_rounds_them():
  data = CardData(s=123.45678, d=1.23456, dr=0.98765, decay=0.16789)
  assert data.to_json() == '{"s":123.4568,"d":1.235,"dr":0.99,"decay":0.168}'


def test_a_half_rounds_away_from_zero():
  """Python's `round(1.2345, 3)` answers 1.234, rounding to the nearest even digit."""
  assert CardData(s=1.0, d=1.2345).to_json() == '{"s":1.0,"d":1.235}'


def test_rounding_happens_at_write_time():
  assert CardData(d=1.23456).fsrs_dificulty == 1.23456


def test_a_column_anki_wrote_round_trips_unchanged():
  """Key order included: the model declares its fields in the struct's order."""
  assert CardData.from_json(AN_ANKI_COLUMN).to_json() == AN_ANKI_COLUMN


# -------------------------------------- Checks -----------------------------------------


def test_a_key_of_eight_bytes_is_accepted():
  assert CardData(cd={"12345678": 1}).check()


def test_a_longer_key_is_rejected():
  with pytest.raises(ValueError, match="at most 8 bytes"):
    CardData(cd={"123456789": 1}).check()


def test_a_key_is_measured_in_bytes_rather_than_characters():
  """Three characters, nine bytes."""
  with pytest.raises(ValueError, match="at most 8 bytes"):
    CardData(cd={"日本語": 1}).check()


def test_custom_data_of_exactly_a_hundred_bytes_is_accepted():
  """`{"k":"` and `"}` are the 8 bytes around the value."""
  assert CardData(cd={"k": "x" * 92}).check()


def test_custom_data_one_byte_over_is_rejected():
  with pytest.raises(ValueError, match="100 bytes"):
    CardData(cd={"k": "x" * 93}).check()


def test_a_custom_data_value_json_cannot_encode_is_rejected():
  """`dict[str, Any]` admits values the column has no encoding for, and the check names
  them instead of letting the serializer raise."""
  with pytest.raises(ValueError, match="not JSON"):
    CardData(cd={"a": {1, 2}}).check()


def test_a_key_inserted_by_mutation_is_rejected():
  """Writing into the dict fires no validator, so the check is what catches the key."""
  data = CardData(cd={"a": 1})
  data.custom_data[1] = 2
  with pytest.raises(ValueError, match="has to be a string"):
    data.check()


@pytest.mark.parametrize("half", [{"s": 1.0}, {"d": 2.0}])
def test_a_memory_state_missing_half_of_itself_warns(half):
  """Anki reads a memory state only when both are there, so a lone value is carried by
  the column and ignored by the scheduler."""
  with pytest.warns(Genanki2Warning, match="not both set"):
    CardData(**half).to_json()


@pytest.mark.parametrize("both", [{"s": 1.0, "d": 2.0}, {}])
def test_a_memory_state_that_is_whole_or_absent_does_not_warn(both, recwarn):
  CardData(**both).to_json()
  assert recwarn.list == []


# ------------------------------- The column on the card --------------------------------


def a_card(**overrides) -> Card:
  return Card(note=A_NOTE, deck=DEFAULT_DECK, **overrides)


def test_a_card_writes_an_empty_column_by_default():
  assert a_card().to_row()["data"] == "{}"


def test_a_card_takes_the_column_as_text_or_as_the_model():
  assert a_card(data='{"pos": 5}').data == CardData(pos=5)


def test_a_card_row_round_trips_its_column():
  row = a_card(data=AN_ANKI_COLUMN).to_row()
  assert row["data"] == AN_ANKI_COLUMN
  assert Card.from_row(row, note=A_NOTE, deck=DEFAULT_DECK).to_row() == row
