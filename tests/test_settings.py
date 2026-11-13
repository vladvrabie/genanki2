"""Do the settings switch off what genanki2 runs automatically, and only that?

    uv run --with pytest pytest tests/test_settings.py

Read-update-write needs a way past a genanki2 check that rejects something Anki accepts.
The check and warning switches are read at the entry point a caller invokes, so an
explicit `check()` or `warn()` still runs inside a block, and each switch leaves the other
alone.

The repair switch covers the four repairs Anki makes only in its database check
(`rslib/src/dbcheck.rs`). It is read by the validators and by `Card.prepare()`, never by
the repair methods, so it has to wrap the read, and an explicit repair still repairs.
"""

import asyncio
import re
import threading
import warnings
from pathlib import Path

import pytest

from genanki2 import (
  checks_disabled,
  checks_enabled,
  dbcheck_repairs_disabled,
  dbcheck_repairs_enabled,
  warnings_disabled,
  warnings_enabled,
)
from genanki2.builtins import builtin_notetype, default_deck
from genanki2.errors import Genanki2Error, Genanki2Warning
from genanki2.models.card import Card, CardQueue, CardType
from genanki2.models.collection import Collection
from genanki2.models.config import ConfigEntry
from genanki2.models.grave import Grave, GraveType
from genanki2.models.note import Note
from genanki2.models.review import Review
from genanki2.sqlite import AnkiSQLite
from genanki2.time import time_s

DOCS = Path(__file__).parent.parent / "docs"
REPAIRS_DOC = DOCS / "reference" / "for-contributors" / "repairs.md"

# The repairs `dbcheck_repairs_disabled()` governs, as the reference doc names them.
DBCHECK_REPAIRS = (
  "Card.wrap_due",
  "Card.clear_unexplained_original_due",
  "Card.clear_unexplained_original_data",
  "Card.clamp_template_ord",
  "Card.clamp_interval_days",
  "Note.clamp_modification_time",
  "Review.clamp_time",
)

A_POSITION_PAST_THE_CEILING = 2_500_005
THE_SAME_POSITION_WRAPPED = 1_500_005


def a_rejected_grave() -> Grave:
  """A grave deleting deck 1, which `Grave.check()` refuses."""
  return Grave(object_id=1, grave_type=GraveType.DECK)


def a_note(**overrides) -> Note:
  return Note(
    notetype=builtin_notetype("Basic (genanki2)"), fields=["front", "back"], **overrides
  )


def a_card(**overrides) -> Card:
  return Card(note=a_note(), deck=default_deck(), template_ord=0, **overrides)


def a_warned_entry() -> ConfigEntry:
  """A config entry whose value is not JSON, which `ConfigEntry.warn()` reports."""
  return ConfigEntry(key="curDeck", value=b"not json")


# ------------------------------------------ Checks ------------------------------------------


def test_a_write_runs_the_checks_by_default():
  with pytest.raises(Genanki2Error, match="deck 1"):
    a_rejected_grave().to_row()


def test_a_write_inside_checks_disabled_skips_the_checks():
  with checks_disabled():
    assert a_rejected_grave().to_row()["oid"] == 1


def test_an_explicit_check_still_runs_inside_checks_disabled():
  with checks_disabled(), pytest.raises(Genanki2Error, match="deck 1"):
    a_rejected_grave().check()


def test_checks_enabled_inside_checks_disabled_checks_again():
  with checks_disabled(), checks_enabled(), pytest.raises(Genanki2Error):
    a_rejected_grave().to_row()


def test_the_checks_come_back_after_the_block_raises():
  with pytest.raises(RuntimeError), checks_disabled():
    raise RuntimeError
  with pytest.raises(Genanki2Error):
    a_rejected_grave().to_row()


