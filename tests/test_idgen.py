"""Does the guid encoder agree with Anki, and is `guid_for` stable across runs?

    uv run --with pytest pytest tests/test_idgen.py

A guid cannot be checked by reading the collection back, so it is pinned against the
Rust source instead.

`BASE91_ALPHABET` is derived from the `string` module rather than written out, which is
readable but leaves the ordering implicit. Position is the digit value, so reordering
the table changes every guid ever emitted, and nothing downstream would notice: a guid
is an opaque string to Anki, and a collision only shows up as one note silently
overwriting another on import. The literal transcribed here is the pin.

`guid_for` has no oracle at all: it answers "is this the same note as the one already in
the collection", and the only wrong answer that is ever visible is a note that quietly
stops updating and starts duplicating. What holds it is that its output never changes,
so the vectors below are the specification rather than a sample of it.

`monotonic_id` answers a different question. An id has to be unique, has to be a
millisecond timestamp because Anki reads a note's creation date back out of it, and has
to keep rising when the clock does not, which are three requirements the same expression
serves: `max(time_ms(), last + 1)`. The tests below use their own `_MonotonicId`
instances rather than the module-level ones, since a generator that has handed out ids
stays ahead of the clock for as long as the process lives, and would carry that drift
into whichever test ran next.
"""

import itertools
import os
import subprocess
import sys
import threading

import pytest

from genanki2 import idgen
from genanki2.time import time_ms

# Transcribed from anki/rslib/src/notes/mod.rs:339-341 (fn anki_base91).
ANKI_BASE91_ALPHABET = (
  "abcdefghijklmnopqrstuvwxyz"
  "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
  "0123456789"
  "!#$%&()*+,-./:;<=>?@[]^_`{|}~"
)

# Independently encoded, and cross-checked against genanki's implementation
# (genanki/util.py:19-27), which is known to produce guids Anki accepts.
VECTORS = {
  # Anki's own four, transcribed from rslib/src/notes/mod.rs:695-701 (fn test_base91).
  # Zero encodes to nothing, because the loop is `while n > 0`. A draw of zero is a
  # 1-in-2**64 event, so this pins a shape that will never be seen rather than one that
  # needs handling.
  0: "",
  1: "b",
  1234567890: "saAKk",
  2**64 - 1: "Rj&Z5m[>Zp",
  # Boundaries of the first two digits, plus the top of the range.
  90: "~",
  91: "ba",
  92: "bb",
  2**63: "vYHz/gPN,i",
}


def decode(encoded: str) -> int:
  """Inverse of the encoding in `idgen.guid`, for round-trip checks."""
  value = 0
  for char in encoded:
    value = value * 91 + ANKI_BASE91_ALPHABET.index(char)
  return value


@pytest.fixture
def fixed_bits(monkeypatch):
  """Pin the randomness so `guid()` becomes a pure function of an integer."""

  def _fixed(value: int) -> list[int]:
    requested = []

    def getrandbits(k: int) -> int:
      requested.append(k)
      return value

    monkeypatch.setattr(idgen.random, "getrandbits", getrandbits)
    return requested

  return _fixed


def test_table_matches_anki():
  assert idgen.BASE91_ALPHABET == ANKI_BASE91_ALPHABET


def test_table_is_91_distinct_characters():
  assert len(idgen.BASE91_ALPHABET) == 91
  assert len(set(idgen.BASE91_ALPHABET)) == 91


@pytest.mark.parametrize("value,expected", sorted(VECTORS.items()))
def test_known_vectors(fixed_bits, value, expected):
  fixed_bits(value)
  assert idgen.guid() == expected


def test_draws_64_bits(fixed_bits):
  """Anki's base91_u64 encodes a u64 (rslib/src/notes/mod.rs:327-329)."""
  requested = fixed_bits(1)
  idgen.guid()
  assert requested == [64]


@pytest.mark.parametrize("value", [1, 2, 90, 91, 8191, 2**32, 2**63 + 12345, 2**64 - 1])
def test_round_trip(fixed_bits, value):
  fixed_bits(value)
  assert decode(idgen.guid()) == value


