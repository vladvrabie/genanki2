import dataclasses as dc
import hashlib
import html
import re
import unicodedata
from collections.abc import Collection, Iterable, Iterator, Mapping
from collections.abc import Set as AbstractSet
from functools import lru_cache
from html.entities import name2codepoint
from pathlib import PureWindowsPath
from typing import NamedTuple

from genanki2._unicode_ages import RE_ASSIGNED_AFTER_ANKIS_UNICODE
from genanki2.errors import Genanki2Error
from genanki2.utils import is_uint16

__all__ = [
  "DECK_HUMAN_SEPARATOR",
  "DECK_NATIVE_SEPARATOR",
  "FIELD_VALUE_SEPARATOR",
  "MUSTACHE_DELIMITERS_DIRECTIVE",
  "RE_ASCII_CONTROL_CHARS",
  "RE_ASCII_CONTROL_CHARS_IN_FIELDS",
  "RE_ASSIGNED_AFTER_ANKIS_UNICODE",
  "RE_CLOZE_CONDITIONAL",
  "RE_EMPTY_FIELD_VALUE",
  "RE_FIELD_NAME_BAD_CHARS",
  "RE_FIELD_NAME_BAD_START_CHARS",
  "RE_FIELD_VALUE_AUDIO_TAG",
  "RE_FIELD_VALUE_CLOZE",
  "RE_HTML",
  "RE_HTML_ENTITY",
  "RE_HTML_MEDIA_TAGS",
  "RE_LATEX",
  "RE_LATEX_NEWLINES",
  "RE_MEDIA_NAME_BAD_CHARS",
  "RE_NOTETYPE_UNDERSCORED_CSS_IMPORTS",
  "RE_REMOTE_MEDIA_REF",
  "RE_STRIP_WHITESPACE_AND_COLON",
  "RE_TAGS_SEPARATORS",
  "RE_TEMPLATE_REFERENCES_ANKI_DELIMITERS",
  "RE_TEMPLATE_REFERENCES_MUSTACHE_DELIMITERS",
  "RE_TEMPLATE_UNDERSCORED_REFERENCES",
  "RE_WINDOWS_DEVICE_NAMES",
  "TAGS_SEPARATORS",
  "TAG_PARTS_SEPARATOR",
  "SectionReference",
  "TemplateReference",
  "cloze_referenced_field_names",
  "decode_html_entities",
  "field_value_cloze_latex_image_names",
  "field_value_cloze_numbers",
  "field_value_latex_image_names",
  "field_value_media_refs",
  "free_normalized_media_name",
  "html_entity_codepoint",
  "human_deck_name",
  "is_field_value_empty",
  "is_media_name_safe",
  "is_template_reference_reachable",
  "is_unicode_scalar",
  "native_deck_name",
  "normalize_deck_name",
  "normalize_deck_name_parts",
  "normalize_field_name",
  "normalize_field_value",
  "normalize_media_name",
  "normalize_note_fields",
  "normalize_note_tag",
  "normalize_note_tags",
  "normalize_notetype_name",
  "normalize_template_name",
  "notetype_underscored_css_imports",
  "replace_ascii_control_chars",
  "replace_field_value_media_refs",
  "replace_notetype_underscored_css_imports",
  "replace_template_underscored_references",
  "split_note_tags",
  "strip_field_value_html_preserving_media_names",
  "tag_spelling_under_registered_ancestor",
  "template_referenced_field_names",
  "template_references",
  "template_underscored_references",
]

# ---------------------------------------- HTML ----------------------------------------

# Transcribed from Anki’s own pattern. If this implementation is out-of-sync
# with Anki's, then the computed checksum on a note will differ, showing up as a
# duplicate.
RE_HTML: re.Pattern[str] = re.compile(
  r"(?si)<!--.*?-->|<style.*?>.*?</style>|<script.*?>.*?</script>|<.*?>"
)
# Anki's writes `[^ >]` on the unquoted branch, but its regex engine discards whitespace
# inside a character class in (?x) mode and Python does not, so the space is not part
# of it. Python therefore uses `[^>]`.
# `\x20` is a literal space.
RE_HTML_MEDIA_TAGS: re.Pattern[str] = re.compile(
  r"""(?xsi)
    # the start of the image, audio, object, or source tag
    <\b(?:img|audio|video|object|source)\b

    # any non-`>`, except inside `"` or `'`
    (?: [^>] | "[^"]+?" | '[^']+?' )+?

    # capture `src` or `data` attribute
    \b(?:src|data)\b=
    (?:
        # 1: double-quoted filename
        " ([^"]+?) " [^>]*>
      |
        # 2: single-quoted filename
        ' ([^']+?) ' [^>]*>
      |
        # 3: unquoted filename, then either a space and the rest or an immediate end
        ([^>]+?) (?: \x20[^>]*> | > )
    )
  """
)
RE_HTML_ENTITY: re.Pattern[str] = re.compile(
  r"&(?:#[0-9]+|#x[0-9a-fA-F]+|[0-9a-zA-Z]+);"
)


