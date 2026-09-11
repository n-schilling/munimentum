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

Some services ration by count, not by concurrency: OneNote allows 120
requests a minute and 400 an hour per user, answers a 429 without
Retry-After, and every retry counts against the same budget. For those the
pacer (takt) spaces the requests out BEFORE they go, so the budget is spent
on content instead of on refusals; Ueberlastet is what a caller gets when
the budget is gone anyway – its cue to stop, not to hammer on.

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


class Ueberlastet(RuntimeError):
    """The service keeps answering 429 after every retry: the budget for
    this window is spent. A caller with a budgeted service ends its run
    cleanly on this instead of paying a full retry ladder per request."""


class Takt:
    """A request pacer over sliding windows – "at most N per minute, M per
    hour".

    Every request registers its moment; before a request the pacer waits
    until both windows have room. The moments can be persisted by the
    caller (a state.db value) so the next process knows what this hour has
    already cost – the service does not forget between runs."""

    def __init__(self, pro_minute=None, pro_stunde=None, zeiten=None):
        self.grenzen = [(60.0, int(pro_minute)) for _ in [0] if pro_minute] + \
            [(3600.0, int(pro_stunde)) for _ in [0] if pro_stunde]
        self.zeiten = [float(t) for t in (zeiten or ())]
        self._lock = threading.Lock()

    def _aufraeumen(self, jetzt):
        aeltestes = jetzt - max((f for f, _n in self.grenzen), default=0.0)
        self.zeiten = [t for t in self.zeiten if t > aeltestes]

    def wartezeit(self, jetzt=None):
        """Seconds until the next request fits – 0 when it fits now."""
        jetzt = time.time() if jetzt is None else jetzt
        with self._lock:
            self._aufraeumen(jetzt)
            warten = 0.0
            for fenster, n in self.grenzen:
                im_fenster = [t for t in self.zeiten if t > jetzt - fenster]
                if len(im_fenster) >= n:
                    warten = max(warten, im_fenster[-n] + fenster - jetzt)
            return max(0.0, warten)

    def warten(self):
        """Block until a request fits, then register it.

        A pause is said once when it is noticeable – a run that stands
        still without a word looks hung. A wait longer than WARTE_MAX is
        not a pause but a spent window: the caller ends its run cleanly on
        Ueberlastet instead of sleeping through the rest of the hour."""
        gemeldet = False
        while True:
            w = self.wartezeit()
            if w <= 0:
                break
            if w > WARTE_MAX:
                raise Ueberlastet(f"Kontingent voll – nächste Anfrage erst in {int(w)} s")
            if not gemeldet and w >= 5:
                progress.event("run.paced_wait", "warn", s=int(round(w)))
                gemeldet = True
            time.sleep(min(w, 60.0))
        with self._lock:
            self.zeiten.append(time.time())

    def verbraucht(self, fenster=3600.0, jetzt=None):
        """Requests registered within the window – for budget decisions."""
        jetzt = time.time() if jetzt is None else jetzt
        with self._lock:
            return sum(1 for t in self.zeiten if t > jetzt - fenster)


TAKT = None          # the process-wide pacer; None means unpaced (the default)
WARTE_MAX = 120.0    # longer than this is a spent window, not a pause


def takt(pro_minute=None, pro_stunde=None, zeiten=None):
    """Pace every request of this process – once per main(), for services
    that ration by count. Returns the pacer so the caller can persist its
    moments."""
    global TAKT
    TAKT = Takt(pro_minute, pro_stunde, zeiten) if (pro_minute or pro_stunde) else None
    return TAKT


def fetch(url, headers, params=None, timeout=TIMEOUT_JSON, stream=False, label="",
          json_body=None):
    """One GET against Graph (a POST when `json_body` is given – the JSON
    batch is the only caller); retries ONLY on network errors.

    Its own counter: a network hiccup must not use up the attempts for
    429/5xx. Without this retry, a single ReadTimeout ends the whole export
    after hours.
    """
    # Pass stream through only when requested – this keeps lean session
    # fakes in tests valid without a stream parameter.
    extra = {"stream": True} if stream else {}
    for net in range(NET_RETRIES):
        _drossel_warten()
        if TAKT is not None:
            TAKT.warten()
        try:
            with GATE:   # only the actual request counts against the limit
                if json_body is not None:
                    return SESSION.post(url, headers=headers, json=json_body,
                                        timeout=timeout)
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
    _drosseln(_wartezeit(r.headers, r.status_code, versuch), r.status_code)


def _wartezeit(headers, status, versuch):
    ra = (headers or {}).get("Retry-After")
    if ra and str(ra).isdigit():
        return min(int(ra), 300)
    if TAKT is not None and status == 429:
        # A paced service refuses without saying for how long: its minute
        # window is the honest wait, not a guess that starts at a second.
        return 60
    return min(2 ** versuch, 60)


def _drosseln(w, status):
    bis = time.time() + w
    with _DROSSEL_LOCK:
        neu = bis > _DROSSEL["bis"]
        if neu:
            _DROSSEL["bis"] = bis
    if neu:
        progress.event("run.throttled", "warn", status=status, s=w)


def _aufgeben(r, versuch):
    """Under pacing a second 429 in a row is the spent hour, not a hiccup:
    every further attempt would cost a request of the next hour and be
    refused too. Without pacing the ladder runs as before."""
    return TAKT is not None and r.status_code == 429 and versuch >= 1


