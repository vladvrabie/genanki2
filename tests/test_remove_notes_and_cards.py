"""Do `Collection.remove_notes` and `remove_empty_cards` remove what Anki removes?

    uv run --with pytest pytest tests/test_remove_notes_and_cards.py

A removal goes from reviews to cards to the note, because writing collects references
the other way: a review brings back its card, and a card its note.

Anki's Empty Cards (`empty_cards_for_notetype` in `rslib/src/notetype/emptycards.rs`)
picks, for each note with cards, the cards whose ordinal the note no longer renders,
asking `new_cards_required` without the card 0 fallback. When every card of a note is
empty, it removes the note too, unless "keep notes" is ticked: then it keeps the card
with the lowest ordinal (`will_delete_note` in `rslib/src/card_rendering/service.rs`,
`_delete_cards` in `qt/aqt/emptycards.py`). A note left without cards would get card
0 again from the next edit or "Check Database", so it is never kept without one.

The models carry no anki dependency and neither does this file.
"""

import pytest

from genanki2.builtins import (
  BASIC_NOTETYPE,
  BASIC_OPTIONAL_REVERSED_NOTETYPE,
  CLOZE_NOTETYPE,
)
from genanki2.models.card import Card
from genanki2.models.collection import Collection
from genanki2.models.deck import Deck
from genanki2.models.note import Note
from genanki2.models.review import Review, ReviewRating, ReviewType

HOME = Deck(name="Home")


def a_note(front: str = "f", reverse: str = "") -> Note:
  """A note with card 0 while "Front" is filled and card 1 while "Add Reverse" is."""
  return Note(notetype=BASIC_OPTIONAL_REVERSED_NOTETYPE, fields=[front, "b", reverse])


def a_cloze_note(text: str) -> Note:
  return Note(notetype=CLOZE_NOTETYPE, fields=[text, ""])


def a_card(note: Note, ordinal: int = 0) -> Card:
  return Card(note=note, deck=HOME, template_ord=ordinal)


def a_review(card: Card) -> Review:
  return Review(card=card, ease=ReviewRating.GOOD, type=ReviewType.REVIEW)


def a_collection(*cards: Card, reviews: tuple[Review, ...] = ()) -> Collection:
  """A collection holding `cards`, `reviews` and the notes of both."""
  collection = Collection(cards=list(cards), reviews=list(reviews))
  collection.collect_cards().collect_notes()
  return collection


def ordinals(collection: Collection) -> list[tuple[str, int]]:
  return [(card.note.guid, card.template_ord) for card in collection.cards]


def guids(collection: Collection) -> list[str]:
  return [note.guid for note in collection.notes]


# ------------------------------------ remove_notes ------------------------------------


def test_a_removed_note_takes_its_cards_and_their_reviews():
  removed, kept = a_note(reverse="y"), a_note()
  cards = (a_card(removed, 0), a_card(removed, 1))
  kept_card = a_card(kept)
  reviews = (a_review(cards[1]), a_review(kept_card))
  collection = a_collection(*cards, kept_card, reviews=reviews)
  assert collection.remove_notes([removed]) == ((cards, reviews[:1]),)
  assert guids(collection) == [kept.guid]
  assert collection.cards == [kept_card]
  assert collection.reviews == [reviews[1]]


def test_each_removed_note_gets_its_own_cards_and_reviews_in_order():
  first, second, kept = a_note(reverse="y"), a_note(), a_note()
  first_cards = (a_card(first, 0), a_card(first, 1))
  second_card, kept_card = a_card(second), a_card(kept)
  reviews = (a_review(first_cards[0]), a_review(second_card), a_review(kept_card))
  collection = a_collection(*first_cards, second_card, kept_card, reviews=reviews)
  assert collection.remove_notes([second, first]) == (
    ((second_card,), reviews[1:2]),
    (first_cards, reviews[:1]),
  )
  assert guids(collection) == [kept.guid]
  assert collection.cards == [kept_card]
  assert collection.reviews == [reviews[2]]


