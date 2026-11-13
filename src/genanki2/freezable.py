from collections import UserString
from collections.abc import Mapping, MappingView, MutableSequence, Sequence
from collections.abc import Set as AbstractSet
from typing import Any, cast

import pydantic as pyd
from frozendict import frozendict
from typing_extensions import Self, override

from genanki2.utils import nested_instances


class Freezable:
  """Mixin that lets one instance, and every `Freezable` it holds, refuse changes.

  A frozen instance refuses assigning or deleting a public attribute. Names beginning
  with `_` are by convention private and stay writable. A frozen instance still
  compares equal to an unfrozen copy of itself and dumps the same.

  The freeze covers attributes. A mutable container held by a frozen instance still
  accepts mutations (for example `list.append()`).

  When combining with pydantic, `pydantic.BaseModel.model_copy(update=...)` bypasses
  the guard. A copy made with `pydantic.BaseModel.model_copy()`, shallow or deep, is
  frozen too, since the flag is copied with the rest of the instance. List it before
  `pydantic.BaseModel` among the bases, so the guard runs ahead of pydantic's
  validation.
  """

  _frozen: bool = False

  def is_frozen(self) -> bool:
    """Return whether the instance refuses changes or not."""
    return self._frozen

  def freeze(self) -> Self:
    """Freeze this instance and every `Freezable` it holds.

    A `Freezable` is found in an attribute or inside the containers an attribute holds,
    at any depth, as `utils.nested_instances()` describes. Each one found freezes what
    it holds in turn. Freezing a frozen instance does nothing.

    An instance counts as frozen only once everything it holds is frozen too, so two
    instances holding each other raise `RecursionError`.
    """
    if self._frozen:
      return self
    # Possible improvement: freeze only public attrs, aligning with the write/delete
    # guard and the content equivalence comparison.
    for model in nested_instances(self.__dict__.values(), Freezable):
      model.freeze()
    self._frozen = True
    return self

  @override
  def __setattr__(self, name: str, value: object) -> None:
    """Assign an attribute unless the instance is frozen.

    Raises:
      AttributeError: The instance is frozen and `name` does not begin with `_`.
    """
    if getattr(self, "_frozen", False) and not name.startswith("_"):
      raise AttributeError(f"{type(self).__name__} is frozen")
    super().__setattr__(name, value)

  @override
  def __delattr__(self, name: str, /) -> None:
    """Delete an attribute unless the instance is frozen.

    Raises:
      AttributeError: The instance is frozen and `name` does not begin with `_`.
    """
    if getattr(self, "_frozen", False) and not name.startswith("_"):
      raise AttributeError(f"{type(self).__name__} is frozen")
    super().__delattr__(name)


class DeepFreezable(Freezable):
  """Mixin that makes an instance refuse changes, making its containers immutable.

  In addition to refusing attribute changes, a frozen instance refuses mutable
  operations on attributes that are containers. See `deepfreeze()` for the conversion
  mapping.

  Due to the conversion, a frozen instance may no longer compare equal to an unfrozen
  copy of itself: `data == data.freeze()` returns `False` when a mutable container is
  replaced. Use `DeepFreezable.is_content_equivalent()` instead.

  Pydantic serialization with `model_dump()` is also affected: pydantic warns when held
  types differ from their declared annotated type. A dict field holding a `frozendict`
  makes a JSON dump raise if frozendict is running as its C extension, which is not a
  `dict` subclass. Its pure Python class dumps like a dict. The solution is to add field
  serializers for mutable containers.
  """

  @override
  def freeze(self) -> Self:
    """Freeze this instance, the containers it holds, and every `Freezable` in them.

    `deepfreeze()` is called on each attribute, then the instance is frozen, which
    freezes each model inside the immutable containers. Freezing a frozen instance does
    nothing.

    An instance counts as frozen only once everything it holds is frozen too, so two
    instances holding each other raise `RecursionError`.
    """
    if self._frozen:
      return self
    for name, value in self.__dict__.items():
      self.__dict__[name] = deepfreeze(value)
    return super().freeze()

  def is_content_equivalent(self, other: object) -> bool:
    """Tell whether `other` holds the same content as this instance, frozen or not.

    `==` distinguishes an unfrozen object from a frozen copy of itself. This method
    compares the content at any depth:
    - two mappings are equivalent when they hold the same keys with matching values
      (so a dict matches a `frozendict`);
    - two sequences are equivalent when their items match in order (so a list matches
      a tuple);
    - two pydantic models are equivalent when they are of the same class and their
      fields match;
    - two `Freezable` instances are equivalent when they are of the same class and
      their public attributes match;
    - anything else is compared with `==` (strings, bytes, sets, etc.); a string
      never matches a list of its characters.

    Private attributes (starting with `_`) and a pydantic model's extra attributes are
    not checked. The comparison is symmetric, and a container that holds itself raises
    `RecursionError`.

    Args:
      other: The object to compare with.
    """
    return _is_content_equivalent(self, other)


