import enum
import hashlib
import warnings
from collections import deque
from collections.abc import Iterator
from functools import cache
from inspect import isclass
from types import NoneType
from typing import Any, ClassVar, Generic, TypeVar, cast, get_args

import pydantic as pyd
from google.protobuf.message import Message
from typing_extensions import Self, override

from genanki2.errors import Genanki2Warning
from genanki2.freezable import DeepFreezable
from genanki2.models.serialization_context import SerializationContext
from genanki2.settings import (
  _checks_enabled,
  _timestamping_enabled,
  _warnings_enabled,
)
from genanki2.sqlite import Row
from genanki2.time import time_s
from genanki2.utils import nested_instances

R = TypeVar("R", bound=Row)


def _can_hold_model(annotation: Any) -> bool:
  """Tell whether a field with this annotation could ever hold a `CheckedModel`.

  The annotation is read as a tree: a union, a container or an `Annotated` is decided
  by its arguments, and the field qualifies when any leaf does. A leaf qualifies when
  it is `Any` or a `CheckedModel` subclass. A leaf that is not a class, such as the
  constraint metadata inside `Annotated`, never does.
  """
  if annotations := get_args(annotation):
    return any(_can_hold_model(annot) for annot in annotations)
  return annotation is Any or (
    isclass(annotation) and issubclass(annotation, CheckedModel)
  )


@cache
def _checked_model_fields(cls: type["CheckedModel"]) -> tuple[str, ...]:
  """Return the names of the fields of `cls` that could hold a `CheckedModel`.

  The phases read only these fields, so a scalar field, or a list of strings is never
  walked. The answer depends on the class alone and is computed once per class, on
  first use, when every annotation is resolved.
  """
  return tuple(
    name
    for name, field_info in cls.model_fields.items()
    if _can_hold_model(field_info.annotation)
  )


