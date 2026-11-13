"""Anki's protobuf definitions, as message classes.

A v18 collection stores several columns as serialized protobuf: the three notetype
configurations, a deck's `common` and `kind`, and a deck preset's `config`. A package
adds two more, the `meta` entry and the media map.

The definitions come from `descriptors.binpb`, written by
scripts/dump_proto_descriptors.py, and are loaded into a private descriptor pool.
The pool is private because protobuf's default one is process-wide and keyed by
proto file name: a second registration of `anki/notetypes.proto` is aliased to the
first, so with the `anki` package installed as a test oracle, whichever import ran
first would decide which definitions everything else sees.

The definitions are Anki's, Copyright Ankitects Pty Ltd and contributors, licensed
under the GNU AGPL, version 3 or later.
"""

from importlib import resources

from google.protobuf.descriptor import Descriptor
from google.protobuf.descriptor_pb2 import FileDescriptorSet
from google.protobuf.descriptor_pool import DescriptorPool
from google.protobuf.message import Message
from google.protobuf.message_factory import GetMessageClass

__all__ = [
  "CardRequirement",
  "DayLimit",
  "DeckCommon",
  "DeckConfigConfig",
  "DeckFiltered",
  "DeckKindContainer",
  "DeckNormal",
  "FieldConfig",
  "MediaEntries",
  "MediaEntry",
  "NotetypeConfig",
  "PackageMetadata",
  "SearchTerm",
  "TemplateConfig",
]


def _descriptor_set() -> FileDescriptorSet:
  """Parse the vendored `descriptors.binpb`."""
  descriptors_path = resources.files(__name__).joinpath("descriptors.binpb")
  return FileDescriptorSet.FromString(descriptors_path.read_bytes())


def _pool() -> DescriptorPool:
  """Return a descriptor pool holding the vendored definitions.

  Files are added in the order the dump wrote them, which is imports first, because a
  pool refuses a file whose imports it has not been given yet.
  """
  pool: DescriptorPool = DescriptorPool()
  for file_descriptor in _descriptor_set().file:
    pool.Add(file_descriptor)
  return pool


def _set_module(descriptor: Descriptor) -> None:
  """Set `__module__` on the Message type and on any nested type.

  Pydantic `BaseModel.model_validate(..., from_attributes=True)` fails without
  `__module__` set.
  """
  GetMessageClass(descriptor).__module__ = __name__
  for nested in descriptor.nested_types:
    _set_module(nested)


def _message(name: str) -> type[Message]:
  """Return the message class registered under `name`."""
  message = GetMessageClass(_POOL.FindMessageTypeByName(name))
  _set_module(message.DESCRIPTOR)  # pyright: ignore[reportArgumentType]
  return message


_POOL = _pool()
FieldConfig = _message("anki.notetypes.Notetype.Field.Config")
TemplateConfig = _message("anki.notetypes.Notetype.Template.Config")
NotetypeConfig = _message("anki.notetypes.Notetype.Config")
CardRequirement = _message("anki.notetypes.Notetype.Config.CardRequirement")
DeckConfigConfig = _message("anki.deck_config.DeckConfig.Config")
DeckCommon = _message("anki.decks.Deck.Common")
DeckKindContainer = _message("anki.decks.Deck.KindContainer")
DeckNormal = _message("anki.decks.Deck.Normal")
DayLimit = _message("anki.decks.Deck.Normal.DayLimit")
DeckFiltered = _message("anki.decks.Deck.Filtered")
SearchTerm = _message("anki.decks.Deck.Filtered.SearchTerm")
MediaEntries = _message("anki.import_export.MediaEntries")
MediaEntry = _message("anki.import_export.MediaEntries.MediaEntry")
PackageMetadata = _message("anki.import_export.PackageMetadata")
