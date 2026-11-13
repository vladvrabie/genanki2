"""Do `Notetype`, `Field` and `Template` survive a trip through the row layer?

    uv run --with pytest pytest tests/test_notetype_models.py

The models are the writer's only description of a notetype, so a column dropped or
renamed on the way to a row is not caught by the schema (SQLite accepts any subset of
columns on INSERT) and surfaces much later as a notetype Anki reads with a missing field.
`_default_rows` is a real empty collection dumped by Anki, so a round trip through it
compares against rows Anki itself wrote rather than against rows written here.

A notetype spans three tables, and `ntid` and `ord` only exist in the relationship between
them, so the round trip runs at the `from_rows`/`to_rows` level. A `Field` on its own
carries neither and cannot be checked against a row alone.

The models carry no anki dependency and neither does this file.
"""

import re
import warnings

import pydantic as pyd
import pytest

from genanki2 import _default_rows
from genanki2.builtins import (
  BASIC_NOTETYPE,
  BUILTIN_NOTETYPES,
  CLOZE_NOTETYPE,
  DEFAULT_DECK,
  IMAGE_OCCLUSION_NOTETYPE,
  builtin_notetype,
)
from genanki2.errors import CollectionReadError, Genanki2Warning
from genanki2.models.card import Card
from genanki2.models.collection import Collection
from genanki2.models.config import ConfigEntry
from genanki2.models.field import Field
from genanki2.models.note import Note
from genanki2.models.notetype import Notetype
from genanki2.models.package import CollectionPackage, Package
from genanki2.models.template import Template

NOTETYPE_ROWS = _default_rows.table_notetypes
FIELD_ROWS = _default_rows.table_fields
TEMPLATE_ROWS = _default_rows.table_templates


def rows_for(rows, notetype_id: int) -> list[dict]:
  return [row for row in rows if row["ntid"] == notetype_id]


def as_lists(triple) -> tuple[dict, list[dict], list[dict]]:
  """`Rows` is a `Sequence`, so a round trip is about the rows, not their container."""
  row, field_rows, template_rows = triple
  return row, list(field_rows), list(template_rows)


def builtin_ids() -> list[str]:
  """Readable pytest ids, so a failure names the notetype instead of a row index."""
  return [f"{row['id']}-{row['name']}" for row in NOTETYPE_ROWS]


@pytest.fixture(params=NOTETYPE_ROWS, ids=builtin_ids())
def builtin(request):
  """One of Anki's six stock notetypes, as the three row lists that describe it."""
  row = request.param
  return row, rows_for(FIELD_ROWS, row["id"]), rows_for(TEMPLATE_ROWS, row["id"])


# --------------------------------- Row round trips ---------------------------------


def test_rows_survive_a_round_trip(builtin):
  assert as_lists(Notetype.from_rows(*builtin).to_rows()) == as_lists(builtin)


def test_the_notetype_row_alone_survives_a_round_trip(builtin):
  """`from_row` is the path for a notetype whose fields are not being read."""
  row, _, _ = builtin
  assert Notetype.from_row(row).to_row() == row


def test_reading_rows_does_not_write_to_them(builtin):
  """`_default_rows` hands out live dicts, so a row stashed on the way in would poison
  every later reader, starting with the writer's own default rows."""
  row, field_rows, template_rows = builtin
  before = {"nt": dict(row), "f": [dict(r) for r in field_rows]}
  Notetype.from_rows(row, field_rows, template_rows)
  assert row == before["nt"]
  assert [dict(r) for r in field_rows] == before["f"]


def test_notetype_row_keeps_its_own_id():
  """`to_row` takes no id, so the row can only disagree with the model by losing it."""
  assert Notetype(id=1234, name="N", config=b"").to_row()["id"] == 1234


def test_the_notetype_row_alone_does_not_need_fields_or_templates():
  """`to_row` writes one table, so completeness is not its business: it belongs to
  `to_rows`, which writes all three. This is what lets `from_row` hand back a notetype
  whose fields were never read and have it written straight back out."""
  notetype = Notetype(name="N", config=b"")
  assert notetype.to_row()["name"] == "N"
  with pytest.raises(ValueError, match="no fields"):
    notetype.to_rows()


# ------------------------------------ Ordinals -------------------------------------


