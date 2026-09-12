"""planner_export.py – boards, tasks and above all: both comment worlds.

The Graph side is faked throughout; what matters here is Planner's own part:
URL -> plan, etag-driven refresh, legacy comments via the group thread
listing, chat comments via the beta endpoint, and the tombstone section for
tasks that left the board.
"""

import json

import pytest

import progress
import planner_export as pl
import state_db


@pytest.fixture(autouse=True)
def _feste_umgebung(monkeypatch):
    """The module reads the app's settings file for what the environment
    does not say – pinned here, so a developer's own config never steers a
    test: a sweep every day, no cadence, no "Sync now"."""
    monkeypatch.setenv("PLANNER_SWEEP_HOURS", "24")
    monkeypatch.setenv("SYNC_CADENCE", "{}")
    monkeypatch.delenv("SYNC_NOW", raising=False)


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
    """URL -> response; paged() yields value lists, get() the object."""

    def __init__(self, antworten):
        self.antworten = antworten
        self.aufrufe = []
        self.batches = []

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

    def batch_get(self, urls, extra_headers=None):
        self.batches.append(list(urls))
        return {url: (200, self.get(url)) for url in urls}


def _task(tid, titel, etag="e1", bucket="b1", thread=None, **extra):
    t = {"id": tid, "title": titel, "@odata.etag": etag, "bucketId": bucket,
         "percentComplete": 0, "assignments": {}, "appliedCategories": {},
         **extra}
    if thread:
        t["conversationThreadId"] = thread
    return t


