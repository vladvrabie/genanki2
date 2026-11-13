"""Does a note report the same media filenames Anki would find in its fields?

    uv run --with anki --with pytest pytest tests/test_media_reference_extraction.py

A package carries a file because some note refers to it, and the reference lives in the
field text in one of two syntaxes. Finding fewer references than Anki leaves a file out
of the package, and the imported note then shows a missing image or plays nothing.
Finding more puts a file in the package that no card ever asks for.

The backend's `extract_media_files` returns the decoded filename of every reference in a
string, in order, which is the function being reimplemented, so the assertions can be
differential.

anki is a test-only dependency, never a dependency of genanki2.
"""

import itertools
import random
from collections.abc import Callable

import pytest
from anki.collection import Collection as AnkiCollection

from genanki2.builtins import BASIC_NOTETYPE
from genanki2.models.note import Note
from genanki2.text import field_value_media_refs

FIELDS = [
  # Both syntaxes, and the order the two patterns run in.
  ('a<img src="x.jpg">b[sound:y.mp3]', ["x.jpg", "y.mp3"]),
  ('[sound:y.mp3]<img src="x.jpg">', ["x.jpg", "y.mp3"]),
  # Every tag the HTML pattern knows, and one it does not.
  ("<img src=x.jpg>", ["x.jpg"]),
  ("<video src='v.mp4'>", ["v.mp4"]),
  ('<audio src="a.ogg">', ["a.ogg"]),
  ('<source src="s.ogg">', ["s.ogg"]),
  ('<object data="o.swf">', ["o.swf"]),
  ('<div src="c.jpg">', []),
  # A field is HTML, so an ampersand in a filename arrives escaped, in either syntax.
  ('<img src="a&amp;b.jpg">', ["a&b.jpg"]),
  ("[sound:s&amp;t.mp3]", ["s&t.mp3"]),
  ("[sound:&foo;.mp3]", ["&foo;.mp3"]),  # not an entity, so nothing decodes
  # The sound pattern is non-greedy and spans newlines.
  ("[sound:a.mp3] e [sound:b.mp3]", ["a.mp3", "b.mp3"]),
  ("[sound:a\nb.mp3]", ["a\nb.mp3"]),
  ("[sound:a]b]", ["a"]),
  ("[sound:]", []),  # a filename of at least one character or no reference at all
  # Two references to one file are two references.
  ("[sound:a.mp3][sound:a.mp3]", ["a.mp3", "a.mp3"]),
]

# Chosen so combinations can close one syntax with the other's punctuation, quote a
# filename with the wrong quote, and put an entity inside either.
PIECES = [
  "",
  "x",
  " ",
  "\n",
  "&amp;",
  "&foo;",
  "[sound:",
  "]",
  "a.mp3",
  '<img src="',
  "<img src='",
  "<img src=",
  '"',
  "'",
  ">",
  "b.jpg",
]


@pytest.fixture(scope="module")
def anki_extract(tmp_path_factory):
  """Anki's own answer for a string, from the backend that gathers a note's media."""
  collection = AnkiCollection(str(tmp_path_factory.mktemp("oracle") / "oracle.anki2"))
  yield lambda text: list(collection._backend.extract_media_files(text))
  collection.close()


@pytest.mark.parametrize(("field", "refs"), FIELDS)
def test_a_field_gives_up_its_filenames(
  field: str, refs: list[str], anki_extract: Callable[[str], list[str]]
):
  """The literal answer is pinned beside Anki's, so a regression names the shape."""
  assert field_value_media_refs(field) == refs
  assert anki_extract(field) == refs


def test_agrees_with_anki_on_every_combination_of_fragments(anki_extract):
  """Three fragments deep, so a reference can be opened, filled and closed."""
  disagreements = [
    text
    for text in map("".join, itertools.product(PIECES, repeat=3))
    if field_value_media_refs(text) != anki_extract(text)
  ]
  assert disagreements == []


def test_agrees_with_anki_on_random_noise(anki_extract):
  """The alphabet is the punctuation both syntaxes are built out of."""
  rng = random.Random(0)
  alphabet = "[]<>=\"' abimgqsound.3&;/"
  disagreements = [
    text
    for text in (
      "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 32)))
      for _ in range(5_000)
    )
    if field_value_media_refs(text) != anki_extract(text)
  ]
  assert disagreements == []


def test_a_note_reports_the_references_of_every_field_in_order():
  note = Note(
    notetype=BASIC_NOTETYPE,
    fields=['<img src="front.jpg">[sound:front.mp3]', "[sound:back.mp3]"],
  )
  assert note.media_references == ("front.jpg", "front.mp3", "back.mp3")


def test_a_note_with_no_media_reports_nothing():
  note = Note(notetype=BASIC_NOTETYPE, fields=["ciao", "hello"])
  assert note.media_references == ()


def test_a_note_reports_the_name_the_database_will_hold():
  """`flds` is normalized on the way to the row, so the search runs on that text.

  A reference read off the raw field could name a file that no reference in the written
  package asks for.
  """
  note = Note(
    notetype=BASIC_NOTETYPE,
    fields=["[sound:café.mp3]", "[sound:bell\x07.mp3]"],
  )
  assert note.media_references == ("café.mp3", "bell.mp3")
