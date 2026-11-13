"""Does `Collection.add_notes` give a note the cards Anki gives it when it is added?

    uv run --with pytest pytest tests/test_add_notes.py

Anki adds a note in `add_note_inner` (`rslib/src/notes/mod.rs`) and makes its cards in
`add_cards_for_new_note` (`rslib/src/notetype/cardgen.rs`). Which ordinals a note gets is
covered against Anki in `test_card_ordinals_match_anki.py`. This file covers what
happens around them:

- the deck each card goes in: the one passed, a template's override, or the Default
  deck when the override names a deck that is missing or filtered (`deck_for_adding`);
- card 0 for a note that gets no card, in the deck passed (`cardgen.rs:107-115`);
- the new-card position, shared by a note's cards and advanced once per note
  (`get_and_update_next_card_position`, `rslib/src/config/mod.rs:200`), or drawn from
  it when the deck's preset inserts new cards at random;
- what genanki2 refuses where Anki would not: a filtered deck, which Anki swaps for the
  Default deck, and a note added twice.

The models carry no anki dependency and neither does this file.
"""

import re

import pytest

from genanki2.builtins import (
  BASIC_AND_REVERSED_NOTETYPE,
  BASIC_NOTETYPE,
  CLOZE_NOTETYPE,
  DEFAULT_DECK,
  builtin_notetype,
  default_config_entries,
  default_deck,
  default_deck_config,
)
from genanki2.errors import Genanki2Error
from genanki2.models.card import Card, CardQueue, CardType
from genanki2.models.collection import Collection
from genanki2.models.config import ConfigEntry
from genanki2.models.deck import Deck, DeckFilteredKind
from genanki2.models.deck_config import DeckConfig, DeckConfigInner
from genanki2.models.note import Note
from genanki2.models.notetype import Notetype
from genanki2.models.review import Review, ReviewRating, ReviewType

RANDOM = DeckConfigInner.NewCardInsertOrder.RANDOM


def a_collection(*decks: Deck) -> Collection:
  """A collection holding `decks`, whose new-card counter starts at 1."""
  return Collection(config=default_config_entries(), decks=list(decks))


def a_collection_with_counter(value: object) -> Collection:
  """A collection whose `nextPos` entry holds `value`, or has none for `None`."""
  config = [entry for entry in default_config_entries() if entry.key != "nextPos"]
  if value is not None:
    entry = ConfigEntry(key="nextPos")
    entry.set_value(value)
    config.append(entry)
  return Collection(config=config)


def stored_counter(collection: Collection) -> object:
  entry = collection.get_config_entry("nextPos")
  assert entry is not None
  return entry.value_or()


def a_basic_note(front: str = "front", back: str = "back") -> Note:
  return Note(notetype=BASIC_NOTETYPE, fields=[front, back])


def a_notetype_overriding_the_deck(name: str, deck_id: int) -> Notetype:
  """`name`'s builtin with its first template sending cards to `deck_id`."""
  notetype = builtin_notetype(name, id=1_000_000 + deck_id % 1_000_000)
  notetype.templates[0].config.target_deck_id = deck_id
  return notetype


def a_random_preset() -> DeckConfig:
  preset = DeckConfig(name="Random")
  preset.config.new_card_insert_order = RANDOM
  return preset


def placement(cards: tuple[Card, ...]) -> list[tuple[int, str]]:
  return [(card.template_ord, card.deck.name) for card in cards]


# ------------------------------- The cards a note gets --------------------------------


def test_a_normal_note_gets_a_card_per_template_whose_front_renders():
  deck = Deck(name="Chosen")
  note = Note(notetype=BASIC_AND_REVERSED_NOTETYPE, fields=["front", "back"])
  (cards,) = a_collection(deck).add_notes([note], deck)
  assert placement(cards) == [(0, "Chosen"), (1, "Chosen")]


def test_a_cloze_note_gets_a_card_per_cloze_number():
  deck = Deck(name="Chosen")
  note = Note(notetype=CLOZE_NOTETYPE, fields=["{{c1::a}} {{c3::b}}", ""])
  (cards,) = a_collection(deck).add_notes([note], deck)
  assert placement(cards) == [(0, "Chosen"), (2, "Chosen")]


def test_the_cards_come_back_per_note_in_the_order_given():
  deck = Deck(name="Chosen")
  notes = [a_basic_note(front) for front in ["one", "two", "three"]]
  all_cards = a_collection(deck).add_notes(notes, deck)
  assert [cards[0].note for cards in all_cards] == notes


