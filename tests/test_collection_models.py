"""Does the `col` row survive a trip through `CollectionMetadata`, and do the
collection-level checks refuse what Anki would have repaired?

    uv run --with pytest pytest tests/test_collection_models.py

The `col` table holds one row, and the model holds five of its thirteen columns. The
other eight are constants (`id`, `ver`, `dty`) or the schema 11 JSON blobs (`conf`,
`models`, `decks`, `dconf`, `tags`), which every upgrade step blanks on the way to v18
(`storage/config/mod.rs:115`, `deck/mod.rs:402`, `deckconfig/mod.rs:208`,
`notetype/mod.rs:372`, `tag/mod.rs:156`) and only the schema 11 downgrade refills. So a
v18 row always carries them empty, and writing them back empty drops nothing.

`creation_time_s` is checked against the clock, and that validator is covered in
`test_timestamps.py` alongside the other models that carry a timestamp.

`Collection`'s own checks are about a table rather than a row, so no other model can see
them: the writer builds every table from these lists, and nothing else checks that the
lists agree. Each one stands for a repair Anki performs on a collection that already
holds the bad data, and every one of those repairs is destructive: a card whose note is
missing is deleted, a duplicate `(nid, ord)` pair is deleted, a name that collides gains
a `+` suffix. Refusing to write is the only point at which the data still exists.

The models carry no anki dependency and neither does this file.
"""

import time
import warnings
from datetime import datetime
from zoneinfo import ZoneInfo

import pydantic as pyd
import pytest

from genanki2 import _default_rows
from genanki2.builtins import (
  BASIC_AND_REVERSED_NOTETYPE,
  BASIC_NOTETYPE,
  BUILTIN_NOTETYPES,
  DEFAULT_DECK,
  DEFAULT_DECK_CONFIG,
  builtin_notetype,
  default_config_entries,
  default_deck,
  default_deck_config,
)
from genanki2.datetime import days_elapsed
from genanki2.errors import CollectionReadError, Genanki2Error, Genanki2Warning
from genanki2.models import collection_metadata
from genanki2.models.card import Card, CardQueue, CardType
from genanki2.models.collection import Collection
from genanki2.models.collection_metadata import CollectionMetadata
from genanki2.models.config import ConfigEntry
from genanki2.models.deck import Deck, DeckFilteredKind, DeckNormalKind
from genanki2.models.deck_config import DeckConfig
from genanki2.models.grave import Grave, GraveType
from genanki2.models.note import Note
from genanki2.models.notetype import Notetype
from genanki2.models.package import CollectionPackage, Package
from genanki2.models.review import Review, ReviewRating, ReviewType
from genanki2.models.serialization_context import SerializationContext
from genanki2.models.tag import Tag

COL_ROW = _default_rows.table_col[0]

# The columns no attribute holds, and the values `to_row` writes for them.
CONSTANT_COLUMNS = {"id": 1, "ver": 18, "dty": 0}
LEGACY_COLUMNS = {"conf": "", "models": "", "decks": "", "dconf": "", "tags": ""}


# --------------------------------- Row round trips ---------------------------------


def test_the_row_survives_a_round_trip():
  assert CollectionMetadata.from_row(COL_ROW).to_row() == COL_ROW


def test_every_column_the_model_holds_is_read_under_its_own_name():
  """Each of the five is aliased, so a wrong alias silently substitutes the default
  instead of failing, and the collection is written with someone else's timestamps."""
  metadata = CollectionMetadata.from_row(
    COL_ROW | {"crt": 1_600_000_000, "mod": 2, "scm": 3, "usn": 4, "ls": 5}
  )
  assert metadata.creation_time_s == 1_600_000_000
  assert metadata.modification_time_ms == 2
  assert metadata.schema_modification_time_ms == 3
  assert metadata.update_sequence_n == 4
  assert metadata.last_sync_time_ms == 5


def test_a_row_built_by_attribute_name_still_writes_the_column_names():
  row = CollectionMetadata(
    creation_time_s=1,
    modification_time_ms=2,
    schema_modification_time_ms=3,
    update_sequence_n=4,
    last_sync_time_ms=5,
  ).to_row()
  assert {key: row[key] for key in ("crt", "mod", "scm", "usn", "ls")} == {
    "crt": 1,
    "mod": 2,
    "scm": 3,
    "usn": 4,
    "ls": 5,
  }


# ------------------------- The columns the model does not hold -------------------------


def test_the_constant_columns_are_written_without_being_stored():
  """`id` is 1 because the table holds one row, `dty` is dead, and `ver` is the whole
  claim the file makes about its schema."""
  row = CollectionMetadata().to_row()
  assert {column: row[column] for column in CONSTANT_COLUMNS} == CONSTANT_COLUMNS


def test_the_legacy_json_columns_are_written_empty():
  row = CollectionMetadata().to_row()
  assert {column: row[column] for column in LEGACY_COLUMNS} == LEGACY_COLUMNS


def test_a_row_carrying_legacy_json_does_not_carry_it_back():
  """No v18 collection has these filled, so a row that does is one Anki would have
  upgraded, and the values are the pre-upgrade copies of tables that now exist."""
  row = CollectionMetadata.from_row(COL_ROW | {"models": '{"1": {}}'}).to_row()
  assert row["models"] == ""


# ---------------------------------- The schema version ----------------------------------


@pytest.mark.parametrize("version", [11, 17, 19])
def test_another_schema_version_is_refused(version):
  """A real Anki export also ships a schema 11 `collection.anki2` holding a single
  "please update Anki" note, so reading the wrong member of the zip is a live mistake
  rather than a hypothetical one."""
  with pytest.raises(CollectionReadError, match=f"declares {version}"):
    CollectionMetadata.from_row(COL_ROW | {"ver": version})


# ------------------------------------ Sync columns ------------------------------------


def test_a_fresh_collection_has_never_seen_a_server():
  """0, not the -1 every other table uses: this column is the last USN the server sent
  (`storage/sync.rs:12`), not a marker of unsynced changes."""
  assert CollectionMetadata().update_sequence_n == 0


@pytest.mark.parametrize("column", ["crt", "mod", "scm", "ls"])
def test_a_timestamp_before_the_epoch_is_rejected(column):
  with pytest.raises(pyd.ValidationError):
    CollectionMetadata(**{column: -1})


# ------------------------------ A collection that agrees ------------------------------


def a_note(notetype: Notetype = BASIC_NOTETYPE, **overrides) -> Note:
  return Note(notetype=notetype, fields=["front", "back"], **overrides)


def a_deck(name: str, **overrides) -> Deck:
  return Deck(name=name, **overrides)


def a_review(card: Card) -> Review:
  return Review(card=card, ease=ReviewRating.GOOD, type=ReviewType.REVIEW)


def a_collection(**overrides) -> Collection:
  """One deck, one notetype, one note and the card it produces, all agreeing.

  The preset is there because every normal deck names one and the referential check
  reads it; `DEFAULT_DECK` is on preset 1, which is what `DEFAULT_DECK_CONFIG` holds.

  Each builtin is deep-copied. A test that reaches into one of them, a nested blob
  included, would otherwise change the constant for every test that runs after it.
  """
  deck = default_deck()
  note = a_note(builtin_notetype(BASIC_NOTETYPE.name))
  return Collection(
    deck_configs=[default_deck_config()],
    decks=[deck],
    notetypes=[note.notetype],
    notes=[note],
    cards=[Card(note=note, deck=deck)],
    **overrides,
  )


