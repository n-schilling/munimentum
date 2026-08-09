"""Tests für i18n.py und die Sprachdateien.

Der wichtigste Test steht ganz unten: er liest jeden Textschlüssel aus app.py –
aus dem Markup, aus dem JavaScript und aus den serverseitigen Meldungen – und
prüft ihn gegen die Sprachdateien. Beide Richtungen: kein Schlüssel ohne Text,
kein Text ohne Verwendung. Damit fällt auf, wenn jemand einen Text ergänzt und
eine Sprache vergisst, und ebenso, wenn eine Zeile verwaist zurückbleibt.
"""

import json
import re
from pathlib import Path

import pytest

import app as app_mod
import i18n

SPRACHEN = ("de", "en", "fr")
LANG = Path(__file__).resolve().parent.parent / "lang"


@pytest.fixture(autouse=True)
def sauber():
    i18n.reset()
    yield
    i18n.reset()


def roh(code):
    d = json.loads((LANG / f"{code}.json").read_text(encoding="utf-8"))
    d.pop("_meta", None)
    return d


# --------------------------------------------------------------------------
# Sprachdateien finden und lesen
# --------------------------------------------------------------------------
def test_available_listet_alle_sprachen():
    codes = [e["code"] for e in i18n.available()]
    assert set(codes) >= set(SPRACHEN)
    assert codes[0] == i18n.FALLBACK          # Quellsprache zuerst
    namen = {e["code"]: e["name"] for e in i18n.available()}
    # Jede Sprache nennt sich in ihrer eigenen Sprache – sonst müsste man die
    # Auswahl erst übersetzen, um sie zu finden.
    assert namen["de"] == "Deutsch" and namen["en"] == "English"
    assert namen["fr"] == "Français"


def test_available_bei_leerem_ordner(tmp_path):
    (tmp_path / "lang").mkdir()
    assert i18n.available(tmp_path) == []


def test_strings_ergaenzt_fehlende_aus_der_quellsprache(tmp_path):
    """Eine unvollständige Übersetzung darf keine leeren Stellen hinterlassen."""
    d = tmp_path / "lang"
    d.mkdir()
    (d / "de.json").write_text(json.dumps({"a": "Ah", "b": "Beh"}), encoding="utf-8")
    (d / "xx.json").write_text(json.dumps({"_meta": {"code": "xx", "name": "X"},
                                           "a": "Ay", "b": ""}), encoding="utf-8")
    s = i18n.strings("xx", tmp_path)
    assert s == {"a": "Ay", "b": "Beh"}       # leerer Text zählt als fehlend
    assert "_meta" not in s


def test_strings_bei_unbekannter_sprache_ist_die_quellsprache():
    assert i18n.strings("kl") == i18n.strings("de")


def test_kaputte_sprachdatei_wird_ignoriert(tmp_path):
    d = tmp_path / "lang"
    d.mkdir()
    (d / "de.json").write_text('{"a": "Ah"}', encoding="utf-8")
    (d / "xx.json").write_text("{kein json", encoding="utf-8")
    assert i18n.strings("xx", tmp_path) == {"a": "Ah"}


# --------------------------------------------------------------------------
# Sprachwahl
# --------------------------------------------------------------------------
@pytest.mark.parametrize("header,erwartet", [
    ("de-DE,de;q=0.9,en;q=0.8", "de"),
    ("en-US,en;q=0.9", "en"),
    ("fr-CH,fr;q=0.9,de;q=0.8", "fr"),          # Regionalcode zählt für die Sprache
    ("en;q=0.5,fr;q=0.9", "fr"),                # Gewichtung schlägt Reihenfolge
    ("it-IT,it;q=0.9", "de"),                   # nichts passt -> Quellsprache
    ("", "de"),
    (None, "de"),
    ("*", "de"),
])
def test_negotiate_ohne_einstellung_folgt_dem_browser(header, erwartet):
    assert i18n.negotiate(None, header) == erwartet


