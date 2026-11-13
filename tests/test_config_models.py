"""Does a `config` row survive a trip through `ConfigEntry`?

    uv run --with pytest pytest tests/test_config_models.py

Every column of this table reaches the model under an alias, and the key column is
spelled `KEY`. SQLite compares column names case-insensitively and pydantic does not, so
an alias that does not match its column fails only when a real row is read: the value
falls back to the model's default, or the row is rejected for a missing field.
`_default_rows` is a real empty collection dumped by Anki, so the rows below are the ones
Anki itself writes.

The models carry no anki dependency and neither does this file.
"""

import pytest

from genanki2 import _default_rows
from genanki2.models.config import ConfigEntry

CONFIG_ROWS = _default_rows.table_config


def config_ids() -> list[str]:
  """Readable pytest ids, so a failure names the setting instead of a row index."""
  return [str(row["KEY"]) for row in CONFIG_ROWS]


# --------------------------------- Row round trips ---------------------------------


@pytest.mark.parametrize("row", CONFIG_ROWS, ids=config_ids())
def test_rows_survive_a_round_trip(row):
  assert ConfigEntry.from_row(row).to_row() == row


def test_the_key_column_is_read_and_written_under_its_own_name():
  """`KEY` is the column and `key` is the attribute, so both directions go through the
  alias. Without it the row has no `key` and reading raises."""
  entry = ConfigEntry.from_row(
    {"KEY": "rollover", "val": b"4", "mtime_secs": 0, "usn": 0}
  )
  assert entry.key == "rollover"
  assert entry.to_row()["KEY"] == "rollover"


def test_an_entry_built_by_attribute_name_still_writes_the_column_names():
  """A caller holds a model, not a row, so the two spellings meet here."""
  entry = ConfigEntry(
    key="rollover", value=b"4", modification_time_s=0, update_sequence_n=0
  )
  assert entry.to_row() == {"KEY": "rollover", "val": b"4", "mtime_secs": 0, "usn": 0}


def test_sync_columns_are_settable_by_name_and_by_alias():
  """Without `validate_by_name` the attribute name is an unknown key, silently dropped in
  favour of the default, so a caller not holding a row loses the value with no error."""
  assert ConfigEntry(key="k", modification_time_s=7).modification_time_s == 7
  assert ConfigEntry(key="k", mtime_secs=7).modification_time_s == 7
  assert ConfigEntry(key="k", update_sequence_n=7).update_sequence_n == 7
  assert ConfigEntry(key="k", usn=7).update_sequence_n == 7


# ------------------------------------ The columns ------------------------------------


def test_an_empty_key_is_accepted():
  """`set_config` takes any string and does not check it (rslib/src/config/mod.rs:133-144),
  so an add-on can write an empty key and a collection can hold one. Reading such a
  collection has to give it back unchanged."""
  assert ConfigEntry(key="").to_row()["KEY"] == ""


def test_the_default_value_is_the_json_null():
  """A value that does not decode reads as a key that was never set, so the default is
  inert either way (rslib/src/config/mod.rs:109-121). `null` is the inert value that is
  still JSON, so a whole-table read does not fail on it: `get_all_config`
  (rslib/src/storage/config/mod.rs:69-77) propagates the decode error rather than
  swallowing it, and it backs sync and the schema 11 downgrade."""
  entry = ConfigEntry(key="rollover")
  assert entry.to_row()["val"] == b"null"
  assert entry.value_or(4) is None


# ---------------------------------- Reading a value ----------------------------------


@pytest.mark.parametrize(
  ("value", "decoded"),
  [(b"4", 4), (b"true", True), (b'"noteFld"', "noteFld"), (b"[1]", [1])],
)
def test_a_value_is_decoded_as_json(value, decoded):
  """Not protobuf, unlike the blobs on the other tables."""
  assert ConfigEntry(key="k", value=value).value_or() == decoded


@pytest.mark.parametrize("value", [b"", b"not json", b"\xff"])
def test_a_value_that_cannot_be_decoded_is_the_default(value):
  """Anki cannot fail here: it reads any undecodable value as a setting that was never
  written and applies the caller's default (rslib/src/config/mod.rs:107-121). The empty
  value is the common case, and the invalid UTF-8 one raises before JSON is reached."""
  assert ConfigEntry(key="k", value=value).value_or(4) == 4


def test_a_decoded_false_is_not_mistaken_for_a_missing_value():
  """`false`, `0` and `[]` all decode, so returning the default for them would invert
  settings that are off."""
  assert ConfigEntry(key="k", value=b"false").value_or(True) is False


@pytest.mark.parametrize("row", CONFIG_ROWS, ids=config_ids())
def test_setting_a_decoded_value_reproduces_the_bytes_anki_wrote(row):
  """Anki encodes with `serde_json::to_vec` (rslib/src/config/mod.rs:133-139), so a
  value read and set again must not change its row."""
  entry = ConfigEntry.from_row(row)
  assert ConfigEntry(key="k").set_value(entry.value_or()).value == entry.value


@pytest.mark.parametrize(
  ("value", "encoded"),
  [
    (None, b"null"),
    (False, b"false"),
    ({"a": [1, 2.5]}, b'{"a":[1,2.5]}'),
    ("città", '"città"'.encode()),
  ],
  ids=["null", "bool", "compact", "utf-8"],
)
def test_a_set_value_is_encoded_as_serde_json_writes_it(value, encoded):
  """serde_json writes no spaces and leaves non-ASCII as UTF-8, where Python's `json`
  defaults to `, `, `: ` and `\\u` escapes."""
  entry = ConfigEntry(key="k").set_value(value)
  assert entry.value == encoded
  assert entry.value_or() == value


@pytest.mark.parametrize(
  ("value", "error"),
  [(float("nan"), ValueError), (float("inf"), ValueError), ({1, 2}, TypeError)],
  ids=["nan", "inf", "set"],
)
def test_a_value_with_no_json_form_is_refused(value, error):
  """Python's `json` writes NaN and Infinity, which serde_json rejects, so Anki would
  read the setting as never written."""
  with pytest.raises(error):
    ConfigEntry(key="k").set_value(value)


def test_a_fresh_entry_is_unsynced():
  """-1 is Anki's "never synced" marker; 0 would claim the row is in sync with a server."""
  assert ConfigEntry(key="rollover").update_sequence_n == -1
