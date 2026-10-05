#!/usr/bin/env python3
"""
recurrence.py – a series of appointments: the rule, and the dates it makes.

The export writes a series as ONE .ics (outlook_export.build_ics): the
master VEVENT with DTSTART in the series' own time zone and an RRULE, an
EXDATE for every date taken out of the series, and one more VEVENT per
exception – a date moved, renamed or cancelled on its own – whose
RECURRENCE-ID names the date it replaces. The calendar view expands that
back into dates here (combined_search.read_calendar), and so does the
index for its date filter (rag_index._series_rows).

Everything is counted on the series' own clock: a 13:00 Berlin meeting
stays 13:00 across the change to summer time, which it would not if the
dates were made in UTC. RECURRENCE-ID and EXDATE find their occurrence by
that wall-clock key (`wall_key`), never by an instant. A wall time the
clock change skips (02:30 on the night the clocks go forward) lands where
the clock does, 03:30 – as Exchange places that date.

The rule is the subset the export itself writes (build_rrule): FREQ,
INTERVAL, BYDAY (with an ordinal), BYMONTHDAY, BYMONTH, BYSETPOS, WKST,
UNTIL and COUNT – what Graph's six recurrence patterns need, nothing more.
A rule outside that subset expands to its first date only.
"""

from calendar import monthrange
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]

# How many dates of one series the view and the index keep: the newest
# ones up to the horizon – a daily series without an end, ten years of it,
# is some 3 650, beyond that the view is noise.
CAP = 4000

# How far ahead a series without an end is unfolded – the calendar view
# and the index agree on it (horizon()).
YEARS_AHEAD = 2

# How many periods a rule is walked for its dates at most – a century of
# days, weeks, months or years – so a rule that never matches (the 30th
# of February) still ends.
_PERIODS = {"DAILY": 40000, "WEEKLY": 5300, "MONTHLY": 1250, "YEARLY": 110}

# Windows time zone names (what Graph's originalStartTimeZone, the
# recurrence and Exchange's invitation mails carry) -> IANA. The table
# only grows: a name missing here leaves a series in UTC and a mail's
# appointment in local time, and nothing but the log says so.
WIN_TZ = {
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw",
    "Romance Standard Time": "Europe/Paris",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Etc/UTC",
    "E. Europe Standard Time": "Europe/Chisinau",
    "FLE Standard Time": "Europe/Helsinki",
    "GTB Standard Time": "Europe/Athens",
    "Russian Standard Time": "Europe/Moscow",
    "Turkey Standard Time": "Europe/Istanbul",
    "Israel Standard Time": "Asia/Jerusalem",
    "Arabian Standard Time": "Asia/Dubai",
    "Arab Standard Time": "Asia/Riyadh",
    "India Standard Time": "Asia/Kolkata",
    "SE Asia Standard Time": "Asia/Bangkok",
    "China Standard Time": "Asia/Shanghai",
    "Singapore Standard Time": "Asia/Singapore",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "E. Australia Standard Time": "Australia/Brisbane",
    "W. Australia Standard Time": "Australia/Perth",
    "New Zealand Standard Time": "Pacific/Auckland",
    "Eastern Standard Time": "America/New_York",
    "US Eastern Standard Time": "America/Indiana/Indianapolis",
    "Central Standard Time": "America/Chicago",
    "Central Standard Time (Mexico)": "America/Mexico_City",
    "Mountain Standard Time": "America/Denver",
    "US Mountain Standard Time": "America/Phoenix",
    "Pacific Standard Time": "America/Los_Angeles",
    "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "Atlantic Standard Time": "America/Halifax",
    "SA Pacific Standard Time": "America/Bogota",
    "SA Eastern Standard Time": "America/Cayenne",
    "Pacific SA Standard Time": "America/Santiago",
    "E. South America Standard Time": "America/Sao_Paulo",
    "Argentina Standard Time": "America/Argentina/Buenos_Aires",
    "Central America Standard Time": "America/Guatemala",
    "Canada Central Standard Time": "America/Regina",
    "South Africa Standard Time": "Africa/Johannesburg",
    "W. Central Africa Standard Time": "Africa/Lagos",
    "E. Africa Standard Time": "Africa/Nairobi",
    "Egypt Standard Time": "Africa/Cairo",
    "Morocco Standard Time": "Africa/Casablanca",
    "UTC": "UTC",
}

