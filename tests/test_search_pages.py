"""Tests for the calendar/contact evaluation combined_search.py.

Covered are the iCalendar/vCard parsers, the reconstruction of deleted
events from invitation and cancellation mails, and the JSON output for
app.py – all from synthetic export trees in tmp_path. No network access.
"""

import json
import re
import sys

import pytest

import combined_search
import progress


# --------------------------------------------------------------------------
# Shared helpers and fixtures
# --------------------------------------------------------------------------
def make_eml(body="Hallo Bob, hier der Inhalt.", subject="Testmail",
             frm="Alice Example <alice@example.com>",
             to="Bob Builder <bob@example.com>", cc=None,
             date="Mon, 07 Jul 2025 10:00:00 +0000",
             ctype="text/plain; charset=utf-8"):
    """Builds a minimal .eml as bytes."""
    lines = [f"From: {frm}", f"To: {to}"]
    if cc:
        lines.append(f"Cc: {cc}")
    lines += [f"Subject: {subject}", f"Date: {date}", f"Content-Type: {ctype}", "", body]
    return "\r\n".join(lines).encode("utf-8")


ICS = "\r\n".join([
    "BEGIN:VCALENDAR",
    "BEGIN:VEVENT",
    "SUMMARY:Planung\\, Quartal",
    "LOCATION:Raum 42",
    "DESCRIPTION:Agenda folgt",
    "DTSTART:20250601T120000Z",
    'ORGANIZER;CN="Alice Example":mailto:alice@example.com',
    'ATTENDEE;CN="Bob Builder":mailto:bob@example.com',
    "END:VEVENT",
    "END:VCALENDAR",
])

def make_ical_eml(ical, subject="Abgesagt: Planung", frm="Alice Example <alice@example.com>",
                  date="Tue, 08 Jul 2025 08:00:00 +0000", method="CANCEL"):
    """Builds an .eml with a text/calendar part (base64, as Exchange sends them)."""
    import base64
    b64 = base64.b64encode(ical.encode("utf-8")).decode("ascii")
    return "\r\n".join([
        "From: " + frm, "To: Bob Builder <bob@example.com>",
        "Subject: " + subject, "Date: " + date,
        'Content-Type: multipart/alternative; boundary="BND"', "",
        "--BND", "Content-Type: text/plain; charset=utf-8", "",
        "Der Termin wurde abgesagt.", "",
        "--BND", f'Content-Type: text/calendar; charset=utf-8; method={method}',
        "Content-Transfer-Encoding: base64", "", b64, "", "--BND--", "",
    ]).encode("utf-8")


# Exchange invitation: VTIMEZONE with its own DTSTART before the VEVENT
MAIL_ICS = "\r\n".join([
    "BEGIN:VCALENDAR",
    "METHOD:CANCEL",
    "VERSION:2.0",
    "BEGIN:VTIMEZONE",
    "TZID:W. Europe Standard Time",
    "BEGIN:STANDARD",
    "DTSTART:16010101T030000",          # trap: must not be read as an event
    "TZOFFSETFROM:+0200",
    "END:STANDARD",
    "END:VTIMEZONE",
    "BEGIN:VEVENT",
    "UID:GELOESCHT-1",
    "SUMMARY;LANGUAGE=de-DE:Abgesagt: Jour Fixe",
    "DTSTART;TZID=W. Europe Standard Time:20250610T150000",
    "DTEND;TZID=W. Europe Standard Time:20250610T160000",
    "LOCATION:Raum 7",
    "STATUS:CANCELLED",
    "SEQUENCE:2",
    'ORGANIZER;CN="Alice Example":mailto:alice@example.com',
    "END:VEVENT",
    "END:VCALENDAR",
])

VCF = "\r\n".join([
    "BEGIN:VCARD",
    "FN:Alice Example",
    "N:Example;Alice;;;",
    "ORG:Firma GmbH;Entwicklung",
    "TITLE:Engineer",
    "EMAIL:alice@example.com",
    "TEL:+49 123 456",
    "NOTE:Erste Zeile",
    " weiter gefaltet",
    "END:VCARD",
])


