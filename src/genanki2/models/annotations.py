import math
from typing import Any, Protocol

import pydantic as pyd
from google.protobuf.message import DecodeError as ProtobufDecodeError

from genanki2.idgen import guid, monotonic_id
from genanki2.time import time_ms, time_s
from genanki2.utils import float32 as f32

### Aliases and Fields

MTimeSecsAlias = pyd.Field(alias="mtime_secs")
ModAlias = pyd.Field(alias="mod")
UsnField = pyd.Field(ge=-(2**31), le=2**31 - 1, alias="usn")


### Numeric Constraints

Id = pyd.Field(gt=0, le=2**63 - 1)
Int32 = pyd.Field(ge=-(2**31), le=2**31 - 1)
Int64 = pyd.Field(ge=-(2**63), le=2**63 - 1)
NonNegative = pyd.Field(ge=0)
UInt8 = pyd.Field(ge=0, le=2**8 - 1)
UInt16 = pyd.Field(ge=0, le=2**16 - 1)
UInt32 = pyd.Field(ge=0, le=2**32 - 1)
UInt63 = pyd.Field(ge=0, le=2**63 - 1)


def Bounded(
  minimum: float, maximum: float, default: float, float32: bool = False
) -> pyd.AfterValidator:
  """Return a validator replacing a value outside `minimum` to `maximum` with `default`.

  Args:
    minimum: Smallest accepted value.
    maximum: Largest accepted value.
    default: What replaces a value outside the interval.
    float32: Compare the value and both bounds as 32-bit floats. The value is returned
      as given. Default: `False`.

  Raises:
    ValueError: default lies outside minimum to maximum or minimum is greater than
      maximum.
  """
  if float32:
    minimum, maximum = f32(minimum), f32(maximum)

  if not (minimum <= default <= maximum):
    msg: str = "Values don't respect the condition `minimum <= default <= maximum`."
    raise ValueError(msg)

  def bounded_value_or_default(value: float) -> float:
    value_: float = f32(value) if float32 else value
    return value if (minimum <= value_ <= maximum) else default

  return pyd.AfterValidator(bounded_value_or_default)


def Clamped(minimum: float, maximum: float) -> pyd.AfterValidator:
  """Return a validator clamping a value outside `minimum` to `maximum`.

  Args:
    minimum: Smallest accepted value.
    maximum: Largest accepted value.

  Raises:
    ValueError: minimum is greater than maximum.
  """

  if minimum > maximum:
    raise ValueError(f"Minimum {minimum} must be smaller than the maximum {maximum}.")

  def clamp(value: float) -> float:
    return max(minimum, min(value, maximum))

  return pyd.AfterValidator(clamp)


### Size Constraints

NotEmpty = pyd.Field(min_length=1)


### Default factories

Guid = pyd.Field(default_factory=guid)
MonotonicId = pyd.Field(default_factory=monotonic_id)
TimeS = pyd.Field(default_factory=time_s)
TimeMs = pyd.Field(default_factory=time_ms)


### Type constraints

LtRUnion = pyd.Field(union_mode="left_to_right")


### Float serializer


def Rounded(places: int) -> pyd.PlainSerializer:
  """Return a serializer that rounds floats to the number of decimals given."""
  factor: float = 10.0**places

  def serialize(value: float | None) -> float | None:
    return None if value is None else math.floor(value * factor + 0.5) / factor

  return pyd.PlainSerializer(serialize, return_type=float | None)


### Bytes column decoder


class DecodesBytes(Protocol):
  """A model a blob column decodes into."""

  @classmethod
  def from_bytes(cls, obj: bytes) -> "DecodesBytes": ...


def BytesDecoder(cls: type[DecodesBytes]) -> pyd.BeforeValidator:
  """Return a validator decoding a blob column into `cls`.

  Raises:
    ValueError: the protobuf message can't be decoded.
  """

  def decode(value: Any) -> Any:
    try:
      return cls.from_bytes(value) if isinstance(value, bytes) else value
    except ProtobufDecodeError as error:
      msg: str = f"The protobuf message can't be decoded. Error: {error}"
      raise ValueError(msg) from error

  return pyd.BeforeValidator(decode)


### JSON column decoder


class DecodesJSON(Protocol):
  """A model a JSON column decodes into."""

  @classmethod
  def from_json(cls, json_data: str) -> "DecodesJSON": ...


def JSONDecoder(cls: type[DecodesJSON]) -> pyd.BeforeValidator:
  """Return a validator decoding a JSON column into `cls`."""

  def decode(value: Any) -> Any:
    return cls.from_json(value) if isinstance(value, str) else value

  return pyd.BeforeValidator(decode)
