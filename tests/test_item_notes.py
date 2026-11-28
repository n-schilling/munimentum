"""item_notes.py – the keeper's own note on an item of the archive: the
store with its earlier wordings, the routes, the note that comes with an
uploaded file, and what an MCP client reads of it."""

import sqlite3
from urllib.parse import quote

import pytest

import corpus
import faelle
import item_notes
import rag_index
import uploads
from tests.test_rest import roh, sandbox, server  # noqa: F401 – fixtures
from tests.test_uploads import EML, ICS, PDF, VCF, journaled, put  # noqa: F401 – fixture

KEY = "mail:<paper-1@example.com>"


def test_a_note_is_written_changed_and_removed_and_keeps_what_it_said(tmp_path, monkeypatch):
    notes = item_notes.ItemNotes(tmp_path / faelle.DB_NAME)
    # Reading creates nothing: a reader beside the app finds no file and no table.
    assert notes.get(KEY) == {"key": KEY, "text": "", "updated": None, "versions": 0}
    assert notes.keys() == [] and notes.versions(KEY) == [] and not (tmp_path / faelle.DB_NAME).exists()
    stamps = iter(f"2026-11-2{n}T10:00:00Z" for n in range(1, 9))
    monkeypatch.setattr(item_notes, "now", lambda: next(stamps))
    first = notes.put(KEY, "  Handed over on paper.\nScanned the same day.  ")
    assert first == {"key": KEY, "text": "Handed over on paper.\nScanned the same day.",
                     "updated": "2026-11-21T10:00:00Z", "versions": 0}
    # The same words again change nothing – not the date, and no version is made.
    assert notes.put(KEY, "Handed over on paper.\nScanned the same day.") == first
    changed = notes.put(KEY, "Handed over by Dana Dienstleister.")
    assert (changed["updated"], changed["versions"]) == ("2026-11-22T10:00:00Z", 1)
    # An empty text takes the note away; what it said stays as a version.
    assert notes.put(KEY, "") == {"key": KEY, "text": "", "updated": None, "versions": 2}
    assert notes.keys() == []
    assert notes.versions(KEY) == [
        {"text": "Handed over by Dana Dienstleister.", "written": "2026-11-22T10:00:00Z", "replaced": "2026-11-23T10:00:00Z"},
        {"text": "Handed over on paper.\nScanned the same day.", "written": "2026-11-21T10:00:00Z",
         "replaced": "2026-11-22T10:00:00Z"}]
    # A note written again continues the history; another item's note is its own.
    notes.put(KEY, "Back again.")
    notes.put("file:uploads:Dateien/scan.pdf", "A scan.")
    assert notes.get(KEY)["versions"] == 2 and notes.keys() == ["file:uploads:Dateien/scan.pdf", KEY]
    assert notes.versions("file:uploads:Dateien/scan.pdf") == []
    for key, text, field in (("", "x", "key"), (None, "x", "key"), ("k" * 2001, "x", "key"),
                             (KEY, 7, "text"), (KEY, None, "text"), (KEY, "x" * (item_notes.MOST + 1), "text")):
        with pytest.raises(item_notes.BadValue) as refused:
            notes.put(key, text)
        assert refused.value.field == field


def _path(route, key=KEY):
    return f"/api/v1/{route}?key={quote(key, safe='')}"


def test_the_routes_read_write_and_list_a_note_by_the_items_key(server):  # noqa: F811
    _a, port = server
    assert roh(port, "GET", _path("notes"))[2] == {"key": KEY, "text": "", "updated": None, "versions": 0}
    assert roh(port, "GET", "/api/v1/notes/keys")[2] == {"items": [], "count": 0}
    code, _, note = roh(port, "PUT", _path("notes"), {"text": "From the landlord's folder."})
    assert code == 200 and note["text"] == "From the landlord's folder." and note["updated"].endswith("Z")
    roh(port, "PUT", _path("notes"), {"text": "From the landlord's folder, page 3."})
    assert roh(port, "GET", "/api/v1/notes/keys")[2] == {"items": [KEY], "count": 1}
    code, _, earlier = roh(port, "GET", _path("notes/versions"))
    assert code == 200 and earlier["count"] == 1 and earlier["items"][0]["text"] == "From the landlord's folder."
    assert set(earlier["items"][0]) == {"text", "written", "replaced"}
    # Removing is writing nothing; the list forgets the item, the versions do not.
    assert roh(port, "PUT", _path("notes"), {"text": ""})[2]["versions"] == 2
    assert roh(port, "GET", "/api/v1/notes/keys")[2]["items"] == []
    for method, path, body, name in (("GET", "/api/v1/notes", None, "key"), ("GET", "/api/v1/notes/versions", None, "key"),
                                     ("PUT", "/api/v1/notes", {"text": "x"}, "key"), ("PUT", _path("notes"), {}, "text"),
                                     ("PUT", _path("notes"), {"text": 7}, "text"),
                                     ("PUT", _path("notes"), {"text": "x", "why": "y"}, "why")):
        code, _, r = roh(port, method, path, body)
        assert code == 400 and r["error"] == {"k": "srv.badparam", "v": {"name": name}}, (method, path, body)


