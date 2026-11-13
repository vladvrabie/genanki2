"""Does writing back a read collection warn when a notetype's shape has changed?

    uv run --with pytest pytest tests/test_read_notetype_schema.py

Anki updates a notetype it already holds only while the incoming field and template
lists match its own: same lengths, and each item matching the one at the same position
by id, or by name where either side has no id (`equal_schema` and `is_match` in
`rslib/src/import_export/package/apkg/import/notes.rs` and `notetype/merge.rs`).
Otherwise, with "Merge notetypes" off, the notetype is added as a copy and every note
the user already has is logged as conflicting and skipped. The importing user sees only
that the note type changed, so the warning has to reach the deck's author at write time.
"""

import warnings
from pathlib import Path

import pytest

from genanki2.builtins import DEFAULT_DECK, DEFAULT_DECK_CONFIG, builtin_notetype
from genanki2.errors import Genanki2Warning
from genanki2.models.collection import Collection
from genanki2.models.config import ConfigEntry
from genanki2.models.field import Field
from genanki2.models.notetype import Notetype
from genanki2.models.package import CollectionPackage, Package
from genanki2.models.template import Template, TemplateConfig

SCHEDULER_V2 = [ConfigEntry(key="schedVer", value=b"2")]
NOTETYPE_ID = 1_700_000_000_000
MATCH = "different field or template list"


def a_notetype(name: str = "Basic (genanki2)") -> Notetype:
  return builtin_notetype(name, id=NOTETYPE_ID)


def a_collection(notetype: Notetype) -> Collection:
  return Collection(
    deck_configs=[DEFAULT_DECK_CONFIG],
    decks=[DEFAULT_DECK],
    notetypes=[notetype],
    config=SCHEDULER_V2,
  )


def read_back(tmp_path: Path, notetype: Notetype) -> Collection:
  """Write a collection holding `notetype` and read it back, as last release's file."""
  path = tmp_path / "collection.anki2"
  with warnings.catch_warnings():
    warnings.simplefilter("ignore", Genanki2Warning)
    a_collection(notetype).write_sqlite(path)
    return Collection.read_sqlite(path)


def no_warning(collection: Collection) -> None:
  with warnings.catch_warnings():
    warnings.simplefilter("error", Genanki2Warning)
    collection.warn_read_notetype_schemas_mismatch()


def test_an_unchanged_notetype_does_not_warn(tmp_path):
  no_warning(read_back(tmp_path, a_notetype()))


def test_an_appended_field_warns_with_both_lists(tmp_path):
  collection = read_back(tmp_path, a_notetype())
  collection.notetypes[0].fields.append(Field(name="Audio"))

  with pytest.warns(Genanki2Warning, match=MATCH) as record:
    collection.warn_read_notetype_schemas_mismatch()

  message = str(record[0].message)
  assert f"'Basic (genanki2)' (id {NOTETYPE_ID})" in message
  assert "fields: ['Front', 'Back'] -> ['Front', 'Back', 'Audio']" in message
  assert "templates:" not in message


def test_a_removed_template_warns(tmp_path):
  collection = read_back(tmp_path, a_notetype("Basic (and reversed card) (genanki2)"))
  collection.notetypes[0].templates.pop()

  with pytest.warns(Genanki2Warning, match=MATCH) as record:
    collection.warn_read_notetype_schemas_mismatch()

  assert "templates: ['Card 1', 'Card 2'] -> ['Card 1']" in str(record[0].message)


def test_an_appended_template_warns(tmp_path):
  collection = read_back(tmp_path, a_notetype())
  collection.notetypes[0].templates.append(
    Template(
      name="Card 2", config=TemplateConfig(q_format="{{Back}}", a_format="{{Front}}")
    )
  )

  with pytest.warns(Genanki2Warning, match=MATCH):
    collection.warn_read_notetype_schemas_mismatch()


def test_reordered_fields_warn(tmp_path):
  """Same length, but each position now holds the other field."""
  collection = read_back(tmp_path, a_notetype())
  collection.notetypes[0].fields.reverse()

  with pytest.warns(Genanki2Warning, match=MATCH):
    collection.warn_read_notetype_schemas_mismatch()


def test_a_renamed_field_keeping_its_id_does_not_warn(tmp_path):
  """Both sides carry the id, so Anki matches by id and the rename updates."""
  collection = read_back(tmp_path, a_notetype().fill_missing_ids())
  collection.notetypes[0].fields[0].name = "Question"

  no_warning(collection)


def test_a_renamed_field_without_an_id_warns(tmp_path):
  """With no id Anki falls back to the name, and the new name is a different field."""
  collection = read_back(tmp_path, a_notetype())
  collection.notetypes[0].fields[0].name = "Question"

  with pytest.warns(Genanki2Warning, match=MATCH):
    collection.warn_read_notetype_schemas_mismatch()


def test_a_notetype_replaced_under_the_same_id_is_compared(tmp_path):
  """The read lists are kept by notetype id, so swapping the object does not hide the
  change: Anki matches by id too."""
  collection = read_back(tmp_path, a_notetype())
  replacement = a_notetype()
  replacement.fields.append(Field(name="Audio"))
  collection.notetypes = [replacement]

  with pytest.warns(Genanki2Warning, match=MATCH):
    collection.warn_read_notetype_schemas_mismatch()


def test_a_collection_built_in_code_is_not_compared():
  """It was never read, so there is nothing to compare against."""
  notetype = a_notetype()
  notetype.fields.append(Field(name="Audio"))

  no_warning(a_collection(notetype))


@pytest.mark.parametrize(
  ("write", "expected"),
  [
    pytest.param(
      lambda col, d: Package(collection=col).write_apkg(d / "deck.apkg"),
      "An apkg import adds it as a new copy",
      id="apkg",
    ),
    pytest.param(
      lambda col, d: CollectionPackage(collection=col).write_colpkg(
        d / "collection.colpkg"
      ),
      None,
      id="colpkg",
    ),
    pytest.param(
      lambda col, d: col.write_sqlite(d / "rewritten.anki2"),
      "If imported from an apkg, Anki adds it as a new copy",
      id="sqlite",
    ),
    pytest.param(
      lambda col, d: col.warn(),
      "If imported from an apkg, Anki adds it as a new copy",
      id="no context",
    ),
  ],
)
def test_a_changed_schema_is_reported_for_what_the_write_produces(
  tmp_path, write, expected
):
  """A restore replaces the user's notetypes outright, so there is no copy to make."""
  collection = read_back(tmp_path, a_notetype())
  collection.notetypes[0].fields.append(Field(name="Audio"))
  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always", Genanki2Warning)
    write(collection, tmp_path)

  messages = [str(w.message) for w in caught if MATCH in str(w.message)]
  if expected is None:
    assert messages == []
  else:
    assert len(messages) == 1
    assert expected in messages[0]


def test_writing_a_changed_collection_warns(tmp_path):
  collection = read_back(tmp_path, a_notetype())
  collection.notetypes[0].fields.append(Field(name="Audio"))

  with pytest.warns(Genanki2Warning, match=MATCH):
    collection.write_sqlite(tmp_path / "rewritten.anki2")


def test_the_read_lists_do_not_count_towards_equality(tmp_path):
  """They are what the read left behind, not content."""
  collection = read_back(tmp_path, a_notetype())
  copy = collection.model_copy(deep=True)
  copy._read_notetypes = type(collection._read_notetypes)()

  assert collection == copy
