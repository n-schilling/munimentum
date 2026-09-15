"""schluessel.py – the stable key of every item: from the item itself
where the parse has it, from the exports' bookkeeping otherwise, from the
path only as a last resort. Everything on disk in miniature."""

import json

import pytest

import corpus
import rag_index
import schluessel
import state_db
from tests.hilfen import ohne_schluesselspalte

TEAMS_HTML = ('<html><body><h1>Projekt Alpha</h1>'
              '<div class="msg" data-id="m-1"><div class="head"><span class="name">Alice Example</span>'
              '<span class="time">2025-06-01 09:30</span></div><div class="body">Hallo Bob</div></div>'
              '<div class="msg"><div class="head"><span class="name">Bob</span>'
              '<span class="time">2025-06-01 09:35</span></div><div class="body">Danke!</div></div>'
              '</body></html>')
MAIL = ("From: Alice Example <alice@example.com>\nTo: bob@example.com\n"
        "Subject: Angebot\nMessage-ID: <abc-123@example.com>\nDate: Mon, 2 Jun 2025 10:00:00 +0200\n"
        "\nHallo Bob, anbei das Angebot.\n")
ICS = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:ev-77\nSUMMARY:Planung\n"
       "DTSTART:20250601T120000Z\nEND:VEVENT\nEND:VCALENDAR\n")
VCF = "BEGIN:VCARD\nVERSION:3.0\nUID:c-9\nFN:Alice Beispiel\nEMAIL:alice@example.com\nEND:VCARD\n"


def test_kopfzeile_liest_eine_kopfzeile_und_nicht_die_datei(tmp_path):
    p = tmp_path / "a.eml"
    p.write_text("Subject: x\nMessage-ID: <a@b>\n continued\n\nMessage-ID: <im-body@x>\n", encoding="utf-8")
    assert schluessel.kopfzeile(p, "Message-ID") == "<a@b>continued"
    assert schluessel.kopfzeile(p, "Subject") == "x"
    assert schluessel.kopfzeile(p, "Nirgends") is None
    assert schluessel.kopfzeile(tmp_path / "fehlt.eml", "Subject") is None
    (tmp_path / "t.ics").write_text(ICS, encoding="utf-8")
    assert schluessel.kopfzeile(tmp_path / "t.ics", "UID") == "ev-77"


def test_die_parser_bringen_den_schluessel_mit(tmp_path):
    """Mail, calendar, contact: the record carries its key from the parse."""
    out = tmp_path / "outlook"
    (out / "E-Mail/Posteingang").mkdir(parents=True)
    (out / "E-Mail/Posteingang/a.eml").write_text(MAIL, encoding="utf-8")
    (out / "kalender/Arbeit").mkdir(parents=True)
    (out / "kalender/Arbeit/t.ics").write_text(ICS, encoding="utf-8")
    (out / "kontakte").mkdir()
    (out / "kontakte/alice.vcf").write_text(VCF, encoding="utf-8")
    (mail,) = corpus.load_outlook(str(out))
    assert mail["key"] == "mail:<abc-123@example.com>"
    (termin,) = corpus.load_calendar(str(out))
    assert termin["key"] == "event:ev-77"
    (kontakt,) = corpus.load_contacts(str(out))
    assert kontakt["key"] == "contact:c-9"


def test_teams_nachricht_kennt_ihre_kennung_aus_dem_html(tmp_path):
    d = tmp_path / "teams" / "1on1"
    d.mkdir(parents=True)
    (d / "Alice__c1.html").write_text(TEAMS_HTML, encoding="utf-8")
    recs = corpus.load_teams(str(tmp_path / "teams"))
    assert [r.get("msg_id") for r in recs] == ["m-1", None]


