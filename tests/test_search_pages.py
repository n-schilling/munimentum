"""Tests für den statischen Suchseiten-Generator combined_search.py.

Geprüft werden die HTML-/Text-Helfer und Parser, der Index-Aufbau aus
synthetischen Export-Bäumen in tmp_path sowie die erzeugte Suchseite samt
eingebettetem JSON-Index (Template-Gerüst, Records, Escaping, relative Links).
Keine Netzwerkzugriffe.
"""

import json
import re
import sys
from datetime import datetime
from email.header import Header

import pytest

import combined_search

# --------------------------------------------------------------------------
# Gemeinsame Helfer und Fixtures
# --------------------------------------------------------------------------
IDX_RE = re.compile(r'<script type="application/json" id="idx">(.*?)</script>', re.S)


def read_page(path):
    """Liest die erzeugte Suchseite und extrahiert den eingebetteten JSON-Index.

    Wäre das Escaping des Payloads kaputt (rohes "</script>" im Index), endete
    der Script-Block zu früh und json.loads schlüge fehl – die Extraktion
    prüft das Escaping also implizit mit.
    """
    html = path.read_text(encoding="utf-8")
    m = IDX_RE.search(html)
    assert m is not None, "Index-<script>-Block nicht gefunden"
    return html, json.loads(m.group(1))


TEAMS_HTML = """<html><body>
<h1>Projekt Alpha</h1>
<p class="sub">2 Teilnehmer</p>
<div class="msg">
  <span class="name">Alice Example</span>
  <span class="time">2025-06-01 09:30</span>
  <div class="body"><p>Hallo <b>Bob</b>,</p><div>Gr&uuml;&szlig;e &amp; bis morgen 🎉</div></div>
</div>
<div class="msg">
  <span class="name">Bob</span>
  <span class="time">2025-06-01 09:35</span>
  <div class="body">Danke!</div>
</div>
</body></html>"""

EVIL_SNIPPET = "</script><script>alert(1)</script>"


def make_eml(body="Hallo Bob, hier der Inhalt.", subject="Testmail",
             frm="Alice Example <alice@example.com>",
             to="Bob Builder <bob@example.com>", cc=None,
             date="Mon, 07 Jul 2025 10:00:00 +0000",
             ctype="text/plain; charset=utf-8"):
    """Baut eine minimale .eml als Bytes."""
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
    """Baut eine .eml mit text/calendar-Teil (base64, wie Exchange sie verschickt)."""
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


