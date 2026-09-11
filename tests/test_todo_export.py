"""todo_export.py – lists, tasks with their steps, and the tombstone lane.

The Graph side is faked throughout; what matters here is the export's own
part: the delta round per list, etag-driven refresh, attachments next to
the list, the rules over the list titles, and the greyed section for tasks
that left the list.
"""

import json

import pytest

import progress
import state_db
import todo_export as td


@pytest.fixture(autouse=True)
def _feste_umgebung(monkeypatch):
    """The module reads the app's settings file for what the environment
    does not say – pinned here, so a developer's own config never steers a
    test: no rules, no cadence, no "Sync now"."""
    monkeypatch.setenv("TODO_RULES", "")
    monkeypatch.setenv("SYNC_CADENCE", "{}")
    monkeypatch.delenv("SYNC_NOW", raising=False)


def _events(capsys):
    return [e for e in (progress.lies_event(z) for z in
                        capsys.readouterr().out.splitlines()) if e]


class _Graph:
    """URL -> response; paged() yields value lists, get() the object."""

    def __init__(self, antworten):
        self.antworten = antworten
        self.aufrufe = []
        self.geladen = []

    def _finde(self, url):
        for muster, antwort in self.antworten.items():
            if muster in url:
                return antwort
        raise AssertionError(f"unerwartete URL: {url}")

    def get(self, url):
        self.aufrufe.append(url)
        antwort = self._finde(url)
        if isinstance(antwort, Exception):
            raise antwort
        return antwort

    def paged(self, url):
        d = self.get(url)
        return iter(d.get("value", []))

    def get_bytes(self, url, label=""):
        self.geladen.append(url)
        return b"PDF", "application/pdf"


class _Antwort:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


class _HttpFehler(Exception):
    """What graph_client raises for a refused request: the response rides
    along, status and body readable."""

    def __init__(self, status, text=""):
        super().__init__(f"HTTP {status}")
        self.response = _Antwort(status, text)


def _task(tid, titel, etag="e1", status="notStarted", **extra):
    return {"id": tid, "title": titel, "@odata.etag": etag, "status": status,
            "importance": "normal", "body": {"content": "", "contentType": "text"},
            "createdDateTime": "2026-07-01T10:00:00Z", **extra}


LISTE = {"id": "l1", "titel": "Einkauf", "art": "none", "geteilt": False}


def _graph(tasks, anhaenge=None):
    return _Graph({
        "/me/todo/lists/l1/tasks/t1/attachments": {"value": anhaenge or []},
        "/me/todo/lists/l1/tasks": {"value": tasks},
    })


def test_list_lists_stellt_die_standardliste_voran():
    g = _Graph({"/me/todo/lists": {"value": [
        {"id": "b", "displayName": "Zeta"},
        {"id": "a", "displayName": "Aufgaben", "wellknownListName": "defaultList"},
        {"id": "c", "displayName": "Alpha", "isShared": True}]}})
    listen = td.list_lists(g)
    assert [e["titel"] for e in listen] == ["Aufgaben", "Alpha", "Zeta"]
    assert listen[1]["geteilt"] is True and listen[0]["art"] == "defaultList"


