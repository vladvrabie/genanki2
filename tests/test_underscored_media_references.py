"""Does genanki2 find the files a notetype names outside its notes?

    uv run --with pytest pytest tests/test_underscored_media_references.py

A notetype reaches media no note mentions: a webfont its styling imports, an image one
of its templates draws. Anki collects those in `gather_media_names`, and collects only
names beginning with `_`, the convention that also exempts such a file from the unused
report. Missing one ships a deck whose font or logo is absent on every card built from
the notetype.

There is no oracle to call: both extractors are crate-private in Anki, so the cases
below are transcribed from `UNDERSCORED_CSS_IMPORTS` and `UNDERSCORED_REFERENCES`
(rslib/src/text.rs:170-208) and from the source's own `extracting` test
(rslib/src/text.rs:636-671), whose whole input and expected output are the first case of
each set.

Renaming one of those files is checked over the same cases. Anki never rewrites a
stylesheet or a template, so there is nothing to be differential against at all; what
pins the repair instead is the collector, which has to report the new name afterwards
and the old one nowhere.
"""

import pytest

from genanki2.builtins import (
  BASIC_NOTETYPE,
  builtin_notetype,
)
from genanki2.models.notetype import Notetype
from genanki2.text import (
  notetype_underscored_css_imports,
  replace_notetype_underscored_css_imports,
  replace_template_underscored_references,
  template_underscored_references,
)

ANKI_CSS_CASE = (
  "@IMPORT '_foo.css'\n"
  '@import "_bar.css"\n'
  "@import '_baz.css'\n"
  "@import 'nope.css'\n"
  "url(_foo.css)\n"
  'URL("_bar.css")\n'
  "@import url('_baz.css')\n"
  "url('nope.css')\n"
  "url(_foo.woff2) format('woff2')"
)

CSS_CASES = [
  (
    ANKI_CSS_CASE,
    (
      "_foo.css",
      "_bar.css",
      "_baz.css",
      "_foo.css",
      "_bar.css",
      "_baz.css",
      "_foo.woff2",
    ),
  ),
  ("@import '_a.css'", ("_a.css",)),
  ('@IMPORT "_a.css"', ("_a.css",)),  # keyword and function name are case insensitive
  ("URL(_a.woff2)", ("_a.woff2",)),
  ("url( _a.woff2 )", ("_a.woff2",)),  # padding inside the call is dropped
  ("@import 'plain.css'", ()),  # no underscore, so not a file the package carries
  ("url(plain.woff2)", ()),
  # The two quote characters are not read alike. A double quoted name is collected only
  # after an `@import`, where it has to end in the `.css` the branch spells out, or
  # inside a `url()`. A single quoted one is collected wherever it appears, since one of
  # the four branches carries neither prefix nor suffix.
  ('@import "_a.woff2"', ()),
  ("@import '_a.woff2'", ("_a.woff2",)),
  ('body { background: "_bg.png" }', ()),
  ("body { background: '_bg.png' }", ("_bg.png",)),
  ("src: url('_a.woff2') format('woff2')", ("_a.woff2",)),  # the format is not a name
  ("url(_a.woff2) url(_b.woff2)", ("_a.woff2", "_b.woff2")),
  ("url(_a.woff2) url(_a.woff2)", ("_a.woff2", "_a.woff2")),  # duplicates are kept
]

ANKI_REFERENCE_CASE = (
  '<img src="_foo.jpg">'
  '<object data="_bar">'
  '"_baz.js"'
  '"nope.js"'
  "<img src=_foo.jpg>"
  "<object data=_bar>"
  "'_baz.js'"
)

REFERENCE_CASES = [
  (
    ANKI_REFERENCE_CASE,
    ("_foo.jpg", "_bar", "_baz.js", "_foo.jpg", "_bar", "_baz.js"),
  ),
  ("[sound:_a.mp3]", ("_a.mp3",)),
  ("[sound:a.mp3]", ()),  # a note's own sound tag is not the notetype's file
  ('<img src="_a.jpg">', ("_a.jpg",)),
  ("<img src='_a.jpg'>", ("_a.jpg",)),
  ("<img src=_a.jpg>", ("_a.jpg",)),
  ('<img src="a.jpg">', ()),
  ('<audio data="_a.mp3">', ("_a.mp3",)),
  ('<img alt="_a.jpg">', ("_a.jpg",)),  # any quoted string, whatever attribute holds it
  # A field reference is not a name, whatever it is spelled with.
  ('{{Front}}<img src="_a.jpg">{{Back}}', ("_a.jpg",)),
  ("'_a.jpg' \"_b.jpg\"", ("_a.jpg", "_b.jpg")),
  ('<img src="_a.jpg"><img src="_a.jpg">', ("_a.jpg", "_a.jpg")),  # duplicates are kept
  # Entities are part of the name here. A note field is decoded before it is searched,
  # because Anki decodes one there and not in a template.
  ('"_caf&eacute;.mp3"', ("_caf&eacute;.mp3",)),
]


