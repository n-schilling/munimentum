#!/usr/bin/env python3
"""
ollama_client.py – the one wire to the local model server.

Four places used to talk to Ollama individually: embedding during the index
run (rag_index), embedding the search query (mcp_server), the phrased
answer (answer) and the interface's status question (app). Each had its own
URL default and its own error picture. Here the API lives once – if a
second model server arrives (openai-compatible), exactly this module learns
its interface.

The error policy stays with the callers: an index run wants to end with a
clear message, the search to fall back silently to full text, the answer to
never throw an exception into the data stream. Only what the callers must
distinguish is distinguished here: "model not loaded" (404) versus
everything else.

requests is imported only inside the calls – the callers do the same today,
so importing this module costs nothing.
"""

import json

DEFAULT_URL = "http://localhost:11434"


class ModellFehlt(RuntimeError):
    """The server is running, but the model is not loaded (HTTP 404)."""

    def __init__(self, model):
        super().__init__(f"Modell nicht geladen: {model}")
        self.model = model


def embed(texts, model, url=DEFAULT_URL, timeout=600):
    """POST /api/embed for a batch of texts -> list of vectors.

    Network and HTTP errors pass through unchanged; only the 404 is
    translated into ModellFehlt, because every caller wants to answer it
    differently.
    """
    import requests
    r = requests.post(f"{url.rstrip('/')}/api/embed",
                      json={"model": model, "input": texts}, timeout=timeout)
    if r.status_code == 404:
        raise ModellFehlt(model)
    r.raise_for_status()
    data = r.json()
    embs = data.get("embeddings")
    if embs is None and "embedding" in data:      # older single form
        embs = [data["embedding"]]
    if not embs:
        raise RuntimeError(f"Unerwartete Embedding-Antwort: {str(data)[:200]}")
    return embs


def chat_stream(messages, model, url=DEFAULT_URL, options=None, think=False,
                timeout=600):
    """POST /api/chat with stream=True -> the NDJSON lines as dicts.

    Yields every parsed line as it comes – what of it is text and when it
    ends ("done") is the caller's call. 404 -> ModellFehlt.
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
            continue                  # Ollama occasionally sends blank lines


def tags(url=DEFAULT_URL, timeout=1.5):
    """GET /api/tags -> the names of the loaded models.

    The short timeout is deliberate: the interface asks this in its status
    poll, and a server that is not running must not delay the answer for
    seconds.
    """
    import requests
    r = requests.get(f"{url.rstrip('/')}/api/tags", timeout=timeout)
    r.raise_for_status()
    return [m.get("name", "") for m in (r.json().get("models") or [])]


def hat_modell(namen, gesucht):
    """"bge-m3" in the list means "bge-m3:latest" – compare without the tag."""
    if not gesucht:
        return False
    rumpf = gesucht.split(":", 1)[0]
    return any(n == gesucht or n.split(":", 1)[0] == rumpf for n in namen)