def is_unicode_scalar(codepoint: int) -> bool:
  """Decide if `codepoint` stands for a character.

  Unicode numbers characters from 0 to 0x10FFFF, but two ranges in there are not
  characters: anything above 0x10FFFF (outside the standard) and the surrogates.

  Rust's `char` only holds a scalar value. Python's `chr()` is more flexible:
  it raises above 0x10FFFF but hands back a surrogate, which fails to `.encode()`.
  """
  last_char_code = 0x10FFFF  # the end of plane 16, the last plane Unicode defines
  # UTF-16 defines a range of values that only work in pairs. A number in this range,
  # without its other half (also in the same range), is not a character.
  surrogates_start, surrogates_end = 0xD800, 0xDFFF
  return (
    0 <= codepoint < surrogates_start or surrogates_end < codepoint <= last_char_code
  )


def html_entity_codepoint(entity: str) -> int | None:
  """Return the codepoint `entity` denotes, or `None` if Anki rejects it.

  Anki's Rust implementation uses the HTML 4.01 named table (in Python that's
  `html.entities.name2codepoint`).

  Note: `entity` is accepted with or without the `&` and `;`.
  """
  entity = entity.strip("&;")
  if len(entity) < 2:
    return None
  if not entity.startswith("#"):
    return name2codepoint.get(entity)
  digits, base = (entity[2:], 16) if entity[1] == "x" else (entity[1:], 10)
  digits = digits.lstrip("0") or "0"
  if len(digits) > 7:  # max 7 decimal digits or 6 hexadecimal
    return None
  code = int(digits, base)
  # Need to match Rust's `htmlescape` crate: non-scalar chars abort the decode.
  # Python's `html.unescape()` does not match, decoding values which Rust refuses to
  # decode or decoding to different values (i.e. for `&#0;`).
  return code if is_unicode_scalar(code) else None


def decode_html_entities(text: str) -> str:
  """Decode every entity in `text`, or none of them if any one is bad."""
  plain_start, entity_start = 0, text.find("&")
  if entity_start == -1:
    return text
  decoded: list[str] = []
  while entity_start != -1:
    if (match := RE_HTML_ENTITY.match(text, entity_start)) is None:
      return text
    if (codepoint := html_entity_codepoint(match.group())) is None:
      return text
    decoded.extend([text[plain_start:entity_start], chr(codepoint)])
    plain_start, entity_start = match.end(), text.find("&", match.end())
  decoded.append(text[plain_start:])
  # Anki replaces any U+A0 (&nbsp) with a space
  return "".join(decoded).replace("\u00a0", " ")


# -------------------------------------- Helpers ---------------------------------------


def _normalize_name_dropping_quotes(name: str, kind: str) -> str:
  """Normalize a name for database storage and lookup.

  Quotes are removed because they would break the search and template syntax if a name
  contained one.

  Raises:
    Genanki2Error: `name` is empty, or the repair empties it.
  """
  name = unicodedata.normalize("NFC", name).replace('"', "")
  if not name:
    raise Genanki2Error(f"{kind.title()} name cannot be empty.")
  return name


def _captured_names(pattern: re.Pattern[str], text: str) -> tuple[str, ...]:
  """Collect the captured names in `text`.

  Works on patterns with alternations in which it is guaranteed that exactly one group
  will match.

  Duplicates and order of names are preserved.
  """
  captured_names: list[str] = []
  for match in pattern.finditer(text):
    assert match.lastindex is not None
    captured_names.append(match.group(match.lastindex))
  return tuple(captured_names)


def _replace_captured_name(
  pattern: re.Pattern[str], text: str, old: str, new: str
) -> str:
  """Rename every captured name from `old` to `new` in `text`.

  Captured names are matched to `old` and text outside them is unchanged.

  A name is matched literally, without decoding HTML entities: `_a&amp;b.png` is
  renamed under that spelling.
  """

  def get_replacement(match: re.Match[str]) -> str:
    assert match.lastindex is not None
    if old == (matched := match.group(match.lastindex)):
      return match.group().replace(matched, new)
    return match.group()

  return pattern.sub(get_replacement, text)


# ------------------------------------- Templates --------------------------------------


def normalize_template_name(name: str) -> str:
  """Normalize a template name for database storage and lookup.

  Raises:
    Genanki2Error: `name` is empty, or the repair empties it.
  """
  return _normalize_name_dropping_quotes(name, "template")


RE_TEMPLATE_REFERENCES_ANKI_DELIMITERS: re.Pattern[str] = re.compile(
  r"(?s)<!--.*?-->|\{\{(.*?)\}\}"
)
RE_TEMPLATE_REFERENCES_MUSTACHE_DELIMITERS: re.Pattern[str] = re.compile(
  r"(?s)<!--.*?-->|<%(.*?)%>"
)
RE_CLOZE_CONDITIONAL: re.Pattern[str] = re.compile(r"c\d+$")

MUSTACHE_DELIMITERS_DIRECTIVE: str = "{{=<% %>=}}"


class SectionReference(NamedTuple):
  """A conditional section around a reference: `{{#key}}` or `{{^key}}`.

  Attributes:
    key: The name the section tests, without the "#" or "^".
    filled: Whether the section requires a non-empty field or not: true for
      `{{#key}}`, false for `{{^key}}`.
  """

  key: str
  filled: bool


class TemplateReference(NamedTuple):
  """A reference in a card template, with the conditional sections around it.

  Attributes:
    key: The field or special name, as written.
    filters: A replacement's filters, in the order written. Empty for a conditional.
    is_conditional: Whether the reference opens a conditional section.
    sections: The sections enclosing the reference, outermost first. A conditional's
      own section is not among them.
  """

  key: str
  filters: tuple[str, ...]
  is_conditional: bool
  sections: tuple[SectionReference, ...]


