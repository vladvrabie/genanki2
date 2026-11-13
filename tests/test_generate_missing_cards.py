"""Does `Collection.generate_missing_cards` give notes the cards Anki gives them?

    uv run --with pytest pytest tests/test_generate_missing_cards.py

Anki fills in a note's missing cards in `generate_cards_for_note`
(`rslib/src/notetype/cardgen.rs`), reading the existing cards through
`rslib/src/storage/notetype/existing_cards.sql`. Which ordinals a note gets is covered
against Anki in `test_card_ordinals_match_anki.py`, and the deck override and the
random draw in `test_add_notes.py`. This file covers what an existing note changes:

- only the ordinals the note's cards lack, and no card removed;
- the position of the note's existing new card with the lowest id (`odue` in a
  filtered deck, at least 0), with the counter left alone, else the counter
  (`extract_data_from_existing_cards`, `cardgen.rs:205`);
- the deck all the existing cards belong to, else the deck passed, else the Default
  deck (`new_cards_required_normal`, `deck_for_adding`);
- notes listed without cards, the counter warning, and what is refused.

The models carry no anki dependency and neither does this file.
"""

import pytest

from genanki2.builtins import (
  BASIC_NOTETYPE,
  BASIC_OPTIONAL_REVERSED_NOTETYPE,
  CLOZE_NOTETYPE,
  DEFAULT_DECK,
  builtin_notetype,
  default_config_entries,
  default_deck,
)
from genanki2.errors import Genanki2Error
from genanki2.models.card import Card, CardQueue, CardType
from genanki2.models.collection import Collection
from genanki2.models.config import ConfigEntry
from genanki2.models.deck import Deck, DeckFilteredKind
from genanki2.models.deck_config import DeckConfig, DeckConfigInner
from genanki2.models.note import Note


def a_collection(*cards: Card, counter: int = 1, decks: tuple[Deck, ...] = ()):
  """A collection holding `cards` and `decks`, whose new-card counter is `counter`."""
  config = [entry for entry in default_config_entries() if entry.key != "nextPos"]
  entry = ConfigEntry(key="nextPos")
  entry.set_value(counter)
  config.append(entry)
  return Collection(config=config, cards=list(cards), decks=list(decks))


def stored_counter(collection: Collection) -> object:
  entry = collection.get_config_entry("nextPos")
  assert entry is not None
  return entry.value_or()


def an_optional_reversed_note(reverse: str = "") -> Note:
  """A note whose second card exists only while "Add Reverse" is filled."""
  return Note(notetype=BASIC_OPTIONAL_REVERSED_NOTETYPE, fields=["f", "b", reverse])


def a_cloze_note(text: str) -> Note:
  return Note(notetype=CLOZE_NOTETYPE, fields=[text, ""])


def a_new_card(note: Note, deck: Deck, ordinal: int = 0, due: int = 1, **kw) -> Card:
  return Card(note=note, deck=deck, template_ord=ordinal, due=due, **kw)


def a_review_card(note: Note, deck: Deck, ordinal: int = 0, **kw) -> Card:
  """A review card, whose `due` is a day number rather than a position."""
  return Card(
    note=note,
    deck=deck,
    template_ord=ordinal,
    card_type=CardType.REVIEW,
    queue=CardQueue.REVIEW,
    due=900,
    interval_days=3,
    **kw,
  )


def placement(cards: tuple[Card, ...]) -> list[tuple[int, str]]:
  return [(card.template_ord, card.deck.name) for card in cards]


# --------------------------------- Which cards are made ---------------------------------


def test_a_filled_field_gives_the_note_the_card_it_now_renders():
  home = Deck(name="Home")
  note = an_optional_reversed_note()
  collection = a_collection(a_new_card(note, home))
  note["Add Reverse"] = "y"
  (cards,) = collection.generate_missing_cards()
  assert placement(cards) == [(1, "Home")]
  assert cards[0].note is note
  assert collection.cards[-1] is cards[0]


def test_a_cloze_note_gains_a_card_per_new_cloze_number():
  home = Deck(name="Home")
  note = a_cloze_note("{{c1::a}}")
  collection = a_collection(a_new_card(note, home))
  note["Text"] = "{{c1::a}} {{c2::b}} {{c4::c}}"
  (cards,) = collection.generate_missing_cards()
  assert [card.template_ord for card in cards] == [1, 3]


def test_only_the_notes_that_gain_cards_are_returned():
  home = Deck(name="Home")
  complete, gaining = an_optional_reversed_note(), an_optional_reversed_note()
  collection = a_collection(a_new_card(complete, home), a_new_card(gaining, home))
  gaining["Add Reverse"] = "y"
  all_cards = collection.generate_missing_cards()
  assert [cards[0].note for cards in all_cards] == [gaining]