def test_list_lauf_rendert_schritte_notizen_und_links(tmp_path):
    g = _graph([
        _task("t1", "Milch kaufen", importance="high",
              dueDateTime={"dateTime": "2026-08-01T00:00:00.0000000", "timeZone": "UTC"},
              body={"content": "Bio, <b>1,5 %</b><script>x()</script>",
                    "contentType": "html"},
              checklistItems=[{"displayName": "Vollmilch", "isChecked": True},
                              {"displayName": "Hafermilch", "isChecked": False}],
              linkedResources=[{"displayName": "Rezept", "webUrl": "https://example.com/r"}],
              categories=["Haushalt"]),
        _task("t2", "Alt", status="completed",
              completedDateTime={"dateTime": "2026-07-02T00:00:00", "timeZone": "UTC"}),
    ])
    neu, unveraendert, fehler = td.list_lauf(g, tmp_path, LISTE)
    assert (neu, unveraendert, fehler) == (2, 0, 0)
    html = (td.list_ziel(tmp_path, LISTE) / "list.html").read_text(encoding="utf-8")
    assert "Milch kaufen" in html and "fällig 2026-08-01" in html
    assert "wichtig" in html and "Haushalt" in html
    assert "☑ Vollmilch" in html and "☐ Hafermilch" in html
    assert 'href="https://example.com/r"' in html and "Rezept" in html
    assert "<b>1,5 %</b>" in html and "x()" not in html, "Skripte müssen draußen bleiben"
    assert "Offen (1)" in html and "Erledigt (1)" in html
    assert 'class="karte erledigt"' in html


def test_unveraenderte_tasks_bleiben_ohne_abrufe(tmp_path):
    g = _graph([_task("t1", "Milch kaufen", hasAttachments=True)],
               anhaenge=[{"id": "a1", "name": "Bon.pdf", "size": 3}])
    td.list_lauf(g, tmp_path, LISTE)
    assert len(g.geladen) == 1
    ziel = td.list_ziel(tmp_path, LISTE)
    dateien = list((ziel / td.ANHANG_DIR).glob("*"))
    assert len(dateien) == 1 and dateien[0].read_bytes() == b"PDF"
    html = (ziel / "list.html").read_text(encoding="utf-8")
    assert f'href="{td.ANHANG_DIR}/' in html and "Bon.pdf" in html

    g2 = _graph([_task("t1", "Milch kaufen", hasAttachments=True)],
                anhaenge=[{"id": "a1", "name": "Bon.pdf", "size": 3}])
    neu, unveraendert, fehler = td.list_lauf(g2, tmp_path, LISTE)
    assert (neu, unveraendert) == (0, 1)
    assert g2.geladen == [], "Anhang trotz gleichem etag erneut geladen"


def test_verschwundene_task_bleibt_als_grabstein(tmp_path):
    td.list_lauf(_graph([_task("t1", "A"), _task("t2", "B")]), tmp_path, LISTE)
    td.list_lauf(_graph([_task("t1", "A")]), tmp_path, LISTE)
    db = state_db.StateDb(td.list_ziel(tmp_path, LISTE))
    eintraege = json.loads(db.kv_lesen("tasks"))
    assert eintraege["t2"]["deleted"] and not eintraege["t1"]["deleted"]
    html = (td.list_ziel(tmp_path, LISTE) / "list.html").read_text(encoding="utf-8")
    assert "Nicht mehr in der Liste" in html and ">B<" in html


def test_fehler_beim_task_verhindert_grabsteine(tmp_path):
    td.list_lauf(_graph([_task("t1", "A"), _task("t2", "B")]), tmp_path, LISTE)
    g = _graph([_task("t1", "A", etag="e2", hasAttachments=True)])
    g.antworten["/me/todo/lists/l1/tasks/t1/attachments"] = RuntimeError("kaputt")
    neu, unveraendert, fehler = td.list_lauf(g, tmp_path, LISTE)
    assert fehler == 1
    eintraege = json.loads(state_db.StateDb(td.list_ziel(tmp_path, LISTE))
                           .kv_lesen("tasks"))
    assert not eintraege["t2"]["deleted"], \
        "ein Lauf mit Fehlern darf nichts für verschwunden erklären"


def test_lauf_meldet_ergebnis_und_kaputte_listen(tmp_path, capsys):
    g = _graph([_task("t1", "A")])
    g.antworten["/me/todo/lists/l2/tasks"] = RuntimeError("weg")
    td.lauf(g, tmp_path, [LISTE, dict(LISTE, id="l2", titel="Kaputt")])
    ausgabe = capsys.readouterr().out
    ergebnis = [e for e in (progress.lies_ergebnis(z) for z in ausgabe.splitlines()) if e]
    assert ergebnis and ergebnis[-1]["new"] == 1 and ergebnis[-1]["errors"] == 1
    assert ergebnis[-1]["extra"]["lists"] == 2
    assert any(progress.lies_event(z) and progress.lies_event(z)["k"] == "run.todo.list_failed"
               for z in ausgabe.splitlines())


