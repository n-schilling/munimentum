"""evidence.py: the chain of checksums the archive keeps of itself, what
it says about a file's versions, the check, the time stamps and what a
case pins."""

import json

import pytest

import evidence
import state_db
import teams_export
import versions


@pytest.fixture
def data(tmp_path, monkeypatch):
    for folder in ("onedrive_export", "teams_export", "planner_export"):
        (tmp_path / folder).mkdir()
    monkeypatch.setenv("MUNIMENTUM_VERSIONS_DIR", str(tmp_path / "versions"))
    monkeypatch.setenv("MUNIMENTUM_EVIDENCE_DIR", str(tmp_path / "evidence"))
    monkeypatch.setenv("KEEP_VERSIONS", "1")
    monkeypatch.setitem(versions._JOURNAL, "path", None)
    return tmp_path


def roots(data):
    return [data / f for f in ("onedrive_export", "teams_export", "planner_export")]


def lines(data):
    return [json.loads(z) for z in (data / "evidence" / "chain.jsonl").read_text(encoding="utf-8").splitlines()]


def new_run(monkeypatch):
    """A later step is a new process: a journal of its own."""
    monkeypatch.setitem(versions._JOURNAL, "path", None)


def test_the_first_run_chains_the_archive_as_found(data):
    (data / "onedrive_export" / "a.txt").write_text("a", encoding="utf-8")
    (data / "onedrive_export" / "state.db").write_bytes(b"bookkeeping")
    (data / "onedrive_export" / "b.txt.tmp").write_text("half", encoding="utf-8")
    c = evidence.sweep(data, roots(data))
    assert c["first"] and c["initial"] == 1
    genesis, first = lines(data)
    assert genesis["kind"] == "genesis" and genesis["n"] == 1 and "prev" not in genesis
    assert first["kind"] == "initial" and first["rel"] == "onedrive_export/a.txt"
    assert first["sha256"] == versions.sha256_bytes(b"a")
    assert first["prev"] == evidence.line_hash(
        (data / "evidence" / "chain.jsonl").read_text(encoding="utf-8").splitlines()[0])
    # Nothing moved: a second run adds no line
    assert not any(v for k, v in evidence.sweep(data, roots(data)).items() if k not in ("first", "stamp"))
    assert len(lines(data)) == 2


def test_a_write_the_journal_announced_is_a_write_anything_else_outside(data, monkeypatch):
    path = data / "onedrive_export" / "plan.md"
    versions.write_text(path, "draft 1")
    evidence.sweep(data, roots(data))
    new_run(monkeypatch)
    versions.write_text(path, "draft 2")
    (data / "onedrive_export" / "notes.txt").write_text("dropped in by hand", encoding="utf-8")
    c = evidence.sweep(data, roots(data))
    assert (c["write"], c["outside"], c["version"]) == (1, 1, 1)
    by_rel = {d.get("rel"): d for d in lines(data)}
    assert by_rel["onedrive_export/plan.md"]["kind"] == "write"
    assert by_rel["onedrive_export/plan.md"]["was"] == versions.sha256_bytes(b"draft 1")
    assert by_rel["onedrive_export/notes.txt"]["kind"] == "outside"
    kept = [d for d in lines(data) if d.get("kind") == "version"]
    assert kept[0]["sha256"] == versions.sha256_bytes(b"draft 1")
    # The journal is spent once chained
    assert not list((data / "evidence" / "pending").glob("*.jsonl"))


def test_a_file_gone_without_the_journal_is_missing_with_it_removed(data, monkeypatch):
    a, b = data / "onedrive_export" / "a.txt", data / "onedrive_export" / "b.txt"
    versions.write_text(a, "a")
    versions.write_text(b, "b")
    evidence.sweep(data, roots(data))
    new_run(monkeypatch)
    versions.remove(a)
    b.unlink()
    c = evidence.sweep(data, roots(data))
    assert (c["removed"], c["missing"]) == (1, 1)
    kinds = {d["rel"]: d["kind"] for d in lines(data) if d.get("kind") in ("removed", "missing")}
    assert kinds == {"onedrive_export/a.txt": "removed", "onedrive_export/b.txt": "missing"}


