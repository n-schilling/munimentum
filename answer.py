#!/usr/bin/env python3
"""
answer.py – have an answer written from the places already found (locally).

Search stays as it is: it finds messages and shows them. This module only
sits on top – it sends the *already found* passages to a language model in
Ollama and has it produce a paragraph with source references.

Deliberately without its own search: with a second retrieval here, the
answer could cite things that are nowhere in the hit list – and nobody
could trace where they came from. The numbers in square brackets therefore
point exactly at the hits visible right next to the answer.

Everything runs locally: nothing leaves the machine for an outside service.
"""

import ollama_client

# Ollama delivers its answer token by token. With a 14B model a paragraph
# takes 20 to 60 seconds depending on the machine – without streaming you
# stare at a spinner that long, with streaming you simply read along.
STREAM_TIMEOUT = 600

# Characters per source in the context. More context means more time and
# more memory, and beyond a certain length a small model loses the thread
# rather than gaining anything.
CHARS_PER_SOURCE = 2000

# Ollama's default for num_ctx is 2048 tokens. With up to 20 sources of
# 2000 characters each that is around 40,000 characters of context alone –
# the model would see a twentieth of it and answer about hits it never read.
#
# A fixed large window is just as wrong though: Ollama allocates the cache
# for the full length whether it is needed or not. On a machine with 24 GB
# and a 17 GB model that pushes into swapping, and the answer trickles.
# Measured, with eight sources:
#
#     32768 token window ->  2.4 tokens/s   (watching letters crawl)
#      8192 token window ->  5.5 tokens/s
#
# So the window is sized to the actual text instead.
NUM_CTX_MIN, NUM_CTX_MAX = 4096, 32768
ANTWORT_RESERVE = 1024          # tokens the answer itself needs


def num_ctx(messages):
    """A window that fits the text – rounded up to the next power of two.

    Four characters per token is rough, but rough in the right direction:
    the estimate errs on the large side, and too large only means "a bit
    more headroom", while too small would mean sources get dropped.
    """
    zeichen = sum(len(m.get("content") or "") for m in messages)
    gebraucht = zeichen // 4 + ANTWORT_RESERVE
    fenster = NUM_CTX_MIN
    while fenster < gebraucht and fenster < NUM_CTX_MAX:
        fenster *= 2
    return min(fenster, NUM_CTX_MAX)

# Qwen 3 thinks before answering by default. For a summary of already
# found passages that only costs time – and the chain of thought would run
# as text through the stream the UI displays live.
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
    """Instruction to the model – in the language of the UI.

    The rule itself is written in the same language as the desired answer:
    a small model follows an instruction far more reliably when it does not
    have to translate it first.
    """
    code = lang if lang in _REGELN else "de"
    return _REGELN[code].format(sprache=SPRACHNAME.get(code, "Deutsch"))


def build_context(quellen, chars=CHARS_PER_SOURCE):
    """The found passages as numbered context.

    The numbering is the promise to the reader: [1] is the first hit in
    the list, [2] the second. Which is why nothing may be reordered here.
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
    """Produce the answer piece by piece. Yields text chunks, never raises.

    At the end there is either nothing more (done) or an error chunk – the
    caller should not have to wrestle with exceptions out of a running
    stream. The HTTP part lives in ollama_client.
    """
    try:
        messages = build_messages(query, quellen, lang, chars)
        for daten in ollama_client.chat_stream(
                messages, model, ollama, think=THINK,
                options={"temperature": 0.2, "num_ctx": num_ctx(messages)},
                timeout=timeout):
            stueck = (daten.get("message") or {}).get("content") or ""
            if stueck:
                yield {"text": stueck}
            if daten.get("done"):
                return
    except ollama_client.ModellFehlt:
        yield {"error": "model", "detail": model}
    except Exception as e:                    # noqa: BLE001 – never hit the caller
        yield {"error": "ollama", "detail": f"{type(e).__name__}: {e}"}
