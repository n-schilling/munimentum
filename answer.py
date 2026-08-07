#!/usr/bin/env python3
"""
answer.py – aus gefundenen Stellen eine Antwort formulieren lassen (lokal).

Die Suche bleibt, wie sie ist: sie findet Nachrichten und zeigt sie. Dieses
Modul setzt nur obendrauf – es schickt die *bereits gefundenen* Stellen an ein
Sprachmodell in Ollama und lässt daraus einen Absatz mit Quellenangaben machen.

Bewusst ohne eigene Suche: gäbe es hier ein zweites Retrieval, könnte die
Antwort Dinge zitieren, die in der Trefferliste gar nicht stehen – und niemand
könnte nachvollziehen, woher sie kommen. Die Nummern in eckigen Klammern
verweisen deshalb genau auf die Treffer, die daneben zu sehen sind.

Genutzt von app.py (Reiter „Suche“) und rag_server.py, damit die Antwortregeln
nicht an zwei Stellen auseinanderlaufen.

Alles läuft lokal: nichts geht an einen Dienst außerhalb des Rechners.
"""

import json

# Ollama liefert seine Antwort tokenweise. Bei einem 14B-Modell dauert ein
# Absatz je nach Rechner 20 bis 60 Sekunden – ohne Streaming starrt man so
# lange auf einen Wartehinweis, mit Streaming liest man einfach mit.
STREAM_TIMEOUT = 600

# Zeichen je Quelle im Kontext. Mehr Kontext heißt mehr Zeit und mehr Speicher,
# und ab einer gewissen Länge verliert ein kleines Modell eher den Faden, als
# dass es gewinnt.
CHARS_PER_SOURCE = 2000

SPRACHNAME = {"de": "Deutsch", "en": "English", "fr": "français"}

_REGELN = {
    "de": ("Du beantwortest Fragen ausschließlich anhand des bereitgestellten "
           "Kontexts aus E-Mails, Teams-Nachrichten, Terminen und Kontakten. "
           "Antworte auf {sprache}, knapp und präzise. Belege jede Aussage mit "
           "Quellennummern in eckigen Klammern, z. B. [2]. Wenn der Kontext die "
           "Frage nicht beantwortet, sage das ausdrücklich und rate nicht."),
    "en": ("Answer questions solely from the provided context of mail, Teams "
           "messages, appointments and contacts. Answer in {sprache}, briefly "
           "and precisely. Back every statement with source numbers in square "
           "brackets, e.g. [2]. If the context does not answer the question, "
           "say so plainly and do not guess."),
    "fr": ("Réponds uniquement à partir du contexte fourni (courriels, messages "
           "Teams, rendez-vous, contacts). Réponds en {sprache}, brièvement et "
           "précisément. Étaye chaque affirmation par des numéros de source "
           "entre crochets, par ex. [2]. Si le contexte ne répond pas à la "
           "question, dis-le clairement et n'invente rien."),
}


def system_prompt(lang="de"):
    """Anweisung an das Modell – in der Sprache der Oberfläche.

    Die Regel selbst steht in derselben Sprache wie die gewünschte Antwort:
    ein kleines Modell folgt einer Anweisung deutlich zuverlässiger, wenn sie
    nicht erst übersetzt werden muss.
    """
    code = lang if lang in _REGELN else "de"
    return _REGELN[code].format(sprache=SPRACHNAME.get(code, "Deutsch"))


def build_context(quellen, chars=CHARS_PER_SOURCE):
    """Die gefundenen Stellen als nummerierten Kontext.

    Die Nummerierung ist die Zusage an den Leser: [1] ist der erste Treffer in
    der Liste, [2] der zweite. Deshalb darf hier nichts umsortiert werden.
    """
    teile = []
    for n, q in enumerate(quellen, 1):
        kopf = " · ".join(str(x) for x in
                          (q.get("date"), q.get("who"), q.get("source_label"),
                           q.get("title")) if x)
        text = (q.get("text") or "")[:chars]
        teile.append(f"[{n}] {kopf}\n{text}")
    return "\n\n".join(teile)


def build_messages(query, quellen, lang="de", chars=CHARS_PER_SOURCE):
    frage = {"de": "Frage", "en": "Question", "fr": "Question"}.get(lang, "Frage")
    kontext = {"de": "Kontext", "en": "Context", "fr": "Contexte"}.get(lang, "Kontext")
    return [
        {"role": "system", "content": system_prompt(lang)},
        {"role": "user", "content": f"{kontext}:\n{build_context(quellen, chars)}\n\n"
                                    f"{frage}: {query}"},
    ]


def stream(query, quellen, model, ollama, lang="de", chars=CHARS_PER_SOURCE,
           timeout=STREAM_TIMEOUT):
    """Antwort stückweise erzeugen. Liefert Textstücke, wirft nie.

    Am Ende steht entweder nichts mehr (fertig) oder ein Fehlerstück – der
    Aufrufer soll sich nicht mit Ausnahmen aus einem laufenden Datenstrom
    herumschlagen müssen.
    """
    import requests
    try:
        r = requests.post(
            f"{ollama.rstrip('/')}/api/chat",
            json={"model": model, "stream": True, "options": {"temperature": 0.2},
                  "messages": build_messages(query, quellen, lang, chars)},
            stream=True, timeout=timeout)
        if r.status_code == 404:
            yield {"error": "model", "detail": model}
            return
        r.raise_for_status()
        for zeile in r.iter_lines(decode_unicode=False):
            if not zeile:
                continue
            try:
                daten = json.loads(zeile.decode("utf-8", "replace"))
            except ValueError:
                continue                      # Ollama schickt gelegentlich Leerzeilen
            stueck = (daten.get("message") or {}).get("content") or ""
            if stueck:
                yield {"text": stueck}
            if daten.get("done"):
                return
    except Exception as e:                    # noqa: BLE001 – nie den Aufrufer treffen
        yield {"error": "ollama", "detail": f"{type(e).__name__}: {e}"}


def complete(query, quellen, model, ollama, lang="de", chars=CHARS_PER_SOURCE,
             timeout=STREAM_TIMEOUT):
    """Dasselbe am Stück – für Aufrufer ohne Datenstrom (rag_server.py)."""
    teile, fehler = [], None
    for stueck in stream(query, quellen, model, ollama, lang, chars, timeout):
        if "error" in stueck:
            fehler = stueck
            break
        teile.append(stueck["text"])
    return "".join(teile), fehler