def test_wiederholung_und_erinnerung_stehen_in_der_zeile():
    eintrag = {"task": _task("t1", "Wasser", isReminderOn=True,
                             reminderDateTime={"dateTime": "2026-08-03T08:00:00"},
                             recurrence={"pattern": {"type": "weekly", "interval": 2,
                                                     "daysOfWeek": ["monday"]}})}
    html = td._task_html(eintrag)
    assert "Erinnerung 2026-08-03" in html
    assert "wiederkehrend: alle 2 (wöchentlich), monday" in html


def test_corpus_liest_tasks_mit_schritten_und_anhaengen(tmp_path):
    import corpus
    g = _graph([_task("t1", "Vertrag verlängern", hasAttachments=True,
                      body={"content": "<p>Bis <b>August</b></p>", "contentType": "html"},
                      checklistItems=[{"displayName": "Angebot lesen", "isChecked": False}],
                      linkedResources=[{"displayName": "Mail von Alice", "webUrl": "https://example.com/m"}],
                      lastModifiedDateTime="2026-07-03T09:00:00Z"),
                _task("t2", "Alt", status="completed")],
               anhaenge=[{"id": "a1", "name": "Angebot 2026.pdf", "size": 3}])
    td.list_lauf(g, tmp_path, LISTE)
    td.list_lauf(_graph([_task("t1", "Vertrag verlängern", hasAttachments=True)],
                        anhaenge=[{"id": "a1", "name": "Angebot 2026.pdf", "size": 3}]),
                 tmp_path, LISTE)
    saetze = sorted(corpus.load_todo(tmp_path), key=lambda s: s["uid"])
    assert [s["title"] for s in saetze] == ["Vertrag verlängern", "Alt"]
    satz = saetze[0]
    assert satz["src"] == "todo" and satz["root"] == "todo" and satz["ctx"] == "Einkauf"
    assert "Bis August" in satz["text"] and "Angebot lesen" in satz["text"]
    assert "Mail von Alice" in satz["text"] and "<b>" not in satz["text"]
    assert satz["att"] == "Angebot_2026.pdf" and satz["date"].startswith("2026-07-03")
    assert satz["rel"].endswith("/list.html") and "gone" not in satz
    assert saetze[1]["gone"], "die verschwundene Task trägt ihren Marker"
    assert corpus.load_records(None, None, todo_dir=tmp_path)[0]["src"] == "todo"


# ---------------------------------------------------------------------------
# 9.0: delta per list, rules, --lists, source cadence, quiet rewrites,
# lists side by side
# ---------------------------------------------------------------------------
LINK1 = f"{td.GRAPH}/me/todo/lists/l1/tasks/delta?$deltatoken=abc"
LINK2 = f"{td.GRAPH}/me/todo/lists/l1/tasks/delta?$deltatoken=def"


def _ergebnis(ausgabe):
    return [e for e in (progress.lies_ergebnis(z) for z in ausgabe.splitlines())
            if e][-1]