def test_the_cards_are_stored_and_the_notes_are_collected_later():
  deck = Deck(name="Chosen")
  collection = a_collection(deck)
  note = a_basic_note()
  (cards,) = collection.add_notes([note], deck)
  assert collection.cards == list(cards)
  assert collection.notes == []
  assert collection.collect_notes().notes == [note]


def test_a_generator_of_notes_is_added_whole():
  """The checks read the notes before the cards are made, so a one-shot iterable has
  to be read once and kept."""
  deck = Deck(name="Chosen")
  collection = a_collection(deck)
  all_cards = collection.add_notes((a_basic_note(str(n)) for n in range(3)), deck)
  assert len(all_cards) == 3
  assert len(collection.cards) == 3


# ---------------------------------- Deck overrides ------------------------------------


def test_a_template_override_takes_the_card_to_its_deck():
  chosen, override = Deck(name="Chosen"), Deck(name="Override")
  notetype = a_notetype_overriding_the_deck(BASIC_NOTETYPE.name, override.id)
  note = Note(notetype=notetype, fields=["front", "back"])
  (cards,) = a_collection(chosen, override).add_notes([note], chosen)
  assert placement(cards) == [(0, "Override")]


def test_an_override_on_one_template_leaves_the_others_in_the_deck_passed():
  chosen, override = Deck(name="Chosen"), Deck(name="Override")
  notetype = a_notetype_overriding_the_deck(
    BASIC_AND_REVERSED_NOTETYPE.name, override.id
  )
  note = Note(notetype=notetype, fields=["front", "back"])
  (cards,) = a_collection(chosen, override).add_notes([note], chosen)
  assert placement(cards) == [(0, "Override"), (1, "Chosen")]


def test_a_cloze_card_ignores_the_override():
  """Anki reads `target_deck_id` only for normal notetypes (`cardgen.rs:63-70`)."""
  chosen, override = Deck(name="Chosen"), Deck(name="Override")
  notetype = a_notetype_overriding_the_deck(CLOZE_NOTETYPE.name, override.id)
  note = Note(notetype=notetype, fields=["{{c1::a}}", ""])
  (cards,) = a_collection(chosen, override).add_notes([note], chosen)
  assert placement(cards) == [(0, "Chosen")]


def test_an_override_deck_reached_only_through_a_card_is_found():
  """`add_notes` collects the decks first, so a deck no list holds yet still counts."""
  chosen, override = Deck(name="Chosen"), Deck(name="Override")
  collection = a_collection(chosen)
  collection.cards.append(Card(note=a_basic_note("held"), deck=override))
  notetype = a_notetype_overriding_the_deck(BASIC_NOTETYPE.name, override.id)
  note = Note(notetype=notetype, fields=["front", "back"])
  (cards,) = collection.add_notes([note], chosen)
  assert placement(cards) == [(0, "Override")]


@pytest.mark.parametrize("filtered", [False, True], ids=["missing", "filtered"])
def test_an_override_naming_no_normal_deck_sends_the_card_to_the_default_deck(
  filtered: bool,
):
  """`deck_for_adding` falls back to deck 1 for a missing or filtered deck."""
  chosen, default = Deck(name="Chosen"), default_deck()
  target = Deck(name="Filtered", kind=DeckFilteredKind())
  collection = a_collection(chosen, default, *([target] if filtered else []))
  notetype = a_notetype_overriding_the_deck(BASIC_NOTETYPE.name, target.id)
  note = Note(notetype=notetype, fields=["front", "back"])
  with pytest.warns(UserWarning, match=f"deck with id {target.id}") as reported:
    (cards,) = collection.add_notes([note], chosen)
  assert len(reported) == 1
  assert cards[0].deck is default


def test_without_a_deck_1_the_card_goes_to_the_builtin_default_deck():
  chosen = Deck(name="Chosen")
  notetype = a_notetype_overriding_the_deck(BASIC_NOTETYPE.name, 999)
  note = Note(notetype=notetype, fields=["front", "back"])
  with pytest.warns(UserWarning, match="deck with id 999"):
    (cards,) = a_collection(chosen).add_notes([note], chosen)
  assert cards[0].deck is DEFAULT_DECK


# -------------------------------- A note with no card ---------------------------------


def test_a_normal_note_with_no_card_gets_card_0_in_the_deck_passed():
  """Not in the override deck: the fallback uses the deck the note was added to
  (`cardgen.rs:107-115`), checked against Anki 26.08."""
  chosen, override = Deck(name="Chosen"), Deck(name="Override")
  notetype = a_notetype_overriding_the_deck(BASIC_NOTETYPE.name, override.id)
  note = Note(notetype=notetype, fields=["", "back"])
  with pytest.warns(UserWarning, match="no template front side renders"):
    (cards,) = a_collection(chosen, override).add_notes([note], chosen)
  assert placement(cards) == [(0, "Chosen")]