def test_guid_uses_only_table_characters_and_fits_in_ten():
  """91**10 > 2**64, so no u64 needs an eleventh digit."""
  for _ in range(200):
    encoded = idgen.guid()
    assert set(encoded) <= set(idgen.BASE91_ALPHABET)
    assert 1 <= len(encoded) <= 10


# ------------------------------- guid_for --------------------------------

# The recipe these vectors encode, in order: a `blake2b` hasher with `digest_size=8`; the
# notetype id as 8 big-endian bytes; then, per key part, the NFC form encoded as UTF-8,
# preceded by its byte length as 4 big-endian bytes; the digest read big-endian as a u64;
# that u64 through the same base91 table as `guid`.
GUID_FOR_VECTORS = [
  (1, ("ciao",), "H**O6(qp(1"),
  (2, ("ciao",), "y>:y}aPZD5"),
  (1, ("Ciao",), "B#w8ZNmem"),
  (1, ("a", "bc"), "o7|((}~|jm"),
  (1, ("ab", "c"), "v,E;0LH&;]"),
  (1, ("caff\u00e8",), "KuZr/rv)Vw"),
  (5, ("italian.csv", "42"), "k>Vq0I@r^I"),
]

# A guid computed in this process proves nothing about the next one: `hash()` is seeded
# per process, so a scheme built on it agrees with itself all day and forks the deck
# tomorrow. The only way to see that is to ask a second interpreter.
CHILD_PROGRAM = "from genanki2 import idgen; print(idgen.guid_for(1, 'ciao'))"


def guid_for_in_a_fresh_process(hash_seed: str) -> str:
  environment = {**os.environ, "PYTHONHASHSEED": hash_seed}
  result = subprocess.run(
    [sys.executable, "-c", CHILD_PROGRAM],
    capture_output=True,
    text=True,
    check=True,
    env=environment,
  )
  return result.stdout.strip()


@pytest.mark.parametrize(("notetype_id", "key", "expected"), GUID_FOR_VECTORS)
def test_guid_for_known_vectors(notetype_id, key, expected):
  """These values are frozen. A deck built by one version of genanki2 and rebuilt by the
  next has to produce the same guid per note, or every note in it forks on import, so
  changing the hash, the widths, the byte order or the order of what is fed in is not a
  refactor."""
  assert idgen.guid_for(notetype_id, *key) == expected


def test_the_notetype_is_part_of_the_identity():
  """Two notes with the same content under different notetypes are two notes, and the
  importer matches on guid alone (rslib/src/import_export/package/apkg/import/notes.rs:
  338-348), so a guid that ignores the notetype makes the second one vanish."""
  assert idgen.guid_for(1, "ciao") != idgen.guid_for(2, "ciao")


def test_key_parts_cannot_run_together():
  """Length-prefixing is what makes a multi-part key unambiguous. Concatenation would
  read `("a", "bc")` and `("ab", "c")` as one key, and a `\\x1f` separator would read
  `("a\\x1fb",)` as two parts."""
  assert idgen.guid_for(1, "a", "bc") != idgen.guid_for(1, "ab", "c")


def test_one_key_in_two_unicode_forms_is_one_key():
  """A CSV exported on macOS carries decomposed text. Without the NFC step the same row
  from two sources is two notes."""
  precomposed, decomposed = "caff\u00e8", "caffe\u0300"
  assert precomposed != decomposed
  assert idgen.guid_for(1, precomposed) == idgen.guid_for(1, decomposed)


def test_a_key_with_no_parts_raises():
  """A guid derived from the notetype alone is the same guid for every note that uses
  that notetype, so a whole CSV would import as a single note."""
  with pytest.raises(ValueError):
    idgen.guid_for(1)


def test_the_same_key_gives_the_same_guid_in_a_fresh_process():
  first = guid_for_in_a_fresh_process("0")
  second = guid_for_in_a_fresh_process("1")
  assert first == second == idgen.guid_for(1, "ciao")


