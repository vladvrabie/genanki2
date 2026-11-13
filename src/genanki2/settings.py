from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar, Token

_checks_enabled: ContextVar[bool] = ContextVar("_checks_enabled", default=True)
_warnings_enabled: ContextVar[bool] = ContextVar("_warnings_enabled", default=True)
_dbcheck_repairs_enabled: ContextVar[bool] = ContextVar(
  "_dbcheck_repairs_enabled", default=True
)
_timestamping_enabled: ContextVar[bool] = ContextVar(
  "_timestamping_enabled", default=True
)


def _temporary_set(var: ContextVar[bool], value: bool) -> Generator[None]:
  """Set `var` to `value` for the duration of a `with` block.

  The previous value comes back when the block exits.
  """
  token: Token[bool] = var.set(value)  # Python 3.14: use token in with block.
  try:
    yield
  finally:
    var.reset(token)


@contextmanager
def checks_enabled() -> Generator[None]:
  """In a `with` block run the model checks before writing.

  A model is checked when it is written, by `to_row()`, `to_bytes()` and the package
  and collection writers. Checks are on by default. Use this to reactivate them inside
  a `checks_disabled()` block.

  This does not affect the other phases.
  """
  yield from _temporary_set(_checks_enabled, True)


@contextmanager
def checks_disabled() -> Generator[None]:
  """In a `with` block skip the model checks before writing.

  A model is checked when it is written by `to_row()`, `to_bytes()` and the package
  and collection writers. Inside the block those checks are skipped, so a package read
  from a file can be written back even when it holds something Anki accepts and a
  genanki2 check rejects. A `check()` called explicitly still runs.

  This does not affect the other phases.

  The setting affects only the current context: asyncio tasks created inside the block
  inherit it and threads do not.
  """
  yield from _temporary_set(_checks_enabled, False)


@contextmanager
def warnings_enabled() -> Generator[None]:
  """In a `with` block emit the model warnings before writing.

  A model is warned about surprising or possibly unintended values when it is written,
  by `to_row()`, `to_bytes()` and the package and collection writers. Warnings are on
  by default. Use this to reactivate them inside a `warnings_disabled()` block.

  This does not affect the other phases.
  """
  yield from _temporary_set(_warnings_enabled, True)


@contextmanager
def warnings_disabled() -> Generator[None]:
  """In a `with` block skip emitting the model warnings before writing.

  A model is warned about surprising or possibly unintended values when it is written
  by `to_row()`, `to_bytes()` and the package and collection writers. Inside the block
  the warnings are not computed at all, while filtering `Genanki2Warning` through the
  `warnings` module computes and discards them. A `warn()` called explicitly still
  warns.

  This does not affect the other phases.

  The setting affects only the current context: asyncio tasks created inside the block
  inherit it and threads do not.
  """
  yield from _temporary_set(_warnings_enabled, False)


@contextmanager
def dbcheck_repairs_enabled() -> Generator[None]:
  """In a `with` block make the repairs Anki makes only in its database check.

  Anki keeps certain values as they are until the user runs Check Database. genanki2
  repairs them while a model is built or assigned. They are on by default. Use this
  to reactivate them inside a `dbcheck_repairs_disabled()` block.

  This does not affect the checks or the warnings.
  """
  yield from _temporary_set(_dbcheck_repairs_enabled, True)


@contextmanager
def dbcheck_repairs_disabled() -> Generator[None]:
  """In a `with` block skip the repairs Anki makes only in its database check.

  Anki keeps certain values as they are until the user runs Check Database. genanki2
  repairs them while a model is built or assigned. Inside the block they are kept, so
  a package read and written back holds them as the file did.

  Database repairs happen during both reading and writing, so the block is relevant
  during both processes. During reading, repairs run as the models are built. During
  writing, cards are repaired.

  On models that have these repairs in pydantic validators, an assignment outside the
  block repairs the whole model again. A repair method called explicitly still repairs.

  A write warns about each value kept, unless it runs inside `warnings_disabled()`.
  The checks are not affected.

  The setting affects only the current context: asyncio tasks created inside the block
  inherit it and threads do not.
  """
  yield from _temporary_set(_dbcheck_repairs_enabled, False)


@contextmanager
def timestamping_enabled() -> Generator[None]:
  """In a `with` block set the modification time of a note or notetype if changed.

  A note or notetype is written or frozen with the current time when its content
  changed since its last stamp (the read, or a write that moved its time forward), so
  that Anki's importer takes it as newer.
  This is on by default. Use this to reactivate it inside a `timestamping_disabled()`
  block.

  This does not affect the other phases.
  """
  yield from _temporary_set(_timestamping_enabled, True)


@contextmanager
def timestamping_disabled() -> Generator[None]:
  """In a `with` block keep the modification time of a note or notetype if changed.

  A note or notetype is written or frozen with the current time when its content
  changed since its last stamp (the read, or a write that moved its time forward).
  Inside the block it keeps its time, so an import leaves the copy a user already has
  unchanged. The last stamp stays too, so a write after the block stamps the change.

  Freezing respects the settings when called (if enabled, freezings sets the time).
  `TimestampedRowModel.update_modification_time()` called explicitly still sets the
  time.

  A write warns about each change an import would skip, unless it runs inside
  `warnings_disabled()`.

  The setting affects only the current context: asyncio tasks created inside the block
  inherit it and threads do not.
  """
  yield from _temporary_set(_timestamping_enabled, False)