def _iter_template_references(template: str) -> Iterator[TemplateReference]:
  """Yield what a card template references, as `TemplateReference` tuples.

  A reference is:
    1. A replacement: `{{Front}}` or `{{filter:Front}}` for a filtered one. The key is
      its last colon-separated segment, while everything before it is a filter.
    2. A conditional section: opened with `{{#Front}}` or `{{^Front}}` and closed with
      `{{/Front}}`. The key is taken whole, so colons in it are part of the name.

  Keys are yielded unmodified. Empty and 1 character names are legal references, so
  `{{}}` and `{{#}}` return `""` and `"#"` respectively.

  References inside HTML comments are ignored. Cloze conditionals (`{{#c1}}`) select a
  cloze number, they don't name a field, so they are not yielded, but they still appear
  in the `sections` of the references inside them. A tag left unclosed is not an error:
  `{{Front` is ordinary text.

  A template whose first non-whitespace characters are `{{=<% %>=}}` is read with `<%`
  and `%>` as its delimiters and `{{` then means nothing.

  Raises:
    Genanki2Error: a conditional is closed without having been opened, closed under a
      different name than the one it opened, or never closed.
  """
  template = template.lstrip()
  references_pattern: re.Pattern[str] = RE_TEMPLATE_REFERENCES_ANKI_DELIMITERS
  if template.startswith(MUSTACHE_DELIMITERS_DIRECTIVE):
    references_pattern = RE_TEMPLATE_REFERENCES_MUSTACHE_DELIMITERS
    while template.startswith(MUSTACHE_DELIMITERS_DIRECTIVE):
      template = template[len(MUSTACHE_DELIMITERS_DIRECTIVE) :]

  conditionals: list[SectionReference] = []
  for match in references_pattern.finditer(template):
    reference: str | None = match.group(1)
    if reference is None:  # matched to comment <!-- * -->
      continue
    reference = reference.lstrip("{").strip()
    if len(reference) < 2 or reference[0] not in {"#", "^", "/"}:
      is_conditional = False
      *filters, key = reference.split(":")
      yield TemplateReference(key, tuple(filters), is_conditional, tuple(conditionals))
    elif reference[0] in {"#", "^"}:
      is_conditional = True
      filled: bool = reference[0] == "#"
      reference = reference[1:].lstrip()
      if not RE_CLOZE_CONDITIONAL.match(reference):
        yield TemplateReference(reference, (), is_conditional, tuple(conditionals))
      conditionals.append(SectionReference(reference, filled))
    elif reference[0] == "/":
      if not conditionals:
        raise Genanki2Error(f'Reference "{{{{{reference}}}}}" was never opened.')
      open_reference, filled = conditionals.pop()
      if open_reference != reference[1:].lstrip():
        raise Genanki2Error(
          f'Closing reference "{{{{{reference}}}}}" does not match the last opened '
          f'reference "{{{{{"#" if filled else "^"}{open_reference}}}}}".'
        )
  # Done
  if conditionals:
    listing = ", ".join(
      f'"{{{{{"#" if filled else "^"}{key}}}}}"' for key, filled in conditionals
    )
    raise Genanki2Error(f"Some opened references have not been closed: {listing}.")


@lru_cache(maxsize=128, typed=True)
def template_references(template: str) -> tuple[TemplateReference, ...]:
  """Return all references as `(key, filters, is_conditional, sections)` tuples.

  Each returned value is an instance of the `TemplateReference` named tuple.

  The references are cached by template.

  Materializing the generator is what makes a malformed template raise on the call
  rather than partway through iteration, and the cache is what keeps a template read by
  several checks from being parsed once per check. A template that raises is not
  cached, so it is parsed again on the next call.

  Raises:
    Genanki2Error: a conditional is closed without having been opened, closed under a
      different name than the one it opened, or never closed.
  """
  return tuple(_iter_template_references(template))


def template_referenced_field_names(template: str) -> frozenset[str]:
  """Return every field name that a template references, conditionals included.

  Names are returned as found in the template, special fields included.

  Raises:
    Genanki2Error: the template contains a syntax error (a conditional is closed without
      having been opened, closed under a different name than the one it opened, or
      never closed).
  """
  return frozenset(reference.key for reference in template_references(template))


def cloze_referenced_field_names(template: str) -> frozenset[str]:
  """Return the field names that a template references through the `cloze` filter.

  Conditionals are not included.

  Raises:
    Genanki2Error: the template contains a syntax error (a conditional is closed without
      having been opened, closed under a different name than the one it opened, or
      never closed).
  """
  return frozenset(
    field
    for field, filters, is_conditional, _ in template_references(template)
    if "cloze" in filters and not is_conditional
  )


