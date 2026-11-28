"""uploads.py – what the keeper adds to the archive by hand: staging,
what a file is taken for, the step that takes it in through versions.py,
how the index reads it, and the three routes."""

import http.client
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

import corpus
import correspondents
import rag_index
import steps
import uploads
import versions
from tests.test_rest import roh, sandbox, server  # noqa: F401 – fixtures

EML = (b"From: Dana Dienstleister <dana@example.com>\r\nTo: alice@nordwind.example\r\n"
       b"Subject: Handed over on paper\r\nDate: Tue, 02 Jun 2026 09:00:00 +0000\r\n"
       b"Message-ID: <paper-1@example.com>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
       b"The zeppelin clause is attached.\r\n")
ICS = (b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:handed-1\r\nSUMMARY:Notary appointment\r\n"
       b"DTSTART:20260610T090000Z\r\nDTEND:20260610T100000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
VCF = (b"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Greta Gast\r\nORG:Beispiel GmbH\r\n"
       b"EMAIL:greta@example.com\r\nUID:greta-1\r\nEND:VCARD\r\n")
PDF = b"%PDF-1.4 a scan"


def put(home, name, data):
    """Stage one file as the app does: bytes into a slot, then named."""
    ident, folder = uploads.new_slot(home)
    (folder / uploads.INCOMING).write_bytes(data)
    return uploads.stage(home, ident, name)


def test_a_file_is_taken_for_what_it_reads_as(tmp_path):
    assert put(tmp_path, "letter.eml", EML)["kind"] == "mail"
    assert put(tmp_path, "notary.ICS", ICS)["kind"] == "event"
    assert put(tmp_path, "greta.vcf", VCF)["kind"] == "contact"
    assert put(tmp_path, "scan.pdf", PDF)["kind"] == "file"
    # The extension alone does not make it one: without a header, an event, a card it is a file.
    for name in ("empty.eml", "empty.ics", "empty.vcf"):
        assert put(tmp_path, name, b"nothing of the kind")["kind"] == "file", name
    # What the archive will read in it is said before it is added.
    mail = put(tmp_path, "letter.eml", EML)
    assert mail["facts"]["title"] == "Handed over on paper" and mail["facts"]["people"] == "Dana Dienstleister"
    assert put(tmp_path, "notary.ics", ICS)["facts"]["title"] == "Notary appointment"
    assert put(tmp_path, "greta.vcf", VCF)["facts"] == {"title": "Greta Gast", "date": "", "people": "Beispiel GmbH"}
    assert put(tmp_path, "scan.pdf", PDF)["facts"] == {"title": "", "date": "", "people": ""}


@pytest.mark.parametrize("given, kept", [
    ("Scan.pdf", "Scan.pdf"), ("../../etc/passwd", "passwd"), ('a"b:c*.PDF', "a_b_c_.pdf"),
    ("C:\\Users\\x\\letter.eml", "C_Users_x_letter.eml"), ("", "file"), (" .hidden ", "hidden"),
    ("x" * 300 + ".pdf", "x" * 100 + ".pdf"), ("note.tar.gz", "note.tar.gz"),
    # The extension as the readers look for it; names the archive keeps for itself or a disk refuses.
    ("LETTER.EML", "LETTER.eml"), ("draft.tmp", "draft_tmp"), ("half.TEIL", "half_teil"),
    ("state.db.bak", "_state.db.bak"), ("CON.txt", "_CON.txt"), ("incoming", "incoming"),
    ("\u6587" * 100 + ".pdf", "\u6587" * 50 + ".pdf"),
    # A dot is not always an extension; and no way around the names the archive passes over.
    ("Dr. Smith letter", "Dr. Smith letter"), ("Scan 3. Seite", "Scan 3. Seite"),
    ("foo.tmp.!!!", "foo.tmp.!!!"), ("x.TEIL.\u00e4", "x.TEIL.\u00e4"), (".pdf", "pdf"),
])
def test_a_name_is_made_safe_for_the_disk(given, kept):
    assert uploads.safe_name(given) == kept


def test_a_staged_file_is_dropped_by_hand_or_after_a_day(tmp_path):
    one, two = put(tmp_path, "a.pdf", PDF), put(tmp_path, "b.pdf", PDF)
    assert uploads.staged(tmp_path, one["id"]).name == "a.pdf"
    assert uploads.discard(tmp_path, one["id"]) and uploads.staged(tmp_path, one["id"]) is None
    assert not uploads.discard(tmp_path, one["id"]) and not uploads.discard(tmp_path, "../x")
    uploads.sweep(tmp_path)
    assert uploads.staged(tmp_path, two["id"]) is not None
    uploads.sweep(tmp_path, now=time.time() + uploads.STALE_SECONDS + 5)
    assert uploads.staged(tmp_path, two["id"]) is None


def test_what_is_typed_is_checked_before_anything_is_written(tmp_path):
    scan, mail = put(tmp_path, "scan.pdf", PDF), put(tmp_path, "letter.eml", EML)
    listed = uploads.commit(tmp_path, [
        {"id": scan["id"], "title": "  Signed   contract ", "date": "2026-05-20", "people": "Greta Gast",
         "description": "Two pages.\nSigned."},
        # What a mail says itself is not typed over: nothing but its id counts.
        {"id": mail["id"], "title": "ignored", "date": "", "people": "", "description": ""}])
    assert listed == [{"id": scan["id"], "kind": "file", "title": "Signed contract", "date": "2026-05-20",
                       "people": "Greta Gast", "description": "Two pages.\nSigned."},
                      {"id": mail["id"], "kind": "mail"}]
    for bad, field in (([], "items"), ([{"id": "nope"}], "id"), ([{"id": scan["id"], "date": "20.05.2026"}], "date"),
                       ([{"id": scan["id"], "title": 7}], "title"), ([{"id": scan["id"], "why": "x"}], "why"),
                       ([{"id": scan["id"], "description": "x" * 20001}], "description"), (["x"], "items")):
        with pytest.raises(uploads.BadValue) as refused:
            uploads.commit(tmp_path, bad)
        assert refused.value.field == field, bad


@pytest.fixture
def journaled(tmp_path, monkeypatch):
    """A data folder whose writes are journaled, as in a run."""
    data = tmp_path / "data"
    monkeypatch.setenv("MUNIMENTUM_VERSIONS_DIR", str(data / versions.VERSIONS_DIRNAME))
    monkeypatch.setenv("MUNIMENTUM_EVIDENCE_DIR", str(data / versions.EVIDENCE_DIRNAME))
    monkeypatch.setattr(versions, "_JOURNAL", {"path": None})
    return tmp_path, data / "uploads_export", data


def _journal(data):
    lines = []
    for path in sorted((data / versions.EVIDENCE_DIRNAME / versions.PENDING_DIRNAME).glob("*.jsonl")):
        lines += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return lines


def _take(home, target, items):
    return uploads.take_in(home, target, uploads.commit(home, items))


def test_the_step_moves_each_file_into_its_folder_through_the_journal(journaled):
    home, target, data = journaled
    staged = [put(home, name, raw) for name, raw in (("letter.eml", EML), ("notary.ics", ICS), ("greta.vcf", VCF))]
    scan = put(home, "Scan contract.pdf", PDF)
    assert _take(home, target, [{"id": s["id"]} for s in staged] + [
        {"id": scan["id"], "title": "Signed contract", "date": "2026-05-20", "people": "Greta Gast",
         "description": "Scan of the signed quagga contract."}]) == (4, 0, 0)
    sha = versions.sha256_bytes(PDF)[:8]
    files = sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file())
    assert files == sorted([f"Dateien/Scan contract__{sha}.pdf", f"meta/Scan contract__{sha}.pdf.json",
                            f"E-Mail/Uploaded/letter__{versions.sha256_bytes(EML)[:8]}.eml",
                            f"kalender/Uploaded/notary__{versions.sha256_bytes(ICS)[:8]}.ics",
                            f"kontakte/Uploaded/greta__{versions.sha256_bytes(VCF)[:8]}.vcf"])
    # Every write went through versions.py: the evidence step will chain it as the app's own.
    written = {line["rel"]: line for line in _journal(data)}
    assert set(written) == {"uploads_export/" + rel for rel in files}
    assert written[f"uploads_export/Dateien/Scan contract__{sha}.pdf"]["sha256"] == versions.sha256_bytes(PDF)
    meta = uploads.read_meta(target, f"Dateien/Scan contract__{sha}.pdf")
    assert (meta["title"], meta["date"], meta["people"], meta["name"]) == \
        ("Signed contract", "2026-05-20", "Greta Gast", "Scan contract.pdf") and meta["added"].endswith("Z")
    # The staging slots are empty again.
    assert not [p for p in uploads.staging_dir(home).iterdir() if p.is_dir()]