_ZONES = {}


def zone(name):
    """A Windows or IANA time zone name -> tzinfo, None when unknown (or
    the machine has no zone data – Windows without tzdata)."""
    name = (name or "").strip().strip('"')
    if not name:
        return None
    if name not in _ZONES:
        try:
            _ZONES[name] = ZoneInfo(WIN_TZ.get(name, name))
        except Exception:
            _ZONES[name] = None
    return _ZONES[name]


def iana(name):
    """The IANA name of a Windows or IANA zone name – None when unknown."""
    name = (name or "").strip().strip('"')
    if not name or zone(name) is None:
        return None
    return WIN_TZ.get(name, name)


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------
def parse_rrule(text):
    """The RRULE the export writes, as a dict – None for an empty text.
    BYDAY entries are (ordinal, weekday index), ordinal 0 for "every"."""
    if not (text or "").strip():
        return None
    rule = {"freq": "", "interval": 1, "byday": [], "bymonthday": [], "bymonth": [],
            "bysetpos": [], "wkst": 0, "until": None, "count": None}
    for part in text.strip().split(";"):
        key, _, value = part.partition("=")
        key, value = key.strip().upper(), value.strip()
        if key == "FREQ":
            rule["freq"] = value.upper()
        elif key == "INTERVAL":
            rule["interval"] = max(1, _int(value, 1))
        elif key == "BYDAY":
            for item in value.split(","):
                item = item.strip().upper()
                if item[-2:] in WEEKDAYS:
                    rule["byday"].append((_int(item[:-2], 0), WEEKDAYS.index(item[-2:])))
        elif key == "BYMONTHDAY":
            rule["bymonthday"] = [n for n in map(_int_or_zero, value.split(",")) if n]
        elif key == "BYMONTH":
            rule["bymonth"] = [n for n in map(_int_or_zero, value.split(",")) if 1 <= n <= 12]
        elif key == "BYSETPOS":
            rule["bysetpos"] = [n for n in map(_int_or_zero, value.split(",")) if n]
        elif key == "WKST" and value.upper() in WEEKDAYS:
            rule["wkst"] = WEEKDAYS.index(value.upper())
        elif key == "UNTIL":
            rule["until"] = value
        elif key == "COUNT":
            rule["count"] = _int(value, None)
    if rule["freq"] not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return None
    # The form earlier releases wrote for "the first weekday of the
    # month": BYDAY=1MO,1TU,1WE,1TH,1FR, the ordinal on every day. Read to
    # the letter that is five dates a month; what was meant is the set
    # with a position, BYDAY=MO,TU,WE,TH,FR;BYSETPOS=1 – the form written
    # since 14.3. A series that ended before the window is never written
    # again, so the old form stays in the archive and is read as meant.
    ordinals = {o for o, _ in rule["byday"]}
    if (rule["freq"] in ("MONTHLY", "YEARLY") and len(rule["byday"]) > 1
            and len(ordinals) == 1 and 0 not in ordinals and not rule["bysetpos"]):
        rule["bysetpos"] = [ordinals.pop()]
        rule["byday"] = [(0, wd) for _, wd in rule["byday"]]
    return rule


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _int_or_zero(value):
    return _int(value, 0)


def wall_key(when):
    """The key an occurrence is known by: its wall-clock start in the
    series' zone, "YYYYMMDDTHHMMSS" – a date alone for an all-day series."""
    if isinstance(when, datetime):
        return when.strftime("%Y%m%dT%H%M%S")
    return when.strftime("%Y%m%d")


