"""Does a real Anki import the package genanki2 writes? (milestone 1, step 4)

    uv run --with anki --with pytest pytest tests/test_apkg_roundtrip.py

The importer is the only authority on whether the bytes are right. Almost every way of
getting the collection wrong still produces a file that opens: a bad `csum` leaves the
note unfindable, a bad `sfld` leaves it unsortable, a `kind` blob that fails to decode
takes the whole deck out, and a deck name joined with `::` instead of `\\x1f` still draws
the right tree while no lookup can find it. So the assertions here go past "it imported".

anki is a test-only dependency, never a dependency of genanki2.
"""

import itertools
import json
import re
import sqlite3
import sys
import warnings
import zipfile

import pytest
from anki.collection import Collection as AnkiCollection
from anki.collection import ImportAnkiPackageRequest
from anki.import_export_pb2 import ImportAnkiPackageOptions

from genanki2.builtins import (
  BASIC_AND_REVERSED_NOTETYPE,
  BASIC_NOTETYPE,
  DEFAULT_DECK_CONFIG,
  default_config_entries,
)
from genanki2.errors import Genanki2Warning
from genanki2.idgen import monotonic_id
from genanki2.models.card import Card, CardQueue, CardType
from genanki2.models.collection import Collection
from genanki2.models.deck import Deck, DeckFilteredKind
from genanki2.models.deck_config import DeckConfig, DeckConfigInner
from genanki2.models.media_entries import MediaEntries
from genanki2.models.note import Note
from genanki2.models.notetype import Notetype
from genanki2.models.package import Package
from genanki2.models.package_metadata import PackageMetadata
from genanki2.models.review import Review, ReviewRating, ReviewType

if sys.version_info >= (3, 14):
  from compression import zstd  # pyright: ignore[reportUnreachable]
else:
  import zstandard as zstd

DECK_NAME = "Roundtrip"
FRONT = "What is the capital of Germany?"
BACK = "Berlin"

SUBDECK_NAME = "Roundtrip::Nested Test"
SUBDECK_TAG = "Capitals"
SUBDECK_NOTES = [
  ["Capital of France", "Paris"],
  ["Capital of Romania", "Bucharest"],
  ["Capital of India", "New Delhi"],
]

NOTE_COUNT = 1 + len(SUBDECK_NOTES)
CARD_COUNT = 1 + 2 * len(SUBDECK_NOTES)  # the subdeck notes are Basic (and reversed)
DECK_COUNT = 3  # Default, Roundtrip, Roundtrip::Nested Test

PRESET_ID = 1234567890123
PRESET_NAME = "Roundtrip preset"
PRESET_NEW_PER_DAY = 7  # not the 20 a default preset carries


def normal_deck(name: str) -> Deck:
  """A normal deck on the default preset, which is what the model defaults to."""
  return Deck(name=name)


def add_note(
  collection: Collection,
  deck: Deck,
  notetype: Notetype,
  fields: list[str],
  position: int,
  tags: list[str] | None = None,
) -> Note:
  """Add a note and one card per template, all sharing the note's queue position."""
  note = Note(notetype=notetype, fields=fields, tags=tags if tags else [])
  collection.notes.append(note)
  collection.cards.extend(
    Card(note=note, deck=deck, template_ord=ordinal, due=position)
    for ordinal in range(len(notetype.templates))
  )
  return note


def build_collection() -> Collection:
  """The collection every test in this module imports: two decks, four notes."""
  collection = Collection(
    config=default_config_entries(), deck_configs=[DEFAULT_DECK_CONFIG]
  )
  positions = itertools.count(1)

  deck = normal_deck(DECK_NAME)
  collection.decks.append(deck)
  add_note(collection, deck, BASIC_NOTETYPE, [FRONT, BACK], next(positions))

  subdeck = normal_deck(SUBDECK_NAME)
  collection.decks.append(subdeck)
  for fields in SUBDECK_NOTES:
    add_note(
      collection,
      subdeck,
      BASIC_AND_REVERSED_NOTETYPE,
      fields,
      next(positions),
      tags=[SUBDECK_TAG],
    )

  return collection.collect_notetypes()


