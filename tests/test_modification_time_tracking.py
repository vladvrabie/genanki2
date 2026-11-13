"""Does a note or notetype read and changed carry a time Anki's importer takes as newer?

    uv run --with anki --with pytest pytest tests/test_modification_time_tracking.py

Anki updates an existing note on import only when the incoming one is newer
(`should_update`, `rslib/src/import_export/package/apkg/import/notes.rs`), and stamps a
note it saves only when its content differs from the stored copy, ignoring the sort
field, the checksum and the time (`note_differs_from_db`, `rslib/src/notes/mod.rs`). A
note read with `from_row()` does the same: it is stamped with the current time when it
is written or frozen with content changed since the read. An unchanged note keeps its
time, so a user's own edit of it survives the next import. A notetype is tracked the
same way, across its own row, its fields and its templates (`notes.rs:173` for the
import, `set_modified` in `rslib/src/notetype/mod.rs` for the stamp).

Freezing prepares a model before locking it, and the phases skip a frozen model, so a
frozen model is written as it was when frozen.

anki is a test-only dependency, never a dependency of genanki2.
"""

import warnings
from pathlib import Path

import pytest
from anki.collection import Collection as AnkiCollection
from anki.collection import ImportAnkiPackageRequest

from genanki2.builtins import (
  BASIC_NOTETYPE,
  DEFAULT_DECK,
  DEFAULT_DECK_CONFIG,
  builtin_notetype,
  default_config_entries,
)
from genanki2.errors import Genanki2Warning
from genanki2.models.card import Card
from genanki2.models.collection import Collection
from genanki2.models.deck import Deck, DeckFilteredKind, DeckNormalKind
from genanki2.models.note import Note
from genanki2.models.notetype import Notetype
from genanki2.models.package import CollectionPackage, Package
from genanki2.models.serialization_context import SerializationContext
from genanki2.settings import timestamping_disabled
from genanki2.time import time_s

READ_TIME = 1_700_000_000
NOW = 1_800_000_000


@pytest.fixture
def clock(monkeypatch) -> list[int]:
  """The current time the tracking and the clamp read, set by assigning `clock[0]`."""
  now = [NOW]
  monkeypatch.setattr("genanki2.models.base.time_s", lambda: now[0])
  monkeypatch.setattr("genanki2.models.note.time_s", lambda: now[0])
  monkeypatch.setattr("genanki2.models.notetype.time_s", lambda: now[0])
  return now


def a_notetype() -> Notetype:
  return builtin_notetype("Basic (genanki2)", id=1_700_000_000_000)


def a_read_note(modification_time_s: int = READ_TIME) -> Note:
  """A note read back from the row a note with this time writes."""
  notetype = a_notetype()
  row = Note(
    notetype=notetype,
    fields=["front", "back"],
    tags=["tag"],
    modification_time_s=modification_time_s,
  ).to_row()
  return Note.from_row(row, notetype)


def a_read_notetype(modification_time_s: int = READ_TIME) -> Notetype:
  """A notetype read back from the rows a notetype with this time writes."""
  notetype = a_notetype()
  notetype.modification_time_s = modification_time_s
  return Notetype.from_rows(*notetype.to_rows())


# ------------------------------------ Written notes -----------------------------------


@pytest.mark.filterwarnings("error")
def test_a_note_read_and_written_unchanged_keeps_its_time(clock):
  assert a_read_note().to_row()["mod"] == READ_TIME


@pytest.mark.parametrize(
  "change",
  [
    pytest.param(lambda note: note.fields.__setitem__(0, "edited"), id="field"),
    pytest.param(lambda note: setattr(note, "tags", ["other"]), id="tags"),
    pytest.param(lambda note: setattr(note, "guid", "another guid"), id="guid"),
  ],
)
def test_a_note_changed_after_the_read_is_written_with_the_current_time(clock, change):
  note = a_read_note()
  change(note)
  assert note.to_row()["mod"] == NOW


def test_a_change_to_the_sync_marker_alone_keeps_the_time(clock):
  """Anki stamps the marker on every write, so it says nothing about the content."""
  note = a_read_note()
  note.update_sequence_n = 5
  assert note.to_row()["mod"] == READ_TIME


