"""Tests für outlook_export.py – Helfer, ICS/VCF-Erzeugung, DoneLog, Baum-Aufbau
und HTTP-Verhalten der Graph-Clients. Alles ohne Netzwerk: SESSION bzw. die
Graph-Objekte werden durch Fakes ersetzt."""

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
import requests

import outlook_export


# --------------------------------------------------------------------------
# Gemeinsame Fakes (kein Netzwerk)
# --------------------------------------------------------------------------
class FakeResponse:
    """Nachgebaute requests-Response ohne Netzwerk."""

    def __init__(self, status=200, payload=None, content=b"", headers=None):
        self.status_code = status
        self._payload = payload
        self.content = content
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """Liefert vorbereitete Antworten der Reihe nach und protokolliert Aufrufe."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {}),
                           "params": params, "timeout": timeout})
        r = self.responses.pop(0)
        if isinstance(r, Exception):   # Netzwerkfehler simulieren
            raise r
        return r


def _unfold(text):
    """RFC-Zeilenfaltung auflösen und in logische Zeilen zerlegen."""
    return text.replace("\r\n ", "").split("\r\n")


@pytest.fixture(autouse=True)
def _stop_zuruecksetzen():
    """Globales STOP-Event nach jedem Test löschen (Modul-Zustand)."""
    yield
    outlook_export.STOP.clear()


# --------------------------------------------------------------------------
# Dateinamen- und Pfad-Helfer
# --------------------------------------------------------------------------
def test_safe_ersetzt_verbotene_zeichen_und_kuerzt():
    assert outlook_export.safe('a\\b/c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"
    # Tab zählt zu den verbotenen Zeichen (-> "_"), Mehrfach-Leerzeichen kollabieren
    assert outlook_export.safe("  viel   Platz \t hier ") == "viel Platz _ hier"
    assert outlook_export.safe("  viel   Platz  hier ") == "viel Platz hier"
    assert outlook_export.safe("x" * 200) == "x" * 80
    assert outlook_export.safe("x" * 200, maxlen=10) == "x" * 10
    assert outlook_export.safe("...") == "unbenannt"
    assert outlook_export.safe("") == "unbenannt"
    assert outlook_export.safe(None) == "unbenannt"


def test_short_id_ist_deterministisch_und_kurz():
    a = outlook_export.short_id("abc")
    assert a == outlook_export.short_id("abc")
    assert len(a) == 8
    assert re.fullmatch(r"[0-9a-f]{8}", a)
    assert a != outlook_export.short_id("abd")
    # None wird wie leerer String behandelt
    assert outlook_export.short_id(None) == outlook_export.short_id("")


def test_mail_filename_mit_datum_betreff_und_id():
    msg = {"id": "AAA", "subject": "Bericht: Q3/2025?",
           "receivedDateTime": "2025-07-07T10:00:00.0000000Z"}
    name = outlook_export.mail_filename(msg)
    # Zeitstempel hängt von der lokalen Zeitzone ab -> nur das Format prüfen
    assert re.match(r"^\d{4}-\d{2}-\d{2}_\d{4}__", name)
    assert "Bericht_ Q3_2025_" in name
    assert name.endswith(f"__{outlook_export.short_id('AAA')}.eml")


def test_mail_filename_ohne_datum_und_ohne_betreff():
    name = outlook_export.mail_filename({"id": "BBB"})
    assert name == f"(kein Betreff)__{outlook_export.short_id('BBB')}.eml"


def test_mail_filename_faellt_auf_sentdatetime_und_rohpraefix_zurueck():
    # sentDateTime greift, wenn receivedDateTime fehlt
    name = outlook_export.mail_filename({"id": "C", "subject": "x",
                                         "sentDateTime": "2025-01-02T03:04:05Z"})
    assert re.match(r"^\d{4}-\d{2}-\d{2}_\d{4}__x__", name)
    # Unparsebares Datum -> erste 10 Zeichen als Präfix
    name = outlook_export.mail_filename({"id": "D", "subject": "x",
                                         "receivedDateTime": "unfug-datum-99"})
    assert name.startswith("unfug-datu__x__")


def test_folder_params_beruecksichtigt_include_hidden(monkeypatch):
    assert outlook_export.folder_params() == {"$top": 100}
    monkeypatch.setattr(outlook_export, "INCLUDE_HIDDEN", True)
    assert outlook_export.folder_params() == {"$top": 100, "includeHiddenFolders": "true"}


# --------------------------------------------------------------------------
# Auswahl-Logik (Indizes, Standard-Ausschlüsse, Prompts)
# --------------------------------------------------------------------------
def test_parse_indices_filtert_und_dedupliziert():
    assert outlook_export.parse_indices("1, 3 2", 5) == [1, 3, 2]
    assert outlook_export.parse_indices("2 2 2", 5) == [2]
    assert outlook_export.parse_indices("0 6 -1 abc", 5) == []
    assert outlook_export.parse_indices("", 5) == []
    assert outlook_export.parse_indices("  4  ", 3) == []


def test_is_default_skip_vergleicht_case_insensitive():
    assert outlook_export._is_default_skip({"folder": {"displayName": "Junk-E-Mail"}})
    assert outlook_export._is_default_skip({"folder": {"displayName": "  DRAFTS  "}})
    assert outlook_export._is_default_skip({"folder": {"displayName": "Gelöschte Elemente"}})
    assert not outlook_export._is_default_skip({"folder": {"displayName": "Posteingang"}})
    assert not outlook_export._is_default_skip({"folder": {}})


def test_interactive_beachtet_tty_und_default_flag(monkeypatch):
    monkeypatch.setattr(outlook_export.sys.stdin, "isatty", lambda: True)
    assert outlook_export._interactive()
    monkeypatch.setattr(outlook_export, "ASSUME_DEFAULT", True)
    assert not outlook_export._interactive()
    monkeypatch.setattr(outlook_export, "ASSUME_DEFAULT", False)
    monkeypatch.setattr(outlook_export.sys.stdin, "isatty", lambda: False)
    assert not outlook_export._interactive()


def test_read_liefert_leerstring_bei_eof(monkeypatch):
    def _eof(prompt):
        raise EOFError
    monkeypatch.setattr("builtins.input", _eof)
    assert outlook_export._read("? ") == ""


def _tops():
    """Drei oberste Ordner, einer davon ein Standard-Ausschluss (Drafts)."""
    return [
        {"folder": {"displayName": "Projekte"}, "nfolders": 1, "items": 3},
        {"folder": {"displayName": "Drafts"}, "nfolders": 1, "items": 2},
        {"folder": {"displayName": "Inbox"}, "nfolders": 2, "items": 10},
    ]


def test_select_mail_folders_nicht_interaktiv_nimmt_default(monkeypatch):
    monkeypatch.setattr(outlook_export, "_interactive", lambda: False)
    sel = outlook_export.select_mail_folders(_tops())
    names = [t["folder"]["displayName"] for t in sel]
    assert names == ["Inbox", "Projekte"]  # sortiert, ohne Drafts


def test_select_mail_folders_interaktive_auswahl(monkeypatch):
    monkeypatch.setattr(outlook_export, "_interactive", lambda: True)
    # Nach Sortierung: 1) Drafts  2) Inbox  3) Projekte
    monkeypatch.setattr(outlook_export, "_read", lambda p: "1")
    sel = outlook_export.select_mail_folders(_tops())
    assert [t["folder"]["displayName"] for t in sel] == ["Drafts"]
    # Ungültige Eingabe -> Default (ohne Ausschlüsse)
    monkeypatch.setattr(outlook_export, "_read", lambda p: "99")
    sel = outlook_export.select_mail_folders(_tops())
    assert [t["folder"]["displayName"] for t in sel] == ["Inbox", "Projekte"]


def test_select_calendars_default_und_auswahl(monkeypatch):
    cals = [{"name": "B"}, {"name": "A", "isDefaultCalendar": True}]
    monkeypatch.setattr(outlook_export, "_interactive", lambda: False)
    assert outlook_export.select_calendars(cals) == [cals[1]]
    # Ohne Standardkalender: erster Eintrag
    assert outlook_export.select_calendars([{"name": "X"}, {"name": "Y"}]) == [{"name": "X"}]
    assert outlook_export.select_calendars([]) == []
    # Interaktiv: gezielte Auswahl
    monkeypatch.setattr(outlook_export, "_interactive", lambda: True)
    monkeypatch.setattr(outlook_export, "_read", lambda p: "1")
    assert outlook_export.select_calendars(cals) == [cals[0]]


def test_prompt_categories_env(monkeypatch, capsys):
    """EXPORT_CATEGORIES setzt die Auswahl ohne jede Abfrage (app.py, Zeitplan)."""
    monkeypatch.setattr(outlook_export, "_interactive", lambda: True)
    monkeypatch.setenv("EXPORT_CATEGORIES", "mail;CONTACTS")
    assert outlook_export.prompt_categories() == {"mail", "contacts"}
    assert "EXPORT_CATEGORIES" in capsys.readouterr().out

    # Nur Unbekanntes zählt wie „nicht gesetzt“ – dann greift die Standardauswahl
    monkeypatch.setenv("EXPORT_CATEGORIES", "quatsch")
    monkeypatch.setattr(outlook_export, "_interactive", lambda: False)
    assert outlook_export.prompt_categories() == {"mail", "calendar", "contacts"}


def test_prompt_categories(monkeypatch):
    monkeypatch.delenv("EXPORT_CATEGORIES", raising=False)
    monkeypatch.setattr(outlook_export, "_interactive", lambda: False)
    assert outlook_export.prompt_categories() == {"mail", "calendar", "contacts"}
    monkeypatch.setattr(outlook_export, "_interactive", lambda: True)
    monkeypatch.setattr(outlook_export, "_read", lambda p: "1 3")
    assert outlook_export.prompt_categories() == {"mail", "contacts"}
    monkeypatch.setattr(outlook_export, "_read", lambda p: "")
    assert outlook_export.prompt_categories() == {"mail", "calendar", "contacts"}
    monkeypatch.setattr(outlook_export, "_read", lambda p: "abc")
    assert outlook_export.prompt_categories() == {"mail", "calendar", "contacts"}


# --------------------------------------------------------------------------
# Token aus Datei/Umgebung
# --------------------------------------------------------------------------
def test_load_pasted_token_aus_env_und_datei(tmp_path, monkeypatch):
    # Beides umbiegen: seit auth.py sucht die Funktion neben dem Aufruf UND im
    # Datenordner. Ohne die zweite Zeile fände sie das gx_token.txt des Repos –
    # der Test hinge dann an der Reihenfolge der Testläufe.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFFICE365_DATA_DIR", str(tmp_path))
    import settings
    settings.reset()
    monkeypatch.delenv("GRAPH_TOKEN", raising=False)
    assert outlook_export.load_pasted_token() is None

    monkeypatch.setenv("GRAPH_TOKEN", '  "Bearer eyJ0abc"  ')
    assert outlook_export.load_pasted_token() == "eyJ0abc"
    monkeypatch.setenv("GRAPH_TOKEN", "   ")
    assert outlook_export.load_pasted_token() is None

    monkeypatch.delenv("GRAPH_TOKEN")
    (tmp_path / "gx_token.txt").write_text("'bearer eyJ0datei'\n", encoding="utf-8")
    assert outlook_export.load_pasted_token() == "eyJ0datei"


# --------------------------------------------------------------------------
# Text-, Escaping- und Faltungs-Helfer (ICS/VCF)
# --------------------------------------------------------------------------
def test_plain_text_entfernt_html_und_kollabiert_whitespace():
    body = {"contentType": "html",
            "content": "<p>Hallo <b>Welt</b></p><script>alert(1)</script>"
                       "<style>p{}</style>&amp; mehr<br>Zeile"}
    out = outlook_export._plain_text(body)
    assert "alert" not in out and "p{}" not in out and "<" not in out
    assert "Hallo Welt" in out
    assert "& mehr Zeile" in out
    # Text-Body bleibt bis auf Whitespace unangetastet
    assert outlook_export._plain_text({"contentType": "text", "content": " a \n b "}) == "a b"
    assert outlook_export._plain_text(None) == ""


def test_esc_maskiert_sonderzeichen_und_zeilenumbrueche():
    assert outlook_export._esc("a;b,c\\d\r\ne\rf") == "a\\;b\\,c\\\\d\\ne\\nf"
    assert outlook_export._esc(None) == ""


def test_cn_setzt_anfuehrungszeichen_und_ersetzt_doppelte():
    assert outlook_export._cn("Alice  Example") == '"Alice Example"'
    assert outlook_export._cn('Bob "Builder"') == "\"Bob 'Builder'\""
    assert outlook_export._cn(None) == '""'


def test_fold_haelt_75_oktett_grenze_und_ist_umkehrbar():
    line = "DESCRIPTION:" + "ä" * 100 + "x" * 50
    folded = outlook_export._fold(line)
    assert "\r\n " in folded
    # Jede physische Zeile bleibt unter der RFC-Grenze (75 Oktette ohne CRLF)
    for part in folded.split("\r\n"):
        assert len(part.encode("utf-8")) <= 75
    # Entfalten stellt das Original wieder her
    assert folded.replace("\r\n ", "") == line
    # Kurze Zeilen bleiben unverändert
    assert outlook_export._fold("SUMMARY:kurz") == "SUMMARY:kurz"


def test_graph_dt_parst_graph_zeitstempel():
    # 7 Nachkommastellen (Graph-Format) werden auf 6 gekürzt, "Z" entfernt
    assert outlook_export._graph_dt("2025-06-01T12:00:00.0000000Z") == datetime(2025, 6, 1, 12)
    assert outlook_export._graph_dt("2025-06-01T12:30:45") == datetime(2025, 6, 1, 12, 30, 45)
    # Fallback über strptime auf die ersten 19 Zeichen
    assert outlook_export._graph_dt("2025-06-01T12:00:00+9999") == datetime(2025, 6, 1, 12)
    assert outlook_export._graph_dt("") is None
    assert outlook_export._graph_dt(None) is None
    assert outlook_export._graph_dt("unsinn") is None


def test_ics_dt_und_stamp_formate():
    node = {"dateTime": "2025-06-01T12:30:00.0000000"}
    assert outlook_export._ics_dt(node, all_day=False) == "20250601T123000Z"
    assert outlook_export._ics_dt(node, all_day=True) == "20250601"
    assert outlook_export._ics_dt(None, all_day=False) is None
    assert outlook_export._ics_dt({}, all_day=False) is None
    assert outlook_export._stamp(node, all_day=False) == "2025-06-01_1230"
    assert outlook_export._stamp(node, all_day=True) == "2025-06-01"
    assert outlook_export._stamp(None, all_day=False) == ""


# --------------------------------------------------------------------------
# RRULE-Aufbau
# --------------------------------------------------------------------------
def test_build_rrule_taeglich_mit_intervall_und_count():
    rec = {"pattern": {"type": "daily", "interval": 2},
           "range": {"type": "numbered", "numberOfOccurrences": 10}}
    assert outlook_export.build_rrule(rec, all_day=False) == "FREQ=DAILY;INTERVAL=2;COUNT=10"


def test_build_rrule_woechentlich_mit_enddatum():
    rec = {"pattern": {"type": "weekly", "daysOfWeek": ["monday", "wednesday"], "interval": 1},
           "range": {"type": "endDate", "endDate": "2025-12-31"}}
    assert outlook_export.build_rrule(rec, all_day=False) == \
        "FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20251231T235959Z"
    assert outlook_export.build_rrule(rec, all_day=True) == \
        "FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20251231"


def test_build_rrule_monatlich_und_jaehrlich():
    rec = {"pattern": {"type": "relativeMonthly", "daysOfWeek": ["friday"], "index": "last"},
           "range": {"type": "noEnd"}}
    assert outlook_export.build_rrule(rec, all_day=False) == "FREQ=MONTHLY;BYDAY=-1FR"
    rec = {"pattern": {"type": "absoluteMonthly", "dayOfMonth": 15}, "range": {}}
    assert outlook_export.build_rrule(rec, all_day=False) == "FREQ=MONTHLY;BYMONTHDAY=15"
    rec = {"pattern": {"type": "absoluteYearly", "month": 7, "dayOfMonth": 4}, "range": {}}
    assert outlook_export.build_rrule(rec, all_day=False) == "FREQ=YEARLY;BYMONTH=7;BYMONTHDAY=4"
    rec = {"pattern": {"type": "relativeYearly", "month": 11,
                       "daysOfWeek": ["thursday"], "index": "fourth"}, "range": {}}
    assert outlook_export.build_rrule(rec, all_day=False) == "FREQ=YEARLY;BYMONTH=11;BYDAY=4TH"


def test_build_rrule_unbekannt_oder_leer():
    assert outlook_export.build_rrule(None, all_day=False) is None
    assert outlook_export.build_rrule({"pattern": {"type": "mystisch"}}, all_day=False) is None


# --------------------------------------------------------------------------
# ICS-Erzeugung (Termine)
# --------------------------------------------------------------------------
EVENT = {
    "id": "ev1", "iCalUId": "uid-1", "subject": "Planung; Q3",
    "isAllDay": False,
    "start": {"dateTime": "2025-06-01T12:00:00.0000000", "timeZone": "UTC"},
    "end": {"dateTime": "2025-06-01T13:00:00.0000000", "timeZone": "UTC"},
    "lastModifiedDateTime": "2025-05-01T08:00:00Z",
    "location": {"displayName": "Raum 42"},
    "body": {"contentType": "html", "content": "<p>Agenda &amp; Themen</p>"},
    "organizer": {"emailAddress": {"name": "Alice Example", "address": "alice@example.com"}},
    "attendees": [{"emailAddress": {"name": 'Bob "Builder"', "address": "bob@example.com"}}],
    "showAs": "busy",
}


def test_build_ics_vollstaendiger_termin():
    ics = outlook_export.build_ics(EVENT)
    assert ics.startswith("BEGIN:VCALENDAR\r\n")
    assert ics.endswith("END:VCALENDAR\r\n")
    lines = _unfold(ics)
    assert "UID:uid-1" in lines
    assert "DTSTAMP:20250501T080000Z" in lines
    assert "DTSTART:20250601T120000Z" in lines
    assert "DTEND:20250601T130000Z" in lines
    assert "SUMMARY:Planung\\; Q3" in lines
    assert "LOCATION:Raum 42" in lines
    assert "DESCRIPTION:Agenda & Themen" in lines
    assert 'ORGANIZER;CN="Alice Example":mailto:alice@example.com' in lines
    assert "ATTENDEE;CN=\"Bob 'Builder'\":mailto:bob@example.com" in lines
    assert "STATUS:CONFIRMED" in lines
    assert "TRANSP:OPAQUE" in lines


def test_build_ics_ganztaegig_abgesagt_und_frei():
    ev = {"id": "ev2", "subject": "Feiertag", "isAllDay": True, "isCancelled": True,
          "showAs": "free",
          "start": {"dateTime": "2025-06-01T00:00:00.0000000"},
          "end": {"dateTime": "2025-06-02T00:00:00.0000000"},
          "lastModifiedDateTime": "2025-05-01T08:00:00Z",
          "recurrence": {"pattern": {"type": "daily"}, "range": {}}}
    lines = _unfold(outlook_export.build_ics(ev))
    assert "DTSTART;VALUE=DATE:20250601" in lines
    assert "DTEND;VALUE=DATE:20250602" in lines
    assert "STATUS:CANCELLED" in lines
    assert "TRANSP:TRANSPARENT" in lines
    assert "RRULE:FREQ=DAILY" in lines
    # UID fällt auf die Ereignis-ID zurück
    assert "UID:ev2" in lines


def test_build_ics_tentative_und_faltung():
    ev = dict(EVENT)
    ev["showAs"] = "tentative"
    ev["body"] = {"contentType": "text", "content": "Ä" * 200}
    ics = outlook_export.build_ics(ev)
    lines = _unfold(ics)
    assert "STATUS:TENTATIVE" in lines
    assert "DESCRIPTION:" + "Ä" * 200 in lines
    # Alle physischen Zeilen unter der 75-Oktett-Grenze
    for part in ics.split("\r\n"):
        assert len(part.encode("utf-8")) <= 75


def test_event_filename():
    name = outlook_export.event_filename(EVENT)
    assert name == f"2025-06-01_1200__Planung; Q3__{outlook_export.short_id('ev1')}.ics"
    ganz = {"id": "e", "subject": "Tag", "isAllDay": True,
            "start": {"dateTime": "2025-06-01T00:00:00.0000000"}}
    assert outlook_export.event_filename(ganz).startswith("2025-06-01__Tag__")
    leer = outlook_export.event_filename({})
    assert leer.startswith("(kein Betreff)__") and leer.endswith(".ics")


# --------------------------------------------------------------------------
# VCF-Erzeugung (Kontakte)
# --------------------------------------------------------------------------
CONTACT = {
    "id": "c1", "displayName": "Alice Example", "givenName": "Alice",
    "surname": "Example", "middleName": "M",
    "companyName": "Firma GmbH", "department": "Entwicklung", "jobTitle": "Engineer",
    "emailAddresses": [{"address": "alice@example.com"}, {"name": "ohne Adresse"}],
    "businessPhones": ["+49 30 1", ""], "homePhones": ["+49 30 2"],
    "mobilePhone": "+49 170 3", "personalNotes": "Zeile1\nZeile2",
}


def test_build_vcf_vollstaendiger_kontakt():
    vcf = outlook_export.build_vcf(CONTACT)
    assert vcf.startswith("BEGIN:VCARD\r\nVERSION:3.0\r\n")
    assert vcf.endswith("END:VCARD\r\n")
    lines = _unfold(vcf)
    assert "N:Example;Alice;M;;" in lines
    assert "FN:Alice Example" in lines
    assert "ORG:Firma GmbH;Entwicklung" in lines
    assert "TITLE:Engineer" in lines
    assert "EMAIL;TYPE=INTERNET:alice@example.com" in lines
    assert "TEL;TYPE=WORK,VOICE:+49 30 1" in lines
    assert "TEL;TYPE=HOME,VOICE:+49 30 2" in lines
    assert "TEL;TYPE=CELL,VOICE:+49 170 3" in lines
    assert "NOTE:Zeile1\\nZeile2" in lines
    assert "UID:c1" in lines
    # Leere Telefonnummern und E-Mail-Einträge ohne Adresse werden übergangen
    assert sum(1 for x in lines if x.startswith("EMAIL")) == 1
    assert sum(1 for x in lines if x.startswith("TEL;TYPE=WORK")) == 1


def test_build_vcf_minimal_ohne_namen():
    lines = _unfold(outlook_export.build_vcf({}))
    assert "N:;;;;" in lines
    assert "FN:(ohne Namen)" in lines


def test_contact_filename_varianten():
    assert outlook_export.contact_filename(CONTACT) == \
        f"Alice Example__{outlook_export.short_id('c1')}.vcf"
    nur_namen = {"givenName": "Erika", "surname": "Muster"}
    assert outlook_export.contact_filename(nur_namen).startswith("Erika Muster__")
    assert outlook_export.contact_filename({}).startswith("Kontakt__")


# --------------------------------------------------------------------------
# DoneLog (Resume-Datei)
# --------------------------------------------------------------------------
def test_donelog_roundtrip_und_is_done(tmp_path):
    p = tmp_path / "exported.tsv"
    log = outlook_export.DoneLog(p)
    assert not log.is_done(tmp_path, "m1")
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.eml").write_bytes(b"x")
    log.mark("m1", "a/x.eml")
    log.mark("m2", "a/fehlt.eml")
    log.close()

    log2 = outlook_export.DoneLog(p)
    assert log2.done == {"m1": "a/x.eml", "m2": "a/fehlt.eml"}
    assert log2.is_done(tmp_path, "m1")
    assert not log2.is_done(tmp_path, "m2")   # Zieldatei existiert nicht
    assert not log2.is_done(tmp_path, "m3")   # unbekannte ID
    log2.close()


def test_donelog_remap_schreibt_neu_und_bleibt_beschreibbar(tmp_path):
    p = tmp_path / "exported.tsv"
    log = outlook_export.DoneLog(p)
    log.mark("m1", "Alt/x.eml")
    log.remap(lambda rel: f"E-Mail/{rel}")
    log.mark("m2", "E-Mail/y.eml")   # Anhängen nach remap funktioniert weiter
    log.close()
    log2 = outlook_export.DoneLog(p)
    assert log2.done == {"m1": "E-Mail/Alt/x.eml", "m2": "E-Mail/y.eml"}
    log2.close()


def test_donelog_paralleles_markieren(tmp_path):
    log = outlook_export.DoneLog(tmp_path / "exported.tsv")
    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(lambda i: log.mark(f"m{i}", f"a/{i}.eml"), range(100)))
    log.close()
    log2 = outlook_export.DoneLog(tmp_path / "exported.tsv")
    assert len(log2.done) == 100
    assert log2.done["m42"] == "a/42.eml"
    log2.close()


# --------------------------------------------------------------------------
# TokenClient: Retry, Drosselung, Paging, TokenExpired
# --------------------------------------------------------------------------
def test_tokenclient_get_sendet_bearer_und_params(monkeypatch):
    fake = FakeSession([FakeResponse(payload={"value": [1]})])
    monkeypatch.setattr(outlook_export, "SESSION", fake)
    tc = outlook_export.TokenClient("tok123")
    assert tc.get("https://example.invalid/x", {"$top": 5},
                  extra_headers={"Prefer": "utc"}) == {"value": [1]}
    call = fake.calls[0]
    assert call["headers"]["Authorization"] == "Bearer tok123"
    assert call["headers"]["Prefer"] == "utc"
    assert call["params"] == {"$top": 5}


def test_tokenclient_get_wiederholt_nach_429(monkeypatch):
    fake = FakeSession([FakeResponse(429, headers={"Retry-After": "0"}),
                        FakeResponse(payload={"ok": 1})])
    monkeypatch.setattr(outlook_export, "SESSION", fake)
    tc = outlook_export.TokenClient("t")
    assert tc.get("https://example.invalid/x") == {"ok": 1}
    assert len(fake.calls) == 2


def test_tokenclient_401_wirft_tokenexpired(monkeypatch):
    monkeypatch.setattr(outlook_export, "SESSION", FakeSession([FakeResponse(401)]))
    tc = outlook_export.TokenClient("t")
    with pytest.raises(outlook_export.TokenExpired):
        tc.get("https://example.invalid/x")
    monkeypatch.setattr(outlook_export, "SESSION", FakeSession([FakeResponse(401)]))
    with pytest.raises(outlook_export.TokenExpired):
        tc.get_bytes("https://example.invalid/x")


def test_tokenclient_bricht_nach_sechs_serverfehlern_ab(monkeypatch):
    fake = FakeSession([FakeResponse(500) for _ in range(6)])
    monkeypatch.setattr(outlook_export, "SESSION", fake)
    sleeps = []
    monkeypatch.setattr(outlook_export.time, "sleep", sleeps.append)
    tc = outlook_export.TokenClient("t")
    with pytest.raises(RuntimeError, match="Zu viele Fehlversuche"):
        tc.get("https://example.invalid/x")
    assert len(fake.calls) == 6
    assert sleeps == [1, 2, 4, 8, 16, 32]   # exponentielles Backoff, Kappe 60


def test_tokenclient_4xx_wirft_httperror(monkeypatch):
    monkeypatch.setattr(outlook_export, "SESSION", FakeSession([FakeResponse(404)]))
    tc = outlook_export.TokenClient("t")
    with pytest.raises(requests.HTTPError):
        tc.get("https://example.invalid/x")


def test_tokenclient_get_bytes_liefert_inhalt_und_contenttype(monkeypatch):
    fake = FakeSession([FakeResponse(content=b"MIME",
                                     headers={"Content-Type": "message/rfc822"})])
    monkeypatch.setattr(outlook_export, "SESSION", fake)
    tc = outlook_export.TokenClient("t")
    assert tc.get_bytes("https://example.invalid/m") == (b"MIME", "message/rfc822")


def test_tokenclient_paged_folgt_nextlink(monkeypatch):
    fake = FakeSession([
        FakeResponse(payload={"value": [1, 2],
                              "@odata.nextLink": "https://example.invalid/p2"}),
        FakeResponse(payload={"value": [3]}),
    ])
    monkeypatch.setattr(outlook_export, "SESSION", fake)
    tc = outlook_export.TokenClient("t")
    assert list(tc.paged("https://example.invalid/p1", {"$top": 2})) == [1, 2, 3]
    # Folgeseite ohne die ursprünglichen Params (nextLink enthält sie bereits)
    assert fake.calls[1]["url"] == "https://example.invalid/p2"
    assert fake.calls[1]["params"] is None


# --------------------------------------------------------------------------
# Netzwerkfehler: Timeout/Verbindungsabbruch werden wiederholt statt zu beenden
# --------------------------------------------------------------------------
def _timeout():
    return requests.exceptions.ReadTimeout("read timed out")


def test_fetch_wiederholt_nach_netzwerkfehler(monkeypatch):
    fake = FakeSession([_timeout(), _timeout(), FakeResponse(payload={"ok": 1})])
    monkeypatch.setattr(outlook_export, "SESSION", fake)
    sleeps = []
    monkeypatch.setattr(outlook_export.time, "sleep", sleeps.append)
    assert outlook_export.TokenClient("t").get("https://example.invalid/x") == {"ok": 1}
    assert len(fake.calls) == 3
    assert sleeps == [1, 2]


def test_fetch_gibt_nach_allen_netzwerkversuchen_auf(monkeypatch):
    fake = FakeSession([_timeout() for _ in range(outlook_export.NET_RETRIES)])
    monkeypatch.setattr(outlook_export, "SESSION", fake)
    monkeypatch.setattr(outlook_export.time, "sleep", lambda s: None)
    with pytest.raises(requests.exceptions.ReadTimeout):
        outlook_export.TokenClient("t").get("https://example.invalid/x")
    assert len(fake.calls) == outlook_export.NET_RETRIES


def test_netzwerkfehler_verbraucht_keinen_http_versuch(monkeypatch):
    """Ein Aussetzer darf die Versuche für 429/5xx nicht aufbrauchen."""
    fake = FakeSession([_timeout()]
                       + [FakeResponse(500) for _ in range(outlook_export.HTTP_RETRIES - 1)]
                       + [FakeResponse(payload={"ok": 1})])
    monkeypatch.setattr(outlook_export, "SESSION", fake)
    monkeypatch.setattr(outlook_export.time, "sleep", lambda s: None)
    assert outlook_export.TokenClient("t").get("https://example.invalid/x") == {"ok": 1}


def test_get_bytes_wiederholt_nach_netzwerkfehler(monkeypatch):
    fake = FakeSession([_timeout(), FakeResponse(content=b"MIME")])
    monkeypatch.setattr(outlook_export, "SESSION", fake)
    monkeypatch.setattr(outlook_export.time, "sleep", lambda s: None)
    content, _ = outlook_export.TokenClient("t").get_bytes("https://example.invalid/m")
    assert content == b"MIME"
    assert fake.calls[0]["timeout"] == outlook_export.TIMEOUT_BYTES


# --------------------------------------------------------------------------
# Graph-Client: Token-Erneuerung bei 401 (ohne echte Anmeldung)
# --------------------------------------------------------------------------
class _StubAnmeldung:
    """Nur was die HTTP-Schicht von auth.Login braucht. Die Anmeldung selbst
    hat eigene Tests (test_auth.py); hier geht es um Retry und Paging."""

    def __init__(self, token="alt"):
        self.token = token

    def headers(self):
        return {"Authorization": f"Bearer {self.token}"}


def _bare_graph():
    """Graph-Instanz ohne interaktiven Login (kein __init__)."""
    g = object.__new__(outlook_export.Graph)
    g.anmeldung = _StubAnmeldung()
    g._refresh_lock = threading.Lock()
    return g


def test_graph_get_erneuert_token_bei_401(monkeypatch):
    fake = FakeSession([FakeResponse(401), FakeResponse(payload={"ok": True})])
    monkeypatch.setattr(outlook_export, "SESSION", fake)
    g = _bare_graph()
    aufrufe = []

    def refresh():
        aufrufe.append(1)
        g.anmeldung.token = "neu"
    g._refresh = refresh

    assert g.get("https://example.invalid/x", extra_headers={"Prefer": "utc"}) == {"ok": True}
    assert aufrufe == [1]
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer alt"
    assert fake.calls[1]["headers"]["Authorization"] == "Bearer neu"
    assert fake.calls[1]["headers"]["Prefer"] == "utc"


def test_graph_get_bytes_erneuert_token_bei_401(monkeypatch):
    fake = FakeSession([FakeResponse(401), FakeResponse(content=b"X")])
    monkeypatch.setattr(outlook_export, "SESSION", fake)
    g = _bare_graph()
    g._refresh = lambda: setattr(g.anmeldung, "token", "neu")
    content, _ = g.get_bytes("https://example.invalid/m")
    assert content == b"X"
    assert fake.calls[1]["headers"]["Authorization"] == "Bearer neu"


# --------------------------------------------------------------------------
# Ordnerbaum und Mail-Iteration (mit Fake-Graph)
# --------------------------------------------------------------------------
class FakeTreeGraph:
    """Stellt paged() für /me/mailFolders und childFolders aus Testdaten bereit."""

    def __init__(self, roots, children):
        self.roots = roots
        self.children = children   # dict: folder-id -> Kinderliste

    def paged(self, url, params=None, extra_headers=None):
        if url.endswith("/me/mailFolders"):
            yield from self.roots
        else:
            fid = url.rsplit("/mailFolders/", 1)[1].split("/", 1)[0]
            yield from self.children.get(fid, [])


def test_build_tree_erfasst_teilbaeume_und_zaehlt_elemente():
    roots = [{"id": "r1", "displayName": "Posteingang", "totalItemCount": 5},
             {"id": "r2", "displayName": "Projekte", "totalItemCount": 1}]
    children = {"r1": [{"id": "c1", "displayName": "Sub/Ordner", "totalItemCount": 2}],
                "c1": [{"id": "g1", "displayName": "Tief", "totalItemCount": 1}]}
    tops = outlook_export.build_tree(FakeTreeGraph(roots, children))
    assert len(tops) == 2
    t = tops[0]
    assert t["rel"] == "E-Mail/Posteingang"
    assert t["nfolders"] == 3
    assert t["items"] == 8   # 5 + 2 + 1 rekursiv
    rels = [rel for _, rel in t["subtree"]]
    # Unsichere Zeichen im Ordnernamen werden ersetzt
    assert rels == ["E-Mail/Posteingang", "E-Mail/Posteingang/Sub_Ordner",
                    "E-Mail/Posteingang/Sub_Ordner/Tief"]
    assert tops[1]["nfolders"] == 1 and tops[1]["items"] == 1


def test_list_children_faengt_fehler_ab_und_reicht_tokenexpired_durch():
    class Kaputt:
        def paged(self, url, params=None, extra_headers=None):
            raise ValueError("kaputt")

    class Abgelaufen:
        def paged(self, url, params=None, extra_headers=None):
            raise outlook_export.TokenExpired()

    folder = {"id": "f1", "displayName": "X"}
    assert outlook_export.list_children(Kaputt(), folder) == []
    with pytest.raises(outlook_export.TokenExpired):
        outlook_export.list_children(Abgelaufen(), folder)


class FakeMsgGraph:
    """paged() liefert vorbereitete Nachrichtenlisten für Mail-Ordner."""

    def __init__(self, msgs):
        self.msgs = msgs

    def paged(self, url, params=None, extra_headers=None):
        yield from self.msgs


def test_iter_messages_ueberspringt_erledigte_und_legt_ordner_an(tmp_path):
    folder = {"id": "f1", "displayName": "Posteingang", "totalItemCount": 2}
    selected = [{"subtree": [(folder, "E-Mail/Posteingang")]}]
    m1 = {"id": "m1", "subject": "alt"}
    m2 = {"id": "m2", "subject": "neu"}
    done = outlook_export.DoneLog(tmp_path / "exported.tsv")
    (tmp_path / "E-Mail" / "Posteingang").mkdir(parents=True)
    (tmp_path / "E-Mail" / "Posteingang" / "alt.eml").write_bytes(b"x")
    done.mark("m1", "E-Mail/Posteingang/alt.eml")
    stats = {"new": 0, "skipped": 0}

    got = list(outlook_export.iter_messages_to_export(
        FakeMsgGraph([m1, m2]), tmp_path, done, stats, selected))
    done.close()
    assert stats["skipped"] == 1
    assert len(got) == 1
    mid, rel = got[0]
    assert mid == "m2"
    assert rel == "E-Mail/Posteingang/" + outlook_export.mail_filename(m2)
    assert (tmp_path / "E-Mail" / "Posteingang").is_dir()


def test_iter_messages_ueberspringt_ordner_mit_netzwerkfehler(tmp_path):
    """Ein nicht listbarer Ordner beendet nicht den ganzen Lauf."""
    kaputt = {"id": "f1", "displayName": "Ablage"}
    heil = {"id": "f2", "displayName": "Posteingang"}
    selected = [{"subtree": [(kaputt, "E-Mail/Ablage"), (heil, "E-Mail/Posteingang")]}]

    class HalbKaputt:
        def paged(self, url, params=None, extra_headers=None):
            if "f1" in url:
                yield {"id": "m1", "subject": "geht noch"}
                raise requests.exceptions.ReadTimeout("read timed out")
            yield {"id": "m2", "subject": "danach"}

    done = outlook_export.DoneLog(tmp_path / "exported.tsv")
    stats = {"new": 0, "skipped": 0}
    got = list(outlook_export.iter_messages_to_export(
        HalbKaputt(), tmp_path, done, stats, selected))
    done.close()
    assert [mid for mid, _ in got] == ["m1", "m2"]   # danach wird weitergemacht
    assert stats["folder_errors"] == 1


def test_iter_messages_reicht_tokenexpired_durch(tmp_path):
    folder = {"id": "f1", "displayName": "Posteingang"}
    selected = [{"subtree": [(folder, "E-Mail/Posteingang")]}]

    class Abgelaufen:
        def paged(self, url, params=None, extra_headers=None):
            raise outlook_export.TokenExpired()
            yield   # pragma: no cover – macht paged zum Generator

    done = outlook_export.DoneLog(tmp_path / "exported.tsv")
    with pytest.raises(outlook_export.TokenExpired):
        list(outlook_export.iter_messages_to_export(
            Abgelaufen(), tmp_path, done, {"new": 0, "skipped": 0}, selected))
    done.close()


# --------------------------------------------------------------------------
# Download-Worker und paralleler Treiber
# --------------------------------------------------------------------------
class FakeExportGraph:
    """paged() liefert Mails, get_bytes() den MIME-Inhalt (oder TokenExpired)."""

    def __init__(self, msgs, fail=False):
        self.msgs = msgs
        self.fail = fail

    def paged(self, url, params=None, extra_headers=None):
        yield from self.msgs

    def get_bytes(self, url):
        if self.fail:
            raise outlook_export.TokenExpired()
        return b"MIME " + url.encode(), "message/rfc822"


def test_download_one_schreibt_datei_und_markiert(tmp_path):
    done = outlook_export.DoneLog(tmp_path / "exported.tsv")
    (tmp_path / "E-Mail").mkdir()
    status, rel = outlook_export.download_one(
        FakeExportGraph([]), tmp_path, done, "m1", "E-Mail/a.eml")
    assert status == "ok" and rel == "E-Mail/a.eml"
    assert (tmp_path / "E-Mail" / "a.eml").read_bytes().startswith(b"MIME ")
    assert done.is_done(tmp_path, "m1")
    done.close()


def test_download_one_meldet_expired_stopped_und_fehler(tmp_path):
    done = outlook_export.DoneLog(tmp_path / "exported.tsv")
    # Token abgelaufen
    status, info = outlook_export.download_one(
        FakeExportGraph([], fail=True), tmp_path, done, "m1", "E-Mail/a.eml")
    assert (status, info) == ("expired", "m1")
    # Schreibfehler (Zielordner fehlt) -> "error", nichts markiert
    status, info = outlook_export.download_one(
        FakeExportGraph([]), tmp_path, done, "m2", "fehlt/tief/a.eml")
    assert status == "error"
    assert not done.is_done(tmp_path, "m2")
    # STOP gesetzt -> gar nichts tun
    outlook_export.STOP.set()
    status, info = outlook_export.download_one(
        FakeExportGraph([]), tmp_path, done, "m3", "E-Mail/b.eml")
    assert (status, info) == ("stopped", "m3")
    done.close()


def test_run_export_laedt_alle_neuen_mails(tmp_path):
    folder = {"id": "f1", "displayName": "Inbox", "totalItemCount": 3}
    msgs = [{"id": f"m{i}", "subject": f"Mail {i}"} for i in range(3)]
    selected = [{"subtree": [(folder, "E-Mail/Inbox")]}]
    done = outlook_export.DoneLog(tmp_path / "exported.tsv")
    stats = {"new": 0, "skipped": 0}
    result = outlook_export.run_export(
        FakeExportGraph(msgs), tmp_path, done, stats, selected, workers=2)
    done.close()
    assert result == "done"
    assert stats == {"new": 3, "skipped": 0}
    eml = list((tmp_path / "E-Mail" / "Inbox").glob("*.eml"))
    assert len(eml) == 3


def test_run_export_meldet_expired_und_setzt_stop(tmp_path):
    folder = {"id": "f1", "displayName": "Inbox", "totalItemCount": 1}
    selected = [{"subtree": [(folder, "E-Mail/Inbox")]}]
    done = outlook_export.DoneLog(tmp_path / "exported.tsv")
    stats = {"new": 0, "skipped": 0}
    result = outlook_export.run_export(
        FakeExportGraph([{"id": "m1", "subject": "x"}], fail=True),
        tmp_path, done, stats, selected, workers=2)
    done.close()
    assert result == "expired"
    assert outlook_export.STOP.is_set()
    assert stats["new"] == 0


# --------------------------------------------------------------------------
# Kalender- und Kontakte-Export (kompletter Ablauf mit Fake-Graph)
# --------------------------------------------------------------------------
def test_export_calendar_schreibt_ics_und_markiert(tmp_path):
    class KalGraph:
        def paged(self, url, params=None, extra_headers=None):
            assert "/me/calendars/cal1/events" in url
            # Zeiten müssen per Prefer-Header in UTC angefordert werden
            assert extra_headers == {"Prefer": 'outlook.timezone="UTC"'}
            yield EVENT

    done = outlook_export.DoneLog(tmp_path / "exported.tsv")
    stats = {"new": 0, "skipped": 0}
    outlook_export.export_calendar(KalGraph(), tmp_path, done, stats,
                                   [{"id": "cal1", "name": "Arbeit"}])
    assert stats["new"] == 1
    files = list((tmp_path / "kalender" / "Arbeit").glob("*.ics"))
    assert len(files) == 1
    assert "SUMMARY:Planung\\; Q3" in files[0].read_text(encoding="utf-8")
    assert done.is_done(tmp_path, "ev1")
    # Zweiter Lauf überspringt den Termin
    outlook_export.export_calendar(KalGraph(), tmp_path, done, stats,
                                   [{"id": "cal1", "name": "Arbeit"}])
    assert stats == {"new": 1, "skipped": 1}
    done.close()


def test_export_contacts_standard_und_ordner(tmp_path):
    class KonGraph:
        def paged(self, url, params=None, extra_headers=None):
            if url.endswith("/me/contactFolders"):
                yield {"id": "cf1", "displayName": "Team"}
            elif url.endswith("/me/contacts"):
                yield CONTACT
            elif "/contactFolders/cf1/contacts" in url:
                yield {"id": "c2", "displayName": "Bob Builder"}

    done = outlook_export.DoneLog(tmp_path / "exported.tsv")
    stats = {"new": 0, "skipped": 0}
    outlook_export.export_contacts(KonGraph(), tmp_path, done, stats)
    done.close()
    assert stats["new"] == 2
    haupt = list((tmp_path / "kontakte").glob("*.vcf"))
    team = list((tmp_path / "kontakte" / "Team").glob("*.vcf"))
    assert len(haupt) == 1 and len(team) == 1
    assert "FN:Alice Example" in haupt[0].read_text(encoding="utf-8")
    assert "FN:Bob Builder" in team[0].read_text(encoding="utf-8")


def test_list_calendars_sortiert_default_zuerst_und_faengt_fehler():
    class CalGraph:
        def paged(self, url, params=None, extra_headers=None):
            yield from [{"name": "Zeta"}, {"name": "Arbeit", "isDefaultCalendar": True},
                        {"name": "beta"}]

    cals = outlook_export.list_calendars(CalGraph())
    assert [c["name"] for c in cals] == ["Arbeit", "beta", "Zeta"]

    class Kaputt:
        def paged(self, url, params=None, extra_headers=None):
            raise ValueError("keine Berechtigung")

    assert outlook_export.list_calendars(Kaputt()) == []


# --------------------------------------------------------------------------
# Migration der Alt-Struktur nach E-Mail/
# --------------------------------------------------------------------------
def test_migrate_verschiebt_ordner_und_remappt_pfade(tmp_path):
    (tmp_path / "Posteingang").mkdir()
    (tmp_path / "Posteingang" / "m.eml").write_bytes(b"x")
    (tmp_path / "kalender").mkdir()
    done = outlook_export.DoneLog(tmp_path / "exported.tsv")
    done.mark("m1", "Posteingang/m.eml")
    done.mark("e1", "kalender/t.ics")

    outlook_export.migrate_to_email_subdir(tmp_path, done)
    assert (tmp_path / "E-Mail" / "Posteingang" / "m.eml").exists()
    assert not (tmp_path / "Posteingang").exists()
    assert done.done["m1"] == "E-Mail/Posteingang/m.eml"
    assert done.done["e1"] == "kalender/t.ics"   # reservierte Pfade unangetastet
    done.close()

    # Persistiert: neue DoneLog-Instanz liest die remappten Pfade
    log2 = outlook_export.DoneLog(tmp_path / "exported.tsv")
    assert log2.done["m1"] == "E-Mail/Posteingang/m.eml"
    log2.close()


def test_migrate_ist_noop_bei_neuer_struktur(tmp_path):
    (tmp_path / "E-Mail").mkdir()
    (tmp_path / "kontakte").mkdir()
    done = outlook_export.DoneLog(tmp_path / "exported.tsv")
    done.mark("m1", "E-Mail/Inbox/a.eml")
    outlook_export.migrate_to_email_subdir(tmp_path, done)
    assert done.done["m1"] == "E-Mail/Inbox/a.eml"
    # Auch ein nicht existierender Ausgabeordner ist kein Fehler
    outlook_export.migrate_to_email_subdir(tmp_path / "gibtsnicht", done)
    done.close()


def test_export_ohne_graph_id_resumt_ueber_dateipfad(tmp_path):
    """Termine/Kontakte ohne id dürfen nicht unter dem Schlüssel None landen
    (sonst greift Resume nie und sie werden bei jedem Lauf neu exportiert)."""
    ev = {k: v for k, v in EVENT.items() if k not in ("id", "iCalUId")}

    class KalGraph:
        def paged(self, url, params=None, extra_headers=None):
            yield ev

    done = outlook_export.DoneLog(tmp_path / "exported.tsv")
    stats = {"new": 0, "skipped": 0}
    for _ in range(2):
        outlook_export.export_calendar(KalGraph(), tmp_path, done, stats,
                                       [{"id": "cal1", "name": "Arbeit"}])
    assert stats == {"new": 1, "skipped": 1}
    assert len(list((tmp_path / "kalender" / "Arbeit").glob("*.ics"))) == 1

    kontakt = {"displayName": "Ohne Id"}

    class KonGraph:
        def paged(self, url, params=None, extra_headers=None):
            if url.endswith("/me/contacts"):
                yield kontakt

    stats = {"new": 0, "skipped": 0}
    for _ in range(2):
        outlook_export.export_contacts(KonGraph(), tmp_path, done, stats)
    done.close()
    assert stats == {"new": 1, "skipped": 1}
    assert len(list((tmp_path / "kontakte").glob("*.vcf"))) == 1
    # Der Schlüssel None darf nie im Log stehen
    log = (tmp_path / "exported.tsv").read_text(encoding="utf-8")
    assert not any(z.startswith("None\t") for z in log.splitlines())


def test_default_skip_folders_stammt_aus_der_eingebauten_liste():
    """Ohne SKIP_FOLDERS und ohne app_config.json gilt die Liste im Skript."""
    assert outlook_export.DEFAULT_SKIP_FOLDERS == outlook_export.BUILTIN_SKIP_FOLDERS


# --------------------------------------------------------------------------
# Verschwundene Mails erkennen
#
# Die gefährliche Stelle ist die Verwechslung von gelöscht und verschoben – und
# ein abgebrochenes Listing, das den halben Ordner für gelöscht erklärt.
# --------------------------------------------------------------------------
class _Done:
    def __init__(self, eintraege):
        self.done = dict(eintraege)


def _bestand(gesehen, ordner):
    b = outlook_export.Bestand()
    b.gesehen = set(gesehen)
    for o in ordner:
        b.ordner_fertig(o)
    return b


def test_verdaechtig_ist_was_fehlt():
    done = _Done({"m1": "E-Mail/Posteingang/a.eml", "m2": "E-Mail/Posteingang/b.eml"})
    b = _bestand({"m1"}, ["E-Mail/Posteingang"])
    assert outlook_export.verdaechtige(done, b) == [("m2", "E-Mail/Posteingang/b.eml")]


def test_nicht_gelistete_ordner_bleiben_unangetastet():
    """Wer nur Kontakte exportiert, hat keine Aussage über den Posteingang."""
    done = _Done({"m1": "E-Mail/Archiv/alt.eml"})
    b = _bestand(set(), ["E-Mail/Posteingang"])
    assert outlook_export.verdaechtige(done, b) == []


def test_umzug_zwischen_gelisteten_ordnern_ist_keine_loeschung():
    done = _Done({"m1": "E-Mail/Posteingang/a.eml"})
    b = _bestand({"m1"}, ["E-Mail/Posteingang", "E-Mail/Projekte"])
    assert outlook_export.verdaechtige(done, b) == []


class _FakeGraph:
    """get() wirft für gelöschte IDs einen 404, liefert sonst etwas."""

    def __init__(self, weg=(), fehler=()):
        self.weg, self.fehler = set(weg), set(fehler)
        self.gefragt = []

    def get(self, url, params=None):
        mid = url.rsplit("/", 1)[-1]
        self.gefragt.append(mid)
        if mid in self.weg:
            raise RuntimeError("HTTP 404 Not Found")
        if mid in self.fehler:
            raise RuntimeError("HTTP 429 zu viele Anfragen")
        return {"id": mid}


def test_nur_ein_404_zaehlt_als_geloescht():
    g = _FakeGraph(weg={"m2"})
    weg, verschoben = outlook_export.wirklich_weg(
        g, [("m1", "a.eml"), ("m2", "b.eml")])
    assert weg == ["b.eml"] and verschoben == 1


def test_unklares_gilt_als_nicht_geloescht():
    """Drosselung oder Netzfehler: lieber eine Löschung später melden als eine
    falsche jetzt."""
    g = _FakeGraph(fehler={"m1"})
    weg, verschoben = outlook_export.wirklich_weg(g, [("m1", "a.eml")])
    assert weg == [] and verschoben == 0


def test_grenze_bremst_die_nachfragen(capsys):
    g = _FakeGraph(weg={f"m{i}" for i in range(10)})
    weg, _ = outlook_export.wirklich_weg(
        g, [(f"m{i}", f"{i}.eml") for i in range(10)], grenze=3)
    assert len(g.gefragt) == 3 and len(weg) == 3
    assert "7 weitere" in capsys.readouterr().out


def test_verschwunden_datei_behaelt_den_ersten_zeitpunkt(tmp_path):
    """Sonst wanderte das Datum bei jedem Lauf nach vorn und die Angabe
    „seit wann“ wäre wertlos."""
    pfad = tmp_path / "verschwunden.tsv"
    outlook_export.schreibe_verschwunden(pfad, {}, ["a.eml"], "2026-01-01T10:00:00")
    zusammen = outlook_export.schreibe_verschwunden(
        pfad, outlook_export.lies_verschwunden(pfad), ["a.eml", "b.eml"],
        "2026-06-01T10:00:00")
    assert zusammen["a.eml"] == "2026-01-01T10:00:00"
    assert zusammen["b.eml"] == "2026-06-01T10:00:00"
    assert not pfad.with_name(pfad.name + ".tmp").exists()


def test_verschwunden_datei_ueberlebt_fehlen(tmp_path):
    assert outlook_export.lies_verschwunden(tmp_path / "gibtsnicht.tsv") == {}


class _ListenGraph:
    """paged() liefert Mails und bricht optional mittendrin ab."""

    def __init__(self, mails, abbruch_nach=None):
        self.mails, self.abbruch_nach = mails, abbruch_nach

    def paged(self, url, params=None):
        for i, m in enumerate(self.mails):
            if self.abbruch_nach is not None and i == self.abbruch_nach:
                raise RuntimeError("Ordner haengt")
            yield m


def _lauf(graph, tmp_path):
    """Einen Ordner listen lassen und den Bestand zurückgeben."""
    bestand = outlook_export.Bestand()
    done = outlook_export.DoneLog(tmp_path / "exported.tsv")
    stats = {"new": 0, "skipped": 0, "folder_errors": 0}
    selected = [{"subtree": [({"id": "f1", "totalItemCount": 3}, "E-Mail/Posteingang")]}]
    list(outlook_export.iter_messages_to_export(
        graph, tmp_path, done, stats, selected, bestand))
    return bestand, stats


def test_abgebrochenes_listing_taugt_nicht_zum_vergleich(tmp_path):
    """DER gefährliche Fall: ein Ordner, der nach der Hälfte hängen bleibt.
    Würde er als vollständig gelten, erklärte der nächste Schritt die andere
    Hälfte für gelöscht."""
    mails = [{"id": f"m{i}", "subject": "X", "receivedDateTime": "2025-06-01T10:00:00Z"}
             for i in range(4)]
    bestand, stats = _lauf(_ListenGraph(mails, abbruch_nach=2), tmp_path)
    assert stats["folder_errors"] == 1
    assert bestand.vollstaendig == [], "abgebrochener Ordner gilt als vollständig"
    # Und damit ist auch nichts verdächtig, obwohl zwei IDs ungesehen blieben.
    done = _Done({"m3": "E-Mail/Posteingang/x.eml"})
    assert outlook_export.verdaechtige(done, bestand) == []


def test_vollstaendiges_listing_taugt_zum_vergleich(tmp_path):
    mails = [{"id": f"m{i}", "subject": "X", "receivedDateTime": "2025-06-01T10:00:00Z"}
             for i in range(3)]
    bestand, stats = _lauf(_ListenGraph(mails), tmp_path)
    assert stats["folder_errors"] == 0
    assert bestand.vollstaendig == ["E-Mail/Posteingang/"]
    assert bestand.gesehen == {"m0", "m1", "m2"}


# --------------------------------------------------------------------------
# Vollständigkeit: Graph zählt, die Platte auch
# --------------------------------------------------------------------------
class _BaumGraph:
    """paged() liefert Ordner mit totalItemCount."""

    def __init__(self, ordner):
        self.ordner = ordner

    def paged(self, url, params=None):
        if "mailFolders" in url and "childFolders" not in url:
            yield from self.ordner
        return


def test_vollstaendigkeit_rechnet_geloeschtes_heraus(tmp_path, monkeypatch):
    """Gelöschtes ist keine Lücke – es liegt ja noch im Archiv, nur nicht mehr
    im Postfach. Ohne diese Verrechnung meldete jede Löschung Fehlalarm."""
    post = tmp_path / "E-Mail" / "Posteingang"
    post.mkdir(parents=True)
    for i in range(8):
        (post / f"m{i}.eml").write_bytes(b"x")

    monkeypatch.setattr(outlook_export, "build_tree", lambda g: [
        {"folder": {"displayName": "Posteingang"},
         "subtree": [({"totalItemCount": 10}, "E-Mail/Posteingang")]}])
    weg = {f"E-Mail/Posteingang/m{i}.eml": "2026-01-01T00:00:00" for i in range(3)}
    b = outlook_export.pruefe_vollstaendigkeit(None, tmp_path, weg)

    zeile = b["ordner"][0]
    assert zeile["erwartet"] == 10 and zeile["vorhanden"] == 8
    assert zeile["geloescht"] == 3
    # 8 auf der Platte, davon 3 geloescht -> 5 zaehlen gegen die 10 -> 5 fehlen
    assert zeile["fehlt"] == 5
    assert b["fehlt"] == 5 and b["geloescht"] == 3


def test_vollstaendigkeit_ohne_luecke(tmp_path, monkeypatch):
    post = tmp_path / "E-Mail" / "Posteingang"
    post.mkdir(parents=True)
    for i in range(10):
        (post / f"m{i}.eml").write_bytes(b"x")
    monkeypatch.setattr(outlook_export, "build_tree", lambda g: [
        {"folder": {"displayName": "Posteingang"},
         "subtree": [({"totalItemCount": 10}, "E-Mail/Posteingang")]}])
    b = outlook_export.pruefe_vollstaendigkeit(None, tmp_path, {})
    assert b["fehlt"] == 0


def test_mehr_da_als_erwartet_ist_keine_luecke(tmp_path, monkeypatch):
    """Kommt vor: eine Mail wurde exportiert und danach im Postfach verschoben.
    Eine negative Zahl wäre keine Auskunft."""
    post = tmp_path / "E-Mail" / "Posteingang"
    post.mkdir(parents=True)
    for i in range(12):
        (post / f"m{i}.eml").write_bytes(b"x")
    monkeypatch.setattr(outlook_export, "build_tree", lambda g: [
        {"folder": {"displayName": "Posteingang"},
         "subtree": [({"totalItemCount": 10}, "E-Mail/Posteingang")]}])
    assert outlook_export.pruefe_vollstaendigkeit(None, tmp_path, {})["fehlt"] == 0


def test_ordner_ohne_zahl_wird_uebergangen(tmp_path, monkeypatch):
    """Manche Ordner liefern kein totalItemCount – dazu lässt sich nichts sagen."""
    monkeypatch.setattr(outlook_export, "build_tree", lambda g: [
        {"folder": {"displayName": "Ohne"},
         "subtree": [({}, "E-Mail/Ohne")]}])
    assert outlook_export.pruefe_vollstaendigkeit(None, tmp_path, {})["ordner"] == []


def test_bericht_wird_atomar_geschrieben(tmp_path):
    ziel = outlook_export.schreibe_bericht(tmp_path, {"fehlt": 0, "ordner": []})
    assert ziel.exists() and not ziel.with_name(ziel.name + ".tmp").exists()
    assert json.loads(ziel.read_text(encoding="utf-8"))["fehlt"] == 0


def test_ausgelassene_ordner_sind_keine_luecke(tmp_path, monkeypatch):
    """Beim ersten echten Lauf meldete die Prüfung 19.649 fehlende Mails im
    Archiv – einem Ordner, den die Standardauswahl absichtlich nie exportiert.
    Ein Bericht, der beim ersten Mal Unsinn zeigt, wird nie wieder aufgemacht.
    """
    monkeypatch.setattr(outlook_export, "DEFAULT_SKIP_FOLDERS", {"archiv"})
    monkeypatch.setattr(outlook_export, "build_tree", lambda g: [
        {"folder": {"displayName": "Archiv"},
         "subtree": [({"totalItemCount": 19649}, "E-Mail/Archiv")]},
        {"folder": {"displayName": "Posteingang"},
         "subtree": [({"totalItemCount": 10}, "E-Mail/Posteingang")]},
    ])
    post = tmp_path / "E-Mail" / "Posteingang"
    post.mkdir(parents=True)
    for i in range(10):
        (post / f"m{i}.eml").write_bytes(b"x")

    b = outlook_export.pruefe_vollstaendigkeit(None, tmp_path, {})
    assert b["fehlt"] == 0, "ausgelassener Ordner wurde als Lücke gezählt"
    assert b["erwartet"] == 10, "ausgelassener Ordner wurde mitgezählt"
    assert b["ausgelassen"] == 19649
    assert b["ausgelassene_ordner"] == ["Archiv"]
    # Sichtbar bleibt er trotzdem – nur eben als Zeile ohne Lücke.
    archiv = [z for z in b["ordner"] if z["ordner"] == "E-Mail/Archiv"][0]
    assert archiv["ausgelassen"] is True and archiv["fehlt"] == 0


# --------------------------------------------------------------------------
# Ordnerbaum: einmal holen, danach von der Platte
# --------------------------------------------------------------------------
def test_baum_eintraege_flach_mit_id_und_zahl(monkeypatch):
    monkeypatch.setattr(outlook_export, "build_tree", lambda g: [
        {"folder": {"displayName": "Posteingang"},
         "subtree": [({"id": "AAA", "displayName": "Posteingang",
                       "totalItemCount": 42}, "E-Mail/Posteingang")]},
    ])
    e = outlook_export.baum_eintraege(None)[0]
    assert e == {"id": "AAA", "pfad": "E-Mail/Posteingang",
                 "name": "Posteingang", "elemente": 42}


def test_export_nimmt_den_puffer_statt_graph(tmp_path, monkeypatch):
    """Der Kern von 3.0: 110 s für 417 Ordner will niemand bei jedem Lauf
    zahlen."""
    import folders
    folders.speichere(tmp_path, [
        {"id": "1", "pfad": "E-Mail/Posteingang", "name": "Posteingang", "elemente": 5},
        {"id": "2", "pfad": "E-Mail/Archiv", "name": "Archiv", "elemente": 99},
    ])
    monkeypatch.setattr(outlook_export, "aktuelle_regeln",
                        lambda: folders.lies_regeln("- E-Mail/Archiv/**"))

    def nie(*a, **kw):
        raise AssertionError("build_tree wurde trotz Puffer aufgerufen")
    monkeypatch.setattr(outlook_export, "build_tree", nie)

    auswahl = outlook_export.waehle_ordner(None, tmp_path)
    pfade = [rel for _, rel in auswahl[0]["subtree"]]
    assert pfade == ["E-Mail/Posteingang"]
    assert auswahl[0]["subtree"][0][0]["id"] == "1"


def test_ohne_puffer_wird_er_einmal_angelegt(tmp_path, monkeypatch):
    import folders
    monkeypatch.setattr(outlook_export, "aktuelle_regeln", list)
    monkeypatch.setattr(outlook_export, "build_tree", lambda g: [
        {"folder": {"displayName": "Posteingang"},
         "subtree": [({"id": "1", "totalItemCount": 5}, "E-Mail/Posteingang")]}])
    auswahl = outlook_export.waehle_ordner(None, tmp_path)
    assert auswahl and folders.lade(tmp_path) is not None


def test_regeln_die_nichts_waehlen_exportieren_nichts(tmp_path, monkeypatch):
    """Lieber gar nichts holen als überraschend alles."""
    import folders
    folders.speichere(tmp_path, [
        {"id": "1", "pfad": "E-Mail/Posteingang", "name": "P", "elemente": 5}])
    monkeypatch.setattr(outlook_export, "aktuelle_regeln",
                        lambda: folders.lies_regeln("- **"))
    assert outlook_export.waehle_ordner(None, tmp_path) == []


def test_alte_namensliste_gilt_ohne_regeln(monkeypatch):
    """Wer aus einer früheren Fassung kommt, behält seine Auswahl."""
    monkeypatch.delenv("FOLDER_RULES", raising=False)
    monkeypatch.setattr(outlook_export.settings, "value", lambda *a, **kw: None)
    monkeypatch.setattr(outlook_export, "DEFAULT_SKIP_FOLDERS", {"archiv"})
    r = outlook_export.aktuelle_regeln()
    assert r == [(False, "E-Mail/archiv/**")]


def test_umgebung_schlaegt_die_datei(monkeypatch):
    monkeypatch.setenv("FOLDER_RULES", "+ E-Mail/Nur/**")
    assert outlook_export.aktuelle_regeln() == [(True, "E-Mail/Nur/**")]