@pytest.fixture
def package(tmp_path):
  path = tmp_path / "deck.apkg"
  Package(collection=build_collection()).write_apkg(path)
  return path


@pytest.fixture
def target(tmp_path):
  """An empty collection standing in for the user Anki is importing into."""
  col = AnkiCollection(str(tmp_path / "target.anki2"))
  yield col
  col.close()


@pytest.fixture
def imported(target, package):
  target.import_anki_package(ImportAnkiPackageRequest(package_path=str(package)))
  return target


def only_note(col, query):
  (note_id,) = col.find_notes(query)
  return col.get_note(note_id)


def test_zip_holds_the_v18_envelope(package):
  """`meta` is what sends the importer to `collection.anki21b` and to zstd.

  All three choices have to agree. A `meta` declaring the current version and a
  collection under any other name fails to import, and so does an uncompressed
  `media` entry, because the same version decides both. `collection.anki2` is the
  fourth entry, carried for clients that cannot read `meta` at all.
  """
  with zipfile.ZipFile(package) as z:
    assert set(z.namelist()) == {
      "meta",
      "collection.anki21b",
      "collection.anki2",
      "media",
    }

    metadata = PackageMetadata.from_bytes(z.read("meta"))
    assert metadata.version == PackageMetadata.Version.LATEST

    collection = zstd.decompress(z.read("collection.anki21b"))
    assert collection.startswith(b"SQLite format 3\x00")

    assert MediaEntries.from_bytes(zstd.decompress(z.read("media"))).entries == []


def test_the_zip_carries_a_schema_11_collection_for_older_clients(package, tmp_path):
  """`collection.anki2` is what a client too old to read `meta` opens.

  Such a client does not know the modern collection is there: it looks for
  `collection.anki21`, then falls back to `collection.anki2`. Under that name genanki2
  ships the same stub Anki's own exporter writes, a schema 11 collection holding one
  note that asks the user to update.
  """
  stub = tmp_path / "stub.anki2"
  with zipfile.ZipFile(package) as z:
    stub.write_bytes(z.read("collection.anki2"))

  db = sqlite3.connect(stub)
  try:
    assert db.execute("select ver from col").fetchone()[0] == 11
    ((fields,),) = db.execute("select flds from notes").fetchall()
  finally:
    db.close()

  assert "update" in fields.lower()


def test_the_schema_11_stub_does_not_reach_the_imported_collection(imported):
  """`meta` sends the importer to `collection.anki21b`, and it opens nothing else."""
  assert imported.note_count() == NOTE_COUNT
  assert not imported.find_notes("update")


def test_the_legacy_envelope_zip_holds_a_plain_collection_and_a_json_map(tmp_path):
  """The legacy envelope is the same tables in a different wrapper.

  It is written by absence: no `meta` entry, so the importer takes the path that reads
  `collection.anki2` uncompressed and parses `media` as JSON.
  """
  path = tmp_path / "legacy.apkg"
  Package(collection=build_collection())._write_v18_legacy_1(path)

  with zipfile.ZipFile(path) as z:
    assert set(z.namelist()) == {"collection.anki2", "media"}
    assert z.read("collection.anki2").startswith(b"SQLite format 3\x00")
    assert json.loads(z.read("media")) == {}


def test_a_package_in_the_legacy_envelope_still_imports(target, tmp_path):
  """Nothing enforces the schema version on the legacy path, so v18 tables pass."""
  path = tmp_path / "legacy.apkg"
  Package(collection=build_collection())._write_v18_legacy_1(path)

  target.import_anki_package(ImportAnkiPackageRequest(package_path=str(path)))
  assert target.note_count() == NOTE_COUNT
  assert target.card_count() == CARD_COUNT


def test_import_adds_every_note_and_card(imported):
  assert imported.note_count() == NOTE_COUNT
  assert imported.card_count() == CARD_COUNT


