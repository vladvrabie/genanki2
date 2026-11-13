"""Does `free_normalized_media_name` keep both files when two names repair to one?

    uv run --with pytest pytest tests/test_media_name_collisions.py

A legacy media map keys its entries by position, so it can hold two names that
`normalize_media_name()` repairs to the same string. Anki writes both into its media
folder and the second overwrites the first, losing a file and leaving the note that
referenced it pointing at the wrong audio. genanki2 extracts into a directory it just
created, so it can place the second file beside the first instead and hand the caller
the rename to repair the reference with.

The suffix has to survive normalization, which is why every candidate goes back through
it: `normalize_media_name()` truncates to 120 bytes, and a suffix on a name already at
that limit is cut straight back off.
"""

import pytest

from genanki2.text import free_normalized_media_name, normalize_media_name

# 120 bytes, so a suffix pushes it over the limit and is truncated away again.
AT_THE_LIMIT = "a" * 116 + ".mp3"


def test_a_free_name_comes_back_normalized():
  """The repair happens here too, so a caller needs no second call to place a name."""
  assert free_normalized_media_name("a?b.mp3", set()) == "ab.mp3"


def test_a_taken_name_is_placed_beside_the_one_holding_it():
  assert free_normalized_media_name("a*b.mp3", {"ab.mp3"}) == "ab-1.mp3"


def test_the_suffix_goes_before_the_extension():
  """`.mp3` has to keep working as an extension, and only the last dot starts one."""
  assert free_normalized_media_name("a.b.mp3", {"a.b.mp3"}) == "a.b-1.mp3"
  assert free_normalized_media_name("noext", {"noext"}) == "noext-1"


def test_the_suffix_counts_up_past_names_already_placed():
  assert free_normalized_media_name("ab.mp3", {"ab.mp3", "ab-1.mp3"}) == "ab-2.mp3"


def test_three_names_repairing_to_one_all_keep_a_file():
  """The loop a reader runs: each placed name joins `taken` and the next moves aside."""
  taken: set[str] = set()
  placed: list[str] = []
  for name in ("ab.mp3", "a?b.mp3", "a*b.mp3"):
    free: str = free_normalized_media_name(name, taken)
    taken.add(free)
    placed.append(free)

  assert placed == ["ab.mp3", "ab-1.mp3", "ab-2.mp3"]


def test_a_name_that_normalizes_to_nothing_comes_back_empty():
  """Rejecting it needs the name it started as, which only the caller still holds."""
  assert normalize_media_name("\x07") == ""
  assert free_normalized_media_name("\x07", set()) == ""


def test_a_name_at_the_length_limit_is_placed_once():
  """The suffix is truncated away, so the candidate is a name of its own, not a series."""
  assert len(AT_THE_LIMIT.encode()) == 120

  placed = free_normalized_media_name(AT_THE_LIMIT, {AT_THE_LIMIT})

  assert placed != AT_THE_LIMIT
  assert placed == "a" * 115 + ".mp3"


def test_a_name_at_the_length_limit_with_nowhere_left_to_go_is_refused():
  """Both the name and its one candidate are taken, and truncation makes every further
  candidate the same string, so the loop ends rather than spinning."""
  taken = {AT_THE_LIMIT, "a" * 115 + ".mp3"}

  with pytest.raises(ValueError, match="No free name could be derived"):
    _ = free_normalized_media_name(AT_THE_LIMIT, taken)