def is_template_reference_reachable(
  sections: Iterable[SectionReference],
  fillable: AbstractSet[str],
  always_filled: AbstractSet[str],
) -> bool:
  """Tell whether one note can satisfy every conditional at once.

  `{{#K}}` needs `K` filled and `{{^K}}` needs it empty. The conditionals cannot all be
  satisfied when a name is needed both ways, or when a condition contradicts a name
  that is never or always filled. A plain replacement is passed as a final filled
  section, so one inside `{{^K}}` on its own name is unreachable.

  For a notetype, any note will do: a field can be filled or empty, so it belongs in
  `fillable` only, while the special names Anki fills for every note belong in both.
  For a note, pass its filled names as both sets, since each name is known to be filled
  or empty.

  Args:
    sections: The conditionals to satisfy, outermost first.
    fillable: The names that can be filled. Any other name is never filled.
    always_filled: The names that are always filled.
  """
  required: dict[str, bool] = {}
  return not any(
    required.setdefault(key, filled) != filled  # {{#K}} inside {{^K}} or vice versa
    or (filled and key not in fillable)
    or (not filled and key in always_filled)
    for key, filled in sections
  )


# Anki's writes `[^ >]` on the unquoted branch, but its regex engine discards whitespace
# inside a character class in (?x) mode and Python does not, so the space is not part
# of it. Python therefore uses `[^>]`.
RE_TEMPLATE_UNDERSCORED_REFERENCES: re.Pattern[str] = re.compile(
  r"""(?x)
          \[sound:(_[^]]+)\]  # a filename in an Anki sound tag
      |                       # or
          "(_[^"]+)"          # a double quoted
      |                       # or
          '(_[^']+)'          # single quoted string
      |                       # or
          \b(?:src|data)      # a 'src' or 'data' attribute
          =                   # followed by
          (_[^>]+)            # an unquoted value
  """
)


def template_underscored_references(template: str) -> tuple[str, ...]:
  """Collect the underscored media filenames in a template.

  A template names files beginning with `_`. This is Anki's convention.

  HTML entities are not decoded here. Duplicates and order of references are preserved.
  """
  return _captured_names(RE_TEMPLATE_UNDERSCORED_REFERENCES, template)


def replace_template_underscored_references(template: str, old: str, new: str) -> str:
  """Rename every underscored reference from `old` to `new` in a template.

  `template_underscored_references()` finds references, which are matched to `old`.
  Text outside a reference is untouched.

  A name is matched literally, without decoding HTML entities, as
  `template_underscored_references()` returns them: `_a&amp;b.png` is renamed under
  that spelling.
  """
  return _replace_captured_name(RE_TEMPLATE_UNDERSCORED_REFERENCES, template, old, new)


# ------------------------------------- Notetypes --------------------------------------


def normalize_notetype_name(name: str) -> str:
  """Normalize a notetype name for database storage and lookup.

  Raises:
    Genanki2Error: `name` is empty, or the repair empties it.
  """
  return _normalize_name_dropping_quotes(name, "notetype")


RE_NOTETYPE_UNDERSCORED_CSS_IMPORTS: re.Pattern[str] = re.compile(
  r"""(?xi)
      (?:@import\s+           # import statement with a bare
          "(_[^"]*.css)"      # double quoted
          |                   # or
          '(_[^']*.css)'      # single quoted css filename
      )
      |                       # or
      (?:url\(\s*             # a url function with a
          "(_[^"]+)"          # double quoted
          |                   # or
          '(_[^']+)'          # single quoted
          |                   # or
          (_.+?)              # unquoted filename
      \s*\))
  """
)


def notetype_underscored_css_imports(css: str) -> tuple[str, ...]:
  """Collect the underscored filenames a notetype's CSS uses.

  Duplicates and order of references are preserved.
  """
  return _captured_names(RE_NOTETYPE_UNDERSCORED_CSS_IMPORTS, css)


def replace_notetype_underscored_css_imports(css: str, old: str, new: str) -> str:
  """Rename every underscored import from `old` to `new` in a CSS.

  `notetype_underscored_css_imports()` finds imports, which are matched to `old`.
  Text outside an import is untouched.

  A name is matched literally, without decoding HTML entities, as
  `notetype_underscored_css_imports()` returns them: `_a&amp;b.css` is renamed under
  that spelling.
  """
  return _replace_captured_name(RE_NOTETYPE_UNDERSCORED_CSS_IMPORTS, css, old, new)


# --------------------------------------- Decks ----------------------------------------

RE_ASCII_CONTROL_CHARS: re.Pattern[str] = re.compile(r"[\x00-\x1f\x7f]")


def replace_ascii_control_chars(string: str, replacement: str) -> str:
  """Replace ASCII control characters (0x00-0x1f, 0x7f) in `string` with `replacement`."""
  return RE_ASCII_CONTROL_CHARS.sub(replacement, string)


RE_STRIP_WHITESPACE_AND_COLON: re.Pattern[str] = re.compile(r"^[\s:]+|[\s:]+$")

DECK_HUMAN_SEPARATOR: str = "::"
DECK_NATIVE_SEPARATOR: str = "\x1f"


def normalize_deck_name_parts(name: str, separator: str) -> list[str]:
  """Split a deck name on `separator` and normalize each component the way Anki does.

  Note: a component that normalizes to nothing is returned empty, not substituted.
  """
  native_parts: list[str] = []
  for part in name.split(separator):
    part = unicodedata.normalize("NFC", part)
    part = replace_ascii_control_chars(part, "")
    part = RE_STRIP_WHITESPACE_AND_COLON.sub("", part)
    native_parts.append(part)
  return native_parts


def native_deck_name(name: str) -> str:
  """Convert a human deck name to an Anki-native deck name."""
  return DECK_NATIVE_SEPARATOR.join(
    part if part else "blank"
    for part in normalize_deck_name_parts(name, DECK_HUMAN_SEPARATOR)
  )


