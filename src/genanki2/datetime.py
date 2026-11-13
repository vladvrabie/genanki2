from datetime import datetime, timedelta, timezone

from genanki2.utils import is_uint8

__all__ = [
  "days_elapsed",
  "rollover_datetime",
  "utc_offset_minutes_west",
]


def rollover_datetime(
  dt: datetime | None = None, rollover_hour: int = 4, *, round_back: bool = True
) -> datetime:
  """Compute when the day turns over, relative to `dt`.

  Anki defaults: 4AM local and rounding back.

  Args:
    dt: Date for which to compute the rollover. Can be non-tz-aware. If `None`, it
      defaults to `datetime.now().astimezone()`.
    rollover_hour: When the day ends and a new one starts. It is usually set using the
      `rollover` config value. Any UInt8 value is clamped to [0, 23], otherwise the
      default is used. Default: 4.
    round_back: If true, returns the last rollover at or before `dt`, else the one on
      `dt`'s own date. Default: `True`.

  Returns:
    The rollover instant itself, in `dt`'s timezone, with minutes, seconds and
    microseconds zeroed.
  """
  dt = dt if dt is not None else datetime.now().astimezone()
  rollover_hour = min(rollover_hour, 23) if is_uint8(rollover_hour) else 4
  rollover_dt: datetime = dt.replace(
    hour=rollover_hour, minute=0, second=0, microsecond=0
  )
  if round_back and rollover_dt > dt:
    rollover_dt -= timedelta(days=1)
  return rollover_dt


def utc_offset_minutes_west(dt: datetime) -> int:
  """Return the UTC offset of `dt` in minutes west.

  The sign convention Anki uses:
  - +02:00 -> -120
  - -08:00 -> 480

  Raises:
    ValueError: `dt` is not timezone aware.
  """
  if (offset := dt.utcoffset()) is None:
    raise ValueError("Provided date is not timezone aware.")
  return -int(offset.total_seconds() // 60)


def days_elapsed(
  creation_time_s: int,
  now: datetime | None = None,
  creation_offset_minutes_west: int | None = None,
  rollover_hour: int = 4,
) -> int:
  """Count the rollovers that have passed since the collection was created.

  A day is counted when time passed the rollover hour.

  Important: Anki has two counts and `creation_offset_minutes_west` picks which one is
  used. With an offset (not `None`), local dates are counted since rollover. Without
  it, whole 24-hour periods are counted from the creation date's rollover.

  Set `creation_offset_minutes_west` using the `creationOffset` config value, as that
  is the expected value.

  The count is floored at 0: a creation time in the future cannot return negative
  values.

  Note: This function assumes the v2 scheduler is used.

  Args:
    creation_time_s: Set using the collection metadata creation time.
    now: If `None`, defaults to the system clock. Its offset is used when no creation
      offset is provided.
    creation_offset_minutes_west: Creation offset. See `utc_offset_minutes_west()` for
      details. Selects the counting method (whether it's timezone aware or not). Values
      outside the interval (-1440, 1440) are rejected and the default value is used.
      Default: `None`.
    rollover_hour: Set using the `rollover` config value. Default: 4.
  """
  now = now if now is not None else datetime.now().astimezone()
  offset: int | None = creation_offset_minutes_west
  offset = offset if offset is not None and abs(offset) < 1440 else None
  if offset is not None:
    creation_tz = timezone(timedelta(minutes=-offset))
    creation_dt: datetime = datetime.fromtimestamp(creation_time_s, creation_tz)
    delta_days: int = (now.date() - creation_dt.date()).days
    rollover_passed: bool = rollover_datetime(now, rollover_hour).date() == now.date()
    return max(0, delta_days if rollover_passed else delta_days - 1)
  else:
    creation_tz = now.tzinfo
    creation_dt = datetime.fromtimestamp(creation_time_s, creation_tz)
    return max(
      0, (now - rollover_datetime(creation_dt, rollover_hour, round_back=False)).days
    )
