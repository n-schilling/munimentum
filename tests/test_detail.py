"""detail.py – the facts the hit's detail shows per kind of item (11.2).

Every kind gets what it is known by, read from the index row and, where
the row cannot know it, from the source file; a vanished file leaves the
row's facts standing."""
import json

import detail
import state_db


def zeile(**felder):
    """An index row as detail.fakten reads it – every column present."""
    z = {"uid": "outlook:inbox/a.eml:0", "src": "outlook", "root": "outlook",
         "rel": "inbox/a.eml", "who": "Carla Chef", "who_mail": "carla@example.com",
         "date": "2026-09-15 09:40", "title": "Angebot", "ctx": "inbox",
         "att": "", "domains": "example.com", "thread": None, "gone": None,
         "key": "mail:x", "text": ""}
    z.update(felder)
    return z


MAIL = (
    "Message-ID: <m1@example.com>\nFrom: Carla Chef <carla@example.com>\n"
    "To: alice@example.com, Bob Baumeister <bob@example.com>\nCc: Dana <dana@nordwind.example>\n"
    "Bcc: Erik Einkauf <erik@nordwind.example>\n"
    "Subject: Angebot\nDate: Tue, 15 Sep 2026 09:40:00 +0000\n"
    "MIME-Version: 1.0\nContent-Type: multipart/mixed; boundary=\"xx\"\n\n"
    "--xx\nContent-Type: text/plain; charset=utf-8\n\nHallo, anbei das Angebot.\n"
    "--xx\nContent-Type: application/pdf\nContent-Disposition: attachment; filename=\"Angebot.pdf\"\n"
    "Content-Transfer-Encoding: base64\n\nSGFsbG8gV2VsdA==\n"
    "--xx\nContent-Type: image/png\nContent-Disposition: inline; filename=\"image001.png\"\n"
    "Content-Transfer-Encoding: base64\n\nSGFsbG8=\n--xx--\n")


def test_mail_liest_an_cc_bcc_und_anhaenge_aus_der_datei(tmp_path):
    ziel = tmp_path / "a.eml"
    ziel.write_text(MAIL, encoding="utf-8")
    f = detail.fakten(zeile(), "Hallo, anbei das Angebot.", ziel, {})
    assert f["kind"] == "outlook"
    assert f["from"] == {"name": "Carla Chef", "mail": "carla@example.com"}
    assert f["to"] == [{"name": "", "mail": "alice@example.com"},
                       {"name": "Bob Baumeister", "mail": "bob@example.com"}]
    assert f["cc"] == [{"name": "Dana", "mail": "dana@nordwind.example"}]
    # The blind copy: the search can filter by it, so the detail must show it
    # – otherwise the hit looks as though it had none.
    assert f["bcc"] == [{"name": "Erik Einkauf", "mail": "erik@nordwind.example"}]
    # the real attachment with its size – the inline logo is not one
    assert f["attachments"] == [{"name": "Angebot.pdf", "size": len(b"Hallo Welt")}]
    assert f["folder"] == "inbox" and f["date"] == "2026-09-15 09:40"
    assert f["text"] == "Hallo, anbei das Angebot."


def test_mail_mit_blosser_adresse_behaelt_den_namen_der_zeile(tmp_path):
    ziel = tmp_path / "a.eml"
    ziel.write_text("From: carla@example.com\nSubject: x\n\nText\n", encoding="utf-8")
    f = detail.fakten(zeile(), "Text", ziel, {})
    assert f["from"] == {"name": "Carla Chef", "mail": "carla@example.com"}


def test_mail_ohne_datei_behaelt_die_fakten_der_zeile(tmp_path):
    f = detail.fakten(zeile(att="Vertrag.pdf Anlage.xlsx"), "Text", None, {})
    assert f["from"] == {"name": "Carla Chef", "mail": "carla@example.com"}
    assert f["to"] == [] and f["cc"] == [] and f["bcc"] == []
    assert [a["name"] for a in f["attachments"]] == ["Vertrag.pdf", "Anlage.xlsx"]
    assert all(a["size"] is None for a in f["attachments"])
    # a vanished file is the same case
    f = detail.fakten(zeile(), "Text", tmp_path / "weg.eml", {})
    assert f["from"]["name"] == "Carla Chef" and f["text"] == "Text"


ICS = (
    "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:ev1\nSUMMARY:Budget review\n"
    # floating local times: the display must not depend on the runner's zone
    "DTSTART:20260916T140000\nDTEND:20260916T150000\n"
    "LOCATION:Raum 3.12\nDESCRIPTION:Agenda\\: der Rahmen.\n"
    "ORGANIZER;CN=Alice Beispiel:mailto:alice@example.com\n"
    "ATTENDEE;CN=Bob Baumeister:mailto:bob@example.com\n"
    "ATTENDEE;CN=Dana:mailto:Dana@nordwind.example\nEND:VEVENT\nEND:VCALENDAR\n")


