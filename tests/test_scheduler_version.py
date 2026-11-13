"""What does each write say about the `schedVer` and `sched2021` config entries?

    uv run --with anki --with pytest pytest tests/test_scheduler_version.py

Anki's backend reads `schedVer` as JSON into a version enum, and anything other than the
integer 2 fails to parse or parses as 1, so a missing entry, `3`, `"2"`, `2.0` and text
that is not JSON all mean the v1 scheduler (`rslib/src/config/mod.rs:213`).

A merge upgrades its temporary copy of the package to v2 before reading it, but only when
some card sits outside the new queue (`maybe_upgrade_scheduler`,
`apkg/import/mod.rs:161`). The upgrade raises by one every learning and relearning
rating of 2 or 3 (`storage/revlog/v2_upgrade.sql`), which is right for reviews recorded
under v1 and wrong for reviews recorded under v2. So an apkg write warns, and only when
the upgrade runs.

A restore upgrades nothing, and neither does the database check. On 1 or a missing entry
the collection opens on a scheduler that cannot study until the user accepts Anki's
update (`qt/aqt/deckbrowser.py:166`); on most other values the desktop refuses to open
the collection (`pylib/anki/collection.py:212`). So a colpkg write is refused.

On 2, the desktop studies with the v3 scheduler only when `sched2021` holds the JSON
`true` (`v3_scheduler`, `pylib/anki/collection.py:236`). A missing entry, or a value
that does not parse as a bool, reads as false (`get_config_bool`,
`rslib/src/config/bool.rs:58`), and the collection opens on the same scheduler that
cannot study. Accepting the update only sets the flag (`set_v3_scheduler`,
`pylib/anki/collection.py:239`), so a colpkg write warns rather than raises. An apkg
import does not carry config entries, so an apkg write says nothing about it.

`Collection` holds both, and the serialization context says which write it is. Without
one, or for a row or SQLite write, it warns and names what happens to each package.
"""

import re
import sqlite3
import warnings

import pytest
from anki.collection import Collection as AnkiCollection
from anki.collection import ImportAnkiPackageRequest
from anki.import_export_pb2 import ImportAnkiPackageOptions

from genanki2.builtins import BASIC_NOTETYPE, DEFAULT_DECK, default_config_entries
from genanki2.errors import Genanki2Error, Genanki2Warning
from genanki2.models.card import Card, CardQueue, CardType
from genanki2.models.collection import Collection
from genanki2.models.config import ConfigEntry
from genanki2.models.note import Note
from genanki2.models.package import CollectionPackage, Package
from genanki2.models.review import Review, ReviewRating, ReviewType
from genanki2.models.serialization_context import SerializationContext
from genanki2.settings import checks_disabled

MISSING = None
APKG = SerializationContext(output=SerializationContext.Output.APKG)
COLPKG = SerializationContext(output=SerializationContext.Output.COLPKG)

# Every value Anki reads as v1, each with the text the messages print for it.
NOT_V2 = [
  pytest.param(MISSING, "missing", id="missing"),
  pytest.param(b"1", "1", id="1"),
  pytest.param(b"3", "3", id="3"),
  pytest.param(b'"2"', "b'\"2\"'", id="string"),
  pytest.param(b"2.0", "b'2.0'", id="float"),
  pytest.param(b"x", "b'x'", id="not-json"),
]


# Every `sched2021` value Anki reads as false.
V3_OFF = [
  pytest.param(MISSING, id="missing"),
  pytest.param(b"false", id="false"),
  pytest.param(b"1", id="1"),
  pytest.param(b'"true"', id="string"),
]


def config(value: bytes | None) -> list[ConfigEntry]:
  return [] if value is None else [ConfigEntry(key="schedVer", value=value)]


def a_card(queue: CardQueue = CardQueue.NEW) -> Card:
  note = Note(notetype=BASIC_NOTETYPE, fields=["front", "back"])
  if queue == CardQueue.REVIEW:
    return Card(
      note=note,
      deck=DEFAULT_DECK,
      type=CardType.REVIEW,
      queue=queue,
      due=1,
      ivl=1,
      factor=2500,
    )
  return Card(note=note, deck=DEFAULT_DECK, queue=queue)


def a_collection(value: bytes | None, queue: CardQueue = CardQueue.NEW) -> Collection:
  card = a_card(queue)
  return Collection(
    config=config(value), notes=[card.note], cards=[card], decks=[DEFAULT_DECK]
  )


def a_v2_collection(sched2021: bytes | None) -> Collection:
  collection = a_collection(b"2")
  if sched2021 is not None:
    collection.config.append(ConfigEntry(key="sched2021", value=sched2021))
  return collection


def scheduler_warnings(action, entry: str = "schedVer") -> list[str]:
  """The messages `action` warns about the `entry` config entry, leaving out
  `ConfigEntry`'s own warning about a value that is not JSON."""
  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    action()
  return [str(w.message) for w in caught if f"`{entry}` config entry" in str(w.message)]


