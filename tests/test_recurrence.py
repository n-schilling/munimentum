"""recurrence.py: the rule the export writes, read back, and the dates it
makes – on the series' own clock."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import recurrence

BERLIN = ZoneInfo("Europe/Berlin")


def _starts(rule, start, **kw):
    return recurrence.expand(start, recurrence.parse_rrule(rule), **kw)


def test_a_rule_reads_back_as_written():
    rule = recurrence.parse_rrule("FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=1;INTERVAL=2;WKST=SU;UNTIL=20270101T225959Z")
    assert rule["freq"] == "MONTHLY" and rule["interval"] == 2 and rule["wkst"] == 6
    assert rule["byday"] == [(0, 0), (0, 1), (0, 2), (0, 3), (0, 4)] and rule["bysetpos"] == [1]
    assert rule["until"] == "20270101T225959Z"
    assert recurrence.parse_rrule("FREQ=WEEKLY;BYDAY=-1FR")["byday"] == [(-1, 4)]
    assert recurrence.parse_rrule("") is None and recurrence.parse_rrule("FREQ=SECONDLY") is None


def test_a_weekly_series_keeps_its_wall_clock_across_the_clock_change():
    """13:00 in Berlin on both sides of the change to summer time – in UTC
    that is 12:00 and then 11:00. Made in UTC it would drift by an hour."""
    starts = _starts("FREQ=WEEKLY;BYDAY=MO", datetime(2026, 3, 16, 13, 0, tzinfo=BERLIN),
                     horizon_end=datetime(2026, 4, 10, tzinfo=UTC))
    assert [s.strftime("%Y-%m-%d %H:%M %Z") for s in starts] == [
        "2026-03-16 13:00 CET", "2026-03-23 13:00 CET", "2026-03-30 13:00 CEST", "2026-04-06 13:00 CEST"]
    assert [s.astimezone(UTC).hour for s in starts] == [12, 12, 11, 11]


def test_every_weekday_every_second_week_and_the_week_start():
    start = datetime(2026, 1, 5, 12, 0, tzinfo=BERLIN)          # a Monday
    days = _starts("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;INTERVAL=2", start,
                   horizon_end=datetime(2026, 1, 25, tzinfo=UTC))
    assert [d.day for d in days] == [5, 6, 7, 8, 9, 19, 20, 21, 22, 23]
    # Sunday as the week's first day: a Sunday belongs to the week before Monday.
    start = datetime(2026, 1, 11, 9, 0, tzinfo=BERLIN)          # a Sunday
    days = _starts("FREQ=WEEKLY;BYDAY=SU,MO;INTERVAL=2;WKST=SU", start,
                   horizon_end=datetime(2026, 2, 1, tzinfo=UTC))
    assert [d.day for d in days] == [11, 12, 25, 26]
    days = _starts("FREQ=WEEKLY;BYDAY=SU,MO;INTERVAL=2", start, horizon_end=datetime(2026, 2, 1, tzinfo=UTC))
    assert [d.day for d in days] == [11, 19, 25]                 # Monday-first weeks: Sunday ends a week


def test_monthly_rules_by_day_of_month_and_by_ordinal():
    start = datetime(2026, 1, 31, 10, 0, tzinfo=BERLIN)
    days = _starts("FREQ=MONTHLY;BYMONTHDAY=31", start, horizon_end=datetime(2026, 6, 1, tzinfo=UTC))
    # A month without a 31st has the date on its last day, as Exchange places it.
    assert [(d.month, d.day) for d in days] == [(1, 31), (2, 28), (3, 31), (4, 30), (5, 31)]
    days = _starts("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29", date(2024, 2, 29), horizon_end=date(2026, 3, 1))
    assert days == [date(2024, 2, 29), date(2025, 2, 28), date(2026, 2, 28)]
    start = datetime(2026, 1, 30, 10, 0, tzinfo=BERLIN)        # the last Friday of January 2026
    days = _starts("FREQ=MONTHLY;BYDAY=-1FR", start, horizon_end=datetime(2026, 4, 1, tzinfo=UTC))
    assert [(d.month, d.day) for d in days] == [(1, 30), (2, 27), (3, 27)]
    # "The first weekday of the month" – Graph's relativeMonthly with five days.
    start = datetime(2026, 1, 1, 8, 0, tzinfo=BERLIN)           # a Thursday
    days = _starts("FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=1", start, horizon_end=datetime(2026, 4, 1, tzinfo=UTC))
    assert [(d.month, d.day) for d in days] == [(1, 1), (2, 2), (3, 2)]
    days = _starts("FREQ=MONTHLY;BYDAY=SA,SU;BYSETPOS=-1", start, horizon_end=datetime(2026, 3, 1, tzinfo=UTC))
    assert [(d.month, d.day) for d in days] == [(1, 31), (2, 28)]  # the last weekend day


def test_yearly_rules():
    start = date(2026, 5, 1)
    days = _starts("FREQ=YEARLY;BYMONTH=5;BYMONTHDAY=1", start, horizon_end=date(2029, 1, 1))
    assert days == [date(2026, 5, 1), date(2027, 5, 1), date(2028, 5, 1)]
    start = datetime(2026, 11, 26, 18, 0, tzinfo=BERLIN)        # the fourth Thursday of November
    days = _starts("FREQ=YEARLY;BYMONTH=11;BYDAY=4TH", start, horizon_end=datetime(2028, 12, 31, tzinfo=UTC))
    assert [(d.year, d.day) for d in days] == [(2026, 26), (2027, 25), (2028, 23)]


def test_until_count_exdate_and_the_cap():
    start = date(2026, 1, 1)
    assert _starts("FREQ=DAILY;UNTIL=20260103", start) == [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]
    assert len(_starts("FREQ=DAILY;COUNT=5", start)) == 5
    # An exdate takes a date out but does not add one at the end.
    assert _starts("FREQ=DAILY;COUNT=3", start, exdates={"20260102"}) == [date(2026, 1, 1), date(2026, 1, 3)]
    start = datetime(2026, 1, 1, 23, 30, tzinfo=BERLIN)
    # UNTIL is an instant: 22:59:59Z on the 3rd is 23:59:59 Berlin – the 3rd is in.
    assert len(_starts("FREQ=DAILY;UNTIL=20260103T225959Z", start)) == 3
    assert len(_starts("FREQ=DAILY", start, cap=10)) == 10
    assert _starts("FREQ=DAILY", start, horizon_end=datetime(2026, 1, 1, tzinfo=UTC)) == []


def test_wall_keys_and_zones():
    when = datetime(2026, 3, 30, 13, 0, tzinfo=BERLIN)
    assert recurrence.wall_key(when) == "20260330T130000" and recurrence.wall_key(date(2026, 3, 30)) == "20260330"
    assert recurrence.parse_wall("20260330T110000Z", BERLIN) == when
    assert recurrence.parse_wall("20260330T130000", BERLIN) == when
    assert recurrence.parse_wall("20260330") == date(2026, 3, 30)
    assert recurrence.parse_wall("nonsense") is None
    assert recurrence.iana("W. Europe Standard Time") == "Europe/Berlin"
    assert recurrence.iana("Europe/Berlin") == "Europe/Berlin" and recurrence.iana("UTC") == "UTC"
    assert recurrence.iana("tzone://Microsoft/Custom") is None and recurrence.zone("") is None


def test_a_series_is_set_up_from_its_ics_lines_and_stops_at_the_horizon():
    """What the index and the view share: DTSTART with its TZID, the RRULE
    text and the EXDATEs as (value, TZID) pairs become the start on the
    series' clock, the rule and the keys taken out; a "Z" start counts in
    UTC, an all-day series in dates."""
    start, rule, out, tz = recurrence.series_setup(
        "20260105T123000", "Europe/Berlin", False, "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
        [("20260113T123000", "Europe/Berlin"), ("20260114T103000Z", None)])
    assert start == datetime(2026, 1, 5, 12, 30, tzinfo=BERLIN) and tz is BERLIN
    assert rule["freq"] == "WEEKLY" and out == {"20260113T123000", "20260114T113000"}
    assert recurrence.series_setup("20260105T113000Z", "", False, "FREQ=DAILY", [])[3] is UTC
    assert recurrence.series_setup("20260105", "", True, "FREQ=DAILY", [])[0] == date(2026, 1, 5)
    assert recurrence.series_setup("nonsense", "", False, "FREQ=DAILY", []) is None
    # The horizon is counted in days, so a leap day is no trap; the
    # timestamps are instants, an all-day date its local midnight.
    assert recurrence.horizon(datetime(2028, 2, 29, tzinfo=UTC)) == datetime(2030, 2, 28, tzinfo=UTC)
    stamps = list(recurrence.timestamps(start, rule, out, datetime(2026, 1, 16, tzinfo=UTC)))
    assert len(stamps) == 8 and stamps[0] == start.timestamp()
    assert list(recurrence.timestamps(date(2026, 1, 5), recurrence.parse_rrule("FREQ=DAILY;COUNT=2"),
                                      set(), datetime(2027, 1, 1, tzinfo=UTC))) == [
        datetime(2026, 1, 5).timestamp(), datetime(2026, 1, 6).timestamp()]


def test_a_rule_outside_the_subset_is_the_first_date_alone():
    start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    assert recurrence.expand(start, None) == [start]
    assert recurrence.expand(start, recurrence.parse_rrule("FREQ=HOURLY")) == [start]


def test_the_cap_keeps_the_newest_dates_and_the_window_start_leaves_the_past_out():
    """A daily series since 2015 has more dates than the cap by the time
    one looks: the cap keeps this year's, not the first ones – and from
    the window's start the past is not even made."""
    start = datetime(2015, 1, 1, 9, 0, tzinfo=BERLIN)
    out = _starts("FREQ=DAILY", start, horizon_end=datetime(2026, 10, 5, tzinfo=UTC))
    assert len(out) == recurrence.CAP and out[-1].date() == date(2026, 10, 4)
    out = _starts("FREQ=DAILY", start, horizon_end=datetime(2026, 10, 5, tzinfo=UTC),
                  horizon_start=datetime(2026, 9, 1, tzinfo=UTC))
    assert [out[0].date(), out[-1].date(), len(out)] == [date(2026, 9, 1), date(2026, 10, 4), 34]
    # COUNT still counts from the first date, the window only hides them.
    assert _starts("FREQ=DAILY;COUNT=3", date(2026, 1, 1), horizon_start=date(2026, 1, 3),
                   horizon_end=date(2026, 2, 1)) == [date(2026, 1, 3)]


