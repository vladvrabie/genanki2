# Settings

genanki2 has four settings, each turned on and off by a pair of context managers:

| Setting                | Off                          | On                          | Affects                      |
| ---------------------- | ---------------------------- | --------------------------- | ---------------------------- |
| Checks                 | `checks_disabled()`          | `checks_enabled()`          | writing                      |
| Warnings               | `warnings_disabled()`        | `warnings_enabled()`        | writing                      |
| Database check repairs | `dbcheck_repairs_disabled()` | `dbcheck_repairs_enabled()` | reading, assignment, writing |
| Timestamping           | `timestamping_disabled()`    | `timestamping_enabled()`    | writing                      |

All four are on by default.

At the end of the `with` block, the setting returns to its previous state.

The `*_enabled()` methods turn a setting back on inside a block that turned it off:

```python
from genanki2 import Package, warnings_disabled, warnings_enabled

pkg = Package.read_apkg("italian.apkg", keep_source_media=True)
with warnings_disabled():
  pkg.write_apkg("quiet.apkg")
  with warnings_enabled():
    pkg.write_apkg("loud.apkg")
```

`examples/settings.py` shows each one.

## Checks

A check raises `Genanki2Error` for something that Anki would reject or quietly change, or that would break later, when reviewing cards. The checks run when a model is written or when called manually. See [Checks and warnings](checks-and-warnings.md) for details on how they work and a complete list.

A shared package that someone else made can hold values that Anki accepts, but genanki2's checks refuse, which would stop you from writing it back. `checks_disabled()` skips the checks during a write. An explicit `pkg.check()` still runs inside the block, so you can report a problem and then write anyway:

```python
from genanki2 import Genanki2Error, Package, checks_disabled

pkg = Package.read_apkg("italian.apkg", keep_source_media=True)
try:
  pkg.check()
except Genanki2Error as error:
  print(f"Writing anyway: {error}")
with checks_disabled():
  pkg.write_apkg("copy.apkg")
```

## Warnings

A warning (`Genanki2Warning`) points at something that Anki accepts but that is probably a mistake. The warnings run when a model is written, after the checks, or when called manually. See [Checks and warnings](checks-and-warnings.md) for details on how they work and a complete list.

`warnings_disabled()` skips the warnings during a write: they are not computed at all. An explicit `pkg.warn()` still warns inside the block.

Python's `warnings` module works on them as on any other warning. A filter computes each warning and then discards it, so it is slower than `warnings_disabled()`, but it can pick warnings by message or turn them into errors:

```python
import warnings

from genanki2 import Genanki2Warning

warnings.simplefilter("error", Genanki2Warning)  # every warning raises
```

The context manager does not affect warnings raised during a read; filter them with the `warnings` module.

## Database check repairs

Anki keeps some values as they are until the user runs Check Database, which then repairs them. genanki2 makes these repairs itself, so the package already holds what Anki would repair to, and warns about each value it changes. [Repairs](repairs.md) lists them.

The repairs run when a package is read or written and when an attribute is assigned. `dbcheck_repairs_disabled()` keeps the values as they are, so the block has to wrap the read, the write and any assignment in between to disable the repairs completely. A model whose repairs run on assignment repairs itself again when it is assigned to outside the block. A repair method called explicitly still repairs.

A write warns about each value kept, unless it runs inside `warnings_disabled()`. The checks are not affected.

## Timestamping

Anki's import updates a note or a notetype that a user already has only when the incoming one has a newer modification time. So when a note or a notetype read from a package has changed, the write sets its modification time to the current time, as Anki does when it saves an edit. A modification time that you set yourself is kept. Freezing a model (`freeze()`) also sets it, following the setting at the time of the freeze.

`timestamping_disabled()` keeps the time that was read, so an import leaves the copy that a user already has unchanged. A write warns about each change that an import would skip, unless it runs inside `warnings_disabled()`. An explicit `update_modification_time()` still sets the time. See [Updating a package](updating.md) for what an import does with each change.

## Writing a package back as it was read

With the checks, the warnings and the repairs disabled, the rows written are the rows read: nothing repaired, nothing refused (although a read can still refuse values in the wrong value range). The repairs run while reading, so the read has to be inside the block too:

```python
from genanki2 import (
  Package,
  checks_disabled,
  dbcheck_repairs_disabled,
  warnings_disabled,
)

with dbcheck_repairs_disabled(), checks_disabled(), warnings_disabled():
  pkg = Package.read_apkg("shared.apkg", keep_source_media=True)
  pkg.write_apkg("copy.apkg")
```