# ------------------------------------ Apkg writes -------------------------------------


@pytest.mark.parametrize(("value", "printed"), NOT_V2)
def test_an_apkg_on_v1_with_a_studied_card_warns(value, printed):
  collection = a_collection(value, CardQueue.REVIEW)
  with pytest.warns(Genanki2Warning, match=re.escape(f"entry is {printed}")):
    collection.warn_scheduler_version(APKG)


@pytest.mark.parametrize("queue", [CardQueue.SUSPENDED, CardQueue.USER_BURIED])
def test_a_new_card_out_of_the_new_queue_counts_as_studied(queue):
  """The importer asks for `queue != 0`, not for a card type."""
  collection = a_collection(MISSING, queue)
  with pytest.warns(Genanki2Warning, match="schedVer"):
    collection.warn_scheduler_version(APKG)


@pytest.mark.parametrize(("value", "printed"), NOT_V2)
def test_an_apkg_of_new_cards_stays_silent_on_v1(value, printed):
  """The importer does not upgrade a package whose cards are all in the new queue."""
  collection = a_collection(value)
  assert scheduler_warnings(lambda: collection.warn_scheduler_version(APKG)) == []


def test_an_apkg_on_v2_stays_silent():
  collection = a_collection(b"2", CardQueue.REVIEW)
  assert scheduler_warnings(lambda: collection.warn_scheduler_version(APKG)) == []


def test_write_apkg_warns_about_the_import_and_still_writes(tmp_path):
  path = tmp_path / "deck.apkg"
  package = Package(collection=a_collection(MISSING, CardQueue.REVIEW))
  (message,) = scheduler_warnings(lambda: package.write_apkg(path))
  assert "the import rewrites the revlog" in message
  assert "restored" not in message
  assert path.is_file()


@pytest.mark.parametrize(("value", "printed"), NOT_V2)
def test_an_apkg_not_on_v2_is_not_refused(value, printed):
  a_collection(value).check_scheduler_version(APKG)


@pytest.mark.parametrize("sched2021", V3_OFF)
def test_an_apkg_with_v3_off_stays_silent(sched2021):
  """The import does not carry config entries."""
  collection = a_v2_collection(sched2021)
  warned = scheduler_warnings(
    lambda: collection.warn_v3_scheduler_off(APKG), "sched2021"
  )
  assert warned == []


# ----------------------------------- Colpkg writes ------------------------------------


@pytest.mark.parametrize(("value", "printed"), NOT_V2)
def test_a_colpkg_not_on_v2_is_refused(value, printed):
  """New cards only: a restore upgrades nothing, studied or not."""
  collection = a_collection(value)
  with pytest.raises(Genanki2Error, match=re.escape(f"entry is {printed}")):
    collection.check_scheduler_version(COLPKG)


def test_a_colpkg_on_v2_passes():
  collection = a_collection(b"2", CardQueue.REVIEW)
  assert collection.check_scheduler_version(COLPKG) is collection


def test_collection_check_runs_the_scheduler_check():
  collection = a_collection(MISSING).prepare()
  with pytest.raises(Genanki2Error, match="schedVer"):
    collection.check(context=COLPKG)


def test_a_refused_colpkg_writes_nothing(tmp_path):
  path = tmp_path / "collection.colpkg"
  package = CollectionPackage(collection=a_collection(b"1"))
  with pytest.raises(Genanki2Error, match="schedVer"):
    package.write_colpkg(path)
  assert not path.exists()


@pytest.mark.parametrize("queue", [CardQueue.NEW, CardQueue.REVIEW])
def test_a_colpkg_on_v1_written_with_checks_disabled_warns_about_the_restore(
  tmp_path, queue
):
  """A restore upgrades nothing, so new cards do not spare it."""
  package = CollectionPackage(collection=a_collection(b"1", queue))
  path = tmp_path / "collection.colpkg"
  with checks_disabled():
    (message,) = scheduler_warnings(lambda: package.write_colpkg(path))
  assert "A restored collection needs 2" in message
  assert "import" not in message


@pytest.mark.parametrize("sched2021", V3_OFF)
def test_a_colpkg_with_v3_off_warns(sched2021):
  collection = a_v2_collection(sched2021)
  with pytest.warns(Genanki2Warning, match="After a restore"):
    collection.warn_v3_scheduler_off(COLPKG)


def test_a_colpkg_with_v3_on_stays_silent():
  collection = a_v2_collection(b"true")
  with warnings.catch_warnings():
    warnings.simplefilter("error", Genanki2Warning)
    collection.warn_v3_scheduler_off(COLPKG)