def human_deck_name(name: str) -> str:
  """Convert a native deck name to a human-readable deck name."""
  return DECK_HUMAN_SEPARATOR.join(
    normalize_deck_name_parts(name, DECK_NATIVE_SEPARATOR)
  )


def normalize_deck_name(name: str) -> str:
  """Normalize every component of the name, rejecting one that turns out empty.

  Raises:
    Genanki2Error: a component is empty, or the repair empties it.
  """
  native_parts: list[str] = normalize_deck_name_parts(name, DECK_HUMAN_SEPARATOR)
  if "" in native_parts:
    parts = ", ".join(f'"{part}"' for part in native_parts)
    raise Genanki2Error(
      "A deck cannot contain an empty part after normalization. "
      f"Normalized parts: {parts}"
    )
  return DECK_HUMAN_SEPARATOR.join(native_parts)


# ------------------------------------- Note tags --------------------------------------

TAG_PARTS_SEPARATOR: str = "::"
TAGS_SEPARATORS: str = " \u3000"
RE_TAGS_SEPARATORS: re.Pattern[str] = re.compile(f"[{TAGS_SEPARATORS}]")


def split_note_tags(tags: str) -> list[str]:
  """Split a concatenated string of tags into separate non-empty tags."""
  return [part for part in RE_TAGS_SEPARATORS.split(tags) if part]


@lru_cache(maxsize=1_000, typed=True)
def normalize_note_tag(tag: str) -> str:
  """Normalize every `::` part of a tag, rejecting a tag Anki would read as several.

  A part left empty becomes `blank`, so a tag keeps the number of levels it was given.

  Anki splits a tag holding a tag separator into two instead of rejecting it.

  Raises:
    Genanki2Error: `tag` holds a tag separator.
  """
  tag = tag.strip(TAGS_SEPARATORS)
  if any(separator in tag for separator in TAGS_SEPARATORS):
    raise Genanki2Error(
      f'Tag "{tag}" contains spaces, which Anki will silently treat as two '
      "separate tags."
    )
  normalized: list[str] = []
  for part in tag.split(TAG_PARTS_SEPARATOR):
    part = unicodedata.normalize("NFC", part)
    part = replace_ascii_control_chars(part, "")
    part = part.strip()
    normalized.append(part if part else "blank")
  return TAG_PARTS_SEPARATOR.join(normalized)


def normalize_note_tags(tags: Iterable[str]) -> list[str]:
  """Normalize a note's tags, dropping the empty ones, deduplicating and sorting.

  A tag holding nothing but separators is dropped and a tag emptied by normalization is
  kept as `blank`.

  Tags are compared (and sorted) case-insensitively and deduplication takes the first
  one seen.

  See `normalize_note_tag()` for normalization details.
  """
  unique: dict[str, str] = {}
  for tag in tags:
    if not tag.strip(TAGS_SEPARATORS):
      continue
    normalized: str = normalize_note_tag(tag)
    unique.setdefault(normalized.casefold(), normalized)
  return sorted(unique.values(), key=str.casefold)


def tag_spelling_under_registered_ancestor(
  tag: str, registered: Mapping[str, str]
) -> str:
  """Return `tag` under the case of the nearest ancestor `registered` holds.

  The ancestor supplies the case of the whole prefix and the components below it are
  kept as written, which is how Anki registers a tag whose parent it already holds.

  Args:
    tag: The tag to spell, `::` separated.
    registered: Every tag already registered, keyed by its casefolded name.
  """
  parts: list[str] = tag.split(TAG_PARTS_SEPARATOR)
  for depth in range(len(parts) - 1, 0, -1):
    prefix: str = TAG_PARTS_SEPARATOR.join(parts[:depth])
    if (ancestor := registered.get(prefix.casefold())) is not None:
      return TAG_PARTS_SEPARATOR.join((ancestor, *parts[depth:]))
  return tag


# -------------------------------- Fields / Note fields --------------------------------

RE_FIELD_NAME_BAD_CHARS: re.Pattern[str] = re.compile(r'[:{}"]')
RE_FIELD_NAME_BAD_START_CHARS: re.Pattern[str] = re.compile(r"^[#/^\s]+")
"""Leading bad characters in a field's name.

Python's whitespace class `\\s` counts U+001C to U+001F as whitespace and Anki does
not, so a name starting with one of them loses it here and keeps it there."""
FIELD_VALUE_SEPARATOR: str = "\x1f"


def normalize_field_name(name: str) -> str:
  """Normalize a note field name for database storage and lookup.

  Characters that mean something in Anki template or search syntax are removed.

  Anki refuses an empty name too, and nothing could refer to it.

  Raises:
    Genanki2Error: `name` is empty, or the repair empties it.
  """
  name = unicodedata.normalize("NFC", name)
  name = RE_FIELD_NAME_BAD_CHARS.sub("", name)
  name = RE_FIELD_NAME_BAD_START_CHARS.sub("", name.strip())
  if len(name) < 1:
    raise Genanki2Error("Name cannot be empty.")
  return name


RE_ASCII_CONTROL_CHARS_IN_FIELDS: re.Pattern[str] = re.compile(
  r"[\x00-\x08\x0b-\x1f\x7f]"
)


