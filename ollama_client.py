#!/usr/bin/env python3
"""
ollama_client.py – der eine Draht zum lokalen Modellserver.

Bis 5.3 sprachen vier Stellen einzeln mit Ollama: das Einbetten beim
Indexlauf (rag_index), das Einbetten der Suchanfrage (mcp_server), die
formulierte Antwort (answer) und die Statusfrage der Oberfläche (app). Jede
hatte ihre eigene URL-Vorgabe und ihr eigenes Fehlerbild. Hier liegt die
API einmal – kommt ein zweiter Modellserver (openai-kompatibel), lernt
genau dieses Modul seine Schnittstelle.

Die Fehlerpolitik bleibt bei den Aufrufern: ein Indexlauf will mit klarer
Meldung enden, die Suche still auf Volltext zurückfallen, die Antwort nie
eine Ausnahme in den Datenstrom werfen. Hier wird nur unterschieden, was
die Aufrufer unterscheiden müssen: „Modell nicht geladen" (404) gegenüber
allem anderen.

requests wird erst im Aufruf importiert – die Aufrufer tun das heute genauso,
damit der Import dieses Moduls nichts kostet.
"""

import json

DEFAULT_URL = "http://localhost:11434"


class ModellFehlt(RuntimeError):
    """Der Server läuft, aber das Modell ist nicht geladen (HTTP 404)."""

    def __init__(self, model):
        super().__init__(f"Modell nicht geladen: {model}")
        self.model = model


def embed(texts, model, url=DEFAULT_URL, timeout=600):
    """POST /api/embed für einen Stapel Texte -> Liste von Vektoren.

    Netz- und HTTP-Fehler kommen unverändert durch; nur der 404 wird als
    ModellFehlt übersetzt, weil jeder Aufrufer ihn anders beantworten will.
    """
    import requests
    r = requests.post(f"{url.rstrip('/')}/api/embed",
                      json={"model": model, "input": texts}, timeout=timeout)
    if r.status_code == 404:
        raise ModellFehlt(model)
    r.raise_for_status()
    data = r.json()
    embs = data.get("embeddings")
    if embs is None and "embedding" in data:      # ältere Single-Form
        embs = [data["embedding"]]
    if not embs:
        raise RuntimeError(f"Unerwartete Embedding-Antwort: {str(data)[:200]}")
    return embs


def chat_stream(messages, model, url=DEFAULT_URL, options=None, think=False,
                timeout=600):
    """POST /api/chat mit stream=True -> die NDJSON-Zeilen als dicts.

    Liefert jede geparste Zeile, wie sie kommt – was davon Text ist und wann
    Schluss ist ("done"), entscheidet der Aufrufer. 404 -> ModellFehlt.
    """
    import requests
    r = requests.post(f"{url.rstrip('/')}/api/chat",
                      json={"model": model, "stream": True, "think": think,
                            "options": options or {}, "messages": messages},
                      stream=True, timeout=timeout)
    if r.status_code == 404:
        raise ModellFehlt(model)
    r.raise_for_status()
    for zeile in r.iter_lines(decode_unicode=False):
        if not zeile:
            continue
        try:
            yield json.loads(zeile.decode("utf-8", "replace"))
        except ValueError:
            continue                  # Ollama schickt gelegentlich Leerzeilen


def tags(url=DEFAULT_URL, timeout=1.5):
    """GET /api/tags -> die Namen der geladenen Modelle.

    Der kurze Timeout ist Absicht: die Oberfläche fragt das im Statuspoll,
    und ein nicht laufender Server soll die Antwort nicht sekundenlang
    verzögern.
    """
    import requests
    r = requests.get(f"{url.rstrip('/')}/api/tags", timeout=timeout)
    r.raise_for_status()
    return [m.get("name", "") for m in (r.json().get("models") or [])]


def hat_modell(namen, gesucht):
    """"bge-m3" in der Liste heißt "bge-m3:latest" – ohne Tag vergleichen."""
    if not gesucht:
        return False
    rumpf = gesucht.split(":", 1)[0]
    return any(n == gesucht or n.split(":", 1)[0] == rumpf for n in namen)
