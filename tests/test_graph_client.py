"""Tests für graph_client.py – Retry, Drosselung, Paging, Token-Behandlung.

Bis 5.3 trug jedes Exportskript eine eigene Kopie dieser Schicht samt eigener
Tests; hier steht beides einmal. Alles ohne Netzwerk: SESSION wird durch einen
Fake ersetzt.
"""

import threading

import pytest
import requests

import graph_client


class FakeResponse:
    """Nachgebaute requests-Response ohne Netzwerk."""

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
    """Liefert vorbereitete Antworten der Reihe nach und protokolliert Aufrufe."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None, stream=False):
        self.calls.append({"url": url, "headers": dict(headers or {}),
                           "params": params, "timeout": timeout, "stream": stream})
        r = self.responses.pop(0)
        if isinstance(r, Exception):   # Netzwerkfehler simulieren
            raise r
        return r


@pytest.fixture
def session(monkeypatch):
    """Fake-Session einhängen; Antworten setzt der Test über .responses."""
    fake = FakeSession([])
    monkeypatch.setattr(graph_client, "SESSION", fake)
    return fake


@pytest.fixture
def sleeps(monkeypatch):
    """time.sleep abklemmen und die gewünschten Wartezeiten mitschreiben.

    Die Drosselsperre ist prozessweit und rechnet mit der echten Uhr – vor
    jedem Test auf null, sonst wartete ein Test auf die Sperre des vorigen.
    Die mitgeschriebenen Zeiten sind Restzeiten (float), gerundet vergleichen.
    """
    graph_client._DROSSEL["bis"] = 0.0
    calls = []
    monkeypatch.setattr(graph_client.time, "sleep", lambda s: calls.append(s))
    yield calls
    graph_client._DROSSEL["bis"] = 0.0


# --------------------------------------------------------------------------
# TokenClient: Retry, Drosselung, Paging, TokenExpired
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
    # Retry-After wird respektiert; gewartet wird vor dem Folge-Request.
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
    # Exponentielles Backoff vor jedem Folge-Request; nach dem letzten
    # Fehlversuch wird nicht mehr gewartet – aufgeben kostet keine 32 s.
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
    # Folgeseite ohne die ursprünglichen Params (nextLink enthält sie bereits)
    assert session.calls[1]["url"] == "https://example.invalid/p2"
    assert session.calls[1]["params"] is None


# --------------------------------------------------------------------------
# Netzwerkfehler: Timeout/Verbindungsabbruch werden wiederholt statt zu beenden
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
    """Ein Aussetzer darf die Versuche für 429/5xx nicht aufbrauchen."""
    session.responses = ([_timeout()]
                         + [FakeResponse(500) for _ in range(graph_client.HTTP_RETRIES - 1)]
                         + [FakeResponse(payload={"ok": 1})])
    assert graph_client.TokenClient("t").get("https://example.invalid/x") == {"ok": 1}


def test_get_bytes_wiederholt_nach_netzwerkfehler(session, sleeps):
    session.responses = [_timeout(), FakeResponse(content=b"MIME")]
    content, _ = graph_client.TokenClient("t").get_bytes("https://example.invalid/m")
    assert content == b"MIME"


# --------------------------------------------------------------------------
# Graph-Client: Token-Erneuerung bei 401 (ohne echte Anmeldung)
# --------------------------------------------------------------------------
class _StubAnmeldung:
    """Nur was die HTTP-Schicht von auth.Login braucht. Die Anmeldung selbst
    hat eigene Tests (test_auth.py); hier geht es um Retry und Paging."""

    def __init__(self, token="alt"):
        self.token = token

    def headers(self):
        return {"Authorization": f"Bearer {self.token}"}


def _bare_graph():
    """Graph-Instanz ohne interaktiven Login."""
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
    """Nur ein Thread erneuert gleichzeitig – der Lock existiert und wird benutzt."""
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
# stream() und konfiguriere()
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
        # BoundedSemaphore(2): zweimal belegen geht, dreimal nicht
        assert graph_client.GATE.acquire(blocking=False)
        assert graph_client.GATE.acquire(blocking=False)
        assert not graph_client.GATE.acquire(blocking=False)
        assert gemountet["prefix"] == "https://"
        assert gemountet["adapter"]._pool_maxsize == 4   # Untergrenze 4
    finally:
        graph_client.GATE = alt


def test_gate_wird_um_das_request_gehalten(session):
    """Das Request läuft im GATE; gewartet wird ohne belegten Slot."""
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
    """Mehr Threads als Slots: alle kommen durch, keiner verhungert."""
    session.responses = [FakeResponse(payload={"ok": 1})] * 8
    lock = threading.Lock()
    echt_get = session.get

    def sicher_get(*a, **kw):
        with lock:                       # FakeSession.pop ist nicht threadsicher
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
    """Sechzehn Fäden, ein 429: gemeldet wird einmal, gewartet von jedem, der
    danach anfragt – die Sperre gehört dem Prozess, nicht der Verbindung."""
    import progress

    session.responses = [FakeResponse(429, headers={"Retry-After": "7"}),
                         FakeResponse(payload={"ok": 1}),
                         FakeResponse(payload={"ok": 2})]
    tc = graph_client.TokenClient("t")
    assert tc.get("https://example.invalid/a") == {"ok": 1}
    assert tc.get("https://example.invalid/b") == {"ok": 2}   # zweiter Aufruf
    events = [e for e in (progress.lies_event(z) for z in
                          capsys.readouterr().out.splitlines())
              if e and e["k"] == "run.throttled"]
    assert len(events) == 1 and events[0]["v"]["s"] == 7
    # Beide Folge-Requests warteten die Sperre ab (Restzeit jeweils ~7 s).
    assert [round(s) for s in sleeps] == [7, 7]