# --------------------------------------------------------------------------
# combined_search: helpers
# --------------------------------------------------------------------------
def test_combined_link_kodiert_segmente(tmp_path):
    p = tmp_path / "export" / "Ordner mit Leerzeichen" / "datei ä.html"
    href = combined_search.link(p, tmp_path)
    assert href == "export/Ordner%20mit%20Leerzeichen/datei%20%C3%A4.html"


# --------------------------------------------------------------------------
# combined_search: readers (calendar, contacts, invitation mails)
# --------------------------------------------------------------------------
def test_combined_read_calendar(tmp_path):
    root = tmp_path / "outlook_export"
    d = root / "kalender" / "Arbeit"
    d.mkdir(parents=True)
    (d / "termin.ics").write_text(ICS, encoding="utf-8")

    recs = combined_search.read_calendar(root.resolve(), tmp_path.resolve())

    assert len(recs) == 1
    r = recs[0]
    assert r["src"] == "kalender"
    assert r["title"] == "Planung, Quartal"      # \\, unescaped
    assert r["ctx"] == "Kalender: Arbeit"
    assert r["who"] == "Alice Example"
    assert r["x"].startswith("Ort: Raum 42.")
    assert "bob@example.com" in r["ppl"]
    assert r["ts"] is not None
    # Extra fields for the calendar view (defaults without DTEND/STATUS)
    assert r["te"] is None and r["ad"] == 0 and r["st"] == "confirmed"
    assert r["cal"] == "Arbeit" and r["loc"] == "Raum 42"
    assert r["att"] == ["Bob Builder"]


def test_combined_read_calendar_ganztaegig_und_status(tmp_path):
    d = tmp_path / "outlook_export" / "kalender" / "Arbeit"
    d.mkdir(parents=True)
    (d / "urlaub.ics").write_text("\r\n".join([
        "BEGIN:VCALENDAR", "BEGIN:VEVENT",
        "SUMMARY:Urlaub",
        "DTSTART;VALUE=DATE:20250601",
        "DTEND;VALUE=DATE:20250604",
        "STATUS:CANCELLED",
        "END:VEVENT", "END:VCALENDAR",
    ]), encoding="utf-8")
    (d / "workshop.ics").write_text("\r\n".join([
        "BEGIN:VCALENDAR", "BEGIN:VEVENT",
        "SUMMARY:Workshop",
        "DTSTART:20250601T080000Z",
        "DTEND:20250601T093000Z",
        "STATUS:TENTATIVE",
        "END:VEVENT", "END:VCALENDAR",
    ]), encoding="utf-8")

    recs = {r["title"]: r
            for r in combined_search.read_calendar(
                (tmp_path / "outlook_export").resolve(), tmp_path.resolve())}

    u = recs["Urlaub"]
    assert u["ad"] == 1 and u["st"] == "cancelled"
    assert u["te"] - u["ts"] == 3 * 86400        # DTEND exclusive, 3 days
    w = recs["Workshop"]
    assert w["ad"] == 0 and w["st"] == "tentative"
    assert w["te"] - w["ts"] == 90 * 60


def test_combined_parse_vevents_ignoriert_vtimezone():
    method, evs = combined_search.parse_vevents(MAIL_ICS)
    assert method == "CANCEL" and len(evs) == 1
    ev = evs[0]
    # The event's DTSTART, not the time zone rule's (16010101T030000)
    assert ev["dtstart"] == "20250610T150000" and ev["uid"] == "GELOESCHT-1"
    assert ev["status"] == "cancelled" and ev["seq"] == 2
    assert ev["org_cn"] == "Alice Example" and ev["location"] == "Raum 7"