def test_a_cloze_note_with_no_cloze_gets_card_0_with_a_warning():
  deck = Deck(name="Chosen")
  note = Note(notetype=CLOZE_NOTETYPE, fields=["no cloze here", ""])
  with pytest.warns(UserWarning, match="none of its fields holds a cloze"):
    (cards,) = a_collection(deck).add_notes([note], deck)
  assert placement(cards) == [(0, "Chosen")]


def test_the_warning_names_the_note_and_the_deck():
  deck = Deck(name="Chosen")
  note = a_basic_note(front="")
  with pytest.warns(UserWarning) as reported:
    a_collection(deck).add_notes([note], deck)
  assert len(reported) == 1
  message = str(reported[0].message)
  assert note.guid in message
  assert '"Chosen"' in message


def test_a_note_that_gets_its_cards_is_not_warned_about(recwarn):
  deck = Deck(name="Chosen")
  a_collection(deck).add_notes([a_basic_note()], deck)
  assert len(recwarn) == 0


# ----------------------------- Where the warnings point -------------------------------


def a_note_overriding_a_missing_deck() -> Note:
  notetype = a_notetype_overriding_the_deck(BASIC_NOTETYPE.name, 999)
  return Note(notetype=notetype, fields=["f", "b"])


@pytest.mark.parametrize(
  "make_note", [a_note_overriding_a_missing_deck, lambda: a_basic_note(front="")]
)
def test_a_warning_points_at_the_line_that_added_the_notes(make_note):
  deck = Deck(name="Chosen")
  collection = a_collection(deck)
  note = make_note()
  with pytest.warns(UserWarning) as reported:
    collection.add_notes([note], deck)
  assert reported[0].filename == __file__


# --------------------------------- New-card position ----------------------------------


def test_the_cards_of_one_note_share_its_position():
  deck = Deck(name="Chosen")
  note = Note(notetype=BASIC_AND_REVERSED_NOTETYPE, fields=["front", "back"])
  (cards,) = a_collection(deck).add_notes([note], deck)
  assert [card.due for card in cards] == [1, 1]


def test_each_note_takes_the_next_position_and_the_counter_advances_once_per_note():
  deck = Deck(name="Chosen")
  collection = a_collection(deck)
  notes = [
    Note(notetype=BASIC_AND_REVERSED_NOTETYPE, fields=[front, "back"])
    for front in ["one", "two", "three"]
  ]
  all_cards = collection.add_notes(notes, deck)
  assert [[card.due for card in cards] for cards in all_cards] == [
    [1, 1],
    [2, 2],
    [3, 3],
  ]
  assert stored_counter(collection) == 4
  assert collection.next_new_card_position == 4


def test_a_second_call_continues_from_where_the_first_left_the_counter():
  deck = Deck(name="Chosen")
  collection = a_collection(deck)
  collection.add_notes([a_basic_note("one")], deck)
  (cards,) = collection.add_notes([a_basic_note("two")], deck)
  assert cards[0].due == 2


def a_collection_holding(counter: int, *cards: Card) -> Collection:
  """A collection whose counter is `counter`, holding `cards` built by hand."""
  collection = a_collection_with_counter(counter)
  collection.cards.extend(cards)
  return collection


def a_new_card(due: int, deck: Deck) -> Card:
  return Card(note=a_basic_note(f"held at {due}"), deck=deck, due=due)


@pytest.mark.parametrize("counter", [1, 5], ids=["below", "at"])
def test_a_counter_behind_the_existing_new_cards_is_reported(counter: int):
  """Anki uses the counter as it finds it, so the added notes take positions existing
  new cards hold, and keep them through an apkg import, which leaves new-card
  positions alone (`rslib/src/import_export/package/apkg/import/cards.rs:181`)."""
  deck = Deck(name="Chosen")
  collection = a_collection_holding(counter, a_new_card(3, deck), a_new_card(5, deck))
  match = rf"counter \({counter}\).*holds \(5\)"
  with pytest.warns(UserWarning, match=match) as reported:
    (cards,) = collection.add_notes([a_basic_note()], deck)
  assert len(reported) == 1
  assert reported[0].filename == __file__
  assert cards[0].due == counter