def test_a_collection_whose_lists_agree_passes_every_check():
  collection = a_collection()
  assert collection.check() is collection


def test_an_empty_collection_passes_every_check():
  """Every check is about a disagreement between two lists, and empty lists cannot
  disagree. This is the state a caller starts from and appends to."""
  collection = Collection()
  assert collection.check() is collection


# --------------------------------- Dangling references ---------------------------------


def test_a_note_whose_notetype_the_collection_does_not_hold_is_refused():
  """The fields and templates live on the notetype, so a note pointing at one the
  collection never declares writes a `mid` no `notetypes` row answers."""
  collection = a_collection()
  collection.notetypes.clear()
  with pytest.raises(ValueError, match="notetype"):
    collection.check_referential_integrity()


def test_a_card_whose_deck_the_collection_does_not_hold_is_refused():
  collection = a_collection()
  collection.decks.clear()
  with pytest.raises(ValueError, match="deck"):
    collection.check_referential_integrity()


def test_a_card_whose_note_the_collection_does_not_hold_is_refused():
  """Anki's repair is `delete from cards where nid not in (select id from notes)`
  (rslib/src/dbcheck.rs:190-197), so a dangling card leaves the package as a card and
  arrives as nothing."""
  collection = a_collection()
  collection.notes.clear()
  with pytest.raises(ValueError, match="note"):
    collection.check_referential_integrity()


# ----------------------------------- Missing presets -----------------------------------


def config_id(deck: Deck) -> int:
  assert isinstance(deck.kind, DeckNormalKind)
  return deck.kind.config_id


def a_collection_with_a_dangling_preset(config_id: int = 7) -> Collection:
  """A deck naming a preset by id alone, the state a deck read from a file is in when
  the collection does not hold the preset."""
  collection = a_collection()
  collection.decks.append(a_deck("Custom scheduling", kind={"config_id": config_id}))
  return collection


def test_a_deck_whose_preset_the_collection_does_not_hold_is_not_refused():
  """Anki resolves a dangling `config_id` to preset 1 (rslib/src/deckconfig/mod.rs:
  130-149) and dbcheck has no repair for it, so the id is a value Anki tolerates."""
  collection = a_collection_with_a_dangling_preset()
  assert collection.check() is collection


def test_a_deck_whose_preset_the_collection_does_not_hold_is_reported():
  collection = a_collection_with_a_dangling_preset()
  with pytest.warns(
    UserWarning, match="'Custom scheduling'.*names preset 7"
  ) as reported:
    collection.warn_missing_deck_configs()
  assert len(reported) == 1


def test_every_deck_with_a_missing_preset_is_named_in_one_warning():
  collection = a_collection_with_a_dangling_preset()
  collection.decks.append(a_deck("Other scheduling", kind={"config_id": 8}))
  with pytest.warns(UserWarning) as reported:
    collection.warn_missing_deck_configs()
  assert len(reported) == 1
  assert str(reported[0].message).count("names preset") == 2


def test_a_deck_holding_a_preset_the_list_misses_is_not_reported(recwarn):
  """`write_sqlite()` collects the preset before it warns, so there is nothing to
  report when a caller warns before collecting."""
  collection = Collection(decks=[a_lone_card().deck])
  collection.warn_missing_deck_configs()
  assert len(recwarn) == 0


def test_a_deck_naming_preset_1_is_not_reported(recwarn):
  """`write_sqlite()` adds the builtin `Default` preset for it."""
  collection = Collection(decks=[a_deck("Default scheduling")])
  collection.warn_missing_deck_configs()
  assert len(recwarn) == 0


def test_writing_warns_about_a_missing_preset_and_keeps_the_id(tmp_path):
  """Nothing repairs the id unasked, so read-update-write carries it through as Anki
  wrote it."""
  path = tmp_path / "collection.anki2"
  with pytest.warns(UserWarning, match="names preset 7"):
    a_collection_with_a_dangling_preset().write_sqlite(path)
  deck = next(
    deck
    for deck in Collection.read_sqlite(path).decks
    if deck.name == "Custom scheduling"
  )
  assert deck.config is None
  assert config_id(deck) == 7


def test_the_repair_points_a_deck_with_a_missing_preset_at_preset_1(recwarn):
  collection = a_collection_with_a_dangling_preset().repair_missing_deck_configs()
  assert config_id(collection.decks[-1]) == 1
  collection.warn_missing_deck_configs()
  assert len(recwarn) == 0


def test_the_repair_leaves_a_deck_on_a_preset_the_collection_holds_alone():
  collection = a_collection()
  custom = DEFAULT_DECK_CONFIG.model_copy(deep=True, update={"id": 7, "name": "Custom"})
  collection.deck_configs.append(custom)
  collection.decks.append(a_deck("Custom scheduling", kind={"config_id": 7}))
  collection.repair_missing_deck_configs()
  assert config_id(collection.decks[-1]) == 7


def test_the_repair_collects_a_preset_a_deck_holds_instead_of_repointing_the_deck(
  tmp_path,
):
  """Repointing it would leave `kind.config_id` disagreeing with `Deck.config`, which
  `Deck.to_row()` refuses."""
  deck = a_lone_card().deck
  deck.config = DEFAULT_DECK_CONFIG.model_copy(
    deep=True, update={"id": 7, "name": "Custom"}
  )
  collection = Collection(decks=[deck]).repair_missing_deck_configs()
  assert collection.deck_configs == [deck.config]
  assert config_id(deck) == 7
  collection.write_sqlite(tmp_path / "collection.anki2")


# ------------------------------------ Unique keys ------------------------------------


@pytest.mark.parametrize("list_name", ["decks", "notetypes", "notes", "cards"])
def test_duplicate_ids_in_one_list_are_refused(list_name):
  """Every one of the four is a primary key, so the duplicate otherwise surfaces as an
  `IntegrityError` naming a table and no object."""
  collection = a_collection()
  objects = getattr(collection, list_name)
  objects.append(objects[0].model_copy())
  with pytest.raises(ValueError, match=list_name):
    collection.check_unique_ids()


def test_two_notes_sharing_a_guid_are_refused():
  """The importer matches an incoming note to an existing one by guid
  (rslib/src/import_export/package/apkg/import/notes.rs:338-348), so the second of two
  notes sharing a guid updates the first or is skipped, and is never added."""
  collection = a_collection()
  collection.notes.append(a_note(guid=collection.notes[0].guid))
  with pytest.raises(ValueError, match="guid"):
    collection.check_unique_note_guids()


@pytest.mark.parametrize("list_name", ["decks", "notetypes"])
def test_names_that_differ_only_in_case_are_refused(list_name):
  """Both indexes are UNIQUE `COLLATE unicase` (rslib/src/decks/name.rs:153-165,
  rslib/src/notetype/mod.rs:271-293). Anki resolves the clash by appending `+`, which
  renames a deck the user already has, or a notetype every `{{Front}}` refers to."""
  collection = a_collection()
  objects = getattr(collection, list_name)
  other = objects[0].model_copy(update={"id": 999, "name": objects[0].name.upper()})
  objects.append(other)
  with pytest.raises(ValueError, match=list_name):
    collection.check_unique_names()


