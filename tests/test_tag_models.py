"""Does the tag registry survive a round trip, and does collecting it stay additive?

    uv run --with pytest pytest tests/test_tag_models.py

The `tags` table backs the browser's sidebar and nothing else: a note carries its own
tags as text, so nothing during study reads a row here. No export writes the table, and
an import rebuilds it from the notes it read (`canonify_note_tags`,
`rslib/src/import_export/package/apkg/import/notes.rs:432`), so a package carries none
of it and registering a tag is not what makes tags work.

The table is therefore not the union of the notes' tags, and `collect_tags()` is written
around that. It only adds: the registry holds `marked` and `leech` after an export
without scheduling strips them off the notes (`remove_system_tags`,
`rslib/src/import_export/gather.rs:98-106`), and it keeps a tag whose last note dropped
it until a database check rebuilds the table. Removing either would rewrite a table
Anki wrote.

`tag` is the primary key under `COLLATE unicase`, so two spellings that fold together
cannot both be registered. Anki settles that by rewriting the note to the spelling the
registry already holds (`prepare_tag_for_registering`,
`rslib/src/tags/register.rs:104`), which is why a mismatch is a warning here rather than
a repair: rewriting the note would change data on a read-update-write cycle.

The models carry no anki dependency and neither does this file.
"""

import warnings

import pydantic as pyd
import pytest

from genanki2.builtins import BASIC_NOTETYPE, DEFAULT_DECK, DEFAULT_DECK_CONFIG
from genanki2.errors import Genanki2Error, Genanki2Warning
from genanki2.models.card import Card
from genanki2.models.collection import Collection
from genanki2.models.config import ConfigEntry
from genanki2.models.note import Note
from genanki2.models.tag import Tag
from genanki2.sqlite import AnkiSQLite

# Written collections carry it so that the writer's v1 scheduler warning stays out of
# the way of what these tests are about.
SCHEDULER_V2 = [ConfigEntry(key="schedVer", value=b"2")]


def a_note(*tags: str) -> Note:
  return Note(notetype=BASIC_NOTETYPE, fields=["front", "back"], tags=list(tags))


def a_collection(**overrides) -> Collection:
  return Collection(notetypes=[BASIC_NOTETYPE], config=SCHEDULER_V2, **overrides)


def tag_names(collection: Collection) -> list[str]:
  return [tag.tag for tag in collection.tags]


# ----------------------------------- Row round trips ------------------------------------


def test_the_row_survives_a_round_trip():
  row = Tag(tag="vocab::verbs", collapsed=False, usn=4).to_row()
  assert Tag.from_row(row).to_row() == row


def test_a_registered_tag_is_collapsed():
  """`Tag::new` builds a tag with `expanded: false` (`rslib/src/tags/mod.rs:29`), and
  the column holds the opposite of that field."""
  assert Tag(tag="vocab").collapsed is True


def test_the_collapsed_column_is_written_as_an_integer():
  assert Tag(tag="vocab", collapsed=False).to_row()["collapsed"] is False


def test_a_fresh_tag_has_not_reached_the_server():
  assert Tag(tag="vocab").update_sequence_n == -1


def test_the_reserved_config_column_is_carried_through_unchanged():
  """Nothing in Anki reads or writes it, and Anki's own insert names the other three
  columns only (`rslib/src/storage/tag/add.sql`). A blob a later Anki writes has to
  survive a read and a write anyway."""
  blob = b"\x08\x01"
  assert Tag.from_row({"tag": "vocab", "usn": 0, "collapsed": 0, "config": blob}).config
  assert Tag(tag="vocab", config=blob).to_row()["config"] == blob


def test_a_tag_without_a_config_writes_the_column_null():
  assert Tag(tag="vocab").to_row()["config"] is None


# ------------------------------------ The tag name --------------------------------------