def test_a_time_set_by_the_caller_is_kept(clock):
  note = a_read_note()
  note.fields[0] = "edited"
  note.modification_time_s = READ_TIME + 10
  assert note.to_row()["mod"] == READ_TIME + 10


@pytest.mark.filterwarnings("error")
def test_a_note_built_in_python_keeps_its_time(clock):
  """With no read to compare against, a note is never taken as changed."""
  note = Note(
    notetype=a_notetype(), fields=["front", "back"], modification_time_s=READ_TIME
  )
  note.fields[0] = "edited"
  assert not note.is_changed_since_read()
  assert note.to_row()["mod"] == READ_TIME


def test_a_note_written_twice_is_stamped_once(clock):
  note = a_read_note()
  note.fields[0] = "edited"
  note.to_row()
  clock[0] = NOW + 5
  assert note.to_row()["mod"] == NOW


def test_an_edit_after_a_write_is_stamped_by_the_next_write(clock):
  """Users who imported the first write hold its stamp, so the second edit needs a
  later one to reach them."""
  note = a_read_note()
  note.fields[0] = "first edit"
  assert note.to_row()["mod"] == NOW
  clock[0] = NOW + 5
  note.fields[0] = "second edit"
  assert note.to_row()["mod"] == NOW + 5


@pytest.mark.filterwarnings("error")
def test_a_note_built_in_python_is_stamped_for_an_edit_after_its_first_write(clock):
  """The first write takes the construction time and content as the last stamp."""
  note = Note(
    notetype=a_notetype(), fields=["front", "back"], modification_time_s=READ_TIME
  )
  assert note.to_row()["mod"] == READ_TIME
  note.fields[0] = "edited"
  assert note.to_row()["mod"] == NOW


def test_a_later_time_set_by_the_caller_becomes_the_last_stamp(clock):
  """The caller's time is kept for its write, and the next edit is stamped after it."""
  note = a_read_note()
  note.fields[0] = "first edit"
  note.modification_time_s = READ_TIME + 10
  assert note.to_row()["mod"] == READ_TIME + 10
  note.fields[0] = "second edit"
  assert note.to_row()["mod"] == NOW


# ------------------------------- Not importable changes -------------------------------


def test_a_change_made_in_the_second_of_the_read_warns(clock):
  """The current time equals the read time, so the import keeps the user's copy."""
  note = a_read_note(modification_time_s=NOW)
  note.fields[0] = "edited"
  with pytest.warns(Genanki2Warning):
    row = note.to_row()
  assert row["mod"] == NOW


def test_a_change_made_in_the_second_of_the_last_stamp_warns(clock):
  """The first write stamped the current second, so a second edit in that second
  cannot be newer, and a user who imported the first write keeps it."""
  note = a_read_note()
  note.fields[0] = "first edit"
  note.to_row()
  note.fields[0] = "second edit"
  with pytest.warns(Genanki2Warning, match="Note: changed since its last stamp"):
    row = note.to_row()
  assert row["mod"] == NOW


def test_a_change_with_a_time_set_before_the_read_warns(clock):
  note = a_read_note()
  note.fields[0] = "edited"
  note.modification_time_s = READ_TIME - 1
  with pytest.warns(Genanki2Warning):
    note.to_row()


def a_collection_with_a_note_changed_in_the_second_of_the_read() -> Collection:
  note = a_read_note(modification_time_s=NOW)
  note.fields[0] = "edited"
  return Collection(
    config=default_config_entries(),
    deck_configs=[DEFAULT_DECK_CONFIG],
    notes=[note],
    cards=[Card(note=note, deck=DEFAULT_DECK)],
  )


APKG_MESSAGE = "An apkg import keeps"
GENERIC_MESSAGE = "If imported from an apkg, the import keeps"
COLPKG_CONTEXT = SerializationContext(output=SerializationContext.Output.COLPKG)