def test_two_cards_from_the_same_note_and_template_are_refused():
  """`(nid, ord)` is what produced the card, and Anki repairs a duplicate by deleting
  cards (`remove_duplicate_card_ordinals`, rslib/src/dbcheck.rs:281-282)."""
  collection = a_collection()
  first = collection.cards[0]
  collection.cards.append(Card(note=first.note, deck=first.deck))
  with pytest.raises(ValueError, match="producer"):
    collection.check_unique_card_producers()


def test_a_second_template_of_one_note_is_a_different_card():
  """The pair is the key, not the note: a two-template notetype produces two cards from
  one note, which is the ordinary case rather than an edge one."""
  note = a_note(notetype=BASIC_AND_REVERSED_NOTETYPE)
  collection = Collection(
    deck_configs=[DEFAULT_DECK_CONFIG],
    decks=[DEFAULT_DECK],
    notetypes=[BASIC_AND_REVERSED_NOTETYPE],
    notes=[note],
    cards=[
      Card(note=note, deck=DEFAULT_DECK, template_ord=0),
      Card(note=note, deck=DEFAULT_DECK, template_ord=1),
    ],
  )
  assert collection.check() is collection


def test_one_template_of_two_notes_is_a_different_card():
  collection = a_collection()
  note = a_note()
  collection.notes.append(note)
  collection.cards.append(Card(note=note, deck=DEFAULT_DECK))
  assert collection.check() is collection


def test_two_config_entries_sharing_a_key_are_refused():
  """`KEY` is the table's primary key, and `get_config_entry` returns the first match, so
  the second entry is a setting that silently does nothing."""
  collection = a_collection(
    config=[ConfigEntry(key="rollover"), ConfigEntry(key="rollover")]
  )
  with pytest.raises(ValueError, match="config"):
    collection.check_unique_config_keys()


# ---------------------------- The checks a caller gets for free ----------------------------


@pytest.mark.parametrize(
  "break_it",
  [
    lambda collection: collection.notetypes.clear(),
    lambda collection: collection.notes.append(collection.notes[0].model_copy()),
    lambda collection: collection.decks.append(
      collection.decks[0].model_copy(update={"id": 999})
    ),
    lambda collection: collection.cards.append(
      Card(note=collection.notes[0], deck=collection.decks[0])
    ),
    lambda collection: collection.config.extend(
      [ConfigEntry(key="rollover"), ConfigEntry(key="rollover")]
    ),
    lambda collection: collection.tags.extend([Tag(tag="vocab"), Tag(tag="Vocab")]),
    lambda collection: collection.reviews.extend(
      [a_review(collection.cards[0])] * 2  # one object, so one id, twice
    ),
    lambda collection: collection.graves.extend(
      [Grave(oid=999, grave_type=GraveType.NOTE)] * 2
    ),
  ],
  ids=[
    "a dangling reference",
    "a duplicate note",
    "a deck name colliding in case only",
    "a second card from one note and template",
    "a repeated config key",
    "a tag name colliding in case only",
    "a duplicate review",
    "a duplicate grave",
  ],
)
def test_check_covers_every_individual_check(break_it):
  """A caller should not have to know each check by name, or which of them matters for
  the change it just made."""
  collection = a_collection()
  break_it(collection)
  with pytest.raises(ValueError):
    collection.check()


def test_writing_a_collection_checks_it_first(tmp_path):
  """The write is the boundary: after it, the disagreement is in a file that Anki will
  repair by deleting whichever half it likes less.

  A repeated config key is the breakage to use, because `write_sqlite()` collects the
  references first, and collecting mends every dangling reference a caller can build.
  """
  collection = a_collection()
  collection.config.extend([ConfigEntry(key="rollover"), ConfigEntry(key="rollover")])
  with pytest.raises(ValueError):
    collection.write_sqlite(tmp_path / "collection.anki2")


def a_differing_copy(model):
  """A distinct object with the same id, differing only in its sync marker."""
  copy = model.model_copy(deep=True)
  copy.update_sequence_n = model.update_sequence_n + 1
  return copy


def test_a_reference_is_resolved_by_id_and_not_by_value():
  """Anki matches a card to its note on `nid` alone (rslib/src/dbcheck.rs:190-197), so a
  card holding a note that differs from the collection's copy still writes a row that
  resolves, and the check has to say so."""
  collection = a_collection()
  differing = a_differing_copy(collection.notes[0])
  collection.cards[0] = Card(note=differing, deck=collection.decks[0])
  assert differing != collection.notes[0]
  assert collection.check_referential_integrity() is collection


def test_a_notetype_reference_is_resolved_by_id_too():
  collection = a_collection()
  differing = a_differing_copy(collection.notetypes[0])
  collection.notes[0].notetype = differing
  assert differing != collection.notetypes[0]
  assert collection.check_referential_integrity() is collection


def test_collecting_is_stricter_than_the_check_on_purpose():
  """The check asks whether a reference resolves, which an id answers. Collecting asks
  which object to write for that id, which needs the two to agree."""
  collection = a_collection()
  collection.cards[0] = Card(
    note=a_differing_copy(collection.notes[0]), deck=collection.decks[0]
  )
  assert collection.check_referential_integrity() is collection
  with pytest.raises(ValueError, match="share id"):
    collection.collect_notes()


# -------------------------------- Collecting references --------------------------------


def a_lone_card() -> Card:
  """A card holding a note, a notetype, a deck and a preset that no list holds."""
  deck = default_deck()
  deck.config = default_deck_config()
  return Card(note=a_note(builtin_notetype(BASIC_NOTETYPE.name)), deck=deck)


def test_a_note_reaches_the_collection_through_its_card():
  collection = Collection(cards=[a_lone_card()])
  assert collection.collect_notes().notes == [collection.cards[0].note]


def test_a_deck_reaches_the_collection_through_its_card():
  collection = Collection(cards=[a_lone_card()])
  assert collection.collect_decks().decks == [collection.cards[0].deck]


def test_a_preset_reaches_the_collection_through_its_deck():
  card = a_lone_card()
  collection = Collection(decks=[card.deck])
  assert collection.collect_deck_configs().deck_configs == [card.deck.config]


def test_a_deck_naming_its_preset_by_id_carries_nothing_to_collect():
  """`Deck.config` is the only source, so a deck that names preset 7 through
  `kind.config_id` alone leaves `Collection.warn_missing_deck_configs()` to report it."""
  collection = Collection(decks=[a_deck("Custom", kind={"config_id": 7})])
  assert collection.collect_deck_configs().deck_configs == []


def test_collecting_the_whole_chain_takes_one_pass():
  """Cards are the leaves: notes and decks come off the cards, and the notetypes and
  presets come off those, so the second half has to run after the first."""
  collection = Collection(cards=[a_lone_card()])
  collection.collect_references()
  assert [len(collection.notes), len(collection.decks)] == [1, 1]
  assert [len(collection.notetypes), len(collection.deck_configs)] == [1, 1]


def test_collecting_twice_collects_once():
  collection = Collection(cards=[a_lone_card()]).collect_references()
  before = [len(collection.notes), len(collection.notetypes), len(collection.decks)]
  collection.collect_references()
  assert [
    len(collection.notes),
    len(collection.notetypes),
    len(collection.decks),
  ] == before