def test_imported_note_keeps_its_fields_and_deck(imported):
  note = only_note(imported, "Germany")
  assert note.fields == [FRONT, BACK]

  (card,) = note.cards()
  assert imported.decks.name(card.did) == DECK_NAME
  assert (card.ord, card.type, card.queue, card.ivl) == (0, 0, 0, 0)


def test_imported_card_renders_through_its_template(imported):
  """Rendering exercises flds, the notetype's templates, and their field references."""
  (card,) = only_note(imported, "Germany").cards()
  rendered = card.render_output()
  assert FRONT in rendered.question_text
  assert BACK in rendered.answer_text


def test_imported_note_is_searchable(imported):
  """A wrong csum or sfld imports cleanly and leaves the note unfindable."""
  assert len(imported.find_notes(BACK)) == 1


def test_subdeck_is_reachable_by_name(imported):
  """`decks.name` renders `\\x1f` and a literal `::` alike, so it cannot catch this.

  Deck rows store the name `\\x1f`-separated (rslib/src/decks/name.rs:14-22). A row
  written with `::` still nests in the tree, which is built from human names split on
  `::` (rslib/src/decks/tree.rs:36), but every lookup converts the query the other way
  (rslib/src/decks/mod.rs:193-195) and misses. The deck then studies normally while
  being invisible to `decks.id`, so creating it in the GUI silently makes a second one.
  """
  subdeck_id = imported.decks.id_for_name(SUBDECK_NAME)
  assert subdeck_id is not None

  parent_id = imported.decks.id_for_name(DECK_NAME)
  assert imported.decks.children(parent_id) == [(SUBDECK_NAME, subdeck_id)]
  assert imported.db.scalar("select count(*) from decks") == DECK_COUNT


def test_subdeck_notes_land_in_the_subdeck_with_both_cards(imported):
  subdeck_id = imported.decks.id_for_name(SUBDECK_NAME)
  for front, back in SUBDECK_NOTES:
    note = only_note(imported, f'"{front}"')
    assert note.fields == [front, back]
    assert note.tags == [SUBDECK_TAG]
    cards = note.cards()
    assert [c.ord for c in cards] == [0, 1]
    assert {c.did for c in cards} == {subdeck_id}


def test_new_card_position_is_one_per_note_across_the_collection(imported):
  """`due` on a new card is `nextPos`, taken once per note and shared by its siblings.

  `due_for_deck` caches the position in `CardGenCache` for the whole generation batch
  (rslib/src/notetype/cardgen.rs:326-329), and `nextPos` lives in the collection config
  (rslib/src/config/mod.rs:70), not on the deck. So positions run 1, 2, 3, 4 over four
  notes in two decks, not 1, 2 restarting per deck and not 1..7 per card.
  """
  by_note = {}
  for card_id in imported.find_cards(""):
    card = imported.get_card(card_id)
    by_note.setdefault(card.nid, set()).add(card.due)

  assert all(len(dues) == 1 for dues in by_note.values())
  assert sorted(dues.pop() for dues in by_note.values()) == list(
    range(1, NOTE_COUNT + 1)
  )


def test_reimporting_the_same_package_does_not_duplicate(target, package):
  """Notes are matched by guid, notetypes by id then original_id.

  Both are stable across a re-import of one file, so the second import is a no-op
  (rslib/src/import_export/package/apkg/import/notes.rs:141,347). Rebuilding the
  package is a different question: guids are random per call, so a fresh build forks
  the note. That is the open GUID decision, not something this asserts.
  """
  for _ in range(2):
    target.import_anki_package(ImportAnkiPackageRequest(package_path=str(package)))
  assert target.note_count() == NOTE_COUNT
  assert target.card_count() == CARD_COUNT


# ---------------------------------- A custom preset ----------------------------------