def test_delta_link_wird_nach_sauberem_lauf_gemerkt_und_dann_benutzt(tmp_path):
    g = _Graph({
        "/me/todo/lists/l1/tasks/t2/attachments": {"value": []},
        "/me/todo/lists/l1/tasks/delta": {
            "value": [_task("t1", "A"), _task("t2", "B")],
            "@odata.deltaLink": LINK1},
    })
    assert td.list_lauf(g, tmp_path, LISTE) == (2, 0, 0)
    assert "$expand=checklistItems,linkedResources" in g.aufrufe[0]
    db = state_db.StateDb(td.list_ziel(tmp_path, LISTE))
    assert db.kv_lesen("delta:l1") == LINK1
    # Second round: only what moved comes, asked via the stored link; a
    # removed task moves to the greyed section.
    g2 = _Graph({
        "/me/todo/lists/l1/tasks/t2/attachments": {"value": []},
        "deltatoken=abc": {
            "value": [_task("t2", "B neu", etag="e2"),
                      {"id": "t1", "@removed": {"reason": "deleted"}}],
            "@odata.deltaLink": LINK2},
    })
    assert td.list_lauf(g2, tmp_path, LISTE) == (1, 0, 0)
    assert g2.aufrufe == [LINK1]
    eintraege = json.loads(db.kv_lesen("tasks"))
    assert eintraege["t1"]["deleted"]
    assert eintraege["t2"]["task"]["title"] == "B neu"
    assert db.kv_lesen("delta:l1") == LINK2
    html = (td.list_ziel(tmp_path, LISTE) / "list.html").read_text(encoding="utf-8")
    assert "Nicht mehr in der Liste" in html and "B neu" in html
    # Third round, nothing moved: the standing task counts as unchanged.
    g3 = _Graph({"deltatoken=def": {"value": [], "@odata.deltaLink": LINK2}})
    assert td.list_lauf(g3, tmp_path, LISTE) == (0, 1, 0)


def test_delta_link_rueckt_nach_fehlern_nicht_vor(tmp_path):
    g = _Graph({"/me/todo/lists/l1/tasks/delta": {
        "value": [_task("t1", "A")], "@odata.deltaLink": LINK1}})
    td.list_lauf(g, tmp_path, LISTE)
    db = state_db.StateDb(td.list_ziel(tmp_path, LISTE))
    g2 = _Graph({
        "/me/todo/lists/l1/tasks/t1/attachments": RuntimeError("kaputt"),
        "deltatoken=abc": {
            "value": [_task("t1", "A", etag="e2", hasAttachments=True)],
            "@odata.deltaLink": LINK2},
    })
    assert td.list_lauf(g2, tmp_path, LISTE)[2] == 1
    assert db.kv_lesen("delta:l1") == LINK1, \
        "a run with errors must not advance the pointer"
    # The next run replays the round – and the task arrives.
    g3 = _Graph({
        "/me/todo/lists/l1/tasks/t1/attachments": {"value": []},
        "deltatoken=abc": {
            "value": [_task("t1", "A", etag="e2", hasAttachments=True)],
            "@odata.deltaLink": LINK2},
    })
    assert td.list_lauf(g3, tmp_path, LISTE) == (1, 0, 0)
    assert db.kv_lesen("delta:l1") == LINK2


def test_verfallener_delta_zeiger_liest_einmal_voll(tmp_path, capsys):
    """410 Gone, or the syncStateNotFound the docs name for Outlook-backed
    feeds: the token is dropped, the list read once in full – and absence
    in that full read is the deletion signal again."""
    g = _Graph({"/me/todo/lists/l1/tasks/delta": {
        "value": [_task("t1", "A"), _task("t2", "B")],
        "@odata.deltaLink": LINK1}})
    td.list_lauf(g, tmp_path, LISTE)
    db = state_db.StateDb(td.list_ziel(tmp_path, LISTE))
    for fehler in (_HttpFehler(410),
                   _HttpFehler(400, '{"error":{"code":"SyncStateNotFound"}}')):
        db.kv_schreiben("delta:l1", LINK1)
        g2 = _Graph({
            "deltatoken=abc": fehler,
            "/me/todo/lists/l1/tasks/delta": {
                "value": [_task("t1", "A")], "@odata.deltaLink": LINK2},
        })
        assert td.list_lauf(g2, tmp_path, LISTE) == (0, 1, 0)
        assert g2.aufrufe[0] == LINK1 and "/tasks/delta?$expand" in g2.aufrufe[1]
        assert any(e["k"] == "run.todo.delta_reset" and
                   e["v"]["name"] == "Einkauf" for e in _events(capsys))
        assert db.kv_lesen("delta:l1") == LINK2
    assert json.loads(db.kv_lesen("tasks"))["t2"]["deleted"]
    # Any other refusal is the list's failure, not a reset.
    db.kv_schreiben("delta:l1", LINK1)
    with pytest.raises(RuntimeError):
        td.list_lauf(_Graph({"deltatoken=abc": RuntimeError("504")}),
                     tmp_path, LISTE)
    assert db.kv_lesen("delta:l1") == LINK1


