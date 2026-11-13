# Validation

How the pydantic models under `src/genanki2/models/` decide whether they can be written, and when those decisions run.

## Permissive while building, strict at the write

A model accepts an incomplete state while it is being built, so "construct empty, append later" works. Completeness is checked once, when the object claims to be finished: in `to_rows()` and the other entry points described below.

The models have no strictness setting. The only way past the checks is [`checks_disabled()`](#switching-checks-and-warnings-off), which covers a whole write.

## Checks and warnings

A write runs two named judgements on every model:

- `check()` raises: the write cannot proceed.
- `warn()` warns: Anki accepts the value, but does something the caller did not ask for.

Keeping them apart lets a caller ask for either verdict without producing any rows, and turn the warnings off with `warnings_disabled()` while keeping the checks.

Three base classes carry this machinery:

- `CheckedModel` holds pydantic's `model_config` that every model shares and the 3 phases.
- `RowModel[R]` adds `from_row()` and `to_row()`, and holds the library's single `cast`.
    - `Notetype` is the exception that doesn't use `from_row()` and `to_row()` and adds its own `from_rows()` and `to_rows()` because it aggregates the fields and templates.
- `ProtobufModel` adds `from_bytes()` and `to_bytes()`.

## Phases, emitters and entry points

Every method that serializes a model has one of three roles:

| Role        | Methods                                                                                                | What it does                                        |
| ----------- | ------------------------------------------------------------------------------------------------------ | --------------------------------------------------- |
| Phase       | `prepare()`, `check()`, `warn()`                                                                       | changes or judges a model, and produces nothing     |
| Emitter     | `_row()`, `_bytes()`, `CardData._json()`, `Collection._sqlite()`, `_PackageBase._write_*()`            | produces output, and calls only other emitters      |
| Entry point | `to_row()`, `to_rows()`, `to_bytes()`, `to_json()`, `write_sqlite()`, `write_apkg()`, `write_colpkg()` | runs the phases over its own tree, then its emitter |

A parent reaches its children through their emitters, never through their entry points. So a write judges each model once, from the entry point the caller invoked, however deep the model sits.

The whole tree is prepared before anything is checked, because a parent's checks read its children. Otherwise a collection would refuse a card for an original deck that the card's own repair is about to clear.

`Notetype.to_row()` runs only its row-local checks, so that `to_rows()` stays the point where a notetype is checked as a whole.

### Why the phases are methods and not pydantic validators

The checks have to run when a package is written, not only when a model is built. A model can change after its validators last ran: a list edited in place, such as `notetype.templates.pop()`, fires no validator. And a check that reads several objects, such as a card's template ordinal against its notetype's templates, can only be judged once all of them are in their final state.

Pydantic validators cannot be made to run at that point. They run when a model is built and, with `validate_assignment`, when a field is assigned. Serializing a model runs no validator.

The only way to make pydantic validate an existing model again is the `revalidate_instances` setting, which applies when a model is passed into a field of another model. It does not fit either way:

- With `"never"`, the default, the model is accepted as it is and nothing runs.
- With `"always"`, pydantic validates the model by building a new copy of it, and of every model it holds. A notetype shared by many notes would become one copy per note. An edit to the notetype in `collection.notetypes` would then no longer reach the notes, and the walk would judge every copy separately.

So `prepare()`, `check()` and `warn()` are plain methods, and the entry points call them.

## What a write changes on the model

The emitters change nothing. A write changes the models only in `prepare()`, in three places:

- `TimestampedRowModel.prepare()` sets the modification time of a note or notetype changed since it was last stamped.
- `Collection.prepare()` appends referenced objects to the collection's lists (see [Referenced objects and the collection's lists](#referenced-objects-and-the-collections-lists)).
- `Card.prepare()` clears an original deck and due that the card's own deck no longer explains. Its validator makes the same repair, but a card cannot tell when its filtered deck turns normal.

Each is idempotent: a second write with no edit in between writes the same rows.

A model holds two kinds of state about the files around it, and only one of them follows a write:

- **What the model was read from** stays as the read left it: the fingerprint of the row, the modification time and id read, and a package's kept media source. Checks and warnings compare against it to report what changed since the read.
- **What has been published** follows the writes: the time a note or notetype was last stamped with, and the content it was stamped for. A later write compares against it, so an edit made after one write is stamped again by the next. A write that leaves the time where it was, in the same second as the last stamp or inside `timestamping_disabled()`, leaves this state too, and the next write stamps the edit.

The published state lives on the model, not on the package or collection that wrote it. Anki compares an incoming note with the copy in the user's collection, and every package of a deck feeds that one collection, so a note written into two packages has one history. The second package carries the first one's stamp when the note is unchanged since, and a later one when it is not.

A package written over its kept source is refused rather than followed for the same reason: the source is read state, describing the file the media is copied from (see [A kept media source is checked before writing](packages.md#a-kept-media-source-is-checked-before-writing)).

## Walking the model tree

A phase called with `recursive=True` runs on every model reachable from where it is called, each object once. The walk lives in `CheckedModel`. It follows every `CheckedModel` that a field holds, directly or inside a container, so a field added later is covered without a hand-written branch.

To keep the walk cheap, it reads only the fields whose annotation could hold a model, decided once per class. The strings in `Note.fields` and the scalar columns are never walked. A test checks that reading every field would reach the same objects.

The walk gathers the reachable models breadth first, telling objects apart by identity, and runs the phase on each with `recursive=False`. An override reads `recursive=False` as "apply only my own rules". The result:

- A check from a note covers its notetype.
- A check from a review covers its card, the card's note and decks, and the notetype.
- From a collection, a notetype that every note shares is judged and warned about once, not once per note.

Some details follow from how the walk is built:

- The set of models already seen lives for one call. A phase run inside another, such as a `to_bytes()` inside a check, starts a walk of its own.
- A model is handed to the phase before its fields are read, so the walk follows what the phase leaves behind. A collection inside a package collects its references when it is prepared, and the models it collects are then prepared too.
- An equal copy of a model held somewhere else is a separate object, and is judged separately. `collect_*` refuses an unequal one at write time.

## Switching checks and warnings off

`checks_disabled()` and `warnings_disabled()` turn off what a write runs automatically, so that a package Anki wrote can be written back even when it holds a value a genanki2 check rejects. `checks_enabled()` and `warnings_enabled()` turn them back on inside such a block (see [Checks](../../guide/settings.md#checks)).

The switches are read at the entry point, never inside a phase, so an explicit `check()` or `warn()` inside the block still runs. Preparation runs whatever the switches say, since collecting fills the lists the writer writes from. Only the context managers are public, so a disabled state cannot outlive the block that set it.

`dbcheck_repairs_disabled()` is the exception: its repairs run during validation, so the validators read it. See [Switching the opt-out repairs off](repairs.md#switching-the-opt-out-repairs-off).

## Referenced objects and the collection's lists

Models refer to each other as objects: a card holds its note and its deck, a note holds its notetype, etc. The collection also keeps flat lists (`notes`, `decks`, etc.) and the writer writes rows from those lists only. Two things can go wrong between the references and the lists:

- **An object is referenced but missing from its list.** Its row would not be written, and the row referring to it would name an id that the file does not hold.
- **Two different objects share one id.** For example, a card's note and a note already in `collection.notes` have the same id but different fields or modification times. Only one row can be written for that id, and nothing says which one.

Collecting handles both (`Collection.collect_references()`, which the prepare phase of every write runs). It appends each referenced object that is missing from its list, and raises when the list already holds a different object under the same id. Collecting compares content, because it decides which object gets written. So a caller can append just the cards it builds: their notes, decks, notetypes and presets fill in by themselves.

`Collection.check_referential_integrity()` asks a narrower question: does every note's notetype, every card's note and every card's deck have its id in the matching list? It compares ids only, because the file stores only ids, and Anki resolves a row whose id is in the file whatever the object behind it holds. A note held in two different versions therefore passes this check, and only collecting refuses it.

In a write, collecting runs before any check, so the check finds the lists already filled. Calling `Collection.check()` without preparing first reports a reference that collecting would have added.

## Arguments of `Field.to_row()` and `Template.to_row()`

A field or template row holds two values that the field or template does not own: the id of its notetype (`ntid`) and its position in the notetype's list (`ord`). So `Field.to_row(notetype_id, ordinal)` and `Template.to_row(notetype_id, ordinal)` take them as two plain ints and write them unchecked. The `fields` and `templates` tables declare no foreign key, so a wrong `ntid` would write rows that no notetype owns, without any error from SQLite.

Every other value a write produces is checked: a model attribute by its annotation, and a row read with `from_row()` by `model_validate`.

The two ints need no check of their own, because both come from the notetype. `Notetype.to_rows()` passes `self.id`, which `Notetype` already validates, and an ordinal from `enumerate`, which is 0, 1, 2… by construction. Validating the notetype's id covers the `ntid` of every row.

What no validation can show is that `to_rows()` passes these values down correctly. Two tests check it:

- `test_the_owning_id_comes_from_the_notetype` changes the notetype's id and checks that every field and template row carries the new one.
- `test_ordinals_follow_list_position` reverses the list and checks that the `ord` values still run 0, 1, 2… in the new order.

Checking the arguments as well would mean `pydantic.validate_call` on both methods. In lax mode it also turns `"1"` into `1`, so it would catch almost nothing the notetype does not already catch.

## Bounds that raise and bounds that repair

How Anki reads a column decides what genanki2 does with a value out of range:

- **Anki reads the column strictly:** a value that does not fit makes the whole row unreadable, so the bound raises. A bound that raises refuses nothing Anki would open.
- **Anki falls back to another value:** Anki reads the fallback in place of what is stored, so every code path that loads the row sees the fallback, and an apkg import stores it. genanki2 repairs the value to the same fallback as it reads it, as a mandatory repair (see [The repairs](repairs.md#the-repairs)). This covers the preset values under `Bounded`, `Card.due` and `Card.original_due` (`0` outside the 32-bit signed range), and `Review.time_ms` (`0` outside the 32-bit unsigned range).

A value inside Anki's type can still be one that Anki's database check changes: a `Card.template_ord` above 30,000, or a `Card.interval_days` or `Review.time_ms` above `2**31 - 1`. Anki opens such a value as it is, so it is not a bound but an [opt-out repair](repairs.md#opt-out-repairs).

Ids and times are bounded to values that make sense, even though Anki reads them as any 64-bit integer: an id is positive, and a time is 0 or later. Anki never writes anything else, and nothing in Anki gives a negative time or id a meaning. The cost is that genanki2 cannot read a collection holding such a value, so the bound is widened if one turns up in real data.

`Review.review_type` is the one value read with a fallback that genanki2 keeps. Anki reads a review kind it does not know as a learning review, while the field is `ReviewType | int` and keeps the number. The fallback is what an older Anki makes of a kind a newer Anki added, and repairing it would erase the kind for the newer Anki too.

`usn` is bounded only by its type, although Anki writes nothing below -1. A value below -1 makes an object count as already synced: a client sync sends the objects whose usn is -1, and the server those at or above the client's last count. Nothing genanki2 writes is held back by it, but only because of two Anki behaviours: an apkg import replaces every `usn` with the collection's own, and a colpkg restore forces a full sync, which uploads the whole file whatever each row's `usn`. If either changes, a value below -1 could keep an object from ever syncing, and the bound has to be reconsidered.