def test_a_note_comes_with_an_uploaded_file_under_the_key_the_index_gives_it(journaled, tmp_path):  # noqa: F811
    """The step files the note before the index has seen the file – under
    exactly the key the index run then writes, for every kind."""
    home, target, _data = journaled
    staged = {name: put(home, name, raw) for name, raw in
              (("letter.eml", EML), ("notary.ics", ICS), ("greta.vcf", VCF), ("scan.pdf", PDF),
               ("bare.eml", EML.replace(b"Message-ID: <paper-1@example.com>\r\n", b"")), ("plain.pdf", b"no note"))}
    items = [{"id": staged[name]["id"], "note": f"Note on {name}."} for name in staged if name != "plain.pdf"]
    listed = uploads.commit(home, items + [{"id": staged["plain.pdf"]["id"], "note": "   "}])
    # The note rides with every kind; an empty one is none.
    assert [x.get("note") for x in listed] == [f"Note on {n}." for n in staged if n != "plain.pdf"] + [None]
    with pytest.raises(uploads.BadValue) as refused:
        uploads.commit(home, [{"id": staged["scan.pdf"]["id"], "note": "x" * 20001}])
    assert refused.value.field == "note"
    notes = item_notes.ItemNotes(home / faelle.DB_NAME)
    assert uploads.take_in(home, target, listed, notes) == (6, 0, 0)
    store = tmp_path / "store"
    store.mkdir()
    chunks, *_ = rag_index.lese_bestand(None, None, None, None, None, None, store, uploads_dir=str(target))
    indexed = {c["key"]: c for c in chunks}
    assert sorted(notes.keys()) == sorted(k for k, c in indexed.items() if "plain" not in c["rel"])
    assert len(notes.keys()) == 5
    by_name = {c["rel"].rsplit("/", 1)[1].split("__")[0]: k for k, c in indexed.items()}
    assert by_name["letter"] == "mail:<paper-1@example.com>" and notes.get(by_name["letter"])["text"] == "Note on letter.eml."
    assert by_name["notary"] == "event:handed-1" and by_name["greta"] == "contact:greta-1"
    # Without an identity of its own the key is the place – the index's and the step's alike.
    assert by_name["bare"].startswith("mail:E-Mail/Uploaded/bare__") and notes.get(by_name["bare"])["text"] == "Note on bare.eml."
    assert by_name["scan"].startswith("file:uploads:Dateien/scan__") and notes.get(by_name["scan"])["text"] == "Note on scan.pdf."
    # The same file again with another note: one file, the note changed, the first kept as a version.
    again = uploads.commit(home, [{"id": put(home, "scan.pdf", PDF)["id"], "note": "Second thoughts."}])
    assert uploads.take_in(home, target, again, notes) == (0, 1, 0)
    assert notes.get(by_name["scan"])["text"] == "Second thoughts." and notes.get(by_name["scan"])["versions"] == 1


def test_the_step_itself_writes_the_notes_into_the_profiles_file(journaled, monkeypatch):  # noqa: F811
    home, target, _data = journaled
    uploads.commit(home, [{"id": put(home, "scan.pdf", PDF)["id"], "title": "Scan", "note": "From the notary."}])
    monkeypatch.setattr("sys.argv", ["uploads.py", str(target), "--staging", str(home)])
    assert uploads.main() in (None, 0)
    con = sqlite3.connect(home / faelle.DB_NAME)
    try:
        assert [row[0] for row in con.execute("SELECT text FROM item_notes")] == ["From the notary."]
    finally:
        con.close()
    assert corpus.load_uploads(target)[0]["title"] == "Scan"