@pytest.mark.parametrize("counter", [6, 50], ids=["next", "gap"])
def test_a_counter_past_the_existing_new_cards_is_not_reported(recwarn, counter: int):
  """A counter ahead of the cards leaves a gap, which Anki leaves too, for example
  after a note is deleted."""
  deck = Deck(name="Chosen")
  collection = a_collection_holding(counter, a_new_card(3, deck), a_new_card(5, deck))
  collection.add_notes([a_basic_note()], deck)
  assert len(recwarn) == 0


def test_only_new_cards_count_against_the_counter(recwarn):
  """A review card's `due` is a day number, and a new card in a filtered deck has a
  negative one."""
  home, filtered = Deck(name="Home"), Deck(name="Filtered", kind=DeckFilteredKind())
  review_card = Card(
    note=a_basic_note("reviewed"),
    deck=home,
    card_type=CardType.REVIEW,
    queue=CardQueue.REVIEW,
    due=900,
    interval_days=3,
  )
  borrowed = Card(
    note=a_basic_note("borrowed"),
    deck=filtered,
    due=-100_000,
    original_deck=home,
    original_due=7,
  )
  collection = a_collection_holding(8, review_card, borrowed)
  collection.add_notes([a_basic_note()], home)
  assert len(recwarn) == 0


@pytest.mark.parametrize(
  ("counter", "due", "stored"),
  [
    pytest.param(None, 0, 1, id="missing"),
    pytest.param(5, 5, 6, id="valid"),
    pytest.param(-1, 0, 1, id="negative"),
    pytest.param("not a number", 0, 1, id="not-int"),
    pytest.param(2**32, 0, 1, id="past-u32"),
  ],
)
def test_the_counter_is_read_and_advanced_as_anki_does(
  counter: object, due: int, stored: int
):
  """Anki reads a missing or unreadable counter as 0 and advances it with a u32
  `wrapping_add(1)` (`rslib/src/config/mod.rs:200-206`)."""
  collection = a_collection_with_counter(counter)
  deck = Deck(name="Chosen")
  collection.decks.append(deck)
  (cards,) = collection.add_notes([a_basic_note()], deck)
  assert cards[0].due == due
  assert stored_counter(collection) == stored


@pytest.mark.parametrize(
  ("counter", "due", "stored"),
  [
    pytest.param(1_999_999, 1_999_999, 2_000_000, id="under"),
    pytest.param(2_000_000, 1_000_000, 2_000_001, id="at"),
    pytest.param(2_345_678, 1_345_678, 2_345_679, id="above"),
    pytest.param(2**31, 1_483_648, 2**31 + 1, id="past-i32"),
    pytest.param(2**32 - 1, 1_967_295, 0, id="wraps"),
  ],
)
def test_a_position_from_2_000_000_is_wrapped_as_check_database_does(
  counter: int, due: int, stored: int
):
  """Anki's "Check Database" wraps a new card at 2,000,000 or more to
  `1000000 + due % 1000000` (`rslib/src/storage/card/fix_due_new.sql`). genanki2 wraps
  it when the card is made, which also keeps a counter past the i32 range out of the
  card's due, where Anki's `due as i32` (`rslib/src/notetype/cardgen.rs:307`) would
  turn it negative. The counter itself still advances as a u32."""
  collection = a_collection_with_counter(counter)
  deck = Deck(name="Chosen")
  collection.decks.append(deck)
  (cards,) = collection.add_notes([a_basic_note()], deck)
  assert cards[0].due == due
  assert stored_counter(collection) == stored


# ----------------------------------- Random order -------------------------------------


def test_a_preset_inserting_at_random_draws_one_position_per_note():
  deck = Deck(name="Random", config=a_random_preset())
  note = Note(notetype=BASIC_AND_REVERSED_NOTETYPE, fields=["front", "back"])
  (cards,) = a_collection(deck).add_notes([note], deck)
  first, second = (card.due for card in cards)
  assert first == second
  assert 1 <= first < 1000


def test_a_random_position_drawn_from_a_large_counter_is_wrapped_too():
  collection = a_collection_with_counter(2**31)
  deck = Deck(name="Random", config=a_random_preset())
  collection.decks.append(deck)
  dues = [
    collection.add_notes([a_basic_note(str(n))], deck)[0][0].due for n in range(5)
  ]
  assert all(1 <= due < 2_000_000 for due in dues)


def test_the_random_position_depends_only_on_the_counter():
  """Like Anki, the draw is seeded with the counter, so one counter value always gives
  one position."""
  dues = []
  for _ in range(2):
    deck = Deck(name="Random", config=a_random_preset())
    (cards,) = a_collection(deck).add_notes([a_basic_note()], deck)
    dues.append(cards[0].due)
  assert dues[0] == dues[1]


