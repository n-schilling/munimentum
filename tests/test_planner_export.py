"""planner_export.py – boards, tasks and above all: both comment worlds.

The Graph side is faked throughout; what matters here is Planner's own part:
URL -> plan, etag-driven refresh, legacy comments via the group thread
listing, chat comments via the beta endpoint, and the tombstone section for
tasks that left the board.
"""

import json

import progress
import planner_export as pl
import state_db


def _events(capsys):
    return [e for e in (progress.lies_event(z) for z in
                        capsys.readouterr().out.splitlines()) if e]


def test_plan_id_aus_beiden_adressformen():
    neu = ("https://planner.cloud.microsoft/webui/v1/plan/"
           "abcdefID123_-x/view/board/task/tsk?tid=t-1")
    alt = ("https://tasks.office.com/firma.com/de-DE/Home/Planner/"
           "#/plantaskboard?groupId=g1&planId=altPlanId99")
    assert pl.plan_id_aus(neu) == "abcdefID123_-x"
    assert pl.plan_id_aus(alt) == "altPlanId99"
    assert pl.plan_id_aus("https://firma.example/irgendwas") is None


class _Graph:
    """URL -> Antwort; paged() liefert value-Listen, get() das Objekt."""

    def __init__(self, antworten):
        self.antworten = antworten
        self.aufrufe = []

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


def _task(tid, titel, etag="e1", bucket="b1", thread=None, **extra):
    t = {"id": tid, "title": titel, "@odata.etag": etag, "bucketId": bucket,
         "percentComplete": 0, "assignments": {}, "appliedCategories": {},
         **extra}
    if thread:
        t["conversationThreadId"] = thread
    return t


def _graph_fuer_plan(tasks, posts=None, msgs=None, threads=None):
    antworten = {
        "/planner/plans/p1/details": {"categoryDescriptions":
                                      {"category1": "Wichtig"}},
        "/planner/plans/p1/buckets": {"value": [
            {"id": "b1", "name": "Offen", "orderHint": "a"}]},
        "/planner/plans/p1/tasks": {"value": tasks},
        "/groups/g1/threads/th1/posts": {"value": posts or []},
        "/groups/g1/threads?": {"value": []},
        "/groups/g1/threads": {"value": [
            {"id": "th1", "lastDeliveredDateTime": w}
            for w in ([threads] if threads else [])]},
        "/planner/tasks/t1/details": {"description": "Beschreibung A",
                                      "checklist": {"c1": {
                                          "title": "Punkt eins",
                                          "isChecked": True,
                                          "orderHint": "a"}},
                                      "references": {}},
        "/planner/tasks/t2/details": {"description": "", "checklist": {},
                                      "references": {}},
        "beta/planner/tasks/t1/messages": {"value": msgs or []},
        "beta/planner/tasks/t2/messages": {"value": []},
        "/users/": {"displayName": "Alice Beispiel"},
    }
    return _Graph(antworten)


PLAN = {"id": "p1", "titel": "Team X Board", "gruppe": "g1",
        "kadenz": "always"}


def test_plan_lauf_holt_beide_kommentarwelten(tmp_path, capsys):
    g = _graph_fuer_plan(
        [_task("t1", "Aufgabe A", thread="th1")],
        posts=[{"from": {"emailAddress": {"name": "Bob"}},
                "receivedDateTime": "2026-07-01T10:00:00Z",
                "body": {"content": "<div>Legacy-Kommentar"
                                    "<script>boese()</script></div>"}}],
        msgs=[{"id": "m1", "content": "<div>Neuer Kommentar</div>",
               "createdDateTime": "2026-07-24T04:53:27Z",
               "createdBy": {"user": {"id": "u-1"}}}],
        threads="2026-07-01T10:00:00Z")
    neu, unveraendert, fehler = pl.plan_lauf(g, tmp_path, PLAN, {})
    assert (neu, unveraendert, fehler) == (1, 0, 0)
    html = (pl.plan_ziel(tmp_path, PLAN) / "board.html").read_text(
        encoding="utf-8")
    assert "Legacy-Kommentar" in html and "Neuer Kommentar" in html
    assert "boese()" not in html, "Skripte müssen draußen bleiben"
    assert "Alice Beispiel" in html, "Kommentar-Autor nicht aufgelöst"
    assert "Punkt eins" in html and "Beschreibung A" in html
    assert "Offen (1)" in html


