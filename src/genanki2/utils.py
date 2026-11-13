import enum
import struct
import sys
from collections import Counter
from collections.abc import Hashable, Iterable, Iterator
from typing import Any, TypeVar

__all__ = [
  "StrEnum",
  "duplicates",
  "float32",
  "is_uint8",
  "is_uint16",
  "is_uint32",
  "nested_instances",
  "uint32",
]

HashableT = TypeVar("HashableT", bound=Hashable)
T = TypeVar("T")


def duplicates(items: Iterable[HashableT]) -> tuple[HashableT, ...]:
  """Return all duplicates found in `items`."""
  return tuple(item for item, count in Counter(items).items() if count > 1)


if sys.version_info >= (3, 12):
  from enum import StrEnum  # pyright: ignore[reportUnreachable]
else:

  class StrEnum(str, enum.Enum):  # StrEnum in Python 3.11+
    """An enum whose members are strings and print as their value."""

    __str__ = str.__str__  # Python 3.11 fix; Do not remove until 3.12 is min version.


def nested_instances(iterable: Any, cls: type[T]) -> Iterator[T]:
  """Find every instance of `cls` inside `iterable`, at any depth.

  Iterate inside containers to yield `cls` instances: items of an iterable and values
  of a mapping-like object with a `values()` method. The search does not enter the
  returned instances. Strings and bytes are skipped.

  An iterator is consumed by the search, and a container that holds itself raises
  `RecursionError`.

  Args:
    iterable: The objects to search. Anything that is not iterable yields nothing.
    cls: The type that is searched for.

  Yields:
    Each instance of `cls` found, as often as it is reached.
  """
  try:
    iterator: Iterator[Any] = iter(iterable)
  except TypeError:
    return
  for obj in iterator:
    if isinstance(obj, (str, bytes)):
      continue
    if isinstance(obj, cls):
      yield obj
      continue
    # go over values of a mapping
    values: Any = getattr(obj, "values", None)
    if callable(values):
      yield from nested_instances(values(), cls)
    # iterate over any iterable
    yield from nested_instances(obj, cls)


def is_uint8(n: Any) -> bool:
  return type(n) is int and 0 <= n < 256


def is_uint16(n: Any) -> bool:
  return type(n) is int and 0 <= n < 2**16


def is_uint32(n: Any) -> bool:
  return type(n) is int and 0 <= n < 2**32


def uint32(n: int) -> int:
  return n % 2**32


def float32(value: float) -> float:
  return struct.unpack("f", struct.pack("f", value))[0]