def parse_wall(value, tz=None):
    """An .ics date-time value -> the datetime the series counts with: a
    "Z" value is an instant and comes back in `tz` (or UTC), a local value
    is taken as wall time in `tz`; a bare date stays a date."""
    v = (value or "").strip()
    if len(v) == 8 and v.isdigit():
        return date(int(v[:4]), int(v[4:6]), int(v[6:8]))
    try:
        naive = datetime.strptime(v.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    if v.endswith("Z"):
        return naive.replace(tzinfo=UTC).astimezone(tz or UTC)
    return naive.replace(tzinfo=tz or UTC)


def key_of(value, tzid, tz):
    """The wall key of an .ics value (an EXDATE, a RECURRENCE-ID) with the
    TZID it carries – read in the series' zone `tz` when it carries none.
    None when the value cannot be read."""
    when = parse_wall(value, zone(tzid) or tz) if value else None
    return wall_key(when) if when is not None else None


# ---------------------------------------------------------------------------
# The dates
# ---------------------------------------------------------------------------
def expand(start, rule, exdates=(), horizon_end=None, cap=CAP, horizon_start=None):
    """The occurrences of a series, in order: datetimes with the zone of
    `start` (a date for an all-day series), at the same wall-clock time.

    `exdates` are wall keys taken out of the set – after COUNT is applied,
    as iCalendar counts (a date deleted from a numbered series does not
    add one at the end). `horizon_end` stops the series there, and dates
    before `horizon_start` are left out: the past nobody looks at costs
    nothing. `cap` keeps the newest dates up to the horizon – a daily
    series since 2015 keeps this year's dates, not its first – and the
    first ones when there is no horizon. A rule the export never writes
    yields `start` alone.
    """
    if rule is None or not rule.get("freq"):
        return [start]
    all_day = not isinstance(start, datetime)
    first = start if all_day else start.date()
    until = _until(rule.get("until"), all_day)
    count = rule.get("count")
    out = []
    exdates = set(exdates or ())
    for made, day in enumerate(_days(first, rule), 1):
        if count and made > count:
            break
        when = day if all_day else _at(day, start)
        if until is not None and _after(when, until, all_day):
            break
        if horizon_end is not None and _after(when, horizon_end, all_day):
            break
        if horizon_start is not None and _after(horizon_start, when, all_day):
            continue
        if wall_key(when) not in exdates:
            out.append(when)
            if horizon_end is None and len(out) >= cap:
                break
    return out[-cap:] if cap and len(out) > cap else out


def bound(start, limit):
    """A horizon as the series counts: a date for an all-day series."""
    if isinstance(start, datetime) or not isinstance(limit, datetime):
        return limit
    return limit.date()


def _at(day, start):
    """`start`'s wall time on another day, in its zone. A wall time the
    clock change skips is placed where the clock lands (02:30 -> 03:30
    on the night the clocks go forward), as Exchange places that date."""
    when = datetime(day.year, day.month, day.day, start.hour, start.minute, start.second,
                    tzinfo=start.tzinfo)
    if start.tzinfo is not None:
        when = when.astimezone(UTC).astimezone(start.tzinfo)
    return when


def _after(when, limit, all_day):
    if all_day:
        return when > limit
    return when.astimezone(UTC) > (limit if limit.tzinfo else limit.replace(tzinfo=UTC))


def _until(value, all_day):
    """UNTIL as the .ics says it: a date for an all-day series, else an
    instant in UTC."""
    if not value:
        return None
    parsed = parse_wall(value)
    if parsed is None:
        return None
    if all_day:
        return parsed if isinstance(parsed, date) and not isinstance(parsed, datetime) else parsed.date()
    if not isinstance(parsed, datetime):
        return datetime(parsed.year, parsed.month, parsed.day, 23, 59, 59, tzinfo=UTC)
    return parsed


def _days(first, rule):
    """The candidate days of the rule from `first` on, in order, a century
    of periods at most – the stopping conditions sit in expand()."""
    freq, interval = rule["freq"], rule["interval"]
    periods = _PERIODS[freq]
    if freq == "DAILY":
        for k in range(periods):
            yield first + timedelta(days=k * interval)
    elif freq == "WEEKLY":
        wkst = rule["wkst"]
        days = sorted({d for _o, d in rule["byday"]}) or [first.weekday()]
        week_start = first - timedelta(days=(first.weekday() - wkst) % 7)
        for k in range(periods):
            base = week_start + timedelta(weeks=k * interval)
            for d in sorted(days, key=lambda d: (d - wkst) % 7):
                day = base + timedelta(days=(d - wkst) % 7)
                if day >= first:
                    yield day
    elif freq == "MONTHLY":
        for k in range(periods):
            m = first.month - 1 + k * interval
            year, month = first.year + m // 12, m % 12 + 1
            for day in _in_month(year, month, rule, first.day):
                if day >= first:
                    yield day
    elif freq == "YEARLY":
        months = rule["bymonth"] or [first.month]
        for k in range(periods):
            year = first.year + k * interval
            for month in sorted(months):
                for day in _in_month(year, month, rule, first.day):
                    if day >= first:
                        yield day


def _in_month(year, month, rule, default_day):
    """The rule's days in one month, in order: BYMONTHDAY, else BYDAY with
    its ordinals (BYSETPOS picks from the whole set), else the start's day
    of month. A day the month does not have falls on its last day – as
    Exchange places it (a report on the 31st happens on 30 April), where
    iCalendar would skip the month; the view counts as the calendar does."""
    last = monthrange(year, month)[1]
    if rule["bymonthday"]:
        days = sorted({min(d, last) if d > 0 else last + 1 + d for d in rule["bymonthday"]})
        return [date(year, month, d) for d in days if 1 <= d <= last]
    if rule["byday"]:
        picked = []
        for ordinal, wd in rule["byday"]:
            matching = [date(year, month, d) for d in range(1, last + 1)
                        if date(year, month, d).weekday() == wd]
            if ordinal == 0:
                picked += matching
            elif -len(matching) <= ordinal <= len(matching):
                picked.append(matching[ordinal - 1] if ordinal > 0 else matching[ordinal])
        picked = sorted(set(picked))
        if rule["bysetpos"]:
            chosen = []
            for pos in rule["bysetpos"]:
                if 1 <= pos <= len(picked):
                    chosen.append(picked[pos - 1])
                elif -len(picked) <= pos <= -1:
                    chosen.append(picked[pos])
            picked = sorted(set(chosen))
        return picked
    return [date(year, month, min(default_day, last))]


# ---------------------------------------------------------------------------
# A series as its .ics says it
# ---------------------------------------------------------------------------
def horizon(now=None):
    """Where a series without an end stops: YEARS_AHEAD years from `now`,
    at the start of that day – so two builds on one day unfold the same
    dates and write the same calendar file."""
    ahead = (now or datetime.now(UTC)) + timedelta(days=365 * YEARS_AHEAD)
    return ahead.replace(hour=0, minute=0, second=0, microsecond=0)


def months_back(now, months):
    """Where the calendar's window begins: the start of the day this many
    months before `now`, the day clamped to the month (the 31st of a month
    with 30 days) – None for 0, which means everything. The export reads
    the calendar from there; the view and the index unfold a series from
    there."""
    if not months:
        return None
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    m = today.month - 1 - int(months)
    year, month = today.year + m // 12, m % 12 + 1
    return today.replace(year=year, month=month, day=min(today.day, monthrange(year, month)[1]))


def series_zone(dtstart, tzid, all_day=False):
    """The zone a series' .ics counts on: its DTSTART's TZID, UTC for a
    "Z" value, none for an all-day series."""
    if all_day:
        return None
    if (dtstart or "").endswith("Z"):
        return UTC
    return zone(tzid) or UTC


def series_setup(dtstart, tzid, all_day, rrule_text, exdates):
    """What unfolding a series needs, read off its master VEVENT: the start
    (a datetime in the series' zone, a date for an all-day series), the
    rule, the EXDATEs as wall keys – `exdates` are (value, tzid) pairs –
    and the zone. None when the start cannot be read."""
    tz = series_zone(dtstart, tzid, all_day)
    start = parse_wall(dtstart, tz)
    if start is None:
        return None
    keys = {k for k in (key_of(value, z, tz) for value, z in exdates or ()) if k}
    return start, parse_rrule(rrule_text), keys, tz


def stamp(when):
    """A date the series counts with as a timestamp – an all-day date at
    local midnight, the way corpus._ics_when dates a file."""
    if isinstance(when, datetime):
        return when.timestamp()
    return datetime(when.year, when.month, when.day).timestamp()


def timestamps(start, rule, exdates, horizon_end, horizon_start=None):
    """The dates of a series as timestamps (stamp)."""
    since = bound(start, horizon_start) if horizon_start is not None else None
    for when in expand(start, rule, exdates=exdates, horizon_end=bound(start, horizon_end),
                       horizon_start=since):
        yield stamp(when)