def test_regeln_lassen_listen_aus_und_ruehren_alte_nicht_an(tmp_path, monkeypatch,
                                                            capsys):
    andere = dict(LISTE, id="l2", titel="Projekte")

    def fake():
        return _Graph({"/me/todo/lists/l1/tasks": {"value": [_task("t1", "A")]},
                       "/me/todo/lists/l2/tasks": {"value": [_task("t9", "Z")]}})

    td.lauf(fake(), tmp_path, [LISTE, andere])
    datei = td.list_ziel(tmp_path, LISTE) / "list.html"
    assert datei.exists() and (td.list_ziel(tmp_path, andere) / "list.html").exists()
    stand = (datei.read_bytes(), datei.stat().st_mtime_ns)
    # The rules speak the folder's name without the id suffix.
    monkeypatch.setenv("TODO_RULES", "- Einkauf")
    g = fake()
    td.lauf(g, tmp_path, [LISTE, andere])
    ergebnis = _ergebnis(capsys.readouterr().out)
    assert ergebnis["excluded"] == 1 and ergebnis["extra"]["lists"] == 1
    assert not any("/lists/l1/" in u for u in g.aufrufe)
    assert (datei.read_bytes(), datei.stat().st_mtime_ns) == stand, \
        "an excluded list stays on disk as it was"
    # Last matching rule wins – gitignore style.
    monkeypatch.setenv("TODO_RULES", "- **\n+ Einkauf")
    g = fake()
    td.lauf(g, tmp_path, [LISTE, andere])
    ergebnis = _ergebnis(capsys.readouterr().out)
    assert ergebnis["excluded"] == 1 and ergebnis["extra"]["lists"] == 1
    assert any("/lists/l1/" in u for u in g.aufrufe)
    assert not any("/lists/l2/" in u for u in g.aufrufe)


def test_gleiche_listen_ab_speichert_den_baum_mit_ordnernamen(tmp_path, monkeypatch,
                                                              capsys):
    import folders
    g = _Graph({"/me/todo/lists": {"value": [
        {"id": "b", "displayName": "Einkauf: Woche"},
        {"id": "a", "displayName": "Aufgaben", "wellknownListName": "defaultList"}]}})
    td.gleiche_listen_ab(g, tmp_path)
    daten = folders.lade(tmp_path)
    assert [e["pfad"] for e in daten["ordner"]] == ["Aufgaben", "Einkauf_ Woche"]
    assert daten["ordner"][1]["ordner"] == \
        f"Einkauf_ Woche__{td.export_util.kuerzel('b')}"
    assert daten["ordner"][0]["standard"] is True and daten["neu"] == []
    assert all(e["elemente"] == 0 for e in daten["ordner"])
    ereignisse = _events(capsys)
    assert any(e["k"] == "run.sync.result" and e["v"]["total"] == 2 and
               e["v"]["chosen"] == 2 and
               e["v"]["unit"]["k"] == "progress.unit.lists" for e in ereignisse)
    assert not list(tmp_path.rglob("list.html")), "--lists exports nothing"
    assert g.aufrufe == [f"{td.GRAPH}/me/todo/lists"]
    # A list added later is new; the rules count against the paths.
    monkeypatch.setenv("TODO_RULES", "- Aufgaben")
    g2 = _Graph({"/me/todo/lists": {"value": [
        {"id": "a", "displayName": "Aufgaben", "wellknownListName": "defaultList"},
        {"id": "b", "displayName": "Einkauf: Woche"},
        {"id": "c", "displayName": "Neu"}]}})
    td.gleiche_listen_ab(g2, tmp_path)
    ausgabe = capsys.readouterr().out
    ereignisse = [e for e in (progress.lies_event(z) for z in ausgabe.splitlines()) if e]
    assert any(e["k"] == "run.sync.changed" and e["v"]["new"] == 1
               for e in ereignisse)
    assert any(e["k"] == "run.sync.result" and e["v"]["chosen"] == 2
               for e in ereignisse)
    ergebnis = _ergebnis(ausgabe)
    assert ergebnis["new"] == 1 and ergebnis["extra"] == \
        {"total": 3, "chosen": 2, "gone": 0}
    assert folders.lade(tmp_path)["neu"] == ["Neu"]


