"""Does a note warn about misplaced cloze markup exactly when Anki's Add dialog refuses it?

    uv run --with anki --with pytest pytest tests/test_note_cloze_warnings_match_anki.py

The Add dialog refuses a note when pylib's `Note.fields_check()` returns
`NOTETYPE_NOT_CLOZE`, a cloze on a normal notetype, or `FIELD_NOT_CLOZE`, a cloze in a
field the first template's front does not pass through `cloze:` (`field_cloze_check`,
`rslib/src/notes/mod.rs:644`). `Note.warn_normal_note_cloze_field()` and
`Note.warn_cloze_on_wrong_field()` stand for those two states. Each notetype below is
mirrored into a real collection with the same fields, fronts and kind, and its notes are
checked without being added, so no duplicate state gets in the way.

`fields_check()` reports an empty first field before `NOTETYPE_NOT_CLOZE`, so every
value has text. `MISSING_CLOZE` and the other states are no refusal and compare as no
warning.

One difference is by design. `fields_check()` reads only the outermost clozes of a
field (`contains_cloze`, `rslib/src/cloze.rs:501`), while card generation reads nested
ones too, and genanki2 follows card generation. A cloze nested in `c0` is the case that
tells them apart, so it is left out of the comparison and pinned on its own.

anki is a test-only dependency, never a dependency of genanki2.
"""

import itertools
import random
import warnings
from collections.abc import Callable, Sequence

import pytest
from anki.collection import Collection as AnkiCollection
from anki.models import MODEL_CLOZE
from anki.notes import NoteFieldsCheckResult

from genanki2.builtins import (
  BASIC_NOTETYPE,
  CLOZE_NOTETYPE,
  IMAGE_OCCLUSION_NOTETYPE,
  builtin_notetype,
)
from genanki2.models.note import Note
from genanki2.models.notetype import Notetype

NOTETYPE_NOT_CLOZE = NoteFieldsCheckResult.NOTETYPE_NOT_CLOZE
FIELD_NOT_CLOZE = NoteFieldsCheckResult.FIELD_NOT_CLOZE

# Field values without a cloze, with one Anki ignores, with a cloze giving two cards, and
# with one nested inside another.
VALUES = [
  "x",
  "{{c0::a}}",
  "{{c1::a}}",
  "{{c1,2::a}}",
  "{{c1::{{c2::a}}}}",
]

NESTED_IN_C0 = "{{c0::{{c1::a}}}}"

# Every combination up to this many per notetype, a seeded sample beyond it.
MAX_COMBINATIONS = 1_000


def a_cloze_notetype_with_front(name: str, front: str) -> Notetype:
  notetype = builtin_notetype("Cloze (genanki2)")
  notetype.name = name
  notetype.templates[0].config.q_format = front
  return notetype


# Back Extra appears on the front, but only as a conditional, so it is no cloze field.
CONDITIONAL_ONLY_NOTETYPE = a_cloze_notetype_with_front(
  "Conditional only", "{{cloze:Text}}{{#Back Extra}}!{{/Back Extra}}"
)

NOTETYPES = [
  BASIC_NOTETYPE,
  CLOZE_NOTETYPE,
  IMAGE_OCCLUSION_NOTETYPE,
  CONDITIONAL_ONLY_NOTETYPE,
]

AnkiState = Callable[[Notetype, Sequence[str]], int | None]


@pytest.fixture(scope="module")
def anki_state(tmp_path_factory):
  """The refusal `fields_check()` returns for a note with these values, if any."""
  collection = AnkiCollection(str(tmp_path_factory.mktemp("oracle") / "oracle.anki2"))
  mirrored: dict[str, dict] = {}

  def mirror(notetype: Notetype) -> dict:
    if notetype.name not in mirrored:
      models = collection.models
      anki_notetype = models.new(notetype.name)
      if notetype.is_cloze():
        anki_notetype["type"] = MODEL_CLOZE
      for name in notetype.field_names:
        models.add_field(anki_notetype, models.new_field(name))
      for template in notetype.templates:
        anki_template = models.new_template(template.name)
        anki_template["qfmt"] = template.config.q_format
        anki_template["afmt"] = template.config.a_format
        models.add_template(anki_notetype, anki_template)
      models.add(anki_notetype)
      mirrored[notetype.name] = models.by_name(notetype.name)
    return mirrored[notetype.name]

  def state(notetype: Notetype, values: Sequence[str]) -> int | None:
    note = collection.new_note(mirror(notetype))
    note.fields = list(values)
    result = note.fields_check()
    return result if result in (NOTETYPE_NOT_CLOZE, FIELD_NOT_CLOZE) else None

  yield state
  collection.close()


def warns(method: Callable[[], object]) -> bool:
  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    method()
  return bool(caught)


def genanki2_state(notetype: Notetype, values: Sequence[str]) -> int | None:
  """The refusal genanki2's warnings stand for, if any."""
  note = Note(notetype=notetype, fields=list(values))
  if warns(note.warn_normal_note_cloze_field):
    return NOTETYPE_NOT_CLOZE
  if warns(note.warn_cloze_on_wrong_field):
    return FIELD_NOT_CLOZE
  return None


def value_combinations(field_count: int) -> list[tuple[str, ...]]:
  combinations = list(itertools.product(VALUES, repeat=field_count))
  if len(combinations) <= MAX_COMBINATIONS:
    return combinations
  return random.Random(0).sample(combinations, MAX_COMBINATIONS)


@pytest.mark.parametrize("notetype", NOTETYPES, ids=[nt.name for nt in NOTETYPES])
def test_a_note_warns_when_anki_refuses_to_add_it(
  notetype: Notetype, anki_state: AnkiState
):
  disagreements = [
    values
    for values in value_combinations(len(notetype.fields))
    if genanki2_state(notetype, values) != anki_state(notetype, values)
  ]
  assert disagreements == []


@pytest.mark.parametrize(
  ("notetype", "values", "anki", "genanki2"),
  [
    (BASIC_NOTETYPE, ["x", NESTED_IN_C0], None, NOTETYPE_NOT_CLOZE),
    (CLOZE_NOTETYPE, ["x", NESTED_IN_C0], None, FIELD_NOT_CLOZE),
    (CLOZE_NOTETYPE, [NESTED_IN_C0, "x"], None, None),
  ],
  ids=["basic", "cloze, outside the cloze field", "cloze, in the cloze field"],
)
def test_a_cloze_nested_in_c0_is_a_cloze_to_genanki2_only(
  notetype: Notetype,
  values: list[str],
  anki: int | None,
  genanki2: int | None,
  anki_state: AnkiState,
):
  """Anki generates a card for the nested `c1`, so the markup does show as plain text on
  a normal notetype, and outside the cloze field nothing hides it. In the cloze field
  the card works, though `fields_check()` reports `MISSING_CLOZE`."""
  assert anki_state(notetype, values) == anki
  assert genanki2_state(notetype, values) == genanki2