def test_the_object_the_collection_already_holds_is_the_one_it_keeps():
  """Two equal notetypes under one id are one notetype, and the collection's own object
  is the one that gets written, so a caller holding it still describes the package."""
  held = builtin_notetype(BASIC_NOTETYPE.name)
  collection = Collection(
    notetypes=[held], notes=[a_note(builtin_notetype(BASIC_NOTETYPE.name))]
  )
  collection.collect_notetypes()
  assert len(collection.notetypes) == 1
  assert collection.notetypes[0] is held


@pytest.mark.parametrize("constant_first", [True, False])
def test_a_frozen_constant_and_a_fresh_copy_are_one_notetype(constant_first: bool):
  """The constant holds tuples where a fresh copy holds lists, so `==` tells them apart.
  Collecting compares content instead, whichever of the two is registered first."""
  notes = [a_note(BASIC_NOTETYPE), a_note(builtin_notetype(BASIC_NOTETYPE.name))]
  if not constant_first:
    notes.reverse()
  collection = Collection(notes=notes).collect_notetypes()
  assert len(collection.notetypes) == 1
  assert collection.notetypes[0] is notes[0].notetype


def test_two_unequal_models_sharing_an_id_are_refused():
  """Only one of them can be written, and picking either would drop content the caller
  described."""
  impostor = builtin_notetype(BASIC_NOTETYPE.name)
  impostor.name = "Impostor"
  collection = Collection(
    notetypes=[builtin_notetype(BASIC_NOTETYPE.name)], notes=[a_note(impostor)]
  )
  with pytest.raises(ValueError, match="share id"):
    collection.collect_notetypes()


def test_the_default_preset_is_added_when_a_deck_names_it():
  """Preset 1 is `DeckNormalKind.config_id`'s default, so a deck described with no
  scheduling at all names it without the caller ever building a preset."""
  collection = Collection(decks=[a_deck("Italian")])
  assert [
    preset.id for preset in collection.collect_default_deck_config().deck_configs
  ] == [1]


def test_the_default_preset_is_added_once_for_many_decks():
  collection = Collection(decks=[a_deck("A"), a_deck("B"), a_deck("C")])
  collection.collect_default_deck_config()
  assert collection.check_unique_ids() is collection


def test_the_default_preset_is_not_added_when_the_collection_holds_it():
  held = default_deck_config()
  collection = Collection(deck_configs=[held], decks=[a_deck("Italian")])
  assert collection.collect_default_deck_config().deck_configs == [held]


def test_a_preset_that_is_not_the_default_is_not_invented():
  """genanki2 holds no copy of a preset it did not write, so 7 has nothing to fall back
  on and stays the referential check's problem."""
  collection = Collection(decks=[a_deck("Custom", kind={"config_id": 7})])
  assert collection.collect_default_deck_config().deck_configs == []


def test_the_default_deck_is_added_when_no_deck_has_id_1():
  """A collection built from models has deck 1 only if the caller put it there, and a
  restore without it cannot be studied."""
  collection = Collection(decks=[a_deck("Italian")])
  collection.collect_default_deck()
  assert [(deck.id, deck.name) for deck in collection.decks][-1] == (1, "Default")


def test_the_default_deck_is_not_added_when_the_collection_holds_one():
  held = default_deck()
  collection = Collection(decks=[held])
  assert collection.collect_default_deck().decks == [held]


def test_a_deck_1_only_a_card_names_counts_as_present():
  """The decks are collected first. Appending the builtin before a card's own deck 1
  reached the list would put two unequal decks under id 1."""
  mine = a_deck("Mine", id=1)
  collection = Collection(cards=[Card(note=a_note(), deck=mine)])
  assert collection.collect_default_deck().decks == [mine]


def test_a_caller_deck_named_default_clashes_with_the_added_one():
  """The added deck is named `Default` whatever the collection holds, so a caller's own
  `Default` under another id is left to the name check rather than renamed."""
  collection = Collection(decks=[a_deck("Default")]).collect_default_deck()
  with pytest.raises(ValueError, match="duplicate names"):
    collection.check_unique_names()


def test_a_parent_is_created_for_a_deck_reached_through_a_card():
  """`create_missing_parent_decks()` reads `decks`, so it collects first or it cannot
  see a deck that only a card names."""
  card = a_lone_card()
  card.deck = a_deck("Italian::Verbs")
  collection = Collection(cards=[card])
  assert deck_names(collection.create_missing_parent_decks()) == {
    "Italian",
    "Italian::Verbs",
  }


def test_writing_a_collection_collects_before_it_checks(tmp_path):
  """A caller appends the cards and the lists that follow from them fill themselves,
  so a collection holding nothing else still writes a whole package."""
  path = tmp_path / "collection.anki2"
  Collection(cards=[a_lone_card()]).write_sqlite(path)
  assert path.exists()


# ------------------------------------ Parent decks ------------------------------------


def deck_names(collection: Collection) -> set[str]:
  return {deck.name for deck in collection.decks}


def test_a_subdeck_on_its_own_gets_its_parent():
  """Anki creates the missing parent on import (`create_missing_parents`,
  rslib/src/decks/addupdate.rs:140-149), so the deck the user ends up with either way is
  the same. What changes is who described it."""
  collection = Collection(decks=[a_deck("Italian::Verbs")])
  assert deck_names(collection.create_missing_parent_decks()) == {
    "Italian",
    "Italian::Verbs",
  }


def test_every_ancestor_is_created_not_only_the_immediate_one():
  collection = Collection(decks=[a_deck("Italian::Verbs::Irregular")])
  assert deck_names(collection.create_missing_parent_decks()) == {
    "Italian",
    "Italian::Verbs",
    "Italian::Verbs::Irregular",
  }


def test_a_top_level_deck_needs_nothing():
  collection = Collection(decks=[DEFAULT_DECK])
  assert deck_names(collection.create_missing_parent_decks()) == {"Default"}


def test_a_parent_the_collection_already_has_is_left_alone():
  """Including the case where the parent comes second in the list, since the deck a
  caller appends last is as likely to be the parent as the child."""
  parent = a_deck("Italian")
  collection = Collection(decks=[a_deck("Italian::Verbs"), parent])
  collection.create_missing_parent_decks()
  assert collection.decks.count(parent) == 1
  assert len(collection.decks) == 2


def test_a_parent_differing_only_in_case_is_the_same_parent():
  """The deck name index is UNIQUE `COLLATE unicase`, so creating `Italian` beside an
  existing `italian` writes a row SQLite refuses. Anki goes further and rewrites the
  child to match the parent's case (`match_or_create_parents`, addupdate.rs:114-126);
  genanki2 leaves the name the caller wrote, and the tree still nests, because Anki
  builds it by unicase comparison too."""
  collection = Collection(decks=[a_deck("italian"), a_deck("Italian::Verbs")])
  assert deck_names(collection.create_missing_parent_decks()) == {
    "italian",
    "Italian::Verbs",
  }


def test_two_children_share_one_parent():
  collection = Collection(
    decks=[a_deck("Italian::Verbs"), a_deck("Italian::Nouns")]
  ).create_missing_parent_decks()
  assert len(collection.decks) == 3


def test_creating_parents_twice_creates_them_once():
  """A caller that builds decks incrementally has no way to know whether it already
  ran."""
  collection = Collection(
    decks=[a_deck("Italian::Verbs")]
  ).create_missing_parent_decks()
  before = list(collection.decks)
  assert collection.create_missing_parent_decks().decks == before


