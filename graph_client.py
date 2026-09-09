#!/usr/bin/env python3
"""
graph_client.py – the one HTTP client for Microsoft Graph.

Each export script used to carry its own copy of this layer, and the copies
diverged in small ways: Teams counted network and HTTP errors in one counter
(a ReadTimeout ate a 429 attempt there), OneDrive knew neither throttling
nor TokenExpired. Here it lives once:

    fetch()        one GET; retries ONLY on network errors (timeout,
                   connection drop, TLS). The HTTP status is not judged –
                   that is the business of the classes below.
    Basis          retries on 429/5xx (Retry-After is respected), 401
                   handling, paging via @odata.nextLink, byte and
                   streaming downloads.
    Graph          signed-in access via auth.Login; a 401 renews the
                   token and tries again.
    TokenClient    ready-made bearer token (Graph Explorer); 401 means
                   TokenExpired – only the user can renew it.

The throttle (GATE) keeps concurrent Graph calls under the mailbox's limit;
waiting always happens WITHOUT holding a slot. konfiguriere() sizes it and
the connection pool to the number of workers – once per main().

What the scripts keep to themselves stays with them: Teams deliberately
treats inline images more leniently (placeholder instead of abort), OneDrive
downloads files in chunks and pages via delta links.
"""

import threading
import time

import requests

import auth
import progress

GRAPH = "https://graph.microsoft.com/v1.0"

# Separate timeouts for connection setup and response. Graph serves large
# pages and downloads rather sluggishly at times; a read timeout that is too
# tight would otherwise abort an hours-long export for no reason.
TIMEOUT_JSON = (30, 120)     # (connect, read) for list/metadata queries
TIMEOUT_BYTES = (30, 300)    # (connect, read) for downloads (.eml, images)
NET_RETRIES = 6              # retries on timeout/connection drop/TLS
HTTP_RETRIES = 6             # retries on 429/5xx or after token renewal

# Shared HTTP session (keep-alive/connection pooling) and throttle gate.
SESSION = requests.Session()
GATE = threading.BoundedSemaphore(4)

TokenExpired = auth.TokenExpired


def konfiguriere(workers):
    """Size the throttle and connection pool to the number of workers.

    Once per main(), BEFORE threads run: a BoundedSemaphore cannot be
    enlarged afterwards, and the default pool of requests would not keep
    enough connections open with more workers.
    """
    global GATE
    GATE = threading.BoundedSemaphore(workers)
    SESSION.mount("https://", requests.adapters.HTTPAdapter(
        pool_connections=max(workers, 4), pool_maxsize=max(workers, 4)))


# One throttle gate for the whole process: Graph throttles the app's
# budget, not the single connection. Neighbouring threads that keep firing
# only extend the gate – so every thread rests before every request.
_DROSSEL = {"bis": 0.0}
_DROSSEL_LOCK = threading.Lock()


def _drossel_warten():
    rest = _DROSSEL["bis"] - time.time()
    if rest > 0:
        time.sleep(rest)


def fetch(url, headers, params=None, timeout=TIMEOUT_JSON, stream=False, label=""):
    """One GET against Graph; retries ONLY on network errors.

    Its own counter: a network hiccup must not use up the attempts for
    429/5xx. Without this retry, a single ReadTimeout ends the whole export
    after hours.
    """
    # Pass stream through only when requested – this keeps lean session
    # fakes in tests valid without a stream parameter.
    extra = {"stream": True} if stream else {}
    for net in range(NET_RETRIES):
        _drossel_warten()
        try:
            with GATE:   # only the actual request counts against the limit
                return SESSION.get(url, headers=headers, params=params,
                                   timeout=timeout, **extra)
        except requests.exceptions.RequestException as e:
            if net == NET_RETRIES - 1:
                raise
            w = min(2 ** net, 60)
            progress.event("run.net_retry", "warn",
                           error=f"{type(e).__name__}{label}", s=w,
                           i=net + 2, n=NET_RETRIES)
            time.sleep(w)   # pause WITHOUT holding a slot
    raise RuntimeError(f"Zu viele Netzwerkfehler: {url}")   # unreachable


