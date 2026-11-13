"""Does a field value give the same cloze numbers Anki finds in it?

    uv run --with anki --with pytest pytest tests/test_cloze_numbers.py

A cloze note gets one card per distinct cloze number in its fields
(`new_cards_required_cloze`, `rslib/src/notetype/cardgen.rs:160-183`). Finding a number
Anki does not adds a card the imported note never had, and missing one loses a card.

Anki parses clozes rather than matching them (`rslib/src/cloze.rs:56-280`): a cloze
counts only once its `}}` arrives, a cloze closed inside one that never closes is lost
with it, a number has to fit in 16 bits, `c0` opens a cloze but numbers no card, and
nesting stops at 10 open clozes. The backend's `cloze_numbers_in_note` returns the
numbers in a note's fields, which is the function being reimplemented, so the assertions
can be differential.

anki is a test-only dependency, never a dependency of genanki2.
"""

import itertools
import random
from collections.abc import Callable

import pytest
from anki.collection import Collection as AnkiCollection

from genanki2.text import field_value_cloze_numbers

FIELDS = [
  # Plain clozes, and several numbers on one cloze.
  ("{{c1::a}} {{c2::b}}", {1, 2}),
  ("{{c1::a}} {{c1::b}}", {1}),
  ("{{c1,3::a}}", {1, 3}),
  ("{{c1,,3::a}}", {1, 3}),
  ("{{c1,::a}}", {1}),
  ("{{c::a}}", set()),
  ("{{c,::a}}", set()),
  ("{{c1::a::hint}}", {1}),
  # Numbers Anki does not turn into cards.
  ("{{c0::a}}", set()),
  ("{{c0::a {{c2::b}} }}", {2}),
  ("{{c70000::a}}", set()),
  ("{{c1::a {{c70000::b}} }}", {1}),
  ("{{c600::a}}", {600}),
  # Opening markers that are not one.
  ("{{c1:a}}", set()),
  ("{{C1::a}}", set()),
  ("{{c 1::a}}", set()),
  ("{{c-1::a}}", set()),
  # A marker without a number is text, even inside a cloze, so it closes nothing.
  ("{{c1::a {{c::b", set()),
  # Only ASCII digits number a cloze, though Python's `\d` and `int` take any digit.
  ("{{c１::a}}", set()),  # full-width
  ("{{c٣::a}}", set()),  # Arabic-Indic
  ("{{c1٣::a}}", set()),
  # A cloze counts once it closes, and takes the ones closed inside it along.
  ("{{c1::a", set()),
  ("{{c1::a {{c2::b}}", set()),
  ("{{c1::a {{c2::b}} }}", {1, 2}),
  ("{{c1::a}} {{c2::b", {1}),
  # Braces that close nothing are text.
  ("}} {{c1::a}}", {1}),
  ("{{c1::a}}}", {1}),
  ("{{{c1::a}}", {1}),
  # Nesting stops at 10, and a closing marker then closes an outer cloze.
  ("".join(f"{{{{c{n}::" for n in range(1, 13)) + "x" + "}}" * 12, set(range(1, 11))),
  ("".join(f"{{{{c{n}::" for n in range(1, 13)) + "x" + "}}" * 10, set(range(1, 11))),
]

# Chosen so combinations can open, nest and close clozes, open one that is not a
# cloze, leave braces that close nothing, and put a `}}` inside a MathJax formula.
# Released Anki up to 26.09.3 lets that `}}` close a cloze opened outside the formula.
# Anki's main branch no longer does (a5a0e4446, #4612), so `{{c1::\(}}\)` stops
# agreeing once the oracle is a release carrying that change.
PIECES = [
  "",
  "x",
  "{",
  "}",
  "}}",
  "::",
  "{{c1::",
  "{{c2::",
  "{{c0::",
  "{{c1,2::",
  "{{c::",
  "{{c,::",
  "{{c70000::",
  "\\(",
  "\\)",
]


@pytest.fixture(scope="module")
def anki_cloze_numbers(tmp_path_factory):
  """Anki's own answer for a string, from the backend that numbers a note's clozes."""
  collection = AnkiCollection(str(tmp_path_factory.mktemp("oracle") / "oracle.anki2"))
  note = collection.new_note(collection.models.by_name("Cloze"))

  def numbers(text: str) -> set[int]:
    note.fields[0] = text
    return set(collection._backend.cloze_numbers_in_note(note._to_backend_note()))

  yield numbers
  collection.close()


@pytest.mark.parametrize(("field", "numbers"), FIELDS)
def test_a_field_gives_up_its_cloze_numbers(
  field: str, numbers: set[int], anki_cloze_numbers: Callable[[str], set[int]]
):
  """The literal answer is pinned beside Anki's, so a regression names the shape."""
  assert field_value_cloze_numbers(field) == numbers
  assert anki_cloze_numbers(field) == numbers


def test_agrees_with_anki_on_every_combination_of_fragments(anki_cloze_numbers):
  """Four fragments deep, so a cloze can be opened inside another and both closed."""
  disagreements = [
    text
    for text in map("".join, itertools.product(PIECES, repeat=4))
    if field_value_cloze_numbers(text) != anki_cloze_numbers(text)
  ]
  assert disagreements == []


def test_agrees_with_anki_on_random_noise(anki_cloze_numbers):
  """The alphabet is the punctuation the cloze syntax is built out of."""
  rng = random.Random(0)
  alphabet = "{}:c,0129x "
  disagreements = [
    text
    for text in (
      "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
      for _ in range(5_000)
    )
    if field_value_cloze_numbers(text) != anki_cloze_numbers(text)
  ]
  assert disagreements == []