@pytest.fixture
def custom_preset_package(tmp_path):
  """A package whose one deck follows a preset the target collection does not have."""
  preset = DeckConfig(
    id=PRESET_ID,
    name=PRESET_NAME,
    config=DeckConfigInner(new_per_day=PRESET_NEW_PER_DAY),
  )
  deck = Deck(name=DECK_NAME, config=preset)
  collection = Collection(config=default_config_entries(), decks=[deck])
  add_note(collection, deck, BASIC_NOTETYPE, [FRONT, BACK], 1)

  path = tmp_path / "preset.apkg"
  Package(collection=collection).write_apkg(path)
  return path


def import_preset_package(target, path, **options):
  target.import_anki_package(
    ImportAnkiPackageRequest(
      package_path=str(path), options=ImportAnkiPackageOptions(**options)
    )
  )
  return target


def test_the_preset_is_dropped_unless_the_importing_user_asks_for_it(
  target, custom_preset_package
):
  """`with_deck_configs` is off by default, so shipping a preset is a request the person
  importing has to accept. The deck still arrives, scheduled by whatever preset 1 holds
  in their collection.

  The registry is what says the preset is absent. `get_config` resolves a missing id to
  preset 1 rather than to nothing, which is the same fallback a dangling `config_id`
  takes.
  """
  imported = import_preset_package(target, custom_preset_package)
  assert PRESET_ID not in {preset["id"] for preset in imported.decks.all_config()}
  deck_id = imported.decks.id_for_name(DECK_NAME)
  assert imported.decks.config_dict_for_deck_id(deck_id)["id"] == 1


def test_a_preset_the_target_lacks_is_added_under_the_id_the_package_gave_it(
  target, custom_preset_package
):
  """Preset ids are not remapped the way deck, note and notetype ids are: the importer
  adds a preset only when the id is free, and keeps the id verbatim
  (rslib/src/import_export/package/apkg/import/decks.rs:44-50)."""
  imported = import_preset_package(
    target, custom_preset_package, with_deck_configs=True
  )
  preset = imported.decks.get_config(PRESET_ID)
  assert preset["id"] == PRESET_ID
  assert preset["name"] == PRESET_NAME


def test_the_imported_deck_is_scheduled_by_the_preset_the_package_shipped(
  target, custom_preset_package
):
  """A dangling `config_id` degrades to preset 1 and then to Anki's own defaults
  (rslib/src/deckconfig/mod.rs:130-149), so resolving the deck rather than reading its
  `config_id` is what tells a preset that arrived from one that did not."""
  imported = import_preset_package(
    target, custom_preset_package, with_deck_configs=True
  )
  deck_id = imported.decks.id_for_name(DECK_NAME)
  preset = imported.decks.config_dict_for_deck_id(deck_id)
  assert preset["id"] == PRESET_ID
  assert preset["new"]["perDay"] == PRESET_NEW_PER_DAY


def test_the_targets_own_default_preset_is_left_alone(target, custom_preset_package):
  """The package ships one preset and does not name preset 1, so the target keeps its
  own daily limit for every deck that was already there."""
  imported = import_preset_package(
    target, custom_preset_package, with_deck_configs=True
  )
  assert imported.decks.get_config(1)["new"]["perDay"] != PRESET_NEW_PER_DAY


def edit_the_preset(source, path, *, new_id=None):
  """Read the package back, raise the preset's daily limit, and write it to `path`,
  returning the messages of the warnings raised."""
  package = Package.read_apkg(source)
  preset = package.collection.get_deck_config(id=PRESET_ID)
  assert preset is not None
  preset.config.new_per_day = PRESET_NEW_PER_DAY + 1
  if new_id is not None:
    preset.id = new_id
    deck = package.collection.get_deck(name=DECK_NAME)
    assert deck is not None
    deck.sync_normal_kind_config_id()
  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always", Genanki2Warning)
    package.write_apkg(path)
  return [str(w.message) for w in caught]