def test_a_collection_with_nothing_missing_is_left_unchanged(recwarn):
  home = Deck(name="Home")
  note = an_optional_reversed_note("y")
  existing = [a_new_card(note, home, 0), a_new_card(note, home, 1)]
  collection = a_collection(*existing, counter=7)
  assert collection.generate_missing_cards() == ()
  assert collection.cards == existing
  assert stored_counter(collection) == 7
  assert len(recwarn) == 0


def test_a_card_whose_template_no_longer_renders_is_kept():
  """Anki leaves such a card for "Empty Cards" to remove."""
  home = Deck(name="Home")
  note = an_optional_reversed_note("y")
  existing = [a_new_card(note, home, 0), a_new_card(note, home, 1)]
  collection = a_collection(*existing)
  note["Add Reverse"] = ""
  assert collection.generate_missing_cards() == ()
  assert collection.cards == existing


@pytest.mark.parametrize("other_ordinal", [0, 2], ids=["duplicate", "out of range"])
def test_as_many_cards_as_templates_can_still_miss_one(other_ordinal: int):
  """Anki skips a normal note whose card count equals its template count
  (`generate_cards_for_notetype`, `cardgen.rs:276`), which its database makes safe.
  A collection read from a file can repeat an ordinal or keep one past the templates,
  so the skip compares ordinals.
  """
  home = Deck(name="Home")
  note = an_optional_reversed_note("y")
  existing = [a_new_card(note, home, 0), a_new_card(note, home, other_ordinal)]
  collection = a_collection(*existing)
  (cards,) = collection.generate_missing_cards()
  assert placement(cards) == [(1, "Home")]


def test_a_note_with_cards_that_renders_none_gets_no_card_0(recwarn):
  """Card 0 is the fallback for a note with no card at all (`cardgen.rs:107`)."""
  home = Deck(name="Home")
  note = Note(notetype=BASIC_NOTETYPE, fields=["f", "b"])
  collection = a_collection(a_new_card(note, home, 0))
  note["Front"] = ""
  assert collection.generate_missing_cards() == ()
  assert len(recwarn) == 0


def test_a_note_listed_without_cards_gets_its_cards_in_the_deck_passed():
  chosen = Deck(name="Chosen")
  note = an_optional_reversed_note("y")
  collection = a_collection(counter=5)
  collection.notes.append(note)
  (cards,) = collection.generate_missing_cards(chosen)
  assert placement(cards) == [(0, "Chosen"), (1, "Chosen")]
  assert [card.due for card in cards] == [5, 5]
  assert stored_counter(collection) == 6


def test_a_note_listed_without_cards_that_renders_none_gets_card_0_with_a_warning():
  chosen = Deck(name="Chosen")
  note = Note(notetype=BASIC_NOTETYPE, fields=["", "b"])
  collection = a_collection()
  collection.notes.append(note)
  with pytest.warns(UserWarning, match="no template front side renders") as reported:
    (cards,) = collection.generate_missing_cards(chosen)
  assert len(reported) == 1
  assert placement(cards) == [(0, "Chosen")]


# ----------------------------------- New-card position ----------------------------------


def test_a_missing_card_shares_the_existing_new_position_and_the_counter_stays():
  home = Deck(name="Home")
  note = an_optional_reversed_note()
  collection = a_collection(a_new_card(note, home, due=20), counter=50)
  note["Add Reverse"] = "y"
  (cards,) = collection.generate_missing_cards()
  assert cards[0].due == 20
  assert stored_counter(collection) == 50


@pytest.mark.parametrize("lower_id_ord", [0, 1], ids=["ord-0-lower", "ord-1-lower"])
@pytest.mark.parametrize(
  ("lower_id_due", "higher_id_due"), [(20, 50), (50, 20)], ids=["smaller", "larger"]
)
def test_the_position_comes_from_the_new_card_with_the_lowest_id(
  lower_id_ord: int, lower_id_due: int, higher_id_due: int
):
  """Neither the smallest position nor ord 0: the card with the lowest id, checked
  against Anki 26.08 by generating card 3 of a note whose two cards hold different
  positions."""
  home = Deck(name="Home")
  note = a_cloze_note("{{c1::a}} {{c2::b}}")
  lower = a_new_card(note, home, lower_id_ord, lower_id_due, id=1_000_000_000_000)
  higher = a_new_card(note, home, 1 - lower_id_ord, higher_id_due, id=9_000_000_000_000)
  collection = a_collection(higher, lower)
  note["Text"] = "{{c1::a}} {{c2::b}} {{c3::c}}"
  (cards,) = collection.generate_missing_cards()
  assert cards[0].due == lower_id_due


