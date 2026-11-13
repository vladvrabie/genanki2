"""Does a note name the same LaTeX images Anki renders from its fields?

    uv run --with anki --with pytest pytest tests/test_latex_image_names.py

A field holds LaTeX source between `[latex]`, `[$]` or `[$$]` tags, and Anki names the
image it renders from each span `latex-<sha1>.png` (or `.svg`), hashing the source after
wrapping it by its tag and stripping its HTML (`extract_latex` in `rslib/src/latex.rs`).
The file is ordinary media, so the name is the only thing tying it to a note. A name that
differs from Anki's by one byte of the hashed text is a file the trim drops and the
unused-media warning reports.

The backend's `extract_latex` returns the name of every span in a string, without
expanding clozes as the exporter does, so the assertions can be differential.

A cloze inside a span changes what a card shows, so a cloze card renders other names.
With `expand_clozes`, the backend first writes the field out hidden and revealed for each
cloze number (`expand_clozes_to_reveal_latex` in `rslib/src/cloze.rs`), as the media
check does, and `field_value_cloze_latex_image_names()` is compared against that.

anki is a test-only dependency, never a dependency of genanki2.
"""

import random
import re

import pytest
from anki.collection import Collection as AnkiCollection

from genanki2.builtins import BASIC_NOTETYPE, builtin_notetype
from genanki2.models.note import Note
from genanki2.text import (
  field_value_cloze_latex_image_names,
  field_value_latex_image_names,
)

# The three cases of the Rust test in `rslib/src/latex.rs`.
RUST_VECTORS = [
  (
    "a[latex]one<br>and<div>two[/latex]b",
    False,
    ["latex-ef30b3f4141c33a5bf7044b0d1961d3399c05d50.png"],
  ),
  (
    "[$]<b>hello</b>&nbsp; world[/$]",
    True,
    ["latex-060219fbf3ddb74306abddaf4504276ad793b029.svg"],
  ),
  (
    "[$$]math &amp; stuff[/$$]",
    False,
    ["latex-8899f3f849ffdef6e4e9f2f34a923a1f608ebc07.png"],
  ),
]

FIELDS = [
  "",
  "no latex here",
  # Tags are case-insensitive and a span can cross lines.
  "[LATEX]x[/LaTeX]",
  "[$]a\nb[/$]",
  # Each break Anki turns into a newline, and one it removes instead.
  "[latex]a<br>b[/latex]",
  "[latex]a<BR/>b[/latex]",
  "[latex]a<br />b[/latex]",
  "[latex]a<div>b</div>[/latex]",
  # Comments, styles and scripts go whole.
  "[latex]a<!-- c -->b<style>s</style><script>t</script>[/latex]",
  # One bad entity leaves every entity as written.
  "[latex]&amp;&bogus;[/latex]",
  # Each span closes at its nearest closing tag, and the order is kept.
  "[$]a[/$] [$$]b[/$$] [latex]c[/latex][/latex]",
  "[$]a[/$][$]a[/$]",
  # An unclosed tag is no span.
  "[$]a",
]

CLOZE_FIELDS = [
  # A cloze inside a span, hidden and revealed, with a hint, and with an empty one.
  "[$]{{c1::x}}[/$]",
  "[$]{{c1::x::h}}[/$] {{c2::y}}",
  "[$]{{c1::x::}}[/$]",
  # A span inside a cloze, or beside one, reads the same on every side.
  "{{c1::[$]x[/$]}}",
  "[$]a[/$] {{c1::b}}",
  # Nested clozes, and a hint after a nested cloze.
  "[$]{{c1::{{c2::x}}}}[/$]",
  "[$]{{c1::a{{c2::b}}c::h}}[/$]",
  # A marker that opens no cloze is text, closes nothing, and its `::` starts a hint.
  "[$]{{c1::a{{c,::b::h}}[/$]",
  "[$]{{c1::a{{c::b}}c}}[/$]",
  "[$]{{c2::{{c70000::}}[/$]",
  # A cloze never closed is dropped with its content, and a stray `}}` is text.
  "[$]{{c1::a}} {{c2::b[/$]",
  "[$]}}{{c1::a}}[/$]",
  # Nesting stops at 10 open clozes, and an 11th marker is dropped.
  "[$]" + "".join(f"{{{{c{n}::" for n in range(1, 13)) + "x" + "}}" * 12 + "[/$]",
  # The cloze markup can close a `<` the field leaves open, and keeps its breaks.
  "[$]a<{{c1::b}}>c[/$]",
  "[latex]{{c1::a<br>b}}[/latex]",
  # An image occlusion shape shows no text, and the clozes inside it are not revealed.
  "[$]{{c1::image-occlusion:rect:left=.1:top=.1:width=.2}}{{c2::y}}[/$]",
  "[$]{{c1::image-occlusion:rect {{c2::y}}}}[/$]",
]