@pytest.mark.parametrize(
  ("write", "expected"),
  [
    pytest.param(
      lambda col, d: Package(collection=col).write_apkg(d / "deck.apkg"),
      APKG_MESSAGE,
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
      lambda col, d: Package(collection=col).write_apkg(
        d / "deck.apkg", context=COLPKG_CONTEXT
      ),
      APKG_MESSAGE,
      id="apkg given a colpkg context",
    ),
    pytest.param(lambda col, d: col.notes[0].to_row(), GENERIC_MESSAGE, id="row"),
    pytest.param(lambda col, d: col.notes[0].warn(), GENERIC_MESSAGE, id="no context"),
  ],
)
def test_a_change_the_import_would_skip_is_reported_for_what_the_write_produces(
  clock, tmp_path, write, expected
):
  """A colpkg restore replaces the collection, so no time decides anything there.

  Through the package writers, the context has to cross the package, the collection
  and the note's own override to reach `TimestampedRowModel.warn()`.
  """
  collection = a_collection_with_a_note_changed_in_the_second_of_the_read()
  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always", Genanki2Warning)
    write(collection, tmp_path)

  messages = [
    str(w.message)
    for w in caught
    if str(w.message).startswith("Note: changed since its last stamp")
  ]
  if expected is None:
    assert messages == []
  else:
    assert len(messages) == 1
    assert expected in messages[0]


# ------------------------------------ Frozen notes ------------------------------------


def test_freezing_a_changed_note_stamps_it_and_a_later_write_keeps_the_stamp(clock):
  note = a_read_note()
  note.fields[0] = "edited"
  note.freeze()
  assert note.modification_time_s == NOW
  clock[0] = NOW + 5
  assert note.to_row()["mod"] == NOW


@pytest.mark.filterwarnings("error")
def test_a_frozen_note_read_and_unchanged_is_written_with_its_time(clock):
  note = a_read_note().freeze()
  clock[0] = NOW + 5
  assert note.to_row()["mod"] == READ_TIME


def test_a_note_frozen_in_the_second_of_the_read_is_written_later_with_a_warning(clock):
  """The stamp at freeze time cannot be newer, and a later write cannot change it."""
  note = a_read_note(modification_time_s=NOW)
  note.fields[0] = "edited"
  note.freeze()
  clock[0] = NOW + 5
  with pytest.warns(Genanki2Warning):
    row = note.to_row()
  assert row["mod"] == NOW


def test_freezing_twice_is_harmless(clock):
  note = a_read_note()
  note.fields[0] = "edited"
  note.freeze()
  clock[0] = NOW + 5
  note.freeze()
  assert note.modification_time_s == NOW


# ------------------------------------- Notetypes --------------------------------------


@pytest.mark.filterwarnings("error")
def test_a_notetype_read_and_written_unchanged_keeps_its_time(clock):
  assert a_read_notetype().to_rows()[0]["mtime_secs"] == READ_TIME


@pytest.mark.parametrize(
  "change",
  [
    pytest.param(lambda nt: setattr(nt.config, "css", "x"), id="notetype"),
    pytest.param(lambda nt: setattr(nt.fields[0].config, "sticky", True), id="field"),
    pytest.param(lambda nt: setattr(nt.templates[0], "name", "Other"), id="template"),
    pytest.param(lambda nt: nt.fields.reverse(), id="field order"),
  ],
)
def test_a_notetype_changed_after_the_read_is_written_with_the_current_time(
  clock, change
):
  notetype = a_read_notetype()
  change(notetype)
  assert notetype.to_rows()[0]["mtime_secs"] == NOW


def test_a_changed_template_keeps_its_own_time(clock):
  """Anki sets only the notetype's time when it saves an edit (`set_modified`)."""
  notetype = a_read_notetype()
  read_template_time = notetype.templates[0].modification_time_s
  notetype.templates[0].config.q_format += "<br>"
  _, _, template_rows = notetype.to_rows()
  assert template_rows[0]["mtime_secs"] == read_template_time


def test_a_change_to_a_template_time_alone_keeps_the_notetype_time(clock):
  notetype = a_read_notetype()
  notetype.templates[0].modification_time_s = READ_TIME + 10
  assert notetype.to_rows()[0]["mtime_secs"] == READ_TIME


def test_a_notetype_changed_in_the_second_of_the_read_warns(clock):
  notetype = a_read_notetype(modification_time_s=NOW)
  notetype.config.css = "x"
  with pytest.warns(Genanki2Warning, match="Notetype: changed since its last stamp"):
    notetype.to_rows()


# --------------------------------- Built-in notetypes ---------------------------------


