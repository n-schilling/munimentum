"""The evidence on the HTTP surface, against the synthetic archive
(testdata/): an item's versions, one version's bytes, the diff of two,
the chain's state, a case's pinned versions, the manifest a closed case
writes – and the case export that carries all of it."""

import json
import shutil
import threading
import zipfile
from pathlib import Path
from urllib.parse import quote

import pytest

import app as app_mod
import case_export
import evidence
import faelle
import settings
import versions
from hilfen import call
from testdata import build as testdata_build
from testdata import history, sources


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """The synthetic archive, once for this module – with its history."""
    folder = tmp_path_factory.mktemp("evidence-archive")
    out = testdata_build.build(folder, flat=True)
    return out


@pytest.fixture
def archive(built, tmp_path, monkeypatch, with_ollama):
    """A copy per test – some of them close a case or change a file."""
    home = tmp_path / "archive"
    shutil.copytree(built["home"], home)
    for name, value in (("WURZEL", home), ("HEIM", home), ("BASE", home),
                        ("STORE_PFAD", home / app_mod.STORE_DIR),
                        ("CONFIG_FILE", home / "app_config.json"),
                        ("TOKEN_FILE", home / "gx_token.txt")):
        monkeypatch.setattr(app_mod, name, value)
    settings.reset()
    a = app_mod.App(app_mod.load_config())
    httpd = app_mod.make_server(a, 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield a, httpd.server_address[1], home, built
    httpd.shutdown()
    httpd.server_close()


def _uid(port, query, root):
    code, r = call(port, "GET", f"/api/v1/search?q={quote(query)}&limit=20")
    assert code == 200, r
    return next(h["uid"] for h in r["items"] if h.get("root") == root or h["uid"].startswith(root))


def test_an_items_versions_come_newest_first_with_their_capture(archive):
    _a, port, _home, built = archive
    uid = _uid(port, "rollout plan", "datei:Dateien/Documents/Ostwind/rollout-plan.md")
    code, r = call(port, "GET", f"/api/v1/documents/versions?uid={quote(uid)}")
    assert code == 200 and r["unit"] == "file" and r["format"] == "text"
    now, before = r["items"]
    assert now["current"] and not before["current"] and before["available"]
    assert before["sha256"] == built["earlier"]["onedrive_export/Dateien/Documents/Ostwind/rollout-plan.md"]
    assert before["modified"] < now["modified"] and before["captured"]
    assert before["kind"] == "initial" and now["kind"] == "write"


def test_one_version_and_its_difference_to_today(archive):
    _a, port, _home, built = archive
    uid = _uid(port, "rollout plan", "datei:Dateien/Documents/Ostwind/rollout-plan.md")
    sha = built["earlier"]["onedrive_export/Dateien/Documents/Ostwind/rollout-plan.md"]
    code, body = call(port, "GET", f"/api/v1/documents/versions/content?uid={quote(uid)}&sha={sha[:16]}")
    assert code == 200 and body == history.ROLLOUT_DRAFT_1
    code, r = call(port, "GET", f"/api/v1/documents/versions/diff?uid={quote(uid)}&sha={sha}")
    assert code == 200 and r["from"] == sha
    assert ["-", "1,"] in r["ops"] and ["+", "2,"] in r["ops"]
    assert "".join(t for op, t in r["ops"] if op != "-").startswith("# Ostwind rollout plan\n\nDraft 2")
    # The same version on both sides: its text in one piece
    code, r = call(port, "GET", f"/api/v1/documents/versions/diff?uid={quote(uid)}&sha={sha}&to={sha}")
    assert r["ops"] == [["=", history.ROLLOUT_DRAFT_1]]


def test_a_page_version_is_shown_in_a_sandbox(archive):
    _a, port, _home, built = archive
    rel = sources.page_rel(history._PROJECT_PAGE[1])
    sha = built["earlier"][f"{settings.SHAREPOINT_PAGES_DIR}/{rel}"]
    import http.client
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    con.request("GET", f"/api/v1/documents/versions/content?root=pages&rel={quote(rel)}&sha={sha}")
    resp = con.getresponse()
    body = resp.read().decode("utf-8")
    con.close()
    assert resp.status == 200 and resp.getheader("Content-Security-Policy") == "sandbox"
    assert resp.getheader("Content-Disposition") is None and "planned for 3 June" in body
    code, r = call(port, "GET", f"/api/v1/documents/versions/diff?root=pages&rel={quote(rel)}&sha={sha}")
    assert code == 200 and any(op == "+" and "finished" in t for op, t in r["ops"])


def test_a_messages_versions_are_its_earlier_texts(archive):
    _a, port, _home, _built = archive
    conv = next(c for c in sources.CONVERSATIONS if c["id"] == "chat-bob")
    q = (f"root=teams&rel={quote(sources._conversation_rel(conv))}"
         f"&key={quote('teams:chat-bob#msg-b5')}")
    code, r = call(port, "GET", f"/api/v1/documents/versions?{q}")
    assert code == 200 and r["unit"] == "message"
    assert [v["kind"] for v in r["items"]] == ["current", "earlier"]
    code, d = call(port, "GET", f"/api/v1/documents/versions/diff?{q}&sha={r['items'][1]['sha256']}")
    old = sources.EDITED["msg-b5"][0]
    assert "".join(t for op, t in d["ops"] if op != "+") == old
    # A message has no bytes of its own
    code, _ = call(port, "GET", f"/api/v1/documents/versions/content?{q}&sha={r['items'][1]['sha256']}")
    assert code == 404


def test_refusals_speak_the_vocabulary(archive):
    _a, port, _home, _built = archive
    assert call(port, "GET", "/api/v1/documents/versions")[0] == 400
    assert call(port, "GET", "/api/v1/documents/versions?uid=nope")[0] == 404
    assert call(port, "GET", "/api/v1/documents/versions?root=nowhere&rel=a")[0] == 404
    assert call(port, "GET", "/api/v1/documents/versions?root=onedrive&rel=../../etc")[0] == 404
    uid = _uid(port, "rollout plan", "datei:Dateien/Documents/Ostwind/rollout-plan.md")
    code, r = call(port, "GET", f"/api/v1/documents/versions/content?uid={quote(uid)}&sha=xyz")
    assert code == 400 and r["error"]["k"] == "srv.badparam"
    code, r = call(port, "GET", f"/api/v1/documents/versions/content?uid={quote(uid)}&sha={'0' * 64}")
    assert code == 404 and r["error"]["k"] == "srv.versions.none"
    png = _uid(port, "logo", "datei:Dateien/Pictures/logo.png")
    code, v = call(port, "GET", f"/api/v1/documents/versions?uid={quote(png)}")
    assert v["format"] == "binary"
    code, r = call(port, "GET", f"/api/v1/documents/versions/diff?uid={quote(png)}&sha={v['items'][0]['sha256']}")
    assert code == 404 and r["error"]["k"] == "srv.versions.notext"


def test_the_chain_state_for_the_settings_card(archive):
    _a, port, home, _built = archive
    code, r = call(port, "GET", "/api/v1/evidence")
    assert code == 200
    assert r["lines"] == evidence.Evidence(home).lines_on_disk() and len(r["head"]) == 64
    assert r["versions"] == len(history.KEPT) and r["stamped"] is None and r["since"]


def test_a_case_marks_what_changed_since_it_came_in(archive):
    _a, port, _home, _built = archive
    code, r = call(port, "GET", "/api/v1/cases/1")
    assert code == 200 and r["case"]["name"] == history.CASE_NAME
    items = r["case"]["item_list"]
    assert len(items) == len(history.EARLIER) + 1
    assert all(e["changed"] and e["pinned"] for e in items)
    assert r["case"]["evidence"] is None                 # open: no manifest


def test_an_item_taken_in_now_is_pinned_and_unchanged(archive):
    a, port, _home, _built = archive
    code, r = call(port, "POST", "/api/v1/cases", {"name": "Fresh"})
    case_id = r["case"]["id"]
    code, found = call(port, "GET", "/api/v1/search?q=offer&limit=3")
    # The rows as the page builds them from hits (eintragAus)
    rows = [{"key": h["key"], "src": h["source"], "root": h["root"], "rel": h["path"],
             "title": h["title"]} for h in found["items"][:2]]
    code, r = call(port, "POST", f"/api/v1/cases/{case_id}/items", {"items": rows})
    assert code in (200, 201), r
    items = r["case"]["item_list"]
    assert items and all(e["pinned"] and not e["changed"] for e in items)


def test_closing_a_case_writes_its_manifest(archive):
    a, port, home, _built = archive
    code, r = call(port, "PATCH", "/api/v1/cases/1", {"status": "closed"})
    assert code == 200
    ev = r["case"]["evidence"]
    assert ev and ev["chained"] is None and ev["stamp"] is None
    manifest = (home / "evidence" / ev["file"]).read_text(encoding="utf-8")
    assert manifest.startswith(",".join(evidence.MANIFEST_FIELDS))
    assert "onedrive_export/Dateien/Documents/Ostwind/rollout-plan.md" in manifest
    # The next run chains it
    evidence.sweep(home, [home / f for f, _i in settings.QUELLEN.values()])
    code, r = call(port, "GET", "/api/v1/cases/1")
    assert r["case"]["evidence"]["chained"]


def test_the_case_export_carries_the_evidence(archive):
    a, port, home, _built = archive
    book = faelle.Fallbuch(home / faelle.DB_NAME)
    pfade = {i: str(home / f) for f, i in settings.QUELLEN.values()}
    target = home / "export"
    zip_path, rows, missing = case_export.exportieren(book, 1, pfade, target, "en", str(Path(app_mod.RES)),
                                                     data=home)
    assert missing == 0
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        top = names[0].split("/")[0]

        def read(name):
            return z.read(f"{top}/{name}").decode("utf-8")
        sums = dict(reversed(line.split("  ", 1)) for line in read("SHA256SUMS.txt").splitlines())
        # Every file but the sums themselves, each with its right checksum
        files = [n[len(top) + 1:] for n in names if not n.endswith("/")]
        assert set(sums) == set(files) - {"SHA256SUMS.txt"}
        for name, sha in sums.items():
            assert versions.sha256_bytes(z.read(f"{top}/{name}")) == sha
        manifest = read("evidence/manifest.csv")
        assert "rollout-plan.md" in manifest and "item_sha256" in manifest
        assert read("evidence/chain.jsonl").splitlines()
        assert "shasum -a 256 -c SHA256SUMS.txt" in read("evidence/README.txt")
        # The version each changed item came in with, beside today's
        kept = [n for n in files if n.startswith("Versions/")]
        assert any(n.endswith(".md") for n in kept) and any(n.endswith(".html") for n in kept)
        assert any(n.endswith(".txt") and "msg-b5" in n for n in kept)
        md = next(n for n in kept if n.endswith(".md"))
        assert z.read(f"{top}/{md}").decode("utf-8") == history.ROLLOUT_DRAFT_1


def test_the_archive_report_names_the_file_changed_by_hand(archive):
    _a, port, _home, _built = archive
    code, r = call(port, "GET", "/api/v1/analytics")
    n = r["archiv"]["nachweis"]
    folder, rel, _extra = history.TAMPERED
    assert n["chain_ok"] and [c["rel"] for c in n["changed"]] == [f"{folder}/{rel}"]
    outside_folder, outside_rel, _ = history.OUTSIDE
    assert [o["rel"] for o in n["outside"]] == [f"{outside_folder}/{outside_rel}"]


def test_fetch_again_takes_the_changed_file_along(archive, monkeypatch):
    a, port, home, _built = archive
    monkeypatch.setattr(app_mod, "read_token", lambda *x, **kw: "tok")
    seen = {}
    monkeypatch.setattr(a.jobs, "start", lambda steps, label, **kw: seen.update(steps=steps) or True)
    code, r = call(port, "POST", "/api/v1/sources/onedrive/refetch", {})
    assert code == 202, r
    listing = json.loads((home / "nachholen-onedrive.json").read_text(encoding="utf-8"))
    assert history.TAMPERED[1] in listing["dateien"]
    assert [s["key"] for s in seen["steps"]][:2] == ["onedrive", "evidence"]


def test_claude_reads_an_items_versions(archive):
    """MCP get_document names the versions – a file's and a message's."""
    a, port, _home, _built = archive
    mod = a.search.ensure(a.cfg)
    uid = _uid(port, "rollout plan", "datei:Dateien/Documents/Ostwind/rollout-plan.md")
    doc = mod.get_document(uid=uid)
    assert [v["current"] for v in doc["versions"]] == [True, False]
    assert doc["versions"][1]["available"] and doc["versions"][1]["captured"]
    message = _uid(port, "printer mapping fails", "teams:1on1/")
    doc = mod.get_document(uid=message)
    earlier = [v for v in doc["versions"] if not v["current"]]
    assert earlier and earlier[0]["text"] == sources.EDITED["msg-b5"][0]


def test_the_facts_carry_the_checksums(archive):
    _a, port, home, _built = archive
    uid = _uid(port, "rollout plan", "datei:Dateien/Documents/Ostwind/rollout-plan.md")
    code, f = call(port, "GET", f"/api/v1/documents/facts?uid={quote(uid)}")
    c = f["checksums"]
    path = home / "onedrive_export/Dateien/Documents/Ostwind/rollout-plan.md"
    assert c["sha256"] == versions.sha256_file(path)
    assert c["quickxor"] == c["microsoft_quickxor"] == versions.quick_xor_file(path)
    assert c["microsoft_match"] is True
    risk = _uid(port, "risk log", "sharepoint:Nordwind/Documents/Dateien/Ostwind/risk-log.csv")
    code, f = call(port, "GET", f"/api/v1/documents/facts?uid={quote(risk)}")
    assert f["checksums"]["microsoft_match"] is False
    assert f["checksums"]["microsoft_quickxor"] != f["checksums"]["quickxor"]
    # A mail: our checksum alone – Microsoft gives none for mail
    mail = _uid(port, "rollout plan", "outlook:")
    code, f = call(port, "GET", f"/api/v1/documents/facts?uid={quote(mail)}")
    assert len(f["checksums"]["sha256"]) == 64 and "microsoft_quickxor" not in f["checksums"]
    # A chat message is part of a file of many: no file checksum for it
    chat = _uid(port, "printer mapping fails", "teams:1on1/")
    code, f = call(port, "GET", f"/api/v1/documents/facts?uid={quote(chat)}")
    assert "checksums" not in f


def test_a_versions_row_names_microsofts_checksum(archive):
    _a, port, _home, _built = archive
    uid = _uid(port, "rollout plan", "datei:Dateien/Documents/Ostwind/rollout-plan.md")
    code, r = call(port, "GET", f"/api/v1/documents/versions?uid={quote(uid)}")
    now, before = r["items"]
    assert now["microsoft_match"] is True and now["quickxor"] == now["microsoft_quickxor"]
    assert "microsoft_quickxor" not in before          # the first run knew no Microsoft checksum


def test_the_report_names_the_file_microsoft_disagrees_with(archive):
    _a, port, _home, _built = archive
    code, r = call(port, "GET", "/api/v1/analytics")
    n = r["archiv"]["nachweis"]
    assert [m["rel"] for m in n["ms_mismatch"]] == [k for k, v in history.MICROSOFT.items() if v == "mismatch"]
    assert n["ms_confirmed"] == sum(1 for v in history.MICROSOFT.values() if v == "match")


def test_a_file_is_found_by_its_place(archive):
    _a, port, _home, _built = archive
    rel = "Nordwind/Documents/Dateien/Ostwind/risk-log.csv"
    code, r = call(port, "GET", f"/api/v1/documents?root=sharepoint&rel={quote(rel)}")
    assert code == 200 and r["path"] == rel and r["root"] == "sharepoint" and r["key"]
    code, r = call(port, "GET", "/api/v1/documents?root=sharepoint&rel=nowhere.csv")
    assert code == 404 and r["error"]["k"] == "srv.detail.none"


# --------------------------------------------------------------------------
# Over MCP: a citation, and an item held against the chain (verify_item)
# --------------------------------------------------------------------------
def _file_uid(port, rel):
    name = Path(rel).stem
    return _uid(port, name.replace("-", " "), f"datei:{rel}")


def _stamp(home, tsa="https://tsa.example"):
    """A stamp line on the chain's head, as stamp_head writes one – no
    authority asked."""
    ev = evidence.Evidence(home)
    n, head = ev.head()
    line = ev.append({"kind": "stamp", "head": head, "lines": n, "tsa": tsa,
                      "sha256": versions.sha256_bytes(evidence.head_text(n, head, "x").encode())})
    ev.flush()
    ev.close()
    return line


def test_claude_verifies_an_item_against_the_chain(archive):
    a, port, home, _built = archive
    mod = a.search.ensure(a.cfg)
    plan = _file_uid(port, "Dateien/Documents/Ostwind/rollout-plan.md")
    before = mod.verify_item(uid=plan)
    assert before["verdict"] == "unchanged" and before["stamp"] is None
    assert before["chain"]["intact"] and before["versions"] == 2 and before["microsoft_match"] is True
    assert before["sha256"] == before["recorded"]["sha256"]
    assert "No time-stamp covers that line yet" in before["summary"]
    stamp = _stamp(home)
    after = mod.verify_item(key=before["key"])
    assert after["verdict"] == "unchanged" and after["uid"] == plan
    assert after["stamp"] == {"line": stamp["n"], "at": stamp["at"], "tsa": "https://tsa.example"}
    assert "time-stamped past that line" in after["summary"]
    # Changed by hand after the last run – and before it: the chain
    # recorded that version as a change made outside the app.
    tampered = mod.verify_item(uid=_file_uid(port, history.TAMPERED[1]))
    assert tampered["verdict"] == "changed_outside"
    assert tampered["sha256"] != tampered["recorded"]["sha256"]
    outside = mod.verify_item(uid=_file_uid(port, history.OUTSIDE[1]))
    assert outside["verdict"] == "unchanged_outside_version"
    assert mod.verify_item(uid="nowhere:0")["error"]
    assert mod.verify_item()["error"]


def test_a_version_the_app_wrote_is_not_an_outside_change(archive):
    a, port, home, _built = archive
    mod = a.search.ensure(a.cfg)
    rel = "Dateien/Documents/Ostwind/rollout-plan.md"
    uid = _file_uid(port, rel)
    path = home / settings.ONEDRIVE_DIR / rel
    path.write_text("a newer version\n", encoding="utf-8")
    assert mod.verify_item(uid=uid)["verdict"] == "changed_outside"
    pending = home / evidence.EVIDENCE_DIRNAME / versions.PENDING_DIRNAME
    pending.mkdir(parents=True, exist_ok=True)
    (pending / "1-1.jsonl").write_text(json.dumps(
        {"op": "write", "rel": f"{settings.ONEDRIVE_DIR}/{rel}",
         "sha256": versions.sha256_file(path)}) + "\n", encoding="utf-8")
    assert mod.verify_item(uid=uid)["verdict"] == "changed_by_app"
    path.unlink()
    assert mod.verify_item(uid=uid)["verdict"] == "missing"


def test_a_broken_chain_proves_nothing(archive):
    a, port, home, _built = archive
    mod = a.search.ensure(a.cfg)
    chain = home / evidence.EVIDENCE_DIRNAME / evidence.CHAIN
    lines = chain.read_text(encoding="utf-8").splitlines()
    lines[3] = lines[3].replace('"at":"', '"at":"1', 1)
    chain.write_text("\n".join(lines) + "\n", encoding="utf-8")
    v = mod.verify_item(uid=_file_uid(port, "Dateien/Documents/Ostwind/rollout-plan.md"))
    assert v["verdict"] == "chain_broken" and v["chain"]["broken_at"] == 5


def test_a_citation_links_into_the_running_app_and_only_then(archive, monkeypatch):
    import instance
    a, port, home, _built = archive
    mod = a.search.ensure(a.cfg)
    monkeypatch.setenv("MUNIMENTUM_HOME", str(home))
    monkeypatch.setitem(mod._APP, "at", None)
    uid = _file_uid(port, "Dateien/Documents/Ostwind/rollout-plan.md")
    cite = mod._with_cite(mod.get_document(uid=uid))["cite"]
    assert cite["link"] is None and cite["app"] == "not_running"
    assert cite["label"].startswith("OneDrive · ") and cite["label"].endswith("rollout-plan.md")
    assert cite["sha256"] and cite["chain_line"] and cite["captured"] and cite["stamped"] is None
    instance.write(home, port, app_mod.PROFIL)
    monkeypatch.setitem(mod._APP, "at", None)
    cite = mod._with_cite(mod.get_document(uid=uid))["cite"]
    # The version read now is pinned in the link – unchanged here, so the
    # one the chain recorded.
    assert cite["link"] == (f"http://127.0.0.1:{port}/#item={quote(cite['key'], safe='')}"
                            f"&sha={cite['sha256']}")
    assert "app" not in cite
    # What the link opens: the page asks for the item by its key.
    code, r = call(port, "GET", f"/api/v1/documents?key={quote(cite['key'], safe='')}")
    assert code == 200 and r["uid"] == uid
    code, r = call(port, "GET", "/api/v1/documents?key=nowhere")
    assert code == 404 and r["error"]["k"] == "srv.detail.none"
    # Another profile on that port: no link into the wrong archive.
    instance.write(home, port, "nordwind")
    monkeypatch.setitem(mod._APP, "at", None)
    assert mod._with_cite(mod.get_document(uid=uid))["cite"]["app"] == "other_profile"


def test_a_message_is_cited_with_its_own_checksum(archive):
    a, port, _home, _built = archive
    mod = a.search.ensure(a.cfg)
    message = _uid(port, "printer mapping fails", "teams:1on1/")
    cite = mod._with_cite(mod.get_document(uid=message))["cite"]
    versions_ = mod.get_document(uid=message)["versions"]
    assert cite["item_sha256"] == next(v["sha256"] for v in versions_ if v["current"])
    assert mod.verify_item(uid=message)["item_sha256"] == cite["item_sha256"]


def test_a_link_pins_what_the_page_lists_as_versions(archive, monkeypatch):
    """The checksum in a citation's link is one the page finds among the
    item's versions: a message's words, a file's bytes, a task's board."""
    import instance
    a, port, home, _built = archive
    mod = a.search.ensure(a.cfg)
    monkeypatch.setenv("MUNIMENTUM_HOME", str(home))
    instance.write(home, port, app_mod.PROFIL)
    monkeypatch.setitem(mod._APP, "at", None)
    for uid in (_uid(port, "printer mapping fails", "teams:1on1/"),
                _file_uid(port, history.TAMPERED[1]),
                mod.browse_messages(source="planner", k=1)["results"][0]["uid"]):
        link = mod._with_cite(mod.get_document(uid=uid))["cite"]["link"]
        pin = link.rsplit("&sha=", 1)[1]
        code, r = call(port, "GET", f"/api/v1/documents/versions?uid={quote(uid)}")
        assert code == 200
        assert next(v["sha256"] for v in r["items"] if v["current"]) == pin, uid