def test_preparation_still_runs_inside_checks_disabled(tmp_path):
  """Collecting fills the lists the writer writes from, so skipping it would drop the
  note, the notetype and the deck the card is the only way to reach."""
  note = Note(notetype=builtin_notetype("Basic (genanki2)"), fields=["front", "back"])
  card = Card(note=note, deck=default_deck(), template_ord=0)
  path = tmp_path / "collection.anki2"
  with checks_disabled():
    Collection(cards=[card]).write_sqlite(path)
  assert [read.id for read in Collection.read_sqlite(path).notes] == [note.id]


def test_a_thread_started_inside_the_block_runs_the_checks():
  raised: list[BaseException] = []

  def write() -> None:
    try:
      a_rejected_grave().to_row()
    except Genanki2Error as error:
      raised.append(error)

  with checks_disabled():
    thread = threading.Thread(target=write)
    thread.start()
    thread.join()
  assert len(raised) == 1


def test_a_task_created_inside_the_block_skips_the_checks():
  async def write() -> int:
    return a_rejected_grave().to_row()["oid"]

  with checks_disabled():
    assert asyncio.run(write()) == 1


# ----------------------------------------- Warnings -----------------------------------------


def test_a_write_emits_the_warnings_by_default():
  with pytest.warns(Genanki2Warning, match="valid JSON"):
    a_warned_entry().to_row()


def test_a_write_inside_warnings_disabled_emits_none():
  with warnings.catch_warnings():
    warnings.simplefilter("error", Genanki2Warning)
    with warnings_disabled():
      a_warned_entry().to_row()


def test_an_explicit_warn_still_warns_inside_warnings_disabled():
  with warnings_disabled(), pytest.warns(Genanki2Warning, match="valid JSON"):
    a_warned_entry().warn()


def test_warnings_enabled_inside_warnings_disabled_warns_again():
  with (
    warnings_disabled(),
    warnings_enabled(),
    pytest.warns(Genanki2Warning, match="valid JSON"),
  ):
    a_warned_entry().to_row()


# ---------------------------------------- Independence --------------------------------------


def test_disabling_the_checks_leaves_the_warnings_on():
  """A caller forcing a write past the checks still hears what it forced past."""
  with checks_disabled(), pytest.warns(Genanki2Warning, match="valid JSON"):
    a_warned_entry().to_row()


def test_disabling_the_warnings_leaves_the_checks_on():
  with warnings_disabled(), pytest.raises(Genanki2Error, match="deck 1"):
    a_rejected_grave().to_row()


# ---------------------------------- Database-check repairs ----------------------------------


def test_the_opt_out_repairs_in_the_reference_doc_are_the_governed_set():
  """The doc's table is where a caller learns what the switch leaves alone."""
  rows = re.findall(
    r"^\| `([\w.]+)` +\|.*\| opt-out +\|", REPAIRS_DOC.read_text(), re.MULTILINE
  )
  assert tuple(rows) == DBCHECK_REPAIRS


@pytest.mark.parametrize("name", DBCHECK_REPAIRS)
def test_each_governed_repair_exists(name):
  owner, method = name.split(".")
  assert callable(
    getattr({"Card": Card, "Note": Note, "Review": Review}[owner], method)
  )


def test_a_new_card_position_is_wrapped_by_default():
  assert a_card(due=A_POSITION_PAST_THE_CEILING).due == THE_SAME_POSITION_WRAPPED


def test_a_new_card_position_is_kept_inside_dbcheck_repairs_disabled():
  with dbcheck_repairs_disabled():
    card = a_card(due=A_POSITION_PAST_THE_CEILING)
  assert card.due == A_POSITION_PAST_THE_CEILING


def test_an_original_due_without_an_original_deck_is_kept_inside_the_block():
  with dbcheck_repairs_disabled():
    card = a_card(type=CardType.LEARNING, odue=42, odid=0)
  assert card.original_due == 42


def test_an_original_pair_in_a_normal_deck_is_kept_inside_the_block():
  with dbcheck_repairs_disabled():
    card = a_card(type=CardType.LEARNING, odue=42, odid=1_234)
  assert (card.original_deck, card.original_due) == (1_234, 42)


