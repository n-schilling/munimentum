"""Tests for answer.py – turning found passages into a formulated answer.

Never touches the network: requests.post is always replaced. Two promises
take centre stage, because traceability depends on them:

  * The numbering in the context is the order of the hits. [1] must be the
    first hit in the list, otherwise the footnotes point at nothing.
  * stream() never raises. An error in the middle of the stream should
    arrive as an error chunk, not as an exception from a half-read body.
"""

import json

import pytest

import answer


class Strom:
    """Ollama's chat response: one JSON line per text chunk."""

    status_code = 200

    def __init__(self, stuecke, abbruch=None):
        self._stuecke = list(stuecke)
        self._abbruch = abbruch

    def raise_for_status(self):
        pass

    def iter_lines(self, decode_unicode=False):
        for i, st in enumerate(self._stuecke):
            if self._abbruch is not None and i == self._abbruch:
                raise ConnectionError("Verbindung weg")
            yield json.dumps({"message": {"content": st}, "done": False}).encode("utf-8")
        yield json.dumps({"message": {"content": ""}, "done": True}).encode("utf-8")


QUELLEN = [
    {"date": "2025-06-01 09:30", "who": "Alice", "source_label": "Teams",
     "title": "Projekt Alpha", "text": "Der Bericht ist fertig."},
    {"date": "2025-06-02 10:00", "who": "Bob", "source_label": "Mail",
     "title": "Rechnung 4711", "text": "Die Rechnung ist bezahlt."},
]


@pytest.fixture
def ollama(monkeypatch):
    """Replace requests.post; returns the calls it has seen."""
    gesehen = []

    def setze(antwort):
        def fake(url, json=None, timeout=None, stream=None):
            gesehen.append({"url": url, "json": json, "stream": stream})
            if isinstance(antwort, Exception):
                raise antwort
            return antwort
        monkeypatch.setattr("requests.post", fake)
        return gesehen
    return setze


# --------------------------------------------------------------------------
# Instructions to the model
# --------------------------------------------------------------------------
@pytest.mark.parametrize("lang,wort", [("de", "Quellennummern"), ("en", "source numbers"),
                                       ("fr", "numéros de source")])
def test_system_prompt_in_der_sprache_der_oberflaeche(lang, wort):
    """The rule is written in the same language as the desired answer – a
    small model then follows it more reliably."""
    assert wort in answer.system_prompt(lang)


def test_system_prompt_nennt_die_antwortsprache():
    assert "Deutsch" in answer.system_prompt("de")
    assert "English" in answer.system_prompt("en")
    assert "français" in answer.system_prompt("fr")


def test_system_prompt_bei_unbekannter_sprache():
    assert answer.system_prompt("kl") == answer.system_prompt("de")


# --------------------------------------------------------------------------
# Context: the numbering is the promise to the reader
# --------------------------------------------------------------------------
def test_build_context_nummeriert_in_trefferreihenfolge():
    ctx = answer.build_context(QUELLEN)
    assert ctx.index("[1]") < ctx.index("[2]")
    assert "[1] 2025-06-01 09:30 · Alice · Teams · Projekt Alpha" in ctx
    assert "Der Bericht ist fertig." in ctx
    assert "[2] 2025-06-02 10:00 · Bob · Mail · Rechnung 4711" in ctx


def test_build_context_kuerzt_lange_texte():
    lang = [{"date": "d", "who": "w", "source_label": "s", "title": "t",
             "text": "x" * 9000}]
    assert len(answer.build_context(lang, chars=100)) < 300


def test_build_context_vertraegt_leere_felder():
    ctx = answer.build_context([{"text": "nur Text"}])
    assert ctx.startswith("[1]") and "nur Text" in ctx


def test_build_messages_enthaelt_frage_und_kontext():
    msgs = answer.build_messages("Wer hat bezahlt?", QUELLEN, "de")
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert "Frage: Wer hat bezahlt?" in msgs[1]["content"]
    assert "Kontext:" in msgs[1]["content"]
    assert "Rechnung 4711" in msgs[1]["content"]


# --------------------------------------------------------------------------
# Streaming
# --------------------------------------------------------------------------
def test_stream_liefert_die_stuecke(ollama):
    gesehen = ollama(Strom(["Die ", "Rechnung ", "ist bezahlt [2]."]))
    stuecke = list(answer.stream("Frage?", QUELLEN, "m", "http://o.test", "de"))
    assert [s["text"] for s in stuecke] == ["Die ", "Rechnung ", "ist bezahlt [2]."]
    assert gesehen[0]["json"]["stream"] is True
    assert gesehen[0]["json"]["model"] == "m"
    assert gesehen[0]["url"] == "http://o.test/api/chat"


def test_stream_haengt_kein_leeres_stueck_an(ollama):
    ollama(Strom(["Text"]))
    assert all(s["text"] for s in answer.stream("F", QUELLEN, "m", "http://o.test"))


