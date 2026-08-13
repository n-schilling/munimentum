#!/usr/bin/env python3
"""
graph_client.py – der eine HTTP-Client für Microsoft Graph.

Bis 5.3 trug jedes Exportskript eine eigene Kopie dieser Schicht, und die
Kopien wichen in Kleinigkeiten voneinander ab: Teams verrechnete Netz- und
HTTP-Fehler in einem Zähler (ein ReadTimeout fraß dort einen 429-Versuch),
OneDrive kannte weder Drossel noch TokenExpired. Hier steht sie einmal:

    fetch()        ein GET; wiederholt NUR bei Netzwerkfehlern (Timeout,
                   Verbindungsabbruch, TLS). Der HTTP-Status wird nicht
                   bewertet – das ist Sache der Klassen darunter.
    Basis          Wiederholung bei 429/5xx (Retry-After wird respektiert),
                   401-Behandlung, Paging über @odata.nextLink, Byte- und
                   Streaming-Downloads.
    Graph          angemeldeter Zugriff über auth.Login; 401 erneuert den
                   Token und versucht es erneut.
    TokenClient    fertiger Bearer-Token (Graph Explorer); 401 heißt
                   TokenExpired – erneuern kann ihn nur der Benutzer.

Die Drossel (GATE) hält gleichzeitige Graph-Aufrufe unter dem Limit des
Postfachs; gewartet wird immer OHNE belegten Slot. konfiguriere() stellt sie
und den Connection-Pool auf die Zahl der Arbeiter ein – einmal je main().

Was die Skripte eigen haben, bleibt bei ihnen: Teams behandelt Inline-Bilder
bewusst nachsichtiger (Platzhalter statt Abbruch), OneDrive lädt Dateien
stückweise und pagt über Delta-Links.
"""

import threading
import time

import requests

import auth

GRAPH = "https://graph.microsoft.com/v1.0"

# Getrennte Timeouts für Verbindungsaufbau und Antwort. Graph liefert große
# Seiten und Downloads teils sehr träge; ein zu knapper Read-Timeout bricht
# sonst einen stundenlangen Export grundlos ab.
TIMEOUT_JSON = (30, 120)     # (connect, read) für Listen-/Metadaten-Abfragen
TIMEOUT_BYTES = (30, 300)    # (connect, read) für Downloads (.eml, Bilder)
NET_RETRIES = 6              # Wiederholungen bei Timeout/Verbindungsabbruch/TLS
HTTP_RETRIES = 6             # Wiederholungen bei 429/5xx bzw. nach Token-Erneuerung

# Geteilte HTTP-Session (Keep-Alive/Connection-Pooling) und Drossel-Gate.
SESSION = requests.Session()
GATE = threading.BoundedSemaphore(4)

TokenExpired = auth.TokenExpired


def konfiguriere(workers):
    """Drossel und Connection-Pool auf die Zahl der Arbeiter stellen.

    Einmal je main(), BEVOR Threads laufen: ein BoundedSemaphore lässt sich
    nicht nachträglich vergrößern, und der Standard-Pool von requests hielte
    bei mehr Arbeitern nicht genug Verbindungen offen.
    """
    global GATE
    GATE = threading.BoundedSemaphore(workers)
    SESSION.mount("https://", requests.adapters.HTTPAdapter(
        pool_connections=max(workers, 4), pool_maxsize=max(workers, 4)))


def _meld(text):
    """Fortschritt melden, ohne je einen Lauf aufzuhalten.

    Die Exportskripte stellen ihre Ausgabe auf UTF-8; eine Bibliothek verlässt
    sich nicht darauf. Auf einer Konsole mit Legacy-Codepage wirft print an
    „…“ einen UnicodeEncodeError – die Meldung ist verzichtbar, der Lauf nicht.
    """
    try:
        print(text)
    except ValueError:
        pass


def fetch(url, headers, params=None, timeout=TIMEOUT_JSON, stream=False, label=""):
    """Ein GET gegen Graph; wiederholt NUR bei Netzwerkfehlern.

    Eigener Zähler: ein Netzaussetzer soll die Versuche für 429/5xx nicht
    aufbrauchen. Ohne dieses Retry beendet ein einzelner ReadTimeout nach
    Stunden den kompletten Export.
    """
    # stream nur durchreichen, wenn gefordert – so bleiben schlanke
    # Session-Fakes in Tests ohne stream-Parameter gültig.
    extra = {"stream": True} if stream else {}
    for net in range(NET_RETRIES):
        try:
            with GATE:   # nur das eigentliche Request zählt gegen das Limit
                return SESSION.get(url, headers=headers, params=params,
                                   timeout=timeout, **extra)
        except requests.exceptions.RequestException as e:
            if net == NET_RETRIES - 1:
                raise
            w = min(2 ** net, 60)
            _meld(f"    … Netzwerkfehler{label} ({type(e).__name__}), warte {w}s "
                  f"(Versuch {net + 2}/{NET_RETRIES})")
            time.sleep(w)   # Pause OHNE belegten Slot
    raise RuntimeError(f"Zu viele Netzwerkfehler: {url}")   # nicht erreichbar


def warte_auf(r, versuch, was=""):
    """Backoff nach 429/5xx: Retry-After wenn beziffert, sonst exponentiell."""
    ra = r.headers.get("Retry-After")
    w = min(int(ra) if ra and ra.isdigit() else 2 ** versuch, 60)
    _meld(f"    … HTTP {r.status_code}{was}, warte {w}s (Drosselung/Server)")
    time.sleep(w)


class Basis:
    """Was beide Zugangsarten teilen: Wiederholung, Paging, Downloads."""

    def _headers(self):
        raise NotImplementedError

    def _erneuern(self):
        """401: Token erneuern und noch einmal – oder TokenExpired."""
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
        """Eine Streaming-Antwort (Status schon geprüft) – für große Dateien."""
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
            data = self.get(nxt, extra_headers=extra_headers)   # Link ist absolut


class Graph(Basis):
    """Angemeldeter Zugriff. Die Anmeldung selbst steckt in auth.Login."""

    def __init__(self, scopes=None, nur_still=False, anmeldung=None):
        self._refresh_lock = threading.Lock()
        if anmeldung is not None:      # Aufrufer hat schon angemeldet (Teams)
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
        with self._refresh_lock:   # nur ein Thread erneuert gleichzeitig
            self.anmeldung.erneuern()

    def _headers(self):
        return self.anmeldung.headers()

    def _erneuern(self):
        self._refresh()


class TokenClient(Basis):
    """Nutzt einen fertigen Bearer-Token; keine Anmeldung, kein Refresh."""

    def __init__(self, token):
        self.token = token
        self.account = None

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}