def test_the_counter_still_advances_by_one_under_random_order():
  deck = Deck(name="Random", config=a_random_preset())
  collection = a_collection(deck)
  collection.add_notes([a_basic_note(str(n)) for n in range(3)], deck)
  assert stored_counter(collection) == 4


def test_a_preset_named_by_id_is_found_through_the_deck_holding_it():
  """`add_notes` collects the presets first, so a preset only another deck holds is
  found by the deck that names it."""
  preset = a_random_preset()
  holder = Deck(name="Holder", config=preset)
  deck = Deck(name="Named", kind={"config_id": preset.id})
  collection = a_collection(holder, deck)
  dues = [
    collection.add_notes([a_basic_note(str(n))], deck)[0][0].due for n in range(5)
  ]
  assert dues != [1, 2, 3, 4, 5]


def test_a_deck_on_a_missing_preset_inserts_as_preset_1_does():
  preset_1 = default_deck_config()
  preset_1.config.new_card_insert_order = RANDOM
  deck = Deck(name="Dangling", kind={"config_id": 7})
  collection = a_collection(deck)
  collection.deck_configs.append(preset_1)
  dues = [
    collection.add_notes([a_basic_note(str(n))], deck)[0][0].due for n in range(5)
  ]
  assert dues != [1, 2, 3, 4, 5]


def test_a_deck_on_no_preset_at_all_inserts_in_order():
  """With neither its preset nor preset 1, Anki uses its builtin default, which inserts
  in order."""
  deck = Deck(name="Dangling", kind={"config_id": 7})
  collection = a_collection(deck)
  dues = [
    collection.add_notes([a_basic_note(str(n))], deck)[0][0].due for n in range(3)
  ]
  assert dues == [1, 2, 3]


# ---------------------------------- What is refused -----------------------------------


def test_a_filtered_deck_is_refused():
  """Anki would put the cards in the Default deck instead (`deck_for_adding`)."""
  deck = Deck(name="Filtered", kind=DeckFilteredKind())
  with pytest.raises(Genanki2Error, match='filtered deck "Filtered"'):
    a_collection(deck).add_notes([a_basic_note()], deck)


def test_a_note_given_twice_in_one_call_is_refused():
  deck = Deck(name="Chosen")
  note = a_basic_note()
  with pytest.raises(Genanki2Error, match=re.escape(f"Repeated guids: {(note.guid,)}")):
    a_collection(deck).add_notes([note, note], deck)


def test_two_notes_sharing_a_guid_in_one_call_are_refused():
  deck = Deck(name="Chosen")
  first = a_basic_note("one")
  second = Note(notetype=BASIC_NOTETYPE, fields=["two", "back"], guid=first.guid)
  with pytest.raises(Genanki2Error, match="repeat a guid"):
    a_collection(deck).add_notes([first, second], deck)


def test_a_note_added_by_an_earlier_call_is_refused():
  deck = Deck(name="Chosen")
  collection = a_collection(deck)
  note = a_basic_note()
  collection.add_notes([note], deck)
  with pytest.raises(Genanki2Error, match="already have cards"):
    collection.add_notes([note], deck)


def test_a_note_whose_card_only_a_review_holds_is_refused():
  """`add_notes` collects the cards the reviews reference before it looks."""
  deck = Deck(name="Chosen")
  note = a_basic_note()
  review = Review(
    card=Card(note=note, deck=deck), ease=ReviewRating.GOOD, type=ReviewType.REVIEW
  )
  collection = a_collection(deck)
  collection.reviews.append(review)
  with pytest.raises(Genanki2Error, match="already have cards"):
    collection.add_notes([note], deck)


@pytest.mark.parametrize(
  "refused",
  ["filtered deck", "repeated guid", "already added"],
)
def test_a_refused_call_adds_no_card_and_leaves_the_counter(refused: str):
  deck = Deck(name="Chosen")
  collection = a_collection(deck)
  added = a_basic_note("added")
  collection.add_notes([added], deck)
  cards_before = list(collection.cards)
  fresh = a_basic_note("fresh")
  if refused == "filtered deck":
    args = ([fresh], Deck(name="Filtered", kind=DeckFilteredKind()))
  elif refused == "repeated guid":
    args = ([fresh, fresh], deck)
  else:
    args = ([fresh, added], deck)
  with pytest.raises(Genanki2Error):
    collection.add_notes(*args)
  assert collection.cards == cards_before
  assert stored_counter(collection) == 2