def test_notes_from_a_generator_are_removed():
  """`notes` is read twice, once for the cards and once for the notes."""
  removed, kept = a_note(), a_note()
  collection = a_collection(a_card(removed), a_card(kept))
  collection.remove_notes(note for note in [removed])
  assert guids(collection) == [kept.guid]
  assert ordinals(collection) == [(kept.guid, 0)]


def test_a_card_only_a_review_holds_goes_with_the_note():
  """`remove_notes` collects the cards the reviews reference before it looks."""
  note = a_note()
  card = a_card(note)
  collection = Collection(reviews=[a_review(card)])
  ((cards, _),) = collection.remove_notes([note])
  assert cards == (card,)
  collection.collect_references()
  assert collection.reviews == collection.cards == collection.notes == []


def test_a_note_added_with_add_notes_does_not_come_back():
  """`add_notes` stores only the cards; the note would join on collecting."""
  collection = Collection(decks=[HOME])
  note = a_note()
  collection.add_notes([note], HOME)
  collection.remove_notes([note])
  collection.collect_references()
  assert collection.cards == collection.notes == []


def test_removing_a_note_the_collection_lacks_changes_nothing():
  kept = a_note()
  collection = a_collection(a_card(kept))
  assert collection.remove_notes([a_note()]) == (((), ()),)
  assert guids(collection) == [kept.guid]
  assert len(collection.cards) == 1


def test_removing_no_notes_changes_nothing():
  kept = a_note()
  collection = a_collection(a_card(kept))
  assert collection.remove_notes([]) == ()
  assert guids(collection) == [kept.guid]
  assert len(collection.cards) == 1


def test_removed_notes_are_not_written(tmp_path):
  collection = Collection.anki_default()
  removed, kept = a_note("removed"), a_note("kept")
  (removed_cards, _) = collection.add_notes([removed, kept], collection.decks[0])
  collection.reviews.append(a_review(removed_cards[0]))
  collection.remove_notes([removed])
  collection.write_sqlite(tmp_path / "collection.anki2")
  written = Collection.read_sqlite(tmp_path / "collection.anki2")
  assert guids(written) == [kept.guid]
  assert [card.note.guid for card in written.cards] == [kept.guid]
  assert written.reviews == []


# --------------------------------- remove_empty_cards ---------------------------------


def test_a_card_whose_front_no_longer_renders_goes_with_its_reviews():
  note = a_note(reverse="y")
  cards = (a_card(note, 0), a_card(note, 1))
  review = a_review(cards[1])
  collection = a_collection(*cards, reviews=(review,))
  note["Add Reverse"] = ""
  assert collection.remove_empty_cards() == ((note, (cards[1],), (review,)),)
  assert ordinals(collection) == [(note.guid, 0)]
  assert guids(collection) == [note.guid]
  assert collection.reviews == []


def test_a_collection_without_empty_cards_is_left_unchanged():
  note = a_note(reverse="y")
  collection = a_collection(a_card(note, 0), a_card(note, 1))
  assert collection.remove_empty_cards() == ()
  assert ordinals(collection) == [(note.guid, 0), (note.guid, 1)]


def test_a_note_whose_cards_are_all_empty_goes_with_them():
  note, kept = a_note(reverse="y"), a_note()
  cards = (a_card(note, 0), a_card(note, 1))
  collection = a_collection(*cards, a_card(kept))
  note["Front"] = note["Add Reverse"] = ""
  assert collection.remove_empty_cards() == ((note, cards, ()),)
  assert guids(collection) == [kept.guid]
  assert ordinals(collection) == [(kept.guid, 0)]


