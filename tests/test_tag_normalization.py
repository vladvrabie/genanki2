"""Do `normalize_note_tag` and `normalize_note_tags` agree with Anki?

    uv run --with anki --with pytest pytest tests/test_tag_normalization.py

Anki canonifies a note's tags on every write (`canonify_tags_inner`,
rslib/src/tags/register.rs:41-71): each incoming string is split on the tag separators,
every `::` component is normalized, and the result is deduplicated case-insensitively and
sorted. Tags written as they were handed in therefore differ from the row Anki keeps for
the same note, which shows up the first time anything round-trips.

Two levels of separator are in play and only the outer one splits: `' '` and `'\\u3000'`
divide one tag from the next (rslib/src/tags/mod.rs:54-56), while `::` divides one tag
into the levels of the sidebar tree. Everything else, tab and newline included, is an
ordinary character inside a single tag.

genanki2 diverges on one point: a separator inside a tag raises, where Anki stores two
tags instead of the one that was asked for.

There is no oracle to call directly, so the comparison is against what a real collection
stores: a note added and its raw `tags` column read back. The collection is per test
because canonification reads collection state, `register_tag` rewriting an incoming tag's
case to match a tag or parent already registered, so a shared collection leaks case from
one case into the next.

anki is a test-only dependency, never a dependency of genanki2.
"""

import sys

import pytest
from anki.collection import Collection

from genanki2.text import normalize_note_tag, normalize_note_tags

# Mixed case, both sides of the `::` split, and an accent, so the sort and the
# case-insensitive dedup both have something to do.
CORPUS = ["zebra", "Apple", "banana", "APRICOT", "Ärger", "arger", "b", "A", "a"]

TAG_CASES = [
  CORPUS,
  ["Italian::Verbs", "italian::verbs", "Italian::Nouns"],
  ["a\x01b", "ab"],  # distinct on the way in, one tag afterwards
  ["a::\x01::b"],
  ["::"],
  ["\t"],
  ["a\tb"],
  ["\ta"],
  ["  a  "],
  ["   "],
  ["　"],
  ["a::\xa0::b"],  # neither a control nor a separator: only the trim clears it
  ["a:: ::b"],
  ["\xa0"],
]

# What Anki itself does with the characters around the edge of the separator set.
ANKI_TAG_CASES = [
  (["\t"], ["blank"]),  # a control-only tag survives as a tag
  (["a\tb"], ["ab"]),  # a control is removed, not split on
  (["a::\xa0::b"], ["a::blank::b"]),
  (["\xa0"], ["blank"]),
  (["a b"], ["a", "b"]),  # a space is a separator
  (["a　b"], ["a", "b"]),  # so is an ideographic space
  (["   "], []),  # separators only: no tag at all
]


@pytest.fixture
def col(tmp_path):
  collection = Collection(str(tmp_path / "oracle.anki2"))
  yield collection
  collection.close()


def anki_tags(col, tags):
  """The tags Anki stores for a note handed `tags`."""
  note = col.new_note(col.models.by_name("Basic"))
  note.fields[0] = "x"
  note.tags = list(tags)
  col.add_note(note, col.decks.id("Default"))
  stored = col.db.scalar("select tags from notes where id = ?", note.id)
  # Split on the separator Anki joins with rather than on Python whitespace, which is
  # wider: a stored tag can still hold a non-breaking space.
  return [tag for tag in stored.split(" ") if tag]


@pytest.mark.parametrize(("tags", "expected"), ANKI_TAG_CASES)
def test_the_separator_set_is_still_what_anki_uses(col, tags, expected):
  assert anki_tags(col, tags) == expected


@pytest.mark.parametrize("tags", TAG_CASES)
def test_normalize_note_tags_matches_anki(col, tags):
  assert normalize_note_tags(tags) == anki_tags(col, tags)


@pytest.mark.parametrize("tags", TAG_CASES)
def test_normalize_note_tags_is_idempotent(tags):
  """`to_row` normalizes again on the way out, over tags a validator already normalized."""
  once = normalize_note_tags(tags)
  assert normalize_note_tags(once) == once


def test_dedup_keeps_the_first_spelling_seen():
  """`canonify_tags_inner` collects into a `HashSet`, and an insert of an equal element
  leaves the one already there."""
  assert normalize_note_tags(["A", "a", "B", "b"]) == ["A", "B"]


def test_the_order_is_casefolded_not_codepoint():
  """`Vec<UniCase<String>>::sort_unstable` orders by the same comparison the `unicase`
  collation registers (rslib/src/storage/sqlite.rs:37-39)."""
  out = normalize_note_tags(CORPUS)
  assert out == sorted(out, key=str.casefold)
  assert out != sorted(out)  # a codepoint sort puts every capital first


@pytest.mark.parametrize("tag", ["a b", "a　b", "a::b c::d"])
def test_a_separator_inside_a_tag_raises_where_anki_splits(col, tag):
  assert len(anki_tags(col, [tag])) == 2
  with pytest.raises(ValueError):
    normalize_note_tags([tag])


def test_separators_around_a_tag_are_trimmed_rather_than_rejected(col):
  assert normalize_note_tags([" a "]) == ["a"] == anki_tags(col, [" a "])


def test_only_the_plural_drops_a_tag():
  """The substitution and the drop are different rules on different values: a component
  that empties out becomes `blank`, a tag that was never more than separators is gone."""
  assert normalize_note_tag("  ") == "blank"
  assert normalize_note_tag("　") == "blank"
  assert normalize_note_tags(["  ", "　"]) == []


# Rust's `char::is_whitespace` is the Unicode White_Space property.
RUST_WHITE_SPACE = {
  0x09,
  0x0A,
  0x0B,
  0x0C,
  0x0D,
  0x20,
  0x85,
  0xA0,
  0x1680,
  *range(0x2000, 0x200B),
  0x2028,
  0x2029,
  0x202F,
  0x205F,
  0x3000,
}

ASCII_CONTROL = {*range(0x20), 0x7F}


def test_str_strip_trims_what_rust_trim_trims():
  """`normalize_note_tag` spells Anki's `trim()` as `str.strip()`, which is only the same
  call because the ASCII controls are already gone by then: that range is the whole of the
  disagreement between Python's whitespace set and Unicode's White_Space."""
  python_white_space = {cp for cp in range(sys.maxunicode + 1) if chr(cp).isspace()}
  assert python_white_space - RUST_WHITE_SPACE <= ASCII_CONTROL
  assert RUST_WHITE_SPACE <= python_white_space