def _graph_fuer_plan(tasks, posts=None, msgs=None, threads=None, pid="p1"):
    antworten = {
        f"/planner/plans/{pid}/details": {"categoryDescriptions":
                                          {"category1": "Wichtig"}},
        f"/planner/plans/{pid}/buckets": {"value": [
            {"id": "b1", "name": "Offen", "orderHint": "a"}]},
        f"/planner/plans/{pid}/tasks": {"value": tasks},
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
    """The board libraries are never mirrored on their own – when enabled
    the export fetches the referenced files and links locally; the
    Graph-encoded reference keys are defused along the way."""
    monkeypatch.setenv("PLANNER_ATTACHMENTS", "1")
    g = _graph_mit_referenz([_task("t1", "Aufgabe A")])
    pl.plan_lauf(g, tmp_path, PLAN, {})
    ziel = pl.plan_ziel(tmp_path, PLAN)
    dateien = list((ziel / pl.ANHANG_DIR).glob("*"))
    assert len(dateien) == 1 and dateien[0].read_bytes() == b"PDF"
    html = (ziel / "board.html").read_text(encoding="utf-8")
    assert f'href="{pl.ANHANG_DIR}/' in html
    assert "firma.sharepoint.com" not in html.split("refs")[1].split("</div>")[0]

    # Second run, task changed, file not: the cTag saves the download.
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
    """Chips at the top name the swimlanes; lane, card and comments are one
    <details> level each and all start collapsed."""
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


def _legacy_fake(posts, geliefert, tasks=None):
    """A one-task board whose legacy thread the group lists as moved at
    `geliefert`; `posts` is what the thread holds."""
    return _Graph({
        "/planner/plans/p1/details": {"categoryDescriptions": {}},
        "/planner/plans/p1/buckets": {"value": [
            {"id": "b1", "name": "Offen", "orderHint": "a"}]},
        "/planner/plans/p1/tasks": {"value": tasks or [
            _task("t1", "Aufgabe A", thread="th1")]},
        "/planner/tasks/t1/details": {"description": "", "checklist": {},
                                      "references": {}},
        "/groups/g1/threads/th1/posts": {"value": posts},
        "/groups/g1/threads?$top=100": {"value": [
            {"id": "th1", "lastDeliveredDateTime": geliefert}]},
        "beta/planner/tasks/t1/messages": {"value": []},
        "/users/": {"displayName": "Alice Beispiel"},
    })


def _post(wann):
    return {"from": {"emailAddress": {"name": "Bob"}},
            "receivedDateTime": wann, "body": {"content": "<div>x</div>"}}


def test_legacy_faeden_nur_auf_wunsch_neu_gelesen(tmp_path, monkeypatch,
                                                   capsys):
    """The first run fetches all posts WITHOUT listing the complete group
    conversation (minutes of silence for large groups). Planner has taken
    no legacy comments since February 2026, so afterwards the listing runs
    only when asked for (PLANNER_LEGACY_SYNC) – a normal run keeps what it
    has and says so in the log."""
    monkeypatch.delenv("PLANNER_LEGACY_SYNC", raising=False)
    g = _legacy_fake([_post("2026-07-01T10:00:00Z")], "2026-07-01T10:00:00Z")
    pl.plan_lauf(g, tmp_path, PLAN, {})
    assert not any("$top=100" in u for u in g.aufrufe), \
        "Erstlauf listet die Gruppen-Konversation"
    assert not any(e["k"] == "run.planner.legacy_skip"
                   for e in _events(capsys)), "Erstlauf meldet Überspringen"
    db = state_db.StateDb(pl.plan_ziel(tmp_path, PLAN))

    # Second run, thread moved on the server: neither listed nor fetched,
    # the comment stays, the log says why.
    g2 = _legacy_fake([_post("2026-07-01T10:00:00Z"),
                       _post("2026-07-05T09:00:00Z")], "2026-07-05T09:00:00Z")
    pl.plan_lauf(g2, tmp_path, PLAN, {})
    assert not any("$top=100" in u or "/th1/posts" in u for u in g2.aufrufe)
    assert len(json.loads(db.kv_lesen("tasks"))["t1"]["kommentare"]) == 1
    assert any(e["k"] == "run.planner.legacy_skip" and
               e["v"]["name"] == PLAN["titel"] for e in _events(capsys))

    # Asked for: the listing runs, the moved thread arrives fresh.
    monkeypatch.setenv("PLANNER_LEGACY_SYNC", "1")
    g3 = _legacy_fake([_post("2026-07-01T10:00:00Z"),
                       _post("2026-07-05T09:00:00Z")], "2026-07-05T09:00:00Z")
    pl.plan_lauf(g3, tmp_path, PLAN, {})
    assert any("$top=100" in u for u in g3.aufrufe)
    assert any("/th1/posts" in u for u in g3.aufrufe)
    assert len(json.loads(db.kv_lesen("tasks"))["t1"]["kommentare"]) == 2
    assert not any(e["k"] == "run.planner.legacy_skip"
                   for e in _events(capsys))

    # Asked for again, nothing moved: no more post fetches.
    g4 = _legacy_fake([], "2026-07-05T09:00:00Z")
    pl.plan_lauf(g4, tmp_path, PLAN, {})
    assert not any("/th1/posts" in u for u in g4.aufrufe), \
        "unbewegter Faden wurde erneut geholt"


def test_fehlgeschlagener_faden_kommt_ohne_listing_nach(tmp_path,
                                                        monkeypatch):
    """"First clean sync" is per thread: a task whose posts failed leaves
    no thread state, so the next run fetches it directly – no group listing
    for that either."""
    monkeypatch.delenv("PLANNER_LEGACY_SYNC", raising=False)
    tasks = [_task("t1", "Aufgabe A", thread="th1"),
             _task("t2", "Aufgabe B", thread="th2")]

    def fake(th2):
        g = _legacy_fake([_post("2026-07-01T10:00:00Z")],
                         "2026-07-01T10:00:00Z", tasks)
        g.antworten.update({
            "/planner/tasks/t2/details": {"description": "", "checklist": {},
                                          "references": {}},
            "/groups/g1/threads/th2/posts": th2,
            "beta/planner/tasks/t2/messages": {"value": []}})
        return g

    g = fake(RuntimeError("504 Gateway Timeout"))
    assert pl.plan_lauf(g, tmp_path, PLAN, {})[2] == 1
    db = state_db.StateDb(pl.plan_ziel(tmp_path, PLAN))
    assert set(json.loads(db.kv_lesen("threads"))) == {"th1"}

    g2 = fake({"value": [_post("2026-07-02T08:00:00Z")]})
    assert pl.plan_lauf(g2, tmp_path, PLAN, {})[2] == 0
    assert any("/th2/posts" in u for u in g2.aufrufe)
    assert not any("/th1/posts" in u or "$top=100" in u for u in g2.aufrufe)
    eintraege = json.loads(db.kv_lesen("tasks"))
    assert len(eintraege["t2"]["kommentare"]) == 1
    assert set(json.loads(db.kv_lesen("threads"))) == {"th1", "th2"}


def test_erzwungener_legacy_abgleich_wartet_nicht_auf_die_kadenz(
        tmp_path, monkeypatch, capsys):
    """The button is the user's explicit wish: a monthly board just synced
    is read again all the same."""
    monkeypatch.delenv("PLANNER_LEGACY_SYNC", raising=False)
    plan = dict(PLAN, kadenz="monthly")
    pl.lauf(_legacy_fake([], ""), tmp_path, [plan])
    pl.lauf(_legacy_fake([], ""), tmp_path, [plan])
    assert any(e["k"] == "run.cadence.skip" for e in _events(capsys))
    monkeypatch.setenv("PLANNER_LEGACY_SYNC", "1")
    g = _legacy_fake([], "")
    pl.lauf(g, tmp_path, [plan])
    assert not any(e["k"] == "run.cadence.skip" for e in _events(capsys))
    assert any("/planner/plans/p1/tasks" in u for u in g.aufrufe)


# ---------------------------------------------------------------------------
# 9.0: cadence before the fetch, shared names, sweep hours, quiet rewrites,
# parallel refresh
# ---------------------------------------------------------------------------
def test_nicht_faelliges_board_kostet_keinen_abruf(tmp_path, monkeypatch,
                                                    capsys):
    """The cadence is decided from the folder on disk before the plan is
    fetched: a weekly board synced a moment ago costs no request at all –
    and the skip line still knows its title."""
    import time
    pid = "p1planid001"
    url = f"https://planner.cloud.microsoft/webui/v1/plan/{pid}/view/board"
    ordner = tmp_path / f"Team X Board__{pl.export_util.kuerzel(pid)}"
    db = state_db.StateDb(ordner)
    db.kv_schreiben("last_sync", str(time.time()))
    db.kv_schreiben("plan", json.dumps({"id": pid, "titel": "Team X Board"}))
    monkeypatch.setenv("SYNC_CADENCE",
                       json.dumps({f"planner-url:{url}": "weekly"}))
    g = _Graph({})
    plaene, fehl = pl.resolve_plans(g, [url], out=tmp_path)
    assert fehl == 0 and g.aufrufe == [], \
        "a skipped board must not cost a request"
    assert plaene[0]["ordner"] == ordner.name
    assert plaene[0]["titel"] == "Team X Board"
    gelaufen = []
    monkeypatch.setattr(pl, "plan_lauf",
                        lambda *a, **kw: gelaufen.append(1) or (1, 0, 0))
    pl.lauf(g, tmp_path, plaene, fehl)
    assert gelaufen == [] and g.aufrufe == []
    assert any(e["k"] == "run.cadence.skip" and
               e["v"]["name"] == "Team X Board" for e in _events(capsys))
    # "Sync now" lets the gate step aside: now the plan is fetched.
    monkeypatch.setenv("SYNC_NOW", "1")
    g2 = _Graph({f"/planner/plans/{pid}": {
        "title": "Team X Board",
        "container": {"type": "group", "containerId": "g1"}}})
    plaene, fehl = pl.resolve_plans(g2, [url], out=tmp_path)
    assert len(g2.aufrufe) == 1 and "ordner" not in plaene[0]
    assert plaene[0]["gruppe"] == "g1"


def test_erstlauf_ohne_ordner_holt_den_plan(tmp_path, monkeypatch):
    """No folder yet means nothing to decide from – the plan is fetched,
    weekly cadence or not."""
    pid = "p1planid001"
    url = f"https://planner.cloud.microsoft/webui/v1/plan/{pid}/view/board"
    monkeypatch.setenv("SYNC_CADENCE",
                       json.dumps({f"planner-url:{url}": "weekly"}))
    g = _Graph({f"/planner/plans/{pid}": {"title": "Neu", "owner": "g9"}})
    plaene, fehl = pl.resolve_plans(g, [url], out=tmp_path)
    assert fehl == 0 and len(g.aufrufe) == 1 and plaene[0]["titel"] == "Neu"


def test_namen_kommen_gebuendelt_aus_dem_gemeinsamen_zwischenspeicher(tmp_path):
    """Names are asked once, as one JSON batch, and cached in the root's
    state.db for every board; each board keeps its own small view for the
    corpus reader."""
    g = _graph_fuer_plan([_task("t1", "Aufgabe A",
                                assignments={"u-1": {}, "u-2": {}})])
    pl.plan_lauf(g, tmp_path, PLAN, {})
    assert len(g.batches) == 1 and sorted(g.batches[0]) == [
        f"{pl.GRAPH}/users/u-1?$select=displayName",
        f"{pl.GRAPH}/users/u-2?$select=displayName"]
    wurzel = state_db.StateDb(tmp_path)
    assert json.loads(wurzel.satz_lesen("namen", "u-1")) == "Alice Beispiel"
    # A second board with the same people: not one request for the names.
    plan2 = dict(PLAN, id="p2", titel="Zweites Board")
    g2 = _graph_fuer_plan([_task("t1", "Aufgabe A", assignments={"u-1": {}})],
                          pid="p2")
    pl.plan_lauf(g2, tmp_path, plan2, {})
    assert g2.batches == [] and not any("/users/" in u for u in g2.aufrufe)
    ziel2 = pl.plan_ziel(tmp_path, plan2)
    assert "Alice Beispiel" in (ziel2 / "board.html").read_text(encoding="utf-8")
    assert json.loads(state_db.StateDb(ziel2).kv_lesen("namen")) == \
        {"u-1": "Alice Beispiel"}


def test_alter_namenszwischenspeicher_wird_uebernommen(tmp_path):
    """A board exported before 9.0 carries its names in its own state.db:
    they move into the shared cache without a request."""
    db = state_db.StateDb(pl.plan_ziel(tmp_path, PLAN))
    db.kv_schreiben("namen", json.dumps({"u-1": "Bob Baumeister"}))
    g = _graph_fuer_plan([_task("t1", "Aufgabe A", assignments={"u-1": {}})])
    pl.plan_lauf(g, tmp_path, PLAN, {})
    assert g.batches == []
    assert json.loads(state_db.StateDb(tmp_path).satz_lesen("namen", "u-1")) \
        == "Bob Baumeister"


def test_sweep_folgt_den_stunden_und_nicht_dem_sync_now(tmp_path, monkeypatch):
    import time
    pl.plan_lauf(_graph_fuer_plan([_task("t1", "Aufgabe A")]), tmp_path,
                 PLAN, {})
    db = state_db.StateDb(pl.plan_ziel(tmp_path, PLAN))
    # Two hours since the last sweep, one hour configured: due.
    db.kv_schreiben("sweep", str(time.time() - 2 * 3600))
    monkeypatch.setenv("PLANNER_SWEEP_HOURS", "1")
    g2 = _graph_fuer_plan([_task("t1", "Aufgabe A")])
    pl.plan_lauf(g2, tmp_path, PLAN, {})
    assert any("beta/" in u for u in g2.aufrufe), "a due sweep did not run"
    # Just swept, "Sync now" pressed: the button is not a sweep.
    monkeypatch.setenv("SYNC_NOW", "1")
    g3 = _graph_fuer_plan([_task("t1", "Aufgabe A")])
    pl.plan_lauf(g3, tmp_path, PLAN, {})
    assert not any("beta/" in u for u in g3.aufrufe), \
        "Sync now forced a sweep"
    # 0 hours: never, however old the last sweep is.
    monkeypatch.setenv("PLANNER_SWEEP_HOURS", "0")
    db.kv_schreiben("sweep", "0")
    g4 = _graph_fuer_plan([_task("t1", "Aufgabe A")])
    pl.plan_lauf(g4, tmp_path, PLAN, {})
    assert not any("beta/" in u for u in g4.aufrufe)


def test_unveraendertes_board_wird_nicht_neu_geschrieben(tmp_path, monkeypatch):
    pl.plan_lauf(_graph_fuer_plan([_task("t1", "Aufgabe A")]), tmp_path,
                 PLAN, {})
    ziel = pl.plan_ziel(tmp_path, PLAN)
    board = ziel / "board.html"
    vorher = (board.read_bytes(), board.stat().st_mtime_ns)
    db = state_db.StateDb(ziel)
    blobs = {k: db.kv_lesen(k)
             for k in ("plan", "tasks", "threads", "namen", "stand")}
    schreibungen = []
    echt = pl.export_util.schreibe_atomar
    monkeypatch.setattr(pl.export_util, "schreibe_atomar",
                        lambda z, t: schreibungen.append(z) or echt(z, t))
    # Second run, nothing changed: neither the file nor the blobs move.
    pl.plan_lauf(_graph_fuer_plan([_task("t1", "Aufgabe A")]), tmp_path,
                 PLAN, {})
    assert schreibungen == []
    assert (board.read_bytes(), board.stat().st_mtime_ns) == vorher
    assert {k: db.kv_lesen(k) for k in blobs} == blobs
    # A changed task rewrites the board.
    pl.plan_lauf(_graph_fuer_plan([_task("t1", "Aufgabe A neu", etag="e2")]),
                 tmp_path, PLAN, {})
    assert schreibungen == [board]
    assert "Aufgabe A neu" in board.read_text(encoding="utf-8")
    # A board file someone deleted comes back, the state untouched.
    board.unlink()
    pl.plan_lauf(_graph_fuer_plan([_task("t1", "Aufgabe A neu", etag="e2")]),
                 tmp_path, PLAN, {})
    assert board.exists() and len(schreibungen) == 2


def test_parallele_auffrischung_liefert_dasselbe_board(tmp_path, capsys):
    import re
    tasks = [_task(f"t{i}", f"Aufgabe {i}", thread="th1" if i == 1 else None)
             for i in range(1, 7)]

    def fake():
        g = _graph_fuer_plan(tasks, posts=[_post("2026-07-01T10:00:00Z")],
                             threads="2026-07-01T10:00:00Z")
        for i in range(3, 7):
            g.antworten[f"/planner/tasks/t{i}/details"] = {
                "description": f"Text {i}", "checklist": {}, "references": {}}
            g.antworten[f"beta/planner/tasks/t{i}/messages"] = {"value": []}
        return g

    eins, vier = tmp_path / "eins", tmp_path / "vier"
    assert pl.plan_lauf(fake(), eins, PLAN, {}, workers=1) == (6, 0, 0)
    capsys.readouterr()
    assert pl.plan_lauf(fake(), vier, PLAN, {}, workers=4) == (6, 0, 0)
    fortschritt = [progress.lies(z)["done"]
                   for z in capsys.readouterr().out.splitlines()
                   if progress.lies(z)]
    assert fortschritt == list(range(7)), "progress not monotonic"

    def ohne_stand(html):
        return re.sub(r"Stand [^<]+", "Stand", html)

    lesen = [(pl.plan_ziel(o, PLAN) / "board.html").read_text(encoding="utf-8")
             for o in (eins, vier)]
    assert ohne_stand(lesen[0]) == ohne_stand(lesen[1])
    a, b = [json.loads(state_db.StateDb(pl.plan_ziel(o, PLAN)).kv_lesen("tasks"))
            for o in (eins, vier)]
    assert a == b and set(a) == {f"t{i}" for i in range(1, 7)}
    assert len(a["t1"]["kommentare"]) == 1


def test_fehler_im_worker_zaehlt_und_stoert_die_anderen_nicht(tmp_path, capsys):
    g = _graph_fuer_plan([_task("t1", "Aufgabe A"), _task("t2", "Aufgabe B")])
    g.antworten["/planner/tasks/t2/details"] = RuntimeError("504")
    assert pl.plan_lauf(g, tmp_path, PLAN, {}, workers=3) == (1, 0, 1)
    assert any(e["k"] == "run.planner.task_failed" and
               e["v"]["name"] == "Aufgabe B" for e in _events(capsys))


def test_full_sync_holt_jede_karte_und_referenz_erneut(tmp_path, monkeypatch):
    """"Force full sync": no etag and no cTag counts – details, comments
    and the referenced files come again, the board is written over."""
    monkeypatch.setenv("PLANNER_ATTACHMENTS", "1")
    g = _graph_mit_referenz([_task("t1", "Aufgabe A")])
    pl.plan_lauf(g, tmp_path, PLAN, {})
    monkeypatch.setenv("FULL_SYNC", "1")
    g2 = _graph_mit_referenz([_task("t1", "Aufgabe A")])
    neu, unveraendert, fehler = pl.plan_lauf(g2, tmp_path, PLAN, {})
    assert (neu, unveraendert, fehler) == (1, 0, 0)
    assert any("/planner/tasks/t1/details" in u for u in g2.aufrufe)
    assert len(g2.geladen) == 1, "the referenced file is fetched again"
    ziel = pl.plan_ziel(tmp_path, PLAN)
    assert len(list((ziel / pl.ANHANG_DIR).glob("*"))) == 1