def test_a_created_parent_is_the_deck_anki_would_have_created():
  """`add_parent_deck` builds `Deck::new_normal()` (rslib/src/decks/addupdate.rs:103-108,
  decks/mod.rs:64-81): a normal deck on config 1, collapsed in both trees. That is the
  `Default` deck's pair of blobs byte for byte, which is where these come from."""
  collection = Collection(
    decks=[a_deck("Italian::Verbs")]
  ).create_missing_parent_decks()
  parent = next(deck for deck in collection.decks if deck.name == "Italian")
  row, default_row = parent.to_row(), DEFAULT_DECK.to_row()
  assert (row["common"], row["kind"]) == (default_row["common"], default_row["kind"])
  assert parent.id > 0


def test_a_collection_with_created_parents_still_passes_every_check():
  collection = a_collection()
  collection.decks.append(a_deck("Italian::Verbs"))
  collection.create_missing_parent_decks()
  assert collection.check() is collection


def test_a_missing_parent_is_not_a_reason_to_refuse_the_collection():
  """Anki imports `Italian::Verbs` alone without complaint, so refusing it here would
  reject a package Anki accepts. Creating parents is a choice the caller makes, which is
  why it is not part of `check`."""
  collection = a_collection()
  collection.decks.append(a_deck("Italian::Verbs"))
  assert collection.check() is collection


# ----------------------------- Unused decks and notetypes -----------------------------


def unused_deck_names(collection: Collection) -> list[str]:
  return [deck.name for deck in collection.unused_decks]


def a_collection_with_a_card_in(*names: str) -> Collection:
  """The agreeing collection, plus a deck for each name and a card in the last one."""
  collection = a_collection()
  decks = [a_deck(name) for name in names]
  collection.decks.extend(decks)
  note = a_note(collection.notetypes[0])
  collection.notes.append(note)
  collection.cards.append(Card(note=note, deck=decks[-1]))
  return collection


def test_a_collection_whose_lists_agree_uses_every_deck_and_notetype():
  collection = a_collection()
  assert [collection.unused_decks, collection.unused_notetypes] == [(), ()]


def test_a_deck_no_card_is_in_is_unused():
  collection = a_collection()
  collection.decks.append(a_deck("Empty"))
  assert unused_deck_names(collection) == ["Empty"]


def test_the_default_deck_is_unused_when_no_card_is_in_it():
  """The import warning leaves it out, since every target collection has deck 1, but
  the value itself reports it like any other deck."""
  collection = a_collection_with_a_card_in("Italian")
  collection.cards = collection.cards[1:]
  assert unused_deck_names(collection) == ["Default"]


def test_every_ancestor_of_a_deck_holding_a_card_is_used():
  collection = a_collection_with_a_card_in(
    "Italian", "Italian::Verbs", "Italian::Verbs::Irregular"
  )
  assert unused_deck_names(collection) == []


def test_a_deck_above_a_gap_in_the_hierarchy_is_unused():
  """The import walks up by name and stops at the first parent the package lacks
  (`add_parent_decks`, rslib/src/import_export/gather.rs:286-301), so `Italian` is
  skipped and Anki creates a blank one in its place."""
  collection = a_collection_with_a_card_in("Italian", "Italian::Verbs::Irregular")
  assert unused_deck_names(collection) == ["Italian"]


def test_a_parent_differing_only_in_case_is_used():
  """Anki looks the parent up by name, and the deck name index is `COLLATE unicase`."""
  collection = a_collection_with_a_card_in("italian", "Italian::Verbs")
  assert unused_deck_names(collection) == []


def test_a_sibling_of_a_deck_holding_a_card_is_unused():
  collection = a_collection_with_a_card_in(
    "Italian::Nouns", "Italian", "Italian::Verbs"
  )
  assert unused_deck_names(collection) == ["Italian::Nouns"]


def test_a_home_deck_holding_no_card_is_unused():
  """Without scheduling, the import gathers only the decks the cards are in, so a home
  deck arrives only with "Import any learning progress"."""
  home = a_deck("Home")
  collection = a_collection_with_a_filtered_card(home)
  collection.decks.append(home)
  assert unused_deck_names(collection) == ["Home"]


def test_a_parent_found_through_a_home_deck_no_list_holds_is_used():
  """The write collects `Italian::Verbs` off the card, so the import finds it and walks
  on to `Italian`."""
  collection = a_collection_with_a_filtered_card(a_deck("Italian::Verbs"))
  collection.decks[-1].name = "Italian::Verbs::Cram"
  collection.decks.append(a_deck("Italian"))
  assert unused_deck_names(collection) == []


def test_a_deck_known_only_through_a_review_is_used():
  collection = a_collection()
  deck = a_deck("Reviewed")
  collection.decks.append(deck)
  note = a_note(collection.notetypes[0])
  collection.reviews.append(a_review(Card(note=note, deck=deck)))
  assert unused_deck_names(collection) == []


def test_a_notetype_no_note_uses_is_unused():
  collection = a_collection()
  cloze = builtin_notetype("Cloze (genanki2)")
  collection.notetypes.append(cloze)
  assert collection.unused_notetypes == (cloze,)


def test_a_notetype_used_by_a_note_only_a_card_holds_is_used():
  collection = a_collection()
  collection.notes = []
  assert collection.unused_notetypes == ()


def test_a_notetype_used_by_a_note_only_a_review_holds_is_used():
  collection = a_collection()
  notetype = builtin_notetype(BASIC_AND_REVERSED_NOTETYPE.name)
  collection.notetypes.append(notetype)
  card = Card(note=a_note(notetype), deck=collection.decks[0])
  collection.reviews.append(a_review(card))
  assert collection.unused_notetypes == ()


def test_reading_the_unused_values_changes_no_list():
  collection = Collection(cards=[a_lone_card()])
  assert [collection.unused_decks, collection.unused_notetypes] == [(), ()]
  assert [collection.decks, collection.notes, collection.notetypes] == [[], [], []]


# ------------------------------ Unused, as it is warned -------------------------------

APKG = SerializationContext(output=SerializationContext.Output.APKG)
COLPKG = SerializationContext(output=SerializationContext.Output.COLPKG)
APKG_MESSAGE = "An apkg import creates only"
GENERIC_MESSAGES = ("If imported from an apkg", "A restored colpkg keeps all of them")

# Written collections carry it so that the writer's v1 scheduler warning stays out of
# the way of what these tests are about.
SCHEDULER_V2 = [ConfigEntry(key="schedVer", value=b"2")]


def skipped_warnings(action) -> list[str]:
  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always", Genanki2Warning)
    action()
  return [str(w.message) for w in caught if "never reach the user" in str(w.message)]


def test_an_empty_deck_and_an_unused_notetype_are_named_in_one_warning():
  collection = a_collection()
  collection.decks.append(a_deck("Empty"))
  collection.notetypes.append(builtin_notetype("Cloze (genanki2)"))
  [message] = skipped_warnings(lambda: collection.warn_import_skips_unused(APKG))
  assert message.endswith('\n  Decks: "Empty"\n  Note types: "Cloze (genanki2)"')
  assert "learning progress" not in message


def test_an_unused_default_deck_warns_about_nothing():
  """Every target collection has deck 1, so the import skipping it costs nothing."""
  collection = a_collection_with_a_card_in("Italian")
  collection.cards = collection.cards[1:]
  assert unused_deck_names(collection) == ["Default"]
  assert skipped_warnings(lambda: collection.warn_import_skips_unused(APKG)) == []


