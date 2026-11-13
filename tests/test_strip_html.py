"""Does `strip_html_preserving_media_filenames` agree with the Rust one, character for character?

    uv run --with anki --with pytest pytest tests/test_strip_html.py

Anki recomputes `sfld` and `csum` from the fields it reads out of an imported package, so
a note whose stripping disagrees with Anki's gets a checksum Anki does not expect and
surfaces as a duplicate rather than as an error. That makes this a compatibility layer,
not a text utility, and the assertions are differential: `anki.utils.strip_html_media`
dispatches to exactly the function being reimplemented
(rslib/src/card_rendering/service.rs:232), so it can be compared against directly.

The literal cases below are the shapes that were actually got wrong on the way here, kept
so a regression names itself instead of appearing as a count in the corpus test.

anki is a test-only dependency, never a dependency of genanki2.
"""

import itertools
import random

import anki.lang
import pytest
from anki.utils import strip_html_media

from genanki2.text import strip_field_value_html_preserving_media_names

anki.lang.set_lang("en_US")  # the backend refuses to render without a locale


@pytest.mark.parametrize(
  ("markup", "stripped"),
  [
    # Anki's own assertions, transcribed from anki/rslib/src/text.rs:602-615.
    ("test", "test"),
    ("t<b>e</b>st", "test"),
    ("so<SCRIPT>t<b>e</b>st</script>me", "some"),
    ("<img src=foo.jpg>", " foo.jpg "),
    ("<img src='foo.jpg'><html>", " foo.jpg "),
    ("<html>", ""),
    # Shapes a hand-rolled `src=[\'"]?(.+)[\'"]?>` gets wrong: a greedy group runs past
    # its own closing quote, and requiring `<img` immediately before `src` rejects a tag
    # carrying any other attribute first.
    ('<img src="foo.jpg">', " foo.jpg "),
    ('<IMG SRC="foo.jpg">', " foo.jpg "),
    ('<img class="c" src="foo.jpg">', " foo.jpg "),
    ('<img src="a.jpg" alt="x">', " a.jpg "),
    ('<img src="a.jpg"><img src="b.jpg">', " a.jpg  b.jpg "),
    ("<img\nsrc='a.jpg'>", " a.jpg "),
    # Not a media tag, so the whole thing goes.
    ('<images src="foo.jpg">', ""),
    ('<div src="foo.jpg">', ""),
    # The padding is what keeps a filename from merging into its neighbours, and a
    # merged filename is a different csum.
    ("<b>a</b><img src=x.jpg>", "a x.jpg "),
    # SOUND_TAG runs in html_to_text_line (display), never in prepare_for_update, so a
    # TTS field carries the literal tag into both sfld and csum (notes/mod.rs:206-217).
    ("ciao[sound:a.mp3]", "ciao[sound:a.mp3]"),
  ],
)
def test_markup_is_stripped_and_filenames_survive(markup, stripped):
  assert strip_field_value_html_preserving_media_names(markup) == stripped


@pytest.mark.parametrize(
  ("markup", "stripped"),
  [
    ("caff<b>&egrave;</b>", "caffè"),
    ("a&nbsp;b", "a b"),  # U+00A0 is flattened to a plain space
    ("a b", "a b"),  # no `&` at all, so the text is returned untouched
    # Flattening U+00A0 is part of decoding, not a pass of its own, so a literal one
    # only turns into a space when there was something to decode in the first place.
    ("a\u00a0b", "a\u00a0b"),  # no `&`, so no decode, so no flattening
    ("a\u00a0b &amp; c", "a b & c"),  # a decode ran, so the literal one goes too
    ("a\u00a0b &foo;", "a\u00a0b &foo;"),  # abandoned decode, so it survives
    ("&#233;", "é"),
    ("&#xe9;", "é"),
    ("&amp;amp;", "&amp;"),
    # All-or-nothing: one bad entity and every good one in the field stays raw too.
    ("5 &lt; 6 & 7", "5 &lt; 6 & 7"),
    ("R&D &amp; more", "R&D &amp; more"),
    ("&amp; &foo;", "&amp; &foo;"),
    ("&amp", "&amp"),  # htmlescape wants the semicolon
    ("&AMP;", "&AMP;"),  # its table is HTML 4.01: no uppercase aliases
    ("&apos;", "&apos;"),  # and no HTML5-only names
    ("&#X13;", "&#X13;"),  # hex marker must be lowercase
    ("&#1114112;", "&#1114112;"),  # past the last plane
    ("&#xd800;", "&#xd800;"),  # a surrogate is not a Rust char
    # Nothing bad, so everything decodes.
    ("&#0;", "\x00"),
    ("Rock & Roll", "Rock & Roll"),
    ("a &amp; b", "a & b"),
  ],
)
def test_entities_decode_the_way_anki_decodes_them(markup, stripped):
  assert strip_field_value_html_preserving_media_names(markup) == stripped


