"""Do the models reject a timestamp in the future when they are written?

    uv run --with pytest pytest tests/test_timestamps.py

The check runs at write time, not at construction. Anki stores a timestamp past now
without complaint, so a v18 collection can legitimately hold one, and refusing it on a
read would block reading a collection Anki is happy with. `to_row()` is the point where
genanki2 is the one producing the value, and that is where it refuses.

What a future timestamp costs is an object that stops taking updates. Sync compares the
two sides and keeps the newer: `existing.mtime < entry.mtime` for cards and notes
(`rslib/src/sync/collection/chunks.rs:184`, `:204`), `existing.mtime_secs <= incoming`
for notetypes, decks and deck configs (`sync/collection/changes.rs:249`, `:277`, `:294`).
An object stamped in 2099 therefore overrides the server's copy until 2099. The import
path agrees for notes and notetypes, whose default update condition is `IfNewer`
(`import_export/package/apkg/import/notes.rs:529`, `notes.rs:196-199`), and which keep
their incoming mtime because `add_notetype` writes the row without `set_modified`
(`notes.rs:204-211`).

`CollectionMetadata.crt` is a creation time rather than a modification time, and costs
something else: the day count it feeds clamps at zero rather than reporting a creation
date in the future (`scheduler/timing.rs:79`), and the importer shifts every relative due
date by the gap between the two collections' day counts (`cards.rs:69-70`,
`cards.rs:180-187`), so a review card arrives due at the wrong time.

That is why this raises where an empty first field only warns: the mistake is silent, and
it stays silent until the user notices their edits stopped arriving.

`Template` and `ConfigEntry` carry a modification time and are left alone. A template's
is written through and never read for an update decision, and the config block's is
replaced with now when a sync merges it (`changes.rs:239`). So are `CollectionMetadata`'s
own `mod`, `scm` and `ls`, which the importer never reads: it takes only the day count and
the creation UTC offset from the incoming collection row (`import_export/gather.rs:42-43`).

`Note` is the exception, and repairs rather than refusing. A field validator replaces a
future `mod` with now and warns, on every path in and not only on a read. Anki's database
check makes the same repair (`rslib/src/dbcheck.rs:291-293`) and makes it for notes and
nothing else, which is why the other five only check. The clamp leaves nothing for a
write check to catch, so `Note` has none, and the tests below split accordingly.
"""

import pydantic as pyd
import pytest

from genanki2.builtins import BASIC_NOTETYPE, DEFAULT_DECK
from genanki2.errors import Genanki2Warning
from genanki2.models.card import Card
from genanki2.models.collection_metadata import CollectionMetadata
from genanki2.models.deck import Deck
from genanki2.models.deck_config import DeckConfig
from genanki2.models.note import Note
from genanki2.models.notetype import Notetype
from genanki2.time import time_s

# Far enough ahead that no plausible gap between reading the clock here and reading it
# inside the check can close it, and near enough that a check with a day of slack, or one
# comparing against a bound fixed at import time, still fails.
AHEAD_S = 5

A_TIMESTAMP_IN_2020 = 1577880000


def a_note(timestamp: int) -> Note:
  return Note(notetype=BASIC_NOTETYPE, fields=["front", "back"], mod=timestamp)


def a_card(timestamp: int) -> Card:
  return Card(note=a_note(A_TIMESTAMP_IN_2020), deck=DEFAULT_DECK, mod=timestamp)


def a_notetype(timestamp: int) -> Notetype:
  return Notetype(name="Basic", config=b"", mtime_secs=timestamp)


def a_deck(timestamp: int) -> Deck:
  return Deck(name="Study", mtime_secs=timestamp)


def a_deck_config(timestamp: int) -> DeckConfig:
  return DeckConfig(name="Study", mtime_secs=timestamp)


def a_collection_metadata(timestamp: int) -> CollectionMetadata:
  return CollectionMetadata(crt=timestamp)


# Both the alias and the attribute differ per table, so each constructor is written out
# rather than driven from a field name, and every test reads the value back by attribute.
BUILDERS = [
  pytest.param(a_note, "modification_time_s", id="Note"),
  pytest.param(a_card, "modification_time_s", id="Card"),
  pytest.param(a_notetype, "modification_time_s", id="Notetype"),
  pytest.param(a_deck, "modification_time_s", id="Deck"),
  pytest.param(a_deck_config, "modification_time_s", id="DeckConfig"),
  pytest.param(a_collection_metadata, "creation_time_s", id="CollectionMetadata"),
]

