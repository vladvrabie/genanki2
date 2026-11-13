"""Does writing back a read deck warn when an apkg import would drop the change?

    uv run --with anki --with pytest pytest tests/test_read_deck_changes.py

Anki's importer matches an incoming deck by name, and when the user already has a deck
of that name and kind it keeps the user's deck and copies over part of the incoming one
only (`update_deck` and `update_normal_with_other` in
`rslib/src/import_export/package/apkg/import/decks.rs:134-195`): the description when it
is not empty, the preset when it is not 1, and each of the four limits when it is set.
The desired retention is never copied. Emptying a description, putting a deck back on
preset 1, clearing a limit or changing the desired retention therefore never reaches a
user who imported the deck before. A renamed deck is added as a new one, and a filtered
deck has its kind replaced whole, so neither loses anything.

The last section checks those claims against the anki wheel.
"""

import warnings
from collections.abc import Callable
from pathlib import Path

import pytest
from anki.collection import Collection as AnkiCollection
from anki.collection import ImportAnkiPackageRequest
from anki.import_export_pb2 import ImportAnkiPackageOptions

from genanki2.builtins import (
  BASIC_NOTETYPE,
  DEFAULT_DECK,
  DEFAULT_DECK_CONFIG,
  default_config_entries,
)
from genanki2.errors import Genanki2Warning
from genanki2.models.card import Card
from genanki2.models.collection import Collection
from genanki2.models.config import ConfigEntry
from genanki2.models.deck import Deck, DeckFilteredKind, DeckNormalKind
from genanki2.models.deck_config import DeckConfig
from genanki2.models.note import Note
from genanki2.models.package import CollectionPackage, Package
from genanki2.models.serialization_context import SerializationContext

SCHEDULER_V2 = [ConfigEntry(key="schedVer", value=b"2")]
APKG = SerializationContext(output=SerializationContext.Output.APKG)
MATCH = 'Deck: "'

DECK_NAME = "Italian::Verbs"
PRESET_ID = 1_700_000_000_000
DESCRIPTION = "Shipped with the package"
REVIEW_LIMIT = 50
NEW_LIMIT = 5
RETENTION = 0.85  # comes back as 0.8500000238418579 from the 32-bit field


def a_kind(**overrides) -> DeckNormalKind:
  """A normal deck with every setting the import treats specially set."""
  values = {
    "config_id": PRESET_ID,
    "description": DESCRIPTION,
    "review_limit": REVIEW_LIMIT,
    "new_limit": NEW_LIMIT,
    "review_limit_today": DeckNormalKind.DayLimit(limit=100, today=3),
    "new_limit_today": DeckNormalKind.DayLimit(limit=10, today=3),
    "desired_retention": RETENTION,
  }
  return DeckNormalKind(**(values | overrides))


def read_deck(kind: DeckNormalKind | DeckFilteredKind | None = None) -> Deck:
  """A deck written to a row and read back, as last release's file."""
  deck = Deck(name=DECK_NAME, kind=kind if kind is not None else a_kind())
  with warnings.catch_warnings():
    warnings.simplefilter("ignore", Genanki2Warning)
    return Deck.from_row(deck.to_row())


def change_warnings(
  deck: Deck, context: SerializationContext | None = APKG
) -> list[str]:
  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always", Genanki2Warning)
    deck.warn_unimported_changes(context)
  return [str(w.message) for w in caught]


def normal(deck: Deck) -> DeckNormalKind:
  assert isinstance(deck.kind, DeckNormalKind)
  return deck.kind


# ------------------------------- What the import drops --------------------------------


def empty_description(deck: Deck) -> None:
  normal(deck).description = ""


def back_to_preset_1(deck: Deck) -> None:
  normal(deck).config_id = 1


def clear(limit: str) -> Callable[[Deck], None]:
  return lambda deck: setattr(normal(deck), limit, None)


def change_retention(deck: Deck) -> None:
  normal(deck).desired_retention = 0.95


def unset_retention(deck: Deck) -> None:
  normal(deck).desired_retention = None


@pytest.mark.parametrize(
  ("edit", "line"),
  [
    pytest.param(empty_description, "  description: emptied", id="description"),
    pytest.param(
      back_to_preset_1, f"  config_id: set back to 1 from {PRESET_ID}", id="preset"
    ),
    *(
      pytest.param(clear(limit), f"  {limit}: cleared", id=limit)
      for limit in (
        "review_limit",
        "new_limit",
        "review_limit_today",
        "new_limit_today",
      )
    ),
    pytest.param(
      change_retention,
      "  desired_retention: changed from 0.85 to 0.95",
      id="retention-changed",
    ),
    pytest.param(
      unset_retention,
      "  desired_retention: changed from 0.85 to unset",
      id="retention-unset",
    ),
  ],
)
def test_a_dropped_edit_is_listed(edit: Callable[[Deck], None], line: str):
  deck = read_deck()
  edit(deck)
  [message] = change_warnings(deck)
  assert message.startswith(f'Deck: "{DECK_NAME}" changed since it was read.')
  assert message.splitlines()[1:] == [line]


