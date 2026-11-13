"""Does the rollover calculation agree with Anki?

    uv run --with pytest pytest tests/test_datetime.py

`rollover_datetime` decides `col.crt`, which fixes the day boundary for the life of the
collection, so it is checked against Anki's `v1_creation_date_inner` rather than against
what 4am local means in the abstract. The two differ across a DST boundary, and Anki is
the one that has to be matched.

`days_elapsed` is the other half: the count the importer takes off both collections to
shift review due dates by their difference. Anki computes it two ways under the v2
scheduler, picked by whether the collection records the offset it was created in
(`sched_timing_today`). With it, `sched_timing_today_v2_new` counts local calendar dates
crossed and subtracts one until the day has turned over; without it,
`sched_timing_today_v2_legacy` counts 24-hour periods from the creation date's rollover.
Both are exercised here, since a collection read rather than written can be either.
"""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from genanki2.datetime import days_elapsed, rollover_datetime, utc_offset_minutes_west


def frozen(local: str, tz: str = "Europe/Rome") -> dt.datetime:
  """A machine in `tz` whose clock reads `local`.

  `dt.datetime.now().astimezone()` yields local wall time carrying a *fixed* offset,
  the one in force at that instant, so arguments are built the same way.
  """
  aware = dt.datetime.fromisoformat(local).replace(tzinfo=ZoneInfo(tz))
  return aware.replace(tzinfo=dt.timezone(aware.utcoffset()))


def at(local: str, tz: str = "Europe/Rome") -> dt.datetime:
  """`rollover_datetime` as called from that machine."""
  return rollover_datetime(frozen(local, tz))


def elapsed(
  created: str, now: str, tz: str = "Europe/Rome", *, creation_offset: bool = False
) -> int:
  """`days_elapsed` as called from that machine, for a collection created at `created`.

  `creation_offset` picks the branch: with it, the collection knows the offset it was
  created in and Anki counts calendar dates; without it, Anki falls back to counting
  24-hour periods from the creation date's rollover.
  """
  created_dt = frozen(created, tz)
  return days_elapsed(
    int(created_dt.timestamp()),
    frozen(now, tz),
    utc_offset_minutes_west(created_dt) if creation_offset else None,
  )


@pytest.mark.parametrize(
  "now,expected",
  [
    ("2026-08-06 05:00:00", "2026-08-06 04:00:00"),  # after today's rollover
    ("2026-08-06 04:00:00", "2026-08-06 04:00:00"),  # exactly on it
    ("2026-08-06 23:59:59", "2026-08-06 04:00:00"),  # late the same day
    ("2026-08-06 03:59:59", "2026-08-05 04:00:00"),  # a second before it
    ("2026-08-06 01:00:00", "2026-08-05 04:00:00"),  # the small hours
    ("2026-08-06 00:00:00", "2026-08-05 04:00:00"),  # midnight
  ],
)
def test_rollover_picks_the_last_four_am(now, expected):
  assert at(now).replace(tzinfo=None) == dt.datetime.fromisoformat(expected)


@pytest.mark.parametrize("hour", range(24))
def test_rollover_is_never_in_the_future(hour):
  """A crt ahead of now makes days_elapsed negative (rslib/src/scheduler/timing.rs)."""
  now = dt.datetime.fromisoformat("2026-08-06 00:30:00") + dt.timedelta(hours=hour)
  rollover = at(now.isoformat(sep=" ")).replace(tzinfo=None)
  assert rollover <= now
  assert now - rollover < dt.timedelta(days=1)


def test_rollover_is_timezone_aware():
  """crt is written as a Unix timestamp, so the result must know its own offset."""
  assert at("2026-08-06 12:00:00").utcoffset() == dt.timedelta(hours=2)
  assert at("2026-01-06 12:00:00").utcoffset() == dt.timedelta(hours=1)
  assert at("2026-08-06 12:00:00", tz="UTC").utcoffset() == dt.timedelta(0)
  assert at("2026-08-06 12:00:00", tz="America/New_York") == dt.datetime(
    2026, 8, 6, 4, tzinfo=dt.timezone(-dt.timedelta(hours=4))
  )


def test_rollover_defaults_to_the_system_clock():
  """The no-argument path is the one write_sqlite uses."""
  before = dt.datetime.now().astimezone()
  rollover = rollover_datetime()
  after = dt.datetime.now().astimezone()
  assert rollover.tzinfo is not None
  assert rollover <= after
  assert before - rollover < dt.timedelta(days=1)
  assert (rollover.hour, rollover.minute, rollover.second) == (4, 0, 0)


def test_rollover_keeps_the_offset_frozen_across_a_dst_boundary():
  """Anki freezes the offset too, so matching it means reproducing this.

  Rome springs forward at 02:00 on 2026-03-29. Asked at 03:30 that day, the last 4am
  is yesterday's, and `- 86_400` seconds from a +02:00 datetime lands on 04:00+02:00,
  which was 03:00 wall clock on the 28th. Anki does the same: it builds `now_dt` in a
  single FixedOffset taken from `now` and subtracts a flat 86_400
  (rslib/src/scheduler/timing.rs:102-113), never re-resolving the zone for the target
  date. Re-resolving through zoneinfo would give 04:00+01:00 and a crt an hour later
  than the one Anki writes.
  """
  rome = ZoneInfo("Europe/Rome")

  rollover = at("2026-03-29 03:30:00")
  assert rollover == dt.datetime(
    2026, 3, 28, 4, tzinfo=dt.timezone(dt.timedelta(hours=2))
  )
  assert int(rollover.timestamp()) == 1774663200
  assert rollover.astimezone(rome).hour == 3  # not 4, and that is Anki's answer

  # Once past 4am on the transition day, and on the autumn boundary, they agree.
  assert at("2026-03-29 05:00:00").astimezone(rome).hour == 4
  assert at("2026-10-25 01:30:00").astimezone(rome) == dt.datetime(
    2026, 10, 24, 4, tzinfo=rome
  )


