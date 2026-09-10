"""todo_export.py – lists, tasks with their steps, and the tombstone lane.

The Graph side is faked throughout; what matters here is the export's own
part: etag-driven refresh, attachments next to the list, and the greyed
section for tasks that left the list.
"""

import json

import progress
import state_db
import todo_export as td


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