def warte_auf(r, versuch, was=""):
    """Backoff after 429/5xx: Retry-After when given, exponential otherwise.

    The waiting happens in _drossel_warten() before the next request – by
    every thread: the gate belongs to the process. Only whoever extends the
    gate reports it; sixteen simultaneous 429s are one log line, not
    sixteen."""
    ra = r.headers.get("Retry-After")
    w = min(int(ra), 300) if ra and ra.isdigit() else min(2 ** versuch, 60)
    bis = time.time() + w
    with _DROSSEL_LOCK:
        neu = bis > _DROSSEL["bis"]
        if neu:
            _DROSSEL["bis"] = bis
    if neu:
        progress.event("run.throttled", "warn", status=r.status_code, s=w)


class Basis:
    """What both access paths share: retries, paging, downloads."""

    def _headers(self):
        raise NotImplementedError

    def _erneuern(self):
        """401: renew the token and try once more – or TokenExpired."""
        raise TokenExpired()

    def get(self, url, params=None, extra_headers=None):
        headers = self._headers()
        if extra_headers:
            headers = {**headers, **extra_headers}
        for versuch in range(HTTP_RETRIES):
            r = fetch(url, headers, params, TIMEOUT_JSON)
            if r.status_code == 401:
                self._erneuern()
                headers = {**self._headers(), **(extra_headers or {})}
                continue
            if r.status_code == 429 or 500 <= r.status_code < 600:
                warte_auf(r, versuch)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Zu viele Fehlversuche: {url}")

    def get_bytes(self, url, timeout=TIMEOUT_BYTES, label=""):
        for versuch in range(HTTP_RETRIES):
            r = fetch(url, self._headers(), timeout=timeout, label=label)
            if r.status_code == 401:
                self._erneuern()
                continue
            if r.status_code == 429 or 500 <= r.status_code < 600:
                warte_auf(r, versuch, label)
                continue
            r.raise_for_status()
            return r.content, r.headers.get("Content-Type", "")
        raise RuntimeError(f"Zu viele Fehlversuche: {url}")

    def stream(self, url, timeout=TIMEOUT_BYTES, label=""):
        """A streaming response (status already checked) – for large files."""
        for versuch in range(HTTP_RETRIES):
            r = fetch(url, self._headers(), timeout=timeout, stream=True, label=label)
            if r.status_code == 401:
                self._erneuern()
                continue
            if r.status_code == 429 or 500 <= r.status_code < 600:
                warte_auf(r, versuch, label)
                continue
            r.raise_for_status()
            return r
        raise RuntimeError(f"Zu viele Fehlversuche: {url}")

    def paged(self, url, params=None, extra_headers=None):
        data = self.get(url, params, extra_headers)
        while True:
            yield from data.get("value", [])
            nxt = data.get("@odata.nextLink")
            if not nxt:
                break
            data = self.get(nxt, extra_headers=extra_headers)   # link is absolute


class Graph(Basis):
    """Signed-in access. The sign-in itself lives in auth.Login."""

    def __init__(self, scopes=None, nur_still=False, anmeldung=None):
        self._refresh_lock = threading.Lock()
        if anmeldung is not None:      # caller has already signed in (Teams)
            self.anmeldung = anmeldung
            return
        self.anmeldung = auth.Login(scopes)
        if not self.anmeldung.anmelden(nur_still=nur_still):
            raise SystemExit("Keine gültige Anmeldung im Zwischenspeicher.")

    @property
    def account(self):
        return self.anmeldung.account

    @property
    def token(self):
        return self.anmeldung.token

    @property
    def scopes(self):
        return self.anmeldung.scopes

    def _refresh(self):
        with self._refresh_lock:   # only one thread renews at a time
            self.anmeldung.erneuern()

    def _headers(self):
        return self.anmeldung.headers()

    def _erneuern(self):
        self._refresh()


class TokenClient(Basis):
    """Uses a ready-made bearer token; no sign-in, no refresh."""

    def __init__(self, token):
        self.token = token
        self.account = None

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}
