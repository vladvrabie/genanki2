"""Do the builtins hand out copies, and do they keep the identity an import matches on?

    uv run --with pytest pytest tests/test_builtins.py

Two things are being checked here, and they pull in opposite directions.

`_default_rows` is module-level mutable state that every builtin is rebuilt from, so a
function that writes into a row corrupts every later call in the process, and the
constants are handed to every caller at once, so an object that leaks a reference into
one deck shows up in the next. Everything below that copies is checked for copying.

What must *not* change is the identity: the stock ids fixed at 1 to 6, and the
` (genanki2)` suffix on every name. Anki matches an incoming notetype by id
(`get_target_notetype`, rslib/src/import_export/package/apkg/import/notes.rs:156) and
renames one whose name is already taken (`ensure_notetype_name_unique`, :207), so both
are part of the file format rather than of this module's presentation. A package built
today has to present the same pair as one built two releases ago, or an update to a deck
imports as a second copy of it.
"""

import re

import pytest

from genanki2 import _default_rows
from genanki2.builtins import (
  BASIC_NOTETYPE,
  BUILTIN_NOTETYPES,
  BUILTIN_NOTETYPES_VERSIONS,
  DEFAULT_DECK,
  DEFAULT_DECK_CONFIG,
  builtin_notetype,
  default_collection_metadata,
  default_config_entries,
  default_deck,
)
from genanki2.models.package import Package

BASIC = "Basic (genanki2)"
STOCK_NAMES = [row["name"] for row in _default_rows.table_notetypes]
AN_ID = 99
A_TIMESTAMP = 1_700_000_000


@pytest.fixture(autouse=True)
def rows_are_unchanged():
  """Every test asserts this, so none of them has to. A test that corrupts the rows would
  otherwise be diagnosed from the failure of whichever test happened to run next."""
  before = {
    name: [dict(row) for row in getattr(_default_rows, name)]
    for name in ("table_notetypes", "table_fields", "table_templates", "table_decks")
  }
  yield
  for name, rows in before.items():
    assert [dict(row) for row in getattr(_default_rows, name)] == rows, (
      f"_default_rows.{name} was written to"
    )


# ------------------------------------ Identity -------------------------------------


@pytest.mark.parametrize("notetype", BUILTIN_NOTETYPES, ids=lambda nt: nt.name)
def test_a_builtin_name_carries_the_suffix(notetype):
  """Anki assigns no stable id to its own stock notetypes, so an incoming `Basic` is a
  different notetype under a name the target already uses, and the importer renames it to
  `Basic+`. The suffix is what keeps the name genanki2 wrote the name the user sees."""
  assert notetype.name.endswith(" (genanki2)")


def test_the_stock_ids_are_one_to_six():
  """Fixed by `scripts/dump_empty_collection_rows.py`, because Anki mints timestamp ids
  and an id that moves between releases makes every rebuilt deck import as a new one."""
  assert [notetype.id for notetype in BUILTIN_NOTETYPES] == [1, 2, 3, 4, 5, 6]


def test_there_are_six_builtins_and_the_names_are_distinct():
  """`notetypes.name` is UNIQUE, and two builtins sharing a name would collide inside a
  single package, before any import."""
  assert len(BUILTIN_NOTETYPES) == 6
  assert len({notetype.name for notetype in BUILTIN_NOTETYPES}) == 6


def test_the_tuple_holds_the_constants_themselves():
  """The constants are the reference copy, so the tuple has to be a second way to reach
  them rather than a second set of equal objects."""
  assert BUILTIN_NOTETYPES[0] is BASIC_NOTETYPE


# --------------------------------- Copies, not views ---------------------------------


def test_the_stock_name_without_the_suffix_is_not_a_builtin():
  """`Basic` is the name of Anki's own notetype, which genanki2 has no id for."""
  with pytest.raises(ValueError, match="Basic"):
    builtin_notetype("Basic")


@pytest.mark.parametrize("name", STOCK_NAMES)
def test_every_stock_name_builds(name):
  notetype = builtin_notetype(name)
  assert notetype.name == name
  assert notetype.fields
  assert notetype.templates


def test_two_calls_are_equal_and_separate():
  """The constants are shared, so the function has to be the way to get one to change.

  The constant is frozen and holds tuples where a fresh copy holds lists, so it is
  compared by content rather than with `==`.
  """
  first, second = builtin_notetype(BASIC), builtin_notetype(BASIC)
  assert first == second
  assert first.is_content_equivalent(BASIC_NOTETYPE)
  assert first is not second
  assert first is not BASIC_NOTETYPE


@pytest.mark.parametrize("attribute", ["fields", "templates"])
def test_changing_what_a_call_returns_leaves_the_constant_alone(attribute):
  """A shallow copy would share the `Field` objects, so this reaches one level in."""
  notetype = builtin_notetype(BASIC)
  getattr(notetype, attribute)[0].name = "Changed"
  assert getattr(BASIC_NOTETYPE, attribute)[0].name != "Changed"


def test_the_default_deck_is_a_copy_too():
  deck = default_deck()
  assert deck.is_content_equivalent(DEFAULT_DECK)
  assert deck is not DEFAULT_DECK
  deck.name = "Changed"
  assert DEFAULT_DECK.name == "Default"