def test_an_unused_home_deck_is_said_to_arrive_with_learning_progress():
  home = a_deck("Home")
  collection = a_collection_with_a_filtered_card(home)
  collection.decks.append(home)
  [message] = skipped_warnings(lambda: collection.warn_import_skips_unused(APKG))
  assert "learning progress" in message


@pytest.mark.parametrize(
  ("write", "expected"),
  [
    pytest.param(
      lambda col, d: Package(collection=col).write_apkg(d / "deck.apkg"),
      (APKG_MESSAGE,),
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
      lambda col, d: col.write_sqlite(d / "collection.anki2"),
      GENERIC_MESSAGES,
      id="sqlite",
    ),
    pytest.param(lambda col, d: col.warn(), GENERIC_MESSAGES, id="no context"),
  ],
)
def test_an_empty_deck_is_reported_for_what_the_write_produces(
  tmp_path, write, expected
):
  """A restore keeps every deck and notetype, so only an apkg import skips one."""
  collection = a_collection(config=list(SCHEDULER_V2))
  collection.decks.append(a_deck("Empty"))
  messages = skipped_warnings(lambda: write(collection, tmp_path))

  if expected is None:
    assert messages == []
  else:
    assert len(messages) == 1
    assert all(text in messages[0] for text in expected)
    assert messages[0].endswith('\n  Decks: "Empty"')


# ----------------------------------- Scheduling keys -----------------------------------


def test_a_new_collection_is_zero_days_old():
  """`crt` defaults to today's rollover, so no day has turned over since."""
  assert Collection().days_elapsed_since_creation == 0


def test_the_day_count_reads_the_two_scheduling_keys():
  """The pair the export gathers together (rslib/src/import_export/gather.rs:42-43),
  pinned by spelling: a key read under the wrong name falls back to Anki's default, and
  the count comes out plausible and wrong rather than absent."""
  created = CollectionMetadata().creation_time_s
  collection = Collection(
    metadata=CollectionMetadata(creation_time_s=created),
    config=[
      ConfigEntry(key="rollover", value=b"23"),
      ConfigEntry(key="creationOffset", value=b"-720"),
    ],
  )
  assert collection.days_elapsed_since_creation == days_elapsed(
    created, creation_offset_minutes_west=-720, rollover_hour=23
  )


def test_a_scheduling_key_that_cannot_be_decoded_counts_as_unset():
  """Anki reads an undecodable value as a key that was never written
  (rslib/src/config/mod.rs:107-121), and the rollover it then substitutes is 4."""
  collection = Collection(config=[ConfigEntry(key="rollover", value=b"")])
  assert collection.days_elapsed_since_creation == 0


def test_a_config_entry_that_is_not_there_is_none():
  assert Collection().get_config_entry("rollover") is None


def test_a_default_collection_takes_its_creation_offset_at_its_creation_time():
  """Anki stores the local offset at `crt`, not at the moment it runs
  (rslib/src/storage/sqlite.rs:513-517). Moscow was UTC+4 in 2012 and is UTC+3 now, so
  an offset taken at the current time comes out 180 instead of 240."""
  created = datetime(2012, 6, 1, 4, tzinfo=ZoneInfo("Europe/Moscow"))
  try:
    with pytest.MonkeyPatch.context() as patch:
      patch.setenv("TZ", "Europe/Moscow")
      time.tzset()
      patch.setattr(collection_metadata, "rollover_datetime", lambda: created)
      collection = Collection.default()
  finally:
    time.tzset()

  assert collection.metadata.creation_time_s == int(created.timestamp())
  offset = collection.get_config_entry("creationOffset")
  assert offset is not None
  assert offset.value_or() == -240


# -------------------------------------- Home decks -------------------------------------


A_DECK_ID_THE_COLLECTION_DOES_NOT_HOLD = 999_999


def a_filtered_deck(name: str = "Filtered") -> Deck:
  return Deck(name=name, kind=DeckFilteredKind())


def a_collection_with_a_filtered_card(original_deck: int | Deck) -> Collection:
  """The agreeing collection, plus a filtered deck holding one more card.

  The card is on a note of its own: a second card on the first note would repeat a
  `(nid, ord)` pair, which is another check's business.
  """
  collection = a_collection()
  filtered = a_filtered_deck()
  collection.decks.append(filtered)
  note = a_note(collection.notetypes[0])
  collection.notes.append(note)
  collection.cards.append(Card(note=note, deck=filtered, odid=original_deck, odue=1))
  return collection


def test_a_card_whose_original_deck_id_names_a_filtered_deck_is_refused():
  """The half a card cannot answer for itself, since it holds the id and not the deck."""
  other = a_filtered_deck("Other")
  collection = a_collection_with_a_filtered_card(other.id)
  collection.decks.append(other)
  with pytest.raises(ValueError, match="names filtered deck 'Other'"):
    collection.check_card_original_deck_is_normal()


def test_a_card_is_repaired_before_the_collection_judges_it(tmp_path):
  """A card cannot tell when its filtered deck turns normal, so it keeps the original
  deck until `Card.prepare()` clears it. `write_sqlite()` prepares every model before
  it checks any, so the collection judges the card after its repair instead of refusing
  an original deck the repair was about to clear."""
  other = a_filtered_deck("Other")
  collection = a_collection_with_a_filtered_card(other.id)
  collection.decks.append(other)
  card = collection.cards[-1]
  card.deck.kind = DeckNormalKind()

  with pytest.warns(UserWarning, match="were cleared"):
    collection.write_sqlite(tmp_path / "collection.anki2")
  assert card.original_deck_id == 0


def test_a_card_whose_original_deck_id_names_a_normal_deck_passes():
  home = a_deck("Home")
  collection = a_collection_with_a_filtered_card(home.id)
  collection.decks.append(home)
  assert collection.check_card_original_deck_is_normal() is collection


def test_an_original_deck_id_the_collection_does_not_hold_is_not_refused():
  """Anki writes packages in that state, so it is reported rather than rejected."""
  collection = a_collection_with_a_filtered_card(A_DECK_ID_THE_COLLECTION_DOES_NOT_HOLD)
  assert collection.check_card_original_deck_is_normal() is collection


def test_a_dangling_original_deck_id_is_reported():
  collection = a_collection_with_a_filtered_card(A_DECK_ID_THE_COLLECTION_DOES_NOT_HOLD)
  with pytest.warns(
    UserWarning,
    match=f"names original deck {A_DECK_ID_THE_COLLECTION_DOES_NOT_HOLD}",
  ) as reported:
    collection.warn_filtered_decks_become_normal()
  assert len(reported) == 1


def test_an_original_deck_id_of_one_is_never_reported(recwarn):
  """Deck 1 is in every collection Anki opens, so a package need not carry it."""
  filtered = a_filtered_deck()
  note = a_note()
  collection = Collection(
    decks=[filtered],
    notes=[note],
    cards=[Card(note=note, deck=filtered, odid=1, odue=1)],
  )
  collection.warn_filtered_decks_become_normal()
  assert len(recwarn) == 0


def test_an_original_deck_the_collection_holds_is_not_reported(recwarn):
  home = a_deck("Home")
  collection = a_collection_with_a_filtered_card(home.id)
  collection.decks.append(home)
  collection.warn_filtered_decks_become_normal()
  assert len(recwarn) == 0