def test_the_history_follows_a_file_through_a_rename(data, monkeypatch):
    old = data / "onedrive_export" / "Termin alt.ics"
    new = data / "onedrive_export" / "Termin neu.ics"
    versions.write_text(old, "SUMMARY:alt")
    evidence.sweep(data, roots(data))
    new_run(monkeypatch)
    versions.write_text(new, "SUMMARY:neu")
    versions.remove(old, successor=new)
    evidence.sweep(data, roots(data))
    ev = evidence.Evidence(data)
    history = ev.history("onedrive_export/Termin neu.ics")
    assert [v["sha256"] for v in history] == [versions.sha256_bytes(b"SUMMARY:neu"),
                                               versions.sha256_bytes(b"SUMMARY:alt")]
    assert history[0]["current"] and not history[1]["current"]
    assert all(v["available"] for v in history)
    assert ev.bytes_of("onedrive_export/Termin neu.ics", history[1]["sha256"]) == b"SUMMARY:alt"
    ev.close()


def test_a_move_keeps_the_history(data, monkeypatch):
    old = data / "onedrive_export" / "Dateien" / "a" / "x.txt"
    old.parent.mkdir(parents=True)
    versions.write_text(old, "x")
    evidence.sweep(data, roots(data))
    new_run(monkeypatch)
    target = data / "onedrive_export" / "Dateien" / "b"
    target.mkdir()
    old.replace(target / "x.txt")
    versions.moved(old, target / "x.txt")
    c = evidence.sweep(data, roots(data))
    assert (c["moved"], c["removed"], c["outside"], c["missing"]) == (1, 1, 0, 0)
    ev = evidence.Evidence(data)
    assert ev.names_of("onedrive_export/Dateien/b/x.txt") == [
        "onedrive_export/Dateien/b/x.txt", "onedrive_export/Dateien/a/x.txt"]
    ev.close()


def test_the_index_is_rebuilt_from_the_chain(data, monkeypatch):
    path = data / "onedrive_export" / "plan.md"
    versions.write_text(path, "1")
    evidence.sweep(data, roots(data))
    new_run(monkeypatch)
    versions.write_text(path, "2")
    evidence.sweep(data, roots(data))
    before = evidence.Evidence(data).history("onedrive_export/plan.md")
    (data / "evidence" / "evidence.db").unlink()
    ev = evidence.Evidence(data)
    assert [v["sha256"] for v in ev.history("onedrive_export/plan.md")] == [v["sha256"] for v in before]
    assert ev.head()[0] == len(lines(data))
    ev.close()
    # and nothing is chained twice after the rebuild
    assert not any(v for k, v in evidence.sweep(data, roots(data)).items() if k not in ("first", "stamp"))


def test_verify_reads_the_chain_and_hashes_every_file(data):
    a, b = data / "onedrive_export" / "a.txt", data / "onedrive_export" / "b.txt"
    versions.write_text(a, "a")
    versions.write_text(b, "b")
    evidence.sweep(data, roots(data))
    ok = evidence.verify(data)
    assert ok["exists"] and ok["chain_ok"] and ok["unchanged"] == 2
    assert (ok["changed_n"], ok["missing_n"]) == (0, 0)
    a.write_text("changed by hand", encoding="utf-8")
    b.unlink()
    found = evidence.verify(data)
    assert [c["rel"] for c in found["changed"]] == ["onedrive_export/a.txt"]
    assert found["changed"][0]["now"] == versions.sha256_bytes(b"changed by hand")
    assert [m["rel"] for m in found["missing"]] == ["onedrive_export/b.txt"]
    assert evidence.verify(data / "nothing") == {"exists": False}


def test_a_changed_chain_line_breaks_everything_after_it(data):
    for name in "abc":
        versions.write_text(data / "onedrive_export" / f"{name}.txt", name)
    evidence.sweep(data, roots(data))
    chain = data / "evidence" / "chain.jsonl"
    texts = chain.read_text(encoding="utf-8").splitlines()
    texts[2] = texts[2].replace('"initial"', '"write"')
    chain.write_text("\n".join(texts) + "\n", encoding="utf-8")
    result = evidence.verify(data)
    assert not result["chain_ok"] and result["broken_at"] == 4