def test_a_future_note_modification_time_is_kept_inside_the_block():
  future = time_s() + 86_400
  with dbcheck_repairs_disabled():
    note = a_note(mod=future)
  assert note.modification_time_s == future


def test_a_kept_future_note_modification_time_warns_on_write():
  with dbcheck_repairs_disabled():
    note = a_note(mod=time_s() + 86_400)
  with pytest.warns(Genanki2Warning, match="was not repaired"):
    note.to_row()


def test_a_kept_future_note_modification_time_is_silenced_by_warnings_disabled():
  with dbcheck_repairs_disabled():
    note = a_note(mod=time_s() + 86_400)
  with warnings.catch_warnings():
    warnings.simplefilter("error", Genanki2Warning)
    with warnings_disabled():
      note.to_row()


def test_a_repaired_note_modification_time_warns_once():
  """The repair reports the change, and the write finds nothing left to report."""
  with pytest.warns(Genanki2Warning, match="was corrected"):
    note = a_note(mod=time_s() + 86_400)
  with warnings.catch_warnings():
    warnings.simplefilter("error", Genanki2Warning)
    note.to_row()


def test_an_explicit_note_repair_still_repairs_inside_the_block():
  with dbcheck_repairs_disabled():
    note = a_note(mod=time_s() + 86_400)
    with pytest.warns(Genanki2Warning, match="was corrected"):
      note.repair_modification_time()
  assert note.modification_time_s <= time_s()


def a_kept_ceiling_card() -> Card:
  return a_card(due=A_POSITION_PAST_THE_CEILING)


def a_kept_original_due_card() -> Card:
  return a_card(type=CardType.LEARNING, odue=42, odid=0)


def a_kept_original_pair_card() -> Card:
  return a_card(type=CardType.LEARNING, odue=42, odid=1_234)


KEPT_CARDS = [
  pytest.param(a_kept_ceiling_card, "position .* was not repaired", id="due"),
  pytest.param(a_kept_original_due_card, "original due 42 should be", id="odue"),
  pytest.param(a_kept_original_pair_card, "original deck 1234 .* should", id="pair"),
]


@pytest.mark.parametrize(("build", "message"), KEPT_CARDS)
def test_a_kept_card_value_warns_on_write(build, message):
  with dbcheck_repairs_disabled():
    card = build()
    with pytest.warns(Genanki2Warning, match=message):
      card.to_row()


@pytest.mark.parametrize(("build", "message"), KEPT_CARDS)
def test_a_kept_card_value_is_silenced_by_warnings_disabled(build, message):
  with dbcheck_repairs_disabled():
    card = build()
    with warnings.catch_warnings(), warnings_disabled():
      warnings.simplefilter("error", Genanki2Warning)
      card.to_row()


@pytest.mark.parametrize(("build", "message"), KEPT_CARDS)
def test_a_repaired_card_value_warns_once(build, message):
  """The repair reports the change, and the write finds nothing left to report."""
  with pytest.warns(Genanki2Warning):
    card = build()
  with warnings.catch_warnings():
    warnings.simplefilter("error", Genanki2Warning)
    card.to_row()


def test_an_assignment_inside_the_block_is_kept():
  card = a_card()
  with dbcheck_repairs_disabled():
    card.due = A_POSITION_PAST_THE_CEILING
  assert card.due == A_POSITION_PAST_THE_CEILING


def test_an_assignment_after_the_block_repairs_what_the_block_kept():
  """`validate_assignment` re-runs every model validator, whichever field is assigned,
  so an update made outside the block repairs the whole card."""
  with dbcheck_repairs_disabled():
    card = a_card(due=A_POSITION_PAST_THE_CEILING)
  card.flags = 1
  assert card.due == THE_SAME_POSITION_WRAPPED


def test_a_write_inside_the_block_keeps_the_original_pair():
  """`Card.prepare()` reads the switch, so the write-time run of the repair is skipped."""
  with dbcheck_repairs_disabled():
    row = a_card(type=CardType.LEARNING, odue=42, odid=1_234).to_row()
  assert (row["odid"], row["odue"]) == (1_234, 42)