def test_every_dangling_card_is_named_in_one_warning():
  """A package that strands many cards reports once, the way both neighbouring warnings
  do, instead of once per card."""
  collection = a_collection_with_a_filtered_card(A_DECK_ID_THE_COLLECTION_DOES_NOT_HOLD)
  filtered = next(deck for deck in collection.decks if deck.is_filtered())
  note = a_note(collection.notetypes[0])
  collection.notes.append(note)
  collection.cards.append(
    Card(
      note=note,
      deck=filtered,
      odid=A_DECK_ID_THE_COLLECTION_DOES_NOT_HOLD + 1,
      odue=1,
    )
  )
  with pytest.warns(UserWarning) as reported:
    collection.warn_filtered_decks_become_normal()
  assert len(reported) == 1
  assert str(reported[0].message).count("names original deck") == 2


def test_a_card_stranded_in_a_filtered_deck_is_reported():
  """The case that used to be refused on the card. Anki converts the deck instead of
  failing, so a collection is the only thing that can see what that costs."""
  collection = a_collection()
  filtered = a_filtered_deck()
  collection.decks.append(filtered)
  note = a_note(collection.notetypes[0])
  collection.notes.append(note)
  collection.cards.append(Card(note=note, deck=filtered))
  with pytest.warns(UserWarning, match="carries no original deck") as reported:
    collection.warn_filtered_decks_become_normal()
  assert len(reported) == 1


@pytest.mark.parametrize(
  ("write", "expected"),
  [
    pytest.param(
      lambda col, d: Package(collection=col).write_apkg(d / "deck.apkg"),
      "An apkg import with learning progress turns",
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
      lambda col, d: col.write_sqlite(d / "collection.anki2"),
      "If imported from an apkg with learning progress, Anki turns",
      id="sqlite",
    ),
    pytest.param(
      lambda col, d: col.warn(),
      "If imported from an apkg with learning progress, Anki turns",
      id="no context",
    ),
  ],
)
def test_a_dangling_original_deck_is_reported_for_what_the_write_produces(
  tmp_path, write, expected
):
  """A restore keeps every deck as it is, so only an apkg import converts one."""
  collection = a_collection_with_a_filtered_card(A_DECK_ID_THE_COLLECTION_DOES_NOT_HOLD)
  collection.config = list(SCHEDULER_V2)
  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always", Genanki2Warning)
    write(collection, tmp_path)

  messages = [str(w.message) for w in caught if "into normal decks" in str(w.message)]
  if expected is None:
    assert messages == []
  else:
    assert len(messages) == 1
    assert messages[0].startswith(f"Collection: {expected} the filtered decks")


# ------------------------------ Subdecks of a filtered deck ---------------------------


def nonleaf_warnings(action) -> list[str]:
  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always", Genanki2Warning)
    action()
  return [str(w.message) for w in caught if "cannot hold subdecks" in str(w.message)]


def a_collection_with_a_subdeck_under(filtered: Deck, *names: str) -> Collection:
  collection = a_collection(config=list(SCHEDULER_V2))
  collection.decks.extend([filtered, *(a_deck(name) for name in names)])
  return collection


def test_a_subdeck_of_a_filtered_deck_is_named():
  collection = a_collection_with_a_subdeck_under(a_filtered_deck(), "Filtered::Sub")
  [message] = nonleaf_warnings(collection.warn_nonleaf_filtered_decks)
  assert message.endswith("\n  'Filtered::Sub' under 'Filtered'")


def test_a_deeper_descendant_is_named_too():
  """Anki checks the first existing parent, so `Filtered::Sub::Deep` is moved with
  `Filtered::Sub` on import, and both are listed."""
  collection = a_collection_with_a_subdeck_under(
    a_filtered_deck(), "Filtered::Sub", "Filtered::Sub::Deep"
  )
  [message] = nonleaf_warnings(collection.warn_nonleaf_filtered_decks)
  assert "'Filtered::Sub::Deep' under 'Filtered'" in message


def test_a_filtered_parent_differing_only_in_case_is_found():
  """Anki looks the parent up by name, and the deck name index is `COLLATE unicase`."""
  collection = a_collection_with_a_subdeck_under(a_filtered_deck(), "filtered::Sub")
  [message] = nonleaf_warnings(collection.warn_nonleaf_filtered_decks)
  assert "'filtered::Sub' under 'filtered'" in message


def test_a_filtered_deck_with_no_subdeck_says_nothing():
  collection = a_collection_with_a_subdeck_under(a_filtered_deck(), "Other::Sub")
  assert nonleaf_warnings(collection.warn_nonleaf_filtered_decks) == []


@pytest.mark.parametrize(
  ("write", "expected"),
  [
    pytest.param(
      lambda col, d: Package(collection=col).write_apkg(d / "deck.apkg"),
      ("An apkg import with learning progress moves them",),
      id="apkg",
    ),
    pytest.param(
      lambda col, d: CollectionPackage(collection=col).write_colpkg(
        d / "collection.colpkg"
      ),
      ("A restore keeps them as they are",),
      id="colpkg",
    ),
    pytest.param(
      lambda col, d: col.write_sqlite(d / "collection.anki2"),
      ("If imported from an apkg with learning progress", "A restored colpkg"),
      id="sqlite",
    ),
    pytest.param(
      lambda col, d: col.warn(),
      ("If imported from an apkg with learning progress", "A restored colpkg"),
      id="no context",
    ),
  ],
)
def test_a_subdeck_of_a_filtered_deck_is_reported_for_what_the_write_produces(
  tmp_path, write, expected
):
  """An import repairs the nesting and a restore keeps it, so both get a warning."""
  collection = a_collection_with_a_subdeck_under(a_filtered_deck(), "Filtered::Sub")
  messages = nonleaf_warnings(lambda: write(collection, tmp_path))

  assert len(messages) == 1
  assert all(text in messages[0] for text in expected)


def test_a_filtered_deck_whose_cards_name_a_home_deck_is_not_reported(recwarn):
  """The package that keeps its filtered decks, which the warning must not name."""
  home = a_deck("Home")
  collection = a_collection_with_a_filtered_card(home)
  collection.decks.append(home)
  collection.warn_filtered_decks_become_normal()
  assert len(recwarn) == 0


def test_a_collection_with_no_filtered_deck_is_never_reported(recwarn):
  """Nothing to convert, so an offending card costs the package nothing. The pair is
  written past the repair that would clear it, which is the only way a card in a normal
  deck reaches the loop at all."""
  collection = a_collection()
  card = collection.cards[0]
  object.__setattr__(card, "original_deck", A_DECK_ID_THE_COLLECTION_DOES_NOT_HOLD)
  collection.warn_filtered_decks_become_normal()
  assert len(recwarn) == 0


def test_a_collection_whose_lists_agree_says_nothing(recwarn):
  """Every card written so far is in a normal deck with no original deck and no
  original due, which no check may refuse and no warning may name. The config entries
  are there because a collection without them draws the scheduler warning, which is
  about the `col` row rather than about any card."""
  collection = a_collection(config=default_config_entries())
  assert collection.check() is collection
  collection.warn()
  assert len(recwarn) == 0


# -------------------------------------- Getters ---------------------------------------