def test_a_changed_last_line_is_found_too(data):
    versions.write_text(data / "onedrive_export" / "a.txt", "a")
    evidence.sweep(data, roots(data))
    chain = data / "evidence" / "chain.jsonl"
    texts = chain.read_text(encoding="utf-8").splitlines()
    texts[-1] = texts[-1].replace('"initial"', '"write"')
    chain.write_text("\n".join(texts) + "\n", encoding="utf-8")
    result = evidence.verify(data)
    assert not result["chain_ok"] and result["broken_at"] == len(texts)


# ---------------------------------------------------------------------------
# Time stamps (RFC 3161)
# ---------------------------------------------------------------------------
def _read_der(buf, pos=0):
    tag, body, end = evidence._der_read(buf, pos)
    return tag, body, end


def test_the_request_is_a_der_timestampreq_for_sha256():
    digest = bytes(range(32))
    req = evidence.timestamp_request(digest, nonce=0x1234)
    tag, body, end = _read_der(req)
    assert tag == 0x30 and end == len(req)
    tag, version, pos = _read_der(body)
    assert (tag, version) == (0x02, b"\x01")
    tag, imprint, pos = _read_der(body, pos)
    assert tag == 0x30
    tag, algo, ipos = _read_der(imprint)
    assert algo.startswith(bytes.fromhex("0609608648016503040201"))
    tag, hashed, _ = _read_der(imprint, ipos)
    assert (tag, hashed) == (0x04, digest)
    tag, nonce, pos = _read_der(body, pos)
    assert (tag, nonce) == (0x02, b"\x12\x34")
    assert body[pos:] == b"\x01\x01\xff"          # certReq


def _answer(status, digest=b""):
    info = evidence._der(0x30, evidence._der_int(status))
    return evidence._der(0x30, info + (evidence._der(0x30, evidence._der(0x04, digest)) if digest else b""))


def test_the_answer_status_is_read():
    assert evidence.response_status(_answer(0)) == 0
    assert evidence.response_status(_answer(2)) == 2
    assert evidence.response_status(b"nonsense") is None


class _Answer:
    def __init__(self, content, code=200):
        self.content, self.status_code = content, code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_a_run_has_the_chain_head_stamped(data, monkeypatch):
    import requests
    sent = []

    def post(url, data=None, headers=None, timeout=None):
        sent.append((url, headers["Content-Type"]))
        # the digest is the imprint's octet string
        _t, imprint, _e = evidence._der_read(data, 0)
        _t, _v, pos = evidence._der_read(imprint, 0)
        _t, mi, _p = evidence._der_read(imprint, pos)
        _t, _a, ipos = evidence._der_read(mi, 0)
        _t, digest, _x = evidence._der_read(mi, ipos)
        return _Answer(_answer(0, digest))
    monkeypatch.setattr(requests, "post", post)
    versions.write_text(data / "onedrive_export" / "a.txt", "a")
    said = []
    c = evidence.sweep(data, roots(data), tsa="https://tsa.example/tsr",
                       events=lambda key, **v: said.append(key))
    assert c["stamp"] and sent == [("https://tsa.example/tsr", "application/timestamp-query")]
    stamp = lines(data)[-1]
    assert stamp["kind"] == "stamp" and stamp["tsa"] == "https://tsa.example/tsr"
    head = (data / "evidence" / stamp["file"]).with_suffix(".head").read_bytes()
    assert stamp["sha256"] == versions.sha256_bytes(head)
    assert f"head: {stamp['head']}".encode() in head
    assert said == ["run.evidence.stamped"]
    ev = evidence.Evidence(data)
    assert ev.summary()["stamped"] == stamp["at"]
    ev.close()


def test_a_refused_stamp_is_one_log_line_and_no_chain_line(data, monkeypatch):
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _Answer(_answer(2)))
    versions.write_text(data / "onedrive_export" / "a.txt", "a")
    said = []
    c = evidence.sweep(data, roots(data), tsa="https://tsa.example/tsr",
                       events=lambda key, **v: said.append((key, v)))
    assert c["stamp"] is None and said[0][0] == "run.evidence.stamp_failed"
    assert "status 2" in said[0][1]["error"]
    assert not any(d["kind"] == "stamp" for d in lines(data))