def test_rows_out_of_order_are_sorted_on_the_way_in(builtin):
  """`ord` is a column, not an attribute, so the order it encodes has to become list
  order at the boundary or it is lost."""
  row, field_rows, template_rows = builtin
  shuffled = Notetype.from_rows(row, field_rows[::-1], template_rows[::-1])
  assert as_lists(shuffled.to_rows()) == as_lists(builtin)


def test_fields_are_listed_in_the_order_their_ordinals_gave(builtin):
  """The list is the order, so the column has to become list position on the way in."""
  notetype = Notetype.from_rows(*builtin)
  _, field_rows, _ = builtin
  assert [field.name for field in notetype.fields] == [
    row["name"] for row in field_rows
  ]


@pytest.mark.parametrize("attribute", ["fields", "templates"])
def test_ordinals_follow_list_position(builtin, attribute):
  """Reordering the list is the only way to reorder fields or templates, and it needs no
  other step. Ordinals are not stored, so nothing else has to be kept in step with the
  list, and nothing else can disagree with it."""
  notetype = Notetype.from_rows(*builtin)
  objects = getattr(notetype, attribute)
  objects.reverse()
  rows = notetype.to_rows()[1 if attribute == "fields" else 2]
  assert [row["ord"] for row in rows] == list(range(len(rows)))
  assert [row["name"] for row in rows] == [obj.name for obj in objects]


@pytest.mark.parametrize("attribute", ["fields", "templates"])
def test_the_owning_id_comes_from_the_notetype(builtin, attribute):
  """`Field.to_row` and `Template.to_row` take `ntid` as an argument and check nothing:
  the table declares no foreign key, so a wrong one writes rows no notetype owns. What
  makes that safe is `Notetype.id` being constrained and `to_rows` passing it down, so
  this is the half of that argument a constraint cannot cover.
  """
  notetype = Notetype.from_rows(*builtin)
  notetype.id = 1234
  rows = notetype.to_rows()[1 if attribute == "fields" else 2]
  assert rows
  assert {row["ntid"] for row in rows} == {1234}


@pytest.mark.parametrize("table", [1, 2], ids=["fields", "templates"])
@pytest.mark.parametrize(
  "reorder",
  [
    lambda rows: [row | {"ord": row["ord"] + 1} for row in rows],
    lambda rows: [rows[0], *rows],
  ],
  ids=["gap", "repeat"],
)
def test_rows_whose_ordinals_do_not_run_from_zero_are_rejected(builtin, table, reorder):
  """A gap or a repeat misreads every note of the notetype, since a field's ordinal is
  its position in the note's values and a template's names the cards it renders."""
  rows = list(builtin)
  rows[table] = reorder(rows[table])
  with pytest.raises(CollectionReadError, match="contiguous ordinals"):
    Notetype.from_rows(*rows)


@pytest.mark.parametrize("table", [1, 2], ids=["fields", "templates"])
def test_rows_without_ordinals_are_rejected(builtin, table):
  rows = list(builtin)
  rows[table] = [{k: v for k, v in row.items() if k != "ord"} for row in rows[table]]
  with pytest.raises(CollectionReadError, match="missing ordinals"):
    Notetype.from_rows(*rows)


def test_rows_some_without_an_ordinal_are_rejected(builtin):
  row, field_rows, template_rows = builtin
  field_rows = [{k: v for k, v in field_rows[0].items() if k != "ord"}, *field_rows[1:]]
  with pytest.raises(CollectionReadError, match="not all fields"):
    Notetype.from_rows(row, field_rows, template_rows)


# ------------------------------------ Ownership ------------------------------------


@pytest.mark.parametrize("table", [1, 2], ids=["fields", "templates"])
def test_rows_belonging_to_another_notetype_are_rejected(builtin, table):
  """`Field` and `Template` drop `ntid`, and `to_rows` writes the notetype's own id in
  its place, so accepting a stray row would move it onto this notetype."""
  rows = list(builtin)
  rows[table] = [rows[table][0] | {"ntid": rows[0]["id"] + 1}, *rows[table][1:]]
  with pytest.raises(CollectionReadError, match="disagree with the notetype id"):
    Notetype.from_rows(*rows)


# ---------------------------------- Corrupt blobs ----------------------------------