# Exchange-Einladung: VTIMEZONE mit eigenem DTSTART vor dem VEVENT
MAIL_ICS = "\r\n".join([
    "BEGIN:VCALENDAR",
    "METHOD:CANCEL",
    "VERSION:2.0",
    "BEGIN:VTIMEZONE",
    "TZID:W. Europe Standard Time",
    "BEGIN:STANDARD",
    "DTSTART:16010101T030000",          # Falle: darf nicht als Termin gelesen werden
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
# combined_search: Helfer
# --------------------------------------------------------------------------
def test_combined_parse_local():
    assert combined_search.parse_local("2025-07-07 10:00") is not None
    assert combined_search.parse_local("kein datum") is None
    assert combined_search.parse_local(None) is None


def test_combined_link_kodiert_segmente(tmp_path):
    p = tmp_path / "export" / "Ordner mit Leerzeichen" / "datei ä.html"
    href = combined_search.link(p, tmp_path)
    assert href == "export/Ordner%20mit%20Leerzeichen/datei%20%C3%A4.html"


def test_combined_unfold_und_unescape():
    assert combined_search._unfold("A:1\r\n b\nB:2\n\tc") == ["A:1b", "B:2c"]
    assert combined_search._unescape(r"a\,b\;c\nd\\e") == "a,b;c\nd\\e"


def test_combined_prop_pval_demail():
    name, params, value = combined_search._prop(
        'ORGANIZER;CN="Alice; Ex":mailto:alice@example.com')
    assert name == "ORGANIZER"
    assert combined_search._pval(params, "CN") == "Alice; Ex"  # Anführungszeichen schützen ;
    assert value == "mailto:alice@example.com"
    assert combined_search._prop("zeile ohne doppelpunkt") == (None, None, None)
    assert combined_search._pval(";CN=Bob", "CN") == "Bob"
    assert combined_search._pval("", "CN") == ""
    assert combined_search._demail("MAILTO:Alice@Example.com") == "Alice@Example.com"
    assert combined_search._demail(None) == ""


def test_combined_ics_when_varianten():
    ts, disp = combined_search._ics_when("20250601", dateonly=True)
    assert disp == "2025-06-01" and ts is not None
    ts, disp = combined_search._ics_when("20250601T120000", dateonly=False)
    assert disp == "2025-06-01 12:00" and ts is not None
    ts, disp = combined_search._ics_when("20250601T120000Z", dateonly=False)
    assert ts is not None and re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", disp)
    assert combined_search._ics_when("", dateonly=False) == (None, "")
    assert combined_search._ics_when("unsinn", dateonly=False) == (None, "unsinn")


# --------------------------------------------------------------------------
# combined_search: Einleser (Teams, Mail, Kalender, Kontakte)
# --------------------------------------------------------------------------
def test_combined_read_teams_kategorien_links_und_cap(tmp_path):
    root = tmp_path / "teams_export"
    (root / "1on1").mkdir(parents=True)
    (root / "1on1" / "alice__abc.html").write_text(
        TEAMS_HTML.replace("Danke!", "a" * 4100), encoding="utf-8")
    kanal = root / "channels" / "Team Rocket"
    kanal.mkdir(parents=True)
    (kanal / "general__1.html").write_text(
        TEAMS_HTML.replace("Projekt Alpha", "Allgemein"), encoding="utf-8")
    # werden übersprungen: Index-/Suchseite und versteckte Ordner
    (root / "index.html").write_text("<html></html>", encoding="utf-8")
    (root / "search.html").write_text("<html></html>", encoding="utf-8")
    (root / ".imgcache").mkdir()
    (root / ".imgcache" / "bild.html").write_text(TEAMS_HTML, encoding="utf-8")

    people = set()
    recs = combined_search.read_teams(root.resolve(), tmp_path.resolve(), people)

    assert len(recs) == 4                        # je 2 Nachrichten, Cache/Index ignoriert
    eins = [r for r in recs if r["ctx"] == "1:1-Chat"]
    kanaele = [r for r in recs if r["ctx"] == "Kanal: Allgemein"]
    assert len(eins) == 2 and len(kanaele) == 2
    assert {"Alice Example", "Bob"} <= people
    assert eins[0]["who"] == "Alice Example"
    assert eins[0]["d"] == "2025-06-01 09:30" and eins[0]["ts"] is not None
    assert "alice example" in eins[0]["ppl"]
    # BODY_CAP: lange Nachricht wird gekappt
    bob = [r for r in eins if r["who"] == "Bob"][0]
    assert len(bob["x"]) == combined_search.BODY_CAP
    # Link: relativ zum Ausgabeordner, Segmente URL-kodiert
    assert kanaele[0]["p"] == "teams_export/channels/Team%20Rocket/general__1.html"


def test_combined_read_outlook_ordner_und_personen(tmp_path):
    root = tmp_path / "outlook_export"
    post = root / "E-Mail" / "Posteingang"
    post.mkdir(parents=True)
    (post / "mail.eml").write_bytes(make_eml(cc="Carol <carol@example.com>"))
    (root / "wurzel.eml").write_bytes(make_eml(subject="Wurzelmail"))
    (root / "kaputt.eml").mkdir()                # unlesbar -> wird übersprungen

    people = set()
    recs = combined_search.read_outlook(root.resolve(), tmp_path.resolve(), people)

    assert len(recs) == 2
    by_title = {r["title"]: r for r in recs}
    assert by_title["Testmail"]["ctx"] == "Posteingang"     # "E-Mail/" nur Anzeige entfernt
    assert by_title["Wurzelmail"]["ctx"] == "(Stamm)"
    r = by_title["Testmail"]
    assert r["src"] == "outlook" and r["who"] == "Alice Example"
    assert "carol@example.com" in r["ppl"]
    assert "hier der Inhalt" in r["x"]
    assert r["ts"] is not None
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", r["d"])
    assert r["p"] == "outlook_export/E-Mail/Posteingang/mail.eml"
    assert {"Alice Example", "Bob Builder", "carol@example.com"} <= people


def test_combined_read_calendar(tmp_path):
    root = tmp_path / "outlook_export"
    d = root / "kalender" / "Arbeit"
    d.mkdir(parents=True)
    (d / "termin.ics").write_text(ICS, encoding="utf-8")

    people = set()
    recs = combined_search.read_calendar(root.resolve(), tmp_path.resolve(), people)

    assert len(recs) == 1
    r = recs[0]
    assert r["src"] == "kalender"
    assert r["title"] == "Planung, Quartal"      # \\, entschärft
    assert r["ctx"] == "Kalender: Arbeit"
    assert r["who"] == "Alice Example"
    assert r["x"].startswith("Ort: Raum 42.")
    assert "bob@example.com" in r["ppl"]
    assert r["ts"] is not None
    assert {"Alice Example", "Bob Builder",
            "alice@example.com", "bob@example.com"} <= people
    # Zusatzfelder für die Kalenderansicht (Defaults ohne DTEND/STATUS)
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
                (tmp_path / "outlook_export").resolve(), tmp_path.resolve(), set())}

    u = recs["Urlaub"]
    assert u["ad"] == 1 and u["st"] == "cancelled"
    assert u["te"] - u["ts"] == 3 * 86400        # DTEND exklusiv, 3 Tage
    w = recs["Workshop"]
    assert w["ad"] == 0 and w["st"] == "tentative"
    assert w["te"] - w["ts"] == 90 * 60