def test_a_card_that_is_not_new_holds_no_position():
  home = Deck(name="Home")
  note = a_cloze_note("{{c1::a}} {{c2::b}}")
  reviewed = a_review_card(note, home, 0, id=1_000_000_000_000)
  new = a_new_card(note, home, 1, due=30, id=9_000_000_000_000)
  collection = a_collection(reviewed, new)
  note["Text"] = "{{c1::a}} {{c2::b}} {{c3::c}}"
  (cards,) = collection.generate_missing_cards()
  assert cards[0].due == 30


def test_a_note_with_no_new_card_takes_the_counter_which_advances():
  home = Deck(name="Home")
  note = an_optional_reversed_note()
  collection = a_collection(a_review_card(note, home), counter=40)
  note["Add Reverse"] = "y"
  (cards,) = collection.generate_missing_cards()
  assert cards[0].due == 40
  assert stored_counter(collection) == 41


def test_a_new_card_in_a_filtered_deck_gives_its_original_due():
  """`existing_cards.sql` reads `odue` when it is set."""
  home, filtered = Deck(name="Home"), Deck(name="Filtered", kind=DeckFilteredKind())
  note = an_optional_reversed_note()
  borrowed = a_new_card(
    note, filtered, due=-100_000, original_deck=home, original_due=7
  )
  collection = a_collection(borrowed, decks=(home,))
  note["Add Reverse"] = "y"
  (cards,) = collection.generate_missing_cards()
  assert cards[0].due == 7


def test_a_negative_position_counts_as_0():
  """`existing_cards.sql` reads `max(0, due)`."""
  home = Deck(name="Home")
  note = an_optional_reversed_note()
  collection = a_collection(a_new_card(note, home, due=-5), counter=40)
  note["Add Reverse"] = "y"
  (cards,) = collection.generate_missing_cards()
  assert cards[0].due == 0
  assert stored_counter(collection) == 40


def test_the_counter_advances_once_per_note_that_takes_it():
  """The notes take the counter in the order of `notes`, where the listed note comes
  before the notes collected from the cards. The complete note has no new card, so
  only its lack of missing cards keeps it off the counter."""
  home = Deck(name="Home")
  reusing, taking, complete = (an_optional_reversed_note() for _ in range(3))
  listed = an_optional_reversed_note("y")
  collection = a_collection(
    a_new_card(reusing, home, due=2),
    a_review_card(taking, home),
    a_review_card(complete, home),
    counter=10,
  )
  collection.notes.append(listed)
  reusing["Add Reverse"] = taking["Add Reverse"] = "y"
  all_cards = collection.generate_missing_cards(home)
  dues = {cards[0].note.guid: [card.due for card in cards] for cards in all_cards}
  assert dues == {listed.guid: [10, 10], reusing.guid: [2], taking.guid: [11]}
  assert stored_counter(collection) == 12


def test_a_reused_position_is_kept_in_a_deck_inserting_at_random():
  """Anki uses the existing position as it is, without a draw (`cardgen.rs:300`)."""
  preset = DeckConfig(name="Random")
  preset.config.new_card_insert_order = DeckConfigInner.NewCardInsertOrder.RANDOM
  home = Deck(name="Random", config=preset)
  note = an_optional_reversed_note()
  collection = a_collection(a_new_card(note, home, due=1234))
  note["Add Reverse"] = "y"
  (cards,) = collection.generate_missing_cards()
  assert cards[0].due == 1234


# -------------------------------------- Which deck --------------------------------------


def test_the_deck_all_existing_cards_belong_to_wins_over_the_deck_passed():
  home, chosen = Deck(name="Home"), Deck(name="Chosen")
  note = an_optional_reversed_note()
  collection = a_collection(a_new_card(note, home))
  note["Add Reverse"] = "y"
  (cards,) = collection.generate_missing_cards(chosen)
  assert placement(cards) == [(1, "Home")]


def test_a_card_in_a_filtered_deck_belongs_to_the_deck_it_came_from():
  home, filtered = Deck(name="Home"), Deck(name="Filtered", kind=DeckFilteredKind())
  note = an_optional_reversed_note()
  borrowed = a_new_card(
    note, filtered, due=-100_000, original_deck=home, original_due=7
  )
  collection = a_collection(borrowed, decks=(home,))
  note["Add Reverse"] = "y"
  (cards,) = collection.generate_missing_cards(Deck(name="Chosen"))
  assert placement(cards) == [(1, "Home")]


@pytest.mark.parametrize("passed", [True, False], ids=["deck-passed", "no-deck"])
def test_cards_in_several_decks_send_the_new_one_to_the_deck_passed(passed: bool):
  """Without one shared deck, Anki uses the deck it is given, else deck 1."""
  first, second, chosen = Deck(name="First"), Deck(name="Second"), Deck(name="Chosen")
  note = a_cloze_note("{{c1::a}} {{c2::b}}")
  collection = a_collection(
    a_new_card(note, first, 0), a_new_card(note, second, 1), decks=(default_deck(),)
  )
  note["Text"] = "{{c1::a}} {{c2::b}} {{c3::c}}"
  (cards,) = collection.generate_missing_cards(chosen if passed else None)
  assert placement(cards) == [(2, "Chosen" if passed else "Default")]