def _erschoepft(r, url):
    """The retry ladder ran out: a 429 at the end means a spent budget."""
    if r is not None and getattr(r, "status_code", None) == 429:
        return Ueberlastet(f"Zu viele Fehlversuche (429): {url}")
    return RuntimeError(f"Zu viele Fehlversuche: {url}")


BATCH_GROESSE = 20           # Graph's cap on requests per JSON batch


def _batch_ziel(url):
    """Where a URL's batch goes and how it is spelled inside: the batch
    endpoint is version-bound, the parts are relative to it."""
    for basis in (GRAPH, GRAPH.replace("/v1.0", "/beta")):
        if url.startswith(basis + "/"):
            return basis + "/$batch", url[len(basis):]
    raise ValueError(f"Not a Graph address: {url}")


class Basis:
    """What both access paths share: retries, paging, downloads, batches."""

    def _headers(self):
        raise NotImplementedError

    def _erneuern(self):
        """401: renew the token and try once more – or TokenExpired."""
        raise TokenExpired()

    def get(self, url, params=None, extra_headers=None):
        return self._json(url, params, extra_headers)

    def _json(self, url, params=None, extra_headers=None, json_body=None):
        headers = self._headers()
        if extra_headers:
            headers = {**headers, **extra_headers}
        r = None
        for versuch in range(HTTP_RETRIES):
            r = fetch(url, headers, params, TIMEOUT_JSON, json_body=json_body)
            if r.status_code == 401:
                self._erneuern()
                headers = {**self._headers(), **(extra_headers or {})}
                continue
            if r.status_code == 429 or 500 <= r.status_code < 600:
                if _aufgeben(r, versuch):
                    break
                warte_auf(r, versuch)
                continue
            r.raise_for_status()
            return r.json()
        raise _erschoepft(r, url)

    def get_bytes(self, url, timeout=TIMEOUT_BYTES, label=""):
        r = None
        for versuch in range(HTTP_RETRIES):
            r = fetch(url, self._headers(), timeout=timeout, label=label)
            if r.status_code == 401:
                self._erneuern()
                continue
            if r.status_code == 429 or 500 <= r.status_code < 600:
                if _aufgeben(r, versuch):
                    break
                warte_auf(r, versuch, label)
                continue
            r.raise_for_status()
            return r.content, r.headers.get("Content-Type", "")
        raise _erschoepft(r, url)

    def stream(self, url, timeout=TIMEOUT_BYTES, label=""):
        """A streaming response (status already checked) – for large files."""
        r = None
        for versuch in range(HTTP_RETRIES):
            r = fetch(url, self._headers(), timeout=timeout, stream=True, label=label)
            if r.status_code == 401:
                self._erneuern()
                continue
            if r.status_code == 429 or 500 <= r.status_code < 600:
                if _aufgeben(r, versuch):
                    break
                warte_auf(r, versuch, label)
                continue
            r.raise_for_status()
            return r
        raise _erschoepft(r, url)

    def batch_get(self, urls, extra_headers=None):
        """Many small GETs as JSON batches of twenty: {url: (status, body)}.

        The per-item round trips this replaces – a tombstone check per
        mail, a name per user id, the metadata per attachment – used to run
        one by one. A part answered 429/5xx is retried in a later batch,
        after the wait one request would take (the part's Retry-After, else
        the ladder); any other status is an answer and is handed back as it
        is – a 404 says "gone", it is not a failure. A 401 on the envelope
        renews the token like a single request would."""
        ergebnis = {}
        offen = list(dict.fromkeys(urls))
        letzter = None
        for versuch in range(HTTP_RETRIES):
            if not offen:
                break
            gruppen = {}
            for url in offen:
                endpunkt, rel = _batch_ziel(url)
                gruppen.setdefault(endpunkt, []).append((url, rel))
            naechste, warten, erneuern = [], None, False
            for endpunkt, teile in gruppen.items():
                for i in range(0, len(teile), BATCH_GROESSE):
                    stueck = teile[i:i + BATCH_GROESSE]
                    koerper = {"requests": [
                        {"id": str(n), "method": "GET", "url": rel,
                         **({"headers": extra_headers} if extra_headers else {})}
                        for n, (_url, rel) in enumerate(stueck)]}
                    antwort = self._json(endpunkt, json_body=koerper)
                    gesehen = set()
                    for a in antwort.get("responses") or []:
                        try:
                            url = stueck[int(a.get("id"))][0]
                        except (TypeError, ValueError, IndexError):
                            continue
                        gesehen.add(url)
                        status = int(a.get("status") or 0)
                        if status == 429 or 500 <= status < 600:
                            letzter = status
                            naechste.append(url)
                            w = _wartezeit(a.get("headers"), status, versuch)
                            warten = max(warten or 0, w)
                        elif status == 401:
                            # The token ran out between two batches: renew
                            # like a single request would, then ask again.
                            letzter = status
                            naechste.append(url)
                            erneuern = True
                        else:
                            ergebnis[url] = (status, a.get("body"))
                    # A part the envelope did not answer counts as a retry.
                    naechste.extend(u for u, _rel in stueck if u not in gesehen)
            if erneuern:
                self._erneuern()
            if warten is not None:
                _drosseln(warten, letzter or 429)
            offen = naechste
        if offen:
            if letzter == 429:
                raise Ueberlastet(f"Zu viele Fehlversuche (429): {offen[0]}")
            raise RuntimeError(f"Zu viele Fehlversuche: {offen[0]}")
        return ergebnis

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