def test_quelle_faellig_haelt_die_kadenz_vor_jedem_listing(tmp_path, monkeypatch,
                                                           capsys):
    """No list overrides: the source's cadence is decided before any
    listing – quelle_faellig needs no Graph at all."""
    import time
    monkeypatch.setenv("SYNC_CADENCE", json.dumps({"todo": "weekly"}))
    assert td.quelle_faellig(tmp_path), "without a last run the source is due"
    wurzel = state_db.StateDb(tmp_path)
    wurzel.kv_schreiben("last_sync", str(time.time()))
    assert not td.quelle_faellig(tmp_path)
    skip = [e for e in _events(capsys) if e["k"] == "run.cadence.skip"]
    assert skip and skip[0]["v"]["name"] == {"k": "settings.todo.title", "v": {}}
    assert skip[0]["v"]["cadence"]["k"] == "cadence.weekly"
    monkeypatch.setenv("SYNC_NOW", "1")
    assert td.quelle_faellig(tmp_path)
    monkeypatch.delenv("SYNC_NOW")
    # One list with a cadence of its own: the lists decide, the source
    # gate steps aside even though the source is not due.
    monkeypatch.setenv("SYNC_CADENCE",
                       json.dumps({"todo": "weekly", "todo:Projekte": "always"}))
    assert td.quelle_faellig(tmp_path)
    assert not _events(capsys)


def _zwei_listen():
    return _Graph({"/me/todo/lists/l1/tasks": {"value": [_task("t1", "A")]},
                   "/me/todo/lists/l2/tasks": {"value": [_task("t9", "Z")]}})


PROJEKTE = dict(LISTE, id="l2", titel="Projekte")


def test_listenkadenz_haelt_einzelne_listen_zurueck(tmp_path, monkeypatch, capsys):
    """A weekly list is held back while the others run – one line for all
    held-back lists, never one each; "Sync now" steps over every gate."""
    import time
    monkeypatch.setenv("SYNC_CADENCE",
                       json.dumps({"todo": "always", "todo:Einkauf": "weekly"}))
    td.lauf(_zwei_listen(), tmp_path, [LISTE, PROJEKTE])
    wurzel = state_db.StateDb(tmp_path)
    assert float(wurzel.kv_lesen("last_sync:l1")) > time.time() - 60
    assert float(wurzel.kv_lesen("last_sync:l2")) > time.time() - 60
    assert float(wurzel.kv_lesen("last_sync")) > time.time() - 60
    capsys.readouterr()
    g = _zwei_listen()
    td.lauf(g, tmp_path, [LISTE, PROJEKTE])
    ausgabe = capsys.readouterr().out
    ereignisse = [e for e in (progress.lies_event(z) for z in ausgabe.splitlines()) if e]
    assert [e["v"] for e in ereignisse if e["k"] == "run.todo.paced"] == [{"n": 1}]
    assert not any(e["k"] == "run.cadence.skip" for e in ereignisse)
    assert not any("/lists/l1/" in u for u in g.aufrufe)
    assert any("/lists/l2/" in u for u in g.aufrufe)
    ergebnis = _ergebnis(ausgabe)
    assert ergebnis["extra"] == {"lists": 1, "skipped": 1}
    monkeypatch.setenv("SYNC_NOW", "1")
    g = _zwei_listen()
    td.lauf(g, tmp_path, [LISTE, PROJEKTE])
    ausgabe = capsys.readouterr().out
    assert any("/lists/l1/" in u for u in g.aufrufe)
    assert not any((progress.lies_event(z) or {}).get("k") in
                   ("run.todo.paced", "run.cadence.skip")
                   for z in ausgabe.splitlines())
    assert _ergebnis(ausgabe)["extra"] == {"lists": 2}