@pytest.mark.filterwarnings("error")
def test_a_stock_notetype_unchanged_keeps_the_stock_time(clock):
  """The stock time 0 never overwrites a copy a user already has."""
  assert builtin_notetype("Basic (genanki2)").to_rows()[0]["mtime_secs"] == 0
  assert BASIC_NOTETYPE.to_rows()[0]["mtime_secs"] == 0


@pytest.mark.parametrize(
  "build",
  [
    pytest.param(lambda: builtin_notetype("Basic (genanki2)", id=7), id="own id"),
    pytest.param(
      lambda: builtin_notetype("Basic (genanki2)").model_copy(update={"name": "Mine"}),
      id="renamed",
    ),
  ],
)
def test_a_customized_stock_notetype_is_written_with_the_current_time(clock, build):
  """The stock rows are the read, so a customization is a change since it."""
  notetype = build()
  assert notetype.is_changed_since_read()
  assert notetype.to_rows()[0]["mtime_secs"] == NOW


# ------------------------------- Timestamping disabled --------------------------------


def test_a_changed_note_written_with_timestamping_disabled_keeps_its_time_and_warns(
  clock,
):
  note = a_read_note()
  note.fields[0] = "edited"
  with timestamping_disabled(), pytest.warns(Genanki2Warning):
    row = note.to_row()
  assert row["mod"] == READ_TIME


def test_a_note_frozen_with_timestamping_disabled_keeps_its_time_when_written_later(
  clock,
):
  """Freezing is the step that sets the time, so the setting at freeze time applies."""
  note = a_read_note()
  note.fields[0] = "edited"
  with timestamping_disabled():
    note.freeze()
  with pytest.warns(Genanki2Warning):
    row = note.to_row()
  assert row["mod"] == READ_TIME


def test_an_explicit_update_sets_the_time_with_timestamping_disabled(clock):
  note = a_read_note()
  with timestamping_disabled():
    note.update_modification_time()
  assert note.modification_time_s == NOW


# ----------------------------- Freezing prepares a model ------------------------------


def test_a_card_frozen_after_its_deck_turned_normal_is_repaired_by_the_freeze():
  """The repair runs inside `prepare`, which the write skips on a frozen card."""
  deck = Deck(name="Filtered", kind=DeckFilteredKind())
  card = Card(
    note=Note(notetype=a_notetype(), fields=["front", "back"]),
    deck=deck,
    due=-100_000,
    original_deck=Deck(name="Home"),
    original_due=7,
  )
  deck.kind = DeckNormalKind()
  with pytest.warns(Genanki2Warning, match="original deck"):
    card.freeze()
  with warnings.catch_warnings():
    warnings.simplefilter("error")
    row = card.to_row()
  assert (row["odid"], row["odue"]) == (0, 0)


def test_a_frozen_collection_holds_the_models_its_notes_reference():
  note = Note(notetype=a_notetype(), fields=["front", "back"])
  collection = Collection(notes=[note]).freeze()
  assert [notetype.id for notetype in collection.notetypes] == [note.notetype.id]


# --------------------------------- Importing into Anki --------------------------------


def a_package(path: Path) -> Path:
  """Two notes last changed an hour ago, each with a card in `Default`."""
  notetype = a_notetype()
  hour_ago = time_s() - 3600
  notes = [
    Note(
      notetype=notetype,
      fields=[front, "back"],
      tags=[front],
      modification_time_s=hour_ago,
    )
    for front in ("edited", "untouched")
  ]
  collection = Collection(
    config=default_config_entries(),
    deck_configs=[DEFAULT_DECK_CONFIG],
    notes=notes,
    cards=[Card(note=note, deck=DEFAULT_DECK) for note in notes],
  )
  Package(collection=collection).write_apkg(path)
  return path


def front_of(col: AnkiCollection, tag: str) -> str:
  (note_id,) = col.find_notes(f"tag:{tag}")
  return col.get_note(note_id)["Front"]