def _invite(uid, *, method="CANCEL", summary="Abgesagt: Jour Fixe",
            dtstart="20250610T150000", recid="", seq=1, mts=100.0, status="cancelled"):
    ev = {"uid": uid, "recid": recid, "summary": summary, "location": "",
          "description": "", "dtstart": dtstart, "dateonly": False,
          "dtend": "", "enddateonly": False, "status": status, "seq": seq,
          "org_cn": "Alice Example", "org_mail": "alice@example.com",
          "att_names": [], "att_mails": []}
    return {"method": method, "ev": ev, "href": "mail.eml", "mts": mts, "md": "2025-07-08 10:00"}


def test_combined_norm_uid_packt_exchange_id_aus():
    # Exchange global object ID with embedded Google UID (as in the real export)
    blob = ("040000008200E00074C5B7101A82E00800000000000000000000000000000000000000003200"
            "00007643616C2D55696401000000326E6934386C71716B6B326E313772687567643335383"
            "46A6F7140676F6F676C652E636F6D00")
    assert combined_search.norm_uid(blob) == "2ni48lqqkk2n17rhugd3584joq@google.com"
    # native UIDs stay unchanged (except for case)
    assert combined_search.norm_uid("ABC@example.com") == "abc@example.com"
    assert combined_search.norm_uid("") == ""
    assert combined_search.norm_uid("040000008200E0FF") == "040000008200e0ff"  # no vCal-Uid
    assert combined_search.norm_uid("ZZZZ" * 30) == ("zzzz" * 30)              # not hex


def test_combined_reconstruct_erkennt_ausgepackte_uid():
    blob = ("040000008200E00074C5B7101A82E00800000000000000000000000000000000000000003200"
            "00007643616C2D55696401000000326E6934386C71716B6B326E313772687567643335383"
            "46A6F7140676F6F676C652E636F6D00")
    im_kalender = {"uid": blob, "st": "confirmed", "title": "Review", "ts": None}
    ghosts, marked, dupes = combined_search.reconstruct_events(
        [_invite("2ni48lqqkk2n17rhugd3584joq@google.com", summary="Review")], [im_kalender])
    # same event -> no ghost copy, the status is healed instead
    assert ghosts == [] and marked == 1 and dupes == 0


def test_combined_reconstruct_verwirft_doppel_bei_gleichem_titel_und_start():
    ts = combined_search._ics_when("20250610T150000", False)[0]
    im_kalender = {"uid": "ANDERE-ID", "st": "confirmed", "title": "Jour Fixe", "ts": ts}
    ghosts, marked, dupes = combined_search.reconstruct_events(
        [_invite("UNBEKANNTES-FORMAT")], [im_kalender])
    assert ghosts == [] and marked == 0 and dupes == 1


def test_combined_reconstruct_geloeschte_termine():
    # present in the calendar -> no reconstruction, but the status is healed
    im_kalender = {"uid": "DA-1", "st": "confirmed"}
    ghosts, marked, _ = combined_search.reconstruct_events(
        [_invite("DA-1"), _invite("WEG-1"),
         _invite("NIE-1", method="REPLY", summary="Angenommen: Workshop",
                 status="", dtstart="20250612T090000")],
        [im_kalender])

    assert marked == 1 and im_kalender["st"] == "cancelled"
    by_title = {r["title"]: r for r in ghosts}
    assert set(by_title) == {"Jour Fixe", "Workshop"}       # status prefixes removed
    weg = by_title["Jour Fixe"]
    assert weg["st"] == "deleted" and weg["src"] == "kalender"
    assert weg["p"] == "mail.eml" and "rekonstruiert aus Mail" in weg["ctx"]
    assert weg["ts"] is not None
    # only invited/accepted, never (any more) in the calendar: no proof of deletion
    assert by_title["Workshop"]["st"] == "gone"