def test_combined_ics_when_zeitzonen():
    utc = combined_search.UTC
    # Windows-Zeitzonenname aus Exchange-Einladungen: 15:00 London = 14:00 UTC
    ts, _ = combined_search._ics_when("20250610T150000", False, "GMT Standard Time")
    assert datetime.fromtimestamp(ts, utc).hour == 14
    ts, _ = combined_search._ics_when("20250610T150000", False, "Pacific Standard Time")
    assert datetime.fromtimestamp(ts, utc).hour == 22
    # IANA-Namen direkt, "Z" schlägt TZID, Unbekanntes bleibt Lokalzeit
    ts, _ = combined_search._ics_when("20250610T150000", False, "America/New_York")
    assert datetime.fromtimestamp(ts, utc).hour == 19
    ts, _ = combined_search._ics_when("20250610T150000Z", False, "Pacific Standard Time")
    assert datetime.fromtimestamp(ts, utc).hour == 15
    naiv = combined_search._ics_when("20250610T150000", False)[0]
    assert combined_search._ics_when("20250610T150000", False, "Quatsch/Zone")[0] == naiv
    assert combined_search._zone("") is None


def test_combined_parse_vevents_ignoriert_vtimezone():
    method, evs = combined_search.parse_vevents(MAIL_ICS)
    assert method == "CANCEL" and len(evs) == 1
    ev = evs[0]
    # DTSTART des Termins, nicht das der Zeitzonenregel (16010101T030000)
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
    # Exchange-Global-Object-ID mit eingebetteter Google-UID (wie im echten Export)
    blob = ("040000008200E00074C5B7101A82E00800000000000000000000000000000000000000003200"
            "00007643616C2D55696401000000326E6934386C71716B6B326E313772687567643335383"
            "46A6F7140676F6F676C652E636F6D00")
    assert combined_search.norm_uid(blob) == "2ni48lqqkk2n17rhugd3584joq@google.com"
    # native UIDs bleiben (bis auf Groß-/Kleinschreibung) unverändert
    assert combined_search.norm_uid("ABC@example.com") == "abc@example.com"
    assert combined_search.norm_uid("") == ""
    assert combined_search.norm_uid("040000008200E0FF") == "040000008200e0ff"  # kein vCal-Uid
    assert combined_search.norm_uid("ZZZZ" * 30) == ("zzzz" * 30)              # kein Hex