def test_termin_liest_zeit_ort_und_teilnehmer(tmp_path):
    ziel = tmp_path / "a.ics"
    ziel.write_text(ICS, encoding="utf-8")
    z = zeile(uid="kalender:kalender/Arbeit/a.ics:0", src="kalender", rel="kalender/Arbeit/a.ics",
              who="Alice Beispiel", who_mail="alice@example.com", ctx="kalender/Arbeit",
              date="2026-09-16 14:00")
    f = detail.fakten(z, "Ort: Raum 3.12. Agenda: der Rahmen.", ziel, {})
    assert f["kind"] == "kalender" and f["allday"] is False
    assert f["start"].endswith("14:00") and f["end"].endswith("15:00")
    assert f["location"] == "Raum 3.12" and f["calendar"] == "kalender/Arbeit"
    assert f["organiser"] == {"name": "Alice Beispiel", "mail": "alice@example.com"}
    assert f["attendees"] == [{"name": "Bob Baumeister", "mail": "bob@example.com"},
                              {"name": "Dana", "mail": "dana@nordwind.example"}]
    # the description alone – the place is a fact of its own now
    assert f["text"] == "Agenda: der Rahmen."


def test_ganztaegiger_termin_endet_nicht_am_naechsten_tag(tmp_path):
    ziel = tmp_path / "a.ics"
    ziel.write_text("BEGIN:VEVENT\nDTSTART;VALUE=DATE:20260916\nDTEND;VALUE=DATE:20260917\n"
                    "SUMMARY:Feiertag\nEND:VEVENT\n", encoding="utf-8")
    z = zeile(src="kalender", date="2026-09-16")
    f = detail.fakten(z, "", ziel, {})
    assert f["allday"] is True and f["start"] == "2026-09-16" and f["end"] == ""
    ziel.write_text("BEGIN:VEVENT\nDTSTART;VALUE=DATE:20260916\nDTEND;VALUE=DATE:20260918\n"
                    "END:VEVENT\n", encoding="utf-8")
    f = detail.fakten(z, "", ziel, {})
    assert f["end"] == "2026-09-17"


def test_kontakt_liest_die_karte(tmp_path):
    ziel = tmp_path / "d.vcf"
    ziel.write_text("BEGIN:VCARD\nFN:Dana Dienstleister\nORG:Nordwind GmbH;Einkauf\nTITLE:Key Account\n"
                    "EMAIL:dana@nordwind.example\nTEL:+49 30 000000\nNOTE:Ruft lieber nachmittags an.\n"
                    "END:VCARD\n", encoding="utf-8")
    z = zeile(uid="kontakte:kontakte/d.vcf:0", src="kontakte", rel="kontakte/d.vcf",
              who="Nordwind GmbH", who_mail="", ctx="kontakte", date="")
    f = detail.fakten(z, "Nordwind GmbH · Key Account", ziel, {})
    assert f["kind"] == "kontakte"
    assert f["org"] == "Nordwind GmbH · Einkauf" and f["role"] == "Key Account"
    assert f["emails"] == ["dana@nordwind.example"] and f["phones"] == ["+49 30 000000"]
    assert f["note"] == "Ruft lieber nachmittags an." and f["text"] == f["note"]
    assert f["folder"] == "kontakte"


def test_datei_kennt_typ_groesse_und_zeit(tmp_path):
    ziel = tmp_path / "Angebot_2026.xlsx"
    ziel.write_bytes(b"x" * 84)
    z = zeile(uid="datei:Nordwind/Dokumente/Angebot_2026.xlsx:0", src="datei", root="sharepoint",
              rel="Nordwind/Dokumente/Angebot_2026.xlsx", who="", who_mail="",
              ctx="Nordwind/Dokumente", title="Angebot_2026.xlsx")
    f = detail.fakten(z, "Nordwind / Dokumente / Angebot_2026.xlsx", ziel, {})
    assert f["kind"] == "datei" and f["ext"] == "xlsx" and f["size"] == 84
    assert len(f["modified"]) == 16 and f["folder"] == "Nordwind/Dokumente"
    assert "text" not in f                    # nothing to show: contents are not indexed
    # the mirror's file gone: the row's date stands, no size
    f = detail.fakten(z, "", tmp_path / "weg.xlsx", {})
    assert f["size"] is None and f["modified"] == z["date"]


def _board(tmp_path, art, eintraege, extra=None):
    ordner = tmp_path / art / "board-1"
    ordner.mkdir(parents=True)
    db = state_db.StateDb(ordner)
    db.kv_schreiben("tasks", json.dumps(eintraege))
    for k, v in (extra or {}).items():
        db.kv_schreiben(k, json.dumps(v))
    return {art + "_dir": str(tmp_path / art)}