@pytest.mark.parametrize(("css", "expected"), CSS_CASES, ids=[c for c, _ in CSS_CASES])
def test_underscored_css_imports(css, expected):
  assert notetype_underscored_css_imports(css) == expected


@pytest.mark.parametrize(
  ("template", "expected"), REFERENCE_CASES, ids=[t for t, _ in REFERENCE_CASES]
)
def test_underscored_template_references(template, expected):
  assert template_underscored_references(template) == expected


def test_an_unquoted_attribute_value_runs_to_the_tag_end():
  """Anki's class is written `[^ >]`, but its regex crate drops whitespace inside a
  character class under the extended flag and Python's `re.VERBOSE` does not, so the
  space is not one of the excluded characters. Transcribing the class literally would
  cut the name at the first space and find a file that is not the one referenced. The
  same difference was measured against Anki's own extractor for the note-field pattern
  in `test_media_reference_extraction.py`, where an oracle exists."""
  assert template_underscored_references("<img src=_a.jpg width=10>") == (
    "_a.jpg width=10",
  )


# -------------------------------------- Renaming --------------------------------------


@pytest.mark.parametrize(("css", "expected"), CSS_CASES, ids=[c for c, _ in CSS_CASES])
def test_a_renamed_import_is_reported_under_its_new_name(
  css: str, expected: tuple[str, ...]
):
  """Every name the collector reports is renamed where it stands, and nothing else is.

  Run over the collector's own cases, so the two stay answerable against each other:
  whatever spelling is found is a spelling that can be repaired.
  """
  for old in dict.fromkeys(expected):
    renamed = replace_notetype_underscored_css_imports(css, old, "_z.css")
    assert notetype_underscored_css_imports(renamed) == tuple(
      "_z.css" if name == old else name for name in expected
    )


@pytest.mark.parametrize(
  ("template", "expected"), REFERENCE_CASES, ids=[t for t, _ in REFERENCE_CASES]
)
def test_a_renamed_reference_is_reported_under_its_new_name(
  template: str, expected: tuple[str, ...]
):
  for old in dict.fromkeys(expected):
    renamed = replace_template_underscored_references(template, old, "_z.jpg")
    assert template_underscored_references(renamed) == tuple(
      "_z.jpg" if name == old else name for name in expected
    )


@pytest.mark.parametrize(("css", "expected"), CSS_CASES, ids=[c for c, _ in CSS_CASES])
def test_renaming_an_import_twice_changes_nothing_the_second_time(
  css: str, expected: tuple[str, ...]
):
  for old in dict.fromkeys(expected):
    once = replace_notetype_underscored_css_imports(css, old, "_z.css")
    assert replace_notetype_underscored_css_imports(once, old, "_z.css") == once


@pytest.mark.parametrize(
  ("template", "expected"), REFERENCE_CASES, ids=[t for t, _ in REFERENCE_CASES]
)
def test_renaming_a_reference_twice_changes_nothing_the_second_time(
  template: str, expected: tuple[str, ...]
):
  for old in dict.fromkeys(expected):
    once = replace_template_underscored_references(template, old, "_z.jpg")
    assert replace_template_underscored_references(once, old, "_z.jpg") == once


def test_every_occurrence_of_an_imported_name_moves():
  assert (
    replace_notetype_underscored_css_imports(
      "url(_a.woff2) url(_a.woff2)", "_a.woff2", "_z.woff2"
    )
    == "url(_z.woff2) url(_z.woff2)"
  )


def test_every_occurrence_of_a_referenced_name_moves():
  assert (
    replace_template_underscored_references(
      '<img src="_a.jpg"><img src="_a.jpg">', "_a.jpg", "_z.jpg"
    )
    == '<img src="_z.jpg"><img src="_z.jpg">'
  )


def test_a_name_the_css_does_not_import_leaves_it_alone():
  css = "@import '_a.css'"
  assert replace_notetype_underscored_css_imports(css, "_b.css", "_z.css") == css


def test_a_name_the_template_does_not_reference_leaves_it_alone():
  template = '<img src="_a.jpg">'
  assert (
    replace_template_underscored_references(template, "_b.jpg", "_z.jpg") == template
  )


def test_a_name_without_an_underscore_cannot_be_renamed():
  """Neither pattern sees such a name, so neither repair reaches it.

  A caller renaming a file the notetype names without the convention gets nothing back
  and no error. The gap is Anki's: the same file is reported unused and deleted.
  """
  assert (
    replace_notetype_underscored_css_imports("url(a.woff2)", "a.woff2", "z.woff2")
    == "url(a.woff2)"
  )
  assert (
    replace_template_underscored_references('<img src="a.jpg">', "a.jpg", "z.jpg")
    == '<img src="a.jpg">'
  )