def test_zuweisen_nimmt_die_buchhaltung_und_faellt_auf_den_pfad_zurueck(tmp_path):
    """Files, pages, Teams conversations and OneNote pages take their id from
    the export's state.db; what no bookkeeping knows keeps a path key, and
    the index says so (stabil)."""
    ordner = {k: tmp_path / k for k in ("teams", "outlook", "onedrive", "sharepoint", "pages", "onenote")}
    for p in ordner.values():
        p.mkdir()
    state_db.StateDb(ordner["onedrive"]).bestand_schreiben(
        {"item-1": {"rel": "Dateien/a.pdf", "ctag": "c", "size": 1}})
    lib = ordner["sharepoint"] / "Nordwind" / "Dokumente"
    state_db.StateDb(lib).bestand_schreiben({"item-2": {"rel": "Dateien/b.pdf", "ctag": "c", "size": 1}})
    kanal = ordner["teams"] / "channels" / "Team Rakete"
    state_db.StateDb(kanal).bestand_schreiben({"item-3": {"rel": "Dateien/c.pdf", "ctag": "c", "size": 1}})
    state_db.StateDb(ordner["teams"]).saetze_schreiben("conversations", {
        "c1": json.dumps({"done": True, "rel": "1on1/Alice__c1.html"}),
        "ch:k1": json.dumps({"done": True, "rel": "channels/Team Rakete/Allgemein__k1.html"})})
    state_db.StateDb(ordner["pages"]).seiten_schreiben({"p-5": {"rel": "Site/SitePages/Start.html", "etag": "e"}})
    nb = ordner["onenote"] / "Projekte__nb1"
    state_db.StateDb(nb).saetze_schreiben("pages", {"pg-8": json.dumps({"rel": "Allgemein/A__pg8.html", "lm": ""})})
    chunks = [
        {"src": "datei", "root": "onedrive", "rel": "Dateien/a.pdf", "uid": "datei:Dateien/a.pdf:0"},
        {"src": "datei", "root": "onedrive", "rel": "Dateien/fremd.pdf", "uid": "datei:Dateien/fremd.pdf:0"},
        {"src": "datei", "root": "sharepoint", "rel": "Nordwind/Dokumente/Dateien/b.pdf", "uid": "sharepoint:x:0"},
        {"src": "datei", "root": "teams", "rel": "channels/Team Rakete/Dateien/c.pdf", "uid": "teamsdatei:x:0"},
        {"src": "teams", "root": "teams", "rel": "1on1/Alice__c1.html", "uid": "teams:1on1/Alice__c1.html:3", "msg_id": "m-9"},
        {"src": "teams", "root": "teams", "rel": "channels/Team Rakete/Allgemein__k1.html", "uid": "teams:channels/Team Rakete/Allgemein__k1.html:0"},
        {"src": "teams", "root": "teams", "rel": "group/unbekannt.html", "uid": "teams:group/unbekannt.html:2"},
        {"src": "pages", "root": "pages", "rel": "Site/SitePages/Start.html", "uid": "pages:x:0"},
        {"src": "onenote", "root": "onenote", "rel": "Projekte__nb1/Allgemein/A__pg8.html", "uid": "onenote:x:0"},
        {"src": "onenote", "root": "onenote", "rel": "Projekte__nb1/Allgemein/fremd.html", "uid": "onenote:y:0"},
        {"src": "planner", "root": "planner", "rel": "Board__p1/board.html", "uid": "planner:Board__p1/t-4:0"},
        {"src": "todo", "root": "todo", "rel": "Liste__l1/list.html", "uid": "todo:Liste__l1/t-5:0"},
        {"src": "outlook", "root": "outlook", "rel": "E-Mail/weg.eml", "uid": "outlook:E-Mail/weg.eml:0"},
        {"src": "kontakte", "root": "outlook", "rel": "kontakte/x.vcf", "uid": "kontakte:x:0", "key": "contact:schon-da"},
    ]
    schluessel.zuweisen(chunks, {k: str(v) for k, v in ordner.items()})
    keys = [c["key"] for c in chunks]
    assert keys == ["file:item-1", "file:onedrive:Dateien/fremd.pdf", "file:item-2", "file:item-3",
                    "teams:c1#m-9", "teams:k1#0", "teams:group/unbekannt.html#2",
                    "page:p-5", "note:pg-8", "note:Projekte__nb1/Allgemein/fremd.html",
                    "planner:t-4", "todo:t-5", "mail:E-Mail/weg.eml", "contact:schon-da"]
    assert [schluessel.stabil(k) for k in keys] == [True, False, True, True, True, True, False,
                                                     True, True, False, True, True, False, True]