@pytest.mark.parametrize("table", [0, 1, 2], ids=["notetype", "fields", "templates"])
def test_a_config_blob_that_is_not_protobuf_names_the_column(builtin, table):
  """protobuf's `DecodeError` is not a `ValueError`, so pydantic would let it escape raw
  and lose the location. Converted, the error names the column it was read from."""
  tables = [[builtin[0]], list(builtin[1]), list(builtin[2])]
  tables[table][0] = tables[table][0] | {"config": b"\xff\xff\xff"}
  with pytest.raises(pyd.ValidationError, match="can't be decoded") as error:
    Notetype.from_rows(tables[0][0], tables[1], tables[2])
  assert error.value.errors()[0]["loc"][-1] == "config"


# ------------------------------- The write-time gate -------------------------------


@pytest.mark.parametrize(
  ("fields", "templates"),
  [
    ([], []),
    ([Field(name="Front", config=b"")], []),
    ([], [Template(name="Card 1", config=b"")]),
  ],
  ids=["neither", "no-templates", "no-fields"],
)
def test_a_notetype_missing_fields_or_templates_cannot_be_written(fields, templates):
  """Anki refuses both (rslib/src/notetype/mod.rs:484-485). No template means no cards,
  and no field means `flds`, `sfld` and `csum` are all meaningless."""
  notetype = Notetype(name="N", config=b"", fields=fields, templates=templates)
  with pytest.raises(ValueError):
    notetype.to_rows()


@pytest.mark.parametrize("second_name", ["Front", "front", "FRONT"])
def test_duplicate_field_names_are_rejected(second_name):
  """`idx_fields_name_ntid` is UNIQUE and inherits `COLLATE unicase`, so a case variant
  is a duplicate, and without this check it arrives as an IntegrityError naming nothing."""
  notetype = Notetype(
    name="N",
    config=b"",
    fields=[Field(name="Front", config=b""), Field(name=second_name, config=b"")],
    templates=[Template(name="Card 1", config=b"")],
  )
  with pytest.raises(ValueError):
    notetype.to_rows()


@pytest.mark.parametrize("second_name", ["Card 1", "card 1"])
def test_duplicate_template_names_are_rejected(second_name):
  notetype = Notetype(
    name="N",
    config=b"",
    fields=[Field(name="Front", config=b"")],
    templates=[
      Template(name="Card 1", config=b""),
      Template(name=second_name, config=b""),
    ],
  )
  with pytest.raises(ValueError):
    notetype.to_rows()


# ----------------------------------- Field lookup -----------------------------------


def test_a_field_is_found_by_name_the_way_sqlite_matches_it():
  """The uniqueness rule is unicase, so lookup has to fold case or it misses a name
  SQLite already considers taken."""
  notetype = Notetype(
    name="N", config=b"", fields=[Field(name="Front", config=b"")], templates=[]
  )
  assert notetype.field_index("Front") == 0
  assert notetype.field_index("front") == 0


@pytest.mark.parametrize("name", ["#Front", " Front ", 'Fr"ont', "Front:"])
def test_a_name_is_repaired_before_the_lookup(name):
  """A field repairs its own name on the way in, so a lookup that skipped the same repair
  would miss the field the caller is naming. Every form here is stored as `Front`."""
  notetype = Notetype(
    name="N", config=b"", fields=[Field(name="Front", config=b"")], templates=[]
  )
  assert notetype.field_index(name) == 0


def test_an_unknown_field_name_lists_the_names_the_notetype_has():
  """The names are the only thing the caller can act on: the lookup folds case and
  repairs the name, so a rejection is not self-explanatory."""
  notetype = Notetype(
    name="N",
    config=b"",
    fields=[Field(name="Front", config=b""), Field(name="Back", config=b"")],
    templates=[],
  )
  with pytest.raises(ValueError, match=r'"Front", "Back"'):
    notetype.field_index("Nope")


