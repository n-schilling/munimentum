"""The calendar view of a series: its dates on the series' own clock, the
exceptions in place of the dates they stand for, the dates taken out, what
is gone at Microsoft – and the index, the search and the detail reading
the same file."""

import json
import sqlite3
from datetime import UTC, datetime

import combined_search
import corpus
import detail
import outlook_export
import state_db

MASTER = {
    "id": "s1", "iCalUId": "uid-s1", "subject": "Jour fixe", "isAllDay": False,
    "originalStartTimeZone": "W. Europe Standard Time",
    "start": {"dateTime": "2025-03-24T12:00:00.0000000", "timeZone": "UTC"},   # 13:00 Berlin, winter time
    "end": {"dateTime": "2025-03-24T13:00:00.0000000", "timeZone": "UTC"},
    "lastModifiedDateTime": "2025-03-01T08:00:00Z",
    "organizer": {"emailAddress": {"name": "Alice Beispiel", "address": "alice@example.com"}},
    "attendees": [{"emailAddress": {"name": "Bob Baumeister", "address": "bob@example.com"}}],
    "recurrence": {"pattern": {"type": "weekly", "daysOfWeek": ["monday"]},
                   "range": {"type": "numbered", "numberOfOccurrences": 5}},
}
SERIES = {"exdates": ["20250407T130000"],
          "exceptions": {"20250331T130000": {"subject": "Jour fixe (moved)", "isAllDay": False,
                                             "start": {"dateTime": "2025-03-31T14:00:00.0000000"},
                                             "end": {"dateTime": "2025-03-31T15:00:00.0000000"},
                                             "location": "Room 7", "description": "Moved for the audit",
                                             "organizer": {"name": "Alice Beispiel", "address": "alice@example.com"},
                                             "attendees": [{"name": "Bob Baumeister", "address": "bob@example.com"}],
                                             "isCancelled": False, "showAs": "busy",
                                             "lastModifiedDateTime": "2025-03-20T08:00:00Z"},
                         "20250414T130000": {"subject": "Jour fixe", "isAllDay": False,
                                             "start": {"dateTime": "2025-04-14T11:00:00.0000000"},
                                             "end": {"dateTime": "2025-04-14T12:00:00.0000000"},
                                             "location": "", "isCancelled": True, "showAs": "busy",
                                             "lastModifiedDateTime": "2025-03-20T08:00:00Z"}}}
SINGLE = {"id": "e1", "iCalUId": "uid-e1", "subject": "Kickoff", "isAllDay": False,
          "start": {"dateTime": "2025-03-25T09:00:00.0000000", "timeZone": "UTC"},
          "end": {"dateTime": "2025-03-25T10:00:00.0000000", "timeZone": "UTC"},
          "lastModifiedDateTime": "2025-03-01T08:00:00Z"}
LATER = {"id": "e2", "iCalUId": "uid-e2", "subject": "Review", "isAllDay": False,
         "start": {"dateTime": "2025-04-15T09:00:00.0000000", "timeZone": "UTC"},
         "end": {"dateTime": "2025-04-15T10:00:00.0000000", "timeZone": "UTC"},
         "lastModifiedDateTime": "2025-03-01T08:00:00Z"}
GONE_SINCE = "2025-03-19T10:00:00+00:00"
NOW = datetime(2025, 3, 20, tzinfo=UTC)


def _archive(tmp_path):
    cal = tmp_path / "kalender" / "Arbeit"
    cal.mkdir(parents=True)
    series = cal / outlook_export.event_filename(MASTER)
    series.write_text(outlook_export.build_ics(MASTER, SERIES), encoding="utf-8")
    single = cal / outlook_export.event_filename(SINGLE)
    single.write_text(outlook_export.build_ics(SINGLE), encoding="utf-8")
    later = cal / outlook_export.event_filename(LATER)
    later.write_text(outlook_export.build_ics(LATER), encoding="utf-8")
    state_db.StateDb(tmp_path).verschwunden_ergaenzen([single.relative_to(tmp_path).as_posix()], GONE_SINCE)
    return series, single, later


def _utc(*parts):
    return datetime(*parts, tzinfo=UTC).timestamp()