def test_negotiate_einstellung_sticht_den_browser_aus():
    assert i18n.negotiate("fr", "de-DE,de;q=0.9") == "fr"
    assert i18n.negotiate("en", "fr-FR") == "en"


def test_negotiate_auto_fragt_den_browser():
    assert i18n.negotiate("auto", "fr-FR,fr;q=0.9") == "fr"


def test_negotiate_unbekannte_einstellung_faellt_auf_den_browser_zurueck():
    """Eine Sprache, die es nicht (mehr) gibt, darf die Oberfläche nicht
    festfahren – dann zählt wieder der Browser."""
    assert i18n.negotiate("kl", "fr-FR,fr;q=0.9") == "fr"


@pytest.mark.parametrize("header,erwartet", [
    ("de-DE,de;q=0.9,en;q=0.8", ["de-de", "de", "en"]),
    ("en;q=0.5,fr", ["fr", "en"]),
    ("  de , en ", ["de", "en"]),
    ("de;q=unsinn,en", ["en", "de"]),           # unlesbares q zählt als 0
    ("", []),
])
def test_parse_accept_language(header, erwartet):
    assert i18n.parse_accept_language(header) == erwartet


# --------------------------------------------------------------------------
# Vollständigkeit der Übersetzungen
# --------------------------------------------------------------------------
def test_alle_sprachen_haben_dieselben_schluessel():
    basis = set(roh("de"))
    for code in SPRACHEN[1:]:
        andere = set(roh(code))
        assert basis - andere == set(), f"{code}.json fehlt: {sorted(basis - andere)}"
        assert andere - basis == set(), f"{code}.json zu viel: {sorted(andere - basis)}"


def test_kein_text_ist_leer():
    for code in SPRACHEN:
        leer = [k for k, v in roh(code).items() if not str(v).strip()]
        assert not leer, f"{code}.json: leer bei {leer}"


def test_platzhalter_stimmen_ueberein():
    """{name} muss in jeder Sprache dieselbe Menge sein – ein vergessener
    Platzhalter zeigt sonst "{n}" statt einer Zahl."""
    basis = roh("de")
    for code in SPRACHEN[1:]:
        andere = roh(code)
        for k, text in basis.items():
            a = set(re.findall(r"\{(\w+)\}", text))
            b = set(re.findall(r"\{(\w+)\}", andere[k]))
            assert a == b, f"{code}.json[{k}]: {sorted(b)} statt {sorted(a)}"


def test_uebersetzungen_sind_nicht_bloss_kopiert():
    """Eine Handvoll auffälliger Schlüssel: hier muss wirklich übersetzt sein."""
    de, en, fr = (roh(c) for c in SPRACHEN)
    for k in ("nav.search", "export.start", "search.go", "wizard.token.title"):
        assert en[k] != de[k], f"{k} ist im Englischen unverändert"
        assert fr[k] != de[k], f"{k} ist im Französischen unverändert"


def test_ki_zusammenfassung_ist_klar_gekennzeichnet():
    """Der Kasten steht ÜBER den Treffern. Er muss deshalb in jeder Sprache
    sagen, dass hier eine KI schreibt, dass sie in Ollama auf diesem Rechner
    läuft und dass sie sich auf die Treffer darunter stützt – "lokal erzeugt"
    allein sagt keines der drei."""
    for code in SPRACHEN:
        d = roh(code)
        for k in ("search.ai", "search.ai.label", "search.ai.note",
                  "settings.chat_model.hint"):
            assert "llama" in d[k], f"{code}.json[{k}] nennt Ollama nicht"
        kopf = d["search.ai.label"] + " " + d["search.ai.tag"]
        assert re.search(r"\bKI\b|\bAI\b|\bIA\b", kopf), \
            f"{code}.json: Kopfzeile kennzeichnet die KI nicht"
        # Der Bezug auf die Treffer darunter – sonst wirkt die Zusammenfassung
        # wie ein eigenständiges Ergebnis.
        assert re.search(r"unten|below|ci-dessous", d["search.ai.label"]), \
            f"{code}.json: Kopfzeile nennt den Bezug zu den Treffern nicht"
        assert re.search(r"Claude", d["search.ai.note"]), \
            f"{code}.json: Fußnote ordnet die Qualität nicht ein"