@pytest.mark.parametrize(("value", "printed"), NOT_V2)
def test_a_colpkg_not_on_v2_leaves_v3_to_the_check(value, printed):
  """The check refuses these already, so the warning would only repeat it."""
  collection = a_collection(value)
  with warnings.catch_warnings():
    warnings.simplefilter("error", Genanki2Warning)
    collection.warn_v3_scheduler_off(COLPKG)


def test_collection_warn_runs_the_v3_warning():
  collection = a_v2_collection(MISSING)
  with pytest.warns(Genanki2Warning, match="`sched2021` config entry"):
    collection.warn(context=COLPKG)


# ---------------------------- Without a context, or SQLite ----------------------------


@pytest.mark.parametrize(("value", "printed"), NOT_V2)
@pytest.mark.parametrize("queue", [CardQueue.NEW, CardQueue.REVIEW])
def test_a_collection_written_to_sqlite_warns_about_both_packages(
  tmp_path, value, printed, queue
):
  """The colpkg half applies whatever the cards, so new cards still warn, and the
  apkg half names the condition it depends on."""
  collection = a_collection(value, queue)
  path = tmp_path / "collection.anki2"
  (message,) = scheduler_warnings(lambda: collection.write_sqlite(path))
  assert f"entry is {printed}" in message
  assert "If any card is not new, an apkg import rewrites" in message
  assert "A restored colpkg" in message


@pytest.mark.parametrize(("value", "printed"), NOT_V2)
def test_a_collection_not_on_v2_is_not_refused_without_a_context(value, printed):
  a_collection(value).check_scheduler_version()


def test_v3_off_without_a_context_warns_about_a_restore():
  collection = a_v2_collection(MISSING)
  with pytest.warns(Genanki2Warning, match="If restored as a collection package"):
    collection.warn_v3_scheduler_off()


@pytest.mark.parametrize(
  ("write", "expected"),
  [
    pytest.param(
      lambda col, d: Package(collection=col).write_apkg(d / "deck.apkg"),
      None,
      id="apkg",
    ),
    pytest.param(
      lambda col, d: CollectionPackage(collection=col).write_colpkg(
        d / "collection.colpkg"
      ),
      "After a restore",
      id="colpkg",
    ),
    pytest.param(
      lambda col, d: col.write_sqlite(d / "collection.anki2"),
      "If restored as a collection package",
      id="sqlite",
    ),
    pytest.param(
      lambda col, d: col.warn(), "If restored as a collection package", id="no context"
    ),
  ],
)
def test_v3_off_is_reported_for_what_the_write_produces(tmp_path, write, expected):
  messages = scheduler_warnings(
    lambda: write(a_v2_collection(MISSING), tmp_path), "sched2021"
  )
  if expected is None:
    assert messages == []
  else:
    assert len(messages) == 1
    assert expected in messages[0]


# ------------------------------------- The oracle -------------------------------------


@pytest.mark.parametrize(
  ("schedver", "imported_ease"),
  [(b"2", ReviewRating.HARD), (None, ReviewRating.GOOD)],
  ids=["v2", "missing"],
)
def test_the_merge_raises_learning_ratings_on_v1(tmp_path, schedver, imported_ease):
  """What the apkg warning describes: a learning review rated Hard arrives as Good
  when the package reads as v1 and a card is out of the new queue."""
  entries = [entry for entry in default_config_entries() if entry.key != "schedVer"]
  collection = Collection(config=entries + config(schedver))
  card = a_card(CardQueue.REVIEW)
  collection.notes.append(card.note)
  collection.cards.append(card)
  collection.reviews.append(
    Review(card=card, ease=ReviewRating.HARD, type=ReviewType.LEARNING)
  )
  path = tmp_path / "deck.apkg"
  with warnings.catch_warnings():
    warnings.simplefilter("ignore", Genanki2Warning)
    Package(collection=collection).write_apkg(path)

  target_path = tmp_path / "target.anki2"
  target = AnkiCollection(str(target_path))
  target.import_anki_package(
    ImportAnkiPackageRequest(
      package_path=str(path),
      options=ImportAnkiPackageOptions(with_scheduling=True),
    )
  )
  target.close()
  with sqlite3.connect(target_path) as db:
    assert db.execute("SELECT ease FROM revlog").fetchall() == [(imported_ease,)]


@pytest.mark.parametrize(
  ("sched2021", "v3"),
  [
    *[pytest.param(p.values[0], False, id=p.id) for p in V3_OFF],
    pytest.param(b"true", True, id="true"),
  ],
)
def test_anki_runs_v3_only_on_a_true_sched2021(tmp_path, sched2021, v3):
  """What the colpkg warning describes: anything but `true` opens the
  collection without the v3 scheduler."""
  path = tmp_path / "collection.anki2"
  with warnings.catch_warnings():
    warnings.simplefilter("ignore", Genanki2Warning)
    a_v2_collection(sched2021).write_sqlite(path)

  collection = AnkiCollection(str(path))
  try:
    assert collection.v3_scheduler() is v3
  finally:
    collection.close()