def test_combined_reconstruct_ohne_startzeit_und_serieninstanz():
    # without DTSTART not placeable -> discarded
    ghosts, _, _ = combined_search.reconstruct_events([_invite("X", dtstart="")], [])
    assert ghosts == []
    # cancelled single instance of an existing series -> its own entry
    ghosts, marked, _ = combined_search.reconstruct_events(
        [_invite("S-1", recid="20250610T150000")], [{"uid": "S-1", "st": "confirmed"}])
    assert marked == 0 and len(ghosts) == 1 and ghosts[0]["st"] == "deleted"
    # unchanged instance of an existing series -> no duplicate
    ghosts, _, _ = combined_search.reconstruct_events(
        [_invite("S-1", recid="20250610T150000", method="REQUEST", status="")],
        [{"uid": "S-1", "st": "confirmed"}])
    assert ghosts == []


def test_combined_read_outlook_sammelt_ical(tmp_path):
    root = tmp_path / "outlook_export"
    post = root / "E-Mail" / "Posteingang"
    post.mkdir(parents=True)
    (post / "absage.eml").write_bytes(make_ical_eml(MAIL_ICS))
    (post / "normal.eml").write_bytes(make_eml())   # no text/calendar -> ignored
    (root / "kaputt.eml").mkdir()                   # unreadable -> skipped

    invites = []
    combined_search.read_outlook(root.resolve(), tmp_path.resolve(), invites)

    assert len(invites) == 1
    it = invites[0]
    assert it["method"] == "CANCEL" and it["ev"]["uid"] == "GELOESCHT-1"
    assert it["href"] == "outlook_export/E-Mail/Posteingang/absage.eml"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", it["md"]) and it["mts"] is not None
    # The cancellation comes from the organiser -> sender as the hint
    assert it["org_hint"] == ("Alice Example", "alice@example.com")

    # Reply mail: no ORGANIZER in the iCal – the organiser is the recipient
    # (To), not the sender (deliberately different people here)
    reply_ics = MAIL_ICS.replace("METHOD:CANCEL", "METHOD:REPLY").replace(
        'ORGANIZER;CN="Alice Example":mailto:alice@example.com',
        'ATTENDEE;PARTSTAT=ACCEPTED;CN="Carol Chef":mailto:carol@example.com')
    (post / "antwort.eml").write_bytes(make_ical_eml(
        reply_ics, subject="Angenommen: Jour Fixe", frm="Carol Chef <carol@example.com>",
        method="REPLY"))
    invites = []
    combined_search.read_outlook(root.resolve(), tmp_path.resolve(), invites)
    antwort = [i for i in invites if i["method"] == "REPLY"][0]
    assert antwort["ev"]["org_cn"] == "" and antwort["ev"]["org_mail"] == ""
    assert antwort["org_hint"] == ("Bob Builder", "bob@example.com")   # the reply's To
    ghosts, _, _ = combined_search.reconstruct_events([antwort], [])
    assert ghosts[0]["who"] == "Bob Builder"
    assert "bob@example.com" in ghosts[0]["ppl"]


def test_combined_read_contacts(tmp_path):
    root = tmp_path / "outlook_export"
    d = root / "kontakte" / "Team"
    d.mkdir(parents=True)
    (d / "alice.vcf").write_text(VCF, encoding="utf-8")
    (root / "erika.vcf").write_text(
        "\r\n".join(["BEGIN:VCARD", "N:Muster;Erika;;;", "END:VCARD"]), encoding="utf-8")

    recs = combined_search.read_contacts(root.resolve(), tmp_path.resolve())

    assert len(recs) == 2
    by_title = {r["title"]: r for r in recs}
    alice = by_title["Alice Example"]
    assert alice["ctx"] == "Kontakte: Team"
    assert alice["who"] == "Firma GmbH · Entwicklung"
    assert "Firma GmbH · Entwicklung" in alice["x"]
    assert "Erste Zeileweiter gefaltet" in alice["x"]   # RFC line folding resolved
    assert alice["ts"] is None and alice["d"] == ""
    assert "alice@example.com" in alice["ppl"]
    # Extra fields for the address book
    assert alice["em"] == ["alice@example.com"] and alice["tel"] == ["+49 123 456"]
    assert alice["org"] == "Firma GmbH · Entwicklung" and alice["role"] == "Engineer"
    # without FN: name from the N property (first name last name), generic rest
    erika = by_title["Erika Muster"]
    assert erika["ctx"] == "Kontakte" and erika["who"] == "Kontakt"