def test_liste_mit_always_laeuft_trotz_ruhender_quelle(tmp_path, monkeypatch,
                                                       capsys):
    import time
    monkeypatch.setenv("SYNC_CADENCE",
                       json.dumps({"todo": "weekly", "todo:Projekte": "always"}))
    wurzel = state_db.StateDb(tmp_path)
    jetzt = str(time.time())
    for k in ("last_sync", "last_sync:l1", "last_sync:l2"):
        wurzel.kv_schreiben(k, jetzt)
    assert td.quelle_faellig(tmp_path)
    g = _zwei_listen()
    td.lauf(g, tmp_path, [LISTE, PROJEKTE])
    ausgabe = capsys.readouterr().out
    assert any("/lists/l2/" in u for u in g.aufrufe)
    assert not any("/lists/l1/" in u for u in g.aufrufe)
    assert any((progress.lies_event(z) or {}).get("v") == {"n": 1}
               for z in ausgabe.splitlines()
               if (progress.lies_event(z) or {}).get("k") == "run.todo.paced")
    assert _ergebnis(ausgabe)["extra"] == {"lists": 1, "skipped": 1}


def test_alle_listen_zurueckgehalten_ist_die_quellenzeile(tmp_path, monkeypatch,
                                                         capsys):
    import time
    monkeypatch.setenv("SYNC_CADENCE", json.dumps(
        {"todo": "always", "todo:Einkauf": "weekly", "todo:Projekte": "weekly"}))
    wurzel = state_db.StateDb(tmp_path)
    for k in ("last_sync:l1", "last_sync:l2"):
        wurzel.kv_schreiben(k, str(time.time()))
    g = _zwei_listen()
    td.lauf(g, tmp_path, [LISTE, PROJEKTE])
    ausgabe = capsys.readouterr().out
    assert g.aufrufe == []
    ereignisse = [e for e in (progress.lies_event(z) for z in ausgabe.splitlines()) if e]
    skip = [e for e in ereignisse if e["k"] == "run.cadence.skip"]
    assert len(skip) == 1 and skip[0]["v"]["name"]["k"] == "settings.todo.title"
    assert skip[0]["v"]["cadence"]["k"] == "cadence.weekly"
    assert not any(e["k"] == "run.todo.paced" for e in ereignisse)
    assert _ergebnis(ausgabe)["extra"] == {"lists": 0, "skipped": 2}
    assert not wurzel.kv_lesen("last_sync"), \
        "a run that synced nothing must not stamp the source"


