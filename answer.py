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

# Ollamas Vorgabe für num_ctx ist 2048 Token. Bei bis zu 20 Quellen à 2000
# Zeichen sind das rund 40.000 Zeichen allein an Kontext – das Modell sähe
# davon ein Zwanzigstel und antwortete auf Treffer, die es nie gelesen hat.
#
# Ein fest eingestelltes großes Fenster ist aber genauso falsch: Ollama legt
# den Zwischenspeicher für die volle Länge an, ob sie gebraucht wird oder
# nicht. Auf einem Rechner mit 24 GB und einem 17-GB-Modell drückt das ins
# Auslagern, und die Antwort tröpfelt. Gemessen, mit acht Quellen:
#
#     32768 Token Fenster ->  2,4 Token/s   (Buchstabenkino)
#      8192 Token Fenster ->  5,5 Token/s
#
# Deshalb wird das Fenster jetzt nach dem tatsächlichen Text bemessen.
NUM_CTX_MIN, NUM_CTX_MAX = 4096, 32768
ANTWORT_RESERVE = 1024          # Token, die die Antwort selbst braucht


def num_ctx(messages):
    """Ein Fenster, das zum Text passt – auf die nächste Zweierpotenz gerundet.

    Vier Zeichen je Token ist grob, aber in der richtigen Richtung grob: die
    Schätzung fällt eher zu groß aus, und zu groß heißt hier nur „etwas mehr
    Reserve", während zu klein hieße, dass Quellen unter den Tisch fallen.
    """
    zeichen = sum(len(m.get("content") or "") for m in messages)
    gebraucht = zeichen // 4 + ANTWORT_RESERVE
    fenster = NUM_CTX_MIN
    while fenster < gebraucht and fenster < NUM_CTX_MAX:
        fenster *= 2
    return min(fenster, NUM_CTX_MAX)

# Qwen 3 denkt von Haus aus vor der Antwort. Für eine Zusammenfassung aus
# bereits gefundenen Stellen kostet das nur Zeit – und der Gedankengang liefe
# als Text mit in den Datenstrom, den die Oberfläche live anzeigt.
THINK = False

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
        messages = build_messages(query, quellen, lang, chars)
        r = requests.post(
            f"{ollama.rstrip('/')}/api/chat",
            json={"model": model, "stream": True, "think": THINK,
                  "options": {"temperature": 0.2, "num_ctx": num_ctx(messages)},
                  "messages": messages},
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