def test_unveraenderte_tasks_kosten_keine_detailabrufe(tmp_path):
    g = _graph_fuer_plan([_task("t1", "Aufgabe A")])
    pl.plan_lauf(g, tmp_path, PLAN, {})
    g.aufrufe = []
    threads = {}
    neu, unveraendert, fehler = pl.plan_lauf(g, tmp_path, PLAN, threads)
    assert (neu, unveraendert) == (0, 1)
    assert not any("/planner/tasks/" in u and "/details" in u
                   for u in g.aufrufe), "Task-Details trotz gleichem etag"
    assert not any("beta/" in u for u in g.aufrufe), \
        "Sweep lief erneut, obwohl der letzte keinen Tag her ist"


def test_verschwundene_task_bleibt_als_grabstein(tmp_path):
    g = _graph_fuer_plan([_task("t1", "Aufgabe A"),
                          _task("t2", "Aufgabe B")])
    pl.plan_lauf(g, tmp_path, PLAN, {})
    g2 = _graph_fuer_plan([_task("t1", "Aufgabe A")])
    pl.plan_lauf(g2, tmp_path, PLAN, {})
    db = state_db.StateDb(pl.plan_ziel(tmp_path, PLAN))
    eintraege = json.loads(db.kv_lesen("tasks"))
    assert eintraege["t2"]["deleted"]
    html = (pl.plan_ziel(tmp_path, PLAN) / "board.html").read_text(
        encoding="utf-8")
    assert "Nicht mehr im Board" in html and "Aufgabe B" in html


def test_lauf_ueberspringt_plan_unter_kadenz(tmp_path, monkeypatch, capsys):
    import time
    plan = dict(PLAN, kadenz="weekly")
    db = state_db.StateDb(pl.plan_ziel(tmp_path, plan))
    db.kv_schreiben("last_sync", str(time.time()))
    gelaufen = []
    monkeypatch.setattr(pl, "plan_lauf",
                        lambda *a, **kw: gelaufen.append(1) or (1, 0, 0))
    pl.lauf(_Graph({}), tmp_path, [plan])
    assert gelaufen == []
    assert any(e["k"] == "run.cadence.skip" for e in _events(capsys))


def test_resolve_plans_haengt_kadenz_an_und_meldet_kaputte_urls(capsys):
    g = _Graph({"/planner/plans/abcdefID123": {
        "id": "abcdefID123", "title": "Team X Board",
        "container": {"type": "group", "containerId": "g1"}}})
    url = "https://planner.cloud.microsoft/webui/v1/plan/abcdefID123/view/board"
    import os
    os.environ["SYNC_CADENCE"] = json.dumps({f"planner-url:{url}": "weekly"})
    try:
        plaene, fehl = pl.resolve_plans(g, [url, "https://kaputt.example/x"])
    finally:
        del os.environ["SYNC_CADENCE"]
    assert fehl == 1 and len(plaene) == 1
    assert plaene[0]["kadenz"] == "weekly" and plaene[0]["gruppe"] == "g1"
    assert any(e["k"] == "run.planner.bad_url" for e in _events(capsys))


