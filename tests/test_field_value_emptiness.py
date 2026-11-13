"""Does `is_field_value_empty` call a value empty exactly when Anki does?

    uv run --with pytest pytest tests/test_field_value_emptiness.py

Anki decides whether a field is filled with `field_is_empty`
(rslib/src/template.rs:543-557), and a card is generated only if its front references a
filled field. A value genanki2 calls empty and Anki calls filled loses a card Anki would
generate, and the other way round adds one Anki would not.

Anki's Python API does not expose the function, so every value below was added to a real
collection behind a `{{B}}` front first, and the expectation is whether Anki generated
the card. Values Anki strips on the way in, such as `\\x0b` and `\\x1c`, are left out:
they read as empty only because they are gone, which is `normalize_field_value`'s job.

The models carry no anki dependency and neither does this file.
"""

import pytest

from genanki2.text import is_field_value_empty

EMPTY = {
  "nothing": "",
  "ascii whitespace": " \t\n",
  "br": "<br>",
  "uppercase br with a space": "<BR />",
  "self-closing br": "<br/>",
  "closing and opening div": "</div><div>",
  "div with a space": "<div >",
  "tags between whitespace": " <br>\n<div> </div> ",
}

FILLED = {
  "text": "a",
  "text after a br": "<br>a",
  "br with two spaces": "<br  >",
  "non-breaking space": "\xa0",
  "em space": " ",
  "nbsp entity": "&nbsp;",
  "empty paragraph": "<p></p>",
  "empty span": "<span></span>",
}


@pytest.mark.parametrize("value", EMPTY.values(), ids=list(EMPTY))
def test_whitespace_and_line_break_tags_are_empty(value: str):
  assert is_field_value_empty(value)


@pytest.mark.parametrize("value", FILLED.values(), ids=list(FILLED))
def test_any_other_content_fills_the_field(value: str):
  """Anki's whitespace class is POSIX `[[:space:]]`, ASCII only, so a Unicode space is
  content even though Python's `\\s` would match it."""
  assert not is_field_value_empty(value)