@pytest.mark.parametrize("name", ["Nope", "#"], ids=["unknown", "repairs-to-nothing"])
@pytest.mark.parametrize("field_names", [[], ["Front"]], ids=["no-fields", "one-field"])
def test_a_lookup_raises_the_same_type_however_it_fails(field_names, name):
  """A name that repairs to nothing is rejected by the repair, an unknown one by the
  search, and a notetype with no fields never reaches the search at all. All of them are
  `ValueError`, so a caller needs one `except`, whatever the notetype holds."""
  notetype = Notetype(
    name="N",
    config=b"",
    fields=[Field(name=field_name, config=b"") for field_name in field_names],
    templates=[],
  )
  with pytest.raises(ValueError):
    notetype.field_index(name)


# ------------------------------------ Defaults -------------------------------------


def test_ids_are_generated_per_instance():
  """A `default_factory`, not a default: an id shared by every notetype would collide."""
  assert Notetype(name="A", config=b"").id != Notetype(name="B", config=b"").id


@pytest.mark.parametrize("model", [Notetype, Template])
def test_a_fresh_model_is_unsynced(model):
  """-1 is Anki's "never synced" marker; 0 would claim the row is in sync with a server."""
  assert model(name="N", config=b"").update_sequence_n == -1


@pytest.mark.parametrize("model", [Notetype, Template])
def test_sync_columns_are_settable_by_name_and_by_alias(model):
  """Without `validate_by_name` the attribute name is an unknown key, silently dropped in
  favour of the default, so a caller not holding a row loses the value with no error."""
  assert model(name="N", config=b"", modification_time_s=7).modification_time_s == 7
  assert model(name="N", config=b"", mtime_secs=7).modification_time_s == 7


@pytest.mark.parametrize("model", [Field, Template, Notetype])
def test_an_empty_name_is_rejected(model):
  """`fields.name`, `templates.name` and `notetypes.name` are all NOT NULL and indexed,
  and Anki rejects an empty notetype name outright (rslib/src/notetype/mod.rs:490)."""
  with pytest.raises(ValueError):
    model(name="", config=b"")


# -------------------------------- The deck override ---------------------------------


def a_template(target_deck_id: int) -> Template:
  template = Template(name="C1", config=b"")
  template.config.target_deck_id = target_deck_id
  return template


def test_a_deck_override_is_reported():
  """The importer gives every incoming deck the id of the target's deck with the same
  name, or a new one (`add_deck` and `update_deck`,
  rslib/src/import_export/package/apkg/import/decks.rs:126-146), and never rewrites the
  template's copy of one, so the override arrives naming a deck that is not the one it
  named."""
  with pytest.warns(UserWarning, match="target_deck_id"):
    a_template(999).warn_misleading_target_deck_id()


def a_collection_with_a_deck_override() -> Collection:
  """A Basic note in the default deck, whose notetype's template overrides the deck.

  The scheduler entry keeps a colpkg write from being refused for the scheduler."""
  notetype = builtin_notetype(BASIC_NOTETYPE.name, id=1_700_000_000_000)
  notetype.templates[0].config.target_deck_id = 999
  note = Note(notetype=notetype, fields=["front", "back"])
  return Collection(
    config=[ConfigEntry(key="schedVer", value=b"2")],
    notes=[note],
    cards=[Card(note=note, deck=DEFAULT_DECK)],
  )


APKG_MESSAGE = "An apkg import renumbers every deck"
GENERIC_MESSAGE = "If imported from an apkg, it references a deck the user does not"


@pytest.mark.parametrize(
  ("write", "expected"),
  [
    pytest.param(
      lambda col, d: Package(collection=col).write_apkg(d / "deck.apkg"),
      APKG_MESSAGE,
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
      lambda col, d: col.write_sqlite(d / "collection.anki2"),
      GENERIC_MESSAGE,
      id="sqlite",
    ),
    pytest.param(
      lambda col, d: col.notes[0].notetype.to_rows(),
      GENERIC_MESSAGE,
      id="row",
    ),
    pytest.param(
      lambda col, d: col.notes[0].notetype.templates[0].warn(),
      GENERIC_MESSAGE,
      id="no context",
    ),
  ],
)
def test_a_deck_override_is_reported_for_what_the_write_produces(
  tmp_path, write, expected
):
  """A restore keeps every deck id, so the override still names its deck. Through the
  writers, the context has to cross the collection, the note and the notetype."""
  collection = a_collection_with_a_deck_override()
  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always", Genanki2Warning)
    write(collection, tmp_path)

  messages = [
    str(w.message) for w in caught if "sets a deck override" in str(w.message)
  ]
  if expected is None:
    assert messages == []
  else:
    assert len(messages) == 1
    assert expected in messages[0]
    assert "Template.repair_target_deck_id()" in messages[0]