def test_a_wall_time_the_clock_change_skips_lands_where_the_clock_does():
    """02:30 does not exist on the night the clocks go forward: Exchange
    puts that date at 03:30, and so does the rule – the EXDATE and the
    RECURRENCE-ID the export writes for it (from Graph's 01:30Z) match."""
    starts = _starts("FREQ=WEEKLY;BYDAY=SU", datetime(2026, 3, 22, 2, 30, tzinfo=BERLIN),
                     horizon_end=datetime(2026, 4, 6, tzinfo=UTC))
    assert [s.strftime("%m-%d %H:%M %Z") for s in starts] == ["03-22 02:30 CET", "03-29 03:30 CEST", "04-05 02:30 CEST"]
    assert recurrence.wall_key(starts[1]) == recurrence.key_of("20260329T013000Z", "", BERLIN)


def test_the_earlier_form_of_a_relative_rule_reads_as_the_set_with_a_position():
    """Releases before 14.3 wrote "the first weekday of the month" as
    BYDAY=1MO,1TU,1WE,1TH,1FR – read to the letter that is five dates a
    month. Such a series that ended before the window is never written
    again, so the old form is read as it was meant."""
    rule = recurrence.parse_rrule("FREQ=MONTHLY;BYDAY=1MO,1TU,1WE,1TH,1FR")
    assert rule["bysetpos"] == [1] and rule["byday"] == [(0, 0), (0, 1), (0, 2), (0, 3), (0, 4)]
    days = recurrence.expand(date(2026, 1, 1), rule, horizon_end=date(2026, 3, 31))
    assert [(d.month, d.day) for d in days] == [(1, 1), (2, 2), (3, 2)]
    # One day with an ordinal stays what it is; a weekly rule has no ordinals to misread.
    assert recurrence.parse_rrule("FREQ=MONTHLY;BYDAY=-1FR")["byday"] == [(-1, 4)]
    assert recurrence.parse_rrule("FREQ=WEEKLY;BYDAY=1MO,1TU")["bysetpos"] == []