def test_an_import_takes_the_changed_note_and_keeps_the_users_edit_of_the_other(
  tmp_path,
):
  """The user edits one note in Anki, the author edits the other and ships again."""
  first = a_package(tmp_path / "first.apkg")
  target = AnkiCollection(str(tmp_path / "target.anki2"))
  target.import_anki_package(ImportAnkiPackageRequest(package_path=str(first)))
  (note_id,) = target.find_notes("tag:untouched")
  users_note = target.get_note(note_id)
  users_note["Front"] = "the user's edit"
  target.update_note(users_note)

  package = Package.read_apkg(first, extract_dir=tmp_path / "media")
  (edited,) = (note for note in package.collection.notes if "edited" in note.tags)
  edited.fields[1] = "the author's edit"
  second = tmp_path / "second.apkg"
  package.write_apkg(second)
  target.import_anki_package(ImportAnkiPackageRequest(package_path=str(second)))

  (author_id,) = target.find_notes("tag:edited")
  assert target.get_note(author_id)["Back"] == "the author's edit"
  assert front_of(target, "untouched") == "the user's edit"
  target.close()


# ----------------------------- Cards an edit makes render -----------------------------

OPTIONAL_REVERSED = "Basic (optional reversed card) (genanki2)"


def an_optional_reversed_package(path: Path) -> Path:
  """One note with an empty `Add Reverse` and its front card, changed an hour ago."""
  hour_ago = time_s() - 3600
  notetype = builtin_notetype(OPTIONAL_REVERSED, id=1_700_000_000_001)
  notetype.modification_time_s = hour_ago
  note = Note(
    notetype=notetype,
    fields=["casa", "house", ""],
    tags=["casa"],
    modification_time_s=hour_ago,
  )
  collection = Collection(
    config=default_config_entries(),
    deck_configs=[DEFAULT_DECK_CONFIG],
    notes=[note],
    cards=[Card(note=note, deck=DEFAULT_DECK)],
  )
  Package(collection=collection).write_apkg(path)
  return path


def imported(tmp_path: Path) -> tuple[AnkiCollection, Path]:
  """A collection that imported the first version, and that version's path."""
  first = an_optional_reversed_package(tmp_path / "first.apkg")
  target = AnkiCollection(str(tmp_path / "target.anki2"))
  target.import_anki_package(ImportAnkiPackageRequest(package_path=str(first)))
  return target, first


def test_a_field_filled_for_a_new_card_is_imported_with_it(tmp_path):
  """The field is a change to the note, so the import takes it along with the card.

  Without the stamp the import adds the card and skips the note
  (`maybe_update_existing_note`, `notes.rs:415`), leaving the card's front empty.
  """
  target, first = imported(tmp_path)
  package = Package.read_apkg(first, extract_dir=tmp_path / "media")
  (note,) = package.collection.notes
  note["Add Reverse"] = "y"
  package.collection.generate_missing_cards()
  second = tmp_path / "second.apkg"
  package.write_apkg(second)
  target.import_anki_package(ImportAnkiPackageRequest(package_path=str(second)))

  assert len(target.find_cards("tag:casa")) == 2
  (note_id,) = target.find_notes("tag:casa")
  assert target.get_note(note_id)["Add Reverse"] == "y"
  target.close()


def test_a_card_a_template_change_renders_is_imported_and_the_users_note_kept(
  tmp_path,
):
  """The note is unchanged, so it is not stamped and the user's edit of it survives.

  The template text is not part of the schema (`equal_schema`, `notes.rs:594`), so the
  stamped notetype updates the user's, and the card renders from the user's fields.
  """
  target, first = imported(tmp_path)
  (note_id,) = target.find_notes("tag:casa")
  users_note = target.get_note(note_id)
  users_note["Front"] = "the user's edit"
  target.update_note(users_note)

  package = Package.read_apkg(first, extract_dir=tmp_path / "media")
  (notetype,) = package.collection.notetypes
  notetype.templates[1].config.q_format = "{{Back}}{{Add Reverse}}"
  package.collection.generate_missing_cards()
  (note,) = package.collection.notes
  assert not note.is_changed_since_read()
  second = tmp_path / "second.apkg"
  package.write_apkg(second)
  target.import_anki_package(ImportAnkiPackageRequest(package_path=str(second)))

  assert len(target.find_cards("tag:casa")) == 2
  assert target.get_note(note_id)["Front"] == "the user's edit"
  # pylib caches notetypes and an import does not clear the cache, so reopen.
  target.close()
  target = AnkiCollection(str(tmp_path / "target.anki2"))
  users_notetype = target.models.by_name(OPTIONAL_REVERSED)
  assert users_notetype is not None
  assert users_notetype["tmpls"][1]["qfmt"] == "{{Back}}{{Add Reverse}}"
  target.close()