def test_the_name_is_repaired_the_way_a_note_tag_is():
  assert Tag(tag=" vocab::::verbs ").tag == "vocab::blank::verbs"


def test_a_name_holding_a_tag_separator_is_refused():
  """Anki splits on the separator, turning one registered tag into two."""
  with pytest.raises(pyd.ValidationError, match="two"):
    Tag(tag="two words")


def test_a_note_tag_appended_with_a_separator_is_refused_at_the_write():
  """Appending to the list runs no validator, so the tag first meets the check when the
  note's row is written, outside pydantic, and has to raise the library's own error."""
  note = a_note()
  note.tags.append("two words")
  with pytest.raises(Genanki2Error, match="two words"):
    note.to_row()


# ------------------------------------ Collecting tags -----------------------------------


def test_every_tag_the_notes_carry_is_registered():
  """A note sorts its own tags, so the registry follows that order rather than the one
  the caller wrote them in."""
  collection = a_collection(notes=[a_note("vocab", "verbs"), a_note("nouns")])
  assert tag_names(collection.collect_tags()) == ["verbs", "vocab", "nouns"]


def test_a_tag_two_notes_share_is_registered_once():
  collection = a_collection(notes=[a_note("vocab"), a_note("vocab")])
  assert tag_names(collection.collect_tags()) == ["vocab"]


def test_two_spellings_of_one_tag_register_the_first():
  """The column folds them together, so only one of the two can exist. Anki resolves a
  clash the same way, keeping what it saw first (`canonify_tags_inner`)."""
  collection = a_collection(notes=[a_note("Vocab"), a_note("vocab")])
  assert tag_names(collection.collect_tags()) == ["Vocab"]


def test_the_registered_spelling_wins_over_the_notes():
  collection = a_collection(tags=[Tag(tag="Vocab")], notes=[a_note("vocab")])
  assert tag_names(collection.collect_tags()) == ["Vocab"]


def test_a_registered_tag_keeps_its_state_when_a_note_carries_it_too():
  """Collecting must not reset the collapse state, which is the one thing the notes
  cannot carry and the only thing a database check preserves."""
  collection = a_collection(tags=[Tag(tag="vocab", collapsed=False, usn=9)])
  collection.notes.append(a_note("vocab"))
  collection.collect_tags()
  assert collection.tags == [Tag(tag="vocab", collapsed=False, usn=9)]


def test_a_registered_tag_no_note_carries_is_kept():
  """`marked` and `leech` are in the registry of a collection whose notes lost them to
  an export, and a tag outlives the last note that carried it."""
  collection = a_collection(tags=[Tag(tag="marked")], notes=[a_note("vocab")])
  assert tag_names(collection.collect_tags()) == ["marked", "vocab"]


def test_collecting_twice_registers_once():
  collection = a_collection(notes=[a_note("vocab")])
  assert tag_names(collection.collect_tags().collect_tags()) == ["vocab"]


def test_a_collection_without_notes_is_left_alone():
  collection = a_collection(tags=[Tag(tag="marked")])
  assert tag_names(collection.collect_tags()) == ["marked"]


def test_a_note_is_not_rewritten_to_the_registered_spelling():
  """Anki rewrites the note instead, but doing that here would change a note read off a
  file. The disagreement is reported by `warn_tag_spelling_mismatch()`."""
  note = a_note("vocab")
  collection = a_collection(tags=[Tag(tag="Vocab")], notes=[note])
  collection.collect_tags()
  assert note.tags == ["vocab"]


# ------------------------------------- Unique keys --------------------------------------


def test_two_tags_that_differ_only_in_case_are_refused():
  """The primary key is `COLLATE unicase`, so SQLite would refuse the second row with
  an `IntegrityError` naming the table and no tag."""
  collection = a_collection(tags=[Tag(tag="vocab"), Tag(tag="Vocab")])
  with pytest.raises(ValueError, match="tags"):
    collection.check_unique_tags()