def test_combined_reconstruct_erkennt_ausgepackte_uid():
    blob = ("040000008200E00074C5B7101A82E00800000000000000000000000000000000000000003200"
            "00007643616C2D55696401000000326E6934386C71716B6B326E313772687567643335383"
            "46A6F7140676F6F676C652E636F6D00")
    im_kalender = {"uid": blob, "st": "confirmed", "title": "Review", "ts": None}
    ghosts, marked, dupes = combined_search.reconstruct_events(
        [_invite("2ni48lqqkk2n17rhugd3584joq@google.com", summary="Review")], [im_kalender])
    # gleicher Termin -> keine Geisterkopie, stattdessen Status geheilt
    assert ghosts == [] and marked == 1 and dupes == 0


def test_combined_reconstruct_verwirft_doppel_bei_gleichem_titel_und_start():
    ts = combined_search._ics_when("20250610T150000", False)[0]
    im_kalender = {"uid": "ANDERE-ID", "st": "confirmed", "title": "Jour Fixe", "ts": ts}
    ghosts, marked, dupes = combined_search.reconstruct_events(
        [_invite("UNBEKANNTES-FORMAT")], [im_kalender])
    assert ghosts == [] and marked == 0 and dupes == 1


def test_combined_reconstruct_geloeschte_termine():
    # im Kalender vorhanden -> keine Rekonstruktion, aber Status wird geheilt
    im_kalender = {"uid": "DA-1", "st": "confirmed"}
    ghosts, marked, _ = combined_search.reconstruct_events(
        [_invite("DA-1"), _invite("WEG-1"),
         _invite("NIE-1", method="REPLY", summary="Angenommen: Workshop",
                 status="", dtstart="20250612T090000")],
        [im_kalender])

    assert marked == 1 and im_kalender["st"] == "cancelled"
    by_title = {r["title"]: r for r in ghosts}
    assert set(by_title) == {"Jour Fixe", "Workshop"}       # Status-Präfixe entfernt
    weg = by_title["Jour Fixe"]
    assert weg["st"] == "deleted" and weg["src"] == "kalender"
    assert weg["p"] == "mail.eml" and "rekonstruiert aus Mail" in weg["ctx"]
    assert weg["ts"] is not None
    # nur eingeladen/zugesagt, nie (mehr) im Kalender: kein Löschbeleg
    assert by_title["Workshop"]["st"] == "gone"


def test_combined_reconstruct_ohne_startzeit_und_serieninstanz():
    # ohne DTSTART nicht platzierbar -> verworfen
    ghosts, _, _ = combined_search.reconstruct_events([_invite("X", dtstart="")], [])
    assert ghosts == []
    # abgesagte Einzelinstanz einer vorhandenen Serie -> eigener Eintrag
    ghosts, marked, _ = combined_search.reconstruct_events(
        [_invite("S-1", recid="20250610T150000")], [{"uid": "S-1", "st": "confirmed"}])
    assert marked == 0 and len(ghosts) == 1 and ghosts[0]["st"] == "deleted"
    # unveränderte Instanz einer vorhandenen Serie -> kein Duplikat
    ghosts, _, _ = combined_search.reconstruct_events(
        [_invite("S-1", recid="20250610T150000", method="REQUEST", status="")],
        [{"uid": "S-1", "st": "confirmed"}])
    assert ghosts == []