def test_the_same_file_again_is_one_file_and_other_words_are_a_change(journaled):
    home, target, data = journaled
    words = {"title": "Signed contract", "date": "", "people": "", "description": "First words."}
    assert _take(home, target, [{"id": put(home, "scan.pdf", PDF)["id"], **words}]) == (1, 0, 0)
    place = next((target / "Dateien").iterdir())
    added, before = uploads.read_meta(target, "Dateien/" + place.name)["added"], len(_journal(data))
    os.utime(place, (1, 1))
    # The same bytes with the same words: nothing new, nothing written, the first moment stays.
    assert _take(home, target, [{"id": put(home, "scan.pdf", PDF)["id"], **words}]) == (0, 1, 0)
    assert len(_journal(data)) == before and place.stat().st_mtime == 1
    assert uploads.read_meta(target, "Dateien/" + place.name)["added"] == added
    # Other words about the same bytes: a change – and the file says so by its time, for the index.
    assert _take(home, target, [{"id": put(home, "scan.pdf", PDF)["id"], **words, "description": "Other words."}]) == (1, 0, 0)
    assert uploads.read_meta(target, "Dateien/" + place.name)["description"] == "Other words."
    assert place.stat().st_mtime > 1 and len(list((target / "Dateien").iterdir())) == 1
    # A file that is gone from its slot is an error, the others still come.
    lost, kept = put(home, "lost.pdf", b"x"), put(home, "kept.pdf", b"y")
    listed = uploads.commit(home, [{"id": lost["id"]}, {"id": kept["id"]}])
    uploads.discard(home, lost["id"])
    assert uploads.take_in(home, target, listed) == (1, 0, 1)


