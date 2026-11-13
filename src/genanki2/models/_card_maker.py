import random
import warnings
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from genanki2.builtins import DEFAULT_DECK, DEFAULT_DECK_CONFIG
from genanki2.errors import Genanki2Error, Genanki2Warning
from genanki2.models.card import Card, CardType
from genanki2.models.deck import Deck, DeckNormalKind
from genanki2.models.deck_config import DeckConfig, DeckConfigInner
from genanki2.models.note import Note
from genanki2.models.notetype import Notetype
from genanki2.models.template import Template
from genanki2.utils import uint32

if TYPE_CHECKING:
  from genanki2.models.collection import Collection


class CardMaker:
  """Producer of cards that Anki generates when a note is added to a collection.

  The maker reads the collection's decks and presets. It is highly recommended to
  collect decks and deck configs before creating cards.

  It is free of side effects on the collection: it advances its own `next_pos`, and
  writing the counter back and storing the cards is left to the collection.

  Whether a deck inserts new cards at random is looked up once per deck and
  remembered, so a preset changed afterwards goes unnoticed by the same maker.

  Attributes:
    collection: The collection for which the cards are built. Its decks and presets are
      needed.
    next_pos: The position for the next note without one. For a card inserted in order,
      it is its position. For one inserted at random, it seeds the random number
      generator. The collection sets it from its counter, `CardMaker.make()` advances
      it, and the collection writes it back.
    random_due_cache: Whether each deck, by id, inserts new cards at random.
  """

  def __init__(self, collection: "Collection", next_pos: int) -> None:
    """Prepare to make cards for notes added to `collection`."""
    self.collection: Collection = collection
    self.next_pos: int = next_pos
    self.random_due_cache: dict[int, bool] = {}

  ###### In which deck?

  def unique_home_deck_or(
    self, cards: Iterable[Card], default: Deck | None = None
  ) -> Deck | None:
    """Return the deck to which all of a note's `cards` belong or `default`.

    If a unique home deck is found and is normal, it is returned, otherwise `None`
    (meaning to use deck id=1). `default` applies only when `cards` is empty or they
    belong to several decks.
    """
    candidates: set[int] = {card.original_deck_id or card.deck.id for card in cards}
    if len(candidates) == 1:
      target_deck: Deck | None = self.collection.get_deck(id=candidates.pop())
      if target_deck is not None and target_deck.is_normal():
        return target_deck
      return None
    return default

  def template_target_deck_or(
    self, template: Template, default: Deck | None = None
  ) -> Deck | None:
    """Return the deck with which `template` overrides the card's deck or `default`.

    An override referencing a missing or filtered deck returns `None` (meaning to use
    deck id=1) and emits a warning. `default` applies only when the template has no
    override.
    """
    if (target_deck_id := template.config.target_deck_id) > 0:
      target_deck: Deck | None = self.collection.get_deck(id=target_deck_id)
      if target_deck is not None and target_deck.is_normal():
        return target_deck
      instead_of: str = f' instead of "{default.name}"' if default is not None else ""
      warnings.warn(
        f'Template "{template.name}" overrides the deck with id {target_deck_id}, '
        "which is not in the collection or is a filtered deck. Like Anki, the card "
        f"goes to the Default deck{instead_of}. Point the override at a normal deck "
        "in the collection or clear it with `Template.repair_target_deck_id()`.",
        Genanki2Warning,
        stacklevel=5,
      )
      return None
    return default

  def deck_or_default(self, deck: Deck | None = None) -> Deck:
    """Return `deck` when it is a normal deck, otherwise the Default deck.

    The Default deck is the collection's deck 1 or `DEFAULT_DECK` when the collection
    doesn't have it.

    Raises:
      Genanki2Error: the collection's deck 1 is a filtered deck.
    """
    if deck is not None and deck.is_normal():
      return deck
    deck = self.collection.get_deck(id=1)
    if deck is not None:
      if deck.is_filtered():
        raise Genanki2Error(
          f'Collection: deck 1 ("{deck.name}") is a filtered deck, so the cards that '
          "go to the Default deck have nowhere to go. Anki refuses to add cards in "
          "that state. Make deck 1 a normal deck."
        )
      return deck
    return DEFAULT_DECK

  ###### In which position?

  def is_due_random(self, deck: Deck) -> bool:
    """Tell whether `deck`'s preset inserts new cards at random positions.

    The preset is the one the deck holds, or else the one its `config_id` names in
    `deck_configs`. When neither exists, Anki uses the Default preset: preset 1 if the
    collection has it, otherwise its builtin default (here
    `genanki2.DEFAULT_DECK_CONFIG`), which inserts in order. Presets held only by other
    decks are found only after `Collection.collect_deck_configs()`.
    """
    deck_config: DeckConfig | None = deck.config
    if deck_config is None and isinstance(deck.kind, DeckNormalKind):
      deck_config = self.collection.get_deck_config(id=deck.kind.config_id)
    if deck_config is None:
      deck_config = self.collection.get_deck_config(id=1)
    if deck_config is None:
      deck_config = DEFAULT_DECK_CONFIG
    RANDOM = DeckConfigInner.NewCardInsertOrder.RANDOM
    return deck_config.config.new_card_insert_order == RANDOM

  def get_random_due(self) -> int:
    """Draw a random new-card position.

    Like Anki, the draw is seeded with `next_pos`, so one counter value always gives
    one position, and falls between 1 and `max(next_pos, 1000) - 1`. The numbers differ
    from Anki's, since its random generator is not Python's.
    """
    return random.Random(self.next_pos).randrange(1, max(self.next_pos, 1000))

  def existing_position(self, cards: Iterable[Card]) -> int | None:
    """Return the position of the new card with the lowest id in `cards`, or `None`.

    A new card in a filtered deck keeps its position in `original_due`. A negative
    position counts as 0, as Anki stores it.
    """
    new_cards = (card for card in cards if card.card_type == CardType.NEW)
    card: Card | None = min(new_cards, key=lambda c: c.id, default=None)
    return max(card.original_due or card.due, 0) if card is not None else None

  ###### Which ordinals?

  def missing_ordinals(self, note: Note, cards: Iterable[Card]) -> tuple[int, ...]:
    """Return the ordinals in `Note.card_ordinals` that no `cards` have.

    A normal notetype with exactly a card for each template is skipped.
    """
    card_ordinals: set[int] = {card.template_ord for card in cards}
    notetype: Notetype = note.notetype
    if notetype.is_normal() and card_ordinals == set(range(len(notetype.templates))):
      return ()
    return tuple(
      ordinal for ordinal in note.card_ordinals if ordinal not in card_ordinals
    )

  ###### Factory methods

  def make_card(
    self,
    note: Note,
    ordinal: int,
    deck: Deck | None = None,
    *,
    due: int | None = None,
    override: bool = True,
  ) -> Card:
    """Make `note`'s card for the template or cloze number at `ordinal`.

    The card goes in the deck overridden by the template when the notetype is normal
    and `override` is true, or `deck` if provided, or the Default deck. Cloze
    cards ignore overrides, as in Anki. Its position is `due` if provided, otherwise
    `CardMaker.next_pos`, or a position drawn from it when the deck's preset inserts
    new cards at random.
    """
    target_deck: Deck | None = deck
    if override and note.notetype.is_normal():
      template: Template = note.notetype.templates[ordinal]
      target_deck = self.template_target_deck_or(template, deck)
    target_deck = self.deck_or_default(target_deck)
    if due is None:
      if target_deck.id not in self.random_due_cache:
        self.random_due_cache[target_deck.id] = self.is_due_random(target_deck)
      is_due_random: bool = self.random_due_cache[target_deck.id]
      due = self.get_random_due() if is_due_random else self.next_pos
    if due >= 2_000_000:  # Repair due
      due = 1_000_000 + due % 1_000_000
    return Card(note=note, deck=target_deck, template_ord=ordinal, due=due)

  def make_card0(self, note: Note, deck: Deck) -> Card:
    """Make card 0 in `deck` for a note that gets no cards and emit a warning.

    The template's deck override does not apply, as in Anki.
    """
    reason: str = "no template front side renders with its fields and tags"
    if note.notetype.is_cloze():
      reason = "none of its fields holds a cloze"
    warnings.warn(
      f"Note: guid={note.guid} gets no card from notetype {note.notetype.name!r} "
      f'because {reason}. Like Anki, it gets card 0 in "{deck.name}" anyway, which '
      "shows an error instead of a question when studied. Fill the fields its cards "
      "need, or leave the note out.",
      Genanki2Warning,
      stacklevel=4,
    )
    return self.make_card(note, ordinal=0, deck=deck, override=False)

  ###### Orchestration over factory

  def make(
    self, note: Note, existing_cards: Sequence[Card] = (), deck: Deck | None = None
  ) -> tuple[Card, ...]:
    """Make the cards Anki generates for `note` that `existing_cards` do not cover.

    There is a card per ordinal in `CardMaker.missing_ordinals()`. They go in the deck
    their template overrides, else in the deck all `existing_cards` belong to, else in
    `deck`, else in the Default deck. They share the position of an existing new card
    when there is one, otherwise `CardMaker.next_pos`, which then advances. A note
    without cards that would not produce any still gets card 0, as in Anki, and emits a
    warning.

    When no `existing_cards` are given, it returns all the cards a note produces.
    """
    ordinals: tuple[int, ...] = self.missing_ordinals(note, existing_cards)
    deck = self.unique_home_deck_or(existing_cards, deck)
    due: int | None = self.existing_position(existing_cards)
    cards: list[Card] = []
    # Keep the loop until Python 3.12: comprehensions and generators add a stack frame
    # that moves where warnings point.
    for ordinal in ordinals:
      cards.append(self.make_card(note, ordinal, deck, due=due))
    if not existing_cards and not ordinals:
      cards = [self.make_card0(note, self.deck_or_default(deck))]
    if cards and due is None:
      self.next_pos = uint32(self.next_pos + 1)
    return tuple(cards)
