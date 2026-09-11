"""Tests for graph_client.py – retry, throttling, paging, token handling.

The layer the export scripts share, tested once here instead of per copy.
Everything without a network: SESSION is replaced by a fake.
"""

import threading

import pytest
import requests

import graph_client


class FakeResponse:
    """Replica of a requests response without a network."""

    def __init__(self, status=200, payload=None, content=b"", headers=None):
        self.status_code = status
        self._payload = payload
        self.content = content
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """Serves prepared responses in order and logs the calls."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None, stream=False):
        self.calls.append({"url": url, "headers": dict(headers or {}),
                           "params": params, "timeout": timeout, "stream": stream})
        r = self.responses.pop(0)
        if isinstance(r, Exception):   # simulate a network error
            raise r
        return r

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {}),
                           "json": json, "timeout": timeout, "post": True})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def session(monkeypatch):
    """Mount the fake session; the test sets responses via .responses."""
    fake = FakeSession([])
    monkeypatch.setattr(graph_client, "SESSION", fake)
    return fake


@pytest.fixture
def sleeps(monkeypatch):
    """Disarm time.sleep and record the requested wait times.

    The gate reset lives in conftest.py; the recorded values are remainders
    (float) – compare rounded.
    """
    calls = []
    monkeypatch.setattr(graph_client.time, "sleep", lambda s: calls.append(s))
    return calls


# --------------------------------------------------------------------------
# TokenClient: retry, throttling, paging, TokenExpired
# --------------------------------------------------------------------------
def test_tokenclient_get_sendet_bearer_und_params(session):
    session.responses = [FakeResponse(payload={"value": [1]})]
    tc = graph_client.TokenClient("tok123")
    assert tc.get("https://example.invalid/x", {"$top": 5},
                  extra_headers={"Prefer": "utc"}) == {"value": [1]}
    call = session.calls[0]
    assert call["headers"]["Authorization"] == "Bearer tok123"
    assert call["headers"]["Prefer"] == "utc"
    assert call["params"] == {"$top": 5}


def test_tokenclient_get_wiederholt_nach_429(session, sleeps):
    session.responses = [FakeResponse(429, headers={"Retry-After": "3"}),
                         FakeResponse(payload={"ok": 1})]
    assert graph_client.TokenClient("t").get("https://example.invalid/x") == {"ok": 1}
    assert len(session.calls) == 2
    # Retry-After is honoured; the wait happens before the follow-up.
    assert [round(s) for s in sleeps] == [3]


def test_tokenclient_401_wirft_tokenexpired(session):
    session.responses = [FakeResponse(401)]
    tc = graph_client.TokenClient("t")
    with pytest.raises(graph_client.TokenExpired):
        tc.get("https://example.invalid/x")
    session.responses = [FakeResponse(401)]
    with pytest.raises(graph_client.TokenExpired):
        tc.get_bytes("https://example.invalid/x")
    session.responses = [FakeResponse(401)]
    with pytest.raises(graph_client.TokenExpired):
        tc.stream("https://example.invalid/x")


def test_tokenclient_bricht_nach_sechs_serverfehlern_ab(session, sleeps):
    session.responses = [FakeResponse(500) for _ in range(6)]
    with pytest.raises(RuntimeError, match="Zu viele Fehlversuche"):
        graph_client.TokenClient("t").get("https://example.invalid/x")
    assert len(session.calls) == 6
    # Exponential backoff before each follow-up; no wait after the final
    # failure – giving up must not cost another 32 s.
    assert [round(s) for s in sleeps] == [1, 2, 4, 8, 16]


def test_tokenclient_4xx_wirft_httperror(session):
    session.responses = [FakeResponse(404)]
    with pytest.raises(requests.HTTPError):
        graph_client.TokenClient("t").get("https://example.invalid/x")


def test_tokenclient_get_bytes_liefert_inhalt_und_contenttype(session):
    session.responses = [FakeResponse(content=b"MIME",
                                      headers={"Content-Type": "message/rfc822"})]
    tc = graph_client.TokenClient("t")
    assert tc.get_bytes("https://example.invalid/m") == (b"MIME", "message/rfc822")
    assert session.calls[0]["timeout"] == graph_client.TIMEOUT_BYTES


def test_tokenclient_paged_folgt_nextlink(session):
    session.responses = [
        FakeResponse(payload={"value": [1, 2],
                              "@odata.nextLink": "https://example.invalid/p2"}),
        FakeResponse(payload={"value": [3]}),
    ]
    tc = graph_client.TokenClient("t")
    assert list(tc.paged("https://example.invalid/p1", {"$top": 2})) == [1, 2, 3]
    # Follow-up page without the original params (nextLink already carries them)
    assert session.calls[1]["url"] == "https://example.invalid/p2"
    assert session.calls[1]["params"] is None


# --------------------------------------------------------------------------
# Network errors: timeouts/dropped connections are retried, not fatal
# --------------------------------------------------------------------------
def _timeout():
    return requests.exceptions.ReadTimeout("read timed out")


def test_fetch_wiederholt_nach_netzwerkfehler(session, sleeps):
    session.responses = [_timeout(), _timeout(), FakeResponse(payload={"ok": 1})]
    assert graph_client.TokenClient("t").get("https://example.invalid/x") == {"ok": 1}
    assert len(session.calls) == 3
    assert sleeps == [1, 2]


def test_fetch_gibt_nach_allen_netzwerkversuchen_auf(session, sleeps):
    session.responses = [_timeout() for _ in range(graph_client.NET_RETRIES)]
    with pytest.raises(requests.exceptions.ReadTimeout):
        graph_client.TokenClient("t").get("https://example.invalid/x")
    assert len(session.calls) == graph_client.NET_RETRIES


def test_netzwerkfehler_verbraucht_keinen_http_versuch(session, sleeps):
    """A hiccup must not use up the attempts reserved for 429/5xx."""
    session.responses = ([_timeout()]
                         + [FakeResponse(500) for _ in range(graph_client.HTTP_RETRIES - 1)]
                         + [FakeResponse(payload={"ok": 1})])
    assert graph_client.TokenClient("t").get("https://example.invalid/x") == {"ok": 1}


def test_get_bytes_wiederholt_nach_netzwerkfehler(session, sleeps):
    session.responses = [_timeout(), FakeResponse(content=b"MIME")]
    content, _ = graph_client.TokenClient("t").get_bytes("https://example.invalid/m")
    assert content == b"MIME"


# --------------------------------------------------------------------------
# Graph client: token renewal on 401 (without a real sign-in)
# --------------------------------------------------------------------------
class _StubAnmeldung:
    """Only what the HTTP layer needs from auth.Login. Sign-in itself has
    its own tests (test_auth.py); here it is about retry and paging."""

    def __init__(self, token="alt"):
        self.token = token

    def headers(self):
        return {"Authorization": f"Bearer {self.token}"}


def _bare_graph():
    """Graph instance without an interactive login."""
    return graph_client.Graph(anmeldung=_StubAnmeldung())


def test_graph_get_erneuert_token_bei_401(session):
    session.responses = [FakeResponse(401), FakeResponse(payload={"ok": True})]
    g = _bare_graph()
    aufrufe = []

    def refresh():
        aufrufe.append(1)
        g.anmeldung.token = "neu"
    g._refresh = refresh

    assert g.get("https://example.invalid/x", extra_headers={"Prefer": "utc"}) == {"ok": True}
    assert aufrufe == [1]
    assert session.calls[0]["headers"]["Authorization"] == "Bearer alt"
    assert session.calls[1]["headers"]["Authorization"] == "Bearer neu"
    assert session.calls[1]["headers"]["Prefer"] == "utc"


def test_graph_get_bytes_erneuert_token_bei_401(session):
    session.responses = [FakeResponse(401), FakeResponse(content=b"X")]
    g = _bare_graph()
    g._refresh = lambda: setattr(g.anmeldung, "token", "neu")
    content, _ = g.get_bytes("https://example.invalid/m")
    assert content == b"X"
    assert session.calls[1]["headers"]["Authorization"] == "Bearer neu"


def test_graph_paged_folgt_nextlink(session):
    session.responses = [
        FakeResponse(payload={"value": ["a"], "@odata.nextLink": "https://example.invalid/n"}),
        FakeResponse(payload={"value": ["b"]}),
    ]
    assert list(_bare_graph().paged("https://example.invalid/1")) == ["a", "b"]


def test_graph_refresh_ist_verriegelt():
    """Only one thread renews at a time – the lock exists and is used."""
    g = _bare_graph()
    erneuert = []

    class Anmeldung(_StubAnmeldung):
        def erneuern(self):
            assert not g._refresh_lock.acquire(blocking=False), "Lock nicht gehalten"
            erneuert.append(1)

    g.anmeldung = Anmeldung()
    g._refresh()
    assert erneuert == [1]


# --------------------------------------------------------------------------
# stream() and konfiguriere()
# --------------------------------------------------------------------------
def test_stream_liefert_die_rohe_antwort(session, sleeps):
    antwort = FakeResponse(content=b"GROSS")
    session.responses = [FakeResponse(503), antwort]
    r = graph_client.TokenClient("t").stream("https://example.invalid/d",
                                             timeout=(30, 600))
    assert r is antwort
    assert session.calls[0]["stream"] is True
    assert session.calls[0]["timeout"] == (30, 600)


def test_konfiguriere_setzt_gate_und_pool(monkeypatch):
    gemountet = {}
    monkeypatch.setattr(graph_client, "SESSION", type(
        "S", (), {"mount": lambda self, prefix, adapter: gemountet.update(
            {"prefix": prefix, "adapter": adapter})})())
    alt = graph_client.GATE
    try:
        graph_client.konfiguriere(2)
        # BoundedSemaphore(2): acquiring twice works, three times does not
        assert graph_client.GATE.acquire(blocking=False)
        assert graph_client.GATE.acquire(blocking=False)
        assert not graph_client.GATE.acquire(blocking=False)
        assert gemountet["prefix"] == "https://"
        assert gemountet["adapter"]._pool_maxsize == 4   # lower bound of 4
    finally:
        graph_client.GATE = alt


def test_gate_wird_um_das_request_gehalten(session):
    """The request runs inside the GATE; waiting happens without a held slot."""
    session.responses = [FakeResponse(payload={"ok": 1})]
    belegt = []
    echt = graph_client.GATE

    class SpionGate:
        def __enter__(self):
            belegt.append("rein")
            return echt.__enter__()

        def __exit__(self, *a):
            belegt.append("raus")
            return echt.__exit__(*a)

    graph_client.GATE = SpionGate()
    try:
        graph_client.TokenClient("t").get("https://example.invalid/x")
    finally:
        graph_client.GATE = echt
    assert belegt == ["rein", "raus"]


def test_threads_teilen_sich_das_gate(session):
    """More threads than slots: all get through, none starves."""
    session.responses = [FakeResponse(payload={"ok": 1})] * 8
    lock = threading.Lock()
    echt_get = session.get

    def sicher_get(*a, **kw):
        with lock:                       # FakeSession.pop is not thread-safe
            return echt_get(*a, **kw)

    session.get = sicher_get
    tc = graph_client.TokenClient("t")
    ergebnisse = []
    threads = [threading.Thread(
        target=lambda: ergebnisse.append(tc.get("https://example.invalid/x")))
        for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert ergebnisse == [{"ok": 1}] * 8


def test_drosselsperre_gilt_dem_ganzen_prozess(session, sleeps, capsys):
    """Sixteen threads, one 429: reported once, awaited by everyone who
    asks afterwards – the gate belongs to the process, not the connection."""
    import progress

    session.responses = [FakeResponse(429, headers={"Retry-After": "7"}),
                         FakeResponse(payload={"ok": 1}),
                         FakeResponse(payload={"ok": 2})]
    tc = graph_client.TokenClient("t")
    assert tc.get("https://example.invalid/a") == {"ok": 1}
    assert tc.get("https://example.invalid/b") == {"ok": 2}   # second call
    events = [e for e in (progress.lies_event(z) for z in
                          capsys.readouterr().out.splitlines())
              if e and e["k"] == "run.throttled"]
    assert len(events) == 1 and events[0]["v"]["s"] == 7
    # Both follow-up requests waited out the gate (~7 s remainder each).
    assert [round(s) for s in sleeps] == [7, 7]


# --------------------------------------------------------------------------
# The pacer: requests spaced out below a per-count limit
# --------------------------------------------------------------------------
def test_takt_rechnet_die_wartezeit_aus_beiden_fenstern():
    t = graph_client.Takt(pro_minute=2, pro_stunde=3, zeiten=[1000.0, 1010.0])
    assert t.wartezeit(jetzt=1011.0) == 49.0          # minute window: 2 of 2 used
    assert t.wartezeit(jetzt=1061.0) == 0.0           # the first one dropped out
    t.zeiten.append(1061.0)
    assert t.wartezeit(jetzt=1062.0) == pytest.approx(1000.0 + 3600.0 - 1062.0)   # hour: 3 of 3
    assert t.verbraucht(3600.0, jetzt=1062.0) == 3
    assert t.verbraucht(60.0, jetzt=1062.0) == 2


def test_takt_wartet_vor_der_anfrage_und_merkt_sie_sich(session, sleeps, monkeypatch):
    zeit = [1000.0]
    monkeypatch.setattr(graph_client.time, "time", lambda: zeit[0])
    takt = graph_client.takt(pro_minute=1)
    session.responses = [FakeResponse(payload={"a": 1}), FakeResponse(payload={"a": 2})]
    tc = graph_client.TokenClient("tok")
    assert tc.get("https://example.invalid/1") == {"a": 1}
    assert sleeps == [] and takt.zeiten == [1000.0]
    # The second request within the minute has to wait for the window –
    # the fake sleep records it and the clock moves on.
    def schlafen(s):
        sleeps.append(s)
        zeit[0] += s
    monkeypatch.setattr(graph_client.time, "sleep", schlafen)
    zeit[0] = 1010.0
    assert tc.get("https://example.invalid/2") == {"a": 2}
    # The first moment has left the minute window by then.
    assert sleeps == [50.0] and takt.zeiten == [1060.0]
    assert graph_client.takt() is None, "no limits means no pacer"


def test_erschoepfte_429_leiter_ist_ueberlastet(session, sleeps):
    session.responses = [FakeResponse(status=429)] * graph_client.HTTP_RETRIES
    with pytest.raises(graph_client.Ueberlastet):
        graph_client.TokenClient("tok").get("https://example.invalid/x")
    session.responses = [FakeResponse(status=503)] * graph_client.HTTP_RETRIES
    with pytest.raises(RuntimeError) as e:
        graph_client.TokenClient("tok").get_bytes("https://example.invalid/y")
    assert not isinstance(e.value, graph_client.Ueberlastet), "a 503 is not a spent budget"


def test_getaktete_429_wartet_die_minute_und_gibt_dann_auf(session, sleeps):
    """Under pacing a 429 without Retry-After means the minute window (or
    the hour) is full: wait the minute once, and a second refusal ends the
    run as a spent budget – not four more refused requests."""
    graph_client.takt(100, 380, [])
    session.responses = [FakeResponse(status=429), FakeResponse(status=429),
                         FakeResponse(200, {"ok": 1})]
    with pytest.raises(graph_client.Ueberlastet):
        graph_client.TokenClient("tok").get("https://example.invalid/x")
    assert len(session.calls) == 2, "the second 429 is the answer"
    assert any(round(s) == 60 for s in sleeps), "the minute window, not a guess"
    # Without pacing the ladder runs as before.
    graph_client.TAKT = None
    session.responses = [FakeResponse(status=429)] * 3 + [FakeResponse(200, {"ok": 1})]
    assert graph_client.TokenClient("tok").get("https://example.invalid/x") == {"ok": 1}


def test_takt_sagt_die_pause_und_haelt_kein_volles_fenster_aus(monkeypatch, capsys):
    """A run that stands still without a word looks hung: a noticeable pause
    is said once. And a wait for the hour window is no pause at all – the
    caller must end cleanly instead of sleeping for the rest of the hour."""
    import progress
    clock = [1000.0]
    monkeypatch.setattr(graph_client.time, "time", lambda: clock[0])
    monkeypatch.setattr(graph_client.time, "sleep",
                        lambda s: clock.__setitem__(0, clock[0] + s))
    t = graph_client.Takt(pro_minute=2, pro_stunde=100, zeiten=[990.0, 995.0])
    t.warten()                       # the minute holds two: wait until 1050
    assert clock[0] >= 1050.0 and len(t.zeiten) == 3
    ereignisse = [progress.lies_event(z) for z in capsys.readouterr().out.splitlines()]
    assert sum(1 for e in ereignisse if e and e["k"] == "run.paced_wait") == 1
    voll = graph_client.Takt(pro_minute=100, pro_stunde=3, zeiten=[clock[0] - 10.0] * 3)
    with pytest.raises(graph_client.Ueberlastet):
        voll.warten()
    assert len(voll.zeiten) == 3, "nothing registered for a refused request"


# ---------------------------------------------------------------------------
# JSON batch: twenty small GETs per round trip
# ---------------------------------------------------------------------------
def _batch_antwort(anfragen, status=200, koerper=None, throttled=()):
    """A batch envelope answering every part – throttled ids get a 429."""
    return FakeResponse(payload={"responses": [
        {"id": r["id"], "status": 429 if r["id"] in throttled else status,
         "headers": {"Retry-After": "7"} if r["id"] in throttled else {},
         "body": (koerper or {}).get(r["id"], {"url": r["url"]})}
        for r in anfragen]})


def test_batch_get_buendelt_zwanzig_und_ordnet_die_antworten_zu(session):
    urls = [f"{graph_client.GRAPH}/me/messages/m{i}?$select=id" for i in range(45)]
    session.responses = [None, None, None]
    fake_post = session.post

    def post(url, headers=None, json=None, timeout=None):
        session.responses[0] = _batch_antwort(json["requests"])
        return fake_post(url, headers=headers, json=json, timeout=timeout)
    session.post = post
    g = _bare_graph()
    ergebnis = g.batch_get(urls)
    posts = [c for c in session.calls if c.get("post")]
    assert len(posts) == 3 and [len(c["json"]["requests"]) for c in posts] == [20, 20, 5]
    assert all(c["url"] == f"{graph_client.GRAPH}/$batch" for c in posts)
    assert posts[0]["json"]["requests"][0]["url"] == "/me/messages/m0?$select=id"
    assert len(ergebnis) == 45
    assert ergebnis[urls[44]] == (200, {"url": "/me/messages/m44?$select=id"})


def test_batch_get_wiederholt_gedrosselte_teile_und_reicht_404_durch(session, sleeps, monkeypatch):
    monkeypatch.setattr(graph_client, "_DROSSEL", {"bis": 0.0})
    urls = [f"{graph_client.GRAPH}/users/u1", f"{graph_client.GRAPH}/users/u2",
            f"{graph_client.GRAPH}/users/u3"]
    erste = FakeResponse(payload={"responses": [
        {"id": "0", "status": 200, "body": {"displayName": "Alice Beispiel"}},
        {"id": "1", "status": 404, "body": {"error": {"code": "Request_ResourceNotFound"}}},
        {"id": "2", "status": 429, "headers": {"Retry-After": "3"}, "body": {}}]})
    zweite = FakeResponse(payload={"responses": [
        {"id": "0", "status": 200, "body": {"displayName": "Bob Baumeister"}}]})
    session.responses = [erste, zweite]
    g = _bare_graph()
    ergebnis = g.batch_get(urls)
    assert ergebnis[urls[0]] == (200, {"displayName": "Alice Beispiel"})
    assert ergebnis[urls[1]][0] == 404
    assert ergebnis[urls[2]] == (200, {"displayName": "Bob Baumeister"})
    posts = [c for c in session.calls if c.get("post")]
    assert len(posts) == 2 and posts[1]["json"]["requests"][0]["url"] == "/users/u3"
    assert graph_client._DROSSEL["bis"] > 0, "der Teil-429 hat das Gatter nicht gesetzt"


def test_batch_get_trennt_beta_von_v1(session):
    beta = graph_client.GRAPH.replace("/v1.0", "/beta")
    urls = [f"{graph_client.GRAPH}/me", f"{beta}/planner/tasks/t1/messages"]
    session.responses = [
        FakeResponse(payload={"responses": [{"id": "0", "status": 200, "body": {}}]}),
        FakeResponse(payload={"responses": [{"id": "0", "status": 200, "body": {}}]})]
    g = _bare_graph()
    g.batch_get(urls)
    posts = [c for c in session.calls if c.get("post")]
    assert {c["url"] for c in posts} == {f"{graph_client.GRAPH}/$batch", f"{beta}/$batch"}


def test_batch_get_gibt_nach_der_leiter_auf(session, sleeps, monkeypatch):
    monkeypatch.setattr(graph_client, "_DROSSEL", {"bis": 0.0})
    url = f"{graph_client.GRAPH}/me/messages/m1"
    session.responses = [FakeResponse(payload={"responses": [
        {"id": "0", "status": 429, "headers": {}, "body": {}}]})
        for _ in range(graph_client.HTTP_RETRIES)]
    g = _bare_graph()
    with pytest.raises(graph_client.Ueberlastet):
        g.batch_get([url])
