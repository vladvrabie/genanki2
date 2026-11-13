__all__ = [
  "CollectionReadError",
  "Genanki2Error",
  "Genanki2Warning",
  "PackageReadError",
]


class Genanki2Warning(UserWarning):
  """Base category for every warning genanki2 raises."""


class Genanki2Error(ValueError):
  """Base class for the errors genanki2 raises on its own.

  It derives from `ValueError` because pydantic converts only a `ValueError` or an
  `AssertionError` raised inside a validator into a `ValidationError`. Anything else
  escapes raw, losing the field it was raised on and the other errors reported with it.
  """


class CollectionReadError(Genanki2Error):
  """Collection data genanki2 cannot read.

  The failure is structural. A collection that reads cleanly can still fail its own
  checks or carry a value pydantic rejects.
  """


class PackageReadError(Genanki2Error):
  """Package data genanki2 cannot make sense of.

  It says nothing about the collection inside. A package that reads cleanly can still
  hold a collection that fails its own checks.
  """
