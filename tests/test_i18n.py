"""Tests for i18n.py and the language files.

The most important test sits at the very bottom: it reads every text key
out of app.py – from the markup, the JavaScript and the server-side
messages – and checks it against the language files. Both directions: no
key without a text, no text without a use. That way it shows when someone
adds a text and forgets a language, and equally when a line is left orphaned.
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
# Finding and reading the language files
# --------------------------------------------------------------------------
def test_available_listet_alle_sprachen():
    codes = [e["code"] for e in i18n.available()]
    assert set(codes) >= set(SPRACHEN)
    assert codes[0] == i18n.FALLBACK          # source language first
    namen = {e["code"]: e["name"] for e in i18n.available()}
    # Each language names itself in its own language – otherwise you would
    # have to translate the choice first in order to find it.
    assert namen["de"] == "Deutsch" and namen["en"] == "English"
    assert namen["fr"] == "Français"


def test_available_bei_leerem_ordner(tmp_path):
    (tmp_path / "lang").mkdir()
    assert i18n.available(tmp_path) == []


def test_strings_ergaenzt_fehlende_aus_der_quellsprache(tmp_path):
    """An incomplete translation must not leave blank spots behind."""
    d = tmp_path / "lang"
    d.mkdir()
    (d / "de.json").write_text(json.dumps({"a": "Ah", "b": "Beh"}), encoding="utf-8")
    (d / "xx.json").write_text(json.dumps({"_meta": {"code": "xx", "name": "X"},
                                           "a": "Ay", "b": ""}), encoding="utf-8")
    s = i18n.strings("xx", tmp_path)
    assert s == {"a": "Ay", "b": "Beh"}       # empty text counts as missing
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
# Language selection
# --------------------------------------------------------------------------
@pytest.mark.parametrize("header,erwartet", [
    ("de-DE,de;q=0.9,en;q=0.8", "de"),
    ("en-US,en;q=0.9", "en"),
    ("fr-CH,fr;q=0.9,de;q=0.8", "fr"),          # region code counts for the language
    ("en;q=0.5,fr;q=0.9", "fr"),                # weighting beats order
    ("it-IT,it;q=0.9", "de"),                   # nothing fits -> source language
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
    """A language that does not (or no longer) exist must not jam the UI –
    then the browser counts again."""
    assert i18n.negotiate("kl", "fr-FR,fr;q=0.9") == "fr"


@pytest.mark.parametrize("header,erwartet", [
    ("de-DE,de;q=0.9,en;q=0.8", ["de-de", "de", "en"]),
    ("en;q=0.5,fr", ["fr", "en"]),
    ("  de , en ", ["de", "en"]),
    ("de;q=unsinn,en", ["en", "de"]),           # unreadable q counts as 0
    ("", []),
])
def test_parse_accept_language(header, erwartet):
    assert i18n.parse_accept_language(header) == erwartet


# --------------------------------------------------------------------------
# Completeness of the translations
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
    """{name} must be the same set in every language – a forgotten
    placeholder otherwise shows "{n}" instead of a number."""
    basis = roh("de")
    for code in SPRACHEN[1:]:
        andere = roh(code)
        for k, text in basis.items():
            a = set(re.findall(r"\{(\w+)\}", text))
            b = set(re.findall(r"\{(\w+)\}", andere[k]))
            assert a == b, f"{code}.json[{k}]: {sorted(b)} statt {sorted(a)}"


def test_uebersetzungen_sind_nicht_bloss_kopiert():
    """A handful of conspicuous keys: these must really be translated."""
    de, en, fr = (roh(c) for c in SPRACHEN)
    for k in ("nav.search", "export.start", "search.go", "wizard.token.title"):
        assert en[k] != de[k], f"{k} ist im Englischen unverändert"
        assert fr[k] != de[k], f"{k} ist im Französischen unverändert"


def test_ki_zusammenfassung_ist_klar_gekennzeichnet():
    """The box sits ABOVE the hits. It must therefore say in every language
    that an AI is writing here, that it runs in Ollama on this machine and
    that it rests on the hits below – "generated locally" alone says none
    of the three."""
    for code in SPRACHEN:
        d = roh(code)
        # The labelling sits in the header line of the box itself; the
        # explanation lives in the (i) of the settings card.
        for k in ("search.ai.label", "search.ai.note", "settings.ki.i"):
            assert "llama" in d[k], f"{code}.json[{k}] nennt Ollama nicht"
        kopf = d["search.ai.label"] + " " + d["search.ai.tag"]
        assert re.search(r"\bKI\b|\bAI\b|\bIA\b", kopf), \
            f"{code}.json: Kopfzeile kennzeichnet die KI nicht"
        # The reference to the hits below – otherwise the summary looks
        # like a standalone result.
        assert re.search(r"unten|below|ci-dessous", d["search.ai.label"]), \
            f"{code}.json: Kopfzeile nennt den Bezug zu den Treffern nicht"
        assert re.search(r"Claude", d["search.ai.note"]), \
            f"{code}.json: Fußnote ordnet die Qualität nicht ein"


# --------------------------------------------------------------------------
# Reconciliation with app.py – the actual clamp
# --------------------------------------------------------------------------
# Namespaces of the text keys. A literal in app.py that starts like this is
# a key – that also catches the composed cases
# (t(x ? 'mcp.mode.hybrid' : 'mcp.mode.lexical')) which a pattern around
# t(…) would miss.
PREFIXE = ("app.", "pill.", "nav.", "export.", "log.", "search.", "cal.", "copy.",
           "book.", "sched.", "mcp.",
           "settings.", "wizard.", "job.", "srv.", "unit.", "update.", "quit.",
           "progress.", "view.", "ana.", "folders.", "plan.", "report.", "flow.",
           "run.", "sharepoint.", "files.", "view.", "cadence.", "stand.",
           "lauf.", "kadenz.", "tour.")

# The scripts narrate via progress.event() in text keys, so keys also live
# outside app.py.
SKRIPTE = ("page.html", "steps.py", "runner.py", # the app.py split
           "outlook_export.py", "teams_export.py", "onedrive_export.py",
           "rag_index.py", "combined_search.py", "auth.py",
           "graph_client.py", "drive_mirror.py",
           "sharepoint_export.py", "planner_export.py",
           "todo_export.py", "onenote_export.py")

# Keys that only come into being at runtime ('cal.st.' + status) and
# therefore appear nowhere in full in the source.
DYNAMISCH = (
    ("cal.st.", ("confirmed", "tentative", "cancelled", "deleted", "gone")),
    ("export.cat.", ("mail", "calendar", "contacts", "1on1", "group",
                     "meeting", "channels", "files")),
    ("progress.unit.", ("chats", "mails", "embeddings", "files", "tasks",
                        "pages", "notebooks")),
    # The placeholder in the search field changes with the search type.
    ("search.ph.", ("text", "aehnlich", "ki")),
    # The four Teams kinds are named in the index after their storage
    # folder; the folder picker prefixes the namespace and shows the
    # readable name.
    ("search.folder.teams.", ("1on1", "group", "meeting", "channels")),
    # The lines of the error report: app.systemangaben delivers only the
    # stem ("os", "cores", …), the UI prefixes the namespace.
    # The tile in the header has three layers and composes the key from them.
    ("pill.mcp.", ("on", "off", "aus")),
    ("pill.ollama.tip.", ("on", "aus", "weg", "modell")),
    ("pill.mcp.tip.", ("on", "off", "aus")),
    ("report.sys.", ("version", "os", "python", "cores", "lang", "auth",
                     "categories", "index", "model", "ollama", "settings",
                     "lastjob", "datadir")),
    ("ana.runs.col.", ("time", "origin", "elements", "duration", "new",
                       "result")),
    ("ana.runs.origin.", ("manual", "schedule")),
    # The cadence window serves two sources; title, (i) and filter text
    # are composed from the source key.
    ("kadenz.title.", ("mail", "teams", "onedrive", "todo", "sharepoint")),
    ("kadenz.info.", ("mail", "teams", "onedrive", "todo", "sharepoint")),
    ("kadenz.filter.", ("mail", "teams", "onedrive", "todo", "sharepoint")),
    ("ana.runs.result.", ("done", "error", "aborted", "token_expired",
                          "running")),
)


def benutzte_schluessel():
    """All text keys app.py uses – read from the source.

    Markup (data-i18n…), JavaScript (t('…'), including in conditions) and
    the server-side messages (logk("…"), {"k": "…"}, step labels).
    Maintained by hand, the list would inevitably drift.
    """
    wurzel = Path(app_mod.__file__).resolve().parent
    quelle = Path(app_mod.__file__).read_text(encoding="utf-8")
    for name in SKRIPTE:
        quelle += (wurzel / name).read_text(encoding="utf-8")
    keys = set(re.findall(r'data-i18n(?:-html|-ph|-title)?="([\w.]+)"', quelle))
    # Literals in single/double quotes and in HTML attributes
    # (there &quot; stands in for ").
    muster = r"""(?:['"]|&quot;)([\w][\w.]*\.[\w.]+)(?:['"]|&quot;)"""
    for treffer in re.findall(muster, quelle):
        if treffer.startswith(PREFIXE) and not treffer.endswith("."):
            keys.add(treffer)
    keys.discard("app.log")          # log file, not a text key
    for rumpf, enden in DYNAMISCH:
        assert f"'{rumpf}'" in quelle, f"{rumpf} wird nicht mehr zusammengesetzt"
        keys |= {rumpf + e for e in enden}
    keys |= set(app_mod.ollama_hint()["steps"])
    return keys


def test_jeder_verwendete_schluessel_ist_uebersetzt():
    fehlt = benutzte_schluessel() - set(roh("de"))
    assert not fehlt, f"in app.py verwendet, aber nicht in lang/: {sorted(fehlt)}"


def test_keine_verwaisten_texte():
    """Finds lines that nobody displays any more after a rebuild."""
    verwaist = set(roh("de")) - benutzte_schluessel()
    assert not verwaist, f"in lang/, aber von app.py nicht verwendet: {sorted(verwaist)}"


# --------------------------------------------------------------------------
# Serving the page
# --------------------------------------------------------------------------
def test_seite_traegt_keine_deutschen_reste_bei_fremder_sprache():
    """Everything visible comes from the language file – the markup carries
    German text only as a stopgap in case JavaScript fails."""
    en = i18n.strings("en")
    for k in ("nav.settings", "export.start", "sched.title", "mcp.title"):
        assert en[k] and not re.search(r"[äöüß]", en[k])