def test_every_dropped_edit_is_listed_in_one_warning():
  deck = read_deck()
  for edit in (empty_description, back_to_preset_1, clear("new_limit")):
    edit(deck)
  [message] = change_warnings(deck)
  assert message.splitlines()[1:] == [
    "  description: emptied",
    f"  config_id: set back to 1 from {PRESET_ID}",
    "  new_limit: cleared",
  ]


def test_setting_a_retention_on_a_deck_read_without_one_is_listed():
  deck = read_deck(a_kind(desired_retention=None))
  normal(deck).desired_retention = RETENTION
  [message] = change_warnings(deck)
  assert message.splitlines()[1:] == ["  desired_retention: changed from unset to 0.85"]


# ------------------------------- What the import copies -------------------------------


@pytest.mark.parametrize(
  ("kind", "edit"),
  [
    pytest.param(
      None,
      lambda deck: setattr(normal(deck), "description", "Reworded"),
      id="description-reworded",
    ),
    pytest.param(
      a_kind(description=""),
      lambda deck: setattr(normal(deck), "description", "Added"),
      id="description-added",
    ),
    pytest.param(
      None,
      lambda deck: setattr(normal(deck), "config_id", PRESET_ID + 1),
      id="preset-moved",
    ),
    pytest.param(
      a_kind(config_id=1),
      lambda deck: setattr(normal(deck), "config_id", PRESET_ID),
      id="preset-moved-off-1",
    ),
    pytest.param(
      None,
      lambda deck: setattr(normal(deck), "review_limit", REVIEW_LIMIT + 1),
      id="limit-changed",
    ),
    pytest.param(
      a_kind(new_limit=None),
      lambda deck: setattr(normal(deck), "new_limit", NEW_LIMIT),
      id="limit-set",
    ),
  ],
)
def test_an_edit_the_import_copies_does_not_warn(
  kind: DeckNormalKind | None, edit: Callable[[Deck], None]
):
  deck = read_deck(kind)
  edit(deck)
  assert change_warnings(deck) == []


def test_reassigning_the_read_retention_does_not_warn():
  """The read value is the 32-bit one, so a plain comparison would see a change."""
  deck = read_deck()
  normal(deck).desired_retention = RETENTION
  assert change_warnings(deck) == []


def test_the_study_state_and_the_sync_fields_are_not_compared():
  """`common` and the extend counters are the user's own, which the import keeps."""
  deck = read_deck()
  deck.common.new_studied = 7
  normal(deck).extend_new = 3
  deck.modification_time_s = 1_600_000_000
  deck.update_sequence_n = 5
  assert change_warnings(deck) == []


# -------------------------------- What is not compared --------------------------------


def test_an_untouched_read_does_not_warn():
  assert change_warnings(read_deck()) == []


def test_a_deck_built_in_code_is_never_compared():
  deck = Deck(name=DECK_NAME, kind=a_kind())
  empty_description(deck)
  assert change_warnings(deck) == []


def test_a_renamed_deck_is_not_compared():
  """The import adds it as a new deck, so every value arrives."""
  deck = read_deck()
  deck.name = "Italian::Nouns"
  empty_description(deck)
  assert change_warnings(deck) == []


def test_a_rename_that_only_changes_case_is_still_compared():
  """The `name` column has the unicase collation, so the import still matches it."""
  deck = read_deck()
  deck.name = DECK_NAME.upper()
  empty_description(deck)
  assert len(change_warnings(deck)) == 1


def test_a_normal_deck_turned_filtered_is_not_compared():
  deck = read_deck()
  deck.kind = DeckFilteredKind()
  assert change_warnings(deck) == []


def test_a_filtered_deck_turned_normal_is_not_compared():
  """It no longer matches the user's filtered deck by kind, so it is added under a new
  name."""
  deck = read_deck(DeckFilteredKind())
  deck.kind = DeckNormalKind(config_id=1)
  assert change_warnings(deck) == []


# ------------------------------ What the write produces -------------------------------


def read_back(tmp_path: Path) -> Collection:
  """Write a collection holding the deck and read it back, as last release's file."""
  path = tmp_path / "collection.anki2"
  deck = Deck(name=DECK_NAME, kind=a_kind(config_id=1))
  with warnings.catch_warnings():
    warnings.simplefilter("ignore", Genanki2Warning)
    Collection(
      deck_configs=[DEFAULT_DECK_CONFIG],
      decks=[DEFAULT_DECK, deck],
      config=SCHEDULER_V2,
    ).write_sqlite(path)
    return Collection.read_sqlite(path)


