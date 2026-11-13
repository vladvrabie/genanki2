import dataclasses as dc
import itertools as it
import operator
import shutil
import sqlite3
import sys
import tempfile
import warnings
import zipfile
from collections.abc import Callable, Iterable
from collections.abc import Set as AbstractSet
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple, TypeVar

import pydantic as pyd
from frozendict import frozendict
from typing_extensions import Self, override

from genanki2._typed_schema import (
  CardRow,
  FieldRow,
  FieldRows,
  NoteRow,
  RevlogRow,
  TemplateRow,
  TemplateRows,
)
from genanki2.builtins import (
  BUILTIN_NOTETYPES,
  DEFAULT_DECK,
  DEFAULT_DECK_CONFIG,
  default_config_entries,
)
from genanki2.datetime import days_elapsed, utc_offset_minutes_west
from genanki2.errors import (
  CollectionReadError,
  Genanki2Error,
  Genanki2Warning,
  PackageReadError,
)
from genanki2.io_utils import (
  atomic_replace,
  open_apkg,
  prepare_reading_file,
  prepare_writing_file,
  zstd_decompress,
)
from genanki2.models._card_maker import CardMaker
from genanki2.models.base import CheckedModel
from genanki2.models.card import Card, CardType
from genanki2.models.collection_metadata import CollectionMetadata
from genanki2.models.config import ConfigEntry
from genanki2.models.deck import Deck, DeckNormalKind
from genanki2.models.deck_config import DeckConfig
from genanki2.models.grave import Grave
from genanki2.models.note import Note
from genanki2.models.notetype import Notetype
from genanki2.models.package_metadata import PackageMetadata
from genanki2.models.review import Review
from genanki2.models.serialization_context import SerializationContext
from genanki2.models.tag import Tag
from genanki2.sqlite import AnkiSQLite
from genanki2.text import (
  DECK_HUMAN_SEPARATOR,
  normalize_deck_name,
  normalize_notetype_name,
  tag_spelling_under_registered_ancestor,
)
from genanki2.time import time_ms
from genanki2.utils import duplicates, is_uint32

if sys.version_info >= (3, 14):
  from compression import zstd  # pyright: ignore[reportUnreachable]
else:
  import zstandard as zstd

SourceT = TypeVar("SourceT", Card, Deck, Note, Review)
SinkT = TypeVar("SinkT", Card, DeckConfig, Deck, Notetype, Note)


class _ReadNotetypeSchema(NamedTuple):
  """The field and template lists of a notetype, as Anki compares them on an update.

  Anki updates a notetype it already has only when both lists keep their length and
  every field and template still matches the one in the same position. Otherwise the
  notetype is imported as a new copy and the notes the user already has are not
  updated, unless they tick "Merge note types" when importing.

  Attributes:
    fields: The fields, in order.
    templates: The templates, in order.
  """

  # ----------------------------------- Nested types -----------------------------------

  class SchemaItem(NamedTuple):
    """One field or template, by the id and the name Anki matches it on.

    Attributes:
      id: The id in the field or template config, if it has one.
      name: The field or template name.
    """

    id: int | None
    name: str

    def is_match(self, item: Self) -> bool:
      """Return whether `item` is the same field or template.

      Ids decide when both sides have one, names otherwise.
      """
      if item.id is not None and self.id is not None:
        return item.id == self.id
      return item.name == self.name

  # ------------------------------------ Attributes ------------------------------------

  fields: tuple[SchemaItem, ...]
  templates: tuple[SchemaItem, ...]

  def is_match(self, other: Self) -> bool:
    """Return whether Anki would update a notetype with `other` instead of copying it.

    Both lists must have the same length, and each item must match the one at the
    same position (see `SchemaItem.is_match()`).
    """
    return (
      len(self.fields) == len(other.fields)
      and len(self.templates) == len(other.templates)
      and all(
        field.is_match(other_field)
        for field, other_field in zip(self.fields, other.fields)
      )
      and all(
        template.is_match(other_template)
        for template, other_template in zip(self.templates, other.templates)
      )
    )

  @classmethod
  def from_notetype(cls, notetype: Notetype) -> Self:
    """Return the schema `notetype` has now."""
    return cls(
      fields=tuple(
        cls.SchemaItem(field.config.id, field.name) for field in notetype.fields
      ),
      templates=tuple(
        cls.SchemaItem(template.config.id, template.name)
        for template in notetype.templates
      ),
    )