def test_a_write_outside_the_block_clears_the_original_pair():
  with dbcheck_repairs_disabled():
    card = a_card(type=CardType.LEARNING, odue=42, odid=1_234)
  row = card.to_row()
  assert (row["odid"], row["odue"]) == (0, 0)


def test_a_read_inside_the_block_keeps_what_the_file_holds(tmp_path):
  path = tmp_path / "collection.anki2"
  with dbcheck_repairs_disabled():
    Collection(cards=[a_card(due=A_POSITION_PAST_THE_CEILING)]).write_sqlite(path)
    kept = Collection.read_sqlite(path).cards[0]
  repaired = Collection.read_sqlite(path).cards[0]
  assert (kept.due, repaired.due) == (
    A_POSITION_PAST_THE_CEILING,
    THE_SAME_POSITION_WRAPPED,
  )


def test_an_explicit_repair_still_repairs_inside_the_block():
  with dbcheck_repairs_disabled():
    card = a_card(due=A_POSITION_PAST_THE_CEILING).repair_due_above_ceiling()
  assert card.due == THE_SAME_POSITION_WRAPPED


def test_dbcheck_repairs_enabled_inside_the_block_repairs_again():
  with dbcheck_repairs_disabled(), dbcheck_repairs_enabled():
    assert a_card(due=A_POSITION_PAST_THE_CEILING).due == THE_SAME_POSITION_WRAPPED


def test_disabling_the_repairs_leaves_the_checks_on():
  """A review card holding a position above the day threshold is refused either way;
  the wrap only moves it, and never below the threshold."""
  with dbcheck_repairs_disabled():
    card = a_card(queue=CardQueue.REVIEW, due=A_POSITION_PAST_THE_CEILING)
    with pytest.raises(Genanki2Error, match="counts due in days"):
      card.to_row()


def a_file_holding_every_kept_value(path: Path) -> Path:
  """A collection carrying one value for each of the four database-check repairs."""
  learning = {"type": CardType.LEARNING, "queue": CardQueue.LEARNING, "due": 100}
  with dbcheck_repairs_disabled(), checks_disabled(), warnings_disabled():
    future_note = a_note(modification_time_s=time_s() + 1_000_000)
    cards = [
      Card(note=future_note, deck=default_deck(), template_ord=0),
      a_card(due=A_POSITION_PAST_THE_CEILING),
      a_card(**learning, odue=42),
      a_card(**learning, odue=42, odid=1_234),
    ]
    Collection(cards=cards).write_sqlite(path)
  return path


def changed_tables(source: Path, target: Path) -> list[AnkiSQLite.Table]:
  def rows(path: Path):
    with AnkiSQLite(path) as db:
      return {table: db.read_table(table) for table in AnkiSQLite.Table}

  before, after = rows(source), rows(target)
  return [table for table in AnkiSQLite.Table if before[table] != after[table]]


def test_a_read_and_write_inside_both_blocks_reproduces_every_row(tmp_path):
  source = a_file_holding_every_kept_value(tmp_path / "source.anki2")
  with dbcheck_repairs_disabled(), warnings_disabled():
    Collection.read_sqlite(source).write_sqlite(tmp_path / "target.anki2")
  assert changed_tables(source, tmp_path / "target.anki2") == []


def test_the_same_trip_outside_the_blocks_repairs_notes_and_cards(tmp_path):
  """Without this, the test above would pass on a file holding nothing to repair."""
  source = a_file_holding_every_kept_value(tmp_path / "source.anki2")
  with warnings.catch_warnings():
    warnings.simplefilter("ignore", Genanki2Warning)
    Collection.read_sqlite(source).write_sqlite(tmp_path / "target.anki2")
  assert changed_tables(source, tmp_path / "target.anki2") == [
    AnkiSQLite.Table.NOTES,
    AnkiSQLite.Table.CARDS,
  ]