@pytest.mark.parametrize(
  "getter, keys",
  [
    ("get_deck", ("id", "name")),
    ("get_deck_config", ("id", "name")),
    ("get_notetype", ("id", "name")),
    ("get_note", ("id", "guid")),
  ],
)
@pytest.mark.parametrize("passed", ["both", "neither"])
def test_a_getter_takes_exactly_one_key(getter: str, keys: tuple[str, str], passed):
  arguments = {keys[0]: 1, keys[1]: "x"} if passed == "both" else {}
  with pytest.raises(Genanki2Error, match="exactly one"):
    getattr(Collection(), getter)(**arguments)


def test_a_deck_is_found_by_id():
  deck = a_deck("Italian")
  assert Collection(decks=[a_deck("Other"), deck]).get_deck(id=deck.id) is deck


def test_a_deck_name_is_normalized_and_compared_without_case():
  """The name is looked up the way Anki stores it, so `italian::verbs` finds
  `Italian::Verbs`, and so do the spaces Anki trims around each part."""
  deck = a_deck("Italian::Verbs")
  collection = Collection(decks=[deck])
  assert collection.get_deck(name="italian::verbs") is deck
  assert collection.get_deck(name=" ITALIAN :: Verbs ") is deck


def test_a_deck_name_with_an_empty_part_is_rejected():
  with pytest.raises(ValueError, match="empty part"):
    Collection().get_deck(name="Italian::")


def test_a_missing_deck_is_none():
  collection = Collection(decks=[a_deck("Italian")])
  assert collection.get_deck(id=12345) is None
  assert collection.get_deck(name="French") is None


def test_a_preset_is_found_by_id_and_by_name_without_case():
  preset = DeckConfig(name="Slow")
  collection = Collection(deck_configs=[DeckConfig(name="Fast"), preset])
  assert collection.get_deck_config(id=preset.id) is preset
  assert collection.get_deck_config(name="SLOW") is preset
  assert collection.get_deck_config(name="Medium") is None


def test_of_two_presets_sharing_a_name_the_first_is_found():
  """Anki lets presets share a name."""
  first, second = DeckConfig(name="Slow"), DeckConfig(name="slow")
  collection = Collection(deck_configs=[first, second])
  assert collection.get_deck_config(name="Slow") is first


def test_a_notetype_is_found_by_id_and_by_normalized_name():
  """Anki drops double quotes from a notetype name, so the lookup drops them too."""
  notetype = builtin_notetype(BASIC_NOTETYPE.name)
  collection = Collection(notetypes=[notetype])
  assert collection.get_notetype(id=notetype.id) is notetype
  assert collection.get_notetype(name=f'"{notetype.name.upper()}"') is notetype
  assert collection.get_notetype(name="Missing") is None


def test_an_empty_notetype_name_is_rejected():
  with pytest.raises(ValueError, match="empty"):
    Collection().get_notetype(name='""')


def test_a_note_is_found_by_id_and_by_guid():
  note = a_note(BASIC_NOTETYPE)
  collection = Collection(notes=[a_note(BASIC_NOTETYPE), note])
  assert collection.get_note(id=note.id) is note
  assert collection.get_note(guid=note.guid) is note


def test_a_guid_is_compared_with_its_case():
  """A guid is an identifier, not a name, and Anki matches it exactly."""
  note = a_note(BASIC_NOTETYPE)
  note.guid = "AbCdEf"
  collection = Collection(notes=[note])
  assert collection.get_note(guid="abcdef") is None


# --------------------------------- New-card counter -----------------------------------


def a_new_card(due: int, deck: Deck | None = None) -> Card:
  return Card(note=a_note(BASIC_NOTETYPE), deck=deck or a_deck("Home"), due=due)


def counter(collection: Collection) -> object:
  entry = collection.get_config_entry("nextPos")
  assert entry is not None
  return entry.value_or()


def a_collection_with_counter(value: int, cards: list[Card]) -> Collection:
  entry = ConfigEntry(key="nextPos")
  entry.set_value(value)
  return Collection(config=[entry], cards=cards)


@pytest.mark.parametrize("start", [1, 500], ids=["behind", "ahead"])
def test_the_repair_sets_the_counter_one_past_the_highest_new_position(start: int):
  """Anki's "Check Database" sets it to `max(due) + 1` over the new cards whichever
  way it was off (`rslib/src/dbcheck.rs:437-440`, `storage/card/mod.rs:438-443`)."""
  collection = a_collection_with_counter(start, [a_new_card(3), a_new_card(41)])
  assert collection.repair_next_new_card_position() is collection
  assert counter(collection) == 42


def test_the_repair_reads_only_new_cards():
  """A review card's `due` is a day number, not a queue position."""
  review_card = Card(
    note=a_note(BASIC_NOTETYPE),
    deck=a_deck("Home"),
    card_type=CardType.REVIEW,
    queue=CardQueue.REVIEW,
    due=900,
    interval_days=3,
  )
  collection = a_collection_with_counter(1, [a_new_card(3), review_card])
  collection.repair_next_new_card_position()
  assert counter(collection) == 4


def test_the_repair_adds_a_missing_counter():
  collection = Collection(cards=[a_new_card(7)])
  collection.repair_next_new_card_position()
  assert counter(collection) == 8


def test_the_repair_counts_a_card_only_a_review_holds():
  card = a_new_card(9)
  collection = a_collection_with_counter(1, [])
  collection.reviews.append(a_review(card))
  collection.repair_next_new_card_position()
  assert counter(collection) == 10


def test_without_new_cards_the_counter_is_0():
  """`max(due) + 1` is NULL, which Anki reads as 0."""
  collection = a_collection_with_counter(5, [])
  collection.repair_next_new_card_position()
  assert counter(collection) == 0


def test_a_new_card_in_a_filtered_deck_is_read_by_its_negative_due():
  """Anki reads `due` even there, and a negative `max(due) + 1` fails to read as a u32,
  which Anki also turns into 0."""
  filtered = a_deck("Filtered", kind=DeckFilteredKind())
  card = Card(
    note=a_note(BASIC_NOTETYPE),
    deck=filtered,
    due=-100_000,
    original_deck=a_deck("Home"),
    original_due=7,
  )
  collection = a_collection_with_counter(5, [card])
  collection.repair_next_new_card_position()
  assert counter(collection) == 0


# -------------------------------- Default collections ---------------------------------


def test_a_default_collection_holds_the_config_and_nothing_else():
  collection = Collection.default()
  assert [entry.key for entry in collection.config] == [
    entry.key for entry in default_config_entries()
  ]
  assert collection.decks == []
  assert collection.deck_configs == []
  assert collection.notetypes == []


def test_an_anki_default_collection_holds_what_a_new_anki_collection_holds():
  collection = Collection.anki_default()
  assert [entry.key for entry in collection.config] == [
    entry.key for entry in default_config_entries()
  ]
  assert collection.decks == [DEFAULT_DECK]
  assert collection.deck_configs == [DEFAULT_DECK_CONFIG]
  assert collection.notetypes == list(BUILTIN_NOTETYPES)


def test_an_anki_default_collection_passes_every_check(recwarn):
  """It is built for a colpkg, where the stock notetypes no note uses are kept."""
  collection = Collection.anki_default()
  assert collection.check(context=COLPKG) is collection
  collection.warn(context=COLPKG)
  assert len(recwarn) == 0