def test_combined_read_outlook_sammelt_ical(tmp_path):
    root = tmp_path / "outlook_export"
    post = root / "E-Mail" / "Posteingang"
    post.mkdir(parents=True)
    (post / "absage.eml").write_bytes(make_ical_eml(MAIL_ICS))

    invites = []
    recs = combined_search.read_outlook(root.resolve(), tmp_path.resolve(), set(), invites)

    assert len(recs) == 1 and len(invites) == 1
    it = invites[0]
    assert it["method"] == "CANCEL" and it["ev"]["uid"] == "GELOESCHT-1"
    assert it["href"] == "outlook_export/E-Mail/Posteingang/absage.eml"
    # Absage kommt vom Organisator -> Absender als Hinweis
    assert it["org_hint"] == ("Alice Example", "alice@example.com")

    # ohne invites-Liste bleibt das Verhalten unverändert
    assert len(combined_search.read_outlook(root.resolve(), tmp_path.resolve(), set())) == 1

    # Antwortmail: kein ORGANIZER im iCal – Organisator ist der Empfänger (To),
    # nicht der Absender (hier bewusst verschiedene Personen)
    reply_ics = MAIL_ICS.replace("METHOD:CANCEL", "METHOD:REPLY").replace(
        'ORGANIZER;CN="Alice Example":mailto:alice@example.com',
        'ATTENDEE;PARTSTAT=ACCEPTED;CN="Carol Chef":mailto:carol@example.com')
    (post / "antwort.eml").write_bytes(make_ical_eml(
        reply_ics, subject="Angenommen: Jour Fixe", frm="Carol Chef <carol@example.com>",
        method="REPLY"))
    invites = []
    combined_search.read_outlook(root.resolve(), tmp_path.resolve(), set(), invites)
    antwort = [i for i in invites if i["method"] == "REPLY"][0]
    assert antwort["ev"]["org_cn"] == "" and antwort["ev"]["org_mail"] == ""
    assert antwort["org_hint"] == ("Bob Builder", "bob@example.com")   # das To der Antwort
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

    people = set()
    recs = combined_search.read_contacts(root.resolve(), tmp_path.resolve(), people)

    assert len(recs) == 2
    by_title = {r["title"]: r for r in recs}
    alice = by_title["Alice Example"]
    assert alice["ctx"] == "Kontakte: Team"
    assert alice["who"] == "Firma GmbH · Entwicklung"
    assert "Firma GmbH · Entwicklung" in alice["x"]
    assert "Erste Zeileweiter gefaltet" in alice["x"]   # RFC-Zeilenfaltung aufgelöst
    assert alice["ts"] is None and alice["d"] == ""
    assert "alice@example.com" in alice["ppl"]
    # Zusatzfelder für das Adressbuch
    assert alice["em"] == ["alice@example.com"] and alice["tel"] == ["+49 123 456"]
    assert alice["org"] == "Firma GmbH · Entwicklung" and alice["role"] == "Engineer"
    # ohne FN: Name aus N-Property (Vorname Nachname), generischer Rest
    erika = by_title["Erika Muster"]
    assert erika["ctx"] == "Kontakte" and erika["who"] == "Kontakt"
    assert {"Alice Example", "alice@example.com", "Erika Muster"} <= people


# --------------------------------------------------------------------------
# combined_search: build() und main()
# --------------------------------------------------------------------------
def test_combined_build_rekonstruiert_geloeschten_termin(tmp_path):
    outlook = tmp_path / "outlook_export"
    post = outlook / "E-Mail" / "Posteingang"
    post.mkdir(parents=True)
    (post / "absage.eml").write_bytes(make_ical_eml(MAIL_ICS))
    kal = outlook / "kalender" / "Arbeit"
    kal.mkdir(parents=True)
    (kal / "termin.ics").write_text(ICS, encoding="utf-8")   # andere UID -> bleibt

    ziel = tmp_path / "combined_search.html"
    _, counts = combined_search.build(str(tmp_path / "fehlt"), str(outlook), ziel)

    assert counts["kalender"] == 1 and counts["rekonstruiert"] == 1
    _, idx = read_page(ziel)
    ghost = [r for r in idx["recs"] if r.get("st") == "deleted"][0]
    assert ghost["title"] == "Jour Fixe"
    assert ghost["p"] == "outlook_export/E-Mail/Posteingang/absage.eml"
    # der vorhandene Termin bleibt unangetastet
    assert [r for r in idx["recs"] if r.get("st") == "confirmed"][0]["title"] == "Planung, Quartal"