@pytest.mark.parametrize("target_deck_id", [0, -1], ids=["zero", "negative"])
def test_no_deck_override_is_nothing_to_report(target_deck_id):
  """Anki reads the override only when it is positive, so neither value points anywhere.
  0 is what an ordinary template carries."""
  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    a_template(target_deck_id).warn_misleading_target_deck_id()
  assert caught == []


def test_an_override_on_the_default_deck_is_nothing_to_report():
  """Every collection has a default deck at id 1, and an incoming one matches it by name
  and keeps that id, so this is the one override that survives the trip."""
  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    a_template(1).warn_misleading_target_deck_id()
  assert caught == []


def test_the_repair_clears_an_override_an_import_would_strand():
  assert a_template(999).repair_target_deck_id().config.target_deck_id == 0


@pytest.mark.parametrize(
  "target_deck_id", [0, -1, 1], ids=["zero", "negative", "default"]
)
def test_the_repair_leaves_alone_what_the_report_leaves_alone(target_deck_id):
  """Clearing a value the report accepts would change data the round trip has to hand
  back unchanged."""
  template = a_template(target_deck_id).repair_target_deck_id()
  assert template.config.target_deck_id == target_deck_id


# ---------------------------------- Unused fields ----------------------------------


def unused_field_warnings(notetype: Notetype) -> list[str]:
  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    notetype.warn_unused_fields()
  return [str(warning.message) for warning in caught]


def without_references(notetype: Notetype, name: str) -> Notetype:
  """Drop every `{{name}}`, `{{#name}}` and `{{/name}}` from both sides of every
  template, since a conditional counts as a use."""
  pattern = re.compile(r"\{\{[#/]?" + re.escape(name) + r"\}\}")
  for template in notetype.templates:
    template.config.q_format = pattern.sub("", template.config.q_format)
    template.config.a_format = pattern.sub("", template.config.a_format)
  return notetype


def test_a_field_no_template_references_is_reported():
  notetype = builtin_notetype(BASIC_NOTETYPE.name)
  notetype.fields.append(Field(name="Notes"))
  assert unused_field_warnings(notetype) == [
    "Notetype: \"Basic (genanki2)\" has unused fields: ('Notes',)"
  ]


def test_the_sort_field_is_not_reported():
  """The browser's Sort Field column shows it, so it is in use without a template."""
  notetype = builtin_notetype(BASIC_NOTETYPE.name)
  notetype.fields.append(Field(name="Notes"))
  notetype.set_sort_field("Notes")
  assert unused_field_warnings(notetype) == []


@pytest.mark.parametrize("notetype", BUILTIN_NOTETYPES, ids=lambda nt: nt.name)
def test_no_builtin_is_reported(notetype: Notetype):
  """Image Occlusion's Comments field is in no template by design
  (rslib/src/image_occlusion/notetype.rs:88-90), so it is exempt."""
  assert unused_field_warnings(notetype) == []


def test_the_image_occlusion_comments_field_is_exempt_after_a_rename():
  """It is matched by the tag Anki gives it, not by its name."""
  notetype = builtin_notetype(IMAGE_OCCLUSION_NOTETYPE.name)
  notetype.fields[4].name = "My notes"
  assert unused_field_warnings(notetype) == []


def test_another_image_occlusion_field_is_reported():
  notetype = without_references(
    builtin_notetype(IMAGE_OCCLUSION_NOTETYPE.name), "Header"
  )
  assert unused_field_warnings(notetype) == [
    "Notetype: \"Image Occlusion (genanki2)\" has unused fields: ('Header',)"
  ]


def test_a_tagged_cloze_field_is_reported():
  """Anki tags both cloze fields too, and only the Image Occlusion Comments field is
  exempt, so a copy that drops Back Extra from its template is told."""
  notetype = without_references(builtin_notetype(CLOZE_NOTETYPE.name), "Back Extra")
  assert unused_field_warnings(notetype) == [
    "Notetype: \"Cloze (genanki2)\" has unused fields: ('Back Extra',)"
  ]