# ---------------------------------------------------------------------------
# Cases: the manifest on closing, and what an item pins
# ---------------------------------------------------------------------------
def test_a_closed_case_is_chained_with_the_next_run(data):
    versions.write_text(data / "onedrive_export" / "a.txt", "a")
    evidence.sweep(data, roots(data))
    ev = evidence.Evidence(data)
    rows = evidence.manifest_rows(ev, [{"archive_rel": "onedrive_export/a.txt", "file": "OneDrive/a.txt",
                                        "src": "datei", "key": "file:1", "item": "abc"}])
    ev.close()
    assert rows[0]["sha256"] == versions.sha256_bytes(b"a") and rows[0]["captured"]
    rel = evidence.record_case(data, 7, rows)
    before = evidence.case_records(data, 7)
    assert before[0]["file"] == rel and before[0]["chained"] is None and before[0]["stamp"] is None
    assert "OneDrive/a.txt" in (data / "evidence" / rel).read_text(encoding="utf-8")
    c = evidence.sweep(data, roots(data))
    assert c["case"] == 1
    after = evidence.case_records(data, 7)
    assert after[0]["chained"] and lines(data)[-1]["for"] == rel
    assert lines(data)[-1]["sha256"] == versions.sha256_file(data / "evidence" / rel)
    assert evidence.case_records(data, 8) == []


def _store(teams_dir, conv, messages):
    db = state_db.StateDb(teams_dir)
    store = teams_export.Nachrichtenspeicher(db, conv)
    for batch in messages:
        store.merge(batch)
    store.sichern()


def _msg(mid, text, **extra):
    return {"id": mid, "messageType": "message", "createdDateTime": "2026-06-02T16:30:00Z",
            "from": {"user": {"displayName": "Bob Baumeister"}},
            "body": {"contentType": "text", "content": text}, **extra}


def test_a_messages_versions_are_its_earlier_texts(data):
    _store(data / "teams_export", "chat-bob", [
        [_msg("m1", "Everything passed.")],
        [_msg("m1", "Printer mapping fails.", lastModifiedDateTime="2026-06-02T17:05:00Z")]])
    found = evidence.message_versions(data / "teams_export", "teams:chat-bob#m1")
    assert [v["kind"] for v in found] == ["current", "earlier"]
    assert evidence.message_text(found[1]) == "Everything passed."
    assert found[0]["modified"] == "2026-06-02T17:05:00Z"
    assert evidence.message_versions(data / "teams_export", "teams:chat-bob#m2") is None
    assert evidence.message_versions(data / "teams_export", "mail:<x>") is None


def test_fingerprints_name_the_item_not_its_neighbours(data):
    board = data / "planner_export" / "board.html"
    card = '<details class="karte" id="k-{id}"><summary>{t}</summary></details>'
    board.write_text("<main>" + card.format(id="t1", t="Order docks") + card.format(id="t2", t="Plan wave 2")
                     + "</main>", encoding="utf-8")
    versions.write_text(data / "onedrive_export" / "a.txt", "a")
    _store(data / "teams_export", "chat-bob", [[_msg("m1", "Hello")]])
    evidence.sweep(data, roots(data))
    pin = evidence.pinner(data)
    items = [{"key": "planner:t1", "root": "planner", "rel": "board.html"},
             {"key": "file:1", "root": "onedrive", "rel": "a.txt"},
             {"key": "teams:chat-bob#m1", "root": "teams", "rel": "1on1/Bob.html"},
             {"key": "file:2", "root": "onedrive", "rel": "gone.txt"}]
    before = pin(items)
    assert before["file:1"] == versions.sha256_bytes(b"a")
    assert before["file:2"] is None
    # Another card changes: this one's print stays
    board.write_text(board.read_text(encoding="utf-8").replace("Plan wave 2", "Plan wave 3"), encoding="utf-8")
    assert pin(items)["planner:t1"] == before["planner:t1"]
    board.write_text(board.read_text(encoding="utf-8").replace("Order docks", "Order docks now"), encoding="utf-8")
    assert pin(items)["planner:t1"] != before["planner:t1"]
    _store(data / "teams_export", "chat-bob", [[_msg("m1", "Hello there")]])
    assert pin(items)["teams:chat-bob#m1"] != before["teams:chat-bob#m1"]