def test_a_preset_edited_under_its_id_does_not_reach_a_user_who_has_it(
  target, custom_preset_package, tmp_path
):
  """The importer adds a preset only when its id is free and never updates one
  (rslib/src/deckconfig/undo.rs:47-52), which is what
  `DeckConfig.warn_unimportable_changes()` reports at write time."""
  import_preset_package(target, custom_preset_package, with_deck_configs=True)
  edited = tmp_path / "edited.apkg"
  messages = edit_the_preset(custom_preset_package, edited)
  assert any("keeps the preset" in m for m in messages)

  imported = import_preset_package(target, edited, with_deck_configs=True)
  assert imported.decks.get_config(PRESET_ID)["new"]["perDay"] == PRESET_NEW_PER_DAY


def test_a_preset_edited_under_a_new_id_is_added_and_the_deck_moves_to_it(
  target, custom_preset_package, tmp_path
):
  """The fix the warning names. The deck update copies an incoming `config_id` other
  than 1 onto the deck the user already has
  (rslib/src/import_export/package/apkg/import/decks.rs:184-195)."""
  import_preset_package(target, custom_preset_package, with_deck_configs=True)
  edited = tmp_path / "edited.apkg"
  new_id = monotonic_id()
  messages = edit_the_preset(custom_preset_package, edited, new_id=new_id)
  assert not any("keeps the preset" in m for m in messages)

  imported = import_preset_package(target, edited, with_deck_configs=True)
  deck_id = imported.decks.id_for_name(DECK_NAME)
  preset = imported.decks.config_dict_for_deck_id(deck_id)
  assert preset["id"] == new_id
  assert preset["new"]["perDay"] == PRESET_NEW_PER_DAY + 1


# ------------------------------- Review history and scheduling -------------------------


REVIEW_IVL = 30
REVIEW_FACTOR = 2500
REVIEW_TIMES_MS = (4200, 3100, 2600)


@pytest.fixture
def reviewed_package(tmp_path):
  """A package whose one card is in review, with three answers behind it."""
  deck = normal_deck(DECK_NAME)
  collection = Collection(config=default_config_entries(), decks=[deck])
  note = Note(notetype=BASIC_NOTETYPE, fields=[FRONT, BACK])
  card = Card(
    note=note,
    deck=deck,
    type=CardType.REVIEW,
    queue=CardQueue.REVIEW,
    due=1,
    ivl=REVIEW_IVL,
    factor=REVIEW_FACTOR,
    reps=len(REVIEW_TIMES_MS),
  )
  collection.notes.append(note)
  collection.cards.append(card)
  collection.reviews.extend(
    Review(
      card=card,
      ease=ReviewRating.GOOD,
      type=ReviewType.REVIEW,
      ivl=REVIEW_IVL,
      factor=REVIEW_FACTOR,
      time=time_ms,
    )
    for time_ms in REVIEW_TIMES_MS
  )

  path = tmp_path / "reviewed.apkg"
  Package(collection=collection.collect_notetypes()).write_apkg(path)
  return path


def import_package(target, path, **options):
  target.import_anki_package(
    ImportAnkiPackageRequest(
      package_path=str(path), options=ImportAnkiPackageOptions(**options)
    )
  )
  return target


def test_the_review_history_crosses_when_the_importer_is_asked_for_scheduling(
  target, reviewed_package
):
  """`gather_revlog` runs inside `if with_scheduling`
  (rslib/src/import_export/gather.rs:53-58), and the same gather runs against the
  package on the way in, so the option gates both ends."""
  imported = import_package(target, reviewed_package, with_scheduling=True)
  times = imported.db.list("select time from revlog order by id")
  assert times == list(REVIEW_TIMES_MS)


def test_an_imported_entry_names_the_card_it_arrived_with(target, reviewed_package):
  """`import_revlog` rewrites `cid` to the id the card was given
  (rslib/src/import_export/package/apkg/import/cards.rs:110-119). An entry pointing at
  the package's own card id would be history attached to nothing."""
  imported = import_package(target, reviewed_package, with_scheduling=True)
  (card_id,) = imported.find_cards(f'"deck:{DECK_NAME}"')
  assert imported.db.list("select distinct cid from revlog") == [card_id]


