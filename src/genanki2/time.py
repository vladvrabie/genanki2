from time import time, time_ns

__all__ = ["time_ms", "time_s"]


def time_s(timestamp: float | None = None) -> int:
  """Convert `timestamp` to seconds.

  If `None`, convert the current time in seconds since the Epoch.
  """
  return int(timestamp if timestamp is not None else time())


def time_ms(timestamp: float | None = None) -> int:
  """Convert `timestamp` to milliseconds.

  If `None`, convert the current time in milliseconds since the Epoch.
  """
  return int(timestamp * 1000) if timestamp is not None else (time_ns() // 1_000_000)
