import importlib.metadata

from .api import (
  deck,
  deck_config,
  field,
  filtered_deck,
  note,
  notetype,
  package,
  template,
)
from .builtins import (
  BASIC_AND_REVERSED_NOTETYPE,
  BASIC_NOTETYPE,
  BASIC_OPTIONAL_REVERSED_NOTETYPE,
  BASIC_TYPE_IN_THE_ANSWER_NOTETYPE,
  BUILTIN_NOTETYPES,
  CLOZE_NOTETYPE,
  DEFAULT_DECK,
  DEFAULT_DECK_CONFIG,
  IMAGE_OCCLUSION_NOTETYPE,
  builtin_notetype,
  default_collection_metadata,
  default_config_entries,
  default_deck,
  default_deck_config,
)
from .errors import (
  CollectionReadError,
  Genanki2Error,
  Genanki2Warning,
  PackageReadError,
)
from .idgen import guid, guid_for, monotonic_id
from .models.card import Card, CardData, CardFlag, CardQueue, CardType
from .models.collection import Collection
from .models.collection_metadata import CollectionMetadata
from .models.config import ConfigEntry
from .models.deck import Deck, DeckCommon, DeckFilteredKind, DeckNormalKind
from .models.deck_config import DeckConfig, DeckConfigInner
from .models.field import Field, FieldConfig
from .models.grave import Grave, GraveType
from .models.media_entries import MediaEntries, MediaEntry
from .models.note import Note
from .models.notetype import Notetype, NotetypeConfig
from .models.package import CollectionPackage, Package
from .models.package_metadata import PackageMetadata
from .models.review import Review, ReviewRating, ReviewType
from .models.tag import Tag
from .models.template import Template, TemplateConfig
from .settings import (
  checks_disabled,
  checks_enabled,
  dbcheck_repairs_disabled,
  dbcheck_repairs_enabled,
  timestamping_disabled,
  timestamping_enabled,
  warnings_disabled,
  warnings_enabled,
)

__version__: str = importlib.metadata.version("genanki2")

__all__ = [
  "BASIC_AND_REVERSED_NOTETYPE",
  "BASIC_NOTETYPE",
  "BASIC_OPTIONAL_REVERSED_NOTETYPE",
  "BASIC_TYPE_IN_THE_ANSWER_NOTETYPE",
  "BUILTIN_NOTETYPES",
  "CLOZE_NOTETYPE",
  "DEFAULT_DECK",
  "DEFAULT_DECK_CONFIG",
  "IMAGE_OCCLUSION_NOTETYPE",
  "Card",
  "CardData",
  "CardFlag",
  "CardQueue",
  "CardType",
  "Collection",
  "CollectionMetadata",
  "CollectionPackage",
  "CollectionReadError",
  "ConfigEntry",
  "Deck",
  "DeckCommon",
  "DeckConfig",
  "DeckConfigInner",
  "DeckFilteredKind",
  "DeckNormalKind",
  "Field",
  "FieldConfig",
  "Genanki2Error",
  "Genanki2Warning",
  "Grave",
  "GraveType",
  "MediaEntries",
  "MediaEntry",
  "Note",
  "Notetype",
  "NotetypeConfig",
  "Package",
  "PackageMetadata",
  "PackageReadError",
  "Review",
  "ReviewRating",
  "ReviewType",
  "Tag",
  "Template",
  "TemplateConfig",
  "__version__",
  "builtin_notetype",
  "checks_disabled",
  "checks_enabled",
  "dbcheck_repairs_disabled",
  "dbcheck_repairs_enabled",
  "deck",
  "deck_config",
  "default_collection_metadata",
  "default_config_entries",
  "default_deck",
  "default_deck_config",
  "field",
  "filtered_deck",
  "guid",
  "guid_for",
  "monotonic_id",
  "note",
  "notetype",
  "package",
  "template",
  "timestamping_disabled",
  "timestamping_enabled",
  "warnings_disabled",
  "warnings_enabled",
]