class Collection(CheckedModel):
  """An Anki collection, one attribute per table.

  Use the class to build a collection of decks, notes, cards, etc. and to write it to
  a SQLite file. The writer builds each table from its list. There are checks to verify
  the lists agree with each other. Every check stands for a repair Anki performs, but
  the difference is that this class raises, while Anki repairs by deleting or renaming
  something.

  No checks run while the collection is built. Use `Collection.check()` to verify for
  any errors. A write calls it, unless it runs inside `genanki2.checks_disabled()`.

  Orphan rows reference other rows (by id), but the referenced rows are not in the
  collection. The reader fills all five `orphan_` lists so the rows can be written
  back. Anki deletes them in a database check, and no genanki2 check or
  `Collection.collect_*()` method sees them.

  Attributes:
    deck_configs: All the presets in the collection.
    decks: Each holds the hierarchical human name and converts at the row boundary.
    notetypes: A notetype spans three tables, and carries its own fields and templates.
    notes: The content. Every one contains a notetype that should be in `notetypes`.
    tags: The table of tag names, which an export never writes and an import rebuilds
      from the notes. Empty unless a caller opts into `Collection.collect_tags()`.
    cards: One per note and template, and where the scheduling lives.
    reviews: The answer history, one entry per answer. An export carries it only
      with scheduling, and an import drops an entry whose card it did not import.
    graves: The deletion log, which only sync reads. Empty unless a caller read a
      collection that carried one, or added them manually. Genanki2 never creates any:
      a grave during a sync tells the server which rows to delete.
    metadata: The single `col` row.
    config: The collection's settings. An import does not copy them, but it reads the
      scheduling ones off the package to rebase due dates.
    orphan_fields: Raw `fields` rows whose notetype is missing.
    orphan_templates: Raw `templates` rows whose notetype is missing.
    orphan_notes: Raw `notes` rows whose notetype is missing.
    orphan_cards: Raw `cards` rows whose note or deck is missing.
    orphan_reviews: Raw `revlog` rows whose card is missing.
  """

  deck_configs: list[DeckConfig] = pyd.Field(default_factory=list)
  decks: list[Deck] = pyd.Field(default_factory=list)
  notetypes: list[Notetype] = pyd.Field(default_factory=list)
  notes: list[Note] = pyd.Field(default_factory=list)
  tags: list[Tag] = pyd.Field(default_factory=list)
  cards: list[Card] = pyd.Field(default_factory=list)
  reviews: list[Review] = pyd.Field(default_factory=list)
  graves: list[Grave] = pyd.Field(default_factory=list)

  metadata: CollectionMetadata = pyd.Field(default_factory=CollectionMetadata)
  config: list[ConfigEntry] = pyd.Field(default_factory=list)

  # Orphans
  orphan_fields: list[FieldRow] = pyd.Field(default_factory=list)
  orphan_templates: list[TemplateRow] = pyd.Field(default_factory=list)
  orphan_notes: list[NoteRow] = pyd.Field(default_factory=list)
  orphan_cards: list[CardRow] = pyd.Field(default_factory=list)
  orphan_reviews: list[RevlogRow] = pyd.Field(default_factory=list)

  # Read data
  _read_notetypes: frozendict[int, _ReadNotetypeSchema] = pyd.PrivateAttr(
    default=frozendict()
  )

  @classmethod
  def default(cls) -> Self:
    """Build an empty collection holding the config a new Anki collection starts with.

    The config is `default_config_entries()`, with `creationOffset` set to the local
    time zone's offset at the creation time, as Anki sets it when it creates a
    collection. Calling `Collection()` doesn't add any config.

    The `Default` preset is added on write. The `Default` deck is not, and a
    `CollectionPackage` needs it: see `Collection.collect_default_deck()` or
    `Collection.anki_default()`.
    """
    collection: Self = cls(config=default_config_entries())
    if (creation_offset := collection.get_config_entry("creationOffset")) is not None:
      creation_time_s: int = collection.metadata.creation_time_s
      creation_datetime: datetime = datetime.fromtimestamp(creation_time_s).astimezone()
      creation_offset.set_value(utc_offset_minutes_west(creation_datetime))
    return collection

  @classmethod
  def anki_default(cls) -> Self:
    """Build an empty collection holding what a new Anki collection starts with.

    It holds what `Collection.default()` adds, plus the `Default` deck, the `Default`
    preset and the six stock notetypes. Genanki2's stock notetypes have a
    `" (genanki2)"` suffix in their names.

    Use it for a `CollectionPackage`, which needs the `Default` deck. A `Package` does
    not: an apkg import skips an empty deck and every collection already holds
    `Default`.

    All are the frozen builtins. To change them, replace them with instances created by
    calling `default_deck_config()`, `default_deck()` or `builtin_notetype()`.
    """
    collection: Self = cls.default()
    collection.deck_configs.append(DEFAULT_DECK_CONFIG)
    collection.decks.append(DEFAULT_DECK)
    collection.notetypes.extend(BUILTIN_NOTETYPES)
    return collection

  # ---------------------------------- Derived values ----------------------------------

  @property
  def days_elapsed_since_creation(self) -> int:
    """The calendar days passed at rollover hour since creation time.

    The count assumes the v2 scheduler is used and the `schedVer` config is not
    checked. Anki has a third count for the v1 scheduler, with no rollover and no
    timezone, but no package currently written by Anki reaches it.

    A collection read directly from an SQLite file can still be v1. It is counted here
    as though it were v2, which is not the number Anki would compute for it, so reading
    one is unsupported and at the caller's own risk.

    Raises:
      Genanki2Error: `creationOffset` config does not contain a nullable int
      Genanki2Error: `rollover` config does not contain an int
    """
    offset: int | None | Any = None
    if (offset_entry := self.get_config_entry("creationOffset")) is not None:
      offset = offset_entry.value_or(offset)
      if offset is not None and type(offset) is not int:
        raise Genanki2Error(
          f"Config entry `creationOffset` did not contain a nullable integer: {offset}"
        )

    rollover_hour: int | Any = 4
    if (rollover_entry := self.get_config_entry("rollover")) is not None:
      rollover_hour = rollover_entry.value_or(rollover_hour)
      if type(rollover_hour) is not int:
        raise Genanki2Error(
          f"Config entry `rollover` did not contain an integer: {rollover_hour}"
        )

    return days_elapsed(
      self.metadata.creation_time_s,
      creation_offset_minutes_west=offset,
      rollover_hour=rollover_hour,
    )

  def get_config_entry(self, key: str) -> ConfigEntry | None:
    """Retrieve a config entry from the config list or `None` if not found."""
    return next((entry for entry in self.config if entry.key == key), None)

  def get_deck_config(
    self, *, id: int | None = None, name: str | None = None
  ) -> DeckConfig | None:
    """Retrieve the preset with `id` or `name` from `deck_configs`, or `None`.

    Pass one of the two. Names compare without case. Anki lets two presets share a name,
    and the first one in the list is returned.

    Raises:
      Genanki2Error: both `id` and `name` are given, or neither.
    """
    if id is not None and name is None:
      return next((dc for dc in self.deck_configs if dc.id == id), None)
    if id is None and name is not None:
      name = name.casefold()
      return next((dc for dc in self.deck_configs if dc.name.casefold() == name), None)
    raise Genanki2Error("Pass exactly one of `id` and `name`.")

  def get_deck(self, *, id: int | None = None, name: str | None = None) -> Deck | None:
    """Retrieve the deck with `id` or `name` from `decks`, or `None`.

    Pass one of the two. A name is normalized the way Anki stores deck names and
    compares without case, as Anki compares them, so `italian::verbs` finds
    `Italian::Verbs`.

    Raises:
      Genanki2Error: a part of `name` is empty after normalization.
      Genanki2Error: both `id` and `name` are given, or neither.
    """
    if id is not None and name is None:
      return next((deck for deck in self.decks if deck.id == id), None)
    if id is None and name is not None:
      name = normalize_deck_name(name).casefold()
      return next((deck for deck in self.decks if deck.name.casefold() == name), None)
    raise Genanki2Error("Pass exactly one of `id` and `name`.")

  def get_notetype(
    self, *, id: int | None = None, name: str | None = None
  ) -> Notetype | None:
    """Retrieve the notetype with `id` or `name` from `notetypes`, or `None`.

    Pass one of the two. A name is normalized the way Anki stores notetype names and
    compares without case, as Anki compares them.

    Raises:
      Genanki2Error: `name` is empty after normalization.
      Genanki2Error: both `id` and `name` are given, or neither.
    """
    if id is not None and name is None:
      return next((notetype for notetype in self.notetypes if notetype.id == id), None)
    if id is None and name is not None:
      name = normalize_notetype_name(name).casefold()
      return next(
        (notetype for notetype in self.notetypes if notetype.name.casefold() == name),
        None,
      )
    raise Genanki2Error("Pass exactly one of `id` and `name`.")

  def get_note(self, *, id: int | None = None, guid: str | None = None) -> Note | None:
    """Retrieve the note with `id` or `guid` from `notes`, or `None`.

    Pass one of the two.

    Raises:
      Genanki2Error: both `id` and `guid` are given, or neither.
    """
    if id is not None and guid is None:
      return next((note for note in self.notes if note.id == id), None)
    if id is None and guid is not None:
      return next((note for note in self.notes if note.guid == guid), None)
    raise Genanki2Error("Pass exactly one of `id` and `guid`.")

  @property
  def max_new_card_position(self) -> int:
    """The highest position a new card in `Collection.cards` holds, or -1 if none.

    Like Anki's database check, it reads `Card.due` even for a new card in a filtered
    deck, where it is negative.
    """
    return max(
      (card.due for card in self.cards if card.card_type == CardType.NEW), default=-1
    )

  @property
  def next_new_card_position(self) -> int:
    """The position in the new-card queue taken by the cards of the next added note.

    The counter is the `nextPos` config. Each added note takes the current value and
    advances it by one, and its cards share that position. Reading the property does not
    advance it.

    A missing or invalid entry (when not a UInt32) returns 0, as Anki does.
    """
    if (entry := self.get_config_entry("nextPos")) is not None and (
      is_uint32(position := entry.value_or(0))
    ):
      return position
    return 0

  @property
  def unused_decks(self) -> tuple[Deck, ...]:
    """The decks in `decks` that no card needs.

    A card needs its deck and that deck's parents, found by name up to the first one
    the collection lacks, so a deck above a gap in the hierarchy is unused. The home
    deck of a card sitting in a filtered deck is listed unless a card is in it, and the
    `Default` deck is listed when no card is in it.

    These are the decks an apkg import skips: it creates the decks holding the cards
    and their parents, and a home deck only when the user imports learning progress. A
    colpkg restore keeps every deck, listed or not.

    The cards are those in `cards` and those the reviews reference, as a write collects
    them.
    """
    cards: list[Card] = [*self.cards, *(review.card for review in self.reviews)]
    deck_names: set[str] = {deck.name.casefold() for deck in self.decks}
    for card in cards:
      deck_names.add(card.deck.name.casefold())
      if isinstance(card.original_deck, Deck):
        deck_names.add(card.original_deck.name.casefold())
    used_names: set[str] = set()
    for card in cards:
      name: str = card.deck.name
      used_names.add(name.casefold())
      while (
        (name := name.rpartition(DECK_HUMAN_SEPARATOR)[0])
        and (parent := name.casefold()) in deck_names
        and parent not in used_names
      ):
        used_names.add(parent)
    return tuple(deck for deck in self.decks if deck.name.casefold() not in used_names)

  @property
  def unused_notetypes(self) -> tuple[Notetype, ...]:
    """The notetypes in `notetypes` that no note uses.

    The notes are those in `notes` and those the cards and reviews reference, as a write
    collects them. These are the notetypes an apkg import skips, since it creates only
    the notetypes of the notes. A colpkg restore keeps every notetype, listed or not.
    """
    reviewed: Iterable[Card] = (review.card for review in self.reviews)
    cards: Iterable[Card] = it.chain(self.cards, reviewed)
    notes: Iterable[Note] = it.chain(self.notes, (card.note for card in cards))
    used_ids: set[int] = {note.notetype.id for note in notes}
    return tuple(notetype for notetype in self.notetypes if notetype.id not in used_ids)

  def _scheduler_version(self) -> int | str:
    """Return the `schedVer` config value for any checks or warnings.

    Returns:
      The decoded value when it is an integer, the raw bytes as text when it is not,
      or "missing" when the collection has no entry.
    """
    version: int | str | Any = "missing"
    if (scheduler_entry := self.get_config_entry("schedVer")) is not None:
      version = scheduler_entry.value_or()
      return version if isinstance(version, int) else repr(scheduler_entry.value)
    return version

  # ------------------------------ Collection management -------------------------------

  # Idea: add_deck_config(), add_deck(), add_notetype(); only if they check for duplicates

  def _reviews_by_card_id(self) -> dict[int, list[Review]]:
    """Group the reviews by card id."""
    grouped_reviews: dict[int, list[Review]] = {}
    for review in self.reviews:
      grouped_reviews.setdefault(review.card.id, []).append(review)
    return grouped_reviews

  def _cards_by_guid(self) -> dict[str, list[Card]]:
    """Collect the cards and group them by note guid."""
    self.collect_cards()
    grouped_cards: dict[str, list[Card]] = {}
    for card in self.cards:
      grouped_cards.setdefault(card.note.guid, []).append(card)
    return grouped_cards

  def _set_next_pos_config(self, position: int) -> ConfigEntry:
    """Store `position` in the `nextPos` counter and return its config entry.

    The entry is added when the collection has none. The counter is a UInt32, and a
    position outside that range is stored as 1, the value Anki leaves after adding a
    note to an unreadable counter.
    """
    if (entry := self.get_config_entry("nextPos")) is None:
      entry = ConfigEntry(key="nextPos")
      self.config.append(entry)
    entry.set_value(position if is_uint32(position) else 1)
    return entry

  def _warn_bad_next_pos_config(self, next_pos: int) -> Self:
    """Warn when `next_pos` is at or below the highest position a new card holds.

    The cards about to be stored are not in `Collection.cards` yet, so only the
    existing ones count.
    """
    if next_pos <= (max_due := self.max_new_card_position):
      warnings.warn(
        f"Collection: the new-card position counter ({next_pos}) is at or below the "
        f"highest position an existing new card holds ({max_due}), so the added notes "
        "share positions with existing new cards and are studied among them instead "
        "of after them. Call `Collection.repair_next_new_card_position()` first to "
        "move the counter past them, as Anki's database check does.",
        Genanki2Warning,
        stacklevel=3,
      )
    return self

  def add_notes(
    self, notes: Iterable[Note], deck: Deck
  ) -> tuple[tuple[Card, ...], ...]:
    """Add `notes` to `deck` with the cards Anki generates for them.

    A note gets the cards in `Note.card_ordinals`. For a normal notetype, a card goes
    in `deck` unless its template config overrides it. If a note would produce no card,
    it still gets card 0 in `deck`, with a warning. The new-card position counter
    advances once per note. When the counter is at or below a position an existing new
    card holds, the new notes would be studied among the existing ones, so a warning
    points to `Collection.repair_next_new_card_position()`.

    Only the cards are stored in `Collection.cards`. The deck, the notes and their
    notetypes join when the collection collects its references (for example by
    explicitly calling a `Collection.collect_*()` method or during writing).

    Important: Producing the cards searches the collection for deck presets and decks.
    In case the notes have templates that override the target deck or the target deck
    uses a custom preset, it is highly recommended to ensure that those deck presets
    and decks are present in the collection (e.g. by adding them before calling
    `add_notes()`), otherwise the builtin defaults (id=1) are used as a fallback.

    The cards are returned grouped by note, in the order of `notes`.

    Args:
      notes: The notes to add. Each one is added once, and not again later.
      deck: The deck to which the cards go, unless a template overrides it.

    Raises:
      Genanki2Error: `deck` is a filtered deck.
      Genanki2Error: a guid appears more than once in `notes`.
      Genanki2Error: a note already has cards in the collection.
      Genanki2Error: a card goes to the Default deck and the collection's deck 1 is a
        filtered deck.
    """
    notes = tuple(notes)
    if not notes:
      return ()

    if deck.is_filtered():
      raise Genanki2Error(
        f'Collection: cannot add notes to the filtered deck "{deck.name}". Anki would '
        "put their cards in the Default deck instead. Pass a normal deck."
      )

    if duplicate_notes := duplicates(note.guid for note in notes):
      raise Genanki2Error(
        "Collection: the notes to add repeat a guid, and a guid identifies one note. "
        f"Repeated guids: {duplicate_notes}"
      )

    guids: AbstractSet[str] = self._cards_by_guid().keys()
    if notes_with_cards := tuple(note.guid for note in notes if note.guid in guids):
      raise Genanki2Error(
        "Collection: some notes to add already have cards in the collection. Leave "
        f"them out. Their guids: {notes_with_cards}"
      )

    next_pos: int = self.next_new_card_position
    self._warn_bad_next_pos_config(next_pos)
    self.collect_decks().collect_deck_configs()
    maker: CardMaker = CardMaker(self, next_pos)
    all_cards: list[tuple[Card, ...]] = []
    # Keep the loop until Python 3.12: comprehensions and generators add a stack frame
    # that moves where warnings point.
    for note in notes:
      all_cards.append(maker.make(note, deck=deck))
    self._set_next_pos_config(maker.next_pos)
    self.cards.extend(it.chain.from_iterable(all_cards))
    return tuple(all_cards)

  def _remove(self, note_guids: set[str], card_ids: set[int]) -> Self:
    """Remove the notes in `note_guids`, the cards in `card_ids` and their reviews.

    The cards of a removed note stay unless their ids are in `card_ids`.
    """
    if note_guids:
      self.notes[:] = [note for note in self.notes if note.guid not in note_guids]
    if card_ids:
      self.cards[:] = [card for card in self.cards if card.id not in card_ids]
      self.reviews[:] = [
        review for review in self.reviews if review.card.id not in card_ids
      ]
    return self

  def remove_notes(
    self, notes: Iterable[Note]
  ) -> tuple[tuple[tuple[Card, ...], tuple[Review, ...]], ...]:
    """Remove `notes` with their cards and the reviews of those cards.

    A note is found by its guid, and its cards include those only a review holds. Anki
    keeps the review log of a deleted card, but a review here holds its card and would
    bring it back on the next write, so the reviews go too.

    An apkg import never deletes, so a note removed from a package stays in the
    collection of every user who imported an earlier version.

    The removed cards and reviews are returned per note, in the order of `notes`. A
    note the collection doesn't hold gets two empty tuples.

    Args:
      notes: The notes to remove.

    Raises:
      Genanki2Error: two cards with different content share an id.
    """
    notes = tuple(notes)
    if not notes:
      return ()

    reviews_by_id: dict[int, list[Review]] = self._reviews_by_card_id()
    cards_by_guid: dict[str, list[Card]] = self._cards_by_guid()
    card_ids_to_remove: set[int] = set()
    note_guids: set[str] = set()
    removed: list[tuple[tuple[Card, ...], tuple[Review, ...]]] = []
    for note in notes:
      note_guids.add(note.guid)
      cards: list[Card] = cards_by_guid.get(note.guid, [])
      card_ids_to_remove.update(card.id for card in cards)
      reviews: tuple[Review, ...] = tuple(
        review for card in cards for review in reviews_by_id.get(card.id, ())
      )
      removed.append((tuple(cards), reviews))

    self._remove(note_guids, card_ids_to_remove)
    return tuple(removed)

  def remove_empty_cards(
    self, keep_notes: bool = False
  ) -> tuple[tuple[Note, tuple[Card, ...], tuple[Review, ...]], ...]:
    """Remove the cards their notes no longer produce, with their reviews.

    Like Anki's "Empty Cards", a card is empty when its ordinal is not in
    `Note.card_ordinals`, which includes a card whose template no longer exists. When
    studied, an empty card shows an error instead of a question.

    With `keep_notes=False`, a note whose cards are all empty is also removed. A note
    without cards gets card 0 again, from `Collection.generate_missing_cards()` or
    from Anki, so a kept note keeps its card with the lowest ordinal. A note that has
    no cards at all is left alone.

    An apkg import never deletes, so users who imported the empty cards earlier keep
    them until they run "Empty Cards" in Anki.

    The removed cards and reviews are returned per note, for each note that lost a
    card.

    Args:
      keep_notes: Whether a note whose cards are all empty stays, with one of them.
        Default: `False`.

    Raises:
      Genanki2Error: two cards with different content share an id.
    """
    reviews_by_id: dict[int, list[Review]] = self._reviews_by_card_id()
    cards_by_guid: dict[str, list[Card]] = self._cards_by_guid()
    card_ids_to_remove: set[int] = set()
    note_guids: set[str] = set()
    removed: list[tuple[Note, tuple[Card, ...], tuple[Review, ...]]] = []
    for cards in cards_by_guid.values():
      note: Note = cards[0].note
      ordinals: set[int] = set(note.card_ordinals)
      empty_cards: list[Card] = [
        card for card in cards if card.template_ord not in ordinals
      ]
      if not empty_cards:
        continue
      if len(empty_cards) == len(cards):
        if keep_notes:
          min_ordinal: int = min(card.template_ord for card in cards)
          empty_cards[:] = [
            card for card in empty_cards if card.template_ord != min_ordinal
          ]
        else:
          note_guids.add(note.guid)
      if empty_cards:
        card_ids_to_remove.update(card.id for card in empty_cards)
        reviews: tuple[Review, ...] = tuple(
          review for card in empty_cards for review in reviews_by_id.get(card.id, ())
        )
        removed.append((note, tuple(empty_cards), reviews))

    self._remove(note_guids, card_ids_to_remove)
    return tuple(removed)

  def create_missing_parent_decks(self) -> Self:
    """Add a deck for every ancestor of a subdeck that the collection does not hold.

    The new decks are normal decks on the default deck config, with the same
    attributes as the `Default` deck. Anki creates them on import too in the same way.

    This calls `Collection.collect_decks()` in order to get all the decks in the
    collection.

    Nothing calls this; a caller opts in.
    """
    self.collect_decks()
    deck_names: set[str] = {deck.name.casefold() for deck in self.decks}
    for deck in list(self.decks):
      parts: list[str] = deck.name.split(DECK_HUMAN_SEPARATOR)
      for depth in range(1, len(parts)):
        name: str = DECK_HUMAN_SEPARATOR.join(parts[:depth])
        if name.casefold() not in deck_names:
          deck_names.add(name.casefold())
          self.decks.append(Deck(name=name))
    return self

  def generate_missing_cards(
    self, deck: Deck | None = None
  ) -> tuple[tuple[Card, ...], ...]:
    """Make all the cards that a note would render and it does not have yet.

    Call it after editing notes, for example after filling a field that a template's
    front needs. The notes are collected from the cards first, so a note in
    `Collection.notes` with no cards gets its cards too, or card 0 with a warning if
    none renders. Cards whose template no longer renders are kept, as in Anki.

    A new card goes in the deck its template overrides it with, otherwise in the deck
    to which all the note's existing cards belong (for a card in a filtered deck,
    the deck it came from), else in `deck`, else in the Default deck. A shared deck
    that is missing or filtered sends the card to the Default deck, as in Anki.

    When a note already has a new card, the added cards share its position and the
    `nextPos` counter is unchanged. Otherwise the note receives the next position and
    the counter advances. A warning points out when the counter is behind the existing
    new cards.

    The cards are returned grouped by note, for the notes that gained cards, in the
    order of `Collection.notes`.

    Args:
      deck: The deck for a note whose existing cards do not share one deck.

    Raises:
      Genanki2Error: `deck` is a filtered deck.
      Genanki2Error: a card goes to the Default deck and the collection's deck 1 is a
        filtered deck.
    """
    if deck is not None and deck.is_filtered():
      raise Genanki2Error(
        f'Collection: cannot add notes to the filtered deck "{deck.name}". Anki would '
        "put their cards in the Default deck instead. Pass a normal deck."
      )

    grouped_cards: dict[str, list[Card]] = self._cards_by_guid()
    self.collect_notes().collect_decks().collect_deck_configs()
    next_pos: int = self.next_new_card_position
    maker: CardMaker = CardMaker(self, next_pos)
    all_cards: list[tuple[Card, ...]] = []
    # Keep the loop until Python 3.12: comprehensions and generators add a stack frame
    # that moves where warnings point.
    for note in self.notes:
      if new_cards := maker.make(note, grouped_cards.get(note.guid, ()), deck):
        all_cards.append(new_cards)
    if next_pos != maker.next_pos:
      self._warn_bad_next_pos_config(next_pos)
    self._set_next_pos_config(maker.next_pos)
    self.cards.extend(it.chain.from_iterable(all_cards))
    return tuple(all_cards)

  # ------------------- Collection management: Collecting references -------------------

  def _collect(
    self,
    source: list[SourceT],
    sink: list[SinkT],
    source_attr: Callable[[SourceT], SinkT | None],
  ) -> Self:
    """Append to `sink` every model `source` references that `sink` does not hold.

    Membership is checked by id. A referenced model whose id is already in the sink
    is not added. Ids have to be unique per model.

    Args:
      source: The collection list with models holding references.
      sink: The collection list of referenced models where they are appended.
      source_attr: Reads the reference off one source model. Only `Deck` can hold a
        nullable reference to a config.

    Raises:
      Genanki2Error: two models with different content share an id.
    """
    if not source:
      return self
    registered: dict[int, SinkT] = {model.id: model for model in sink}
    for source_model in source:
      submodel: SinkT | None = source_attr(source_model)
      if submodel is None:
        continue
      known: SinkT | None = registered.get(submodel.id)
      if known is None:
        registered[submodel.id] = submodel
        sink.append(submodel)
      elif known is not submodel and not known.is_content_equivalent(submodel):
        sink_kind: str = type(submodel).__name__
        source_kind: str = type(source_model).__name__
        raise Genanki2Error(
          f"Two different {sink_kind} objects share id {submodel.id}: one is in the "
          f"collection and the other is referenced by the {source_kind} with id "
          f"{source_model.id}. Only one of them can be written."
        )
    return self

  def collect_cards(self) -> Self:
    """Add every card the reviews reference that the collection does not already hold.

    Only additive: a never reviewed card is kept.

    Raises:
      Genanki2Error: two cards with different content share an id.
    """
    self._collect(self.reviews, self.cards, lambda review: review.card)
    return self

  def collect_notes(self) -> Self:
    """Add every note the cards reference that the collection does not already hold.

    Only additive: a note no card renders is kept.

    Raises:
      Genanki2Error: two notes with different content share an id.
    """
    self._collect(self.cards, self.notes, lambda card: card.note)
    return self

  def collect_notetypes(self) -> Self:
    """Add every notetype the notes use that the collection does not already hold.

    Only additive: an unused notetype is kept, since a collection can hold one and
    a package read from a file usually does.

    Raises:
      Genanki2Error: two notetypes with different content share an id.
    """
    self._collect(self.notes, self.notetypes, lambda note: note.notetype)
    return self

  def collect_tags(self) -> Self:
    """Register every tag the notes carry that the collection does not already hold.

    Only additive: an unused tag is kept, since a collection keeps the tag when it
    drops the last note carrying it.

    The first spelling of an encountered tag gets registered. The tags table has
    priority, then come the notes' tags in order of appearance.

    Names are matched case-insensitively. A tag already registered wins, which is
    what Anki does when a note names a registered tag.

    Nothing calls this; a caller opts in. An export writes no tags at all and an
    import rebuilds the registry from the notes, so collecting them changes nothing
    about how a package imports.
    """
    if not self.notes:
      return self
    registered: dict[str, Tag] = {}
    for tag in self.tags:  # Anki also applies first seen wins
      registered.setdefault(tag.tag.casefold(), tag)
    for note in self.notes:
      for tag in note.tags_to_register:
        if tag.tag.casefold() not in registered:
          registered[tag.tag.casefold()] = tag
          self.tags.append(tag)
    return self

  def collect_decks(self) -> Self:
    """Add every deck the cards reference that the collection does not already hold.

    Both `Card.deck` and `Card.original_deck` can contribute.

    Only additive: a deck no card is in is kept. A collection package carries it, while
    an apkg import skips it, since Anki creates only the decks holding the package's
    cards, their parents and, when scheduling is imported, the home decks of cards in a
    filtered deck.

    Raises:
      Genanki2Error: two decks with different content share an id.
    """
    self._collect(self.cards, self.decks, lambda card: card.deck)
    self._collect(
      self.cards,
      self.decks,
      lambda card: card.original_deck if isinstance(card.original_deck, Deck) else None,
    )
    return self

  def collect_default_deck(self) -> Self:
    """Add the builtin `Default` deck when no deck in the collection has id 1.

    Anki treats deck 1 as always present. A hand-built genanki2 collection holds it
    only if a card is in `DEFAULT_DECK` or the caller added it to the `decks` list.

    `Collection.collect_decks()` is called first, so a default deck is picked up if
    referenced.

    Nothing calls this; a caller opts in. A colpkg write refuses a collection without
    deck 1 (see `Collection.check_present_default_deck()`).

    Raises:
      Genanki2Error: two decks with different content share an id.
    """
    self.collect_decks()
    if all(deck.id != 1 for deck in self.decks):
      self.decks.append(DEFAULT_DECK)
    return self

  def collect_deck_configs(self) -> Self:
    """Add every preset the decks follow that the collection does not already hold.

    Only a deck holding a `DeckConfig` object contributes. A deck that names its preset
    through `DeckNormalKind.config_id` doesn't carry something to collect and
    `Collection.warn_missing_deck_configs()` catches it, except for preset 1, which
    `Collection.collect_default_deck_config()` covers.

    Raises:
      Genanki2Error: two deck configs with different content share an id.
    """
    self._collect(self.decks, self.deck_configs, lambda deck: deck.config)
    return self

  def collect_default_deck_config(self) -> Self:
    """Add the builtin `Default` preset when a deck names preset 1 and nothing holds it.

    Preset 1 is the only id a deck can name without the caller building a preset. It is
    the default preset for any deck. Shipping it is safe because the importer doesn't
    add a preset it already has and a deck the target already has keeps its own preset
    when the incoming one is 1.

    No other id can be repaired this way because genanki2 doesn't hold a copy of a
    preset it did not write.
    """
    if any(deck_config.id == 1 for deck_config in self.deck_configs):
      return self
    if any(
      deck.config is None
      and isinstance(deck.kind, DeckNormalKind)
      and deck.kind.config_id == 1
      for deck in self.decks
    ):
      self.deck_configs.append(DEFAULT_DECK_CONFIG)
    return self

  def collect_references(self) -> Self:
    """Add every model the collection references that its own lists do not already hold.

    The chain: cards name notes and decks, notes name notetypes and decks name presets.

    `Collection.write_sqlite()` calls this, so a caller appends the models it builds
    and the lists that follow from them fill themselves. Nothing is removed. The one
    model added that no caller built is the `Default` preset.

    This method does not call `Collection.collect_tags()` because Anki never exports or
    imports this table.

    Raises:
      Genanki2Error: two models with different content share an id.
    """
    self.collect_cards()
    self.collect_decks()
    self.collect_notes()
    self.collect_deck_configs()
    self.collect_notetypes()
    self.collect_default_deck_config()
    return self

  # ------------------------------------- Repairs --------------------------------------

  def repair_missing_deck_configs(self) -> Self:
    """Point every normal deck with a preset the collection does not have at preset 1.

    When it cannot find a preset, Anki falls back to the default preset, but doesn't
    update the database. Presets are collected first, so a deck holding its deck config
    is not changed.

    Nothing calls this; a caller opts in.

    Raises:
      Genanki2Error: two deck configs with different content share an id.
    """
    self.collect_deck_configs()
    deck_config_ids: set[int] = {deck_config.id for deck_config in self.deck_configs}
    for deck in self.decks:
      if isinstance(deck.kind, DeckNormalKind) and (
        deck.kind.config_id not in deck_config_ids
      ):
        deck.kind.config_id = 1
    self.collect_default_deck_config()
    return self

  def repair_next_new_card_position(self) -> Self:
    """Set the `nextPos` counter one past the highest position a new card holds.

    This is what Anki's "Check Database" does. A counter that is behind gives the next
    added note a position that existing new cards already hold, so the note is not
    studied after them. The cards the reviews reference are collected first.

    Like Anki, it reads a new card's `due` even in a filtered deck, where it is negative
    and the card's real position is kept in `original_due`. When no new card has a
    position of 0 or more, the counter is set to 0.

    Nothing calls this; a caller opts in.
    """
    self.collect_cards()
    # Anki stores a negative due as 0 and new cards in filtered decks have negative dues
    self._set_next_pos_config(max(self.max_new_card_position, -1) + 1)
    return self

  # ----------------------------- Pre-serialization steps ------------------------------

  @override
  def prepare(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Collect the referenced models into the lists, then prepare every model held.

    Args:
      recursive: Whether to prepare every model reachable from this one too, each once.
        Each of them is called with `recursive=False`, so an override makes only its
        own changes and leaves the walk to the call it came from. A frozen model is
        skipped. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.collect_references()
    return super().prepare(recursive=recursive, context=context)

  def check_unique_ids(self) -> Self:
    """Reject an id used twice within one table.

    Lists checked: `deck_configs`, `decks`, `notetypes`, `notes`, `cards`, `reviews`.

    Raises:
      Genanki2Error: two objects in one list share an id.
    """
    lists = ("deck_configs", "decks", "notetypes", "notes", "cards", "reviews")
    for item_list in lists:
      if ids := duplicates(item.id for item in getattr(self, item_list)):
        msg = f"Check failed: the {item_list} list has duplicate ids. Duplicates: {ids}"
        raise Genanki2Error(msg)
    return self

  def check_unique_names(self) -> Self:
    """Reject deck or notetype names that differ only in case.

    Anki renames the clashing name, appending '+' until the name is free.

    This uses Python's `str.casefold()` which folds more than Anki's `unicase`, so what
    this refuses is a superset of what Anki's db refuses.

    Raises:
      Genanki2Error: two decks, or two notetypes, share a name after casefolding.
    """
    for item_list in ("decks", "notetypes"):
      if names := duplicates(obj.name.casefold() for obj in getattr(self, item_list)):
        raise Genanki2Error(
          f"Check failed: the {item_list} list has duplicate names. Duplicates: {names}"
        )
    return self

  def check_unique_note_guids(self) -> Self:
    """Reject two notes sharing a guid.

    The importer matches notes by guid. An incoming note updates an existing note or
    is skipped, meaning it is never added.

    Raises:
      Genanki2Error: two notes share a guid.
    """
    if guids := duplicates(note.guid for note in self.notes):
      msg = f"Check failed: the notes list has duplicate guids. Duplicates: {guids}"
      raise Genanki2Error(msg)
    return self

  def check_unique_tags(self) -> Self:
    """Reject two tags differing only in case.

    `Tag.tag` is primary key under `COLLATE unicase` in SQLite.

    This uses Python's `str.casefold()` which folds more than Anki's `unicase`, so what
    this refuses is a superset of what Anki's db refuses.

    Raises:
      Genanki2Error: two tags are identical after casefolding.
    """
    if tags := duplicates(tag.tag.casefold() for tag in self.tags):
      raise Genanki2Error(
        f"Check failed: the tags list has duplicate tag spellings after casefolding. "
        f"Duplicates: {tags}"
      )
    return self

  def check_unique_card_producers(self) -> Self:
    """Reject two cards produced by the same note and template.

    Note id and template ordinal is what produced a card, and Anki repairs a duplicate
    by deleting cards.

    Raises:
      Genanki2Error: two cards share a note id and a template ordinal.
    """
    producers = duplicates((card.note.id, card.template_ord) for card in self.cards)
    if producers:
      raise Genanki2Error(
        f"Check failed: not all cards have a unique note+template producer. Duplicates "
        f"as (note.id, template_ord): {producers}"
      )
    return self

  def check_unique_graves(self) -> Self:
    """Reject two graves deleting the same object.

    `(object_id, type)` is the table's primary key. Anki inserts a grave with
    `INSERT OR IGNORE` and cannot produce duplicates.

    Raises:
      Genanki2Error: two graves share an object id and a grave type.
    """
    if graves := duplicates(
      (grave.object_id, grave.grave_type) for grave in self.graves
    ):
      raise Genanki2Error(
        "Check failed: the graves list has duplicate primary keys. Duplicate "
        f"(object id, grave type) pairs: {graves}"
      )
    return self

  def check_unique_config_keys(self) -> Self:
    """Reject two config entries sharing a key.

    Raises:
      Genanki2Error: two entries share a key.
    """
    if keys := duplicates(entry.key for entry in self.config):
      raise Genanki2Error(
        "Check failed: the config list has entries with duplicate keys. Duplicates: "
        f"{keys}"
      )
    return self

  def check_referential_integrity(self) -> Self:
    """Reject an object naming another object the collection does not hold.

    Membership is by id, which is what Anki compares. Anki deletes a card whose note
    or deck is missing. `Card.original_deck` is not checked (see
    `Card.check_original_deck_is_normal()` and
    `Collection.check_card_original_deck_is_normal()`).

    Raises:
      Genanki2Error: a note names a notetype, or a card names a note or a deck, that the
        collection does not hold.
    """
    msg: str = (
      "Check failed: {0} ({1}) uses a {2} ({3}) which is not present in the "
      "collection's {2}s list."
    )
    notetype_ids: set[int] = {notetype.id for notetype in self.notetypes}
    deck_ids: set[int] = {deck.id for deck in self.decks}
    note_ids: set[int] = {note.id for note in self.notes}
    # Notetypes in notes
    for note in self.notes:
      if note.notetype.id not in notetype_ids:
        raise Genanki2Error(
          msg.format("note", f"guid={note.guid}", "notetype", f"id={note.notetype.id}")
        )
    # Notes and decks in cards
    for card in self.cards:
      if card.deck.id not in deck_ids:
        raise Genanki2Error(
          msg.format("card", f"id={card.id}", "deck", f"id={card.deck.id}")
        )
      if card.note.id not in note_ids:
        raise Genanki2Error(
          msg.format("card", f"id={card.id}", "note", f"guid={card.note.guid}")
        )
    return self

  def check_present_default_deck(
    self, context: SerializationContext | None = None
  ) -> Self:
    """Reject a colpkg file without deck 1.

    A restore accepts such a file, and the collection opens, but Anki cannot study it:
    every scheduler call looks up deck 1, even when the current deck is another one,
    and fails. The database check does not recreate the deck.

    `Collection.decks` is read as it stands. This check applies only to a colpkg file.
    Otherwise, `Collection.warn_missing_default_deck()` emits a warning.

    Args:
      context: Extra information from the serialization process. The check runs only
        when its output is `SerializationContext.Output.COLPKG`. Default: `None`.

    Raises:
      Genanki2Error: no deck in the collection has id 1.
    """
    if context is None or context.output != SerializationContext.Output.COLPKG:
      return self

    if all(deck.id != 1 for deck in self.decks):
      raise Genanki2Error(
        "Check failed: the collection has no deck with id 1. A restore accepts the "
        "file, but Anki cannot study the result: every scheduler call fails with "
        '"No such deck", and the database check does not recreate the deck. Call '
        "`Collection.collect_default_deck()` to add the builtin `Default` deck."
      )
    return self

  def check_card_original_deck_is_normal(self) -> Self:
    """Reject a card whose original deck id names a filtered deck.

    A card's original deck (its "home" deck) gives it its scheduling, so it cannot be
    a filtered deck. This is a cross-collection check when a card indicates its
    original deck by id. `Card.check_original_deck_is_normal()` covers the cases a card
    can answer alone.

    Deck 1 is skipped because Anki creates it as a normal deck, refuses to delete it
    and resets its name.

    An id naming a deck the collection does not hold in its lists is not rejected: Anki
    manages packages in that state. During import/export, filtered decks are turned
    into normal decks when those normal decks are not part of the package, and the
    cards are reset, ensuring a package imports.

    Raises:
      Genanki2Error: a card's original deck id names a filtered deck in the collection.
    """
    deck_idx: dict[int, Deck] = {}
    for deck in self.decks:
      deck_idx.setdefault(deck.id, deck)
    for card in self.cards:
      if isinstance(card.original_deck, int) and card.original_deck > 1:
        original_deck: Deck | None = deck_idx.get(card.original_deck)
        if original_deck is not None and original_deck.is_filtered():
          raise Genanki2Error(
            f"Check failed: card {card.id} names filtered deck {original_deck.name!r} "
            f"(id {card.original_deck}) as its original deck. The original deck has "
            "to be a normal deck."
          )
    return self

  def check_scheduler_version(
    self, context: SerializationContext | None = None
  ) -> Self:
    """Reject a colpkg whose `schedVer` config entry is not the v2 scheduler.

    A collection restore or a database check doesn't upgrade anything. With the entry
    missing or set to 1, the collection opens on the v1 scheduler, which Anki no longer
    runs: nothing can be studied until the user accepts the update Anki offers, and the
    update rewrites the review history. Other values prevent Anki from opening the
    collection at all.

    This check applies only to a colpkg file. Otherwise,
    `Collection.warn_scheduler_version()` emits a warning.

    Args:
      context: Extra information from the serialization process. The check runs only
        when its output is `SerializationContext.Output.COLPKG`. Default: `None`.

    Raises:
      Genanki2Error: `schedVer` is missing or holds a value other than 2.
    """
    if context is None or context.output != SerializationContext.Output.COLPKG:
      return self

    scheduler_version: int | str = self._scheduler_version()
    if scheduler_version == 2:
      return self

    raise Genanki2Error(
      f"Check failed: the collection's `schedVer` config entry is {scheduler_version}, "
      "but a restored collection needs 2. If missing or equal to 1, Anki uses the v1 "
      "scheduler, which cannot study until the user accepts an update that rewrites "
      "the review history. Other values stop Anki from opening it. Set it with "
      '`ConfigEntry(key="schedVer", value=b"2")`.'
    )

  @override
  def check(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Reject a collection that cannot be written.

    Checks don't repair anything, so they can't influence each other. The uniqueness
    checks come first so that other checks assuming it work correctly.

    Args:
      recursive: Whether to check every model reachable from this one too, each once.
        If false, it only runs this model's own checks. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.check_unique_ids()
    self.check_unique_note_guids()
    self.check_unique_names()
    self.check_unique_tags()
    self.check_unique_card_producers()
    self.check_unique_graves()
    self.check_unique_config_keys()
    self.check_referential_integrity()
    self.check_present_default_deck(context)
    self.check_card_original_deck_is_normal()
    self.check_scheduler_version(context)
    return super().check(recursive=recursive, context=context)

  def warn_future_ids(self, context: SerializationContext | None = None) -> Self:
    """Warn about a note, card or review whose id is more than a day in the future.

    An id is the millisecond when its row was created. Anki renumbers every id more
    than a day ahead of the current time, starting from that time, and updates the ids
    that cards and reviews reference, so the added dates and review times move with
    them. An apkg import renumbers them when it reads the package. A restore keeps them
    until the user runs the database check, which renumbers them. Without a context,
    the warning names both.

    The `orphan_` lists are not checked. An apkg import never carries those rows and
    the database check renumbers them along with the rest.

    Args:
      context: Extra information from the serialization process. Default: `None`.
    """
    match context:
      case SerializationContext(output=SerializationContext.Output.APKG):
        message: str = (
          "An apkg import renumbers them from the import time onwards, so the added "
          "dates and review times that the user gets are the import time, not the ones "
          "these ids encode"
        )
      case SerializationContext(output=SerializationContext.Output.COLPKG):
        message = (
          "A collection restore keeps them until the user runs the database check, "
          "which renumbers them from that time onwards and moves the added dates and "
          "review times with them"
        )
      case _:
        message = (
          "An apkg import renumbers them from the import time onwards. A restored "
          "colpkg keeps them until the user runs the database check, which renumbers "
          "them then. Either way, the added dates and review times move with them"
        )

    tomorrow: int = time_ms() + 24 * 60 * 60 * 1000
    notes: list[Note] = [note for note in self.notes if note.id > tomorrow]
    cards: list[Card] = [card for card in self.cards if card.id > tomorrow]
    reviews: list[Review] = [review for review in self.reviews if review.id > tomorrow]
    if notes or cards or reviews:
      listing: str = "\n".join(
        f"  {kind}: {', '.join(str(row.id) for row in rows)}"
        for kind, rows in (("notes", notes), ("cards", cards), ("reviews", reviews))
        if rows
      )
      warnings.warn(
        f"Collection: some ids are more than a day in the future. {message}:\n"
        f"{listing}",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  def warn_duplicate_deck_config_names(self) -> Self:
    """Warn about two presets sharing a name.

    Nothing in a collection refers to a preset by its name except the FSRS optimizer.
    Without an explicit search parameter string, a preset is looked up by name.
    Then, all presets with the same name are optimized using the cards of the first
    preset that the db returns. This makes all the scheduling params of the presets
    (except the first) wrong.
    """
    if names := duplicates(
      deck_config.name.casefold() for deck_config in self.deck_configs
    ):
      warnings.warn(
        f"Collection: the deck_configs list has duplicate names. Duplicates: {names}. "
        "Anki finds a preset by name when the FSRS optimizer looks for the cards to "
        "train on. All presets with the same name are optimized using the cards of the "
        "first one the db returns. This makes all the scheduling params of the presets "
        "(except the first) wrong.",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  def warn_missing_deck_configs(self) -> Self:
    """Warn about a normal deck naming a preset the collection does not have.

    When it cannot find a preset, Anki falls back to the default preset, but doesn't
    update the database. `Collection.repair_missing_deck_configs()` points it at preset
    1.

    A deck holding its preset object and a deck naming preset 1 are skipped, since
    `Collection.write_sqlite()` collects both presets.
    """
    deck_config_ids: set[int] = {deck_config.id for deck_config in self.deck_configs}
    deck_config_ids.add(1)  # default deck config
    missing_config: list[str] = []
    for deck in self.decks:
      if (
        deck.config is None
        and isinstance(deck.kind, DeckNormalKind)
        and (deck.kind.config_id not in deck_config_ids)
      ):
        missing_config.append(
          f"  deck {deck.name!r} (id {deck.id}) names preset {deck.kind.config_id}"
        )
    if missing_config:
      decks: str = "\n".join(missing_config)
      warnings.warn(
        "Collection: a normal deck names a preset the collection does not hold. Anki "
        "silently falls back to the `Default` preset (id 1). Use "
        "`genanki2.Collection.repair_missing_deck_configs()` to do the same or add the "
        f"missing preset:\n{decks}",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  def warn_missing_default_deck(
    self, context: SerializationContext | None = None
  ) -> Self:
    """Warn about a collection without deck 1.

    A restored collection without it opens, but Anki cannot study it: every scheduler
    call looks up deck 1 and fails, and the database check does not recreate the deck.
    An apkg import lands in a collection that already has deck 1, so an apkg write gets
    no warning.

    A colpkg file is rejected by `Collection.check_present_default_deck()` and this
    warning is reached for it only when checks are disabled. Without a context, the
    warning says it applies to a colpkg file.

    Args:
      context: Extra information from the serialization process. Default: `None`.
    """
    match context:
      case SerializationContext(output=SerializationContext.Output.APKG):
        return self
      case SerializationContext(output=SerializationContext.Output.COLPKG):
        message: str = "After a restore"
      case _:
        message = "If restored as a collection package"

    if all(deck.id != 1 for deck in self.decks):
      warnings.warn(
        f"Collection: no deck has id 1. {message}, Anki cannot study the collection: "
        'every scheduler call fails with "No such deck", and the database check does '
        "not recreate the deck. Call `Collection.collect_default_deck()` to add the "
        "builtin `Default` deck.",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  def warn_nonleaf_filtered_decks(
    self, context: SerializationContext | None = None
  ) -> Self:
    """Warn about a filtered deck that has subdecks.

    A filtered deck gathers its cards by search and it cannot hold subdecks. An apkg
    import that keeps the filtered deck (needs learning progress ticked) repairs this
    by inserting a normal deck with the same name as the filtered one plus a timestamp
    and then moving the subdecks under it. Without learning progress, the filtered deck
    becomes a normal one and keeps them.

    A colpkg restore keeps them as they are and the database check does not repair
    them. Anki then refuses to rename such a subdeck or to add another deck under the
    filtered one. Without a context, the warning names both outcomes.

    Args:
      context: Extra information from the serialization process. Default: `None`.
    """
    match context:
      case SerializationContext(output=SerializationContext.Output.COLPKG):
        message: str = (
          "A restore keeps them as they are and the database check does not repair "
          "them. Anki then refuses to rename such a subdeck or to add another deck "
          "under the filtered one."
        )
      case SerializationContext(output=SerializationContext.Output.APKG):
        message = (
          "An apkg import with learning progress moves them under a new normal deck "
          "named after the filtered one plus a timestamp. Without learning progress, "
          "the filtered deck becomes a normal one."
        )
      case _:
        message = (
          "If imported from an apkg with learning progress, Anki moves them under a "
          "new normal deck named after the filtered one plus a timestamp. A restored "
          "colpkg keeps them, and Anki then refuses to rename such a subdeck or to add "
          "another deck under the filtered one."
        )

    is_filtered_by_name: dict[str, bool] = {
      deck.name.casefold(): deck.is_filtered() for deck in self.decks
    }
    nested_decks: list[tuple[str, str]] = []
    for deck in self.decks:
      parts: list[str] = deck.name.split(DECK_HUMAN_SEPARATOR)
      for depth in range(len(parts) - 1, 0, -1):
        parent_name: str = DECK_HUMAN_SEPARATOR.join(parts[:depth])
        if is_filtered_by_name.get(parent_name.casefold()):
          nested_decks.append((parent_name, deck.name))
    if nested_decks:
      nesting: str = "\n".join(
        f"  {name!r} under {parent!r}" for parent, name in nested_decks
      )
      warnings.warn(
        f"Collection: a filtered deck cannot hold subdecks. {message} Subdecks under "
        f"a filtered deck:\n{nesting}",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  def warn_filtered_decks_become_normal(
    self, context: SerializationContext | None = None
  ) -> Self:
    """Warn about filtered decks an apkg import turns into normal ones.

    An apkg import with learning progress keeps the package's filtered decks only when
    every card is either in a normal deck with no original deck or references an
    original deck the package holds. Otherwise it turns every filtered deck into a
    normal one and the cards stay in them with their scheduling, taking back their
    original due when they have one. Without learning progress, every filtered deck
    becomes a normal one, no matter what the cards hold.

    Deck 1 counts as present, since a collection always has it. A colpkg restore keeps
    every deck as it is, so a colpkg write gets no warning. Without a context, the
    warning says it applies to an apkg import.

    Args:
      context: Extra information from the serialization process. Default: `None`.
    """
    match context:
      case SerializationContext(output=SerializationContext.Output.COLPKG):
        return self
      case SerializationContext(output=SerializationContext.Output.APKG):
        message: str = "An apkg import with learning progress turns"
      case _:
        message = "If imported from an apkg with learning progress, Anki turns"

    filtered_decks: list[Deck] = [deck for deck in self.decks if deck.is_filtered()]
    if not filtered_decks:
      return self
    deck_ids: set[int] = {deck.id for deck in self.decks}
    offending: list[str] = []
    for card in self.cards:
      if card.original_deck_id == 0 and card.deck.is_filtered():
        offending.append(
          f"  card {card.id} is in filtered deck {card.deck.name!r} and carries no "
          "original deck"
        )
      elif card.original_deck_id > 1 and card.original_deck_id not in deck_ids:
        offending.append(
          f"  card {card.id} names original deck {card.original_deck_id}, which the "
          "collection does not hold"
        )
    if offending:
      names: str = ", ".join(repr(deck.name) for deck in filtered_decks)
      causes: str = "\n".join(offending)
      warnings.warn(
        f"Collection: {message} the filtered decks ({names}) into normal decks, and "
        "their cards stay in them with their scheduling. Anki keeps a filtered deck "
        "only when every card is either in a normal deck (with no original deck) or "
        f"references an original deck that is present:\n{causes}",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  def warn_read_notetype_schemas_mismatch(
    self, context: SerializationContext | None = None
  ) -> Self:
    """Warn about a notetype read from a package whose field or template list changed.

    Adding, removing or reordering a field or a template makes Anki import the
    notetype as a new copy, and then the notes the user already has are not updated,
    unless they tick "Merge note types" when importing. Renaming one is safe while it
    keeps its id.

    Only a collection read from a package knows the lists it was read with, so a
    collection built in code is not compared.

    A colpkg restore replaces the whole collection, notetypes included, so a colpkg
    write gets no warning. Without a context, the warning says it applies to an apkg
    import.

    Args:
      context: Extra information from the serialization process. Default: `None`.
    """
    match context:
      case SerializationContext(output=SerializationContext.Output.COLPKG):
        return self
      case SerializationContext(output=SerializationContext.Output.APKG):
        message: str = (
          "An apkg import adds it as a new copy of the notetype, and the notes users "
          'already have are not updated, unless they tick "Merge note types" when '
          "importing."
        )
      case _:
        message = (
          "If imported from an apkg, Anki adds it as a new copy of the notetype, and "
          'the notes users already have are not updated, unless they tick "Merge note '
          'types" when importing.'
        )

    mismatches: list[tuple[Notetype, _ReadNotetypeSchema, _ReadNotetypeSchema]] = []
    for notetype in self.notetypes:
      read: _ReadNotetypeSchema | None = self._read_notetypes.get(notetype.id)
      current: _ReadNotetypeSchema = _ReadNotetypeSchema.from_notetype(notetype)
      if read is not None and not current.is_match(read):
        mismatches.append((notetype, read, current))
    if mismatches:
      lines: list[str] = []
      for notetype, read, current in mismatches:
        lines.append(f"  {notetype.name!r} (id {notetype.id}):")
        for label, before, after in (
          ("fields", read.fields, current.fields),
          ("templates", read.templates, current.templates),
        ):
          if before != after:
            before_names: list[str] = [item.name for item in before]
            after_names: list[str] = [item.name for item in after]
            lines.append(f"    {label}: {before_names} -> {after_names}")
      listing: str = "\n".join(lines)
      warnings.warn(
        "Collection: a notetype has a different field or template list than the one "
        f"it was read with. {message} Renaming a field or a template is safe only "
        f"while it keeps its id. Adding, removing or reordering one is not:\n{listing}",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  def warn_import_skips_unused(
    self, context: SerializationContext | None = None
  ) -> Self:
    """Warn about a deck or notetype an apkg import never creates.

    Anki's apkg import creates only the decks holding the package's cards and their
    parent decks, and only the notetypes of the package's notes. An empty deck, a
    filtered deck with no cards in it and an unused notetype are written but never
    reach the user (see `Collection.unused_decks` and `Collection.unused_notetypes`).
    The `Default` deck is left out, since every collection has one.

    A colpkg restore keeps every deck and notetype, so a colpkg write gets no warning.
    Without a context, the warning says it applies to an apkg import.

    Args:
      context: Extra information from the serialization process. Default: `None`.
    """
    match context:
      case SerializationContext(output=SerializationContext.Output.COLPKG):
        return self
      case SerializationContext(output=SerializationContext.Output.APKG):
        message: str = (
          "An apkg import creates only the decks holding the cards, with their parent "
          "decks, and only the note types of the notes, so these are written but never "
          "reach the user."
        )
      case _:
        message = (
          "If imported from an apkg, these are written but never reach the user: the "
          "import creates only the decks holding the cards, with their parent decks, "
          "and only the note types of the notes. A restored colpkg keeps all of them."
        )

    unused_decks: list[Deck] = [deck for deck in self.unused_decks if deck.id != 1]
    decks: list[str] = [f'"{deck.name}"' for deck in unused_decks]
    notetypes: list[str] = [f'"{notetype.name}"' for notetype in self.unused_notetypes]
    if decks or notetypes:
      home_deck_ids: set[int] = {card.original_deck_id for card in self.cards}
      home_decks: str = (
        " A home deck of a card in a filtered deck arrives only when the user imports "
        "learning progress."
        if any(deck.id in home_deck_ids for deck in unused_decks)
        else ""
      )
      listing: str = "\n".join(
        f"  {kind}: {', '.join(names)}"
        for kind, names in (("Decks", decks), ("Note types", notetypes))
        if names
      )
      warnings.warn(
        f"Collection: {message}{home_decks}\n{listing}",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  def warn_tag_spelling_mismatch(self) -> Self:
    """Warn about a tag spelled two ways, which Anki settles by registration order.

    Registering a tag takes the spelling already held, by the `tags` table or by the
    note that named it first, and rewrites the note to match. A tag whose ancestor is
    held takes that ancestor's case for its whole prefix, so a note carrying
    `Foo::bar` is rewritten to `FOO::bar` once `FOO` is registered.

    Anki cannot hold two spellings of one tag, so a package carrying both usually
    means it contains two notes disagreeing on tag spelling. Both an import and a
    database check fix it.
    """
    registered: dict[str, str] = {tag.tag.casefold(): tag.tag for tag in self.tags}
    mismatches: list[tuple[int, str, str]] = []
    for note in self.notes:
      for note_tag in note.tags:
        if (spelling := registered.get(note_tag.casefold())) is None:
          spelling = tag_spelling_under_registered_ancestor(note_tag, registered)
          registered[spelling.casefold()] = spelling
        if note_tag != spelling:
          mismatches.append((note.id, note_tag, spelling))
    if mismatches:
      spellings: str = "\n".join(
        f"  note {note_id} carries {note_tag!r}, which Anki writes as {spelling!r}"
        for note_id, note_tag, spelling in mismatches
      )
      warnings.warn(
        "Collection: a note spells a registered tag with different casing. Anki keeps "
        "the spelling it registers first (tags table, then note tags) and rewrites "
        f"every later note to it, prefix included:\n{spellings}",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  def warn_scheduler_version(self, context: SerializationContext | None = None) -> Self:
    """Warn about a `schedVer` config entry not representing the v2 scheduler.

    Anki recognises versions 1 and 2. Anything else, a missing entry included, falls
    back to version 1. Under version 1, if any card is not new, an apkg import rewrites
    the revlog and every review and filtered card into their v2 form before reading
    anything.

    A colpkg file is rejected by `Collection.check_scheduler_version()` and this
    warning is reached for it only when checks are disabled. Without a context, the
    warning names what happens to an apkg and to a colpkg.

    Args:
      context: Extra information from the serialization process. Default: `None`.
    """
    match context:
      case SerializationContext(output=SerializationContext.Output.COLPKG):
        message: str = (
          "A restored collection needs 2: without the entry or on 1, Anki uses the "
          "v1 scheduler, which cannot study until the user accepts an update that "
          "rewrites the review history, and other values stop Anki from opening it."
        )
      case SerializationContext(output=SerializationContext.Output.APKG):
        if all(card.queue == 0 for card in self.cards):
          return self
        message = (
          "Anki reads any value other than 2 as the v1 scheduler, and some cards are "
          "not new, so the import rewrites the revlog and every review and filtered "
          "card into their v2 form."
        )
      case _:
        message = (
          "Anki reads any value other than 2 as the v1 scheduler. If any card is not "
          "new, an apkg import rewrites the revlog and every review and filtered card "
          "into their v2 form. A restored colpkg cannot study until the user accepts "
          "an update that does the same, or does not open at all for values other than 1."
        )

    scheduler_version: int | str = self._scheduler_version()
    if scheduler_version == 2:
      return self

    warnings.warn(
      f"Collection: the `schedVer` config entry is {scheduler_version}. {message} "
      'Set it with `ConfigEntry(key="schedVer", value=b"2")`.',
      Genanki2Warning,
      stacklevel=2,
    )
    return self

  def warn_v3_scheduler_off(self, context: SerializationContext | None = None) -> Self:
    """Warn about a collection on the v2 scheduler with the v3 scheduler switched off.

    Anki studies with the v3 scheduler, a mode of v2 that the `sched2021` config entry
    switches on. With the entry missing or holding anything other than `true`, a
    restored collection cannot be studied until the user accepts the scheduler update
    Anki offers. The update only switches the entry on and changes no reviews.

    An apkg import does not carry config entries, so an apkg write gets no warning.
    Without a context, the warning says it applies to a colpkg file. A `schedVer` other
    than 2 is left to `Collection.check_scheduler_version()` and
    `Collection.warn_scheduler_version()`.

    Args:
      context: Extra information from the serialization process. Default: `None`.
    """
    match context:
      case SerializationContext(output=SerializationContext.Output.APKG):
        return self
      case SerializationContext(output=SerializationContext.Output.COLPKG):
        message: str = "After a restore"
      case _:
        message = "If restored as a collection package"

    if self._scheduler_version() == 2 and (
      (scheduler_entry := self.get_config_entry("sched2021")) is None
      # `is not True` matches Rust's JSON parsing
      or scheduler_entry.value_or() is not True
    ):
      warnings.warn(
        "Collection: the `sched2021` config entry is missing or does not hold `true`, "
        f"so the v3 scheduler is off. {message}, nothing can be studied until the "
        "user accepts the scheduler update Anki offers (which changes no reviews). "
        'Set it with `ConfigEntry(key="sched2021", value=b"true")`.',
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  @override
  def warn(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Warn about collection data Anki repairs in unwanted ways.

    Warnings can be turned off using `with genanki2.warnings_disabled():`.

    Args:
      recursive: Whether to warn about every model reachable from this one too, each
        once. If false, it only runs this model's own warnings. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.warn_future_ids(context)
    self.warn_duplicate_deck_config_names()
    self.warn_missing_deck_configs()
    self.warn_missing_default_deck(context)
    self.warn_nonleaf_filtered_decks(context)
    self.warn_filtered_decks_become_normal(context)
    self.warn_read_notetype_schemas_mismatch(context)
    self.warn_import_skips_unused(context)
    self.warn_tag_spelling_mismatch()
    self.warn_scheduler_version(context)
    self.warn_v3_scheduler_off(context)
    return super().warn(recursive=recursive, context=context)

  # ---------------------------------- Serialization -----------------------------------

  @classmethod
  def from_zipfile(
    cls,
    zfile: zipfile.ZipFile,
    version: PackageMetadata.Version,
    extract_dir: str | Path | None = None,
    overwrite: bool = True,
  ) -> Self:
    """Read the collection `zfile` holds, and copy it to `extract_dir` if given.

    The copy survives the call in case the collection raises.

    Args:
      zfile: The package to read.
      version: Envelope version.
      extract_dir: A directory where to copy the database. Default: `None`.
      overwrite: Whether writing over an existing file is accepted. Default: `True`.

    Raises:
      PackageReadError: the package does not carry the collection's entry.
      PackageReadError: bad zip file.
      PackageReadError: failed zstd decompression.
      Genanki2Error: file exists and `overwrite` is false, or `extract_dir` cannot be
        written into.
      OSError: a write to `extract_dir` failed.
      CollectionReadError: the entry read out of the package is not a collection
        genanki2 can read.
      CollectionReadError: the fields or templates of a notetype do not have ordinals
        running contiguously from 0.
      pydantic.ValidationError: a value in the collection failed some field annotation
        check.
    """
    collection_entry: str = version.collection_entry
    if collection_entry not in zfile.namelist():
      raise PackageReadError(
        f'Missing package entries: the package carries no "{collection_entry}" entry, '
        "which is the expected entry in the detected envelope."
      )
    with tempfile.TemporaryDirectory() as tmp:
      sqlite_path: Path = Path(tmp) / collection_entry
      try:
        sqlite_bytes: bytes = zfile.read(collection_entry)
        if version.compressed_entries:
          sqlite_bytes = zstd_decompress(sqlite_bytes)
        sqlite_path.write_bytes(sqlite_bytes)
        if extract_dir is not None:
          extraction_path: Path = prepare_writing_file(
            Path(extract_dir) / collection_entry, overwrite
          )
          shutil.copy2(sqlite_path, extraction_path)
        collection: Self = cls.read_sqlite(sqlite_path)
      except (zipfile.BadZipFile, zstd.ZstdError) as error:
        raise PackageReadError(
          "Corrupt package entries: cannot read the collection stored as entry "
          f'"{collection_entry}". Error: {error}'
        ) from error
      except sqlite3.Error as error:
        raise CollectionReadError(
          f'Cannot read the collection: entry "{collection_entry}" came out of the '
          f"package intact, but it is not a collection genanki2 can read. Error: {error}"
        ) from error
    return collection

  @classmethod
  def from_apkg(cls, path: str | Path) -> Self:
    """Read the collection inside the package at `path`.

    Media files are not read or extracted.

    Raises:
      PackageReadError: the file is not a package genanki2 can read, or its collection
        entry is missing or corrupt.
      CollectionReadError: the entry read out of the package is not a collection
        genanki2 can read.
      CollectionReadError: the fields or templates of a notetype do not have ordinals
        running contiguously from 0.
      pydantic.ValidationError: a value in the collection failed some field annotation
        check.
      OSError: the file cannot be opened.
    """
    with open_apkg(path) as apkg_file:
      version: PackageMetadata.Version = PackageMetadata.zipfile_version(apkg_file)
      version.raise_if_unknown(path)
      return cls.from_zipfile(apkg_file, version)

  def _parse_notetypes(self, db: AnkiSQLite) -> dict[int, Notetype]:
    """Build the notetypes from the three tables they span, and index them by id.

    Fields or templates referencing a missing notetype are kept as raw rows in
    `orphan_fields` or `orphan_templates`.

    Skeleton schemas of the read notetypes are saved in a private attribute for the
    writer's reference.

    Returns:
      The notetypes, indexed by id.
    """
    T: type[AnkiSQLite.Table] = AnkiSQLite.Table
    ntid_key: operator.itemgetter[str] = operator.itemgetter("ntid")

    fields_idx: dict[int, FieldRows] = {
      ntid: list(group)
      for ntid, group in it.groupby(db.read_table(T.FIELDS), key=ntid_key)
    }

    templates_idx: dict[int, TemplateRows] = {
      ntid: list(group)
      for ntid, group in it.groupby(db.read_table(T.TEMPLATES), key=ntid_key)
    }

    for notetype_row in db.read_table(T.NOTETYPES):
      fields: FieldRows = fields_idx.get(notetype_row["id"], [])
      templates: TemplateRows = templates_idx.get(notetype_row["id"], [])
      self.notetypes.append(Notetype.from_rows(notetype_row, fields, templates))
    self._read_notetypes = frozendict(
      {
        notetype.id: _ReadNotetypeSchema.from_notetype(notetype)
        for notetype in self.notetypes
      }
    )

    notetypes_idx: dict[int, Notetype] = {
      notetype.id: notetype for notetype in self.notetypes
    }
    for ntid in fields_idx.keys() - notetypes_idx.keys():
      self.orphan_fields.extend(fields_idx[ntid])
    for ntid in templates_idx.keys() - notetypes_idx.keys():
      self.orphan_templates.extend(templates_idx[ntid])

    return notetypes_idx

  @classmethod
  def read_sqlite(cls, path: str | Path) -> Self:
    """Read a collection from the SQLite file at `path`.

    A row referencing another row that the collection does not hold goes to the
    matching `orphan_` list as the raw row because a model cannot be built without it.
    Anki deletes such a row during a database check.

    Raises:
      Genanki2Error: `path` is missing or is not a file.
      sqlite3.Error: the database cannot be read.
      CollectionReadError: the database does not contain a table or it is missing one
        or more columns from the collection schema.
      CollectionReadError: the `col` table does not hold exactly one row or declares a
        schema version other than 18.
      CollectionReadError: the fields or templates of a notetype do not have ordinals
        running contiguously from 0.
      pydantic.ValidationError: a value in the collection failed some field annotation
        check.
    """
    path = prepare_reading_file(path)
    T: type[AnkiSQLite.Table] = AnkiSQLite.Table
    collection: Self = cls()
    with AnkiSQLite(path) as db:
      collection.metadata = CollectionMetadata.from_sqlite(db)
      collection.config = [ConfigEntry.from_row(row) for row in db.read_table(T.CONFIG)]
      notetypes_idx: dict[int, Notetype] = collection._parse_notetypes(db)

      for note_row in db.read_table(AnkiSQLite.Table.NOTES):
        if (notetype := notetypes_idx.get(note_row["mid"])) is not None:
          collection.notes.append(Note.from_row(note_row, notetype))
        else:
          collection.orphan_notes.append(note_row)
      notes_idx: dict[int, Note] = {note.id: note for note in collection.notes}

      collection.tags = [Tag.from_row(row) for row in db.read_table(T.TAGS)]

      collection.deck_configs = [
        DeckConfig.from_row(row) for row in db.read_table(T.DECK_CONFIG)
      ]
      deck_configs_idx: dict[int, DeckConfig] = {
        deck_config.id: deck_config for deck_config in collection.deck_configs
      }

      collection.decks = [Deck.from_row(row) for row in db.read_table(T.DECKS)]
      for deck in collection.decks:
        if isinstance(deck.kind, DeckNormalKind):
          deck.config = deck_configs_idx.get(deck.kind.config_id)
      decks_idx: dict[int, Deck] = {deck.id: deck for deck in collection.decks}

      for card_row in db.read_table(T.CARDS):
        note: Note | None = notes_idx.get(card_row["nid"])
        deck: Deck | None = decks_idx.get(card_row["did"])
        if note is not None and deck is not None:
          collection.cards.append(Card.from_row(card_row, note, deck))
        else:
          collection.orphan_cards.append(card_row)
      cards_idx: dict[int, Card] = {card.id: card for card in collection.cards}

      for review_row in db.read_table(T.REVLOG):
        if (card := cards_idx.get(review_row["cid"])) is not None:
          collection.reviews.append(Review.from_row(review_row, card))
        else:
          collection.orphan_reviews.append(review_row)

      collection.graves = [Grave.from_row(row) for row in db.read_table(T.GRAVES)]

    return collection

  def _sqlite(self, path: str | Path) -> Self:
    """Write the collection as a new SQLite database at `path`, running no phase.

    Every model writes its row without being judged again, and the orphan rows are
    written back as they were read.
    """
    T: type[AnkiSQLite.Table] = AnkiSQLite.Table
    with AnkiSQLite.create(path) as db:
      db.insert_table(T.COL, [self.metadata._row()])
      db.insert_table(T.CONFIG, [entry._row() for entry in self.config])

      db.insert_table(
        T.DECK_CONFIG, [deck_config._row() for deck_config in self.deck_configs]
      )
      db.insert_table(T.DECKS, [deck._row() for deck in self.decks])

      for notetype in self.notetypes:
        notetype_row, field_rows, template_rows = notetype._rows()
        db.insert_table(T.FIELDS, field_rows)
        db.insert_table(T.TEMPLATES, template_rows)
        db.insert_table(T.NOTETYPES, [notetype_row])

      db.insert_table(T.NOTES, [note._row() for note in self.notes])
      db.insert_table(T.TAGS, [tag._row() for tag in self.tags])
      db.insert_table(T.CARDS, [card._row() for card in self.cards])
      db.insert_table(T.REVLOG, [review._row() for review in self.reviews])
      db.insert_table(T.GRAVES, [grave._row() for grave in self.graves])

      # Orphans
      db.insert_table(T.FIELDS, self.orphan_fields)
      db.insert_table(T.TEMPLATES, self.orphan_templates)
      db.insert_table(T.NOTES, self.orphan_notes)
      db.insert_table(T.CARDS, self.orphan_cards)
      db.insert_table(T.REVLOG, self.orphan_reviews)

    return self

  def write_sqlite(
    self,
    path: str | Path,
    overwrite: bool = True,
    *,
    context: SerializationContext | None = None,
  ) -> Self:
    """Check the collection, write it at `path` and return the collection.

    The collection is checked before anything is written. The database is built in a
    temporary file next to `path` and moved onto it once complete, so a failed write
    leaves an existing file unchanged.

    A context's output attribute is replaced with `SerializationContext.Output.SQLITE`.
    If no context is given, one is created and passed to the phases.

    Args:
      path: Location and file name where to write the database.
      overwrite: Whether writing over an existing file is accepted. Default: `True`.
      context: Extra information from the serialization process, passed to the phases.
        Default: `None`.

    Raises:
      Genanki2Error: `path` is a directory, it exists and `overwrite` is false, or its
        directory cannot be written into.
      Genanki2Error: the collection fails a check.
      OSError: `path` cannot be written.
    """
    context = dc.replace(
      context or SerializationContext(), output=SerializationContext.Output.SQLITE
    )
    self._run_phases(context=context)
    with atomic_replace(path, overwrite) as temp_path:
      return self._sqlite(temp_path)
