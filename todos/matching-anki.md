# Matching Anki

Behaviour changes in Anki that genanki2 has to mirror. An unshipped change is on Anki's `main` but in no release tag yet (`git tag --contains <commit>` in `anki/` is empty); it moves to the shipped section once a release carries it, which is also when the `anki` wheel used as the test oracle can confirm it.

Checked up to Anki `main` at `1fb2da487b0b3b3fac6f3fdc9c307d7b23c1ca8a` (committed 2026-11-10), on 2026-11-10.

## Shipped Anki changes

None pending.

## Unshipped Anki changes

### A card whose deck is invalid falls back to the target deck before deck 1

Commit `7a27a7606` (Anki issue #5540), `deck_for_adding` in `rslib/src/notetype/cardgen.rs:346`. A new card now tries, in order:

1. Its own deck: the template's override if it has one, otherwise the deck the note's existing cards share (`card.target_deck_id.or(extracted.deck_id)`, `cardgen.rs:150`, unchanged).
2. The target deck: the deck added to (`:234`), the notetype's last-used deck on an edit (`:249`), none after a template change (`:286`).
3. Deck 1.

Before, an invalid (missing or filtered) first choice went straight to deck 1.

Not in 26.09.4 (tagged 2026-11-03 from a release branch, with no `rslib` or `proto` changes over 26.09.3), so the `anki` 26.9.4 wheel still has the old behaviour.

What genanki2 has to change:

- [ ] `add_notes` with an invalid override: the card goes to the deck passed, not the Default deck. `CardMaker.template_target_deck_or` (`_card_maker.py:76-89`) returns `None` and its warning says the card goes to the Default deck. `tests/test_add_notes.py:177` pins the old behaviour.
- [ ] `generate_missing_cards(deck)` with a missing or filtered shared deck: the card goes to `deck`, not deck 1. `CardMaker.unique_home_deck_or` (`_card_maker.py:59-64`) returns `None`. `tests/test_generate_missing_cards.py:346` and the docstring at `collection.py:662-663` pin the old behaviour.
- [ ] An invalid override on a note that already has cards falls back to the target deck, not to the shared deck: step 1 has already chosen the override over the shared deck. `CardMaker.make()` (`_card_maker.py:237`) replaces `deck` with the shared deck before looking at the override, so the deck passed has to be kept apart.
- `generate_missing_cards()` without a deck is unchanged: there is no target, so the card still goes to deck 1.