def test_corpus_liest_tasks_samt_kommentaren(tmp_path):
    import corpus
    g = _graph_fuer_plan(
        [_task("t1", "Vertragsverlängerung", thread="th1")],
        posts=[{"from": {"emailAddress": {"name": "Bob"}},
                "receivedDateTime": "2026-07-01T10:00:00Z",
                "body": {"content": "<div>On Hold bis August</div>"}}],
        threads="2026-07-01T10:00:00Z")
    pl.plan_lauf(g, tmp_path, PLAN, {})
    saetze = corpus.load_planner(tmp_path)
    assert len(saetze) == 1
    satz = saetze[0]
    assert satz["src"] == "planner" and satz["title"] == "Vertragsverlängerung"
    assert "On Hold bis August" in satz["text"]
    assert "Beschreibung A" in satz["text"]
    assert satz["ctx"].startswith("Team X Board/")
    assert satz["rel"].endswith("board.html")
    assert "gone" not in satz


def _graph_mit_referenz(tasks, ctag="c-1"):
    g = _graph_fuer_plan(tasks)
    g.antworten["/planner/tasks/t1/details"] = {
        "description": "", "checklist": {},
        "references": {"https%3A//firma%2Esharepoint%2Ecom/x/Angebot%2Epdf":
                       {"alias": "Angebot.pdf"}}}
    g.antworten["/shares/u!"] = {"name": "Angebot.pdf", "cTag": ctag}
    g.geladen = []
    g.get_bytes = lambda url, label="": (g.geladen.append(url)
                                         or (b"PDF", "application/pdf"))
    return g


def test_referenzen_werden_optional_mitgeladen(tmp_path, monkeypatch):
    """Die Board-Libraries werden nie eigenständig gespiegelt – eingeschaltet
    holt der Export die referenzierten Dateien und verlinkt lokal; die
    Graph-kodierten Referenz-Schlüssel werden dabei entschärft."""
    monkeypatch.setenv("PLANNER_ATTACHMENTS", "1")
    g = _graph_mit_referenz([_task("t1", "Aufgabe A")])
    pl.plan_lauf(g, tmp_path, PLAN, {})
    ziel = pl.plan_ziel(tmp_path, PLAN)
    dateien = list((ziel / pl.ANHANG_DIR).glob("*"))
    assert len(dateien) == 1 and dateien[0].read_bytes() == b"PDF"
    html = (ziel / "board.html").read_text(encoding="utf-8")
    assert f'href="{pl.ANHANG_DIR}/' in html
    assert "firma.sharepoint.com" not in html.split("refs")[1].split("</div>")[0]

    # Zweiter Lauf, Task geändert, Datei nicht: der cTag spart den Download.
    g2 = _graph_mit_referenz([_task("t1", "Aufgabe A", etag="e2")])
    pl.plan_lauf(g2, tmp_path, PLAN, {})
    assert g2.geladen == [], "unveränderte Referenz erneut geladen"


def test_referenzen_bleiben_ohne_option_online_links(tmp_path, monkeypatch):
    monkeypatch.delenv("PLANNER_ATTACHMENTS", raising=False)
    monkeypatch.setenv("PLANNER_ATTACHMENTS", "0")
    g = _graph_mit_referenz([_task("t1", "Aufgabe A")])
    pl.plan_lauf(g, tmp_path, PLAN, {})
    ziel = pl.plan_ziel(tmp_path, PLAN)
    assert not (ziel / pl.ANHANG_DIR).exists()
    html = (ziel / "board.html").read_text(encoding="utf-8")
    assert 'href="https://firma.sharepoint.com/x/Angebot.pdf"' in html
    assert "Angebot.pdf" in html


def test_corpus_traegt_referenznamen_als_anhang(tmp_path, monkeypatch):
    import corpus
    monkeypatch.setenv("PLANNER_ATTACHMENTS", "0")
    g = _graph_mit_referenz([_task("t1", "Aufgabe A")])
    pl.plan_lauf(g, tmp_path, PLAN, {})
    satz = corpus.load_planner(tmp_path)[0]
    assert satz["att"] == "Angebot.pdf"