@pytest.mark.parametrize(
  ("write", "expected"),
  [
    pytest.param(
      lambda col, d: Package(collection=col).write_apkg(d / "deck.apkg"),
      "An apkg import updates a deck",
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
      "If imported from an apkg, the import updates a deck",
      id="sqlite",
    ),
    pytest.param(
      lambda col, d: col.warn(),
      "If imported from an apkg, the import updates a deck",
      id="no context",
    ),
  ],
)
def test_a_dropped_edit_is_reported_for_what_the_write_produces(
  tmp_path, write, expected
):
  """A restore replaces the user's decks outright, so nothing is dropped."""
  collection = read_back(tmp_path)
  deck = collection.get_deck(name=DECK_NAME)
  assert deck is not None
  empty_description(deck)
  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always", Genanki2Warning)
    write(collection, tmp_path)

  messages = [str(w.message) for w in caught if str(w.message).startswith(MATCH)]
  if expected is None:
    assert messages == []
  else:
    assert len(messages) == 1
    assert expected in messages[0]


# --------------------------------- Against the wheel ----------------------------------


@pytest.fixture
def shipped(tmp_path) -> Path:
  """A package whose one deck follows its own preset and sets the values the import
  treats specially. The day limits are left out, because the import first rewrites
  their day against the target collection's (`maybe_correct_day_limits`, `decks.rs:90`),
  which is a separate behaviour."""
  preset = DeckConfig(id=PRESET_ID, name="Shipped preset")
  deck = Deck(
    name=DECK_NAME,
    config=preset,
    kind=a_kind(review_limit_today=None, new_limit_today=None),
  )
  collection = Collection(config=default_config_entries(), decks=[deck])
  note = Note(notetype=BASIC_NOTETYPE, fields=["parlare", "to speak"])
  collection.notes.append(note)
  collection.cards.append(Card(note=note, deck=deck, due=1))

  path = tmp_path / "shipped.apkg"
  Package(collection=collection).write_apkg(path)
  return path


@pytest.fixture
def target(tmp_path):
  """A collection that imported the shipped package with every option on."""
  col = AnkiCollection(str(tmp_path / "target.anki2"))
  yield col
  col.close()


def import_package(target: AnkiCollection, path: Path) -> None:
  target.import_anki_package(
    ImportAnkiPackageRequest(
      package_path=str(path),
      options=ImportAnkiPackageOptions(with_deck_configs=True, with_scheduling=True),
    )
  )


def write_edited(source: Path, path: Path, edit: Callable[[Deck], None]) -> list[str]:
  """Read the package back, edit its deck and write it to `path`, returning the deck
  warnings raised."""
  package = Package.read_apkg(source)
  deck = package.collection.get_deck(name=DECK_NAME)
  assert deck is not None
  edit(deck)
  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always", Genanki2Warning)
    package.write_apkg(path)
  return [str(w.message) for w in caught if str(w.message).startswith(MATCH)]


def deck_settings(target: AnkiCollection) -> dict:
  deck = target.decks.by_name(DECK_NAME)
  assert deck is not None
  return {
    key: deck[key]
    for key in ("desc", "conf", "reviewLimit", "newLimit", "desiredRetention")
  }


def test_the_dropped_edits_do_not_reach_a_user_who_has_the_deck(
  target, shipped, tmp_path
):
  """Every edit the warning lists, made at once. The deck keeps what the first import
  gave it."""
  import_package(target, shipped)
  before = deck_settings(target)

  def edit(deck: Deck) -> None:
    empty_description(deck)
    deck.config = None
    back_to_preset_1(deck)
    clear("review_limit")(deck)
    clear("new_limit")(deck)
    change_retention(deck)

  edited = tmp_path / "edited.apkg"
  [message] = write_edited(shipped, edited, edit)
  assert len(message.splitlines()) == 6

  import_package(target, edited)
  assert deck_settings(target) == before


def test_the_edits_the_import_copies_reach_a_user_who_has_the_deck(
  target, shipped, tmp_path
):
  """The other side of the line the warning draws: a reworded description and a changed
  limit arrive, so they are not reported."""
  import_package(target, shipped)

  def edit(deck: Deck) -> None:
    normal(deck).description = "Reworded"
    normal(deck).review_limit = REVIEW_LIMIT + 1

  edited = tmp_path / "edited.apkg"
  assert write_edited(shipped, edited, edit) == []

  import_package(target, edited)
  settings = deck_settings(target)
  assert settings["desc"] == "Reworded"
  assert settings["reviewLimit"] == REVIEW_LIMIT + 1