# Chosen so combinations can open, nest and close clozes, open markers that are not
# one, give a hint, and open and close spans around and inside them.
CLOZE_PIECES = [
  "x",
  "}}",
  "::",
  "{{c1::",
  "{{c2::",
  "{{c0::",
  "{{c::",
  "{{c1,2::",
  "{{c70000::",
  "[$]",
  "[/$]",
  "<",
  ">",
  "<br>",
  "image-occlusion:",
]


@pytest.fixture(scope="module")
def anki_latex_names(tmp_path_factory):
  """Anki's own names for a string, from the backend that renders LaTeX."""
  collection = AnkiCollection(str(tmp_path_factory.mktemp("oracle") / "oracle.anki2"))

  def names(text: str, svg: bool, expand_clozes: bool = False) -> list[str]:
    extracted = collection._backend.extract_latex(
      text=text, svg=svg, expand_clozes=expand_clozes
    )
    return [latex.filename for latex in extracted.latex]

  yield names
  collection.close()


# ------------------------------------- The field --------------------------------------


@pytest.mark.parametrize(("field", "svg", "expected"), RUST_VECTORS)
def test_the_names_match_anki_s_own_test(field, svg, expected):
  assert field_value_latex_image_names(field, svg) == expected


@pytest.mark.parametrize("svg", [False, True])
@pytest.mark.parametrize("field", FIELDS)
def test_the_names_match_anki(anki_latex_names, field, svg):
  assert field_value_latex_image_names(field, svg) == anki_latex_names(field, svg)


def test_a_spaced_self_closing_break_is_removed_rather_than_a_newline():
  """Anki's pattern spells `<br( /)?>` in verbose mode, where the space is ignored."""
  assert field_value_latex_image_names(
    "[latex]a<br />b[/latex]", False
  ) == field_value_latex_image_names("[latex]ab[/latex]", False)
  assert field_value_latex_image_names(
    "[latex]a<br/>b[/latex]", False
  ) == field_value_latex_image_names("[latex]a\nb[/latex]", False)


def test_inline_math_is_the_standard_tag_wrapped_in_dollars():
  """The wrapping happens before the hash, so both spellings share one image."""
  assert field_value_latex_image_names(
    "[$]x^2[/$]", False
  ) == field_value_latex_image_names("[latex]$x^2$[/latex]", False)


def test_a_field_without_latex_names_nothing():
  assert field_value_latex_image_names('<img src="a.png">[sound:b.mp3]', False) == []


# --------------------------------- Clozes inside LaTeX --------------------------------


@pytest.mark.parametrize("svg", [False, True])
@pytest.mark.parametrize("field", CLOZE_FIELDS)
def test_the_cloze_names_match_anki(anki_latex_names, field, svg):
  """Anki's order follows a hash set of cloze numbers, so only the sorted lists agree."""
  assert sorted(field_value_cloze_latex_image_names(field, svg)) == sorted(
    anki_latex_names(field, svg, expand_clozes=True)
  )


def test_the_cloze_names_agree_with_anki_on_random_combinations(anki_latex_names):
  """The backend expands only a field holding `{{c`, and names a field without one as
  written, which is `field_value_latex_image_names()`, so those fields are left out."""
  rng = random.Random(0)
  fields = [
    "".join(rng.choices(CLOZE_PIECES, k=rng.randint(1, 8))) for _ in range(5_000)
  ]
  disagreements = [
    field
    for field in fields
    if "{{c" in field
    and sorted(field_value_cloze_latex_image_names(field, False))
    != sorted(anki_latex_names(field, False, expand_clozes=True))
  ]
  assert disagreements == []


