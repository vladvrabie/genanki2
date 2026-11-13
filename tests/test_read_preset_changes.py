"""Does writing back a read preset warn when an apkg import would drop the change?

    uv run --with pytest pytest tests/test_read_preset_changes.py

Anki's importer adds a preset only when the target collection has none with its id, and
otherwise keeps its own without comparing the two (`import_deck_configs` in
`rslib/src/import_export/package/apkg/import/decks.rs:44-50`, and
`add_deck_config_if_unique_undoable` in `rslib/src/deckconfig/undo.rs:47-52`). A preset
edited under the id it was read with therefore never reaches a user who imported it
before. Under a new id it is added, and the deck update moves a deck the user already
has to it (`update_normal_with_other`, `decks.rs:184-195`).
`tests/test_apkg_roundtrip.py` checks both against the anki wheel.
"""

import warnings
from collections.abc import Callable
from pathlib import Path

import pytest

from genanki2 import _default_rows
from genanki2.builtins import DEFAULT_DECK, DEFAULT_DECK_CONFIG
from genanki2.errors import Genanki2Warning
from genanki2.idgen import monotonic_id
from genanki2.models.collection import Collection
from genanki2.models.config import ConfigEntry
from genanki2.models.deck_config import DeckConfig
from genanki2.models.package import CollectionPackage, Package
from genanki2.models.serialization_context import SerializationContext

SCHEDULER_V2 = [ConfigEntry(key="schedVer", value=b"2")]
APKG = SerializationContext(output=SerializationContext.Output.APKG)
MATCH = "keeps the preset"


def anki_preset() -> DeckConfig:
  """Preset 1 read from the row Anki wrote for it."""
  return DeckConfig.from_row(dict(_default_rows.table_deck_config[0]))


def read_back(tmp_path: Path) -> Collection:
  """Write a collection holding the default preset and read it back, as last release's
  file."""
  path = tmp_path / "collection.anki2"
  with warnings.catch_warnings():
    warnings.simplefilter("ignore", Genanki2Warning)
    Collection(
      deck_configs=[DEFAULT_DECK_CONFIG], decks=[DEFAULT_DECK], config=SCHEDULER_V2
    ).write_sqlite(path)
    return Collection.read_sqlite(path)


def change_warnings(
  preset: DeckConfig, context: SerializationContext | None = APKG
) -> list[str]:
  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always", Genanki2Warning)
    preset.warn_unimported_changes(context)
  return [str(w.message) for w in caught]


# ------------------------------ What counts as a change -------------------------------


def test_a_preset_anki_wrote_is_unchanged_after_the_read():
  assert change_warnings(anki_preset()) == []


def test_a_preset_genanki2_wrote_is_unchanged_after_the_read(tmp_path):
  [preset] = read_back(tmp_path).deck_configs
  assert change_warnings(preset) == []


def test_a_preset_built_in_code_is_never_compared():
  """It was never read, so there is nothing to compare against."""
  preset = DeckConfig(name="Study")
  preset.config.new_per_day = 30
  assert change_warnings(preset) == []


def rename(preset: DeckConfig) -> None:
  preset.name = "Renamed"


def set_new_per_day(preset: DeckConfig) -> None:
  preset.config.new_per_day = 30


def append_learning_step(preset: DeckConfig) -> None:
  preset.config.learn_steps.append(60.0)


@pytest.mark.parametrize(
  "edit",
  [
    pytest.param(rename, id="name"),
    pytest.param(set_new_per_day, id="blob"),
    pytest.param(append_learning_step, id="list-mutated-in-place"),
  ],
)
def test_an_edit_under_the_read_id_warns(edit: Callable[[DeckConfig], None]):
  """The importer drops the whole incoming preset, so a rename is lost as well."""
  preset = anki_preset()
  edit(preset)
  [message] = change_warnings(preset)
  assert message.startswith('DeckConfig: "')
  assert f"(id {preset.id})" in message


def test_the_time_and_the_sync_marker_do_not_count():
  """They are not content, and the importer stamps its own sync marker anyway."""
  preset = anki_preset()
  preset.modification_time_s = 1_600_000_000
  preset.update_sequence_n = 5
  assert change_warnings(preset) == []


def test_an_edit_under_a_new_id_does_not_warn():
  """A new id is the fix the message names: the importer adds the preset."""
  preset = anki_preset()
  preset.config.new_per_day = 30
  preset.id = monotonic_id()
  assert change_warnings(preset) == []


def test_a_copy_under_a_new_id_does_not_warn():
  """`model_copy()` carries the read id along, so the copy is compared with it."""
  preset = anki_preset()
  copy = preset.model_copy(update={"id": monotonic_id()}, deep=True)
  copy.config.new_per_day = 30
  assert change_warnings(copy) == []


# ------------------------------ What the write produces -------------------------------


@pytest.mark.parametrize(
  ("write", "expected"),
  [
    pytest.param(
      lambda col, d: Package(collection=col).write_apkg(d / "deck.apkg"),
      "An apkg import keeps the preset",
      id="apkg",
    ),
    pytest.param(
      lambda col, d: CollectionPackage(collection=col).write_colpkg(
        d / "collection.colpkg"
      ),
      None,
      id="colpkg",
    ),
    pytest.param(
      lambda col, d: col.write_sqlite(d / "rewritten.anki2"),
      "If imported from an apkg, the import keeps the preset",
      id="sqlite",
    ),
    pytest.param(
      lambda col, d: col.warn(),
      "If imported from an apkg, the import keeps the preset",
      id="no context",
    ),
  ],
)
def test_a_changed_preset_is_reported_for_what_the_write_produces(
  tmp_path, write, expected
):
  """A restore replaces the user's presets outright, so nothing is dropped."""
  collection = read_back(tmp_path)
  collection.deck_configs[0].config.new_per_day = 30
  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always", Genanki2Warning)
    write(collection, tmp_path)

  messages = [str(w.message) for w in caught if MATCH in str(w.message)]
  if expected is None:
    assert messages == []
  else:
    assert len(messages) == 1
    assert expected in messages[0]