def test_zuweisen_liest_alte_blobs_und_den_mailkopf(tmp_path):
    """A pre-9.0 Teams blob and a mail without its record: the conversation
    id comes from the blob, the Message-ID from the file's header."""
    teams = tmp_path / "teams"
    teams.mkdir()
    state_db.StateDb(teams).kv_schreiben("state", json.dumps({"conversations": {
        "c7": {"done": True, "rel": "meeting/Standup__c7.html"}}}))
    out = tmp_path / "outlook"
    (out / "E-Mail").mkdir(parents=True)
    (out / "E-Mail/a.eml").write_text(MAIL, encoding="utf-8")
    chunks = [{"src": "teams", "root": "teams", "rel": "meeting/Standup__c7.html", "uid": "teams:meeting/Standup__c7.html:1"},
              {"src": "outlook", "root": "outlook", "rel": "E-Mail/a.eml", "uid": "outlook:E-Mail/a.eml:0"}]
    schluessel.zuweisen(chunks, {"teams": str(teams), "outlook": str(out)})
    assert [c["key"] for c in chunks] == ["teams:c7#1", "mail:<abc-123@example.com>"]


def test_index_schreibt_und_liest_den_schluessel(tmp_path):
    """The store carries the key per chunk, hands it to every hit, and an
    index from before 11.0 – without the column – still loads."""
    import sqlite3
    store = tmp_path / "store"
    store.mkdir()
    chunks = corpus.chunk_records([
        {"uid": "outlook:E-Mail/a.eml:0", "src": "outlook", "root": "outlook", "rel": "E-Mail/a.eml",
         "key": "mail:<abc@x>", "who": "Alice", "ppl": "alice", "ts": 1.0, "date": "2025-06-01",
         "title": "Angebot", "ctx": "E-Mail", "text": "Hallo"}])
    for c in chunks:
        c["hash"] = corpus.chunk_hash(c)
    rag_index.write_db(store, chunks, {("outlook", "E-Mail/a.eml"): (1, 2)})
    con = sqlite3.connect(store / "corpus.db")
    assert con.execute("SELECT key FROM chunks").fetchone()[0] == "mail:<abc@x>"
    assert any(r[1] == "ix_chunks_key" for r in con.execute("PRAGMA index_list(chunks)"))
    con.close()
    manifest, alt = rag_index._alter_bestand(store)
    assert alt[("outlook", "E-Mail/a.eml")][0]["key"] == "mail:<abc@x>"
    # An older store: no key column, the chunks come back without one.
    ohne_schluesselspalte(store / "corpus.db")
    manifest, alt = rag_index._alter_bestand(store)
    assert "key" not in alt[("outlook", "E-Mail/a.eml")][0]


def test_hit_traegt_den_schluessel(state_alle_min):
    import mcp_server
    res = mcp_server.browse_messages(k=50)
    assert res["results"] and all(h.get("key") for h in res["results"])


@pytest.fixture
def state_alle_min(tmp_path, monkeypatch):
    """A tiny index with keys, STATE pointing at it."""
    import mcp_server
    store = tmp_path / "rag_store"
    store.mkdir()
    chunks = corpus.chunk_records([
        {"uid": "outlook:E-Mail/a.eml:0", "src": "outlook", "root": "outlook", "rel": "E-Mail/a.eml",
         "key": "mail:<abc@x>", "who": "Alice", "ppl": "alice", "ts": 1.0, "date": "2025-06-01",
         "title": "Angebot", "ctx": "E-Mail", "text": "Hallo"}])
    for c in chunks:
        c["hash"] = corpus.chunk_hash(c)
    rag_index.write_db(store, chunks)
    rag_index.write_info(store, None, 0, len(chunks))
    old = dict(mcp_server.STATE)
    mcp_server.STATE.clear()
    mcp_server.STATE.update(db=str(store / "corpus.db"), V=None, np=None, semantic=False,
                            outlook_dir=str(tmp_path / "outlook"))
    yield store
    mcp_server.STATE.clear()
    mcp_server.STATE.update(old)


def test_teams_html_traegt_die_nachrichtenkennung_und_karten_ihren_anker():
    import teams_export as te
    html = te.render_message({"id": "m-42", "messageType": "message",
                              "createdDateTime": "2025-06-01T09:30:00Z",
                              "from": {"user": {"displayName": "Alice"}},
                              "body": {"contentType": "text", "content": "hi"}})
    assert 'data-id="m-42"' in html
    pr = corpus.ConvParser()
    pr.feed("<html><body><h1>T</h1>" + html + "</body></html>")
    pr.finish()
    assert pr.msgs[0]["id"] == "m-42"