def test_combined_build_gesamtseite(tmp_path):
    teams = tmp_path / "teams_export"
    (teams / "1on1").mkdir(parents=True)
    (teams / "1on1" / "alice__abc.html").write_text(TEAMS_HTML, encoding="utf-8")
    outlook = tmp_path / "outlook_export"
    post = outlook / "E-Mail" / "Posteingang"
    post.mkdir(parents=True)
    (post / "mail.eml").write_bytes(make_eml(
        subject=Header("Grüße 🎉 Bericht", "utf-8").encode(),
        body="Zusammenfassung folgt. " + EVIL_SNIPPET))
    kal = outlook / "kalender" / "Arbeit"
    kal.mkdir(parents=True)
    (kal / "termin.ics").write_text(ICS, encoding="utf-8")
    kon = outlook / "kontakte" / "Team"
    kon.mkdir(parents=True)
    (kon / "alice.vcf").write_text(VCF, encoding="utf-8")

    ziel = tmp_path / "combined_search.html"
    out, counts = combined_search.build(str(teams), str(outlook), ziel)

    assert out == ziel and ziel.is_file()
    assert counts == {"teams": 2, "outlook": 1, "kalender": 1, "kontakte": 1,
                      "rekonstruiert": 0}
    html, idx = read_page(ziel)
    assert html.startswith("<!DOCTYPE html>")
    assert "Teams + Outlook · Suche" in html     # Template-Gerüst
    for tab in ('data-view="search"', 'data-view="cal"', 'data-view="book"'):
        assert tab in html                       # Suche, Kalender, Adressbuch
    for mode in ('data-mode="week"', 'data-mode="month"', 'data-mode="rebuilt"'):
        assert mode in html                      # Woche, Monat, Rekonstruiert
    # Escaping: kein rohes </script> aus Nachrichteninhalten im HTML
    assert EVIL_SNIPPET not in html
    assert "\\u003c/script" in html
    # Umlaute/Emoji unkodiert im HTML (ensure_ascii=False)
    assert "Grüße 🎉 Bericht" in html and "bis morgen 🎉" in html

    recs = idx["recs"]
    assert len(recs) == 5
    # Sortierung: Zeitstempel absteigend, undatierte (Kontakte) zuletzt
    ts = [r["ts"] for r in recs]
    datiert = [t for t in ts if t is not None]
    assert datiert == sorted(datiert, reverse=True)
    assert ts[-1] is None and recs[-1]["src"] == "kontakte"
    mail = [r for r in recs if r["src"] == "outlook"][0]
    assert mail["title"] == "Grüße 🎉 Bericht"
    assert EVIL_SNIPPET in mail["x"]             # Inhalt unversehrt im Index
    assert mail["p"] == "outlook_export/E-Mail/Posteingang/mail.eml"
    teams_recs = [r for r in recs if r["src"] == "teams"]
    assert len(teams_recs) == 2
    assert teams_recs[0]["ctx"] == "1:1-Chat"
    assert [r for r in recs if r["src"] == "kalender"][0]["title"] == "Planung, Quartal"
    # Personenliste: case-insensitiv sortiert, Namen und Adressen enthalten
    people = idx["people"]
    assert people == sorted(people, key=str.lower)
    assert "Alice Example" in people and "bob@example.com" in people


def test_combined_build_ohne_teams_ordner(tmp_path, capsys):
    outlook = tmp_path / "outlook_export"
    outlook.mkdir()
    (outlook / "mail.eml").write_bytes(make_eml())
    ziel = tmp_path / "nur_outlook.html"
    _, counts = combined_search.build(str(tmp_path / "fehlt"), str(outlook), ziel)
    assert counts["teams"] == 0 and counts["outlook"] == 1
    assert "übersprungen" in capsys.readouterr().out
    _, idx = read_page(ziel)
    assert [r["src"] for r in idx["recs"]] == ["outlook"]