def deepfreeze(obj: object) -> Any:
  """Return `obj` with every container replaced by an immutable one, at any depth.

  Each container is replaced by its immutable counterpart:
  - a `bytearray` becomes `bytes`;
  - a `memoryview` becomes a read-only view of its buffer;
  - a namedtuple is rebuilt as the same namedtuple type;
  - a tuple is rebuilt;
  - a mutable sequence becomes a tuple;
  - a mapping becomes a `frozendict`;
  - a set becomes a `frozenset`;

  Containers are recursed into (except `bytearray`, views and sets), calling
  `deepfreeze()` on each item.

  Everything else is returned unchanged, notably objects holding data in attributes.
  A dict view stays tied to its dict and a tuple subclass (except namedtuple) is also
  unchanged.

  Some conversions keep the values and drop the behaviour: a `defaultdict` stops
  supplying defaults, a `Counter` stops returning 0 for a missing key, a `ChainMap` is
  flattened, and an `array.array` becomes a tuple of numbers. The buffer under a
  read-only view still changes when written through another reference.

  A container referenced twice becomes two separate copies, and a container that holds
  itself raises `RecursionError`.

  Args:
    obj: The value to turn immutable.

  Returns:
    The immutable value. A value that is not a container comes back as the same object.
  """
  match obj:
    case bytearray():
      return bytes(obj)
    case memoryview():
      return obj.toreadonly()
    case tuple():
      items = cast(tuple[object, ...], obj)
      make: object = getattr(type(items), "_make", None)  # a namedtuple
      if callable(make):
        return make(deepfreeze(item) for item in items)
      if type(items) is tuple:
        return tuple(deepfreeze(item) for item in items)
      return items
    case Mapping():
      mapping = cast(Mapping[object, object], obj)
      return frozendict[object, object](
        {key: deepfreeze(value) for key, value in mapping.items()}
      )
    case MutableSequence():
      sequence = cast(MutableSequence[object], obj)
      return tuple(deepfreeze(item) for item in sequence)
    case MappingView():
      return obj
    case AbstractSet():
      return frozenset(obj)
    case _:
      return obj


def _public_attrs(obj: object) -> dict[str, Any]:
  """Return the attributes of `obj` a freeze protects, those not beginning with `_`."""
  return {key: value for key, value in obj.__dict__.items() if not key.startswith("_")}


_TEXT = (str, bytes, UserString)


def _is_content_equivalent(a: object, b: object) -> bool:
  """Tell whether `a` and `b` hold the same content.

  The rules are those `DeepFreezable.is_content_equivalent()` describes. Taking both
  sides as arguments keeps the comparison symmetric, and lets it recurse into values
  that have no such method, such as a list compared with a tuple.
  """
  if a is b:
    return True
  match a, b:
    case _ if isinstance(a, _TEXT) or isinstance(b, _TEXT):
      return a == b
    case Mapping(), Mapping():
      a = cast(Mapping[object, object], a)
      b = cast(Mapping[object, object], b)
      return len(a) == len(b) and all(
        key_a in b and _is_content_equivalent(a[key_a], b[key_a]) for key_a in a
      )
    case Sequence(), Sequence():
      return len(a) == len(b) and all(
        _is_content_equivalent(item_a, item_b) for item_a, item_b in zip(a, b)
      )
    case pyd.BaseModel(), pyd.BaseModel() if type(a) is type(b):
      dict_a: dict[str, Any] = {name: getattr(a, name) for name in type(a).model_fields}
      dict_b: dict[str, Any] = {name: getattr(b, name) for name in type(b).model_fields}
      return _is_content_equivalent(dict_a, dict_b)
    case Freezable(), Freezable() if type(a) is type(b):
      return _is_content_equivalent(_public_attrs(a), _public_attrs(b))
    case _, _:
      return a == b