def test_a_cloze_in_latex_names_the_hidden_then_the_revealed_image():
  assert field_value_cloze_latex_image_names("[$]{{c1::x}}[/$]", False) == [
    *field_value_latex_image_names("[$][...][/$]", False),
    *field_value_latex_image_names("[$]x[/$]", False),
  ]


@pytest.mark.parametrize(
  "field", ["[$]x[/$]", "{{c1::x}}", "[$]x[/$] {{c0::y}}", "[$]x[/$] {{cat}}"]
)
def test_a_field_without_latex_or_a_cloze_number_names_nothing(field):
  assert field_value_cloze_latex_image_names(field, False) == []


def rendered_latex_names(tmp_path, fields: list[str]) -> set[str]:
  """The images the cards of an Anki Cloze note link, LaTeX generation on or off."""
  collection = AnkiCollection(str(tmp_path / "rendered.anki2"))
  note = collection.new_note(collection.models.by_name("Cloze"))
  note.fields[:] = fields
  collection.add_note(note, 1)
  rendered = {
    name
    for card in note.cards()
    for html in (card.question(), card.answer())
    for name in re.findall(r'src="(latex-[0-9a-f]+\.png)"', html)
  }
  collection.close()
  return rendered


def test_the_cloze_names_are_the_ones_the_cards_render(tmp_path):
  """A card renders the LaTeX on its front and back after revealing its cloze."""
  text = "[$]{{c1::x::h}}[/$] [$$]{{c2::y}} {{c1::z}}[/$$]"
  rendered = rendered_latex_names(tmp_path, [text, ""])
  assert rendered == set(field_value_cloze_latex_image_names(text, False))


# -------------------------------------- The note --------------------------------------


def test_the_notetype_picks_the_extension():
  notetype = builtin_notetype(BASIC_NOTETYPE.name)
  note = Note(notetype=notetype, fields=["[$]<b>hello</b>&nbsp; world[/$]", "back"])
  name = "latex-060219fbf3ddb74306abddaf4504276ad793b029"
  assert note.latex_image_references == (f"{name}.png",)
  notetype.config.latex_svg = True
  assert note.latex_image_references == (f"{name}.svg",)


def test_the_names_follow_field_order():
  note = Note(notetype=BASIC_NOTETYPE, fields=["[$]a[/$]", "[$]b[/$][$]a[/$]"])
  a, b = (field_value_latex_image_names(f"[$]{x}[/$]", False) for x in "ab")
  assert note.latex_image_references == (*a, *b, *a)


def test_a_field_changed_in_place_is_normalized_before_it_is_hashed():
  """Anki hashes the field as stored, and a list assignment skips the validator that
  normalizes it on the way in."""
  note = Note(notetype=BASIC_NOTETYPE, fields=["front", "back"])
  note.fields[0] = "[$]é[/$]"  # decomposed, as NFC is not
  composed = Note(notetype=BASIC_NOTETYPE, fields=["[$]é[/$]", "back"])
  assert note.latex_image_references == composed.latex_image_references


def test_a_cloze_in_latex_names_the_written_then_the_hidden_and_revealed_images():
  note = Note(
    notetype=builtin_notetype("Cloze (genanki2)"), fields=["[$]{{c1::x}}[/$]", ""]
  )
  written, hidden, shown = (
    field_value_latex_image_names(f"[$]{x}[/$]", False)[0]
    for x in ("{{c1::x}}", "[...]", "x")
  )
  assert note.latex_image_references == (written, hidden, shown)


def test_a_note_names_the_images_of_a_cloze_field_and_of_a_plain_one(tmp_path):
  """The back shows Back Extra without `cloze:`, so its card renders that LaTeX as
  written, cloze markup included, while Text renders revealed."""
  fields = ["[$]{{c1::x}}[/$]", "[$]{{c1::y}}[/$]"]
  rendered = rendered_latex_names(tmp_path, fields)
  note = Note(notetype=builtin_notetype("Cloze (genanki2)"), fields=fields)
  assert field_value_latex_image_names(fields[1], False)[0] in rendered
  assert rendered <= set(note.latex_image_references)


def test_a_note_without_latex_names_nothing():
  note = Note(notetype=BASIC_NOTETYPE, fields=['<img src="a.png">', "back"])
  assert note.latex_image_references == ()