def test_two_distinct_tags_are_accepted():
  collection = a_collection(tags=[Tag(tag="vocab"), Tag(tag="verbs")])
  assert collection.check() is collection


# --------------------------------- The spelling mismatch --------------------------------


def test_a_note_spelling_a_registered_tag_differently_is_reported():
  collection = a_collection(tags=[Tag(tag="Vocab")], notes=[a_note("vocab")])
  with pytest.warns(Genanki2Warning, match="different casing"):
    collection.warn_tag_spelling_mismatch()


def test_two_notes_spelling_one_tag_differently_are_reported():
  """The table is empty in every package Anki writes, so this is the shape the report
  exists for: the note order alone decides which spelling the deck ends up with."""
  collection = a_collection(notes=[a_note("Vocab"), a_note("vocab")])
  with pytest.warns(Genanki2Warning, match="carries 'vocab', which Anki writes as"):
    collection.warn_tag_spelling_mismatch()


def test_a_tag_under_a_registered_ancestor_takes_its_case():
  """Registering a tag takes the case of an ancestor already held, so the prefix is
  rewritten and only the components below it are kept as written."""
  collection = a_collection(tags=[Tag(tag="Vocab")], notes=[a_note("vocab::verbs")])
  with pytest.warns(Genanki2Warning, match="writes as 'Vocab::verbs'"):
    collection.warn_tag_spelling_mismatch()


def test_the_nearest_registered_ancestor_wins():
  collection = a_collection(
    tags=[Tag(tag="Vocab"), Tag(tag="vocab::Verbs")],
    notes=[a_note("VOCAB::VERBS::irregular")],
  )
  with pytest.warns(Genanki2Warning, match="writes as 'vocab::Verbs::irregular'"):
    collection.warn_tag_spelling_mismatch()


def test_a_tag_whose_ancestor_is_spelled_the_same_is_not_reported():
  collection = a_collection(tags=[Tag(tag="vocab")], notes=[a_note("vocab::verbs")])
  with warnings.catch_warnings():
    warnings.simplefilter("error", Genanki2Warning)
    collection.warn_tag_spelling_mismatch()


def test_the_agreeing_spelling_is_not_reported():
  collection = a_collection(tags=[Tag(tag="vocab")], notes=[a_note("vocab")])
  with warnings.catch_warnings():
    warnings.simplefilter("error", Genanki2Warning)
    collection.warn_tag_spelling_mismatch()


def test_a_tag_the_registry_does_not_hold_is_not_reported():
  """A note tag that was never registered is the ordinary case, since nothing fills the
  table unless a caller asks for it."""
  collection = a_collection(tags=[Tag(tag="verbs")], notes=[a_note("vocab")])
  with warnings.catch_warnings():
    warnings.simplefilter("error", Genanki2Warning)
    collection.warn_tag_spelling_mismatch()


# ------------------------------------ Serialization -------------------------------------


def test_a_registered_tag_reaches_the_table(tmp_path):
  tag = Tag(tag="vocab", collapsed=False, usn=2)
  path = tmp_path / "collection.anki2"
  a_collection(tags=[tag]).write_sqlite(path)
  with AnkiSQLite(path) as db:
    assert db.connection.execute("select * from tags").fetchall() == [tag.to_row()]


def test_a_tagged_note_registers_nothing_on_its_own(tmp_path):
  """The writer writes the list, and only a caller calling `collect_tags()` fills it.
  An import rebuilds the registry from the notes either way."""
  note = a_note("vocab")
  path = tmp_path / "collection.anki2"
  collection = a_collection(
    deck_configs=[DEFAULT_DECK_CONFIG],
    decks=[DEFAULT_DECK],
    notes=[note],
    cards=[Card(note=note, deck=DEFAULT_DECK)],
  )
  collection.write_sqlite(path)
  with AnkiSQLite(path) as db:
    assert db.connection.execute("select * from tags").fetchall() == []