def _local(ts, all_day=False):
    """A timestamp the way a hit's date says it: on the machine's clock –
    an expectation never names a fixed day for it."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d" if all_day else "%Y-%m-%d %H:%M")


def test_a_series_unfolds_on_its_own_clock_with_its_exceptions(tmp_path):
    _archive(tmp_path)
    recs = combined_search.read_calendar(tmp_path, tmp_path, now=NOW)
    dates = [(r["title"], datetime.fromtimestamp(r["ts"], UTC).strftime("%m-%d %H:%MZ"), r["st"]) for r in recs if r["uid"] == "uid-s1"]
    assert dates == [
        ("Jour fixe", "03-24 12:00Z", "confirmed"),             # 13:00 CET
        ("Jour fixe (moved)", "03-31 14:00Z", "confirmed"),     # the exception: 16:00 CEST, its own time
        ("Jour fixe", "04-14 11:00Z", "cancelled"),             # 13:00 CEST – the clock changed, the hour did not
        ("Jour fixe", "04-21 11:00Z", "confirmed"),
    ]                                                           # 04-07 is an EXDATE: the count stays five
    moved = next(r for r in recs if r["title"] == "Jour fixe (moved)")
    assert moved["loc"] == "Room 7" and moved["te"] - moved["ts"] == 3600 and moved["att"] == ["Bob Baumeister"]
    assert moved["rts"] == _utc(2025, 3, 31, 11, 0)             # the slot it stands for: 13:00 CEST
    plain = next(r for r in recs if r["uid"] == "uid-s1" and r["ts"] == _utc(2025, 4, 21, 11, 0))
    assert plain["te"] - plain["ts"] == 3600 and plain["who"] == "Alice Beispiel"
    # The single event the bookkeeping says is gone keeps its record, marked.
    (gone,) = [r for r in recs if r["uid"] == "uid-e1"]
    assert gone["st"] == "removed"
    # The export's own JSON drops the matching keys.
    data = combined_search.collect_calendar_data(tmp_path, reconstruct=False, now=NOW)
    assert all("uid" not in r and "rts" not in r for r in data["recs"])
    assert data["counts"]["kalender"] == 6 and data["horizon"] > _utc(2027, 3, 1)


def test_the_window_start_leaves_a_series_past_out(tmp_path):
    """The view unfolds a series from where the export's window begins –
    the single files beside it stay as they lie. A date moved into the
    window from a slot before it is where it happens, in the view and in
    the index's dates alike."""
    import rag_index
    _archive(tmp_path)
    recs = combined_search.read_calendar(tmp_path, tmp_path, now=NOW, since=datetime(2025, 4, 10, tzinfo=UTC))
    assert [datetime.fromtimestamp(r["ts"], UTC).strftime("%m-%d") for r in recs if r["uid"] == "uid-s1"] == ["04-14", "04-21"]
    assert {r["title"] for r in recs if r["uid"] != "uid-s1"} == {"Kickoff", "Review"}
    # The slot 03-31 13:00 CET (11:00Z) lies before this start, the moved date (14:00Z) inside it.
    since = datetime(2025, 3, 31, 12, 0, tzinfo=UTC)
    recs = combined_search.read_calendar(tmp_path, tmp_path, now=NOW, since=since)
    moved = next(r for r in recs if r["title"] == "Jour fixe (moved)")
    assert moved["ts"] == _utc(2025, 3, 31, 14, 0) and moved["rts"] == _utc(2025, 3, 31, 11, 0)
    chunks = corpus.chunk_records(corpus.load_calendar(tmp_path))
    dates = sorted(ts for _uid, ts in rag_index._series_rows(chunks, NOW, since))
    assert dates == [_utc(2025, 3, 31, 14, 0), _utc(2025, 4, 14, 11, 0), _utc(2025, 4, 21, 11, 0)]


def test_a_moved_window_is_no_change(tmp_path):
    """The calendar file is built again with every run: a series without
    an end gets a new date with every day the horizon moves and loses one
    with every day the window's start moves, and neither is something
    that changed at Microsoft. The file stays JSON throughout."""
    cal = tmp_path / "kalender" / "Arbeit"
    cal.mkdir(parents=True)
    endless = {**MASTER, "recurrence": {"pattern": {"type": "daily"}, "range": {"type": "noEnd"}}}
    (cal / "daily.ics").write_text(outlook_export.build_ics(endless), encoding="utf-8")
    target = tmp_path / "calendar.json"
    day = datetime(2025, 6, 20, tzinfo=UTC)
    first = combined_search.write_calendar_json(tmp_path, target, reconstruct=False, now=day,
                                               since=datetime(2025, 5, 20, tzinfo=UTC))
    again = combined_search.write_calendar_json(tmp_path, target, reconstruct=False,
                                               now=datetime(2025, 6, 21, tzinfo=UTC),
                                               since=datetime(2025, 5, 21, tzinfo=UTC))
    assert first["changed"] > 0 and (again["changed"], again["removed"]) == (0, 0)
    json.dumps(combined_search.collect_calendar_data(tmp_path, reconstruct=False, now=day))
    assert "sr" not in json.loads(target.read_text(encoding="utf-8"))["recs"][0]
    # A single appointment beyond the horizon is new all the same – the
    # export keeps ten years ahead, the horizon is the series' alone.
    far = {**SINGLE, "id": "e9", "iCalUId": "uid-e9",
           "start": {"dateTime": "2028-06-01T09:00:00.0000000", "timeZone": "UTC"},
           "end": {"dateTime": "2028-06-01T10:00:00.0000000", "timeZone": "UTC"}}
    (cal / "far.ics").write_text(outlook_export.build_ics(far), encoding="utf-8")
    third = combined_search.write_calendar_json(tmp_path, target, reconstruct=False,
                                                now=datetime(2025, 3, 24, tzinfo=UTC))
    assert third["changed"] == 1