def test_planner_aufgabe_aus_dem_eigenen_datensatz(tmp_path):
    state = _board(tmp_path, "planner", {
        "t1": {"task": {"title": "Angebot vorbereiten", "percentComplete": 50,
                        "dueDateTime": "2026-09-20T00:00:00Z",
                        "assignments": {"u1": {}, "u2": {}}},
               "details": {"description": "Rahmen, Optionen, MSA.",
                           "checklist": {"a": {"isChecked": True}, "b": {"isChecked": False}, "c": {"isChecked": True}},
                           "references": {"r": {"alias": "Angebot 2026.pdf"}}},
               "kommentare": [{"wer": "u2", "wann": "2026-09-14T09:15:00Z", "html": "<p>Optionen sind drin.</p>"}]}},
        {"namen": {"u1": "Alice Beispiel", "u2": "Bob Baumeister"}})
    z = zeile(uid="planner:board-1/t1:0", src="planner", root="planner", rel="board-1/board.html",
              who="Alice Beispiel, Bob Baumeister", who_mail="", ctx="Nordwind board/Angebote",
              att="Angebot_2026.pdf", title="Angebot vorbereiten")
    f = detail.fakten(z, "Rahmen, Optionen, MSA.\nOptionen sind drin.", None, state)
    assert f["kind"] == "planner"
    assert f["plan"] == "Nordwind board" and f["bucket"] == "Angebote"
    assert f["assigned"] == ["Alice Beispiel", "Bob Baumeister"]
    assert f["due"] == "2026-09-20" and f["state"] == "inprogress"
    assert f["checklist"] == {"done": 2, "total": 3}
    assert f["attachments"] == ["Angebot_2026.pdf"]
    assert f["text"] == "Rahmen, Optionen, MSA."
    assert f["comments"] == [{"who": "Bob Baumeister", "when": "2026-09-14T09:15:00Z",
                              "text": "Optionen sind drin."}]


def test_planner_ohne_datensatz_bleibt_bei_der_zeile(tmp_path):
    z = zeile(uid="planner:board-9/t9:0", src="planner", root="planner", rel="board-9/board.html",
              who="Bob Baumeister", ctx="Board/Bucket", att="")
    f = detail.fakten(z, "Beschreibung", None, {"planner_dir": str(tmp_path / "fehlt")})
    assert f["assigned"] == ["Bob Baumeister"] and f["due"] == "" and f["state"] == ""
    assert f["checklist"] is None and f["text"] == "Beschreibung" and f["comments"] == []
    # no directory known at all – the same
    f = detail.fakten(z, "Beschreibung", None, {})
    assert f["plan"] == "Board" and f["bucket"] == "Bucket"


def test_todo_aufgabe_aus_dem_eigenen_datensatz(tmp_path):
    state = _board(tmp_path, "todo", {
        "t1": {"task": {"title": "Dana anrufen", "status": "completed",
                        "dueDateTime": {"dateTime": "2026-09-11T00:00:00.0000000", "timeZone": "UTC"},
                        "completedDateTime": {"dateTime": "2026-09-11T16:40:00.0000000", "timeZone": "UTC"},
                        "body": {"content": "<p>Nur die <i>Optionen</i> sind offen.</p>", "contentType": "html"},
                        "checklistItems": [{"displayName": "a", "isChecked": True}, {"displayName": "b", "isChecked": True}],
                        "linkedResources": [{"displayName": "Re: Angebot", "applicationName": "Outlook"}]},
               "anhaenge": []}})
    z = zeile(uid="todo:board-1/t1:0", src="todo", root="todo", rel="board-1/list.html",
              who="", who_mail="", ctx="Aufgaben", att="", title="Dana anrufen")
    f = detail.fakten(z, "Nur die Optionen sind offen.\na\nb\nRe: Angebot", None, state)
    assert f["kind"] == "todo" and f["list"] == "Aufgaben"
    assert f["due"] == "2026-09-11" and f["state"] == "completed" and f["completed"] == "2026-09-11"
    assert f["steps"] == {"done": 2, "total": 2} and f["linked"] == ["Re: Angebot"]
    assert f["text"] == "Nur die Optionen sind offen."


def test_chat_und_seiten_kommen_aus_der_zeile():
    z = zeile(uid="teams:channels/Nordwind/Angebote__x.html:3", src="teams", root="teams",
              rel="channels/Nordwind/Angebote__x.html", who="Alice Beispiel", who_mail="",
              ctx="channels/Nordwind", title="Angebote", date="2026-09-15 10:12")
    f = detail.fakten(z, "Bleibt der Rahmen?", None, {})
    assert f["kind"] == "teams" and f["from"]["name"] == "Alice Beispiel"
    assert f["chat"] == "Angebote" and f["folder"] == "channels/Nordwind" and f["text"] == "Bleibt der Rahmen?"
    z = zeile(uid="onenote:Projekte/Nordwind/Kickoff.html:0", src="onenote", root="onenote",
              rel="Projekte/Nordwind/Kickoff.html", ctx="Projekte/Nordwind", date="2026-08-19 15:30")
    f = detail.fakten(z, "Vereinbart …", None, {})
    assert f == {"uid": z["uid"], "kind": "onenote", "folder": "Projekte/Nordwind",
                 "modified": "2026-08-19 15:30", "text": "Vereinbart …"}