def test_the_imported_card_keeps_the_schedule_the_package_gave_it(
  target, reviewed_package
):
  card = imported_card(import_package(target, reviewed_package, with_scheduling=True))
  assert (card.type, card.ivl, card.factor) == (2, REVIEW_IVL, REVIEW_FACTOR)


def test_the_review_history_is_dropped_under_the_default_options(
  target, reviewed_package
):
  """Scheduling is off unless the person importing asks for it, so the same package
  arrives as new cards with no history. The gate is what a deck author ships against."""
  imported = import_package(target, reviewed_package)
  assert imported.db.scalar("select count() from revlog") == 0


def test_the_card_is_reset_to_new_under_the_default_options(target, reviewed_package):
  card = imported_card(import_package(target, reviewed_package))
  assert (card.type, card.ivl, card.factor) == (0, 0, 0)


def imported_card(collection):
  (card_id,) = collection.find_cards(f'"deck:{DECK_NAME}"')
  return collection.get_card(card_id)


# ---------------------------------- Filtered decks -------------------------------------


FILTERED_DECK_NAME = "Filtered"
HOME_DECK_NAME = "Home"
A_DECK_ID_THE_PACKAGE_DOES_NOT_HOLD = 999_999

# The card's `odid` in each of the three states a filtered deck can be shipped in.
HOME_DECKS = {
  "shipped": lambda: Deck(name=HOME_DECK_NAME),
  "absent": lambda: 0,
  "missing": lambda: A_DECK_ID_THE_PACKAGE_DOES_NOT_HOLD,
}


@pytest.fixture
def filtered_package(tmp_path):
  """Builds a package with one filtered deck and one card in it, given the card's `odid`.

  Two of the three states are ones genanki2 warns about rather than refuses, and what
  Anki does with them is what these tests measure.
  """

  def build(original_deck):
    filtered = Deck(name=FILTERED_DECK_NAME, kind=DeckFilteredKind())
    collection = Collection(config=default_config_entries(), decks=[filtered])
    note = Note(notetype=BASIC_NOTETYPE, fields=[FRONT, BACK])
    collection.notes.append(note)
    collection.cards.append(
      Card(note=note, deck=filtered, odid=original_deck, odue=1, due=1)
    )
    path = tmp_path / "filtered.apkg"
    Package(collection=collection).write_apkg(path)
    return path

  return build


def test_a_filtered_deck_crosses_when_its_card_carries_a_home_deck(
  target, filtered_package
):
  """The export gather runs against the package on the way in, and it ships filtered
  decks only when every card is either in a normal deck with no original deck or names
  an original deck that is present (rslib/src/import_export/gather.rs:150-166)."""
  package = filtered_package(HOME_DECKS["shipped"]())
  imported = import_package(target, package, with_scheduling=True)
  assert deck_is_filtered(imported, FILTERED_DECK_NAME)


def test_a_card_with_no_home_deck_costs_the_package_its_filtered_deck(
  target, filtered_package
):
  """Anki does not deliver the card into a filtered deck: it converts the deck."""
  with pytest.warns(UserWarning, match="carries no original deck"):
    package = filtered_package(HOME_DECKS["absent"]())
  imported = import_package(target, package, with_scheduling=True)
  assert not deck_is_filtered(imported, FILTERED_DECK_NAME)


def test_a_home_deck_the_package_does_not_hold_costs_it_the_same(
  target, filtered_package
):
  """The other half of what `Collection.warn_filtered_decks_become_normal()` reports,
  measured: the package imports, without the filtered deck it describes."""
  with pytest.warns(UserWarning, match="does not hold"):
    package = filtered_package(HOME_DECKS["missing"]())
  imported = import_package(target, package, with_scheduling=True)
  assert not deck_is_filtered(imported, FILTERED_DECK_NAME)
  assert filtered_deck_card(imported).odid == 0