def test_an_entity_in_a_name_is_not_decoded_before_matching():
  """A template is searched raw, so the escaped spelling is the one that renames.

  It is the spelling the collector reports, which is what makes the pair usable: a
  caller passes back what it was given. A note field is the other way round, and
  `test_media_reference_replacement.py` covers it.
  """
  template = '"_caf&eacute;.mp3"'
  assert (
    replace_template_underscored_references(template, "_caf&eacute;.mp3", "_z.mp3")
    == '"_z.mp3"'
  )
  assert replace_template_underscored_references(template, "_café.mp3", "_z.mp3") == (
    template
  )


def test_a_name_the_pattern_over_matches_is_not_renamed_by_its_file_name():
  """The unquoted `url()` branch carries no `url(` prefix, so it runs from any
  underscore to the next `)`. Anki's pattern has the same grouping. It costs the repair
  nothing: a caller's `old` is a file name, and what the branch captures here is not
  one, so the comparison fails and the stylesheet is returned untouched."""
  css = '.x { content: "_a.png"; background: url(_a.png); }'
  assert notetype_underscored_css_imports(css) == ('_a.png"; background: url(_a.png',)
  assert replace_notetype_underscored_css_imports(css, "_a.png", "_z.png") == css


def test_a_name_running_to_the_tag_end_is_not_renamed_by_its_file_name():
  """Same for the template pattern's unquoted attribute, whose value has no terminator
  before `>`."""
  template = "<img src=_a.jpg width=10>"
  assert (
    replace_template_underscored_references(template, "_a.jpg", "_z.jpg") == template
  )
  assert (
    replace_template_underscored_references(template, "_a.jpg width=10", "_z.jpg")
    == "<img src=_z.jpg>"
  )


# ------------------------------------- A notetype -------------------------------------


def a_notetype() -> Notetype:
  return builtin_notetype(BASIC_NOTETYPE.name)


def test_a_notetype_renames_every_import_the_mapping_names():
  notetype = a_notetype()
  notetype.config.css += "@import '_a.css';url(_b.woff2)"
  notetype.replace_underscored_css_imports({"_a.css": "_z.css", "_b.woff2": "_y.woff2"})
  assert notetype.underscored_css_imports == ("_z.css", "_y.woff2")


def test_a_notetype_leaves_a_name_its_css_does_not_import_alone():
  """A caller repairs one media map for the whole package, so most keys name nothing here."""
  notetype = a_notetype()
  notetype.config.css += "@import '_a.css';"
  css = notetype.config.css
  notetype.replace_underscored_css_imports({"_b.css": "_z.css", "c.jpg": "z.jpg"})
  assert notetype.config.css == css


def test_renames_compose_in_mapping_order():
  """`_a.css` becomes `_b.css`, which the next entry then renames again."""
  notetype = a_notetype()
  notetype.config.css += "@import '_a.css';"
  notetype.replace_underscored_css_imports({"_a.css": "_b.css", "_b.css": "_c.css"})
  assert notetype.underscored_css_imports == ("_c.css",)


def test_a_notetype_does_not_rewrite_its_templates():
  """The two sites are repaired separately, and this one is the stylesheet."""
  notetype = a_notetype()
  notetype.config.css += "@import '_a.css';"
  notetype.templates[0].config.q_format += '<img src="_a.css">'
  q_format = notetype.templates[0].config.q_format
  assert notetype.templates[0].underscored_references == ("_a.css",)

  notetype.replace_underscored_css_imports({"_a.css": "_z.css"})
  assert notetype.underscored_css_imports == ("_z.css",)
  assert notetype.templates[0].config.q_format == q_format


# ------------------------------------- A template -------------------------------------


def test_a_template_renames_across_both_formats():
  template = a_notetype().templates[0]
  template.config.q_format += '<img src="_a.jpg">'
  template.config.a_format += "[sound:_b.mp3]"
  template.replace_underscored_references({"_a.jpg": "_z.jpg", "_b.mp3": "_y.mp3"})
  assert template.underscored_references == ("_z.jpg", "_y.mp3")


def test_a_template_leaves_a_name_neither_format_references_alone():
  template = a_notetype().templates[0]
  template.config.q_format += '<img src="_a.jpg">'
  formats = template.config.q_format, template.config.a_format
  template.replace_underscored_references({"_b.jpg": "_z.jpg", "c.jpg": "z.jpg"})
  assert (template.config.q_format, template.config.a_format) == formats


def test_a_templates_renames_compose_in_mapping_order():
  template = a_notetype().templates[0]
  template.config.q_format += '<img src="_a.jpg">'
  template.replace_underscored_references({"_a.jpg": "_b.jpg", "_b.jpg": "_c.jpg"})
  assert template.underscored_references == ("_c.jpg",)


def test_a_template_does_not_rewrite_the_notetype_css():
  """The two sites are repaired separately, and this one is the pair of formats."""
  notetype = a_notetype()
  notetype.config.css += "@import '_a.css';"
  notetype.templates[0].config.q_format += '<img src="_a.css">'
  css = notetype.config.css

  notetype.templates[0].replace_underscored_references({"_a.css": "_z.css"})
  assert notetype.templates[0].underscored_references == ("_z.css",)
  assert notetype.config.css == css
