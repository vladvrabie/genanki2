"""Does `normalize_media_name` repair a media name the way Anki does? (media names)

    uv run --with anki --with pytest pytest tests/test_media_name_normalization.py

The current package envelope offers no second chance on a media name. Anki repairs an
unnormalized name only on the legacy path, rewriting the notes that reference it to
match; a `MediaEntry` carrying such a name fails the whole import as `Corrupt`, naming
no file. So genanki2 has to reach the same answer Anki would have reached, and the only
authority on that answer is Anki.

`col.media.write_data` returns the name the file was stored under, which is the output of
the same normalization the importer holds entries to. Two of its other behaviours are not
that normalization and have to be kept from firing. A name arriving twice with different
content gets a content hash appended, so every case writes identical bytes. A name that
is not already lowercase is lowercased whole, as a guard for case-insensitive
filesystems, so every case is lowercase; the importer preserves case, and `CON_.mp3` is
accepted exactly as written.

anki is a test-only dependency, never a dependency of genanki2.
"""

import unicodedata
from collections.abc import Callable

import pytest
from anki.collection import Collection as AnkiCollection

from genanki2.text import normalize_media_name

PAYLOAD = b"ID3"

# One name per rule, and per way of tripping it.
NAMES = [
  "hello.mp3",  # nothing to repair
  "cafè.mp3",  # NFD, composes to café.mp3
  "a<b>c.mp3",
  'quote".mp3',
  "pipe|colon:.mp3",
  "question?.mp3",
  "star*.mp3",
  "caret^.mp3",
  "bracket[1].mp3",
  "back\\slash.mp3",
  "bell\x07.mp3",
  "del\x7f.mp3",
  "͸unassigned.mp3",  # unassigned in every Unicode version to date
  "nbsp gap.mp3",
  "con.mp3",
  "nul",
  "prn",
  "aux",
  "com1.wav",
  "lpt9.ogg",
  "console.mp3",  # a device name is only one when the whole stem is one
  "con.con.mp3",  # only the leading one is a device name
  "nul.nul",
  "trailing space ",
  "trailing period.",
  "a" * 130 + ".mp3",
  "b" * 130,  # no extension to hold back
  "c" * 130 + ".averylongextension",  # an extension of its own to cap
  "é" * 70,  # 140 bytes, and every cut lands mid-character
  "é" * 70 + ".mp3",
  "x" * 130 + ".日本語拡張子",  # a multi-byte extension to cap
]

# One codepoint per Unicode version newer than the 10.0 tables Anki reads assignment
# from, up to the version this Python's `unicodedata` ships (13.0 on Python 3.10). A
# codepoint from a version above that is unassigned on both sides and exercises nothing.
NEWER_THAN_ANKIS_TABLES = {
  "11.0": "\u0560",  # ARMENIAN SMALL LETTER TURNED AYB
  "12.0": "\u0c77",  # TELUGU SIGN SIDDHAM
  "12.1": "\u32ff",  # SQUARE ERA NAME REIWA
  "13.0": "\u08be",  # ARABIC LETTER PEH WITH SMALL V
}


@pytest.fixture(scope="module")
def anki_normalize(tmp_path_factory):
  """Anki's own answer for a name, taken from the collection that would store it."""
  collection = AnkiCollection(str(tmp_path_factory.mktemp("oracle") / "oracle.anki2"))
  yield lambda name: collection.media.write_data(name, PAYLOAD)
  collection.close()


@pytest.mark.parametrize("name", NAMES)
def test_a_name_is_repaired_the_way_anki_repairs_it(
  name: str, anki_normalize: Callable[[str], str]
):
  """A disagreement here is a genanki2 bug, whichever way it goes.

  Repairing less than Anki writes a package Anki refuses. Repairing more renames a file
  Anki would have kept, and the note referencing it then points at nothing.
  """
  assert name == name.lower(), "the oracle lowercases, so an uppercase case is not one"

  assert normalize_media_name(name) == anki_normalize(name)


def test_the_device_name_rule_ignores_case():
  """Anki matches device names case-insensitively and leaves the case it found alone.

  The expectation is written out rather than taken from the oracle, because storing a
  file is where Anki lowercases a name and normalizing one is not. An import accepts
  `CON_.mp3` under that name and refuses `CON.mp3`, so the repair keeps the case.
  """
  assert normalize_media_name("CON.mp3") == "CON_.mp3"
  assert normalize_media_name("Com1.WAV") == "Com1_.WAV"


@pytest.mark.parametrize(
  "char", NEWER_THAN_ANKIS_TABLES.values(), ids=NEWER_THAN_ANKIS_TABLES
)
def test_a_codepoint_newer_than_ankis_tables_is_dropped(
  char: str, anki_normalize: Callable[[str], str]
):
  """Both sides delete a codepoint assigned after Unicode 10.0, for different reasons.

  Anki reads a codepoint's category out of a table pinned at Unicode 10.0 and deletes
  everything that table calls unassigned. `unicodedata` carries a newer one, so it calls
  these characters assigned and the category test alone would keep them, leaving a name
  Anki refuses. `_unicode_ages` closes the gap: its generated class matches every
  codepoint assigned since 10.0, and those go before the category is consulted.

  A failure means Anki moved its Unicode version. Regenerate the table with
  scripts/dump_unicode_ages.py against the version Anki now reads.
  """
  name = f"{char}newer.mp3"

  assert unicodedata.category(char) != "Cn", "this Python does not know the codepoint"
  assert normalize_media_name(name) == "newer.mp3"
  assert anki_normalize(name) == "newer.mp3"
