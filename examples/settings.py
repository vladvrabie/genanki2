"""The ambient switches: when checks, warnings and database repairs run.

    python examples/settings.py downloaded.apkg copy.apkg

Each switch is a context manager that holds for the code inside its `with` block,
including everything that code calls. Switches are on by default. Each one has an
`_enabled()` twin that turns it back on inside a block that turned it off.

- Checks raise on what Anki would reject or repair. They run on a write.
- Warnings point at what Anki accepts but is likely a mistake. They run on a write.
- Database repairs change values that Anki keeps until the user runs Check Database.
  They run as models are built or assigned, so a read applies them too.
"""

import sys
from pathlib import Path

from genanki2 import (
  Genanki2Error,
  Package,
  checks_disabled,
  dbcheck_repairs_disabled,
  warnings_disabled,
)


def write_back_anyway(source: Path, target: Path) -> None:
  """A package someone else made can hold values Anki takes and a check refuses.

  An explicit `check()` (or `warn()`) runs whatever the switches say, so the refusal
  can still be reported.
  """
  pkg = Package.read_apkg(source, keep_source_media=True)
  try:
    pkg.check()
  except Genanki2Error as error:
    print(f"Writing anyway: {error}")
  with checks_disabled():
    pkg.write_apkg(target)


def silence_known_warnings(source: Path, target: Path) -> None:
  """Inside a `with warnings_disabled():` block, warnings are not computed."""
  pkg = Package.read_apkg(source, keep_source_media=True)
  with warnings_disabled():
    pkg.write_apkg(target)


def exact_copy(source: Path, target: Path) -> None:
  """The rows written are the rows read: nothing repaired, nothing refused.

  The repairs apply while reading, so the read has to be inside the `with` block too.
  """
  with dbcheck_repairs_disabled(), checks_disabled(), warnings_disabled():
    pkg = Package.read_apkg(source, keep_source_media=True)
    pkg.write_apkg(target)


if __name__ == "__main__":
  source, target = Path(sys.argv[1]), Path(sys.argv[2])
  exact_copy(source, target)
  # silence_known_warnings(source, target)
  # write_back_anyway(source, target)
