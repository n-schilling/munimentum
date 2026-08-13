"""Tests für answer.py – aus gefundenen Stellen eine Antwort formulieren lassen.

Nie ins Netz: requests.post wird immer ersetzt. Zwei Zusagen stehen im
Mittelpunkt, weil an ihnen die Nachvollziehbarkeit hängt:

  * Die Nummerierung im Kontext ist die Reihenfolge der Treffer. [1] muss der
    erste Treffer in der Liste sein, sonst zeigen die Fußnoten ins Leere.
  * stream() wirft nie. Ein Fehler mitten im Datenstrom soll als Fehlerstück
    ankommen, nicht als Ausnahme aus einem halb gelesenen Körper.
"""

import json

import pytest

import answer


class Strom:
    """Ollamas Chat-Antwort: eine JSON-Zeile je Textstück."""

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
    """requests.post ersetzen; liefert die gesehenen Aufrufe."""
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
# Anweisung an das Modell
# --------------------------------------------------------------------------
@pytest.mark.parametrize("lang,wort", [("de", "Quellennummern"), ("en", "source numbers"),
                                       ("fr", "numéros de source")])
def test_system_prompt_in_der_sprache_der_oberflaeche(lang, wort):
    """Die Regel steht in derselben Sprache wie die gewünschte Antwort – ein
    kleines Modell folgt ihr dann zuverlässiger."""
    assert wort in answer.system_prompt(lang)


def test_system_prompt_nennt_die_antwortsprache():
    assert "Deutsch" in answer.system_prompt("de")
    assert "English" in answer.system_prompt("en")
    assert "français" in answer.system_prompt("fr")


def test_system_prompt_bei_unbekannter_sprache():
    assert answer.system_prompt("kl") == answer.system_prompt("de")


# --------------------------------------------------------------------------
# Kontext: die Nummerierung ist die Zusage an den Leser
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
    """Reißt die Verbindung nach dem zweiten Stück, sind die ersten beiden da
    und danach ein Fehlerstück – keine Ausnahme beim Aufrufer."""
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
# Was im Chat-Request steht – zwei Werte, an denen die Antwort haengt
# --------------------------------------------------------------------------
def test_kontextfenster_wird_gesetzt(ollama):
    """Ollamas Vorgabe ist 2048 Token. Bei bis zu 20 Quellen à 2000 Zeichen
    saehe das Modell ein Zwanzigstel und antwortete auf Treffer, die es nie
    gelesen hat."""
    gesehen = ollama(Strom(["ok"]))
    list(answer.stream("F", QUELLEN, "m", "http://o.test"))
    ktx = gesehen[0]["json"]["options"]["num_ctx"]
    zeichen = sum(len(m["content"]) for m in gesehen[0]["json"]["messages"])
    assert ktx >= zeichen / 4, "der Text passt nicht ins Fenster"
    # … und ist nicht einfach immer das Maximum: genau daran hing die Langsamkeit.
    assert ktx == answer.NUM_CTX_MIN < answer.NUM_CTX_MAX, (
        f"kurzer Text bekommt ein Fenster von {ktx}")


def test_kontextfenster_waechst_mit_dem_text():
    """Ein fest eingestelltes grosses Fenster ist genauso falsch wie ein zu
    kleines: Ollama legt den Zwischenspeicher fuer die volle Laenge an, ob sie
    gebraucht wird oder nicht. Auf einem Rechner mit 24 GB und einem 17-GB-
    Modell drueckt das ins Auslagern - gemessen 2,4 statt 5,5 Token je Sekunde."""
    klein = answer.num_ctx([{"content": "x" * 400}])
    gross = answer.num_ctx([{"content": "x" * 40000}])
    assert klein == answer.NUM_CTX_MIN, "kurze Frage bekommt trotzdem ein grosses Fenster"
    assert gross > klein and gross <= answer.NUM_CTX_MAX
    # Der groesste Fall passt noch hinein.
    assert answer.num_ctx([{"content": "x" * 20 * answer.CHARS_PER_SOURCE}]) \
        <= answer.NUM_CTX_MAX


def test_denken_ist_abgeschaltet(ollama):
    """Qwen 3 denkt sonst vor jeder Antwort – das kostet nur Zeit, und der
    Gedankengang liefe als Text mit in den Datenstrom."""
    gesehen = ollama(Strom(["ok"]))
    list(answer.stream("F", QUELLEN, "m", "http://o.test"))
    assert gesehen[0]["json"]["think"] is False


def test_der_kontext_passt_ins_fenster():
    """Groesster Fall gegen das Fenster gerechnet: 20 Quellen, volle Laenge.
    Grob vier Zeichen je Token – bleibt Luft, ist die Rechnung in Ordnung."""
    quellen = [{"date": "2025-06-01 09:30", "who": "Wer", "source_label": "Mail",
                "title": "Titel", "text": "x" * answer.CHARS_PER_SOURCE}
               for _ in range(20)]
    zeichen = len(answer.build_context(quellen))
    fenster = answer.num_ctx([{"content": answer.build_context(quellen)}])
    assert zeichen / 4 < fenster * 0.9, (
        f"{zeichen} Zeichen passen nicht mit Reserve in {fenster} Token")
    assert fenster <= answer.NUM_CTX_MAX