def test_stream_meldet_fehlendes_modell(ollama):
    class VierNullVier:
        status_code = 404

        def raise_for_status(self):
            raise AssertionError("darf nicht aufgerufen werden")
    ollama(VierNullVier())
    stuecke = list(answer.stream("F", QUELLEN, "fehlt:latest", "http://o.test"))
    assert stuecke == [{"error": "model", "detail": "fehlt:latest"}]


def test_stream_meldet_netzfehler_statt_zu_werfen(ollama):
    ollama(ConnectionError("kein Ollama"))
    stuecke = list(answer.stream("F", QUELLEN, "m", "http://o.test"))
    assert stuecke[-1]["error"] == "ollama" and "kein Ollama" in stuecke[-1]["detail"]


def test_stream_bricht_mitten_im_strom_sauber_ab(ollama):
    """If the connection drops after the second chunk, the first two arrive
    followed by an error chunk – no exception at the caller."""
    ollama(Strom(["eins ", "zwei ", "drei"], abbruch=2))
    stuecke = list(answer.stream("F", QUELLEN, "m", "http://o.test"))
    assert [s.get("text") for s in stuecke[:2]] == ["eins ", "zwei "]
    assert stuecke[-1]["error"] == "ollama"


def test_stream_ueberspringt_unlesbare_zeilen(ollama):
    class Krumm(Strom):
        def iter_lines(self, decode_unicode=False):
            yield b""
            yield b"kein json"
            yield json.dumps({"message": {"content": "gut"}, "done": True}).encode()
    ollama(Krumm([]))
    assert [s["text"] for s in answer.stream("F", QUELLEN, "m", "http://o.test")] == ["gut"]


def test_stream_endet_bei_done(ollama):
    class MitNachspann(Strom):
        def iter_lines(self, decode_unicode=False):
            yield json.dumps({"message": {"content": "A"}, "done": False}).encode()
            yield json.dumps({"message": {"content": "B"}, "done": True}).encode()
            yield json.dumps({"message": {"content": "danach"}}).encode()
    ollama(MitNachspann([]))
    assert [s["text"] for s in answer.stream("F", QUELLEN, "m", "http://o.test")] == ["A", "B"]


# --------------------------------------------------------------------------
# What goes into the chat request – two values the answer depends on
# --------------------------------------------------------------------------
def test_kontextfenster_wird_gesetzt(ollama):
    """Ollama's default is 2048 tokens. With up to 20 sources of 2000
    characters each the model would see one twentieth and answer about hits
    it never read."""
    gesehen = ollama(Strom(["ok"]))
    list(answer.stream("F", QUELLEN, "m", "http://o.test"))
    ktx = gesehen[0]["json"]["options"]["num_ctx"]
    zeichen = sum(len(m["content"]) for m in gesehen[0]["json"]["messages"])
    assert ktx >= zeichen / 4, "der Text passt nicht ins Fenster"
    # … and is not simply always the maximum: that is exactly what made it slow.
    assert ktx == answer.NUM_CTX_MIN < answer.NUM_CTX_MAX, (
        f"kurzer Text bekommt ein Fenster von {ktx}")


def test_kontextfenster_waechst_mit_dem_text():
    """A fixed large window is just as wrong as one that is too small:
    Ollama allocates the cache for the full length whether it is needed or
    not. On a machine with 24 GB and a 17 GB model that pushes into
    swapping - measured 2.4 instead of 5.5 tokens per second."""
    klein = answer.num_ctx([{"content": "x" * 400}])
    gross = answer.num_ctx([{"content": "x" * 40000}])
    assert klein == answer.NUM_CTX_MIN, "kurze Frage bekommt trotzdem ein grosses Fenster"
    assert gross > klein and gross <= answer.NUM_CTX_MAX
    # The largest case still fits.
    assert answer.num_ctx([{"content": "x" * 20 * answer.CHARS_PER_SOURCE}]) \
        <= answer.NUM_CTX_MAX


def test_denken_ist_abgeschaltet(ollama):
    """Qwen 3 otherwise thinks before every answer – that only costs time,
    and the chain of thought would flow into the stream as text."""
    gesehen = ollama(Strom(["ok"]))
    list(answer.stream("F", QUELLEN, "m", "http://o.test"))
    assert gesehen[0]["json"]["think"] is False


def test_der_kontext_passt_ins_fenster():
    """The largest case checked against the window: 20 sources, full length.
    Roughly four characters per token – if there is headroom, the math holds."""
    quellen = [{"date": "2025-06-01 09:30", "who": "Wer", "source_label": "Mail",
                "title": "Titel", "text": "x" * answer.CHARS_PER_SOURCE}
               for _ in range(20)]
    zeichen = len(answer.build_context(quellen))
    fenster = answer.num_ctx([{"content": answer.build_context(quellen)}])
    assert zeichen / 4 < fenster * 0.9, (
        f"{zeichen} Zeichen passen nicht mit Reserve in {fenster} Token")
    assert fenster <= answer.NUM_CTX_MAX
