"""Anki's stock notetypes, decks, presets and collection defaults.

- the six built-in notetypes;
- the `Default` deck & deck config;
- the collection configs and metadata an empty collection holds;

Everything here is rebuilt from an empty collection dump produced by
scripts/dump_empty_collection_rows.py with the official `anki` package, so a builtin
is what Anki wrote rather than a transcription of it. The dump makes a few changes to
those values, all related to how an import matches what it finds and what it does then:
- notetype ids are renumbered 1 to 6, since Anki mints timestamp ids and a stock id
  has to be identical in every package genanki2 writes;
- field and template config ids are cleared, so that a notetype comparison falls back
  to names;
- every notetype name gains a ` (genanki2)` suffix, bypassing Anki's renaming
  behaviour (appending a '+');
- every notetype's modification time is set to a version number instead of a
  timestamp, which is what decides whether an import replaces a notetype the target
  collection already holds (see `BUILTIN_NOTETYPES_VERSIONS`);

The constants are frozen and cannot be changed because every note that references one,
in every package the process builds, shares it: assigning to one or changing a list it
holds raises. A copy made with `model_copy()` is frozen too. Use `builtin_notetype()`,
`default_deck()` and `default_deck_config()` to generate a customizable copy. For
consistent package updates without collection duplicates on import, provide the same id
parameter to `builtin_notetype()` on every package rebuild.
"""

from frozendict import frozendict

import genanki2._default_rows as _default_rows  # noqa: PLR0402, import cycle via __init__
from genanki2._typed_schema import FieldRows, NotetypeRow, NotetypeRows, TemplateRows
from genanki2.errors import Genanki2Error
from genanki2.models.collection_metadata import CollectionMetadata
from genanki2.models.config import ConfigEntry
from genanki2.models.deck import Deck
from genanki2.models.deck_config import DeckConfig
from genanki2.models.notetype import Notetype

__all__ = [
  "BASIC_AND_REVERSED_NOTETYPE",
  "BASIC_NOTETYPE",
  "BASIC_OPTIONAL_REVERSED_NOTETYPE",
  "BASIC_TYPE_IN_THE_ANSWER_NOTETYPE",
  "BUILTIN_NOTETYPES",
  "BUILTIN_NOTETYPES_VERSIONS",
  "CLOZE_NOTETYPE",
  "DEFAULT_DECK",
  "DEFAULT_DECK_CONFIG",
  "IMAGE_OCCLUSION_NOTETYPE",
  "builtin_notetype",
  "default_collection_metadata",
  "default_config_entries",
  "default_deck",
  "default_deck_config",
]


def builtin_notetype(name: str, *, id: int | None = None) -> Notetype:
  """Build one of the six stock notetypes, by its suffixed name.

      notetype = builtin_notetype("Basic (genanki2)")

  A fresh object every call, so what it returns can be changed without touching the
  constant of the same name.

  A changed builtin needs an id of its own. Anki matches an incoming notetype to an
  existing one by id. On a match, Anki also compares the fields and templates but not
  the name, so a simple rename that keeps the stock id merges into whatever holds
  that id instead of landing beside it.

  Important: The id has to be the same on every rebuild of the same deck, or an update
  imports as a duplicate. This is the caller's responsibility: read it from the
  package written last time, never mint a fresh one.

  Args:
    name: One of the six names, suffix included.
    id: The notetype's id. Defaults to the stock id, 1 to 6.

  Returns:
    A new `Notetype`, built from the stock rows and carrying `id` if given.

  Raises:
    Genanki2Error: `name` is not one of the six.
  """
  nt_table: NotetypeRows = _default_rows.table_notetypes  # pyright: ignore[reportAssignmentType]
  nt_row: NotetypeRow | None = next(
    (row for row in nt_table if row["name"] == name), None
  )
  if nt_row is None:
    raise Genanki2Error(f'Could not find a notetype using name="{name}".')

  fields_table: FieldRows = _default_rows.table_fields  # pyright: ignore[reportAssignmentType]
  field_rows: FieldRows = [row for row in fields_table if row["ntid"] == nt_row["id"]]

  templates_table: TemplateRows = _default_rows.table_templates  # pyright: ignore[reportAssignmentType]
  template_rows: TemplateRows = [
    row for row in templates_table if row["ntid"] == nt_row["id"]
  ]

  notetype: Notetype = Notetype.from_rows(nt_row, field_rows, template_rows)
  if id is not None:
    notetype.id = id
  return notetype