# --------------------------------------------------------------------------
# main(): --json is the only output path
# --------------------------------------------------------------------------
def test_combined_main_ohne_json_nennt_die_nutzung(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["combined_search.py"])
    with pytest.raises(SystemExit, match="Usage"):
        combined_search.main()


# --------------------------------------------------------------------------
# collect_calendar_data / --json: the same evaluation without a page around it
# --------------------------------------------------------------------------
def _kalender_export(tmp_path):
    """Outlook export with one event, one cancellation mail and one contact."""
    outlook = tmp_path / "outlook_export"
    post = outlook / "E-Mail" / "Posteingang"
    post.mkdir(parents=True)
    (post / "absage.eml").write_bytes(make_ical_eml(MAIL_ICS))
    kal = outlook / "kalender" / "Arbeit"
    kal.mkdir(parents=True)
    (kal / "termin.ics").write_text(ICS, encoding="utf-8")     # different UID -> stays
    kon = outlook / "kontakte" / "Team"
    kon.mkdir(parents=True)
    (kon / "alice.vcf").write_text(VCF, encoding="utf-8")
    return outlook


def test_collect_calendar_data_liefert_termine_kontakte_und_rekonstruktion(tmp_path):
    daten = combined_search.collect_calendar_data(str(_kalender_export(tmp_path)))

    assert daten["counts"] == {"kalender": 1, "rekonstruiert": 1, "kontakte": 1,
                               "abgesagt_markiert": 0, "doppel_verworfen": 0}
    nach_status = {r.get("st"): r for r in daten["recs"] if r["src"] == "kalender"}
    assert nach_status["confirmed"]["title"] == "Planung, Quartal"

    # The event recovered from the cancellation mail points at the mail itself
    ghost = nach_status["deleted"]
    assert ghost["title"] == "Jour Fixe"
    assert ghost["root"] == "outlook"
    assert ghost["rel"] == "E-Mail/Posteingang/absage.eml"     # relative to the export root

    kontakt = [r for r in daten["recs"] if r["src"] == "kontakte"][0]
    assert kontakt["title"] == "Alice Example"
    assert kontakt["em"] == ["alice@example.com"]
    assert kontakt["rel"] == "kontakte/Team/alice.vcf"


def test_collect_calendar_data_laesst_unnoetiges_weg(tmp_path):
    """uid, ppl and description make up two thirds of the response and are
    read by no view outside the reconstructed events."""
    daten = combined_search.collect_calendar_data(str(_kalender_export(tmp_path)))
    for r in daten["recs"]:
        assert "uid" not in r and "p" not in r
    normal = [r for r in daten["recs"] if r.get("st") == "confirmed"][0]
    assert "ppl" not in normal and "x" not in normal
    ghost = [r for r in daten["recs"] if r.get("st") == "deleted"][0]
    assert "ppl" in ghost                       # the search there filters on it


def test_collect_calendar_data_kuerzt_beschreibungen(tmp_path):
    outlook = _kalender_export(tmp_path)
    lang = MAIL_ICS.replace("LOCATION:Raum 7", "DESCRIPTION:" + "z" * 5000)
    (outlook / "E-Mail" / "Posteingang" / "absage.eml").write_bytes(make_ical_eml(lang))
    daten = combined_search.collect_calendar_data(str(outlook), text_cap=100)
    ghost = [r for r in daten["recs"] if r.get("st") == "deleted"][0]
    assert len(ghost["x"]) == 100


