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
