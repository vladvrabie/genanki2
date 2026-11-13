"""Does genanki2 read a card template's field references the way Anki's parser does?

    uv run --with pytest pytest tests/test_template_references.py

A template naming a field the notetype does not have is `CardTypeError::NoSuchField`, but
the apkg importer builds notetypes with `skip_checks` set, so nothing catches it on the
way in: the deck imports, and the card is either blank or an error the first time it is
looked at. genanki2 therefore has to collect the references itself, which only works if
it agrees with Anki about what counts as one.

Agreement is the whole point, so the cases below are transcribed from the parser rather
than invented: `classify_handle` (rslib/src/template.rs:152-166) decides what a tag is,
`parse_inner` (rslib/src/template.rs:203-249) splits a replacement's filters off its key
and matches conditionals, and `find_field_references` (rslib/src/template.rs:880-903)
decides which of them are field references. The awkward cases are the ones nobody writes
on purpose and every real deck eventually contains: a tag under two characters, which is
a key whatever it starts with, and the alternate delimiters an old template can ask for.

There is no oracle to call: `ParsedTemplate` is not exposed through the Python API, so
this compares against the source rather than against a running collection.
"""

import pytest

from genanki2.text import (
  _iter_template_references,
  cloze_referenced_field_names,
  template_referenced_field_names,
)

# Every field name a template references, conditionals included and cloze conditionals
# excluded. Names are as written: no normalization, and the fields Anki fills in itself
# are not told apart from a notetype's own.
REFERENCE_CASES = [
  ("{{Front}}", {"Front"}),
  ("{{ Front }}", {"Front"}),  # trimmed
  ("{{FrontSide}}", {"FrontSide"}),  # a special name is still a reference
  ("{{text:Front}}", {"Front"}),  # the key is the last colon segment
  ("{{cloze:Text}}", {"Text"}),
  ("{{type:cloze:Text}}", {"Text"}),
  ("{{cloze-x:Text}}", {"Text"}),
  ("{{#Tags}}x{{/Tags}}", {"Tags"}),  # a conditional is a reference
  ("{{^Foo}}x{{/Foo}}", {"Foo"}),  # a negated one too
  ("{{# Tags}}x{{/ Tags}}", {"Tags"}),  # trimmed after the sigil
  ("{{#A}}{{B}}{{/A}}", {"A", "B"}),  # children are walked
  ("{{#a:b}}x{{/a:b}}", {"a:b"}),  # a conditional's key is never split
  ("{{#c1}}{{cloze:Text}}{{/c1}}", {"Text"}),  # c + digits selects a cloze number
  ("{{#c12}}x{{/c12}}", set()),
  ("{{#c1x}}x{{/c1x}}", {"c1x"}),  # all of it has to be digits
  ("{{#c}}x{{/c}}", {"c"}),
  ("<!-- {{Foo}} -->{{Bar}}", {"Bar"}),  # a comment references nothing
  ("<!-- {{Foo}}", {"Foo"}),  # an unterminated comment is not a comment
  ("{{{Foo}}}", {"Foo"}),  # leading braces are stripped
  ("{{}}", {""}),  # the empty name is legal, and add-ons use it
  ("{{:}}", {""}),  # one empty filter, then the empty key
  ("{{#}}", {"#"}),  # under two characters, so a key, not a conditional
  ("{{/}}", {"/"}),
  ("{{!Foo}}", {"!Foo"}),  # Anki's only comment syntax is the HTML one
  ("{{Foo{{Bar}}", {"Foo{{Bar"}),  # one tag, ending at the first `}}`
  ("{{Foo\nBar}}", {"Foo\nBar"}),  # a tag may span lines
  ("{{Front", set()),  # never closed, so ordinary text
  ("", set()),
]