def test_collect_calendar_data_ohne_wiederherstellung(tmp_path):
    """Events and contacts still come out – only the mails stay unread.

    Reported from the field: a run with only "contacts" let the
    reconstruction start anyway and read every one of the 45,000 mails for
    minutes, for a result that could not have changed.
    """
    daten = combined_search.collect_calendar_data(str(_kalender_export(tmp_path)),
                                                  reconstruct=False)
    assert daten["reconstruct"] is False
    assert daten["counts"]["kalender"] == 1 and daten["counts"]["kontakte"] == 1
    assert daten["counts"]["rekonstruiert"] == 0
    assert not [r for r in daten["recs"] if r.get("st") in ("deleted", "gone")]


def test_collect_calendar_data_ohne_wiederherstellung_liest_keine_mail(tmp_path, monkeypatch):
    """The expensive part must not merely be discarded, it must be skipped."""
    gelesen = []
    echt = combined_search.read_outlook
    monkeypatch.setattr(combined_search, "read_outlook",
                        lambda *a, **kw: gelesen.append(1) or echt(*a, **kw))
    outlook = str(_kalender_export(tmp_path))

    combined_search.collect_calendar_data(outlook, reconstruct=False)
    assert gelesen == [], "die Mails wurden trotzdem gelesen"

    combined_search.collect_calendar_data(outlook)          # default: it does
    assert gelesen == [1]


def test_collect_calendar_data_ohne_ordner(tmp_path):
    with pytest.raises(SystemExit, match="nicht gefunden"):
        combined_search.collect_calendar_data(str(tmp_path / "fehlt"))


def test_write_calendar_json_schreibt_atomar(tmp_path):
    ziel = tmp_path / "store" / "calendar.json"
    counts = combined_search.write_calendar_json(str(_kalender_export(tmp_path)), ziel)
    assert counts["rekonstruiert"] == 1
    assert not ziel.with_name(ziel.name + ".tmp").exists()
    daten = json.loads(ziel.read_text(encoding="utf-8"))
    assert daten["counts"] == counts
    assert any(r.get("st") == "deleted" for r in daten["recs"])


def test_main_json_schreibt_die_daten(tmp_path, monkeypatch, capsys):
    """One positional, the Outlook export – that is what the app passes.
    Instead of prose, main() reports only the structured result; the app
    builds the translated log line from it."""
    outlook = _kalender_export(tmp_path)
    ziel = tmp_path / "kal.json"
    monkeypatch.setattr(sys, "argv", ["combined_search.py", str(outlook),
                                      "--json", str(ziel)])
    combined_search.main()
    assert ziel.exists()
    out = capsys.readouterr().out
    fazit = [progress.lies_ergebnis(z) for z in out.splitlines()]
    fazit = [f for f in fazit if f is not None]
    assert fazit and fazit[0]["extra"] == {"events": 1, "rebuilt": 1,
                                          "contacts": 1}


def test_main_json_ohne_wiederherstellung(tmp_path, monkeypatch):
    outlook = _kalender_export(tmp_path)
    ziel = tmp_path / "kal.json"
    monkeypatch.setattr(sys, "argv", ["combined_search.py", str(outlook),
                                      "--json", str(ziel), "--no-reconstruct"])
    combined_search.main()
    daten = json.loads(ziel.read_text(encoding="utf-8"))
    assert daten["reconstruct"] is False and daten["counts"]["rekonstruiert"] == 0


def test_main_json_folgt_der_app_config(tmp_path, monkeypatch):
    """app_config.json also holds when combined_search.py runs by hand."""
    import settings
    monkeypatch.setenv("MUNIMENTUM_DATA_DIR", str(tmp_path))
    (tmp_path / settings.CONFIG_NAME).write_text(
        json.dumps({"calendar_reconstruct": False}), encoding="utf-8")
    settings.reset()
    try:
        outlook = _kalender_export(tmp_path)
        ziel = tmp_path / "kal.json"
        monkeypatch.setattr(sys, "argv", ["combined_search.py", str(outlook),
                                          "--json", str(ziel)])
        combined_search.main()
        assert json.loads(ziel.read_text(encoding="utf-8"))["reconstruct"] is False
    finally:
        settings.reset()