def normalize_field_value(field: str) -> str:
  """Normalize a note field for database storage and lookup.

  Every ASCII control character is removed except tab and newline. Also removed is the
  field separator (`0x1f`) because a note's values are joined on it into the `flds`
  column, so a value allowed to hold one would later split back as two.
  """
  return unicodedata.normalize("NFC", RE_ASCII_CONTROL_CHARS_IN_FIELDS.sub("", field))


def normalize_note_fields(fields: Iterable[str]) -> list[str]:
  """Normalize every field value that a note holds."""
  return [normalize_field_value(field) for field in fields]


RE_EMPTY_FIELD_VALUE: re.Pattern[str] = re.compile(
  r"""(?xai)
    (?:
    \s                  # ASCII whitespace only: a non-breaking space fills a field
    |
    </?(?:br|div)\ ?/?>
    )*
  """
)


def is_field_value_empty(field: str) -> bool:
  """Tell if Anki treats a field value as empty when deciding which cards to generate.

  A value holding only whitespace and line-break tags (`<br>`, `<div>`, `</div>`) is
  empty. Any other markup, an entity such as `&nbsp;` or a Unicode space counts as
  content.
  """
  return RE_EMPTY_FIELD_VALUE.fullmatch(field) is not None


RE_FIELD_VALUE_AUDIO_TAG: re.Pattern[str] = re.compile(r"(?s)\[sound:(.+?)\]")
RE_REMOTE_MEDIA_REF: re.Pattern[str] = re.compile(r"(?i)(https?|ftp)://")
RE_LATEX: re.Pattern[str] = re.compile(
  r"""(?xsi)
    \[latex\](.+?)\[/latex\]     # 1 - standard latex
    |
    \[\$\](.+?)\[/\$\]           # 2 - inline math
    |
    \[\$\$\](.+?)\[/\$\$\]       # 3 - math environment
  """
)
RE_LATEX_NEWLINES: re.Pattern[str] = re.compile(
  r"""(?xi)
    <br( /)?>
    |
    <div>
  """
)


def field_value_media_refs(field: str) -> list[str]:
  """Collect the media filenames that a note field contains.

  A media filename is in either an HTML tag (`src=...`, `data=...`) or Anki's
  `[sound:...]` tag. HTML entities are decoded. Duplicates are kept, in the order they
  are found.
  """
  media_refs: list[str] = []
  for match in RE_HTML_MEDIA_TAGS.finditer(field):
    assert match.lastindex is not None
    media_refs.append(decode_html_entities(match.group(match.lastindex)))
  for match in RE_FIELD_VALUE_AUDIO_TAG.finditer(field):
    media_refs.append(decode_html_entities(match.group(1)))
  return media_refs


def field_value_latex_image_names(field: str, svg: bool) -> list[str]:
  """Name the images Anki renders from the LaTeX in a note field.

  LaTeX goes in a field between `[latex]` and `[/latex]`, `[$]` and `[/$]` for inline
  math, or `[$$]` and `[/$$]` for displayed math. The field keeps the source. Anki
  compiles each span into an image when a card is shown and saves it as a media file
  named from a hash of the source, so the names follow from the field alone. `svg`
  gives them the `.svg` extension instead of `.png`.

  Duplicates are kept, in the order they are found.
  """
  hashes: list[str] = []
  for match in RE_LATEX.finditer(field):
    assert match.lastindex is not None
    body: str = match.group(match.lastindex)
    # 1: as is
    if match.lastindex == 2:
      body = f"${body}$"
    elif match.lastindex == 3:
      body = f"\\begin{{displaymath}}{body}\\end{{displaymath}}"
    body = RE_LATEX_NEWLINES.sub("\n", body)
    body = RE_HTML.sub("", body)
    body = decode_html_entities(body)
    hashes.append(hashlib.sha1(body.encode()).hexdigest())
  return [f"latex-{digest}{'.svg' if svg else '.png'}" for digest in hashes]


def replace_field_value_media_refs(field: str, old: str, new: str) -> str:
  """Rename every media reference from `old` to `new` in a field's value.

  `field_value_media_refs()` finds references, which are matched to `old`.
  Text outside a reference is untouched.

  `new` is escaped only where the replaced reference was itself escaped. This is
  Anki's rule.
  """
  escaped_new: str = html.escape(new)

  def maybe_escaped_new(match: re.Match[str]) -> str:
    assert match.lastindex is not None
    matched: str = match.group(match.lastindex)
    decoded: str = decode_html_entities(matched)
    if old == decoded:
      replacement: str = new if matched == decoded else escaped_new
      return match.group().replace(matched, replacement)
    return match.group()

  new_field: str = RE_HTML_MEDIA_TAGS.sub(maybe_escaped_new, field)
  new_field = RE_FIELD_VALUE_AUDIO_TAG.sub(maybe_escaped_new, new_field)
  return new_field


def strip_field_value_html_preserving_media_names(field: str) -> str:
  """Drop markup but keep media filenames.

  It's the same way Anki derives the sorting field and checksum.
  """
  # 1. Media tags collapse to their filename
  # Note: in the media tags regular expression, only one of the three filename groups
  # ever participates; `re.sub` expands the other two to empty. Also, the padding spaces
  # match Anki's implementation. DO NOT CHANGE: any change would change the checksum.
  out: str = RE_HTML_MEDIA_TAGS.sub(r" \1\2\3 ", field)
  # 2. Remove remaining HTML syntax
  out = RE_HTML.sub("", out)
  # 3. Decode HTML entities, all of them or none
  return decode_html_entities(out)