def test_listenstempel_nur_nach_sauberer_liste(tmp_path, monkeypatch):
    """The per-list stamp follows the delta link's rule: a list whose task
    failed, or that failed as a whole, is due again next time."""
    monkeypatch.setenv("SYNC_CADENCE", json.dumps({"todo": "weekly"}))
    wurzel = state_db.StateDb(tmp_path)
    g = _zwei_listen()
    g.antworten["/me/todo/lists/l2/tasks"] = RuntimeError("weg")
    td.lauf(g, tmp_path, [LISTE, PROJEKTE])
    assert wurzel.kv_lesen("last_sync:l1") and not wurzel.kv_lesen("last_sync:l2")
    assert not wurzel.kv_lesen("last_sync"), \
        "a run with a failed list must not stamp the source"
    wurzel.kv_schreiben("last_sync:l1", "1")
    g = _Graph({"/me/todo/lists/l1/tasks/t1/attachments": RuntimeError("kaputt"),
                "/me/todo/lists/l1/tasks": {
                    "value": [_task("t1", "A", etag="e2", hasAttachments=True)]},
                "/me/todo/lists/l2/tasks": {"value": [_task("t9", "Z")]}})
    td.lauf(g, tmp_path, [LISTE, PROJEKTE])
    assert wurzel.kv_lesen("last_sync:l1") == "1", \
        "a list with a failed task must not be stamped"
    assert wurzel.kv_lesen("last_sync:l2")
    td.lauf(_zwei_listen(), tmp_path, [LISTE, PROJEKTE])
    assert wurzel.kv_lesen("last_sync:l1") != "1" and wurzel.kv_lesen("last_sync")


def test_unveraenderte_liste_wird_nicht_neu_geschrieben(tmp_path, monkeypatch):
    td.list_lauf(_graph([_task("t1", "A")]), tmp_path, LISTE)
    ziel = td.list_ziel(tmp_path, LISTE)
    datei = ziel / "list.html"
    vorher = (datei.read_bytes(), datei.stat().st_mtime_ns)
    db = state_db.StateDb(ziel)
    blobs = {k: db.kv_lesen(k) for k in ("list", "tasks", "stand")}
    schreibungen = []
    echt = td.export_util.schreibe_atomar
    monkeypatch.setattr(td.export_util, "schreibe_atomar",
                        lambda z, t: schreibungen.append(z) or echt(z, t))
    td.list_lauf(_graph([_task("t1", "A")]), tmp_path, LISTE)
    assert schreibungen == []
    assert (datei.read_bytes(), datei.stat().st_mtime_ns) == vorher
    assert {k: db.kv_lesen(k) for k in blobs} == blobs
    td.list_lauf(_graph([_task("t1", "A neu", etag="e2")]), tmp_path, LISTE)
    assert schreibungen == [datei] and "A neu" in datei.read_text(encoding="utf-8")
    datei.unlink()
    td.list_lauf(_graph([_task("t1", "A neu", etag="e2")]), tmp_path, LISTE)
    assert datei.exists() and len(schreibungen) == 2


def test_listen_laufen_nebeneinander_mit_monotonem_fortschritt(tmp_path, capsys):
    listen = [dict(LISTE, id=f"l{i}", titel=f"Liste {i}") for i in range(1, 6)]
    g = _Graph({f"/me/todo/lists/l{i}/tasks": {"value": [_task(f"t{i}", f"A{i}")]}
                for i in range(1, 6)})
    td.lauf(g, tmp_path, listen, workers=4)
    ausgabe = capsys.readouterr().out
    ergebnis = _ergebnis(ausgabe)
    assert ergebnis["new"] == 5 and ergebnis["extra"]["lists"] == 5
    assert ergebnis["excluded"] == 0 and ergebnis["errors"] == 0
    fortschritt = [progress.lies(z) for z in ausgabe.splitlines() if progress.lies(z)]
    assert [f["done"] for f in fortschritt] == list(range(6))
    assert fortschritt[0]["what"] == "lists"
    assert all((td.list_ziel(tmp_path, liste) / "list.html").exists()
               for liste in listen)
    # Every line whole – five lists talking at once must not interleave.
    assert all(z.startswith("@@") for z in ausgabe.splitlines() if z.strip())
    assert sum(1 for z in ausgabe.splitlines()
               if (progress.lies_event(z) or {}).get("k") == "run.todo.list") == 5