def test_guid_for_fits_the_column_like_a_random_guid():
  """`Note.guid` is `min_length=1, max_length=10`, and a hash has to satisfy it for every
  key, not on average."""
  for i in range(200):
    encoded = idgen.guid_for(1, f"lemma-{i}")
    assert set(encoded) <= set(idgen.BASE91_ALPHABET)
    assert 1 <= len(encoded) <= 10


# ----------------------------------- Monotonic ids -----------------------------------

A_FIXED_MS = 1_750_000_000_000
A_DAY_IN_MS = 86_400_000
A_BURST = 100_000


def test_a_fresh_generator_hands_out_the_current_time():
  """An id is a creation timestamp, and Anki dates a note by it, so a counter that only
  rises would be unique and would still date every note wrong."""
  before = time_ms()
  new_id = idgen._MonotonicId().next()
  assert before <= new_id <= time_ms()


def test_ids_minted_within_one_millisecond_step_by_one(monkeypatch):
  """The clock is the source of an id, but two notes built in the same millisecond still
  need two ids."""
  monkeypatch.setattr(idgen, "time_ms", lambda: A_FIXED_MS)
  generator = idgen._MonotonicId()
  assert [generator.next() for _ in range(3)] == [
    A_FIXED_MS,
    A_FIXED_MS + 1,
    A_FIXED_MS + 2,
  ]


def test_an_id_does_not_follow_the_clock_backwards(monkeypatch):
  """An id is a primary key on four tables, so a clock stepped back by NTP would
  otherwise hand out one that is already in use."""
  clock = [A_FIXED_MS]
  monkeypatch.setattr(idgen, "time_ms", lambda: clock[0])
  generator = idgen._MonotonicId()
  first = generator.next()
  clock[0] -= 10_000
  assert generator.next() == first + 1


def test_a_burst_is_strictly_increasing_and_unique():
  generator = idgen._MonotonicId()
  ids = [generator.next() for _ in range(A_BURST)]
  assert len(set(ids)) == A_BURST
  assert all(earlier < later for earlier, later in itertools.pairwise(ids))


def test_a_burst_does_not_reach_a_day_into_the_future():
  """Anki's database check reassigns any note, card or revlog id more than a day ahead
  of now (rslib/src/dbcheck.rs:442-451, storage/dbcheck/invalid_ids_count.sql).

  The `+ 1` fallback is what walks ids forward: minted faster than the clock ticks, a
  burst ends roughly one millisecond ahead per id, and the generator keeps that lead for
  the rest of the process. A day of it takes 86.4 million ids, so this pins the order of
  magnitude rather than a boundary anything reaches.
  """
  generator = idgen._MonotonicId()
  ids = [generator.next() for _ in range(A_BURST)]
  assert ids[-1] < time_ms() + A_DAY_IN_MS


@pytest.mark.parametrize("thread_safe", [True, False])
def test_the_switch_selects_a_working_generator(monkeypatch, thread_safe):
  """The two implementations share no state, one being a module-level `Iterator` and the
  other an object holding a lock, so neither exercises the other."""
  monkeypatch.setattr(idgen, "THREAD_SAFE_ID_GENERATOR", thread_safe)
  ids = [idgen.monotonic_id() for _ in range(1_000)]
  assert all(earlier < later for earlier, later in itertools.pairwise(ids))


def test_concurrent_callers_never_share_an_id():
  """`self._last_id = max(time_ms(), self._last_id + 1)` reads and writes over several
  bytecodes, so without the lock two threads can read one value and hand out one id
  twice. Each thread collects its own list, since appending the ids to a shared one
  would be testing the list rather than the generator."""
  generator = idgen._MonotonicId()
  batches: list[list[int]] = []

  def collect() -> None:
    batches.append([generator.next() for _ in range(2_000)])

  threads = [threading.Thread(target=collect) for _ in range(4)]
  for thread in threads:
    thread.start()
  for thread in threads:
    thread.join()

  ids = [new_id for batch in batches for new_id in batch]
  assert len(ids) == 8_000
  assert len(set(ids)) == len(ids)