# Filters, the conditional flag and the enclosing sections, which the sets above drop.
# A section is `(key, filled)`: `{{#key}}` requires `key` filled, `{{^key}}` empty.
REFERENCE_TUPLE_CASES = [
  ("{{Front}}", [("Front", (), False, ())]),
  ("{{type:cloze:Text}}", [("Text", ("type", "cloze"), False, ())]),
  ("{{:}}", [("", ("",), False, ())]),
  ("{{a:}}", [("", ("a",), False, ())]),
  ("{{::}}", [("", ("", ""), False, ())]),
  ("{{#Tags}}x{{/Tags}}", [("Tags", (), True, ())]),  # not inside its own section
  ("{{#A}}{{B}}{{/A}}", [("A", (), True, ()), ("B", (), False, (("A", True),))]),
  ("{{^A}}{{B}}{{/A}}", [("A", (), True, ()), ("B", (), False, (("A", False),))]),
  (
    "{{#A}}{{^B}}{{C}}{{/B}}{{/A}}",
    [
      ("A", (), True, ()),
      ("B", (), True, (("A", True),)),
      ("C", (), False, (("A", True), ("B", False))),  # outermost first
    ],
  ),
  ("{{#A}}{{/A}}{{B}}", [("A", (), True, ()), ("B", (), False, ())]),  # closed
  # A cloze conditional is not yielded, yet still encloses what it holds
  ("{{#c1}}{{cloze:Text}}{{/c1}}", [("Text", ("cloze",), False, (("c1", True),))]),
]

# A cloze notetype's first template needs a `cloze` filter on both of its sides. The
# filter is matched by exact name, and a conditional never counts however it is named.
CLOZE_CASES = [
  ("{{cloze:Text}}", {"Text"}),
  ("{{type:cloze:Text}}", {"Text"}),
  ("{{cloze:Text}}{{cloze:Extra}}", {"Text", "Extra"}),
  ("{{Text}}", set()),
  ("{{cloze-x:Text}}", set()),  # a filter is matched whole
  ("{{Cloze:Text}}", set()),  # and case-sensitively
  ("{{#c1}}{{Text}}{{/c1}}", set()),  # a cloze conditional is not a cloze filter
  ("{{#cloze}}x{{/cloze}}", set()),
]

# Unbalanced conditionals, the only way a template fails to parse. Anki reports the key
# it could not close against the innermost open one.
MALFORMED_CASES = [
  "{{/Foo}}",
  "{{#Foo}}",
  "{{^Foo}}",
  "{{#A}}{{/B}}",
  "{{#A}}{{#B}}{{/A}}",
]

# `{{=<% %>=}}` at the front switches the delimiters for the whole template
# (rslib/src/template.rs:136-146). Without it `<%` is text; with it `{{` is.
MUSTACHE_CASES = [
  ("{{=<% %>=}}<%Front%>", {"Front"}),
  ("{{=<% %>=}}{{Front}}", set()),
  ("   \n{{=<% %>=}}<%Front%>", {"Front"}),  # the directive is looked for after blanks
  ("{{=<% %>=}}{{=<% %>=}}<%Front%>", {"Front"}),  # every repetition is stripped
  ("{{=<% %>=}}<%#Tags%>x<%/Tags%>", {"Tags"}),
  ("{{=<% %>=}}<!-- <%Foo%> --><%Bar%>", {"Bar"}),
  ("<%Front%>", set()),  # no directive, so these are not delimiters
  ("x{{=<% %>=}}<%Front%>", {"=<% %>="}),  # nor is a directive that starts late
]


@pytest.mark.parametrize(
  ("template", "expected"), REFERENCE_CASES, ids=[t for t, _ in REFERENCE_CASES]
)
def test_referenced_field_names(template, expected):
  assert template_referenced_field_names(template) == expected


@pytest.mark.parametrize(
  ("template", "expected"),
  REFERENCE_TUPLE_CASES,
  ids=[t for t, _ in REFERENCE_TUPLE_CASES],
)
def test_filters_conditional_flag_and_sections(template, expected):
  assert list(_iter_template_references(template)) == expected


@pytest.mark.parametrize(
  ("template", "expected"), CLOZE_CASES, ids=[t for t, _ in CLOZE_CASES]
)
def test_cloze_referenced_field_names(template, expected):
  assert cloze_referenced_field_names(template) == expected


@pytest.mark.parametrize("template", MALFORMED_CASES)
def test_unbalanced_conditionals_raise(template):
  with pytest.raises(ValueError):
    template_referenced_field_names(template)


@pytest.mark.parametrize(
  ("template", "expected"), MUSTACHE_CASES, ids=[t for t, _ in MUSTACHE_CASES]
)
def test_mustache_delimiters(template, expected):
  assert template_referenced_field_names(template) == expected
