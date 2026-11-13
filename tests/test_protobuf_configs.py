"""Do the config models agree with the messages they are read from and written to?

    uv run --with pytest pytest tests/test_protobuf_configs.py

Two ways of disagreeing, one per name the model gets wrong and one per field the model
never had.

Each config model is read out of a message with `from_attributes`, which looks every
attribute up by its wire name, the alias where one is set. A name the message does not
carry counts as missing rather than as an error, so a dropped alias or a typo silently
substitutes the model's default for the value the blob held.

The other direction is what a model cannot name at all. `to_bytes` writes the attributes
the model declares, so a field added to the message by an Anki newer than the vendored
descriptors is only carried by the bytes the configuration was read from. Dropping it
loses data exactly where a blob was supposed to pass through untouched, and the loss is
invisible against a collection this Anki wrote.

A round trip over real blobs does not catch that. Wherever the model's default and the
protobuf zero agree, which is every boolean here, the wrong value re-encodes to the same
bytes, and the loss shows up only against a collection that set the field. Comparing the
two name sets catches it without needing such a collection.

The nested messages are checked alongside the blobs that hold them, because they are
read through the same `from_attributes` call.

The models carry no anki dependency and neither does this file: `_proto` holds the
definitions the models are read against.
"""

from typing import Any

import pydantic as pyd
import pytest

from genanki2 import _default_rows, _proto
from genanki2.models.deck import DeckCommon, DeckFilteredKind, DeckNormalKind
from genanki2.models.deck_config import DeckConfigInner
from genanki2.models.field import FieldConfig
from genanki2.models.notetype import NotetypeConfig
from genanki2.models.template import TemplateConfig

# The descriptor side is `Any`: the C++ and pure-Python runtimes expose two unrelated
# `Descriptor` types, and a type checker rejects whichever one it was not given.
MODELS: list[tuple[type[pyd.BaseModel], Any]] = [
  (FieldConfig, _proto.FieldConfig.DESCRIPTOR),
  (TemplateConfig, _proto.TemplateConfig.DESCRIPTOR),
  (NotetypeConfig, _proto.NotetypeConfig.DESCRIPTOR),
  (
    NotetypeConfig.CardRequirement,
    _proto.NotetypeConfig.DESCRIPTOR.nested_types_by_name["CardRequirement"],
  ),
  (DeckCommon, _proto.DeckCommon.DESCRIPTOR),
  (DeckNormalKind, _proto.DeckNormal.DESCRIPTOR),
  (
    DeckNormalKind.DayLimit,
    _proto.DeckNormal.DESCRIPTOR.nested_types_by_name["DayLimit"],
  ),
  (DeckFilteredKind, _proto.DeckFiltered.DESCRIPTOR),
  (
    DeckFilteredKind.SearchTerm,
    _proto.DeckFiltered.DESCRIPTOR.nested_types_by_name["SearchTerm"],
  ),
  (DeckConfigInner, _proto.DeckConfigConfig.DESCRIPTOR),
]


def model_ids() -> list[str]:
  """Readable pytest ids, so a failure names the model instead of a row index."""
  return [model.__name__ for model, _ in MODELS]


@pytest.mark.parametrize("model, descriptor", MODELS, ids=model_ids())
def test_attributes_reach_the_message_under_its_own_names(
  model: type[pyd.BaseModel], descriptor: Any
) -> None:
  """Every attribute is spelled the way the message spells it, or carries the alias that
  spells it, and the model names every field the message has."""
  wire_names: set[str] = {
    info.alias or name for name, info in model.model_fields.items()
  }
  assert wire_names == set(descriptor.fields_by_name)


# --------------------------- Fields the models cannot name ---------------------------

# Field 100 as a varint, a number none of the three messages declares: the tag byte pair
# is (100 << 3) | 0, the wire type of a varint, and the value is 42.
UNNAMED_FIELD: bytes = b"\xa0\x06\x2a"

ConfigModel = (
  type[FieldConfig]
  | type[TemplateConfig]
  | type[NotetypeConfig]
  | type[DeckCommon]
  | type[DeckNormalKind]
  | type[DeckFilteredKind]
  | type[DeckConfigInner]
)

# The stock deck's `kind` is a `KindContainer`, so its `Normal` payload is what the model
# reads, one level in.
DEFAULT_NORMAL: bytes = _proto.DeckKindContainer.FromString(
  _default_rows.table_decks[0]["kind"]
).normal.SerializeToString()

# A fresh collection holds no filtered deck, so this is what Anki's own constructor
# builds: rescheduling on, the two stock search terms, and the preview delays it sets.
DEFAULT_FILTERED: bytes = _proto.DeckFiltered(
  reschedule=True,
  search_terms=[
    {"search": "", "limit": 100, "order": DeckFilteredKind.SearchTerm.Order.RANDOM},
    {"search": "", "limit": 20, "order": DeckFilteredKind.SearchTerm.Order.DUE},
  ],
  preview_again_secs=60,
  preview_hard_secs=600,
).SerializeToString()