def test_a_locked_store_is_an_error_not_an_empty_note(tmp_path, monkeypatch):
    """No table yet is nothing there; any other trouble reading must not
    look like it – for the notes (the window would show one empty and a
    save write over it), the footers (an index run would read them all
    again) and the cards of the app's own alike: one helper reads for all
    three (profile_db)."""
    import footers
    import own_contacts
    import profile_db
    path = tmp_path / faelle.DB_NAME
    notes, kept, book = item_notes.ItemNotes(path), footers.Footers(path), own_contacts.OwnContacts(path)
    # Before any table: nothing, and nothing made.
    assert notes.get(KEY)["text"] == "" and kept.stamps() == {} and book.all() == [] and book.ignored() == []
    assert not path.exists()
    notes.put(KEY, "Kept.")
    kept.keep("greta@example.com", {"lines": ["Greta Gast"], "basis": "single"})
    book.add("dana@example.com", "Dana Dienstleister")
    book.ignore("news@example.com")
    assert kept.stamps() and [c["address"] for c in book.all()] == ["dana@example.com"]

    class Locked:
        row_factory = None

        def execute(self, *a):
            raise sqlite3.OperationalError("database is locked")

        def close(self):
            pass

    monkeypatch.setattr(profile_db.sqlite3, "connect", lambda *a, **k: Locked())
    for read in (lambda: notes.get(KEY), kept.stamps, kept.lines, book.all, book.ignored, book.ignored_names):
        with pytest.raises(sqlite3.OperationalError):
            read()


def test_every_table_of_the_profile_goes_through_one_way_in(tmp_path, monkeypatch):
    """The case book, the cards, the footers and the notes open the same
    file the same way: one wait, one kind of refusal (profile_db)."""
    import footers
    import own_contacts
    import profile_db
    assert item_notes.BadValue is own_contacts.BadValue is profile_db.BadValue
    assert item_notes.now is own_contacts.now is profile_db.now
    assert footers.profile_db is profile_db and faelle.profile_db is profile_db
    waits = []
    real = profile_db.sqlite3.connect
    monkeypatch.setattr(profile_db.sqlite3, "connect", lambda *a, **k: waits.append(k.get("timeout")) or real(*a, **k))
    path = tmp_path / faelle.DB_NAME
    faelle.Fallbuch(path).fall_anlegen("Site 1")
    own_contacts.OwnContacts(path).ignore("news@example.com")
    footers.Footers(path).keep("greta@example.com", {"lines": []})
    item_notes.ItemNotes(path).put(KEY, "x")
    assert waits and set(waits) == {profile_db.TIMEOUT}


def test_an_old_case_book_gets_the_columns_later_releases_added(tmp_path):
    """A table an older release made lacks columns added since: they come
    when the file is opened, and the rows keep what they held."""
    import sqlite3 as db
    path = tmp_path / faelle.DB_NAME
    con = db.connect(path)
    con.execute("CREATE TABLE notizen(id INTEGER PRIMARY KEY, fall_id INTEGER NOT NULL, "
                "wann TEXT NOT NULL, text TEXT NOT NULL)")
    con.execute("INSERT INTO notizen(fall_id, wann, text) VALUES (1, '2026-01-01T00:00:00+00:00', 'Old words')")
    con.commit()
    con.close()
    faelle.Fallbuch(path).faelle()
    con = db.connect(path)
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info(notizen)")}
        assert "quelle" in columns
        assert con.execute("SELECT text FROM notizen").fetchall() == [("Old words",)]
    finally:
        con.close()


def test_a_profile_folder_with_a_hash_in_its_name_is_read_as_it_is(tmp_path):
    """The read-only open is a URI: a folder named "Archive #2" or "50%"
    must not cut the path at its "#" or read "%20" as a space."""
    import profile_db
    for name in ("Archive #2", "a?b", "50%20 done"):
        path = tmp_path / name / faelle.DB_NAME
        item_notes.ItemNotes(path).put(KEY, "Kept.")
        assert profile_db.rows(path, "SELECT text FROM item_notes")[0][0] == "Kept.", name