def test_without_a_deck_1_the_card_goes_to_the_builtin_default_deck():
  note = an_optional_reversed_note("y")
  collection = a_collection()
  collection.notes.append(note)
  (cards,) = collection.generate_missing_cards()
  assert all(card.deck is DEFAULT_DECK for card in cards)


@pytest.mark.parametrize("shared", ["missing", "filtered"])
def test_a_shared_deck_that_is_missing_or_filtered_sends_the_card_to_deck_1(
  shared: str,
):
  """One shared deck wins over the deck passed even when `deck_for_adding` then
  rejects it, so the card goes to deck 1, not to the deck passed."""
  filtered, default = Deck(name="Filtered", kind=DeckFilteredKind()), default_deck()
  note = an_optional_reversed_note()
  if shared == "missing":
    held = a_new_card(note, filtered, due=-100_000, original_deck=999, original_due=7)
  else:
    held = a_new_card(note, filtered, due=-100_000)
  collection = a_collection(held, decks=(default,))
  note["Add Reverse"] = "y"
  (cards,) = collection.generate_missing_cards(Deck(name="Chosen"))
  assert cards[0].deck is default


def test_a_template_override_wins_over_the_shared_deck():
  home, override = Deck(name="Home"), Deck(name="Override")
  notetype = builtin_notetype(BASIC_OPTIONAL_REVERSED_NOTETYPE.name, id=1_000_001)
  notetype.templates[1].config.target_deck_id = override.id
  note = Note(notetype=notetype, fields=["f", "b", ""])
  collection = a_collection(a_new_card(note, home), decks=(override,))
  note["Add Reverse"] = "y"
  (cards,) = collection.generate_missing_cards()
  assert placement(cards) == [(1, "Override")]


# ------------------------------------ Counter warning -----------------------------------


def test_a_counter_behind_the_existing_new_cards_is_reported_when_a_note_takes_it():
  home = Deck(name="Home")
  held = an_optional_reversed_note()
  listed = an_optional_reversed_note("y")
  collection = a_collection(a_new_card(held, home, due=5), counter=1)
  collection.notes.append(listed)
  with pytest.warns(UserWarning, match=r"counter \(1\).*holds \(5\)") as reported:
    collection.generate_missing_cards(home)
  assert len(reported) == 1


def test_a_counter_behind_is_not_reported_when_no_note_takes_it(recwarn):
  home = Deck(name="Home")
  note = an_optional_reversed_note()
  collection = a_collection(a_new_card(note, home, due=5), counter=1)
  note["Add Reverse"] = "y"
  collection.generate_missing_cards()
  assert len(recwarn) == 0


# --------------------------------- Where the warnings point -----------------------------


def a_collection_listing_a_note_overriding_a_missing_deck() -> Collection:
  notetype = builtin_notetype(BASIC_NOTETYPE.name, id=1_000_999)
  notetype.templates[0].config.target_deck_id = 999
  collection = a_collection()
  collection.notes.append(Note(notetype=notetype, fields=["f", "b"]))
  return collection


def a_collection_listing_a_note_with_no_card() -> Collection:
  collection = a_collection()
  collection.notes.append(Note(notetype=BASIC_NOTETYPE, fields=["", "b"]))
  return collection


def a_collection_with_a_counter_behind() -> Collection:
  home = Deck(name="Home")
  collection = a_collection(a_new_card(an_optional_reversed_note(), home, due=5))
  collection.notes.append(an_optional_reversed_note("y"))
  return collection


@pytest.mark.parametrize(
  "make_collection",
  [
    a_collection_listing_a_note_overriding_a_missing_deck,
    a_collection_listing_a_note_with_no_card,
    a_collection_with_a_counter_behind,
  ],
)
def test_a_warning_points_at_the_line_that_generated_the_cards(make_collection):
  collection = make_collection()
  with pytest.warns(UserWarning) as reported:
    collection.generate_missing_cards()
  assert reported[0].filename == __file__


# ------------------------------------- What is refused ----------------------------------


def test_a_filtered_deck_is_refused_and_nothing_changes():
  home = Deck(name="Home")
  note = an_optional_reversed_note()
  collection = a_collection(a_new_card(note, home), counter=4)
  cards_before = list(collection.cards)
  note["Add Reverse"] = "y"
  with pytest.raises(Genanki2Error, match='filtered deck "Filtered"'):
    collection.generate_missing_cards(Deck(name="Filtered", kind=DeckFilteredKind()))
  assert collection.cards == cards_before
  assert stored_counter(collection) == 4
