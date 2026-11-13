"""Do `normalize_field` and `native_deck_name` agree with Anki?

    uv run --with anki --with pytest pytest tests/test_normalization.py

Both run before anything is written, and both fail silently. A field that keeps a
character Anki would have dropped gets a `csum` Anki does not expect and resurfaces as a
duplicate. A deck name normalized differently, or joined with `::` instead of `\\x1f`,
still draws the right tree but answers to no lookup, so making "the same" deck in the GUI
quietly creates a second one.

Neither has an oracle that can be called directly, so both are compared against what a
real collection stores: a note added and read back, a deck created and its raw `name`
column read. The two normalizers look alike and are not: fields spare `\\t` and `\\n`
(rslib/src/notes/mod.rs:350-352), deck components drop every ASCII control
(rslib/src/decks/name.rs:189-191), and only deck components trim and get a `blank`
fallback.

anki is a test-only dependency, never a dependency of genanki2.
"""

import random

import anki.lang
import pytest
from anki.collection import Collection

from genanki2.text import native_deck_name, normalize_field_value

anki.lang.set_lang("en_US")

# Transcribed from anki/rslib/src/decks/name.rs:230-241 (mod test, fn native_name).
ANKI_DECK_NAME_CASES = [
  ("foo::bar", "foo\x1fbar"),
  ("foo::::baz", "foo\x1fblank\x1fbaz"),
  ("fo\x1fo::ba\nr", "foo\x1fbar"),
  ("fo\x0ao\x1fbar", "foobar"),
  ("foo:::bar", "foo\x1fbar"),
  ("foo:::bar:baz: ", "foo\x1fbar:baz"),
]

# Shapes the transcribed cases leave open: a component that empties out only after being
# trimmed, and whitespace outside the ASCII set.
DECK_NAME_CASES = [
  "foo::   ::bar",
  "foo::\x01::bar",
  "foo::\xa0::bar",
  "foo::　::bar",
  "  foo  ::bar",
  "foo:: : : ::bar",
  "café::bar",
  "café::bar",  # decomposed, so NFC has something to do
  "foo\tbar::baz",  # a tab survives in a field and not in a deck name
  "::",
  "foo",
]

FIELD_CASES = [
  "plain",
  "a\tb",  # kept: invalid_char_for_field spares \t
  "a\nb",  # kept, likewise
  "a\x0bb",
  "a\x00b",
  "a\x1fb",  # would otherwise split the flds column
  "a\x7fb",
  "café",
  "café\x01",  # the control sits between base and combining mark
  "a\x01́b",
  "　 padded \xa0",  # fields are not trimmed
  "  ",
  "<b>markup</b>",
]


@pytest.fixture(scope="module")
def col(tmp_path_factory):
  path = tmp_path_factory.mktemp("normalization") / "oracle.anki2"
  collection = Collection(str(path))
  yield collection
  collection.close()


def anki_native_deck_name(col, human):
  """The `name` Anki stores for a deck asked for by its `::` name."""
  return col.db.scalar("select name from decks where id = ?", col.decks.id(human))


def anki_normalize_field(col, text):
  """The field Anki stores for a note carrying `text`.

  The text goes in the second field: an empty first field makes a note Anki refuses.
  """
  note = col.new_note(col.models.by_name("Basic"))
  note.fields = ["front", text]
  col.add_note(note, col.decks.id("Default"))
  return col.get_note(note.id).fields[1]


@pytest.mark.parametrize(("human", "native"), ANKI_DECK_NAME_CASES)
def test_native_deck_name_matches_anki_own_assertions(human, native):
  assert native_deck_name(human) == native


@pytest.mark.parametrize("human", [case for case, _ in ANKI_DECK_NAME_CASES])
def test_anki_own_assertions_still_describe_anki(col, human):
  """The transcription above is only worth having if upstream still behaves that way."""
  assert anki_native_deck_name(col, human) == native_deck_name(human)


@pytest.mark.parametrize("human", DECK_NAME_CASES)
def test_native_deck_name_matches_anki(col, human):
  assert native_deck_name(human) == anki_native_deck_name(col, human)


@pytest.mark.parametrize("text", FIELD_CASES)
def test_normalize_field_matches_anki(col, text):
  assert normalize_field_value(text) == anki_normalize_field(col, text)


def test_the_two_normalizers_disagree_about_tab_and_newline():
  """Sharing a name for two different rules is how they got conflated in the first place."""
  assert normalize_field_value("a\tb\nc") == "a\tb\nc"
  assert native_deck_name("a\tb\nc") == "abc"


NOISE_ALPHABET = "ab: \t\n\x00\x01\x0b\x1f\x7f\xa0　́  é"


def random_noise(count, max_len=8, seed=0):
  rng = random.Random(seed)
  return [
    "".join(rng.choice(NOISE_ALPHABET) for _ in range(rng.randint(0, max_len)))
    for _ in range(count)
  ]


def test_native_deck_name_agrees_with_anki_on_random_noise(col):
  """Deck names collide after normalization, and `decks.id` is get-or-create, so a
  repeated result is the existing deck rather than a second one with a `+` suffix."""
  disagreements = [
    text
    for text in random_noise(300)
    if native_deck_name(text) != anki_native_deck_name(col, text)
  ]
  assert disagreements == []


def test_normalize_field_agrees_with_anki_on_random_noise(col):
  disagreements = [
    text
    for text in random_noise(300, seed=1)
    if normalize_field_value(text) != anki_normalize_field(col, text)
  ]
  assert disagreements == []