class CheckedModel(DeepFreezable, pyd.BaseModel):  # pyright: ignore[reportUnsafeMultipleInheritance]
  """Base for a model genanki2 serializes, holding the config every model shares.

  A subclass overrides `CheckedModel.prepare()`, `CheckedModel.check()` and
  `CheckedModel.warn()` to make changes before serialization and to reject or
  highlight wrong values. The serialization methods call them before writing anything.

  A model can be frozen using `freeze()`.
  """

  # Pydantic
  model_config: ClassVar[pyd.ConfigDict] = pyd.ConfigDict(
    validate_by_name=True, validate_by_alias=True, validate_assignment=True
  )

  @override
  def __eq__(self, other: object) -> bool:
    """Return whether `other` is the same class and holds the same field values.

    Private attributes are excluded. They hold what a read leaves behind, such as the
    bytes a protobuf blob was decoded from, rather than the content itself.
    """
    if not isinstance(other, type(self)):
      return NotImplemented
    return (
      self.__class__ == other.__class__
      and self.__dict__ == other.__dict__
      and (self.__pydantic_extra__ or {}) == (other.__pydantic_extra__ or {})
    )

  # ----------------------------- Pre-serialization steps ------------------------------

  def _iter_checked_models(self) -> "Iterator[CheckedModel]":
    """Yield the models this model's fields hold, directly or inside a container.

    Every field counts, so a field added later is walked without a hand-written branch.
    A held model is yielded and not entered: `_iter_unique_checked_models()` reads its
    fields in turn.
    """
    yield from nested_instances(
      (getattr(self, name) for name in _checked_model_fields(type(self))), CheckedModel
    )

  def _iter_unique_checked_models(self) -> Iterator["CheckedModel"]:
    """Yield every model reachable from this one, each object once, breadth first.

    Objects are told apart by identity, so a model referenced by many others is yielded
    once, while an equal copy held elsewhere is yielded too. This model is not yielded.

    A model is yielded before its fields are read. A phase run on it at that point can
    change what it holds, and the walk follows the result.
    """
    seen: set[int] = {id(self)}
    queue: deque[CheckedModel] = deque([self])
    while queue:
      for submodel in queue.popleft()._iter_checked_models():
        if (submodel_id := id(submodel)) not in seen:
          seen.add(submodel_id)
          yield submodel
          queue.append(submodel)

  def prepare(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Change a model to be ready for serialization, changing nothing by default.

    This is the first phase in the serialization process. A subclass overrides this to
    make any necessary modifications before checks and serialization run.

    This phase skips any frozen model, since `CheckedModel.freeze()` prepares a model
    before locking it. An explicit call on a frozen model still runs its steps and
    raises on a change.

    Args:
      recursive: Whether to prepare every model reachable from this one too, each once.
        Each of them is called with `recursive=False`, so an override makes only its
        own changes and leaves the walk to the call it came from. A frozen model is
        skipped. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    if recursive:
      for model in self._iter_unique_checked_models():
        if not model.is_frozen():
          model.prepare(recursive=False, context=context)
    return self

  def check(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Reject a model that cannot be serialized, checking nothing by default.

    This is the second phase in the serialization process. A subclass overrides this to
    state what it wants to check before serialization.

    Args:
      recursive: Whether to check every model reachable from this one too, each once.
        If false, it only runs this model's own checks. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    if recursive:
      for model in self._iter_unique_checked_models():
        model.check(recursive=False, context=context)
    return self

  def warn_int_value_in_enum_field(self) -> Self:
    """Warn about an enum field holding a value the enum does not name.

    An enum value might be open (like in protobuf), so a value written by a newer Anki
    carries variants genanki2 might not know about. Those fields admit a plain int and
    carry it through. Anki repairs unrecognized values by falling back to the variant
    numbered zero.

    Opt-in: a subclass calls it by overriding `CheckedModel.warn()`.
    """
    cls = type(self)
    for name, field_info in cls.model_fields.items():
      annotation: tuple[Any, ...] = get_args(field_info.annotation)
      if any(isinstance(a, type) and issubclass(a, enum.IntEnum) for a in annotation):
        value: enum.IntEnum | int = getattr(self, name)
        if not isinstance(value, enum.IntEnum):
          warnings.warn(
            f"{cls.__name__}: {name} is equal to {value}, but this is not a valid value "
            f"expected by the enum type {annotation[0].__name__}.",
            Genanki2Warning,
            stacklevel=2,
          )
    return self

  def warn(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Warn about a model Anki accepts, warning about nothing by default.

    This is the third phase in the serialization process. A subclass overrides this to
    warn about valid values Anki handles in a way the caller probably does not intend.

    Warnings can be turned off using `with genanki2.warnings_disabled():`, although an
    explicit call still warns.

    Args:
      recursive: Whether to warn about every model reachable from this one too, each
        once. If false, it only runs this model's own warnings. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    if recursive:
      for model in self._iter_unique_checked_models():
        model.warn(recursive=False, context=context)
    return self

  def _run_phases(self, *, context: SerializationContext | None = None) -> Self:
    """Prepare the model, then check and warn unless a setting turns them off.

    Every entry point runs this before its emitter. Preparation fills in what the
    emitter writes, but it is skipped if the model is frozen (`CheckedModel.freeze()`
    already ran it). The settings are read here, not in the phases, so an explicit
    `check()` or `warn()` always runs.

    Args:
      context: Extra information from the serialization process, passed to all three
        phases. Default: `None`.
    """
    if not self._frozen:
      self.prepare(recursive=True, context=context)
    if _checks_enabled.get():
      self.check(recursive=True, context=context)
    if _warnings_enabled.get():
      self.warn(recursive=True, context=context)
    return self

  # -------------------------------------- Freeze --------------------------------------

  @override
  def freeze(self) -> Self:
    """Prepare the model, then freeze it and every model it holds.

    Each model held is prepared by its own freeze, before it is locked. Settings are
    respected at this point. A later write does not prepare the model again. Freezing a
    frozen model does nothing.
    """
    if self._frozen:
      return self
    self.prepare(recursive=False)
    return super().freeze()


class RowModel(CheckedModel, Generic[R]):
  """Base for a model that maps one row of a collection table onto pydantic attributes.

  An attribute is matched to a column by its alias when it has one.
  """

  # ---------------------------------- Serialization -----------------------------------

  @classmethod
  def from_row(cls, row: R | Any) -> Self:
    """Build the model from a table row.

    A subclass whose row references other rows by id overrides this with extra
    parameters to take the rows or models those ids stand for, since a row alone cannot
    reach them.
    """
    return cls.model_validate(row)

  def _row(self) -> R:
    """Serialize the model as its row, running no check, then cast it to `R`."""
    return cast(R, self.model_dump(by_alias=True))

  def to_row(self, *, context: SerializationContext | None = None) -> R:
    """Return the table row this model writes.

    `CheckedModel.check()` and `CheckedModel.warn()` run first, so a model is judged at
    the point it is written (inside Anki it may be too late and the Anki error may not
    be informative). A half-built model can be passed around freely.

    If no context is given, one is created with output
    `SerializationContext.Output.ROW` and passed to the phases.

    Args:
      context: Extra information from the serialization process, passed to the phases.
        Default: `None`.
    """
    if context is None:
      context = SerializationContext(output=SerializationContext.Output.ROW)
    return self._run_phases(context=context)._row()


class FingerprintedRowModel(RowModel[R]):
  """Base for a row model that remembers the content it was read with.

  A model read with `from_row()` keeps a hash of its row, so
  `FingerprintedRowModel.is_changed_since_read()` can tell whether it was changed.
  A model built in code has no hash and never counts as changed.

  A subclass:
    - lists in `_FINGERPRINT_EXCLUDED` the columns that the comparison ignores (e.g. a
      modification time, the sync marker, derived values, etc.)
    - may override `_get_fingerprint()` to hash more than its own row, or less
    - passes its row to `super().from_row()`, which records the hash.
  """

  _FINGERPRINT_EXCLUDED: ClassVar[frozenset[str]] = frozenset()

  _read_fingerprint: bytes = b""

  # ---------------------------------- Derived values ----------------------------------

  def _get_fingerprint(self) -> bytes:
    """Return a digest of the exported SQL row, without `_FINGERPRINT_EXCLUDED` columns.

    The columns are sorted by name, so the digest does not depend on the order the row
    is built in.
    """
    items: list[tuple[str, Any]] = sorted(
      (k, v) for k, v in self._row().items() if k not in self._FINGERPRINT_EXCLUDED
    )
    return hashlib.sha256(repr(items).encode()).digest()

  def _is_changed(self, fingerprint: bytes) -> bool:
    """Return whether the row differs from the one `fingerprint` hashes.

    Return `False` when `fingerprint` is empty, since there is nothing to compare with.
    """
    return fingerprint != b"" and fingerprint != self._get_fingerprint()

  def is_changed_since_read(self) -> bool:
    """Return whether the row differs from the one read, ignoring the excluded columns.

    Return `False` when the fingerprint is missing (usually manually built, not with
    `from_row()`).
    """
    return self._is_changed(self._read_fingerprint)

  # ---------------------------------- Serialization -----------------------------------

  @override
  @classmethod
  def from_row(cls, row: R | Any) -> Self:
    """Build the model from a table row and record the hash of its content."""
    model: Self = super().from_row(row)
    model._read_fingerprint = model._get_fingerprint()
    return model


class TimestampedRowModel(FingerprintedRowModel[R]):
  """Base for a row model whose modification time decides whether an import updates it.

  Anki's importer updates an existing object only when the incoming one carries a newer
  modification time. A model tracks the time and content of its last stamp: those it
  was read with, then those of each write that moved the time forward. When it is
  written or frozen with content changed since, the time is set to now, as Anki does
  when it saves an edit. A time set by the caller is kept.

  The stamp is tracked on the model, not on the package that writes it, since every
  package of a deck reaches the same user collection. A model written into two
  packages carries the first one's stamp in the second, unless it changed in between.

  The time has a resolution of one second, so a change made in the same second as the
  last stamp cannot be newer. `TimestampedRowModel.warn_unimported_changes()` reports
  it.

  The content is compared with the hash that `FingerprintedRowModel` saves, so a
  subclass lists the time itself in `_FINGERPRINT_EXCLUDED`, and may declare
  `modification_time_s` with its own alias and default.
  """

  modification_time_s: int
  _read_modification_time: int = -1

  _last_stamp_fingerprint: bytes = b""
  _last_stamp_modification_time: int = -1

  # ---------------------------------- Derived values ----------------------------------

  def is_changed_since_last_stamp(self) -> bool:
    """Return whether the row differs from the last stamped one, ignoring the time.

    The last stamped row is the one read, then the one of each write that moved the
    modification time forward. Return `False` for a model built in code until its
    first write.
    """
    return self._is_changed(self._last_stamp_fingerprint)

  # ------------------------------------- Repairs --------------------------------------

  def update_modification_time(self) -> Self:
    """Set the modification time to now.

    `TimestampedRowModel.prepare_mod_time()` calls this for a model changed since its
    last stamp.
    """
    if self.modification_time_s != (now := time_s()):
      self.modification_time_s = now
    return self

  # ----------------------------- Pre-serialization steps ------------------------------

  def prepare_mod_time(self) -> Self:
    """Set the modification time to now when the content changed since the last stamp.

    A time set by the caller is not overwritten (if different from the last stamp),
    whether newer or older.

    Whenever the time ends up later than the last stamp, set here or by the caller, the
    current time and content become the last stamp. A model built in code takes its
    first one at its first write. A time that does not move forward leaves the last
    stamp in place, so the change is stamped by a later write.
    """
    if (
      _timestamping_enabled.get()
      and self._last_stamp_modification_time == self.modification_time_s
      and self.is_changed_since_last_stamp()
    ):
      self.update_modification_time()
    if self.modification_time_s > self._last_stamp_modification_time:
      self._last_stamp_modification_time = self.modification_time_s
      self._last_stamp_fingerprint = self._get_fingerprint()
    return self

  @override
  def prepare(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Set the modification time of a model changed since its last stamp, then prepare.

    Args:
      recursive: Whether to prepare every model reachable from this one too, each once.
        Each of them is called with `recursive=False`, so an override makes only its
        own changes and leaves the walk to the call it came from. A frozen model is
        skipped. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.prepare_mod_time()
    return super().prepare(recursive=recursive, context=context)

  def warn_unimported_changes(
    self, context: SerializationContext | None = None
  ) -> Self:
    """Warn about a change that does not reach a user who already has the model.

    Anki's importer keeps the existing copy unless the incoming one is newer, so the
    change does not reach a user who imported a package carrying the last stamp. It
    happens when the change is made in the same second as the last stamp, or when the
    caller sets the time to the last stamp or earlier.

    A colpkg restore replaces the whole collection, so modification time does not
    matter and the warning is skipped.

    Args:
      context: Extra information from the serialization process. Default: `None`.
    """
    match context:
      case SerializationContext(output=SerializationContext.Output.COLPKG):
        return self
      case SerializationContext(output=SerializationContext.Output.APKG):
        msg: str = "An apkg import"
      case _:
        msg = "If imported from an apkg, the import"

    if self._last_stamp_modification_time >= self.modification_time_s and (
      self.is_changed_since_last_stamp()
    ):
      warnings.warn(
        f"{type(self).__name__}: changed since its last stamp, but its modification "
        f"time ({self.modification_time_s}) is not later than that stamp "
        f"({self._last_stamp_modification_time}), the time it was read with or last "
        f"written with. {msg} keeps the copy a user already has, so the change does "
        "not reach that user. A change made in the same second as the read or the last "
        "write, or a time set to the stamp or earlier, does this. Set "
        "`modification_time_s` to a later time to have the change imported.",
        Genanki2Warning,
        stacklevel=2,
      )
    return self

  @override
  def warn(
    self, *, recursive: bool = True, context: SerializationContext | None = None
  ) -> Self:
    """Warn about a change that does not reach a user, then run the other warnings.

    Args:
      recursive: Whether to warn about every model reachable from this one too, each
        once. If false, it only runs this model's own warnings. Default: `True`.
      context: Extra information from the serialization process, passed to every model
        during the walk. Default: `None`.
    """
    self.warn_unimported_changes(context)
    return super().warn(recursive=recursive, context=context)

  # ---------------------------------- Serialization -----------------------------------

  @override
  @classmethod
  def from_row(cls, row: R | Any) -> Self:
    """Build the model from a row and take its content and time as the last stamp."""
    model: Self = super().from_row(row)
    model._read_modification_time = model.modification_time_s
    model._last_stamp_modification_time = model.modification_time_s
    model._last_stamp_fingerprint = model._read_fingerprint
    return model


class ProtobufModel(CheckedModel):
  """Base for a model that maps one protobuf message onto pydantic attributes.

  A subclass sets `MessageType` to the message its blob decodes to and declares one
  attribute per field it wants to reach. An attribute is matched to a field by its
  alias when it has one. An optional field is annotated `| None`, a repeated one with
  a list, and a message field with another `ProtobufModel`. Defining the subclass
  checks all of that against the message descriptor and rejects the class when they
  disagree.

  A keyword argument that names no attribute is rejected, so a misspelled option
  raises instead of being dropped.

  Reading a blob keeps the source bytes on the model, at every level of nesting, and
  serialization starts from them. A field added by a newer Anki but not yet added to
  the genanki2 model survives a read-update-write cycle and is not dropped, although
  byte order does not survive a re-encode because protobuf serializes unknown fields
  after the known ones.

  Attributes:
    MessageType: The protobuf class the blob decodes to. Set by the subclass; every
      other class attribute is derived from it.
    PB_TO_PYD_NAMES: Protobuf field name to attribute name, for every attribute.
    NESTED_MODELS: Attribute name to the model class its message field holds, for
      every message field.
  """

  # Pydantic
  model_config: ClassVar[pyd.ConfigDict] = pyd.ConfigDict(extra="forbid")

  MessageType: ClassVar[type[Message]]
  PB_TO_PYD_NAMES: ClassVar[dict[str, str]]
  NESTED_MODELS: ClassVar[dict[str, type["ProtobufModel"]]]

  # Protobuf Message
  _source: bytes = pyd.PrivateAttr(default=b"")

  @classmethod
  @override
  def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
    """Bind the subclass to its message and check that the two describe the same fields.

    It fills `PB_TO_PYD_NAMES` from the attribute aliases and names, and `NESTED_MODELS`
    from the annotation of every message field.

    A warning is emitted for each protobuf field the model does not carry. The value
    survives a read-update-write cycle, but it is unreachable from Python.

    Raises:
      TypeError: the subclass sets no `MessageType`.
      TypeError: an attribute accepts `None` while its protobuf field is not optional,
        or vice-versa.
      TypeError: the annotation of a message field names no `ProtobufModel` bound to
        the message that field carries.
    """
    if not hasattr(cls, "MessageType"):
      raise TypeError(
        f"{cls.__name__} subclasses `genanki2.models.base.ProtobufModel` without "
        "setting `MessageType`. Assign it the protobuf class the blob decodes to."
      )

    cls.PB_TO_PYD_NAMES = {
      field_info.alias or name: name for name, field_info in cls.model_fields.items()
    }
    cls.NESTED_MODELS = {}

    descriptor = cls.MessageType.DESCRIPTOR
    pb_fields: set[str] = set(descriptor.fields_by_name)
    pyd_fields: set[str] = set(cls.PB_TO_PYD_NAMES)

    if missing_pyd_fields := pb_fields - pyd_fields:
      warnings.warn(
        f"{cls.__name__} does not model {sorted(missing_pyd_fields)}, carried by "
        f"{descriptor.full_name}. A blob setting them survives a read-update-write "
        f"cycle, since encoding starts from the source bytes, but the values are "
        f"unreachable from the model.",
        Genanki2Warning,
        stacklevel=3,
      )

    mismatched_optional_fields: list[str] = []
    mismatched_nested_fields: list[str] = []
    for pb_name in pb_fields & pyd_fields:
      pb_field = descriptor.fields_by_name[pb_name]
      pyd_name: str = cls.PB_TO_PYD_NAMES[pb_name]
      annotation = cls.model_fields[pyd_name].annotation
      pyd_annotation: tuple[Any, ...] = (annotation, *get_args(annotation))

      if pb_field.has_presence != (NoneType in pyd_annotation):  # match nullability
        mismatched_optional_fields.append(pb_name)

      if pb_field.message_type is not None:  # gather and match nested model types
        try:
          cls.NESTED_MODELS[pyd_name] = next(
            FieldType
            for FieldType in pyd_annotation
            if isinstance(FieldType, type)
            and issubclass(FieldType, ProtobufModel)
            and FieldType.MessageType.DESCRIPTOR is pb_field.message_type
          )
        except StopIteration:
          mismatched_nested_fields.append(pb_name)

    if mismatched_optional_fields:
      raise TypeError(
        f"{cls.__name__} disagrees with {descriptor.full_name} on which fields are "
        f"optional: {mismatched_optional_fields}. An optional protobuf field requires "
        f"`| None` because a blob omitting it reads back as `None`; one that is not "
        f"optional must not have it because `None` has no encoding."
      )
    if mismatched_nested_fields:
      raise TypeError(
        f"{cls.__name__} does not model the messages carried by "
        f"{mismatched_nested_fields}. A message field has to be annotated with a "
        "`genanki2.models.base.ProtobufModel` whose `MessageType` is the message "
        f"{descriptor.full_name} declares for it, since that is what reading the blob "
        "builds and what writing it serializes."
      )

  # ---------------------------------- Serialization -----------------------------------

  @classmethod
  def from_bytes(cls, serialized: bytes) -> Self:
    """Return the model `serialized` decodes to.

    Every attribute mapped to a message field is set from the message, including the
    ones the message left at their protobuf zero, so no model default ever replaces a
    value carried by the blob. A nullable attribute is set to `None` when its field is
    not present in the message.

    The bytes are kept on the model, so a serializing method (e.g.
    `ProtobufModel.to_bytes()`) can start from them.

    Raises:
      google.protobuf.message.DecodeError: parsing the bytes failed.
      pydantic.ValidationError: validation failed.
    """
    protobuf: Message = cls.MessageType.FromString(serialized)
    model_dict: dict[str, Any] = {}

    descriptor = cls.MessageType.DESCRIPTOR
    for pb_name, pyd_name in cls.PB_TO_PYD_NAMES.items():
      if (pb_field := descriptor.fields_by_name.get(pb_name)) is None:
        continue
      pb_value: Any = getattr(protobuf, pb_name)
      if pb_field.has_presence and not protobuf.HasField(pb_name):  # nullable
        model_dict[pyd_name] = None
      elif pb_field.is_repeated:  # list
        if pb_field.message_type is not None:  # of messages
          NestedType: type[ProtobufModel] = cls.NESTED_MODELS[pyd_name]
          model_dict[pyd_name] = [
            NestedType.from_bytes(i.SerializeToString()) for i in pb_value
          ]
        else:  # of scalars
          model_dict[pyd_name] = pb_value
      elif pb_field.message_type is not None:  # message
        NestedType = cls.NESTED_MODELS[pyd_name]
        model_dict[pyd_name] = NestedType.from_bytes(pb_value.SerializeToString())
      else:  # scalar
        model_dict[pyd_name] = pb_value

    model: Self = cls.model_validate(model_dict)
    model._source = serialized
    return model

  def _bytes(self) -> bytes:
    """Encode the model as its protobuf blob, running no phase.

    Encoding starts from the bytes the model was read from, so a field added by a
    newer Anki and not modeled is carried through instead of dropped. Then the
    attributes present in the model are applied. Also applies to nested models.

    An attribute equal to its protobuf zero encodes to nothing rather than to an
    explicit zero, so the output stays identical to the bytes Anki wrote. A nullable
    attribute on an optional field is written when it is not `None`.

    A blob holding an unnamed field re-encodes to the same values but byte order is
    not preserved by Protobuf because unknown fields are serialized after the known
    ones.
    """
    protobuf: Message = self.MessageType.FromString(self._source)

    descriptor = self.MessageType.DESCRIPTOR
    for pb_name, pyd_name in self.PB_TO_PYD_NAMES.items():
      if (pb_field := descriptor.fields_by_name.get(pb_name)) is None:
        continue
      pyd_value: Any = getattr(self, pyd_name)
      if pb_field.has_presence and pyd_value is None:  # nullable
        protobuf.ClearField(pb_name)
      elif pb_field.is_repeated:  # list
        protobuf.ClearField(pb_name)
        if pb_field.message_type is not None:  # of messages
          for item in pyd_value:
            getattr(protobuf, pb_name).add().ParseFromString(item._bytes())
        else:  # of scalars
          getattr(protobuf, pb_name).extend(pyd_value)
      elif pb_field.message_type is not None:  # message
        getattr(protobuf, pb_name).ParseFromString(pyd_value._bytes())
      else:  # scalar
        setattr(protobuf, pb_name, pyd_value)

    return protobuf.SerializeToString()

  def to_bytes(self, *, context: SerializationContext | None = None) -> bytes:
    """Return this model as the protobuf blob it maps to.

    Encoding starts from the bytes the model was read from, so a field added by a
    newer Anki and not modeled is carried through instead of dropped. Then the
    attributes present in the model are applied. Also applies to nested models.

    An attribute equal to its protobuf zero encodes to nothing rather than to an
    explicit zero, so the output stays identical to the bytes Anki wrote. A nullable
    attribute on an optional field is written when it is not `None`.

    A blob holding an unnamed field re-encodes to the same values but byte order is
    not preserved by Protobuf because unknown fields are serialized after the known
    ones.

    `CheckedModel.prepare()`, `CheckedModel.check()` and `CheckedModel.warn()` run
    first, so a subclass rejecting a configuration here rejects it for every write,
    nested ones included.

    If no context is given, one is created with output
    `SerializationContext.Output.BYTES` and passed to the phases.

    Args:
      context: Extra information from the serialization process, passed to the phases.
        Default: `None`.
    """
    if context is None:
      context = SerializationContext(output=SerializationContext.Output.BYTES)
    return self._run_phases(context=context)._bytes()