@pytest.fixture
def added(journaled):
    home, target, _data = journaled
    scan = put(home, "Scan contract.pdf", PDF)
    _take(home, target, [{"id": put(home, n, raw)["id"]} for n, raw in (("letter.eml", EML), ("notary.ics", ICS), ("greta.vcf", VCF))]
          + [{"id": scan["id"], "title": "Signed contract", "date": "2026-05-20", "people": "Greta Gast",
              "description": "Scan of the signed quagga contract."}])
    return target


def test_the_index_reads_what_was_added_under_its_own_root(added):
    records = {r["src"]: r for r in corpus.load_uploads(added) + corpus.load_upload_cards(added)}
    assert set(records) == {"outlook", "datei", "kalender", "kontakte"}
    # Everything says where it lies – the mark of its origin – and its uid cannot be a mailbox item's.
    assert all(r["root"] == "uploads" and r["uid"].startswith("uploads:") for r in records.values())
    mail, scan = records["outlook"], records["datei"]
    assert (mail["title"], mail["who"], mail["key"]) == ("Handed over on paper", "Dana Dienstleister", "mail:<paper-1@example.com>")
    # A plain file is what was typed about it: title, day, people, and the words as its text.
    assert (scan["title"], scan["who"], scan["date"]) == ("Signed contract", "Greta Gast", "2026-05-20")
    assert "quagga" in scan["text"] and scan["att"] == "Scan contract.pdf"
    assert records["kalender"]["title"] == "Notary appointment" and records["kontakte"]["title"] == "Greta Gast"
    # The manifest the index skips unchanged files by lists the mail and the file – not the words' sidecar.
    assert sorted(Path(rel).suffix for rel in corpus.manifest("uploads", added)) == [".eml", ".pdf"]
    assert corpus.grabsteine("uploads", added) == {} and not (added / "state.db").exists()