# Chosen so combinations reach every htmlescape failure mode: a bare `&`, a missing
# semicolon, an unknown name, a malformed or out-of-range numeric, an uppercase marker.
PIECES = [
  "",
  "x",
  " ",
  "&",
  "\u00a0",
  "AT&T",
  "&amp;",
  "&amp",
  "&foo;",
  "&lt;",
  "&#233;",
  "&#xe9;",
  "&#X13;",
  "&#xZZ;",
  "&#;",
  "&nbsp;",
  "&;",
  "&#1114112;",
  "&#0;",
  "&AMP;",
  "&apos;",
  "&amp;amp;",
  "<b>",
  "</b>",
  "<img src=a.jpg>",
]


def test_agrees_with_anki_on_every_combination_of_fragments():
  """Three fragments deep, so a good entity and a bad one can meet in one string."""
  disagreements = [
    text
    for text in map("".join, itertools.product(PIECES, repeat=3))
    if strip_field_value_html_preserving_media_names(text) != strip_html_media(text)
  ]
  assert disagreements == []


def test_agrees_with_anki_on_random_noise():
  """The alphabet is entity punctuation plus hex digits in both cases, where it matters."""
  rng = random.Random(0)
  alphabet = "&#;xX01239aAbBeEfFmplt<>/ \"'"
  disagreements = [
    text
    for text in (
      "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
      for _ in range(20_000)
    )
    if strip_field_value_html_preserving_media_names(text) != strip_html_media(text)
  ]
  assert disagreements == []


def test_agrees_with_anki_on_every_named_entity():
  """Union of both tables, so an HTML5-only name has to be rejected by both or neither."""
  from html.entities import html5, name2codepoint

  names = {key.rstrip(";") for key in html5} | set(name2codepoint)
  disagreements = [
    name
    for name in sorted(names)
    if strip_field_value_html_preserving_media_names(f"&{name};")
    != strip_html_media(f"&{name};")
  ]
  assert disagreements == []


def test_agrees_with_anki_on_every_numeric_entity():
  codepoints = list(range(0x3000)) + [
    0xD7FF,
    0xD800,
    0xDFFF,
    0xE000,
    0xFFFD,
    0x10FFFF,
    0x110000,
    0x7FFFFFFF,
  ]
  forms = [
    form
    for code in codepoints
    for form in (f"&#{code};", f"&#x{code:x};", f"&#x{code:X};")
  ]
  disagreements = [
    form
    for form in forms
    if strip_field_value_html_preserving_media_names(form) != strip_html_media(form)
  ]
  assert disagreements == []


@pytest.mark.parametrize(
  "entity",
  [
    pytest.param("&#" + "9" * 5000 + ";", id="decimal"),
    pytest.param("&#x" + "f" * 5000 + ";", id="hexadecimal"),
    pytest.param("&#" + "0" * 5000 + "65;", id="leading-zeros"),
  ],
)
def test_agrees_with_anki_on_a_numeric_entity_of_thousands_of_digits(entity):
  """Python refuses to convert a decimal string over 4300 digits, and a package is
  untrusted input. Leading zeros do not count towards the length of a code point."""
  assert strip_field_value_html_preserving_media_names(entity) == strip_html_media(
    entity
  )