def test_a_cancellation_mail_for_one_date_marks_that_date(tmp_path):
    _archive(tmp_path)
    recs = combined_search.read_calendar(tmp_path, tmp_path, now=NOW)
    ev = combined_search._new_event()
    ev.update(uid="uid-s1", recid="20250421T130000", recid_tz="W. Europe Standard Time",
              dtstart="20250421T110000Z", summary="Abgesagt: Jour fixe", status="cancelled")
    invite = {"method": "CANCEL", "ev": ev, "mts": 1.0, "md": "2025-04-01", "href": "x.eml", "org_hint": ("", "")}
    ghosts, marked, dupes = combined_search.reconstruct_events([invite], recs)
    assert ghosts == [] and marked == 1
    assert [r["st"] for r in recs if r["uid"] == "uid-s1"][-1] == "cancelled"


def test_a_cancellation_mail_names_the_slot_a_moved_date_stands_for(tmp_path):
    """Outlook cancels the date by its original slot (13:00), not by the
    time it was moved to (16:00): the moved record takes the mark, no
    ghost appears beside it."""
    _archive(tmp_path)
    recs = combined_search.read_calendar(tmp_path, tmp_path, now=NOW)
    ev = combined_search._new_event()
    ev.update(uid="uid-s1", recid="20250331T130000", recid_tz="W. Europe Standard Time",
              dtstart="20250331T110000Z", summary="Abgesagt: Jour fixe", status="cancelled")
    invite = {"method": "CANCEL", "ev": ev, "mts": 1.0, "md": "2025-03-30", "href": "x.eml", "org_hint": ("", "")}
    ghosts, marked, dupes = combined_search.reconstruct_events([invite], recs)
    assert (ghosts, marked, dupes) == ([], 1, 0)
    assert next(r["st"] for r in recs if r["title"] == "Jour fixe (moved)") == "cancelled"


def test_the_index_and_the_detail_read_the_series_by_its_master(tmp_path):
    """One item per series file, dated and named by the master; the
    exceptions' own words are in its text, so a search for the room a
    date moved to finds the series. The detail shows the master's facts,
    not the last exception's. A tombstoned appointment is `gone` in the
    index like a mail."""
    series, single, later = _archive(tmp_path)
    items = {r["rel"]: r for r in corpus.load_calendar(tmp_path)}
    rec = items[series.relative_to(tmp_path).as_posix()]
    assert rec["title"] == "Jour fixe"
    assert rec["date"] == corpus._ics_when("20250324T130000", False, "Europe/Berlin")[1]
    assert "Room 7" in rec["text"] and "Jour fixe (moved)" in rec["text"] and "Moved for the audit" in rec["text"]
    assert "gone" not in rec and items[single.relative_to(tmp_path).as_posix()]["gone"] == GONE_SINCE
    row = {"date": rec["date"], "who": rec["who"], "who_mail": rec["who_mail"], "ctx": rec["ctx"]}
    facts = detail._termin(row, rec["text"], series)
    assert facts["start"] == rec["date"] and facts["location"] == ""
    assert facts["end"] == corpus._ics_when("20250324T140000", False, "Europe/Berlin")[1]
    assert [a["name"] for a in facts["attendees"]] == ["Bob Baumeister"]


