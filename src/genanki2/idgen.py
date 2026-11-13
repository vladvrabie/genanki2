import hashlib
import random
import string
import unicodedata
from collections.abc import Iterator
from threading import Lock

from genanki2.errors import Genanki2Error
from genanki2.time import time_ms

__all__ = [
  "BASE91_ALPHABET",
  "THREAD_SAFE_ID_GENERATOR",
  "guid",
  "guid_for",
  "monotonic_id",
]

BASE91_ALPHABET: str = (
  string.ascii_letters
  + string.digits
  + "".join(c for c in string.punctuation if c not in "\"'\\")
)


def _base91(bits: int) -> str:
  out: str = ""
  while bits > 0:
    out = BASE91_ALPHABET[bits % 91] + out  # most significant first
    bits //= 91
  return out


def guid() -> str:
  """Generate a GUID using Anki's base91 format."""
  return _base91(random.getrandbits(64))


def guid_for(notetype_id: int, *key: str) -> str:
  """Derive a stable GUID from `notetype_id` and `key`.

  Useful when generating notes from id-less data (e.g. CSV) and a stable GUID is
  required across rebuilds to avoid duplicates. A guid derived from the note's content
  instead forks the note on every edit.

  Args:
    notetype_id: The notetype is part of the identity.
    key: Names the note across runs: a lemma, a source file, a row number, etc. Pass
      several key parts when no single one is unique. Parts are length-prefixed and
      normalized before hashing. No character is forbidden.

  Returns:
    A base91 string, the same format `guid()` produces, deterministic for one
    `notetype_id`/`key` combination.

  Raises:
    Genanki2Error: `key` is empty, which hands every note of a notetype the same guid.
  """
  if not key:
    raise Genanki2Error("Cannot generate a stable GUID without at least one key.")
  hasher = hashlib.blake2b(digest_size=8)
  hasher.update(notetype_id.to_bytes(8, "big"))
  for part in key:
    encoded: bytes = unicodedata.normalize("NFC", part).encode("utf-8")
    hasher.update(len(encoded).to_bytes(4, "big"))
    hasher.update(encoded)
  digest: bytes = hasher.digest()
  return _base91(int.from_bytes(digest, "big"))


def _monotonic_id() -> Iterator[int]:
  """Yield strictly increasing IDs based on the current time in milliseconds.

  Falls back to incrementing by 1 if the clock has not advanced since the last call.

  Note: This function is not thread safe.
  """
  current_id: int = -1
  while True:
    current_id = max(time_ms(), current_id + 1)
    yield current_id


class _MonotonicId:
  """A generator of strictly increasing IDs based on the current time in milliseconds.

  Falls back to incrementing by 1 if the clock has not advanced since the last call.
  Thread safe.
  """

  def __init__(self) -> None:
    self._last_id: int = -1
    self._lock: Lock = Lock()

  def next(self) -> int:
    with self._lock:
      self._last_id = max(time_ms(), self._last_id + 1)
      return self._last_id


THREAD_SAFE_ID_GENERATOR: bool = True
"""The implementation monotonic_id() uses: the thread safe one when `True`.

Default: `True`.

Note: Switching mid-run isn't guaranteed to preserve monotonicity. Set it once before
generating any ids.
"""

_safe_id_gen: _MonotonicId = _MonotonicId()
_unsafe_id_gen: Iterator[int] = _monotonic_id()


def monotonic_id() -> int:
  """Generate strictly increasing IDs based on the current time in milliseconds.

  Falls back to incrementing by 1 if the clock has not advanced since the last call
  (matching Anki's ID convention).

  Use `THREAD_SAFE_ID_GENERATOR` to select a thread safe/unsafe implementation (thread
  safe by default).
  """
  return _safe_id_gen.next() if THREAD_SAFE_ID_GENERATOR else next(_unsafe_id_gen)
