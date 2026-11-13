# Repairs that alter what they read

Wherever genanki2 interprets a value it reads (field-name repair, tag splitting, `sfld` and `csum`, the deck-name separator, media names), it has to leave what Anki wrote unchanged. Reading an untouched package and writing it back reproduces every row.

Repairs are the exception. A repair that runs during validation runs on `from_row()` too, so a package holding a value it repairs does not come back as it went in. Repairs fall into three classes, which differ in whether a caller can switch them off.

## Mandatory repairs

A mandatory repair changes a value that has no effect in Anki as it is:

- **Bounded preset values.** Anki substitutes the default for an out-of-range preset value as it reads the preset, so keeping the value keeps something inert.
- **`cards.due` and `cards.odue` outside the 32-bit signed range, and `revlog.time` outside the 32-bit unsigned range.** Anki reads `0` in their place as it loads the card or the review, and an apkg import stores what it loaded, so the user ends up with `0` whatever the package held.
- **Normalized names and text.** Anki makes the same change when it saves the value.
- **`notes.sfld` and `notes.csum`.** Both are mechanical derivations of the fields, which the caller chose to change. `Note` holds neither column: preserving what was read would mean carrying two values whose only purpose is to be written back, and deciding what they mean once `fields` is edited. Recomputing them reproduces what Anki stored for an unedited note.

## Opt-out repairs

An opt-out repair changes a value that Anki repairs in the database check and nowhere else. Those values are live: a new card at position 3,000,000 stays at that position until the user runs Check Database by hand, through any number of exports and imports. The database check repairs the card values in one function (`fix_card_properties`) and the review durations in another (`fix_revlog_properties`), and the importer shifts `due` only for review and day-learning cards (`shift_collection_relative_dates`).