RE_FIELD_VALUE_CLOZE: re.Pattern[str] = re.compile(
  r"""(?x)
    \{\{c([0-9,]*)::    # 1 - the numbers of an opening marker, in ASCII digits only
    |
    \}\}                # a closing marker
  """
)


def _cloze_marker_numbers(listing: str) -> set[int]:
  """Parse a comma separated list of UInt16 values and return valid unique values."""
  return {
    number for item in listing.split(",") if item and is_uint16(number := int(item))
  }


def field_value_cloze_numbers(field: str) -> frozenset[int]:
  """Return the cloze numbers in a field value, each giving a cloze note one card.

  A cloze is written `{{c1::text}}`, or `{{c1,2::text}}` when several cards share it,
  and counts once its `}}` is reached. A cloze never closed is ignored, together
  with the clozes closed inside it. Numbers above 65535 are ignored, a marker left
  with none is plain text, and `c0` gives no card. Nesting stops at 10 open clozes.
  """
  cloze_numbers: set[int] = set()
  stack: list[set[int]] = []
  for match in RE_FIELD_VALUE_CLOZE.finditer(field):
    if (listing := match.group(1)) is not None:  # opening token
      numbers: set[int] = _cloze_marker_numbers(listing)
      if numbers and len(stack) < 10:
        stack.append(numbers)
    elif stack:  # closing token
      numbers = stack.pop()
      parent_numbers: set[int] = stack[-1] if stack else cloze_numbers
      parent_numbers.update(numbers)

  cloze_numbers.discard(0)
  return frozenset(cloze_numbers)


@dc.dataclass
class _Cloze:
  """A cloze in a parsed field value.

  Attributes:
    numbers: The cloze numbers in its opening marker, sorted, `0` included.
    nodes: Its content: text, and the clozes nested in it.
    hint: The text after `::` in its content, if any.
  """

  numbers: tuple[int, ...]
  nodes: "list[str | _Cloze]" = dc.field(default_factory=list)
  hint: str | None = None

  def is_image_occlusion(self) -> bool:
    return bool(self.nodes) and str(self.nodes[0]).startswith("image-occlusion:")


def _parse_field_value_clozes(field: str) -> "list[str | _Cloze]":
  """Parse a field value into text and clozes, as Anki does before revealing them.

  The rules are those of `field_value_cloze_numbers()`: a marker with no usable
  number is plain text, a `}}` outside any cloze is plain text, an opening marker past
  10 open clozes is dropped, and a cloze never closed is dropped with its content.

  Each text inside a cloze gives the cloze its hint (after `::`), unless it starts with
  `image-occlusion:`. A marker that doesn't open a cloze stays in the text, so its `::`
  can start the hint.
  """
  root: list[str | _Cloze] = []
  stack: list[_Cloze] = []
  text_start: int = 0

  def add_text(text: str) -> None:
    if not text:
      return
    if not stack:
      root.append(text)
      return
    cloze: _Cloze = stack[-1]
    if not text.startswith("image-occlusion:"):
      head, separator, hint = text.partition("::")
      if separator:
        text, cloze.hint = head, hint
    cloze.nodes.append(text)

  for match in RE_FIELD_VALUE_CLOZE.finditer(field):
    listing: str | None = match.group(1)
    numbers: set[int] = _cloze_marker_numbers(listing) if listing is not None else set()
    if listing is not None and not numbers:
      # if listing isn't valid, it will be added as plain text later
      continue
    add_text(field[text_start : match.start()])
    text_start = match.end()
    if listing is not None:  # opening token
      if len(stack) < 10:
        stack.append(_Cloze(tuple(sorted(numbers))))
    elif stack:  # closing token
      closed_cloze: _Cloze = stack.pop()
      parent_nodes: list[str | _Cloze] = stack[-1].nodes if stack else root
      parent_nodes.append(closed_cloze)
    else:
      root.append("}}")

  add_text(field[text_start:])
  return root


def _reveal_field_value_clozes(
  nodes: "list[str | _Cloze]", number: int, question: bool
) -> str:
  """Render parsed clozes for one side of card `number`, or "" if none is active.

  The markup is Anki's with the attributes left out. Only the text survives into a
  LaTeX image name, but the tags stay, since one can close a stray `<` before it.
  """
  visited_numbers: set[int] = set()

  def reveal(cloze: _Cloze) -> str:
    visited_numbers.update(cloze.numbers)
    if cloze.is_image_occlusion():
      return '<div class="cloze"></div>'
    active: bool = number in cloze.numbers
    if question and active:
      hint: str = "..." if cloze.hint is None else cloze.hint
      return f'<span class="cloze">[{hint}]</span>'
    content: str = "".join(
      node if isinstance(node, str) else reveal(node) for node in cloze.nodes
    )
    return f'<span class="{"cloze" if active else "cloze-inactive"}">{content}</span>'

  text: str = "".join(node if isinstance(node, str) else reveal(node) for node in nodes)
  return text if number in visited_numbers else ""