@pytest.mark.parametrize(
  "constant",
  [*BUILTIN_NOTETYPES, DEFAULT_DECK, DEFAULT_DECK_CONFIG],
  ids=lambda constant: f"{type(constant).__name__} {constant.name}",
)
def test_the_constants_are_frozen(constant):
  assert constant.is_frozen()


def test_a_constant_refuses_changes_at_any_depth():
  """An assignment, a list it holds, and a model inside that list are all refused."""
  with pytest.raises(AttributeError, match="is frozen"):
    BASIC_NOTETYPE.name = "Changed"
  with pytest.raises(AttributeError):
    BASIC_NOTETYPE.fields.append(BASIC_NOTETYPE.fields[0])
  with pytest.raises(AttributeError, match="is frozen"):
    BASIC_NOTETYPE.fields[0].name = "Changed"


# ------------------------------------ The id hop ------------------------------------


def test_an_id_can_be_supplied():
  """A changed builtin needs an id of its own, or it merges into whatever holds the stock
  one, and it has to come from the caller so that it survives a rebuild."""
  assert builtin_notetype(BASIC, id=AN_ID).id == AN_ID


def test_supplying_an_id_changes_nothing_else():
  notetype = builtin_notetype(BASIC, id=AN_ID)
  assert notetype.model_dump(exclude={"id"}) == BASIC_NOTETYPE.model_dump(
    exclude={"id"}
  )


def test_the_supplied_id_reaches_the_field_and_template_rows():
  """The fields are looked up by the stock `ntid` and written under the new one, so the
  lookup and the write read the same value in the source and must not."""
  _, field_rows, template_rows = builtin_notetype(BASIC, id=AN_ID).to_rows()
  assert field_rows and template_rows
  assert {row["ntid"] for row in field_rows + template_rows} == {AN_ID}


def test_a_supplied_id_does_not_leak_into_the_next_call():
  """`_default_rows` is one shared dict per notetype. Writing the id into it before the
  fields are collected returns a notetype with none, and leaves every later caller,
  including the constants' own module, reading the id that one caller asked for."""
  builtin_notetype(BASIC, id=AN_ID)
  again = builtin_notetype(BASIC)
  assert again.id == 1
  assert [field.name for field in again.fields] == ["Front", "Back"]


# ------------------------------------ Sync state ------------------------------------


def test_every_builtin_declares_a_version():
  """The check reads the table by name, so a name missing from it is a `KeyError` at
  write time rather than a notetype nobody is comparing."""
  assert set(BUILTIN_NOTETYPES_VERSIONS) == {nt.name for nt in BUILTIN_NOTETYPES}


def test_the_declared_versions_are_what_the_dump_wrote():
  """The table is a second copy of the numbers `update_notetype_mtime_version` stamps
  into the rows, so that the check compares against something the dump did not produce.
  Two copies drift, and a drift means one of the two was raised on its own, which is
  either a version bump that will not reach a collection or one nobody decided on."""
  assert {row["name"]: row["mtime_secs"] for row in _default_rows.table_notetypes} == (
    BUILTIN_NOTETYPES_VERSIONS
  )


def test_the_builtins_carry_the_version_they_declare():
  """An import stores the incoming modification time verbatim and replaces the notetype
  the collection already holds only when that value is the larger of the two, so a
  version that stays put is what leaves a notetype the user restyled alone."""
  assert [nt.modification_time_s for nt in BUILTIN_NOTETYPES] == [
    BUILTIN_NOTETYPES_VERSIONS[nt.name] for nt in BUILTIN_NOTETYPES
  ]


def test_writing_a_package_refuses_a_stamped_builtin(monkeypatch, tmp_path):
  """The constants are shared, so a stamp applies to every package the process writes,
  including the ones already built. The guard runs before anything is written.

  The freeze refuses the assignment, so the stamp is written straight into `__dict__`,
  the one write that goes around the guard.
  """
  monkeypatch.setitem(BASIC_NOTETYPE.__dict__, "modification_time_s", A_TIMESTAMP)
  path = tmp_path / "deck.apkg"
  with pytest.raises(ValueError, match=re.escape(BASIC)):
    Package().write_apkg(path)
  assert not path.exists()


# ------------------------------- The rest of an empty collection -------------------------------


def test_the_config_entries_have_unique_keys():
  """`config.KEY` is the primary key, and `Collection.check_unique_config_keys` refuses a
  duplicate, so a package seeded with these could not be written."""
  entries = default_config_entries()
  assert len(entries) == 16
  assert len({entry.key for entry in entries}) == len(entries)


def test_rollover_is_not_among_the_config_entries():
  """Anki writes no `rollover` entry until the user changes it, and reads a missing one as
  4, which is why `days_elapsed_since_creation` defaults rather than requiring it."""
  assert "rollover" not in {entry.key for entry in default_config_entries()}


def test_the_reference_metadata_is_not_a_creation_stamp():
  """The dump zeroes `crt`, and a collection written with it counts every due date in days
  since 1970."""
  assert default_collection_metadata().creation_time_s == 0