genanki2 repairs them by default anyway. Turning a default repair into an opt-in later only stops rewriting data, while adding a repair later would start rewriting data that earlier releases preserved. `dbcheck_repairs_disabled()` turns them off as a set (see [below](#switching-the-opt-out-repairs-off)).

A repair is mirrored only when genanki2 can reach the result Anki reaches. A repair whose result depends on when Anki runs it, or on what the user's collection already holds, is left to Anki. Note, card and review ids more than a day in the future are such a case: Anki renumbers them from its clock at the moment it runs, on its copy of the package during an apkg import and in the collection at Check Database. genanki2 keeps them, and `Collection.warn_future_ids()` lists them.

## Values Anki stores and then trips over

genanki2 repairs or refuses a value that Anki keeps, when three conditions hold together:

1. Anki accepts the value into the database, so nothing catches it on the way in.
2. It breaks something later, in a way that does not clearly lead back to the setting.
3. genanki2 can identify it exactly, when it is set or written.

Three cases qualify:

- **`seconds_to_show_question` and `seconds_to_show_answer`.** The reviewer reads them through a truthiness test, so a NaN passes the guard and raises on every card once auto advance is on. Both are `Bounded`, so a NaN takes the default `0.0`, which is the documented way of turning the wait off.
- **`ignore_revlogs_before_date`.** Anki parses it only when FSRS computes a memory state (`ignore_revlogs_before_date_to_ms`), which first happens when an imported card is answered. `DeckConfigInner.check_date()` refuses a string that is not `YYYY-MM-DD`.
- **An empty `Note.guid`.** An apkg import matches incoming notes by `guid` against a map of the user's notes, built once per import. The first import adds every note with an empty `guid`. Every later import matches all of them against the same one of the user's notes, which each of them overwrites in turn when it is newer, while the user's other notes never update. `Note` refuses an empty `guid`.

This does not let genanki2 prefer its own values to Anki's. A value Anki repairs as it reads is repaired the same way and to the same result, which is why a bounded preset value takes its default back rather than the nearest bound. A value Anki accepts and acts on is left alone, however odd it looks.

## Values Anki refuses to save

A value Anki refuses when it saves the object is refused by genanki2 too. An apkg import saves every notetype it adds or updates through that same step (`prepare_for_update`), so a package holding such a value fails to import as a whole.

- **A notetype without fields or without templates** ("1 field required", "1 template required").
- **An empty notetype, field or template name**, after the repair Anki makes first: dropping `"` from a notetype name, and the characters Anki strips from a field or template name ("Empty notetype name", "Empty field name", "Empty template name").

## The repairs

| Repair                                                                       | Rewrites                                                                             | Class     | Warns | What Anki does                                                                    |
| ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ | --------- | ----- | --------------------------------------------------------------------------------- |
| `Card.wrap_due`                                                              | a new card's position of 2,000,000 or more, wrapped by `1_000_000 + due % 1_000_000` | opt-out   | yes   | the same, in the database check only (`fix_card_properties`)                      |
| `Card.clear_unexplained_original_due`                                        | `odue` without `odid`, on a learning or review card                                  | opt-out   | yes   | the same, in the database check only (`fix_card_properties`)                      |
| `Card.clear_unexplained_original_data`                                       | `odid` and `odue` together, when the card's own deck is not filtered                 | opt-out   | yes   | the same, in the database check only (`check_filtered_cards`)                     |
| `Card.clamp_template_ord`                                                    | a template ordinal above 30,000, to 30,000                                           | opt-out   | yes   | the same, in the database check only (`fix_card_properties`)                      |
| `Card.clamp_interval_days`                                                   | an interval above `2**31 - 1` days, to `2**31 - 1`                                   | opt-out   | yes   | the same, in the database check only (`fix_card_properties`)                      |
| `Note.clamp_modification_time`                                               | a note modification time in the future, to now                                       | opt-out   | yes   | the same, in the database check, for notes and no other table (`check_notetypes`) |
| `Review.clamp_time`                                                          | a review duration above `2**31 - 1`, to `2**31 - 1`                                  | opt-out   | yes   | the same, in the database check only (`fix_revlog_properties`)                    |
| `Bounded`, on eighteen preset values                                         | a value outside its range, back to the field's default                               | mandatory | no    | substitutes the same default as it reads the preset                               |
| `Bounded`, on `Card.due` and `Card.original_due`                             | a value outside the 32-bit signed range, to `0`                                      | mandatory | no    | reads `0` in its place as it loads the card                                       |
| `Bounded`, on `Review.time_ms`                                               | a value outside the 32-bit unsigned range, to `0`                                    | mandatory | no    | reads `0` in its place as it loads the review                                     |
| `Clamped`, on the learning and relearning steps                              | a step outside its range, to the nearest bound                                       | mandatory | no    | clamps the same way                                                               |
| `normalize_deck_name`                                                        | NFC and the character rules, per `::` component; a part left empty raises            | mandatory | no    | the same, but saves an empty part as `blank` (`normalized_deck_name_component`)   |
| `normalize_note_tag`                                                         | NFC and the character rules, per `::` component; a part left empty becomes `blank`   | mandatory | no    | the same, when it saves or imports a note (`normalized_tag_name_component`)       |
| `normalize_notetype_name`, `normalize_field_name`, `normalize_template_name` | NFC and the character rules                                                          | mandatory | no    | the same, on every notetype save (`normalize_names`)                              |
| `normalize_field_value`, through `normalize_note_fields`                     | NFC, and every ASCII control character except tab and newline                        | mandatory | no    | the same while the `normalize_note_text` config is on, which is its default       |
| `Note.to_row`                                                                | `sfld` and `csum`, recomputed from the fields                                        | mandatory | no    | recomputes both on every note save                                                |
| `Note.to_row`                                                                | `flags` to `0` and `data` to `""`                                                    | mandatory | no    | reads neither column                                                              |

Three rows need more than the table holds:

- **Deck names and tags** treat an empty `::` part differently. A deck name with one raises: the part is most likely a typo, and the `blank` Anki would save renames a deck the caller did not ask to rename. No collection Anki wrote holds one either, since Anki normalizes a deck name whenever it saves it, and the schema 15 upgrade converted every older deck the same way. A tag substitutes `blank`: Anki normalizes a note's tags only when it saves the note, and no upgrade rewrites them, so a collection Anki wrote can still hold a tag with an empty part from before Anki normalized tag components. Raising would refuse such a collection, and an apkg import substitutes `blank` itself.
- **Note text** is the one normalization Anki gates on a config, `normalize_note_text`, checked when it adds, updates or transforms a note. A collection whose owner turned it off holds text genanki2 folds anyway. The config is not reachable from Anki's interface, and honouring it would give a field validator access to the collection that owns the note.
- **`notes.flags` and `notes.data`** are dropped rather than repaired. Nothing reads those columns, and adding fields for them later would cost a caller nothing.

## Switching the opt-out repairs off

`dbcheck_repairs_disabled()` keeps the seven opt-out values as the file holds them, and `dbcheck_repairs_enabled()` turns the repairs back on inside such a block.

Unlike the [other switches](validation.md#switching-checks-and-warnings-off), which an entry point reads, this one is read by the seven validators themselves: a repair runs during validation, so by the time anything is written the value is already gone. The block therefore has to wrap the read. [Anki's "Check Database"](../../guide/repairs.md#ankis-check-database) shows the block in use.

It has to wrap the update and the write too. `validate_assignment` reruns every validator of a model whichever field is assigned, and `Card.prepare()`, which also reads the switch, clears the original deck and due again at write time, since a card cannot tell when its filtered deck turns normal.

Each opt-out repair is split in two: the validator in the table reads the switch, and a repair method does the work without reading it (`Card.repair_due_above_ceiling()`, `Card.repair_unexplained_original_due()`, `Card.repair_unexplained_original_data()`, `Card.repair_template_ord_above_ceiling()`, `Card.repair_interval_days_above_ceiling()`, `Note.repair_modification_time()`, `Review.repair_time_above_ceiling()`). An explicit call to a repair method inside the block still repairs, the way an explicit `check()` still checks.

### Warnings about kept values

A kept value warns at write time. Each repair has a `warn_` counterpart in the model's `warn()`, which finds nothing once the repair has run, so no value is reported twice. With `dbcheck_repairs_disabled()` alone, the round trip is byte-exact, and `warnings_disabled()` also silences the warnings about kept values.

The warning that a repair raises when it changes a value is not affected by `warnings_disabled()`. That switch skips computing warnings, but a repair evaluates its condition either way, so filtering `Genanki2Warning` through the `warnings` module silences it at no extra cost. Reading the switch inside the repair method would also let an explicit repair rewrite data without saying so.