def test_an_index_run_finds_it_and_counts_no_mail_of_it(added, tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    chunks, _manifest, _reused, read = rag_index.lese_bestand(None, None, None, None, None, None, store,
                                                             uploads_dir=str(added))
    assert read == 2 and {c["src"] for c in chunks} == {"outlook", "datei", "kalender", "kontakte"}
    for c in chunks:
        c["hash"] = corpus.chunk_hash(c)
    rag_index.write_db(store, chunks)
    con = sqlite3.connect(store / "corpus.db")
    try:
        hit = con.execute("SELECT c.root, c.title FROM chunks_fts f JOIN chunks c ON c.rowid = f.rowid "
                          "WHERE chunks_fts MATCH 'quagga'").fetchall()
        assert hit == [("uploads", "Signed contract")]
        # A mail added by hand is somebody's mail, not this mailbox's: no row of the correspondence.
        assert list(correspondents.rows(con)) == []
    finally:
        con.close()


def test_the_step_stands_before_the_chain_and_the_index_and_they_know_the_folder():
    order = [e["key"] for e in steps.REGISTRY]
    assert order.index("upload") < order.index("evidence") < order.index("index") < order.index("calendar")
    entry = next(e for e in steps.REGISTRY if e["key"] == "upload")
    # Counted like an export – the index follows only when it brought something – but no Microsoft, no schedule.
    assert entry["corpus"] and not entry["zugang"] and entry["schedule"] is None
    paths = {k: k.upper() for k in ("teams", "outlook", "onedrive", "sharepoint", "sharepoint_pages", "planner",
                                    "todo", "onenote", "uploads", "store", "calendar_json", "data")}
    assert entry["argv"]({}, {"upload": {"home": "/home"}}, paths) == ["UPLOADS", "--staging", "/home"]
    index = steps._index_argv({"embed_model": "m", "ollama": "u"}, {"embeddings": True}, paths)
    assert index[index.index("--uploads") + 1] == "UPLOADS"
    assert steps._calendar_argv({}, {"reconstruct": True}, paths)[-2:] == ["--uploads", "UPLOADS"]
    assert "UPLOADS" in steps._evidence_argv({}, {}, paths)


# -- the routes ---------------------------------------------------------------
def _stage(port, name, data, length=None):
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    con.putrequest("PUT", "/api/v1/uploads/staged" + (f"?name={name}" if name else ""))
    con.putheader("Content-Type", "application/octet-stream")
    con.putheader("Content-Length", str(len(data) if length is None else length))
    con.endheaders()
    con.send(data)
    r = con.getresponse()
    body = json.loads(r.read() or b"{}")
    con.close()
    return r.status, body


def test_a_file_is_staged_named_dropped_and_refused(server, monkeypatch):  # noqa: F811
    a, port = server
    code, staged = _stage(port, "letter.eml", EML)
    assert code == 200 and staged["kind"] == "mail" and staged["size"] == len(EML)
    assert set(staged) == {"id", "name", "size", "kind", "facts"}
    assert uploads.staged(a_home(a), staged["id"]).read_bytes() == EML
    # A name with what a path and a disk refuse is made safe, percent-encoded on the way.
    code, odd = _stage(port, "..%2F..%2FScan%20%E2%80%93%20final.pdf", PDF)
    assert code == 200 and odd["name"] == "Scan \u2013 final.pdf" and odd["kind"] == "file"
    for name, data, key in (("", PDF, "srv.badparam"), ("x.pdf", b"", "srv.upload.empty")):
        code, r = _stage(port, name, data)
        assert code == 400 and r["error"]["k"] == key
    with monkeypatch.context() as smaller:       # only this one patch is taken back
        smaller.setattr(uploads, "MAX_BYTES", 10)
        code, r = _stage(port, "big.pdf", b"x" * 11)
    assert code == 413 and r["error"] == {"k": "srv.upload.toobig", "v": {"mb": 0}}
    # Nothing of the refused ones is kept: two slots, the two that were named.
    assert len([p for p in uploads.staging_dir(a_home(a)).iterdir() if p.is_dir()]) == 2
    assert roh(port, "DELETE", f"/api/v1/uploads/staged/{odd['id']}")[0] == 204
    code, _, r = roh(port, "DELETE", f"/api/v1/uploads/staged/{odd['id']}")
    assert code == 404 and r["error"]["k"] == "srv.upload.unknown"


def a_home(_a):
    import app as app_mod
    return app_mod.HEIM


def test_adding_starts_one_run_that_takes_in_chains_and_indexes(server, monkeypatch):  # noqa: F811
    a, port = server
    started = []
    monkeypatch.setattr(a, "launch", lambda anfrage, **kw: started.append((anfrage, kw)) or (True, None))
    scan, card = _stage(port, "scan.pdf", PDF)[1], _stage(port, "greta.vcf", VCF)[1]
    for body, field in (({}, "items"), ({"items": []}, "items"), ({"items": [{"id": "0" * 32}]}, "id"),
                        ({"items": [{"id": scan["id"], "date": "soon"}]}, "date")):
        code, _, r = roh(port, "POST", "/api/v1/uploads", body)
        assert code == 400 and r["error"] == {"k": "srv.upload.badvalue", "v": {"field": field}}, body
    assert not started
    code, _, r = roh(port, "POST", "/api/v1/uploads", {"items": [{"id": scan["id"], "title": "Scan", "description": "words"}]})
    assert code == 202 and r["count"] == 1 and r["run"] == "/api/v1/runs/current"
    # A plain file alone: the step, the index – the calendar file holds no file.
    assert started[-1] == ({"upload": True, "index": True, "calendar": False}, {"label": "job.upload"})
    listed = json.loads((uploads.staging_dir(a_home(a)) / uploads.COMMIT_NAME).read_text(encoding="utf-8"))
    assert listed["items"][0]["title"] == "Scan" and listed["items"][0]["kind"] == "file"
    # A card came: the calendar and address book are built again.
    roh(port, "POST", "/api/v1/uploads", {"items": [{"id": card["id"]}]})
    assert started[-1][0]["calendar"] is True
    # While a run is on nothing is taken: said, and nothing written down.
    monkeypatch.setattr(type(a.jobs), "busy", property(lambda self: True))
    code, _, r = roh(port, "POST", "/api/v1/uploads", {"items": [{"id": scan["id"]}]})
    assert code == 409 and r["error"]["k"] == "srv.busy"


def test_a_run_started_for_uploads_has_the_step_the_chain_and_the_index(server, monkeypatch):  # noqa: F811
    """What launch makes of the request: the evidence step rides with it
    because the upload counts as a write into the archive."""
    a, _port = server
    seen = {}
    monkeypatch.setattr(a.jobs, "start", lambda steps_, label, **kw: seen.update(steps=[s["key"] for s in steps_]) or True)
    ok, _why = a.launch({"upload": True, "index": True, "calendar": True}, label="job.upload")
    assert ok and seen["steps"] == ["upload", "evidence", "index", "calendar"]


def test_added_by_hand_is_a_filter_beside_the_source_and_an_entry_of_the_file_browser(added, tmp_path):
    import mcp_server
    store = tmp_path / "store"
    store.mkdir()
    chunks, *_ = rag_index.lese_bestand(None, None, None, None, None, None, store, uploads_dir=str(added))
    for c in chunks:
        c["hash"] = corpus.chunk_hash(c)
    rag_index.write_db(store, chunks)
    old = dict(mcp_server.STATE)
    mcp_server.STATE.clear()
    mcp_server.STATE.update(db=str(store / "corpus.db"), uploads_dir=str(added), semantic=False, V=None, np=None)
    try:
        # Where an item comes from is its own question, beside what it is: alone, everything
        # added by hand; with a source, that kind of it – the appointments added by hand.
        found = mcp_server.browse_messages(added_by_hand=True, k=10)
        assert {h["source"] for h in found["results"]} == {"outlook", "datei", "kalender", "kontakte"}
        assert all(h["root"] == "uploads" for h in found["results"])
        events = mcp_server.browse_messages(added_by_hand=True, source="kalender", k=10)["results"]
        assert [h["title"] for h in events] == ["Notary appointment"]
        both = mcp_server.browse_messages(added_by_hand=True, source="kalender,kontakte", k=10)["results"]
        assert {h["source"] for h in both} == {"kalender", "kontakte"}
        assert mcp_server.search_messages("quagga", added_by_hand=True, mode="lexical")["count"] == 1
        assert mcp_server.search_messages("quagga", added_by_hand=True, source="outlook", mode="lexical")["count"] == 0
        scan = next(h for h in found["results"] if h["source"] == "datei")
        assert scan["source_label"] == "File added by hand"
        # The plain file stands in the file browser under an entry of its own – no mirror's.
        roots = mcp_server.list_files()["roots"]
        assert roots == [{"root": "uploads", "path": "Dateien", "label": "Added by hand", "files": 1}]
        listing = mcp_server.list_files("uploads", "Dateien")
        assert listing["label"] == "Added by hand" and listing["base"] == 1
        assert [f["name"] for f in listing["files"]] == [Path(scan["path"]).name]
        # … and its bytes are read through the root the hit names.
        assert mcp_server._resolve_source("uploads", scan["path"])[0].read_bytes() == PDF
    finally:
        mcp_server.STATE.clear()
        mcp_server.STATE.update(old)


def test_an_upper_case_extension_an_odd_name_and_typed_people_all_reach_the_index(journaled):
    """What the step takes in the readers must find: the extension in the
    case they look for, no name the chain or the index passes over, and the
    people of a plain file as the index lists people."""
    import evidence
    home, target, _data = journaled
    assert put(home, "incoming", PDF)["name"] == "incoming"          # a file may be called so
    staged = [put(home, name, raw) for name, raw in (("LETTER.EML", EML), ("Notary.ICS", ICS), ("GRETA.VCF", VCF))]
    assert [s["kind"] for s in staged] == ["mail", "event", "contact"]
    odd = put(home, "draft.tmp", b"a draft")
    # An .ics without an event is a plain file – and stays one: no ghost appointment beside it.
    plain = put(home, "tasks.ics", b"BEGIN:VCALENDAR\r\nBEGIN:VTODO\r\nUID:t-1\r\nEND:VTODO\r\nEND:VCALENDAR\r\n")
    assert plain["kind"] == "file"
    assert _take(home, target, [{"id": s["id"]} for s in staged] + [
        {"id": odd["id"], "people": 'Greta Gast, "Baumeister, Bob";  Carla Chef ,'}, {"id": plain["id"]}]) == (5, 0, 0)
    records = corpus.load_uploads(target) + corpus.load_upload_cards(target)
    assert sorted(r["src"] for r in records) == ["datei", "datei", "kalender", "kontakte", "outlook"]
    draft = next(r for r in records if r["rel"].startswith("Dateien/draft_tmp__"))
    # A quoted name with a comma is one person.
    assert draft["who"] == "Greta Gast \u00b7 Baumeister, Bob \u00b7 Carla Chef"
    # Every file that came is one the chain keeps.
    files = [p for p in target.rglob("*") if p.is_file() and "meta" not in p.parts]
    assert len(files) == 5 and all(evidence.is_archive_file(p.name) for p in files)
    # The calendar and address book build reads only their own folders too.
    import combined_search
    data = combined_search.collect_calendar_data(str(target / "nothing"), reconstruct=False, uploads_dir=str(target))
    listed = data["recs"]
    assert sorted(r["src"] for r in listed if r.get("root") == "uploads") == ["kalender", "kontakte"]


def test_a_note_no_key_is_found_for_fails_alone_and_the_step_goes_on(journaled, monkeypatch):
    import faelle
    import item_notes
    home, target, _data = journaled
    listed = uploads.commit(home, [{"id": put(home, "scan.pdf", PDF)["id"], "note": "kept?"}])
    monkeypatch.setattr(uploads, "item_keys", lambda *a: (_ for _ in ()).throw(OSError("unreadable")))
    notes = item_notes.ItemNotes(home / faelle.DB_NAME)
    # The file is in; the note could not be filed – an error said, no exception.
    assert uploads.take_in(home, target, listed, notes) == (1, 0, 1)
    assert notes.keys() == [] and len(list((target / "Dateien").iterdir())) == 1


def test_a_case_export_and_the_archive_check_know_what_was_added_by_hand(added, tmp_path):
    """Two readers beside the index: the case export copies an uploaded
    item's original, and the archive check does not count the uploads'
    index rows as gone."""
    import archive_check
    import case_export
    scan = next(r for r in corpus.load_uploads(added) if r["src"] == "datei")
    entry = {"src": scan["src"], "root": scan["root"], "rel": scan["rel"], "key": "file:uploads:" + scan["rel"]}
    source, copies, _link = case_export.plan(entry, {"uploads": str(added)})
    assert Path(source) == added and copies == [scan["rel"]]
    assert case_export.quelle_key(entry) == "upload.file" and "uploads" in case_export.ORDNER
    assert case_export.plan(entry, {"outlook": str(tmp_path)})[0] is None     # the folder not handed in: missing, as before
    store = tmp_path / "store"
    store.mkdir()
    chunks, manifest, *_ = rag_index.lese_bestand(None, None, None, None, None, None, store, uploads_dir=str(added))
    for c in chunks:
        c["hash"] = corpus.chunk_hash(c)
    rag_index.write_db(store, chunks, manifest=manifest)
    with_folder = archive_check.pruefe_index(store, {"uploads": str(added)})
    without = archive_check.pruefe_index(store, {})
    assert (with_folder["weg"], with_folder["geaendert"], with_folder["neu"]) == (0, 0, 0)
    assert without["weg"] == 2                   # the mail and the scan: what the missing folder used to cost


def test_where_a_file_lies_keeps_its_extension_last_and_never_one_the_archive_passes_over():
    sha = "ab12cd34" + "0" * 56
    for name, rel in (("Scan.pdf", "Dateien/Scan__ab12cd34.pdf"), ("Dr. Smith letter", "Dateien/Dr. Smith letter__ab12cd34"),
                      ("foo.tmp.!!!", "Dateien/foo.tmp.!!!__ab12cd34"), ("draft.tmp", "Dateien/draft_tmp__ab12cd34"),
                      ("note.tar.gz", "Dateien/note.tar__ab12cd34.gz")):
        assert uploads.target_rel("file", uploads.safe_name(name), sha) == rel, name


@pytest.mark.parametrize("typed, kept", [
    ("Greta Gast, Bob Baumeister", "Greta Gast \u00b7 Bob Baumeister"),
    ('"Beispiel, Alice"; Carla Chef', "Beispiel, Alice \u00b7 Carla Chef"),
    (' , ;"" ', ""), ('"Gast, Greta", ', "Gast, Greta"),
])
def test_names_are_split_at_commas_outside_quotes(typed, kept):
    assert uploads.people_of(typed) == kept