def test_the_window_start_and_the_key_of_an_ics_value():
    assert recurrence.months_back(datetime(2026, 3, 31, 15, 30, tzinfo=UTC), 1) == datetime(2026, 2, 28, tzinfo=UTC)
    assert recurrence.months_back(datetime(2026, 3, 31, tzinfo=UTC), 14) == datetime(2025, 1, 31, tzinfo=UTC)
    assert recurrence.months_back(datetime(2026, 3, 31, tzinfo=UTC), 0) is None
    assert recurrence.key_of("20260113T123000", "Europe/Berlin", UTC) == "20260113T123000"
    assert recurrence.key_of("20260114T103000Z", "", BERLIN) == "20260114T113000"
    assert recurrence.key_of("20260114T103000", "W. Europe Standard Time", UTC) == "20260114T103000"
    assert recurrence.key_of("nonsense", "", BERLIN) is None and recurrence.key_of("", "", BERLIN) is None


def test_a_bound_already_a_date_stays_one():
    assert recurrence.bound(date(2026, 1, 1), date(2026, 2, 1)) == date(2026, 2, 1)
    assert recurrence.bound(date(2026, 1, 1), datetime(2026, 2, 1, 12, tzinfo=UTC)) == date(2026, 2, 1)
    assert recurrence.bound(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)) == datetime(2026, 2, 1, tzinfo=UTC)