# One real blob per model, with an attribute to overwrite and a value to overwrite it
# with, so the carrying can be checked on the path that rewrites the message as well.
BLOBS: list[tuple[ConfigModel, bytes, str, Any]] = [
  (FieldConfig, _default_rows.table_fields[0]["config"], "font_size", 30),
  (TemplateConfig, _default_rows.table_templates[0]["config"], "q_format", "{{Back}}"),
  (
    NotetypeConfig,
    _default_rows.table_notetypes[0]["config"],
    "css",
    "p { color: red }",
  ),
  (DeckCommon, _default_rows.table_decks[0]["common"], "last_day_studied", 7),
  (DeckNormalKind, DEFAULT_NORMAL, "description", "an imported deck"),
  (DeckFilteredKind, DEFAULT_FILTERED, "preview_delay", 30),
  (DeckConfigInner, _default_rows.table_deck_config[0]["config"], "new_per_day", 5),
]


def blob_ids() -> list[str]:
  return [model.__name__ for model, _, _, _ in BLOBS]


@pytest.mark.parametrize("model, blob, attribute, value", BLOBS, ids=blob_ids())
def test_a_field_the_model_does_not_name_survives_a_round_trip(
  model: ConfigModel, blob: bytes, attribute: str, value: Any
) -> None:
  """Byte identity is not the assertion: an unknown field is serialized after every
  known one, so it changes places with `other`, which is field 255."""
  assert UNNAMED_FIELD in model.from_bytes(blob + UNNAMED_FIELD).to_bytes()


@pytest.mark.parametrize("model, blob, attribute, value", BLOBS, ids=blob_ids())
def test_an_unnamed_field_survives_a_configuration_that_was_changed(
  model: ConfigModel, blob: bytes, attribute: str, value: Any
) -> None:
  """The path that matters: reading a package, editing it, and writing it back."""
  config = model.from_bytes(blob + UNNAMED_FIELD)
  setattr(config, attribute, value)
  written: bytes = config.to_bytes()
  assert UNNAMED_FIELD in written
  assert getattr(model.from_bytes(written), attribute) == value


@pytest.mark.parametrize("model, blob, attribute, value", BLOBS, ids=blob_ids())
def test_a_blob_the_model_names_every_field_of_is_unchanged(
  model: ConfigModel, blob: bytes, attribute: str, value: Any
) -> None:
  """Starting from the source bytes must not add to what they already hold."""
  assert model.from_bytes(blob).to_bytes() == blob


def test_the_requirements_are_replaced_rather_than_appended_to() -> None:
  """`reqs` is a repeated field, and encoding starts from a message that already holds
  the ones the blob carried, so writing the model's own has to clear them first."""
  config = NotetypeConfig.from_bytes(_default_rows.table_notetypes[0]["config"])
  assert config.requirements
  assert (
    NotetypeConfig.from_bytes(config.to_bytes()).requirements == config.requirements
  )


def test_the_search_terms_and_delays_are_replaced_rather_than_appended_to() -> None:
  """The same for a filtered deck's two repeated fields. `delays` holds plain floats
  rather than messages, so it is the one that is written a different way."""
  blob: bytes = _proto.DeckFiltered(
    search_terms=[{"search": "deck:current", "limit": 20, "order": 1}],
    delays=[1.0, 10.0],
  ).SerializeToString()
  config = DeckFilteredKind.from_bytes(blob)
  assert config.search_terms and config.delays
  assert DeckFilteredKind.from_bytes(config.to_bytes()) == config


# One case per attribute the encoding loop skips when it is `None`, which are the ones
# the source bytes would otherwise keep alive. The value is per case because a day limit
# is a message rather than a number.
OPTIONALS: list[tuple[ConfigModel, bytes, str, Any]] = [
  (FieldConfig, _default_rows.table_fields[0]["config"], "id", 5),
  (FieldConfig, _default_rows.table_fields[0]["config"], "tag", 5),
  (TemplateConfig, _default_rows.table_templates[0]["config"], "id", 5),
  (NotetypeConfig, _default_rows.table_notetypes[0]["config"], "original_id", 5),
  (DeckNormalKind, DEFAULT_NORMAL, "review_limit", 5),
  (DeckNormalKind, DEFAULT_NORMAL, "new_limit", 5),
  (DeckNormalKind, DEFAULT_NORMAL, "desired_retention", 0.75),
  (
    DeckNormalKind,
    DEFAULT_NORMAL,
    "review_limit_today",
    DeckNormalKind.DayLimit(limit=5, today=3),
  ),
  (
    DeckNormalKind,
    DEFAULT_NORMAL,
    "new_limit_today",
    DeckNormalKind.DayLimit(limit=5, today=3),
  ),
]


@pytest.mark.parametrize(
  "model, blob, attribute, value",
  OPTIONALS,
  ids=[f"{model.__name__}.{attribute}" for model, _, attribute, _ in OPTIONALS],
)
def test_an_optional_can_still_be_unset(
  model: ConfigModel, blob: bytes, attribute: str, value: Any
) -> None:
  """Unsetting one has to reach the blob, not just the model it was set on."""
  config = model.from_bytes(blob)
  setattr(config, attribute, value)
  config = model.from_bytes(config.to_bytes())
  assert getattr(config, attribute) == value
  setattr(config, attribute, None)
  assert getattr(model.from_bytes(config.to_bytes()), attribute) is None