def test_each_note_gets_back_only_its_own_empty_cards_and_reviews():
  """One call removes from several notes; each entry holds only that note's part."""
  partly, wholly, intact = a_note(reverse="y"), a_note(reverse="y"), a_note()
  partly_cards = (a_card(partly, 0), a_card(partly, 1))
  wholly_cards = (a_card(wholly, 0), a_card(wholly, 1))
  intact_card = a_card(intact)
  reviews = (
    a_review(partly_cards[1]),
    a_review(wholly_cards[0]),
    a_review(intact_card),
  )
  collection = a_collection(*partly_cards, *wholly_cards, intact_card, reviews=reviews)
  partly["Add Reverse"] = ""
  wholly["Front"] = wholly["Add Reverse"] = ""
  assert collection.remove_empty_cards() == (
    (partly, partly_cards[1:], reviews[:1]),
    (wholly, wholly_cards, reviews[1:2]),
  )
  assert guids(collection) == [partly.guid, intact.guid]
  assert ordinals(collection) == [(partly.guid, 0), (intact.guid, 0)]
  assert collection.reviews == [reviews[2]]


def test_a_note_with_only_an_empty_card_0_goes_and_is_not_regenerated():
  """Card 0 of a note that renders nothing is empty, as in Anki's report."""
  note = a_note(front="")
  collection = a_collection(a_card(note, 0))
  collection.remove_empty_cards()
  collection.collect_references()
  assert collection.cards == collection.notes == []
  assert collection.generate_missing_cards() == ()


def test_keep_notes_keeps_the_card_with_the_lowest_ordinal():
  note = a_note(reverse="y")
  cards = (a_card(note, 1), a_card(note, 0))
  collection = a_collection(*cards)
  note["Front"] = note["Add Reverse"] = ""
  assert collection.remove_empty_cards(keep_notes=True) == ((note, cards[:1], ()),)
  assert ordinals(collection) == [(note.guid, 0)]
  assert guids(collection) == [note.guid]


def test_keep_notes_leaves_a_note_with_a_single_empty_card_alone():
  note = a_note(front="")
  collection = a_collection(a_card(note, 0))
  assert collection.remove_empty_cards(keep_notes=True) == ()
  assert ordinals(collection) == [(note.guid, 0)]


@pytest.mark.parametrize("keep_notes", [False, True])
def test_a_note_with_a_valid_card_loses_every_empty_one(keep_notes: bool):
  """Only a note whose cards are all empty keeps one; here card 0 is the empty one."""
  note = a_note(reverse="y")
  collection = a_collection(a_card(note, 0), a_card(note, 1))
  note["Front"] = ""
  collection.remove_empty_cards(keep_notes=keep_notes)
  assert ordinals(collection) == [(note.guid, 1)]
  assert guids(collection) == [note.guid]


def test_a_cloze_note_loses_the_cards_of_its_removed_numbers():
  note = a_cloze_note("{{c1::a}} {{c2::b}} {{c3::c}}")
  collection = a_collection(*(a_card(note, ordinal) for ordinal in (0, 1, 2)))
  note["Text"] = "{{c1::a}} b {{c3::c}}"
  collection.remove_empty_cards()
  assert ordinals(collection) == [(note.guid, 0), (note.guid, 2)]


def test_a_card_of_a_missing_template_is_empty():
  """Anki's report counts any ordinal the note does not render, in range or not."""
  note = Note(notetype=BASIC_NOTETYPE, fields=["f", "b"])
  collection = a_collection(a_card(note, 0), a_card(note, 1))
  collection.remove_empty_cards()
  assert ordinals(collection) == [(note.guid, 0)]


def test_an_empty_card_only_a_review_holds_is_found():
  note = a_note()
  card, empty = a_card(note, 0), a_card(note, 1)
  collection = Collection(cards=[card], reviews=[a_review(empty)])
  collection.remove_empty_cards()
  collection.collect_references()
  assert collection.cards == [card]
  assert collection.reviews == []


def test_a_note_without_cards_is_left_to_generate_missing_cards():
  note = a_note(front="")
  collection = Collection(notes=[note])
  assert collection.remove_empty_cards() == ()
  assert guids(collection) == [note.guid]