def test_combined_build_leere_ordner(tmp_path):
    (tmp_path / "teams_export").mkdir()
    (tmp_path / "outlook_export").mkdir()
    ziel = tmp_path / "leer.html"
    _, counts = combined_search.build(str(tmp_path / "teams_export"),
                                      str(tmp_path / "outlook_export"), ziel)
    assert sum(counts.values()) == 0
    _, idx = read_page(ziel)
    assert idx["recs"] == [] and idx["people"] == []


def test_combined_main_default_ausgabe(tmp_path, monkeypatch):
    t = tmp_path / "teams_export" / "1on1"
    t.mkdir(parents=True)
    (t / "a__1.html").write_text(TEAMS_HTML, encoding="utf-8")
    o = tmp_path / "outlook_export" / "Posteingang"
    o.mkdir(parents=True)
    (o / "m.eml").write_bytes(make_eml())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["combined_search.py"])
    combined_search.main()
    # Default-Ausgabe im gemeinsamen übergeordneten Ordner beider Exporte
    out = tmp_path / "combined_search.html"
    assert out.is_file()
    _, idx = read_page(out)
    assert len(idx["recs"]) == 3                 # 2 Teams-Nachrichten + 1 Mail
    assert {r["src"] for r in idx["recs"]} == {"teams", "outlook"}


def test_combined_main_mit_output_flag(tmp_path, monkeypatch):
    outlook = tmp_path / "outlook_export"
    outlook.mkdir()
    (outlook / "m.eml").write_bytes(make_eml())
    ziel = tmp_path / "ergebnis.html"
    monkeypatch.setattr(sys, "argv", ["combined_search.py", str(tmp_path / "fehlt"),
                                      str(outlook), "-o", str(ziel)])
    combined_search.main()
    assert ziel.is_file()
    _, idx = read_page(ziel)
    assert [r["src"] for r in idx["recs"]] == ["outlook"]


def test_combined_main_ohne_ordner_bricht_ab(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["combined_search.py"])
    with pytest.raises(SystemExit, match="nichts zu tun"):
        combined_search.main()


# --------------------------------------------------------------------------
# Regression: "<" wird komplett als < eingebettet (nicht nur "</")
# --------------------------------------------------------------------------
KOMMENTAR_ANGRIFF = "Beispiel <!--<script>alert(2)</script> Ende"

KOMMENTAR_TEAMS_HTML = """<html><body>
<h1>Chat</h1>
<div class="msg">
  <span class="name">Mallory</span>
  <span class="time">2025-06-02 08:00</span>
  <div class="body">Beispiel &lt;!--&lt;script&gt;alert(2)&lt;/script&gt; Ende</div>
</div>
</body></html>"""


def _payload(path):
    m = IDX_RE.search(path.read_text(encoding="utf-8"))
    assert m is not None
    return m.group(1)


def test_generator_escapet_jedes_kleiner_zeichen(tmp_path):
    """'<!--' + '<script' im Inhalt bricht sonst den Script-Block auf.

    Der Browser wechselt bei '<!--' gefolgt von '<script' in den
    "double-escaped"-Zustand und übersieht das echte '</script>' – deshalb
    wird jedes '<' als \\u003c eingebettet; im Payload darf keines übrig sein.
    """
    teams = tmp_path / "teams" / "1on1"
    teams.mkdir(parents=True)
    (teams / "mallory__1.html").write_text(KOMMENTAR_TEAMS_HTML, encoding="utf-8")
    outlook = tmp_path / "outlook"
    outlook.mkdir()
    (outlook / "mail.eml").write_bytes(make_eml(body=KOMMENTAR_ANGRIFF))

    ziel = tmp_path / "combined.html"
    combined_search.build(str(tmp_path / "teams"), str(outlook), ziel)
    assert "<" not in _payload(ziel)
    _, idx = read_page(ziel)
    treffer = [r for r in idx["recs"] if KOMMENTAR_ANGRIFF in r["x"]]
    assert len(treffer) == 2                     # Teams-Nachricht und Mail