LATE_IN_THE_DAY = "2026-08-06 23:00:00"


@pytest.mark.parametrize("hour", [0, 4, 23])
def test_a_rollover_hour_in_range_is_used_as_given(hour):
  assert rollover_datetime(frozen(LATE_IN_THE_DAY), hour).hour == hour


@pytest.mark.parametrize("hour", [24, 25, 100])
def test_a_rollover_hour_past_the_end_of_the_day_is_the_last_hour(hour):
  """Anki caps the value as it reads it, with `.min(23)` (rslib/src/config/mod.rs:186-189)."""
  assert rollover_datetime(frozen(LATE_IN_THE_DAY), hour).hour == 23


@pytest.mark.parametrize("hour", [-1, -4])
def test_a_negative_rollover_hour_falls_back_to_four(hour):
  """Anki reads the value into a `u8`, so a negative number fails to parse and the key
  counts as never written. The caller then substitutes 4, and writes it back into the
  collection (rslib/src/scheduler/mod.rs:64-74), which is why the fallback is 4 rather
  than the nearest legal hour."""
  assert rollover_datetime(frozen(LATE_IN_THE_DAY), hour).hour == 4


A_CREATION_AT_ROLLOVER = "2026-08-06 04:00:00"

# Whether the collection records the offset it was created in, which is what decides
# between Anki's two v2 day counts. They agree on everything below.
BOTH_BRANCHES = [True, False]


@pytest.mark.parametrize("creation_offset", BOTH_BRANCHES)
@pytest.mark.parametrize(
  "now,expected",
  [
    ("2026-08-06 10:00:00", 0),  # the same day
    ("2026-08-07 03:00:00", 0),  # the next date, but the day has not turned over yet
    ("2026-08-07 05:00:00", 1),  # the next date, past the rollover
    ("2026-08-11 01:00:00", 4),  # five dates on, before the rollover
    ("2026-08-11 12:00:00", 5),  # the same dates, after it
  ],
)
def test_days_elapsed_counts_rollovers_not_24_hour_periods(
  now, expected, creation_offset
):
  assert (
    elapsed(A_CREATION_AT_ROLLOVER, now, creation_offset=creation_offset) == expected
  )


@pytest.mark.parametrize("creation_offset", BOTH_BRANCHES)
def test_days_elapsed_from_a_creation_at_another_hour(creation_offset):
  """A crt Anki did not write, which is what `from_row` hands over."""
  assert (
    elapsed(
      "2026-08-06 23:00:00", "2026-08-07 10:00:00", creation_offset=creation_offset
    )
    == 1
  )


@pytest.mark.parametrize("creation_offset", BOTH_BRANCHES)
def test_days_elapsed_is_zero_for_a_creation_in_the_future(creation_offset):
  """`days.max(0)` (rslib/src/scheduler/timing.rs:79)."""
  assert (
    elapsed(
      "2026-08-07 04:00:00", "2026-08-06 10:00:00", creation_offset=creation_offset
    )
    == 0
  )


@pytest.mark.parametrize("creation_offset", BOTH_BRANCHES)
def test_days_elapsed_counts_local_dates_east_of_utc(creation_offset):
  """Anki dates both ends in a local offset (rslib/src/scheduler/timing.rs:34-35).

  At +12:00 a 4am crt is the previous date in UTC, so a collection created this morning
  is already a day old to anything counting UTC dates.
  """
  assert (
    elapsed(
      A_CREATION_AT_ROLLOVER,
      "2026-08-06 10:00:00",
      tz="Pacific/Auckland",
      creation_offset=creation_offset,
    )
    == 0
  )


@pytest.mark.parametrize("creation_offset", BOTH_BRANCHES)
def test_days_elapsed_counts_local_dates_west_of_utc(creation_offset):
  assert (
    elapsed(
      A_CREATION_AT_ROLLOVER,
      "2026-08-07 02:00:00",
      tz="America/New_York",
      creation_offset=creation_offset,
    )
    == 0
  )


@pytest.mark.parametrize("creation_offset", BOTH_BRANCHES)
@pytest.mark.parametrize(
  "now,expected",
  [("2026-03-29 03:30:00", 3), ("2026-03-29 05:00:00", 4)],
)
def test_days_elapsed_across_a_dst_boundary(now, expected, creation_offset):
  """Rome springs forward at 02:00 on 2026-03-29, so that date is 23 hours long."""
  assert (
    elapsed("2026-03-25 04:00:00", now, creation_offset=creation_offset) == expected
  )


@pytest.mark.parametrize(
  "offset_hours,expected",
  [(2, -120), (-8, 480), (0, 0), (5.5, -330)],
)
def test_utc_offset_minutes_is_negated(offset_hours, expected):
  """config's creationOffset is minutes *west* of UTC, the opposite sign to utcoffset."""
  aware = dt.datetime(2026, 8, 6, tzinfo=dt.timezone(dt.timedelta(hours=offset_hours)))
  assert utc_offset_minutes_west(aware) == expected


def test_utc_offset_minutes_rejects_a_naive_datetime():
  with pytest.raises(ValueError):
    utc_offset_minutes_west(dt.datetime(2026, 8, 6))  # naive is the point
