import dataclasses as dc

from genanki2.utils import StrEnum


@dc.dataclass(frozen=True)
class SerializationContext:
  """Information about the serialization process, passed to each of its phases.

  It informs contained models about the serialization and should be passed by the entry
  point to the phases. A phase reads the context to decide whether some checks or
  warnings apply, and how to word them (e.g. some describe what Anki does with a
  package file).

  Attributes:
    output: The kind of output produced during serialization. Default: `None`.
  """

  # ----------------------------------- Nested types -----------------------------------

  class Output(StrEnum):
    """The format that a serialization entry point produces."""

    APKG = "apkg"
    BYTES = "bytes"
    COLPKG = "colpkg"
    JSON = "json"
    ROW = "row"
    SQLITE = "sqlite"

  # ------------------------------------ Attributes ------------------------------------

  output: Output | None = None