def test_the_default_options_drop_a_filtered_deck_that_would_have_crossed(
  target, filtered_package
):
  """`reset_decks` converts every filtered deck when scheduling is off
  (rslib/src/import_export/gather.rs:132-138), so the importing person decides this
  before any card does."""
  package = filtered_package(HOME_DECKS["shipped"]())
  imported = import_package(target, package)
  assert not deck_is_filtered(imported, FILTERED_DECK_NAME)


@pytest.mark.parametrize("home_deck", HOME_DECKS.values(), ids=list(HOME_DECKS))
def test_the_imported_card_can_be_answered_however_it_was_shipped(
  target, filtered_package, home_deck
):
  """No apkg import delivers a card stranded in a filtered deck, which is why that
  state is warned about rather than refused. `get_scheduling_states` is the call that
  resolves the home deck and asks it for a preset
  (rslib/src/scheduler/answering/mod.rs:459-476)."""
  imported = import_package(target, filtered_package(home_deck()), with_scheduling=True)
  card = filtered_deck_card(imported)
  assert imported._backend.get_scheduling_states(card.id) is not None


def deck_is_filtered(collection, name):
  (entry,) = [e for e in collection.decks.all_names_and_ids() if e.name == name]
  return bool(collection.decks.get(entry.id)["dyn"])


def filtered_deck_card(collection):
  (card_id,) = collection.find_cards(f'"deck:{FILTERED_DECK_NAME}"')
  return collection.get_card(card_id)


@pytest.fixture
def nonleaf_collection():
  """A filtered deck holding a normal subdeck, with a card in each."""
  home = Deck(name=HOME_DECK_NAME)
  filtered = Deck(name=FILTERED_DECK_NAME, kind=DeckFilteredKind())
  subdeck = Deck(name=f"{FILTERED_DECK_NAME}::Sub")
  collection = Collection(config=default_config_entries(), decks=[home, subdeck])
  for deck, original_deck in ((filtered, home), (subdeck, 0)):
    note = Note(notetype=BASIC_NOTETYPE, fields=[FRONT, BACK])
    collection.notes.append(note)
    collection.cards.append(
      Card(note=note, deck=deck, odid=original_deck, odue=1 if original_deck else 0)
    )
  return collection


def deck_names(collection):
  return {entry.name for entry in collection.decks.all_names_and_ids()}


def test_an_import_moves_a_subdeck_out_of_a_kept_filtered_deck(
  target, tmp_path, nonleaf_collection
):
  """`ensure_valid_first_existing_parent` adds a normal deck named after the filtered
  one plus the import time in seconds and reparents the subdeck under it
  (rslib/src/import_export/package/apkg/import/decks.rs:148-166)."""
  path = tmp_path / "nonleaf.apkg"
  with pytest.warns(UserWarning, match="cannot hold subdecks"):
    Package(collection=nonleaf_collection).write_apkg(path)
  imported = import_package(target, path, with_scheduling=True)

  assert deck_is_filtered(imported, FILTERED_DECK_NAME)
  [moved] = [name for name in deck_names(imported) if name.endswith("::Sub")]
  assert re.fullmatch(rf"{FILTERED_DECK_NAME} \d+::Sub", moved)


def test_a_restored_subdeck_of_a_filtered_deck_stays_and_cannot_be_renamed(
  tmp_path, nonleaf_collection
):
  """A restore opens the file as written. The database check does not touch the
  nesting, and a rename walks up to the filtered parent and is refused
  (`match_or_create_parents`, rslib/src/decks/addupdate.rs:113-120)."""
  path = tmp_path / "collection.anki2"
  with pytest.warns(UserWarning, match="cannot hold subdecks"):
    nonleaf_collection.write_sqlite(path)
  restored = AnkiCollection(str(path))
  try:
    restored.fix_integrity()
    subdeck = restored.decks.by_name(f"{FILTERED_DECK_NAME}::Sub")
    assert subdeck is not None
    with pytest.raises(Exception, match="can not have child decks"):
      restored.decks.rename(subdeck, f"{FILTERED_DECK_NAME}::Renamed")
  finally:
    restored.close()
