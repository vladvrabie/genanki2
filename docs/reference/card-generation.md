# Card generation

An `.apkg` import doesn't generate cards, it brings in the cards that the package carries and nothing else. So genanki2 makes the cards Anki would make, when a note is added (`Collection.add_notes()`) and after an edit (`Collection.generate_missing_cards()`). `Note.card_ordinals` lists which cards a note gets.

## Which cards a note gets

- **On a normal notetype, a template gives a card when its front renders (aka it's not empty).** That means the front holds a plain field reference `{{K}}` to non-empty field `K` and every conditional around the reference is satisfied (`{{#C}}` or `{{^C}}`).
    - You can check a template using `Template.is_front_renderable()`.
    - Text outside a reference does not count towards determining if the template is empty or not.
    - A field is considered empty if it holds only whitespace and line breaks.
    - The special names count as filled for every note, except `Tags`, which needs the note to have tags, and `FrontSide`, which never does.
    - A filter does not change the answer, so `{{text:F}}` renders when `F` is filled.
- **On a cloze notetype, each cloze number gives a card, from any field.** `{{c1::…}}` is card 0, `{{c1,2::…}}` gives cards 0 and 1.
    - A cloze nested in another cloze counts too.
    - `c0` does not produce any cards.
    - Every number from 500 up shares card 499: Anki gives each of them its own card 499 and removes all but one at the next "Check Database", so genanki2 makes one.
    - All cards use the first template.
- **A note with no card gets card 0, with a warning.** Anki does the same.
    - `Note.card_ordinals` reports the renderable cards, but it can be empty.
    - Both `add_notes()` and `generate_missing_cards()` apply the fallback.
- **Existing cards stay when their front becomes empty.** Anki keeps them until the user runs "Empty Cards".
    - An edit made to the note's values or to the template may make the front unrenderable.
    - `Collection.remove_empty_cards()` removes them, for a package that should not hand those cards to new users.

The two cloze warnings on `Note`, for cloze markup on a normal notetype and in a field for which the front does not pass through `cloze:`, read nested clozes the same way. So they also warn about `{{c0::{{c1::…}}}}`, which Anki's Add dialog accepts because its check reads only the outermost clozes, while the markup still shows as written or the `c1` card still has nothing hidden.

## In which deck cards go

The deck is determined in this order:

1. the deck that a card's template overrides (`Template.config.target_deck_id`);
2. for `Collection.generate_missing_cards()` only: the deck that the note's existing cards are in, if they share exactly _one_.
    - For a card in a filtered deck, its home deck is used (`Card.original_deck`).
3. the deck passed as parameter, if it's a normal deck;
4. the Default deck.

If step 1 or 2 names a deck that is missing or filtered, step 3 is skipped and the card is put in the Default deck, as Anki 26.x does. For a template override, this comes with a warning that names `Template.repair_target_deck_id()`, which clears the override.

## New-card position

A note takes one new-card position, shared by all of its cards.

`Collection.next_new_card_position` hands out the positions and advances once per note, so notes are studied in the order they were added.

A card added after an edit (with `generate_missing_cards()`) takes the position of the note's existing new cards when it has at least one.

A deck whose preset inserts new cards in random order gets a random position instead, drawn from a generator seeded with the counter, as Anki does; the numbers differ from Anki's.