# The five that refuse a future timestamp when they are written. `Note` clamps instead,
# and is covered on its own below.
REFUSING_BUILDERS = [param for param in BUILDERS if param.id != "Note"]


@pytest.mark.parametrize(("build", "attribute"), REFUSING_BUILDERS)
def test_a_timestamp_in_the_future_is_accepted_at_construction(build, attribute):
  """Reading is total for these five: a collection holding one is read back unchanged,
  value and all."""
  ahead = time_s() + AHEAD_S
  assert getattr(build(ahead), attribute) == ahead


@pytest.mark.parametrize(("build", "attribute"), REFUSING_BUILDERS)
def test_a_timestamp_in_the_future_is_rejected_on_write(build, attribute):
  with pytest.raises(ValueError, match="is in the future"):
    build(time_s() + AHEAD_S).to_row()


@pytest.mark.parametrize(("build", "attribute"), REFUSING_BUILDERS)
def test_a_timestamp_far_in_the_future_is_rejected_on_write(build, attribute):
  """Seconds and milliseconds confused, which is the ordinary way to land here. `crt` is
  the one field of its model counted in seconds, so it is the one this catches."""
  with pytest.raises(ValueError, match="is in the future"):
    build(time_s() * 1000).to_row()


@pytest.mark.parametrize(("build", "attribute"), REFUSING_BUILDERS)
def test_an_assignment_in_the_future_is_caught_on_write(build, attribute):
  """The check is not a field constraint, so the assignment goes through. `to_row()` is
  the single place the value is caught, whichever way it arrived."""
  built = build(A_TIMESTAMP_IN_2020)
  setattr(built, attribute, time_s() + AHEAD_S)
  with pytest.raises(ValueError, match="is in the future"):
    built.to_row()


@pytest.mark.parametrize(("build", "attribute"), BUILDERS)
def test_now_is_accepted(build, attribute):
  """Anki clamps `> now`, so the bound is inclusive: an object built this second is not in
  the future, and writing it a moment later still is not."""
  now = time_s()
  built = build(now)
  assert getattr(built, attribute) == now
  built.to_row()


@pytest.mark.parametrize(("build", "attribute"), BUILDERS)
@pytest.mark.parametrize("timestamp", [0, A_TIMESTAMP_IN_2020])
def test_the_past_is_accepted(build, attribute, timestamp):
  """0 is a legitimate value, and the one a row written by something else arrives with."""
  assert getattr(build(timestamp), attribute) == timestamp


@pytest.mark.parametrize(("build", "attribute"), BUILDERS)
def test_a_negative_timestamp_is_rejected(build, attribute):
  """Negativity is a field constraint rather than a check, so it is refused on the way in
  and cannot reach `to_row()` at all."""
  with pytest.raises(pyd.ValidationError):
    build(-1)


def test_the_default_is_not_in_the_future():
  """A default factory has to satisfy the check its model declares, or nothing writes at
  all. Written out, since the builders above all supply a timestamp."""
  before = time_s()
  note = Note(notetype=BASIC_NOTETYPE, fields=["front", "back"])
  card = Card(note=note, deck=DEFAULT_DECK)
  notetype = Notetype(name="Basic", config=b"")
  deck = Deck(name="Study")
  deck_config = DeckConfig(name="Study")
  metadata = CollectionMetadata()
  after = time_s()
  for built in (note, card, notetype, deck, deck_config):
    assert before <= built.modification_time_s <= after
  # `crt` rounds back to the day's 4AM rollover, so it is older than `before`.
  assert metadata.creation_time_s <= after


# ------------------------------- The note clamps instead -------------------------------


def test_a_future_note_timestamp_is_clamped_at_construction():
  """The repair is Anki's, and Anki makes it for notes alone, so `Note` is the one model
  that cannot hold a time that has not happened yet."""
  with pytest.warns(Genanki2Warning, match="modification time is in the future"):
    note = a_note(time_s() + AHEAD_S)
  assert note.modification_time_s <= time_s()


def test_a_future_note_timestamp_is_clamped_on_assignment():
  """`validate_assignment` re-runs the field validator, so the value cannot be set past
  the constructor either."""
  note = a_note(A_TIMESTAMP_IN_2020)
  with pytest.warns(Genanki2Warning, match="modification time is in the future"):
    note.modification_time_s = time_s() + AHEAD_S
  assert note.modification_time_s <= time_s()


def test_a_clamped_note_writes_without_complaint():
  """Nothing is left for a write check to catch, which is why `Note` no longer has one."""
  with pytest.warns(Genanki2Warning):
    note = a_note(time_s() + AHEAD_S)
  assert note.to_row()["mod"] <= time_s()
