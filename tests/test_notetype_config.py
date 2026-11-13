"""Does `NotetypeConfig` hold what Anki puts in a notetype, and do the values that read
it behave the way Anki does?

    uv run --with pytest pytest tests/test_notetype_config.py

Three of these values are only ever compared against a real collection. The stock CSS and
LaTeX constants are written verbatim into every notetype genanki2 builds, and a stray
newline or an indent picked up from a triple-quoted literal is invisible until someone
diffs a deck against one Anki made. `sort_field_idx` and `kind` are read rather than
written, and both have a repair rule that differs from what Python does on its own:
indexing out of range yields `""` rather than raising, and a cloze notetype numbers its
cards by cloze rather than by template.

`_default_rows` is a real empty collection dumped by Anki, so every expectation below is
what Anki itself wrote.

The models carry no anki dependency and neither does this file.
"""

from typing import Any

import pytest

from genanki2 import _proto
from genanki2._default_rows import table_notetypes
from genanki2.api import notetype as build_notetype
from genanki2.builtins import (
  BASIC_AND_REVERSED_NOTETYPE,
  BASIC_NOTETYPE,
  BUILTIN_NOTETYPES,
  CLOZE_NOTETYPE,
  builtin_notetype,
)
from genanki2.errors import Genanki2Error
from genanki2.models.note import Note
from genanki2.models.notetype import (
  DEFAULT_CLOZE_CSS,
  DEFAULT_CSS,
  DEFAULT_LATEX_FOOTER,
  DEFAULT_LATEX_HEADER,
  NotetypeConfig,
)


def stock(name: str) -> Any:
  """The configuration Anki wrote for the stock notetype called `name`.

  `Any`, because the message classes are built from the vendored descriptors at import
  and have no static type to name.
  """
  row = next(row for row in table_notetypes if str(row["name"]).startswith(name))
  blob: Any = row["config"]
  return _proto.NotetypeConfig.FromString(blob)


@pytest.mark.parametrize(
  "constant, attribute",
  [
    (DEFAULT_CSS, "css"),
    (DEFAULT_LATEX_HEADER, "latex_pre"),
    (DEFAULT_LATEX_FOOTER, "latex_post"),
  ],
  ids=["css", "latex_pre", "latex_post"],
)
def test_the_stock_constants_are_what_anki_writes(constant: str, attribute: str):
  """Character for character, so an indent or a newline from the literal is caught."""
  assert constant == getattr(stock("Basic ("), attribute)


def test_a_cloze_config_matches_the_one_anki_builds():
  """The cloze rules are appended to the stock styling rather than replacing it."""
  config = NotetypeConfig.new_cloze_config()
  assert config.kind is NotetypeConfig.Kind.CLOZE
  assert config.css == stock("Cloze").css
  assert config.css == DEFAULT_CSS + DEFAULT_CLOZE_CSS


def test_a_cloze_config_takes_other_options_and_replaces_the_styling():
  """A styling passed in replaces the stock one instead of colliding with it."""
  config = NotetypeConfig.new_cloze_config(css=".card {}", sort_field_idx=1)
  assert config.kind is NotetypeConfig.Kind.CLOZE
  assert config.css == ".card {}"
  assert config.sort_field_idx == 1


@pytest.mark.parametrize(
  "notetype, expected",
  [(BASIC_NOTETYPE, False), (CLOZE_NOTETYPE, True)],
  ids=["basic", "cloze"],
)
def test_is_cloze_follows_the_kind(notetype, expected: bool):
  assert notetype.is_cloze() is expected


@pytest.mark.parametrize("notetype", BUILTIN_NOTETYPES, ids=lambda nt: nt.name)
def test_every_notetype_is_either_normal_or_cloze(notetype):
  assert notetype.is_normal() is not notetype.is_cloze()


def test_filling_ids_leaves_the_ones_already_set():
  """A package read back carries the ids it was written with, and a second call has to
  leave them alone, or two builds of the same deck stop matching."""
  notetype = builtin_notetype(BASIC_NOTETYPE.name)
  notetype.fields[0].config.id = 7
  notetype.fill_missing_ids()
  assert notetype.fields[0].config.id == 7