def test_board_ist_dreistufig_zugeklappt(tmp_path):
    """Chips oben nennen die Swimlanes; Lane, Karte und Kommentare sind je
    eine <details>-Stufe und starten alle zugeklappt."""
    g = _graph_fuer_plan(
        [_task("t1", "Aufgabe A", thread="th1")],
        posts=[{"from": {"emailAddress": {"name": "Bob"}},
                "receivedDateTime": "2026-07-01T10:00:00Z",
                "body": {"content": "<div>Hallo</div>"}}],
        threads="2026-07-01T10:00:00Z")
    pl.plan_lauf(g, tmp_path, PLAN, {})
    html = (pl.plan_ziel(tmp_path, PLAN) / "board.html").read_text(
        encoding="utf-8")
    assert '<nav class="lanes">' in html and ">Offen</b><span>1</span>" in html
    assert '<details class="lane"' in html
    assert '<details class="karte"><summary>' in html
    assert "<summary>Kommentare (1)</summary>" in html
    karte_kopf = html.split('<details class="karte"><summary>')[1] \
        .split("</summary>")[0]
    assert "1 Kommentar" in karte_kopf, \
        "Kommentarzahl fehlt in der zugeklappten Zeile"
    assert "<details open" not in html and " open>" not in html, \
        "nichts darf aufgeklappt starten"


def test_legacy_diff_erst_ab_dem_zweiten_lauf(tmp_path):
    """Der Erstlauf holt alle Posts OHNE die Auflistung der kompletten
    Gruppen-Konversation (bei großen Gruppen Minuten an Stille); ab dem
    zweiten Lauf entscheidet die Auflistung, welcher Faden sich bewegt hat."""
    def fake(posts, geliefert):
        return _Graph({
            "/planner/plans/p1/details": {"categoryDescriptions": {}},
            "/planner/plans/p1/buckets": {"value": [
                {"id": "b1", "name": "Offen", "orderHint": "a"}]},
            "/planner/plans/p1/tasks": {"value": [
                _task("t1", "Aufgabe A", thread="th1")]},
            "/planner/tasks/t1/details": {"description": "", "checklist": {},
                                          "references": {}},
            "/groups/g1/threads/th1/posts": {"value": posts},
            "/groups/g1/threads?$top=100": {"value": [
                {"id": "th1", "lastDeliveredDateTime": geliefert}]},
            "beta/planner/tasks/t1/messages": {"value": []},
            "/users/": {"displayName": "Alice Beispiel"},
        })

    def post(wann):
        return {"from": {"emailAddress": {"name": "Bob"}},
                "receivedDateTime": wann, "body": {"content": "<div>x</div>"}}

    g = fake([post("2026-07-01T10:00:00Z")], "2026-07-01T10:00:00Z")
    pl.plan_lauf(g, tmp_path, PLAN, {})
    assert not any("$top=100" in u for u in g.aufrufe), \
        "Erstlauf listet die Gruppen-Konversation"

    # Zweiter Lauf, Faden bewegt: Auflistung läuft, Posts kommen neu.
    g2 = fake([post("2026-07-01T10:00:00Z"), post("2026-07-05T09:00:00Z")],
              "2026-07-05T09:00:00Z")
    pl.plan_lauf(g2, tmp_path, PLAN, {})
    assert any("$top=100" in u for u in g2.aufrufe)
    assert any("/th1/posts" in u for u in g2.aufrufe)
    db = state_db.StateDb(pl.plan_ziel(tmp_path, PLAN))
    eintraege = json.loads(db.kv_lesen("tasks"))
    assert len(eintraege["t1"]["kommentare"]) == 2

    # Dritter Lauf, nichts bewegt: keine Post-Abrufe mehr.
    g3 = fake([], "2026-07-05T09:00:00Z")
    pl.plan_lauf(g3, tmp_path, PLAN, {})
    assert not any("/th1/posts" in u for u in g3.aufrufe), \
        "unbewegter Faden wurde erneut geholt"