def test_a_window_finds_a_series_by_any_of_its_dates(tmp_path):
    """The index dates a series by its first date; a search for a week in
    which only a later date of it falls finds it all the same – the hit
    says that date, the list sorts by it and the people view counts by
    it, as the calendar the search was handed over from shows it. A
    week with the date taken out finds nothing."""
    import mcp_server
    import rag_index
    _archive(tmp_path)
    store = tmp_path / "store"
    store.mkdir()
    chunks = corpus.chunk_records(corpus.load_calendar(tmp_path))
    for c in chunks:
        c["hash"] = corpus.chunk_hash(c)
    rag_index.write_db(store, chunks, now=NOW)
    rag_index.write_info(store, None, 0, len(chunks))
    with sqlite3.connect(store / "corpus.db") as con:
        # Five dates less the one taken out, every one before the horizon –
        # and the planner's statistics, without which the window scans.
        assert con.execute("SELECT COUNT(*) FROM series_dates").fetchone()[0] == 4
        assert con.execute("SELECT COUNT(*) FROM sqlite_stat1").fetchone()[0] > 0
    kept = dict(mcp_server.STATE)
    mcp_server.STATE.clear()
    mcp_server.STATE.update(db=str(store / "corpus.db"), semantic=False, outlook_dir=str(tmp_path))
    try:
        def found(date_from, date_to, **kw):
            res = mcp_server.browse_messages(date_from=date_from, date_to=date_to, source="kalender", **kw)
            assert "error" not in res, res
            return [(h["title"], h["date"]) for h in res["results"]]
        series_in_window = _local(_utc(2025, 4, 14, 11, 0))
        assert found("2025-04-14", "2025-04-20") == [("Review", _local(_utc(2025, 4, 15, 9, 0))),
                                                     ("Jour fixe", series_in_window)]
        assert found("2025-04-07", "2025-04-13") == []                   # the EXDATE
        assert found("2025-04-28", "2025-05-04") == []                   # after the fifth date
        assert found("2025-03-24", "2025-03-30") == [("Kickoff", _local(_utc(2025, 3, 25, 9, 0))),
                                                     ("Jour fixe", _local(_utc(2025, 3, 24, 12, 0)))]
        hits = mcp_server.search_messages("Jour", date_from="2025-04-14", date_to="2025-04-20",
                                          source="kalender", mode="lexical")["results"]
        assert [h["date"] for h in hits] == [series_in_window]
        rows = mcp_server.facet_rows(date_from="2025-04-14", date_to="2025-04-20", source="kalender")["rows"]
        assert sorted(r[2] for r in rows) == sorted([series_in_window, _local(_utc(2025, 4, 15, 9, 0))])
        # The room a date moved to finds the series; the gone single is listed as gone.
        assert [h["title"] for h in mcp_server.search_messages("Room 7", source="kalender", mode="lexical")["results"]] == ["Jour fixe"]
        gone = mcp_server.browse_messages(source="kalender", only_gone=True)["results"]
        assert [(h["title"], h["gone"]) for h in gone] == [("Kickoff", GONE_SINCE)]
    finally:
        mcp_server.STATE.clear()
        mcp_server.STATE.update(kept)


def test_an_exception_without_a_time_of_its_own_sits_at_its_slot(tmp_path):
    """An exception VEVENT that names no DTSTART (Graph sent no start):
    the date keeps the slot's time on the series' clock and takes what the
    exception says – its name, its status."""
    cal = tmp_path / "kalender" / "Arbeit"
    cal.mkdir(parents=True)
    bare = {"exdates": [], "exceptions": {"20250331T130000": {"subject": "Jour fixe (renamed)", "isAllDay": False,
                                                              "start": None, "end": None, "location": "",
                                                              "isCancelled": False, "showAs": "busy",
                                                              "lastModifiedDateTime": "2025-03-20T08:00:00Z"}}}
    (cal / "s.ics").write_text(outlook_export.build_ics(MASTER, bare), encoding="utf-8")
    recs = combined_search.read_calendar(tmp_path, tmp_path, now=NOW)
    renamed = next(r for r in recs if r["title"] == "Jour fixe (renamed)")
    assert renamed["ts"] == _utc(2025, 3, 31, 11, 0) and renamed["te"] - renamed["ts"] == 3600
    assert renamed["rts"] == renamed["ts"] and renamed["st"] == "confirmed"
    assert len(recs) == 5


def test_an_all_day_series_counts_in_dates(tmp_path):
    master = {**MASTER, "isAllDay": True, "originalStartTimeZone": "UTC",
              "start": {"dateTime": "2025-03-05T00:00:00.0000000", "timeZone": "UTC"},
              "end": {"dateTime": "2025-03-06T00:00:00.0000000", "timeZone": "UTC"},
              "recurrence": {"pattern": {"type": "absoluteMonthly", "dayOfMonth": 5},
                             "range": {"type": "numbered", "numberOfOccurrences": 3}}}
    cal = tmp_path / "kalender" / "Arbeit"
    cal.mkdir(parents=True)
    (cal / "retro.ics").write_text(outlook_export.build_ics(master, {"exdates": ["20250405"], "exceptions": {}}),
                                   encoding="utf-8")
    text = (cal / "retro.ics").read_text(encoding="utf-8")
    assert "DTSTART;VALUE=DATE:20250305" in text and "EXDATE;VALUE=DATE:20250405" in text
    recs = combined_search.read_calendar(tmp_path, tmp_path, now=NOW)
    assert [(r["d"], r["ad"]) for r in recs] == [("2025-03-05", 1), ("2025-05-05", 1)]
    assert json.dumps(recs[0]["te"] - recs[0]["ts"]) == "86400.0"