def test_filling_ids_is_idempotent():
  notetype = builtin_notetype(BASIC_NOTETYPE.name).fill_missing_ids()
  before = [field.config.id for field in notetype.fields]
  notetype.fill_missing_ids()
  assert [field.config.id for field in notetype.fields] == before


@pytest.mark.parametrize("notetype", BUILTIN_NOTETYPES, ids=lambda nt: nt.name)
def test_filling_ids_leaves_a_builtin_constant_alone(notetype):
  """Ids filled on a constant would be new in every process, which forks the notetype
  on the next import, so the constants keep the null ids they ship with."""
  assert notetype.fill_missing_ids() is notetype
  items = [*notetype.fields, *notetype.templates]
  assert all(item.config.id is None for item in items)


def test_filling_ids_on_another_frozen_notetype_raises():
  notetype = builtin_notetype(BASIC_NOTETYPE.name).freeze()
  with pytest.raises(AttributeError, match="is frozen"):
    notetype.fill_missing_ids()


def test_filled_ids_span_the_whole_signed_range():
  """Anki mints across the full range, so about half of them are negative. Sixty-four
  draws make an all-positive run a 2**-64 event rather than a flaky test."""
  ids: list[int | None] = []
  for _ in range(32):
    notetype = builtin_notetype(BASIC_NOTETYPE.name).fill_missing_ids()
    ids += [field.config.id for field in notetype.fields]
  assert all(id is not None and -(2**63) <= id < 2**63 for id in ids)
  assert any(id is not None and id < 0 for id in ids)


def test_the_sort_field_is_the_one_the_notetype_names():
  notetype = builtin_notetype(BASIC_NOTETYPE.name)
  notetype.config.sort_field_idx = 1
  assert Note(notetype=notetype, fields=["front", "back"]).sort_field == "back"


def test_a_sort_field_index_past_the_last_field_is_empty():
  """Anki substitutes `""` rather than failing, where indexing a Python list raises."""
  notetype = builtin_notetype(BASIC_NOTETYPE.name)
  notetype.config.sort_field_idx = 9
  assert Note(notetype=notetype, fields=["front", "back"]).sort_field == ""


def test_the_sort_field_follows_the_index():
  notetype = builtin_notetype(BASIC_NOTETYPE.name)
  notetype.config.sort_field_idx = 1
  assert notetype.sort_field is notetype.fields[1]


def test_a_sort_field_index_past_the_last_field_gives_the_last_field():
  """The field Anki settles on, since it clamps the index when it saves the notetype
  (`reposition_sort_idx` in rslib/src/notetype/mod.rs)."""
  notetype = builtin_notetype(BASIC_NOTETYPE.name)
  notetype.config.sort_field_idx = 9
  assert notetype.sort_field is notetype.fields[-1]


def test_a_notetype_without_fields_has_no_sort_field():
  notetype = builtin_notetype(BASIC_NOTETYPE.name)
  notetype.fields.clear()
  assert notetype.sort_field is None


def test_setting_the_sort_field_by_name_sets_its_index():
  notetype = builtin_notetype(BASIC_NOTETYPE.name).set_sort_field("Back")
  assert notetype.config.sort_field_idx == 1


def test_setting_the_sort_field_matches_the_name_case_sensitively():
  """As `Note.__getitem__` does."""
  notetype = builtin_notetype(BASIC_NOTETYPE.name)
  with pytest.raises(Genanki2Error, match='"back".*"Front", "Back"'):
    notetype.set_sort_field("back")
  assert notetype.config.sort_field_idx == 0


def test_setting_the_sort_field_from_another_notetypes_field_matches_its_name():
  notetype = builtin_notetype(BASIC_NOTETYPE.name)
  notetype.set_sort_field(BASIC_AND_REVERSED_NOTETYPE.fields[1])
  assert notetype.config.sort_field_idx == 1


def test_setting_the_sort_field_on_a_builtin_constant_raises():
  with pytest.raises(AttributeError, match="is frozen"):
    BASIC_NOTETYPE.set_sort_field("Back")


def test_the_notetype_function_sets_the_sort_field_by_name():
  notetype = build_notetype(
    "Vocabulary", ["Italian", "English"], [], sort_field="English"
  )
  assert notetype.config.sort_field_idx == 1