# --------------------------------------------------------------------------
# Abgleich mit app.py – die eigentliche Klammer
# --------------------------------------------------------------------------
# Namensräume der Textschlüssel. Ein Literal in app.py, das so anfängt, ist ein
# Schlüssel – damit werden auch die zusammengesetzten Fälle gefunden
# (t(x ? 'mcp.mode.hybrid' : 'mcp.mode.lexical')), an denen ein Muster um t(…)
# herum vorbeiliefe.
PREFIXE = ("app.", "pill.", "nav.", "export.", "log.", "search.", "cal.", "copy.",
           "book.", "sched.", "mcp.mode.", "mcp.title", "mcp.sub", "mcp.start",
           "mcp.stop", "mcp.running", "mcp.stopped", "mcp.code.", "mcp.desktop.",
           "settings.", "wizard.", "job.", "srv.", "unit.", "update.", "quit.",
           "progress.", "view.")

# Schlüssel, die erst zur Laufzeit entstehen ('cal.st.' + status) und deshalb
# nirgends vollständig im Quelltext stehen.
DYNAMISCH = (
    ("cal.st.", ("confirmed", "tentative", "cancelled", "deleted", "gone")),
    ("export.cat.", ("mail", "calendar", "contacts", "1on1", "group",
                     "meeting", "channels")),
    ("progress.unit.", ("chats", "mails", "embeddings")),
)


def benutzte_schluessel():
    """Alle Textschlüssel, die app.py verwendet – aus dem Quelltext gelesen.

    Markup (data-i18n…), JavaScript (t('…'), auch in Bedingungen) und die
    serverseitigen Meldungen (logk("…"), {"k": "…"}, Schritt-Bezeichnungen).
    Von Hand gepflegt liefe die Liste unweigerlich weg.
    """
    quelle = Path(app_mod.__file__).read_text(encoding="utf-8")
    keys = set(re.findall(r'data-i18n(?:-html|-ph|-title)?="([\w.]+)"', quelle))
    # Literale in einfachen/doppelten Anführungszeichen und in HTML-Attributen
    # (dort steht &quot; statt ").
    muster = r"""(?:['"]|&quot;)([\w][\w.]*\.[\w.]+)(?:['"]|&quot;)"""
    for treffer in re.findall(muster, quelle):
        if treffer.startswith(PREFIXE) and not treffer.endswith("."):
            keys.add(treffer)
    keys.discard("app.log")          # Protokolldatei, kein Textschlüssel
    for rumpf, enden in DYNAMISCH:
        assert f"'{rumpf}'" in quelle, f"{rumpf} wird nicht mehr zusammengesetzt"
        keys |= {rumpf + e for e in enden}
    keys |= set(app_mod.ollama_hint()["steps"])
    return keys


def test_jeder_verwendete_schluessel_ist_uebersetzt():
    fehlt = benutzte_schluessel() - set(roh("de"))
    assert not fehlt, f"in app.py verwendet, aber nicht in lang/: {sorted(fehlt)}"


def test_keine_verwaisten_texte():
    """Findet Zeilen, die nach einem Umbau niemand mehr anzeigt."""
    verwaist = set(roh("de")) - benutzte_schluessel()
    assert not verwaist, f"in lang/, aber von app.py nicht verwendet: {sorted(verwaist)}"


# --------------------------------------------------------------------------
# Auslieferung der Seite
# --------------------------------------------------------------------------
def test_seite_traegt_keine_deutschen_reste_bei_fremder_sprache():
    """Alles Sichtbare kommt aus der Sprachdatei – das Markup enthält deutschen
    Text nur als Notnagel, falls JavaScript ausfällt."""
    en = i18n.strings("en")
    for k in ("nav.settings", "export.start", "sched.title", "mcp.title"):
        assert en[k] and not re.search(r"[äöüß]", en[k])