BASIC_NOTETYPE: Notetype = builtin_notetype(name="Basic (genanki2)").freeze()
BASIC_AND_REVERSED_NOTETYPE: Notetype = builtin_notetype(
  name="Basic (and reversed card) (genanki2)"
).freeze()
BASIC_OPTIONAL_REVERSED_NOTETYPE: Notetype = builtin_notetype(
  name="Basic (optional reversed card) (genanki2)"
).freeze()
BASIC_TYPE_IN_THE_ANSWER_NOTETYPE: Notetype = builtin_notetype(
  name="Basic (type in the answer) (genanki2)"
).freeze()
CLOZE_NOTETYPE: Notetype = builtin_notetype(name="Cloze (genanki2)").freeze()
IMAGE_OCCLUSION_NOTETYPE: Notetype = builtin_notetype(
  name="Image Occlusion (genanki2)"
).freeze()

BUILTIN_NOTETYPES: tuple[Notetype, ...] = (
  BASIC_NOTETYPE,
  BASIC_AND_REVERSED_NOTETYPE,
  BASIC_OPTIONAL_REVERSED_NOTETYPE,
  BASIC_TYPE_IN_THE_ANSWER_NOTETYPE,
  CLOZE_NOTETYPE,
  IMAGE_OCCLUSION_NOTETYPE,
)
"""Contains the 6 built-in notetypes, sorted by the fixed id (1-6).

To get a name-keyed map, use a dict: `{nt.name: nt for nt in BUILTIN_NOTETYPES}`
"""

BUILTIN_NOTETYPES_VERSIONS: frozendict[str, int] = frozendict(
  {
    "Basic (genanki2)": 0,
    "Basic (and reversed card) (genanki2)": 0,
    "Basic (optional reversed card) (genanki2)": 0,
    "Basic (type in the answer) (genanki2)": 0,
    "Cloze (genanki2)": 0,
    "Image Occlusion (genanki2)": 0,
  }
)
"""The version each builtin ships, keyed by name. Currently 0 for all six.

An import reads a notetype's modification time to decide on a merge: the incoming
notetype replaces the one existing in the target collection only when the incoming
value is larger, and the value is stored as it arrives (not restamped). So the six
carry a version number here instead of a timestamp.

A version that stays put is what makes a rebuild safe. Two packages built a year apart
present the same number, the import declines to merge, and a notetype the user restyled
keeps their fields and templates. A current timestamp would present a larger number
every time and overwrite those edits on every import.

Raising a version is the way a corrected builtin reaches a collection that already
holds the old one, and it costs the user whatever they changed in that notetype. It
happens by hand, in `scripts/dump_empty_collection_rows.py`, when the content of a
builtin actually changes, and this table is updated to match.
"""


def default_deck_config() -> DeckConfig:
  """Build the `Default` deck config, id 1, which every collection holds.

  A fresh object every call.

  Deck configs are matched by id on import, so a package that ships this one lands on
  the target's own `Default` rather than adding a second deck config.
  """
  return DeckConfig.from_row(_default_rows.table_deck_config[0])  # pyright: ignore[reportArgumentType]


DEFAULT_DECK_CONFIG: DeckConfig = default_deck_config().freeze()


def default_deck() -> Deck:
  """Build the `Default` deck, id 1, which every collection holds.

  A fresh object every call.

  Decks are matched by name on import, so a package that ships this one lands on
  the target's own `Default` rather than adding a second deck. It is here because
  it is the deck a card falls back to.
  """
  return Deck.from_row(_default_rows.table_decks[0])  # pyright: ignore[reportArgumentType]


DEFAULT_DECK: Deck = default_deck().freeze()


def default_config_entries() -> list[ConfigEntry]:
  """Build the sixteen config entries an empty collection holds.

  An import does not copy the collection's config into the target, but it reads the
  scheduling entries off the package, and `creationOffset` is one of them: with
  `col.crt` (collection creation time) it decides the day count that rebases the due
  date of every review card. `rollover` is not among the sixteen, and Anki reads a
  missing one as 4.
  """
  return [ConfigEntry.from_row(row) for row in _default_rows.table_config]  # pyright: ignore[reportArgumentType]


def default_collection_metadata() -> CollectionMetadata:
  """Build the `col` row an empty collection holds.

  The dump zeroes `crt` (creation time) and `scm` (schema modification time), so this
  is a reference row and not the metadata of a collection being written now: due dates
  are counted in days since `crt`, and 0 is 1970. A new collection wants
  `CollectionMetadata()`, which stamps the current rollover time.
  """
  return CollectionMetadata.from_row(_default_rows.table_col[0])  # pyright: ignore[reportArgumentType]