def field_value_cloze_latex_image_names(field: str, svg: bool) -> list[str]:
  """Return the names of images that Anki renders from LaTeX holding a cloze.

  A cloze is hidden on the front and revealed on the back side. LaTeX containing
  a cloze is rendered on both. Anki renders images for each card side (but the
  names don't match the names produced by `field_value_latex_image_names()` when given
  the raw cloze markup). This function generates all possible field values, for each
  cloze number, hidden or revealed, in order to generate all LaTeX image names.

  A field without LaTeX or without a cloze number doesn't return any name. Duplicates
  are kept.
  """
  if not RE_LATEX.search(field):
    return []
  nodes: list[str | _Cloze] = _parse_field_value_clozes(field)
  expanded: str = "".join(
    _reveal_field_value_clozes(nodes, number, question)
    for number in sorted(field_value_cloze_numbers(field))
    for question in (True, False)
  )
  return field_value_latex_image_names(expanded, svg)


# ---------------------------------- Media filenames -----------------------------------

RE_MEDIA_NAME_BAD_CHARS: re.Pattern[str] = re.compile(r'[\x00-\x1f\x7f\[\]<>:"/?*^\\|]')
RE_WINDOWS_DEVICE_NAMES: re.Pattern[str] = re.compile(
  r"(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)"
)


def is_media_name_safe(name: str) -> bool:
  """Decide whether `name` can be written as one file inside a directory of the caller.

  A package is untrusted input and reading one joins the media file names with the
  extraction path, so a name that resolves anywhere else has to be refused. Both Linux
  and Windows path separators are refused.

  The file name may still be ill-formed. Use `normalize_media_name()` to repair.
  """
  return (
    name not in ("", ".", "..")
    and "/" not in name
    and "\\" not in name
    and not PureWindowsPath(name).drive
  )


def normalize_media_name(name: str) -> str:
  """Repair a media file name in the form that Anki stores it.

  A media file's name is what a note field references and what the media map ties to a
  zip entry. With the latest package envelope, Anki rejects the whole package at import
  if any media name is not normalized. The legacy envelope repairs it instead and this
  function is that repair.

  One step drops every unassigned Unicode char. Python usually ships a newer Unicode
  table (13.0+) than Anki (10.0), so a character assigned after 10.0 counts as assigned
  here and as unassigned in Anki. Left alone, such a character survives this repair and
  the resulting package does not import.

  The repair also deletes every character `RE_ASSIGNED_AFTER_ANKIS_UNICODE` matches,
  the ones assigned after the Unicode version Anki checks against.
  When Anki upgrades their Unicode version, genanki2 will be stricter, so
  a repair is guaranteed to not fail at import. Regenerate the regexes with
  `scripts/dump_unicode_ages.py --anki-unicode MAJOR.MINOR` whenever Anki's Unicode
  version moves.
  """
  # The steps run in order, and the order matters
  name = unicodedata.normalize("NFC", name)
  name = RE_MEDIA_NAME_BAD_CHARS.sub("", name)
  if not name.isascii():
    # Remove any unassigned Unicode codepoint
    name = RE_ASSIGNED_AFTER_ANKIS_UNICODE.sub("", name)
    name = "".join(char for char in name if unicodedata.category(char) != "Cn")
  name = name.replace("\u00a0", " ")  # nbsp -> plain space
  name = RE_WINDOWS_DEVICE_NAMES.sub(r"\1_\2", name)
  if name and name[-1] in " .":
    name += "_"
  if len(name.encode()) > 120:  # truncate to 120 bytes
    stem, ext = name.rsplit(".", 1) if "." in name else (name, "")
    encoded_ext: bytes = ext.encode()
    while ext and len(encoded_ext) > 10:
      ext = ext[:-1]
      encoded_ext = ext.encode()
    encoded_stem: bytes = stem.encode()
    while len(encoded_stem) > 120 - len(encoded_ext) - 2:
      stem = stem[:-1]
      encoded_stem = stem.encode()
    name = f"{stem}.{ext}" if ext else stem
  if name and name[-1] in " .":
    name += "_"
  return name


def free_normalized_media_name(name: str, taken: Collection[str]) -> str:
  """Return `name` normalized, with a `-N` suffix when `taken` already holds it.

  Two names can normalize to one: `a?b.mp3` and `a*b.mp3` both become `ab.mp3`, and
  storing both under that name would drop a file. The suffix is numeric and goes before
  the extension. Every candidate is normalized because a suffix can push a name past
  the length limit and be truncated again.

  A name that normalizes to nothing comes back empty.

  `name` is case folded before comparing to the values in `taken`, so the free names
  work on case-insensitive operating systems (Windows, macOS).

  Args:
    name: The file name to place, normalized or not.
    taken: The names already in use. They should be case folded by the caller.

  Raises:
    Genanki2Error: no free name could be derived. It takes a name at the length limit,
      where every candidate truncates back to the same taken name.
  """
  name = normalize_media_name(name)
  if name.casefold() not in taken:
    return name

  stem, dot, extension = name.rpartition(".")
  if not dot:
    stem, extension = extension, ""
  for suffix in range(1, len(taken) + 2):
    candidate: str = normalize_media_name(f"{stem}-{suffix}{dot}{extension}")
    if candidate.casefold() not in taken:
      return candidate
  raise Genanki2Error(
    f'No free name could be derived from "{name}": every candidate is already taken. '
    "A name at the 120 byte limit loses the suffix to truncation, so it can only be "
    "stored once. Shorten it and rename the references to it."
  )