def test_the_step_speaks_through_the_log(data, capsys, monkeypatch):
    monkeypatch.delenv("EVIDENCE_TSA_URL", raising=False)
    (data / "onedrive_export" / "a.txt").write_text("a", encoding="utf-8")
    evidence.main([str(r) for r in roots(data)] + ["--data", str(data)])
    out = capsys.readouterr().out
    assert "run.evidence.first" in out and "run.evidence.initial" in out
    evidence.main([str(r) for r in roots(data)] + ["--data", str(data)])
    assert "run.evidence.first" not in capsys.readouterr().out


def test_microsofts_checksum_travels_into_the_chain_and_the_check(data, monkeypatch):
    a, b = data / "onedrive_export" / "a.txt", data / "onedrive_export" / "b.txt"
    versions.write_text(a, "a")
    versions.write_text(b, "b")
    evidence.sweep(data, roots(data))
    new_run(monkeypatch)
    versions.write_bytes(a, b"a2", extra={"quickxor": "qa", "ms_quickxor": "qa", "ms_match": True})
    versions.write_bytes(b, b"b2", extra={"quickxor": "qb", "ms_quickxor": "other", "ms_match": False})
    evidence.sweep(data, roots(data))
    line = next(d for d in lines(data) if d.get("rel") == "onedrive_export/b.txt" and d["kind"] == "write")
    assert (line["quickxor"], line["ms_quickxor"], line["ms_match"]) == ("qb", "other", False)
    ev = evidence.Evidence(data)
    current = ev.history("onedrive_export/a.txt")[0]
    assert current["current"] and current["ms_match"] is True and current["quickxor"] == "qa"
    ev.close()
    found = evidence.verify(data)
    assert found["ms_confirmed"] == 1 and found["ms_mismatch_n"] == 1
    assert found["ms_mismatch"][0] == {**found["ms_mismatch"][0], "rel": "onedrive_export/b.txt",
                                       "quickxor": "qb", "ms_quickxor": "other"}
    # The index from before the Microsoft fields is read afresh from the chain
    import sqlite3
    con = sqlite3.connect(data / "evidence" / "evidence.db")
    con.executescript("DROP TABLE history; CREATE TABLE history(n INTEGER PRIMARY KEY, rel TEXT, "
                      "sha256 TEXT, size INTEGER, at TEXT, kind TEXT, other TEXT, modified TEXT);")
    con.commit()
    con.close()
    assert evidence.verify(data)["ms_mismatch_n"] == 1


def test_the_apps_own_files_beside_the_bookkeeping_are_no_items(data):
    out = data / "onedrive_export"
    (out / "state.db").write_bytes(b"bookkeeping")
    (out / "kalender.db").write_bytes(b"manifest")
    (out / "kalender.db-wal").write_bytes(b"wal")
    mirrored = out / "Dateien" / "kalender.db"      # a user's file of that name counts
    mirrored.parent.mkdir()
    mirrored.write_bytes(b"theirs")
    evidence.sweep(data, roots(data))
    rels = {d.get("rel") for d in lines(data)}
    assert "onedrive_export/Dateien/kalender.db" in rels
    assert not {"onedrive_export/kalender.db", "onedrive_export/kalender.db-wal"} & rels
    (out / "kalender.db").write_bytes(b"manifest, next run")
    assert not evidence.sweep(data, roots(data))["outside"]


def test_a_bookkeeping_file_chained_before_is_dropped_quietly(data):
    out = data / "onedrive_export"
    (out / "kalender.db").write_bytes(b"manifest")
    evidence.sweep(data, roots(data))              # no state.db yet: chained as a file
    (out / "state.db").write_bytes(b"bookkeeping")
    (out / "kalender.db").write_bytes(b"manifest, changed")
    c = evidence.sweep(data, roots(data))
    assert (c["outside"], c["missing"], c["removed"]) == (0, 0, 0)
    found = evidence.verify(data)
    assert found["changed_n"] == 0 and found["missing_n"] == 0
    assert not [o for o in found["outside"] if o["rel"] == "onedrive_export/kalender.db"]


def test_the_bookkeeping_names_follow_the_app():
    import app
    import combined_search
    import folders
    assert combined_search.MANIFEST_DB in evidence.BOOKKEEPING
    assert {folders.DATEI, folders.KALENDER, folders.NOTIZBUECHER} <= evidence.BOOKKEEPING
    assert {n for names in app.ALT_STATE.values() for n in names} <= evidence.BOOKKEEPING
