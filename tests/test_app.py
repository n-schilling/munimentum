"""Tests für app.py – Oberfläche, Assistenten, Läufe, Zeitplan, MCP, Suche.

Es geht nie ins Netz: Graph wird gar nicht angesprochen (die App startet nur
die Export-Skripte als Unterprozesse, hier durch kurze python -c-Aufrufe
ersetzt), Ollama wird gemockt. Der Suchteil läuft gegen einen echten kleinen
Store, den rag_index.py schreibt – damit stimmt das Schema garantiert.

app.BASE, app.CONFIG_FILE und app.TOKEN_FILE zeigen in jedem Test auf tmp_path,
damit nichts im Projektordner landet.
"""

import base64
import gzip
import http.client
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

import app as app_mod
import i18n
import corpus
import folders as folders_mod
import rag_index


# --------------------------------------------------------------------------
# Hilfen
# --------------------------------------------------------------------------
def schluessel(m):
    """Textschlüssel einer Meldung. Serverseitige Meldungen sind {k, v} –
    übersetzt wird erst in der Oberfläche."""
    return m.get("k") if isinstance(m, dict) else m


def werte(m):
    return (m or {}).get("v", {}) if isinstance(m, dict) else {}


def make_jwt(exp=None, scp="Mail.Read User.Read", upn="a@example.com", name="A B"):
    """JWT ohne gültige Signatur – app.decode_jwt prüft die auch nicht."""
    claims = {"scp": scp, "upn": upn, "name": name}
    if exp is not None:
        claims["exp"] = exp
    def seg(d):
        raw = json.dumps(d).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return seg({"alg": "none"}) + "." + seg(claims) + "." + "x" * 43


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """app.py so umbiegen, dass alle Pfade in tmp_path liegen."""
    monkeypatch.setattr(app_mod, "BASE", tmp_path)
    monkeypatch.setattr(app_mod, "CONFIG_FILE", tmp_path / "app_config.json")
    monkeypatch.setattr(app_mod, "TOKEN_FILE", tmp_path / "gx_token.txt")
    return tmp_path


@pytest.fixture
def no_ollama(monkeypatch):
    monkeypatch.setattr(app_mod, "check_ollama",
                        lambda url, model, chat_model=None, timeout=1.5: {
                            "running": False, "models": [], "has_model": False,
                            "has_chat_model": False, "error": "ConnectionError",
                            "model": model, "chat_model": chat_model, "url": url})


@pytest.fixture
def with_ollama(monkeypatch):
    monkeypatch.setattr(app_mod, "check_ollama",
                        lambda url, model, chat_model=None, timeout=1.5: {
                            "running": True, "models": [model], "has_model": True,
                            "has_chat_model": True, "error": None, "model": model,
                            "chat_model": chat_model, "url": url})


# --------------------------------------------------------------------------
# Konfiguration
# --------------------------------------------------------------------------
def test_load_config_ergaenzt_fehlende_schluessel(sandbox):
    (sandbox / "app_config.json").write_text(
        json.dumps({"workers": 2, "schedule": {"enabled": True}}), encoding="utf-8")
    cfg = app_mod.load_config()
    assert cfg["workers"] == 2
    assert cfg["schedule"]["enabled"] is True
    assert cfg["schedule"]["interval_minutes"] == 60      # Vorgabe bleibt erhalten


def test_load_config_bei_kaputter_datei(sandbox):
    (sandbox / "app_config.json").write_text("{kein json", encoding="utf-8")
    assert app_mod.load_config() == app_mod.DEFAULT_CONFIG


def test_load_config_ignoriert_unbekannte_schluessel(sandbox):
    (sandbox / "app_config.json").write_text(
        json.dumps({"boesartig": "x"}), encoding="utf-8")
    assert "boesartig" not in app_mod.load_config()


def test_save_config_roundtrip(sandbox):
    cfg = app_mod.load_config()
    cfg["mcp_port"] = 9999
    app_mod.save_config(cfg)
    assert app_mod.load_config()["mcp_port"] == 9999
    assert not (sandbox / "app_config.json.tmp").exists()   # atomarer Tausch


def test_skip_folders_default_ist_mit_outlook_export_deckungsgleich():
    """app.py spiegelt die Liste, statt outlook_export zu importieren (das Modul
    bricht ohne msal/requests ab). Damit die Kopie nicht wegdriftet, hält dieser
    Test beide zusammen."""
    import outlook_export
    assert app_mod.SKIP_FOLDERS_DEFAULT == outlook_export.BUILTIN_SKIP_FOLDERS


@pytest.mark.parametrize("eingabe,erwartet", [
    (["Archiv", "archiv", " Drafts "], ["archiv", "drafts"]),
    ("Archiv, Drafts", ["archiv", "drafts"]),
    ("Archiv\nDrafts\n\n", ["archiv", "drafts"]),      # Textfeld mit Zeilen
    ([], []),
    (None, []),
    ("  ,  ", []),
])
def test_clean_folders(eingabe, erwartet):
    assert app_mod._clean_folders(eingabe) == erwartet


def test_clean_categories_filtert_und_sortiert():
    erlaubt = ["mail", "calendar", "contacts"]
    assert app_mod._clean_categories(["CONTACTS", "mail", "quatsch"], erlaubt) \
        == ["mail", "contacts"]                             # Reihenfolge von `erlaubt`
    assert app_mod._clean_categories(None, erlaubt) == []


# --------------------------------------------------------------------------
# Token
# --------------------------------------------------------------------------
@pytest.mark.parametrize("roh,erwartet", [
    ("  eyJabc  ", "eyJabc"),
    ('"eyJabc"', "eyJabc"),
    ("Bearer eyJabc", "eyJabc"),
    ("bearer  eyJabc", "eyJabc"),
    ("eyJ\nabc\n def", "eyJabcdef"),                        # Umbrüche beim Kopieren
    ("", ""),
    (None, ""),
])
def test_normalize_token(roh, erwartet):
    assert app_mod.normalize_token(roh) == erwartet


def test_decode_jwt_liest_claims():
    claims = app_mod.decode_jwt(make_jwt(exp=1234, scp="Mail.Read"))
    assert claims["exp"] == 1234 and claims["scp"] == "Mail.Read"


@pytest.mark.parametrize("kaputt", ["", "abc", "a.b", "a.@@@@.c"])
def test_decode_jwt_bei_unlesbarem_token(kaputt):
    assert app_mod.decode_jwt(kaputt) == {}


def test_token_status_gueltig_mit_allen_rechten():
    now = 1_000_000
    tok = make_jwt(exp=now + 3600, scp="Mail.Read Chat.Read User.Read")
    st = app_mod.token_status(tok, now=now, needed=["mail", "1on1"])
    assert st["present"] and st["valid"] and not st["expired"]
    assert st["readable"] and st["account"] == "a@example.com"
    assert st["expires_in_minutes"] == 60
    assert st["missing"] == []


def test_token_status_abgelaufen():
    now = 1_000_000
    st = app_mod.token_status(make_jwt(exp=now - 60), now=now)
    assert st["expired"] and not st["valid"]
    assert st["expires_in_minutes"] == -1


def test_token_status_meldet_fehlende_rechte():
    now = 1_000_000
    tok = make_jwt(exp=now + 600, scp="Mail.Read User.Read")
    st = app_mod.token_status(tok, now=now, needed=["mail", "calendar", "channels"])
    assert st["missing"] == ["Calendars.Read", "ChannelMessage.Read.All"]


def test_token_status_akzeptiert_umfassendere_berechtigung():
    """Der Graph Explorer vergibt oft gleich die Schreibvariante. Wer
    Mail.ReadWrite hat, darf erst recht lesen – Mail.Read steht dann aber nie
    im Token, und der Assistent meldete Rechte als fehlend, die da sind."""
    now = 1_000_000
    tok = make_jwt(exp=now + 600,
                   scp="Mail.ReadWrite Contacts.ReadWrite Calendars.Read "
                       "Chat.ReadWrite Group.Read.All User.Read")
    st = app_mod.token_status(tok, now=now, needed=["mail", "contacts", "calendar",
                                                    "1on1", "channels"])
    assert st["missing"] == []


@pytest.mark.parametrize("haben,fehlt", [
    (["Mail.ReadWrite"], []),
    (["Mail.Read.Shared"], []),
    (["Mail.ReadWrite.Shared"], []),
    (["Mail.Read"], []),
    # ReadBasic liefert keine Nachrichteninhalte – deckt den Export nicht ab
    (["Mail.ReadBasic"], ["Mail.Read"]),
    (["Calendars.Read"], ["Mail.Read"]),
    ([], ["Mail.Read"]),
])
def test_scope_missing_mail(haben, fehlt):
    assert app_mod.scope_missing({"Mail.Read"}, haben) == fehlt


def test_scope_missing_kanalnachrichten_ueber_gruppenrechte():
    assert app_mod.scope_missing({"ChannelMessage.Read.All"}, ["Group.Read.All"]) == []
    assert app_mod.scope_missing({"ChannelMessage.Read.All"}, ["Chat.Read"]) \
        == ["ChannelMessage.Read.All"]


def test_scope_missing_ohne_ersatz_bleibt_streng():
    """Chat.ReadBasic liest keine Nachrichteninhalte – kein gültiger Ersatz."""
    assert app_mod.scope_missing({"Chat.Read"}, ["Chat.ReadBasic"]) == ["Chat.Read"]


def test_jede_noetige_berechtigung_hat_eine_beispielabfrage():
    """Der Assistent nennt zu jedem Recht die Abfrage, die es im Graph Explorer
    überhaupt erst sichtbar macht – sonst sucht man es dort vergeblich."""
    noetig = set(app_mod.SCOPE_FOR.values()) | {"User.Read"}
    assert noetig <= set(app_mod.SCOPE_QUERY)


def test_status_liefert_die_beispielabfragen(sandbox, with_ollama):
    s = app_mod.App(app_mod.load_config()).status()
    assert all(s["scope_queries"].get(x, "").startswith("https://graph.microsoft.com/")
               for x in s["scopes_needed"])


def test_token_status_ohne_token():
    st = app_mod.token_status("")
    assert not st["present"] and not st["valid"] and st["missing"] == []


def test_token_status_unlesbar_gilt_als_vorhanden():
    """Graph-Token sind offiziell undurchsichtig – ein nicht zerlegbarer Token
    wird ausprobiert statt vorschnell als kaputt gemeldet."""
    st = app_mod.token_status("undurchsichtig-aber-da", needed=["mail"])
    assert st["present"] and st["valid"] and not st["readable"]
    assert st["missing"] == []                              # keine Falschmeldung


def test_write_und_read_token(sandbox):
    app_mod.write_token("Bearer  eyJtest\n")
    assert (sandbox / "gx_token.txt").read_text(encoding="utf-8") == "eyJtest\n"
    assert app_mod.read_token() == "eyJtest"


def test_write_token_setzt_enge_rechte(sandbox):
    app_mod.write_token("eyJtest")
    if sys.platform != "win32":
        assert (sandbox / "gx_token.txt").stat().st_mode & 0o077 == 0


def test_read_token_ohne_datei(sandbox):
    assert app_mod.read_token() == ""


# --------------------------------------------------------------------------
# Ollama
# --------------------------------------------------------------------------
def test_check_ollama_erkennt_modell_ohne_tag(monkeypatch):
    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"models": [{"name": "bge-m3:latest"}, {"name": "qwen:7b"}]}
    monkeypatch.setattr("requests.get", lambda *a, **k: R())
    out = app_mod.check_ollama("http://x", "bge-m3", "qwen:7b")
    assert out["running"] and out["has_model"]
    assert out["has_chat_model"] is True            # auch ohne genaues Tag
    assert out["models"] == ["bge-m3:latest", "qwen:7b"]


def test_check_ollama_modell_fehlt(monkeypatch):
    class R:
        def raise_for_status(self): pass
        def json(self): return {"models": [{"name": "qwen:7b"}]}
    monkeypatch.setattr("requests.get", lambda *a, **k: R())
    out = app_mod.check_ollama("http://x", "bge-m3")
    assert out["running"] and not out["has_model"]
    assert out["has_chat_model"] is False           # ohne Namen kein Modell


def test_check_ollama_nicht_erreichbar(monkeypatch):
    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr("requests.get", boom)
    out = app_mod.check_ollama("http://x", "bge-m3")
    assert not out["running"] and "connection refused" in out["error"]


@pytest.mark.parametrize("system,erwartet", [
    ("Darwin", "macOS"), ("Windows", "Windows"), ("Linux", "Linux")])
def test_ollama_hint_je_betriebssystem(monkeypatch, system, erwartet):
    monkeypatch.setattr(app_mod.platform, "system", lambda: system)
    h = app_mod.ollama_hint()
    assert h["os"] == erwartet and h["steps"]


# --------------------------------------------------------------------------
# Zustand von Exporten und Index
# --------------------------------------------------------------------------
def test_export_status_ohne_ordner(sandbox):
    st = app_mod.export_status(app_mod.load_config())
    assert not st["teams"]["exists"] and st["teams"]["last_run"] is None
    assert not st["outlook"]["exists"]


def test_export_status_mit_fortschrittsdateien(sandbox):
    (sandbox / "teams_export").mkdir()
    (sandbox / "teams_export" / "export_state.json").write_text("{}", encoding="utf-8")
    (sandbox / "outlook_export").mkdir()
    (sandbox / "outlook_export" / "exported.tsv").write_text("x\n", encoding="utf-8")
    st = app_mod.export_status(app_mod.load_config())
    assert st["teams"]["exists"] and st["teams"]["last_run"]
    assert st["outlook"]["exists"] and st["outlook"]["last_run"]


def test_store_status_ohne_index(sandbox):
    st = app_mod.store_status(app_mod.load_config())
    assert not st["exists"] and st["chunks"] == 0 and not st["semantic"]
    assert st["messages"] == 0


def _index_bauen(sandbox, uids, chunks_je=3):
    """Ein winziger Index: uids Nachrichten mit je chunks_je Textstellen."""
    store = sandbox / "rag_store"
    store.mkdir(exist_ok=True)
    app_mod._ZAEHLUNG.clear()
    con = sqlite3.connect(store / "corpus.db")
    con.execute("CREATE TABLE IF NOT EXISTS chunks(uid TEXT, seq INTEGER)")
    con.executemany("INSERT INTO chunks(uid, seq) VALUES (?, ?)",
                    [(f"m{u}", s) for u in range(uids) for s in range(chunks_je)])
    con.commit()
    con.close()
    return store


def test_store_status_zaehlt_nachrichten_nicht_nur_textstellen(sandbox):
    """Die Kachel nennt Nachrichten – das ist die Einheit, in der jemand sein
    Archiv denkt. Lange Mails stehen als mehrere Textstellen im Index; die
    Zeilenzahl wäre also spürbar höher als das, was er wiederzufinden erwartet.
    """
    _index_bauen(sandbox, uids=4, chunks_je=3)
    st = app_mod.store_status(app_mod.load_config())
    assert st["chunks"] == 12 and st["messages"] == 4


def test_store_status_puffert_die_zaehlung(sandbox, monkeypatch):
    """Die Oberfläche fragt alle paar Sekunden – über den ganzen Index zu
    zählen darf nicht jedes Mal passieren."""
    _index_bauen(sandbox, uids=2)
    cfg = app_mod.load_config()

    abfragen = []
    echt = sqlite3.connect

    def mitzaehlen(*a, **kw):
        con = echt(*a, **kw)
        con.set_trace_callback(abfragen.append)
        return con
    monkeypatch.setattr(sqlite3, "connect", mitzaehlen)

    assert app_mod.store_status(cfg)["messages"] == 2
    erste = len(abfragen)
    assert erste >= 2                       # Textstellen und Nachrichten
    for _ in range(5):
        assert app_mod.store_status(cfg)["messages"] == 2
    assert len(abfragen) == erste, "zählt trotz unveränderter Datei erneut"


def test_store_status_zaehlt_nach_einer_aenderung_neu(sandbox):
    """Der Puffer darf nicht dazu führen, dass ein frischer Index alt aussieht."""
    _index_bauen(sandbox, uids=2)
    cfg = app_mod.load_config()
    assert app_mod.store_status(cfg)["messages"] == 2

    store = sandbox / "rag_store"
    con = sqlite3.connect(store / "corpus.db")
    con.execute("INSERT INTO chunks(uid, seq) VALUES ('m99', 0)")
    con.commit()
    con.close()
    assert app_mod.store_status(cfg)["messages"] == 3


# --------------------------------------------------------------------------
# Kalenderschritt: wann überhaupt, und wann mit Mail-Auswertung
#
# Gemeldet aus der Praxis: ein Lauf mit nur „Kontakte“ ließ trotzdem die
# Wiederherstellung gelöschter Termine anlaufen – jede der 45.000 Mails wurde
# gelesen, minutenlang, für ein Ergebnis, an dem sich nichts geändert haben
# konnte.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("cats,noetig,mit_mails", [
    (["mail", "calendar", "contacts"], True, True),
    (["calendar"], True, False),          # Termine ja, Mails wurden nicht geholt
    (["contacts"], True, False),          # genau der gemeldete Fall
    (["mail"], False, True),              # nichts aufzubauen: kein Kalender, keine Kontakte
    ([], False, False),
    (["mail", "contacts"], True, True),
])
def test_calendar_plan(sandbox, cats, noetig, mit_mails):
    cfg = app_mod.load_config()
    cfg["outlook_categories"] = cats
    assert app_mod.calendar_plan(cfg) == (noetig, mit_mails)


def test_build_steps_laesst_die_wiederherstellung_weg(sandbox):
    """Ohne Mail-Auswertung fällt der teure Teil weg – erkennbar am Schalter
    und daran, dass der Schritt anders heißt."""
    cfg = app_mod.load_config()
    schritt = [s for s in app_mod.build_steps(cfg, calendar=True, reconstruct=False)
               if s["key"] == "calendar"][0]
    assert "--no-reconstruct" in schritt["argv"]
    assert schritt["label"] == "job.step.calendar.plain"


def test_build_steps_folgt_der_einstellung(sandbox):
    """Ohne ausdrückliche Angabe entscheidet app_config.json."""
    cfg = app_mod.load_config()
    voll = [s for s in app_mod.build_steps(cfg, calendar=True) if s["key"] == "calendar"][0]
    assert "--no-reconstruct" not in voll["argv"]      # Vorgabe: an

    cfg["calendar_reconstruct"] = False
    aus = [s for s in app_mod.build_steps(cfg, calendar=True) if s["key"] == "calendar"][0]
    assert "--no-reconstruct" in aus["argv"]


def test_lauf_mit_nur_kontakten_liest_keine_mails(sandbox, monkeypatch, no_ollama):
    """Der gemeldete Fall, einmal durch den ganzen Weg: /api/run -> build_steps."""
    gesehen = {}

    def merken(steps, label):
        gesehen["steps"] = steps
        return True
    app = app_mod.App()
    app.cfg["outlook_categories"] = ["contacts"]
    monkeypatch.setattr(app.jobs, "start", merken)
    monkeypatch.setattr(app_mod, "read_token", lambda *a, **kw: "tok")

    ok, _ = app.launch(outlook=True, index=True, calendar=True,
                       reconstruct=False, label="job.export")
    assert ok
    kal = [s for s in gesehen["steps"] if s["key"] == "calendar"]
    assert kal and "--no-reconstruct" in kal[0]["argv"]


# --------------------------------------------------------------------------
# Schritte eines Laufs
# --------------------------------------------------------------------------
def test_build_steps_setzt_kategorien_und_token(sandbox):
    cfg = app_mod.load_config()
    cfg["outlook_categories"] = ["mail", "contacts"]
    cfg["teams_categories"] = ["1on1", "channels"]
    steps = app_mod.build_steps(cfg, outlook=True, teams=True, index=True, token="tok")

    assert [s["key"] for s in steps] == ["outlook", "teams", "index"]
    assert steps[0]["env"]["EXPORT_CATEGORIES"] == "mail,contacts"
    assert steps[1]["env"]["EXPORT_CATEGORIES"] == "1on1,channels"
    assert all(s["env"]["GRAPH_TOKEN"] == "tok" for s in steps)
    assert all(s["env"]["PYTHONUNBUFFERED"] == "1" for s in steps)
    assert steps[0]["argv"][1].endswith("outlook_export.py")
    assert "outlook_export" in steps[0]["argv"][2:]       # Ausgabeordner
    assert "--no-embeddings" not in steps[2]["argv"]


def test_build_steps_verbietet_rueckfragen(sandbox):
    """Kein Exportschritt darf aus der App heraus etwas fragen können.

    Ohne -default fragte outlook_export nach den Kalendern, sobald stdin
    interaktiv aussah – unter Windows tut das auch das Nullgerät. Der Lauf
    stand dann an einer Frage, die in der Oberfläche niemand beantworten kann.
    """
    steps = app_mod.build_steps(app_mod.load_config(), outlook=True, teams=True)
    assert [s["key"] for s in steps] == ["outlook", "teams"]
    for s in steps:
        assert "-default" in s["argv"]


def test_build_steps_ohne_embeddings(sandbox):
    steps = app_mod.build_steps(app_mod.load_config(), index=True, embeddings=False)
    assert steps[0]["argv"][-1] == "--no-embeddings"
    assert steps[0]["label"] == "job.step.index.lexical"


def test_build_steps_ohne_token_setzt_keine_variable(sandbox):
    steps = app_mod.build_steps(app_mod.load_config(), index=True)
    assert "GRAPH_TOKEN" not in steps[0]["env"]


def test_build_steps_leere_auswahl(sandbox):
    assert app_mod.build_steps(app_mod.load_config()) == []


def test_build_steps_reicht_die_schalter_durch(sandbox):
    """Alles, was in der Oberfläche steht, muss auch beim Skript ankommen –
    sonst ändert ein Klick nur die Datei und nicht den Lauf."""
    cfg = app_mod.load_config()
    cfg.update(embed_images=False, cache_images=False, refresh_channels=False,
               skip_empty_chats=False, include_hidden=True,
               skip_folders=["archiv", "drafts"], workers=2, index_batch=8)
    steps = {s["key"]: s for s in app_mod.build_steps(
        cfg, outlook=True, teams=True, index=True, token="t")}

    o = steps["outlook"]["env"]
    assert o["INCLUDE_HIDDEN"] == "1" and o["SKIP_FOLDERS"] == "archiv,drafts"
    assert o["EXPORT_WORKERS"] == "2"

    t = steps["teams"]["env"]
    assert t["EMBED_IMAGES"] == "0" and t["CACHE_IMAGES"] == "0"
    assert t["REFRESH_CHANNELS"] == "0" and t["SKIP_EMPTY_CHATS"] == "0"

    argv = steps["index"]["argv"]
    assert argv[argv.index("--batch") + 1] == "8"


def test_build_steps_leere_ordnerliste_wird_gesetzt(sandbox):
    """Leer heißt "nichts auslassen". Die Variable muss trotzdem gesetzt sein –
    nicht gesetzt hieße für outlook_export.py "nimm deine Vorgabe"."""
    cfg = app_mod.load_config()
    cfg["skip_folders"] = []
    env = app_mod.build_steps(cfg, outlook=True, token="t")[0]["env"]
    assert env["SKIP_FOLDERS"] == "" and "SKIP_FOLDERS" in env


def test_build_steps_vorgaben_schalten_nichts_ab(sandbox):
    cfg = app_mod.load_config()
    steps = {s["key"]: s for s in app_mod.build_steps(cfg, outlook=True, teams=True,
                                                      token="t")}
    assert steps["teams"]["env"]["EMBED_IMAGES"] == "1"
    assert steps["outlook"]["env"]["INCLUDE_HIDDEN"] == "0"
    assert steps["outlook"]["env"]["SKIP_FOLDERS"].split(",") \
        == sorted(app_mod.SKIP_FOLDERS_DEFAULT)


def _env_namen(modul):
    """Umgebungsvariablen, die ein Skript über settings liest – aus dem Quelltext.

    Selbsttragend: kommt im Skript eine neue Einstellung dazu, fällt der Test
    unten auf, solange app.py sie nicht mitgibt.
    """
    quelle = (Path(app_mod.__file__).parent / f"{modul}.py").read_text(encoding="utf-8")
    return set(re.findall(r'settings\.(?:flag|folders|number)\(\s*"([A-Z_0-9]+)"', quelle))


@pytest.mark.parametrize("modul,key", [("teams_export", "teams"),
                                       ("outlook_export", "outlook")])
def test_app_setzt_alles_was_die_skripte_sonst_aus_der_datei_laesen(sandbox, modul, key):
    """Für einen Lauf aus der App muss die Umgebung vollständig sein – sonst
    gälte teils die Oberfläche, teils app_config.json, und das Skript meldete
    „aus app_config.json übernommen“ mitten in einem App-Lauf."""
    noetig = _env_namen(modul)
    assert noetig, f"keine settings-Aufrufe in {modul}.py gefunden"
    steps = {s["key"]: s for s in app_mod.build_steps(
        app_mod.load_config(), outlook=True, teams=True, token="t")}
    assert noetig <= set(steps[key]["env"])


def test_build_steps_kalender(sandbox):
    steps = app_mod.build_steps(app_mod.load_config(), calendar=True)
    assert steps[0]["key"] == "calendar"
    assert steps[0]["argv"][1].endswith("combined_search.py")
    assert "--json" in steps[0]["argv"]
    ziel = steps[0]["argv"][steps[0]["argv"].index("--json") + 1]
    assert ziel.endswith("calendar.json") and "rag_store" in ziel


def test_build_steps_reihenfolge_export_index_kalender(sandbox):
    steps = app_mod.build_steps(app_mod.load_config(), outlook=True, teams=True,
                                index=True, calendar=True, token="t")
    assert [s["key"] for s in steps] == ["outlook", "teams", "index", "calendar"]


def test_build_steps_suchseite(sandbox):
    steps = app_mod.build_steps(app_mod.load_config(), search_page=True)
    assert steps[0]["key"] == "search_page"
    assert steps[0]["argv"][1].endswith("combined_search.py")


@pytest.mark.parametrize("last,jetzt,faellig", [
    (None, 1000, True),               # noch nie gelaufen
    (1000, 1000 + 59 * 60, False),
    (1000, 1000 + 60 * 60, True),
    (1000, 1000 + 61 * 60, True),
])
def test_due_now(last, jetzt, faellig):
    assert app_mod.due_now(last, 60, jetzt) is faellig


# --------------------------------------------------------------------------
# Ausgabe der Unterprozesse
# --------------------------------------------------------------------------
def test_stream_lines_trennt_auch_an_wagenruecklauf():
    """rag_index.py überschreibt seine Fortschrittszeile mit \\r statt \\n –
    readline() würde bis zum Ende des Schritts blockieren."""
    import io
    roh = b"start\n  1/9 fertig\r  2/9 fertig\rende\n"
    assert list(app_mod._stream_lines(io.BytesIO(roh))) == \
        ["start", "  1/9 fertig", "  2/9 fertig", "ende"]


def test_stream_lines_haelt_rest_ohne_zeilenende():
    import io
    assert list(app_mod._stream_lines(io.BytesIO(b"abc"))) == ["abc"]


def test_stream_lines_ueberspringt_leerzeilen():
    import io
    assert list(app_mod._stream_lines(io.BytesIO(b"a\n\n\nb\n"))) == ["a", "b"]


# --------------------------------------------------------------------------
# JobRunner – echte Unterprozesse, aber winzige
# --------------------------------------------------------------------------
def _py_step(code, label="Schritt"):
    return {"key": "t", "label": label, "argv": [sys.executable, "-c", code],
            "env": {"EXPORT_PROGRESS": "1"}}


def _warte(runner, sekunden=15):
    ende = time.time() + sekunden
    while runner.busy and time.time() < ende:
        time.sleep(0.02)
    assert not runner.busy, "Lauf wurde nicht fertig"


def test_jobrunner_fuehrt_schritte_der_reihe_nach_aus(sandbox):
    r = app_mod.JobRunner()
    assert r.start([_py_step("print('eins')", "A"), _py_step("print('zwei')", "B")], "Lauf")
    _warte(r)
    text = "\n".join(str(ln["text"]) for ln in r.lines)
    assert "eins" in text and "zwei" in text
    assert r.last["ok"] and r.last["label"] == "Lauf"
    assert text.index("eins") < text.index("zwei")


def test_jobrunner_bricht_bei_fehler_ab(sandbox):
    r = app_mod.JobRunner()
    r.start([_py_step("raise SystemExit(3)", "Kaputt"), _py_step("print('nie')", "B")], "Lauf")
    _warte(r)
    text = "\n".join(str(ln["text"]) for ln in r.lines)
    assert "nie" not in text                              # zweiter Schritt lief nicht
    assert not r.last["ok"]
    assert schluessel(r.last["detail"]) == "srv.job.exitcode"
    assert werte(r.last["detail"])["code"] == 3


def test_jobrunner_erkennt_abgelaufenen_token(sandbox):
    r = app_mod.JobRunner()
    r.start([_py_step("print('Abgebrochen: Token abgelaufen.'); raise SystemExit(1)")], "Lauf")
    _warte(r)
    assert r.token_expired is True


def test_jobrunner_nimmt_nur_einen_lauf_gleichzeitig(sandbox):
    r = app_mod.JobRunner()
    assert r.start([_py_step("import time; time.sleep(2)")], "Erster")
    assert r.start([_py_step("print('x')")], "Zweiter") is False
    r.cancel()
    _warte(r)


def test_jobrunner_abbruch(sandbox):
    r = app_mod.JobRunner()
    r.start([_py_step("import time; time.sleep(30)")], "Lang")
    time.sleep(0.4)
    assert r.cancel() is True
    _warte(r)
    assert not r.last["ok"]


def test_jobrunner_leere_schrittliste(sandbox):
    assert app_mod.JobRunner().start([], "Nichts") is False


def test_jobrunner_meldet_nicht_startbaren_befehl(sandbox):
    r = app_mod.JobRunner()
    r.start([{"key": "x", "label": "Weg", "argv": ["/gibt/es/nicht"], "env": {}}], "Lauf")
    _warte(r)
    assert not r.last["ok"]
    assert any(schluessel(ln["text"]) == "srv.job.spawnfail" for ln in r.lines)


def test_jobrunner_nimmt_fortschritt_auf_und_haelt_ihn_aus_dem_protokoll(sandbox):
    """Die Zahlen treiben den Balken; im Protokoll wären sie nur Rauschen."""
    r = app_mod.JobRunner()
    # progress liegt im Projektordner, nicht im Sandkasten
    wurzel = str(Path(app_mod.__file__).resolve().parent)
    skript = (f"import sys, time; sys.path.insert(0, {wurzel!r}); import progress; "
              "[(progress.melde(i, 3, 'chats'), time.sleep(0.05)) for i in range(4)]; "
              "print('fertig')")
    r.start([_py_step(skript)], "Lauf")
    gesehen = []
    while r.busy:
        p = (r.job or {}).get("progress")
        if p and p not in gesehen:
            gesehen.append(p)
        time.sleep(0.02)
    assert {"done": 0, "total": 3, "what": "chats"} in gesehen
    assert gesehen[-1]["done"] == 3
    texte = [ln["text"] for ln in r.lines if isinstance(ln["text"], str)]
    assert "fertig" in texte
    assert not [x for x in texte if "PROGRESS" in x], "Fortschritt landete im Protokoll"


def test_jobrunner_setzt_den_fortschritt_je_schritt_zurueck(sandbox):
    """Sonst zeigte der zweite Schritt kurz den Stand des ersten."""
    r = app_mod.JobRunner()
    staende = []
    echtes = app_mod.JobRunner._exec

    def merke(self, step):
        staende.append((step["label"], (self.job or {}).get("progress")))
        return echtes(self, step)

    app_mod.JobRunner._exec = merke
    try:
        r.start([_py_step("print(1)", "job.step.outlook"),
                 _py_step("print(2)", "job.step.teams")], "Lauf")
        _warte(r)
    finally:
        app_mod.JobRunner._exec = echtes
    assert [p for _, p in staende] == [None, None]


# --------------------------------------------------------------------------
# Nichts Neues exportiert -> Index und Kalender entfallen
#
# Gemeldet aus der Praxis: ein Lauf mit nur "Kontakte" meldete "Neu exportiert:
# 0" und indizierte danach zwei Minuten lang denselben Bestand.
# --------------------------------------------------------------------------
def _melde_step(neu, label="job.step.outlook"):
    wurzel = str(Path(app_mod.__file__).resolve().parent)
    return _py_step(f"import sys; sys.path.insert(0, {wurzel!r}); import progress; "
                    f"print('Fertig.'); progress.ergebnis({neu})", label)


def _folge(sandbox, neu, ziel_da=True, steps_extra=None):
    """Export-Schritt mit `neu` Stück, danach ein markierter Folgeschritt."""
    ziel = sandbox / "corpus.db"
    if ziel_da:
        ziel.write_text("x", encoding="utf-8")
    folge = _py_step("print('INDIZIERT')", "job.step.index")
    folge.update(nur_bei_neuem=True, ziel=ziel)
    r = app_mod.JobRunner()
    r.start([_melde_step(neu)] + (steps_extra or []) + [folge], "job.export")
    _warte(r)
    return r, "\n".join(str(ln["text"]) for ln in r.lines)


def test_jobrunner_ueberspringt_index_wenn_nichts_neu_ist(sandbox):
    r, text = _folge(sandbox, neu=0)
    assert "INDIZIERT" not in text, "der Index lief trotz unverändertem Bestand"
    assert r.last["ok"]
    assert "srv.job.skipped" in text          # und sagt auch, warum
    assert "@@RESULT@@" not in text           # die Meldung selbst ist kein Protokoll


def test_jobrunner_indiziert_wenn_es_etwas_neues_gibt(sandbox):
    _, text = _folge(sandbox, neu=1)
    assert "INDIZIERT" in text


def test_jobrunner_indiziert_ohne_vorhandenen_index(sandbox):
    """Sonst gäbe es nach dem ersten Lauf mit unverändertem Bestand nie einen."""
    _, text = _folge(sandbox, neu=0, ziel_da=False)
    assert "INDIZIERT" in text


def test_jobrunner_zaehlt_ueber_alle_exportschritte(sandbox):
    """Teams bringt etwas, Outlook nicht – dann muss indiziert werden."""
    _, text = _folge(sandbox, neu=0,
                     steps_extra=[_melde_step(3, "job.step.teams")])
    assert "INDIZIERT" in text


def test_jobrunner_indiziert_wenn_der_export_nichts_meldet(sandbox):
    """Unwissen ist kein Grund zu sparen – etwa bei einem älteren Skript."""
    ziel = sandbox / "corpus.db"
    ziel.write_text("x", encoding="utf-8")
    folge = _py_step("print('INDIZIERT')", "job.step.index")
    folge.update(nur_bei_neuem=True, ziel=ziel)
    r = app_mod.JobRunner()
    r.start([_py_step("print('Fertig.')"), folge], "job.export")
    _warte(r)
    assert "INDIZIERT" in "\n".join(str(ln["text"]) for ln in r.lines)


def test_jobrunner_indiziert_wenn_gar_kein_export_lief(sandbox):
    """Der Knopf „Nur indizieren“ muss immer indizieren."""
    ziel = sandbox / "corpus.db"
    ziel.write_text("x", encoding="utf-8")
    folge = _py_step("print('INDIZIERT')", "job.step.index")
    folge.update(nur_bei_neuem=True, ziel=ziel)
    r = app_mod.JobRunner()
    r.start([folge], "job.index")
    _warte(r)
    assert "INDIZIERT" in "\n".join(str(ln["text"]) for ln in r.lines)


def test_build_steps_markiert_index_und_kalender(sandbox):
    """Die Marke samt Ergebnisdatei muss aus build_steps kommen – ohne sie
    greift die Ersparnis nie."""
    cfg = app_mod.load_config()
    steps = {s["key"]: s for s in
             app_mod.build_steps(cfg, outlook=True, index=True, calendar=True)}
    assert steps["index"]["nur_bei_neuem"] and steps["index"]["ziel"].name == "corpus.db"
    assert steps["calendar"]["nur_bei_neuem"]
    assert steps["calendar"]["ziel"].name == "calendar.json"
    # Die Export-Schritte selbst niemals.
    assert not steps["outlook"].get("nur_bei_neuem")


def test_jobrunner_log_since(sandbox):
    r = app_mod.JobRunner()
    r.log("eins")
    r.log("zwei")
    lines, seq = r.log_since(0)
    assert [ln["text"] for ln in lines] == ["eins", "zwei"] and seq == 2
    lines, seq = r.log_since(1)
    assert [ln["text"] for ln in lines] == ["zwei"]


def test_jobrunner_ringpuffer_begrenzt(sandbox, monkeypatch):
    monkeypatch.setattr(app_mod.JobRunner, "MAX_LINES", 5)
    r = app_mod.JobRunner()
    for i in range(20):
        r.log(str(i))
    assert len(r.lines) == 5 and r.seq == 20             # Nummern laufen weiter


# --------------------------------------------------------------------------
# App: Start eines Laufs
# --------------------------------------------------------------------------
def test_launch_ohne_token_wird_abgelehnt(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    ok, why = a.launch(outlook=True)
    assert not ok and schluessel(why) == "srv.notoken"


def test_launch_ohne_auswahl(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    ok, why = a.launch()
    assert not ok and schluessel(why) == "srv.nothing"


def test_launch_waehlt_ohne_ollama_den_volltextindex(sandbox, no_ollama, monkeypatch):
    """Genau der Fall aus der Anforderung: kein Ollama -> es wird trotzdem
    gearbeitet, nur eben ohne Embeddings, und der Grund steht im Protokoll."""
    gesehen = {}
    monkeypatch.setattr(app_mod.JobRunner, "start",
                        lambda self, steps, label: gesehen.update(steps=steps) or True)
    a = app_mod.App(app_mod.load_config())
    ok, _ = a.launch(index=True, label="Index")
    assert ok
    assert "--no-embeddings" in gesehen["steps"][0]["argv"]
    assert any(schluessel(ln["text"]) == "srv.lexical.noollama" for ln in a.jobs.lines)


def test_launch_ohne_embeddings_auf_wunsch_nennt_den_richtigen_grund(
        sandbox, with_ollama, monkeypatch):
    """Ollama läuft – der Volltextindex ist dann eine Entscheidung, kein Mangel."""
    monkeypatch.setattr(app_mod.JobRunner, "start", lambda self, steps, label: True)
    a = app_mod.App(app_mod.load_config())
    assert a.launch(index=True, embeddings=False)[0]
    assert any(schluessel(ln["text"]) == "srv.lexical.choice" for ln in a.jobs.lines)


def test_launch_mit_ollama_baut_embeddings(sandbox, with_ollama, monkeypatch):
    gesehen = {}
    monkeypatch.setattr(app_mod.JobRunner, "start",
                        lambda self, steps, label: gesehen.update(steps=steps) or True)
    a = app_mod.App(app_mod.load_config())
    assert a.launch(index=True)[0]
    assert "--no-embeddings" not in gesehen["steps"][0]["argv"]


def test_launch_lehnt_zweiten_lauf_ab(sandbox, with_ollama, monkeypatch):
    monkeypatch.setattr(app_mod.JobRunner, "busy", property(lambda self: True))
    a = app_mod.App(app_mod.load_config())
    ok, why = a.launch(index=True)
    assert not ok and schluessel(why) == "srv.busy"


def test_ollama_ergebnis_wird_kurz_zwischengespeichert(sandbox, monkeypatch):
    """Der Status wird im Sekundentakt abgefragt – ein Netzaufruf je Abruf wäre Unfug."""
    aufrufe = []
    monkeypatch.setattr(app_mod, "check_ollama", lambda url, model, timeout=1.5:
                        aufrufe.append(1) or {"running": True, "has_model": True,
                                              "models": [], "error": None,
                                              "model": model, "url": url})
    a = app_mod.App(app_mod.load_config())
    a.ollama()
    a.ollama()
    a.ollama()
    assert len(aufrufe) == 1
    a.ollama(force=True)
    assert len(aufrufe) == 2


# --------------------------------------------------------------------------
# Aktualisierungsprüfung
# --------------------------------------------------------------------------
def test_update_check_meldet_nur_neuere_versionen(sandbox, with_ollama, monkeypatch):
    monkeypatch.setattr(app_mod.updates, "check", lambda *a, **k: {
        "status": "ok", "current": "1.0.0", "latest": "1.4.0",
        "url": "https://example.invalid/v1.4.0", "newer": True, "error": None})
    a = app_mod.App(app_mod.load_config())
    a.check_updates(blockierend=True)
    zeile = a.jobs.lines[-1]
    assert schluessel(zeile["text"]) == "srv.update.available"
    assert werte(zeile["text"])["version"] == "1.4.0"
    assert a.status()["update"]["newer"] is True


@pytest.mark.parametrize("zustand", [
    {"status": "none", "newer": False},           # noch kein Release
    {"status": "error", "newer": False, "error": "kein Netz"},
    {"status": "off", "newer": False},
    {"status": "ok", "newer": False, "latest": "1.0.0"},
])
def test_update_check_schweigt_sonst(sandbox, with_ollama, monkeypatch, zustand):
    """Kein Release, kein Netz oder schon aktuell sind normale Zustände – damit
    behelligt man niemanden im Protokoll."""
    voll = {"current": "1.0.0", "latest": None, "url": None, "error": None, **zustand}
    monkeypatch.setattr(app_mod.updates, "check", lambda *a, **k: voll)
    a = app_mod.App(app_mod.load_config())
    a.check_updates(blockierend=True)
    assert list(a.jobs.lines) == []          # deque
    assert a.status()["update"]["status"] == zustand["status"]


def test_update_check_reicht_die_einstellung_durch(sandbox, with_ollama, monkeypatch):
    """Abgeschaltet heißt abgeschaltet – updates.check darf gar nicht erst
    hinausgehen, das prüft es selbst anhand dieses Schalters."""
    gesehen = {}
    monkeypatch.setattr(app_mod.updates, "check",
                        lambda current, repo, enabled=True: gesehen.update(
                            current=current, repo=repo, enabled=enabled) or
                        {"status": "off", "current": current, "latest": None,
                         "url": None, "newer": False, "error": None})
    cfg = app_mod.load_config()
    cfg["update_check"] = False
    app_mod.App(cfg).check_updates(blockierend=True)
    assert gesehen["enabled"] is False
    assert gesehen["current"] == app_mod.version.VERSION
    assert gesehen["repo"] == app_mod.version.REPO


def test_status_kennt_die_version_vor_der_pruefung(sandbox, with_ollama):
    """Der erste Statusabruf kommt, bevor die Prüfung im Hintergrund fertig ist."""
    s = app_mod.App(app_mod.load_config()).status()
    assert s["update"]["current"] == app_mod.version.VERSION
    assert s["update"]["newer"] is False
    assert s["update"]["releases_url"].startswith("https://github.com/")


def test_update_check_laeuft_im_hintergrund(sandbox, with_ollama, monkeypatch):
    """Der Start darf nicht auf eine Netzantwort warten."""
    los = threading.Event()
    def langsam(*a, **k):
        los.wait(5)
        return {"status": "none", "current": "1.0.0", "latest": None,
                "url": None, "newer": False, "error": None}

    monkeypatch.setattr(app_mod.updates, "check", langsam)
    a = app_mod.App(app_mod.load_config())
    a.check_updates()                       # kehrt sofort zurück
    assert a.status()["update"]["status"] == "off"
    los.set()


def test_http_update_check(server, monkeypatch):
    monkeypatch.setattr(app_mod.updates, "check", lambda *a, **k: {
        "status": "ok", "current": "1.0.0", "latest": "2.0.0",
        "url": "u", "newer": True, "error": None})
    code, r = call(server[1], "POST", "/api/update-check")
    assert code == 200 and r["newer"] is True and r["latest"] == "2.0.0"


def test_http_config_schaltet_die_pruefung_ab(server, sandbox):
    code, r = call(server[1], "POST", "/api/config", {"update_check": False})
    assert code == 200 and r["config"]["update_check"] is False
    assert app_mod.load_config()["update_check"] is False


# --------------------------------------------------------------------------
# Status und Assistenten-Steuerung
# --------------------------------------------------------------------------
def test_status_zeigt_token_assistenten_ohne_token(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    assert a.status()["wizard"] == "token"


ALLE_RECHTE = "Mail.Read Calendars.Read Contacts.Read Chat.Read"


def test_status_laesst_gueltigen_token_in_ruhe(sandbox, with_ollama):
    """Ein noch gültiger Token darf beim Start nicht nach einem neuen fragen –
    seine Laufzeit hängt am Tenant und reicht durchaus über einen Arbeitstag."""
    app_mod.write_token(make_jwt(exp=time.time() + 12 * 3600, scp=ALLE_RECHTE))
    a = app_mod.App(app_mod.load_config())
    assert a.status()["wizard"] is None


def test_status_zeigt_assistenten_bei_abgelaufenem_token(sandbox, with_ollama):
    app_mod.write_token(make_jwt(exp=time.time() - 60, scp=ALLE_RECHTE))
    assert app_mod.App(app_mod.load_config()).status()["wizard"] == "token"


def test_status_fragt_bei_fehlenden_rechten_nicht_von_selbst(sandbox, with_ollama):
    """Fehlende Rechte melden Kachel und Protokoll; ungefragt aufpoppen soll der
    Assistent deswegen nicht – der Token selbst ist ja gültig."""
    app_mod.write_token(make_jwt(exp=time.time() + 3600, scp="Chat.Read"))
    a = app_mod.App(app_mod.load_config())
    s = a.status()
    assert s["wizard"] is None
    assert s["token"]["missing"]


def test_status_zeigt_ollama_assistenten_wenn_token_passt(sandbox, no_ollama):
    app_mod.write_token(make_jwt(exp=time.time() + 3600, scp=ALLE_RECHTE))
    a = app_mod.App(app_mod.load_config())
    assert a.status()["wizard"] == "ollama"


def test_status_zeigt_token_assistenten_nach_abgelaufenem_lauf(sandbox, with_ollama):
    """Ein Lauf, der am Token gescheitert ist, holt den Assistenten zurück –
    auch wenn exp formal noch in der Zukunft liegt (zurückgezogener Token)."""
    app_mod.write_token(make_jwt(exp=time.time() + 3600, scp=ALLE_RECHTE))
    a = app_mod.App(app_mod.load_config())
    assert a.status()["wizard"] is None
    a.jobs.token_expired = True
    assert a.status()["wizard"] == "token"


# --------------------------------------------------------------------------
# Rückmeldung beim Start (der Assistent schweigt jetzt im Normalfall)
# --------------------------------------------------------------------------
def test_log_token_state_bei_gueltigem_token(sandbox, with_ollama):
    app_mod.write_token(make_jwt(exp=time.time() + 12 * 3600 + 60,
                                 scp=ALLE_RECHTE, upn="chef@example.com"))
    a = app_mod.App(app_mod.load_config())
    a.log_token_state()
    zeile = a.jobs.lines[-1]
    assert zeile["level"] == "ok"
    assert schluessel(zeile["text"]) == "srv.token.found"
    assert werte(zeile["text"])["account"] == "chef@example.com"
    assert werte(zeile["text"])["minutes"] == 12 * 60      # Formatierung macht die Oberfläche


def test_log_token_state_ohne_token(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    a.log_token_state()
    assert a.jobs.lines[-1]["level"] == "warn"
    assert schluessel(a.jobs.lines[-1]["text"]) == "srv.token.none"


def test_log_token_state_bei_abgelaufenem_token(sandbox, with_ollama):
    app_mod.write_token(make_jwt(exp=time.time() - 60))
    a = app_mod.App(app_mod.load_config())
    a.log_token_state()
    assert a.jobs.lines[-1]["level"] == "warn"
    assert schluessel(a.jobs.lines[-1]["text"]) == "srv.token.expired"


def test_log_token_state_nennt_fehlende_rechte(sandbox, with_ollama):
    app_mod.write_token(make_jwt(exp=time.time() + 3600, scp="Chat.Read"))
    a = app_mod.App(app_mod.load_config())
    a.log_token_state()
    assert a.jobs.lines[-1]["level"] == "warn"
    assert schluessel(a.jobs.lines[-1]["text"]) == "srv.token.scopes"
    assert "Mail.Read" in werte(a.jobs.lines[-1]["text"])["list"]


def test_token_status_liefert_die_restminuten():
    """Formatiert wird in der Oberfläche – nur dort ist die Sprache bekannt."""
    now = 1_000_000
    st = app_mod.token_status(make_jwt(exp=now + 620 * 60), now=now)
    assert st["expires_in_minutes"] == 620
    assert "expires_text" not in st


def test_status_nennt_die_noetigen_berechtigungen(sandbox, with_ollama):
    cfg = app_mod.load_config()
    cfg["outlook_categories"] = ["mail"]
    cfg["teams_categories"] = ["channels"]
    s = app_mod.App(cfg).status()
    assert s["scopes_needed"] == ["ChannelMessage.Read.All", "Mail.Read", "User.Read"]


# --------------------------------------------------------------------------
# Zeitplan
# --------------------------------------------------------------------------
def test_scheduler_startet_lauf_wenn_faellig(sandbox, with_ollama):
    app_mod.write_token(make_jwt(exp=time.time() + 3600))
    a = app_mod.App(app_mod.load_config())
    a.cfg["schedule"].update(enabled=True, interval_minutes=5,
                             outlook=True, teams=False, index=True)
    gestartet = {}
    a.launch = lambda **kw: gestartet.update(kw) or (True, "gestartet")
    a.scheduler._tick()
    assert gestartet["outlook"] is True and gestartet["teams"] is False
    assert gestartet["index"] is True and gestartet["label"] == "job.scheduled"


@pytest.mark.parametrize("cats,kalender,rekonstruktion", [
    (["mail", "calendar"], True, None),    # None = wie eingestellt
    (["contacts"], True, False),           # aufbauen ja, Mails lesen nein
    (["mail"], False, None),               # nichts aufzubauen – dann egal
])
def test_scheduler_stimmt_den_kalenderschritt_ab(sandbox, with_ollama, cats,
                                                 kalender, rekonstruktion):
    """Derselbe Fehler saß im Zeitplan – dort unbemerkt, weil er nachts läuft."""
    app_mod.write_token(make_jwt(exp=time.time() + 3600))
    a = app_mod.App(app_mod.load_config())
    a.cfg["outlook_categories"] = cats
    a.cfg["schedule"].update(enabled=True, interval_minutes=5, outlook=True,
                             teams=False, index=True, calendar=True)
    gestartet = {}
    a.launch = lambda **kw: gestartet.update(kw) or (True, "gestartet")
    a.scheduler._tick()
    assert gestartet["calendar"] is kalender
    assert gestartet["reconstruct"] is rekonstruktion


def test_scheduler_wartet_bis_zum_intervall(sandbox, with_ollama):
    app_mod.write_token(make_jwt(exp=time.time() + 3600))
    a = app_mod.App(app_mod.load_config())
    a.cfg["schedule"].update(enabled=True, interval_minutes=60)
    laeufe = []
    a.launch = lambda **kw: laeufe.append(kw) or (True, "gestartet")
    a.scheduler._tick()
    a.scheduler._tick()                                   # sofort danach: nicht fällig
    assert len(laeufe) == 1


def test_scheduler_ueberspringt_ohne_gueltigen_token(sandbox, with_ollama):
    app_mod.write_token(make_jwt(exp=time.time() - 60))    # abgelaufen
    a = app_mod.App(app_mod.load_config())
    a.cfg["schedule"]["enabled"] = True
    a.launch = lambda **kw: pytest.fail("darf nicht starten")
    a.scheduler._tick()
    assert a.jobs.token_expired is True
    assert any(schluessel(ln["text"]) == "srv.sched.notoken" for ln in a.jobs.lines)


def test_scheduler_tut_nichts_wenn_aus(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    a.launch = lambda **kw: pytest.fail("darf nicht starten")
    a.scheduler._tick()


def test_scheduler_tut_nichts_waehrend_ein_lauf_laeuft(sandbox, with_ollama, monkeypatch):
    a = app_mod.App(app_mod.load_config())
    a.cfg["schedule"]["enabled"] = True
    monkeypatch.setattr(app_mod.JobRunner, "busy", property(lambda self: True))
    a.launch = lambda **kw: pytest.fail("darf nicht starten")
    a.scheduler._tick()


def test_scheduler_next_due(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    assert a.scheduler.next_due() is None                  # aus
    a.cfg["schedule"].update(enabled=True, interval_minutes=30)
    assert a.scheduler.next_due() <= time.time()           # noch nie gelaufen -> sofort
    a.scheduler.last_run = 1000
    assert a.scheduler.next_due() == 1000 + 1800


# --------------------------------------------------------------------------
# MCP-Prozess
# --------------------------------------------------------------------------
def test_mcp_ohne_index_startet_nicht(sandbox):
    a = app_mod.App(app_mod.load_config())
    ok, why = a.mcp.start(a.cfg)
    assert not ok and schluessel(why) == "srv.mcp.noindex"
    assert a.mcp.status(a.cfg)["running"] is False


def test_mcp_status_nennt_die_url(sandbox):
    a = app_mod.App(app_mod.load_config())
    a.cfg["mcp_port"] = 8899
    assert a.mcp.status(a.cfg)["url"] == "http://127.0.0.1:8899/mcp"


def test_autostart_mcp_meldet_fehlenden_index(sandbox):
    a = app_mod.App(app_mod.load_config())
    a.autostart_mcp()
    assert any(schluessel(ln["text"]) == "srv.mcp.notstarted" for ln in a.jobs.lines)


def test_autostart_mcp_kann_abgeschaltet_werden(sandbox):
    cfg = app_mod.load_config()
    cfg["mcp_autostart"] = False
    a = app_mod.App(cfg)
    a.autostart_mcp()
    assert not a.jobs.lines


class FakePopen:
    """Ersatz für den mcp_server.py-Unterprozess – ohne echten Port.

    Bleibt wie das Original laufen, bis terminate() kommt: sonst wäre der
    Prozess schon beendet, sobald der Protokoll-Thread die Ausgabe gelesen hat.
    """

    def __init__(self, argv, **kw):
        import io
        self.argv = argv
        self.kw = kw
        self.stdout = io.BytesIO(b"munimentum MCP: 3 chunks\n")
        self._ende = threading.Event()
        self._code = None

    def poll(self):
        return self._code

    def terminate(self):
        self._code = -15
        self._ende.set()

    def kill(self):
        self._code = -9
        self._ende.set()

    def wait(self, timeout=None):
        self._ende.wait(timeout if timeout is not None else 30)
        return self._code if self._code is not None else 0


@pytest.fixture
def fake_popen(monkeypatch):
    gestartet = []
    monkeypatch.setattr(app_mod.subprocess, "Popen",
                        lambda argv, **kw: gestartet.append(FakePopen(argv, **kw))
                        or gestartet[-1])
    return gestartet


def test_mcp_start_und_stop(sandbox, store, fake_popen):
    a = app_mod.App(app_mod.load_config())
    ok, why = a.mcp.start(a.cfg)
    assert ok and schluessel(why) == "srv.mcp.startok"
    assert a.mcp.running and a.mcp.status(a.cfg)["port"] == a.cfg["mcp_port"]

    argv = fake_popen[0].argv
    assert argv[1].endswith("mcp_server.py")
    assert "--data-dir" in argv
    assert "--port" in argv

    assert schluessel(a.mcp.start(a.cfg)[1]) == "srv.mcp.running"   # kein zweiter Prozess
    assert len(fake_popen) == 1

    assert a.mcp.stop() is True
    assert not a.mcp.running
    assert a.mcp.stop() is False                            # schon gestoppt


def test_mcp_leitet_ausgabe_ins_protokoll(sandbox, store, fake_popen):
    a = app_mod.App(app_mod.load_config())
    a.mcp.start(a.cfg)
    ende = time.time() + 5
    while time.time() < ende and not any("[MCP]" in ln["text"] for ln in a.jobs.lines):
        time.sleep(0.02)
    assert any("[MCP] munimentum MCP" in ln["text"] for ln in a.jobs.lines)
    a.mcp.stop()


def test_mcp_start_scheitert_am_betriebssystem(sandbox, store, monkeypatch):
    def boom(*a, **k):
        raise OSError("kein Python")
    monkeypatch.setattr(app_mod.subprocess, "Popen", boom)
    a = app_mod.App(app_mod.load_config())
    ok, why = a.mcp.start(a.cfg)
    assert not ok and schluessel(why) == "srv.mcp.spawnfail"


def test_autostart_mcp_startet_bei_vorhandenem_index(sandbox, store, fake_popen):
    a = app_mod.App(app_mod.load_config())
    a.autostart_mcp()
    assert a.mcp.running
    a.shutdown()
    assert not a.mcp.running                                # shutdown räumt auf


# --------------------------------------------------------------------------
# Suche über einen echten kleinen Store
# --------------------------------------------------------------------------
TEAMS_HTML = """<html><body>
<h1>Projekt Alpha</h1>
<div class="msg">
  <span class="name">Alice Example</span>
  <span class="time">2025-06-01 09:30</span>
  <div class="body">Die Rechnung 4711 ist bezahlt.</div>
</div>
</body></html>"""


@pytest.fixture
def store(sandbox):
    """Kleiner, echter Store – geschrieben mit den Helfern aus rag_index.py."""
    teams = sandbox / "teams_export" / "1on1"
    teams.mkdir(parents=True)
    (teams / "alice__abc.html").write_text(TEAMS_HTML, encoding="utf-8")
    recs = corpus.load_records(str(sandbox / "teams_export"), str(sandbox / "fehlt"))
    chunks = corpus.chunk_records(recs)
    assert chunks, "Testkorpus ist leer"
    for c in chunks:
        c["hash"] = corpus.chunk_hash(c)
    (sandbox / "rag_store").mkdir()
    rag_index.write_db(sandbox / "rag_store", chunks)
    rag_index.write_info(sandbox / "rag_store", None, 0, len(chunks))
    return sandbox / "rag_store"


def test_searchbridge_ohne_index(sandbox):
    b = app_mod.SearchBridge()
    assert b.ensure(app_mod.load_config()) is None
    assert schluessel(b.error) == "srv.noindex"


def test_searchbridge_sucht_lexikalisch(sandbox, store):
    b = app_mod.SearchBridge()
    mod = b.ensure(app_mod.load_config())
    assert mod is not None and mod.STATE["semantic"] is False
    res = mod.search_messages("Rechnung", mode="lexical")
    assert res["count"] >= 1
    assert "4711" in res["results"][0]["preview"]


def test_searchbridge_laedt_nach_neuem_index_neu(sandbox, store):
    b = app_mod.SearchBridge()
    cfg = app_mod.load_config()
    b.ensure(cfg)
    erster = b.stamp
    time.sleep(0.01)
    con = sqlite3.connect(store / "corpus.db")
    con.execute("UPDATE chunks SET text = 'anderer Inhalt'")
    con.commit()
    con.close()
    b.ensure(cfg)
    assert b.stamp != erster                              # neue Kennung -> neu geladen


# --------------------------------------------------------------------------
# Formulierte Antwort: nutzt die Treffer der Suche, sucht nicht selbst
# --------------------------------------------------------------------------
def _antwort(port, body, kopf=None):
    """POST /api/answer und die NDJSON-Zeilen einsammeln."""
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    con.request("POST", "/api/answer", json.dumps(body),
                {"Content-Type": "application/json", **(kopf or {})})
    r = con.getresponse()
    roh = r.read().decode("utf-8")
    con.close()
    if r.getheader("Content-Type", "").startswith("application/x-ndjson"):
        return r.status, [json.loads(z) for z in roh.splitlines() if z.strip()]
    return r.status, json.loads(roh)


def test_antwort_nutzt_die_treffer_der_suche(sandbox, with_ollama, store, monkeypatch):
    """Kein zweites Retrieval: die Antwort sieht genau die Treffer, die auch in
    der Liste stehen – sonst könnte sie Unauffindbares zitieren."""
    gesehen = {}
    monkeypatch.setattr(app_mod.answer, "stream",
                        lambda q, quellen, model, ollama, lang="de", **kw:
                        gesehen.update(query=q, quellen=quellen, model=model,
                                       lang=lang) or iter([{"text": "Antwort [1]."}]))
    a = app_mod.App(app_mod.load_config())
    httpd = app_mod.make_server(a, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        code, zeilen = _antwort(httpd.server_address[1], {"q": "Rechnung"})
        assert code == 200
        assert zeilen[0]["sources"][0]["n"] == 1          # Nummerierung ab 1
        assert zeilen[0]["model"] == a.cfg["chat_model"]
        assert {"text": "Antwort [1]."} in zeilen
        assert zeilen[-1] == {"done": True}
        # Volltext statt Vorschau – aus 200 Zeichen lässt sich nichts beantworten
        assert "4711" in gesehen["quellen"][0]["text"]
        assert gesehen["query"] == "Rechnung"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_antwort_ohne_chat_modell(sandbox, store, monkeypatch):
    monkeypatch.setattr(app_mod, "check_ollama",
                        lambda url, model, chat_model=None, timeout=1.5: {
                            "running": True, "models": [], "has_model": True,
                            "has_chat_model": False, "error": None,
                            "model": model, "chat_model": chat_model, "url": url})
    a = app_mod.App(app_mod.load_config())
    httpd = app_mod.make_server(a, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        code, d = _antwort(httpd.server_address[1], {"q": "Rechnung"})
        assert code == 503 and schluessel(d["error"]) == "srv.answer.nomodel"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_antwort_ohne_suchbegriff_und_ohne_treffer(sandbox, with_ollama, store):
    a = app_mod.App(app_mod.load_config())
    httpd = app_mod.make_server(a, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        port = httpd.server_address[1]
        code, d = _antwort(port, {"q": "   "})
        assert code == 400 and schluessel(d["error"]) == "srv.answer.noquery"
        code, d = _antwort(port, {"q": "xyzzyplugh"})
        assert code == 200 and schluessel(d["error"]) == "srv.answer.nohits"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_antwort_folgt_der_spracheinstellung(sandbox, with_ollama, store, monkeypatch):
    gesehen = {}
    monkeypatch.setattr(app_mod.answer, "stream",
                        lambda q, quellen, model, ollama, lang="de", **kw:
                        gesehen.update(lang=lang) or iter([]))
    cfg = app_mod.load_config()
    cfg["language"] = "fr"
    a = app_mod.App(cfg)
    httpd = app_mod.make_server(a, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        _antwort(httpd.server_address[1], {"q": "Rechnung"})
        assert gesehen["lang"] == "fr"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_antwort_begrenzt_die_quellenzahl(sandbox, with_ollama, store, monkeypatch):
    gesehen = {}
    monkeypatch.setattr(app_mod.answer, "stream",
                        lambda q, quellen, *a, **kw:
                        gesehen.update(n=len(quellen)) or iter([]))
    cfg = app_mod.load_config()
    cfg["answer_sources"] = 99                   # jenseits der Grenze
    a = app_mod.App(cfg)
    httpd = app_mod.make_server(a, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        _antwort(httpd.server_address[1], {"q": "Rechnung"})
        assert gesehen["n"] <= 20
    finally:
        httpd.shutdown()
        httpd.server_close()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
@pytest.fixture
def server(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    httpd = app_mod.make_server(a, 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield a, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, body=None, host=None):
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if host:
        headers["Host"] = host
    con.request(method, path, json.dumps(body) if body is not None else None, headers)
    r = con.getresponse()
    raw = r.read()
    con.close()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, raw.decode("utf-8", "replace")


def test_http_liefert_die_oberflaeche(server):
    _, port = server
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    con.request("GET", "/")
    r = con.getresponse()
    body = r.read().decode("utf-8")
    con.close()
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/html")
    assert "Munimentum" in body


def test_http_status(server):
    _, port = server
    code, s = call(port, "GET", "/api/status")
    assert code == 200
    assert set(["token", "ollama", "store", "jobs", "mcp", "config"]) <= set(s)


def test_http_fremder_host_wird_abgewiesen(server):
    """Schutz gegen DNS-Rebinding: ein Name, der auf 127.0.0.1 zeigt, reicht sonst,
    damit eine beliebige Webseite den ganzen Mailbestand abfragen kann."""
    _, port = server
    code, _ = call(port, "GET", "/api/status", host="angreifer.example.com")
    assert code == 403
    code, _ = call(port, "POST", "/api/run", {"index": True}, host="angreifer.example.com")
    assert code == 403


def test_http_unbekannter_pfad(server):
    _, port = server
    assert call(port, "GET", "/api/gibtsnicht")[0] == 404
    assert call(port, "POST", "/api/gibtsnicht", {})[0] == 404


def test_http_token_speichern(server, sandbox):
    a, port = server
    tok = make_jwt(exp=time.time() + 3600,
                   scp="Mail.Read Calendars.Read Contacts.Read Chat.Read")
    code, r = call(port, "POST", "/api/token", {"token": "Bearer " + tok})
    assert code == 200 and r["ok"]
    assert app_mod.read_token() == tok
    assert a.status()["wizard"] is None            # gültig -> kein Assistent mehr


def test_http_token_abgelaufen_wird_gemeldet(server):
    _, port = server
    code, r = call(port, "POST", "/api/token", {"token": make_jwt(exp=time.time() - 10)})
    assert not r["ok"] and schluessel(r["message"]) == "srv.token.stale"


def test_http_token_fehlende_rechte_werden_benannt(server):
    _, port = server
    code, r = call(port, "POST", "/api/token",
                   {"token": make_jwt(exp=time.time() + 3600, scp="Mail.Read")})
    assert r["ok"] and schluessel(r["message"]) == "srv.token.saved.scopes"
    assert "Calendars.Read" in werte(r["message"])["list"]


def test_http_token_muell_wird_abgelehnt(server):
    _, port = server
    assert call(port, "POST", "/api/token", {"token": ""})[1]["ok"] is False
    assert call(port, "POST", "/api/token", {"token": "zu-kurz"})[1]["ok"] is False


def test_http_wizard_seen(server):
    """„Später“ setzt die Merkung eines totgelaufenen Tokens zurück – sonst ginge
    der Assistent beim nächsten Statusabruf sofort wieder auf."""
    a, port = server
    a.jobs.token_expired = True
    call(port, "POST", "/api/wizard-seen")
    assert a.jobs.token_expired is False


def test_http_run_ohne_token(server):
    _, port = server
    code, r = call(port, "POST", "/api/run", {"outlook": True})
    assert code == 409 and not r["ok"]


@pytest.mark.parametrize("cats,erwartet", [
    # (gibt es den Kalenderschritt, liest er die Mails)
    (["mail", "calendar"], (True, True)),
    (["contacts"], (True, False)),        # der gemeldete Fall
    (["calendar"], (True, False)),
    (["mail"], (False, False)),           # nichts aufzubauen
])
def test_http_run_stimmt_den_kalenderschritt_ab(server, monkeypatch, cats, erwartet):
    """Der Weg, den die Oberfläche wirklich geht. Sie schickt weiterhin
    calendar=true zu jedem Outlook-Lauf; verfeinert wird serverseitig, damit
    die Regel nur an einer Stelle steht."""
    a, port = server
    a.cfg["outlook_categories"] = cats
    monkeypatch.setattr(app_mod, "read_token", lambda *x, **kw: "tok")
    gesehen = {}
    monkeypatch.setattr(a.jobs, "start",
                        lambda steps, label: gesehen.setdefault("steps", steps) or True)

    code, r = call(port, "POST", "/api/run",
                   {"outlook": True, "index": True, "calendar": True})
    assert code == 200 and r["ok"]
    kal = [s for s in gesehen["steps"] if s["key"] == "calendar"]
    assert (bool(kal), bool(kal) and "--no-reconstruct" not in kal[0]["argv"]) == erwartet


def test_http_kalenderknopf_bleibt_vollstaendig(server, monkeypatch):
    """„Kalender & Kontakte aufbauen“ kommt ohne outlook – wer ihn drückt, will
    die Auswertung, unabhängig davon, was zuletzt exportiert wurde."""
    a, port = server
    a.cfg["outlook_categories"] = ["contacts"]
    gesehen = {}
    monkeypatch.setattr(a.jobs, "start",
                        lambda steps, label: gesehen.setdefault("steps", steps) or True)

    code, r = call(port, "POST", "/api/run", {"calendar": True})
    assert code == 200 and r["ok"]
    kal = [s for s in gesehen["steps"] if s["key"] == "calendar"]
    assert kal and "--no-reconstruct" not in kal[0]["argv"]


def test_http_config_speichern(server, sandbox):
    a, port = server
    code, r = call(port, "POST", "/api/config",
                   {"outlook_categories": ["contacts", "quatsch"], "workers": 2,
                    "mcp_port": "nonsense", "unbekannt": "x"})
    assert code == 200
    assert r["config"]["outlook_categories"] == ["contacts"]
    assert r["config"]["workers"] == 2
    assert r["config"]["mcp_port"] == app_mod.DEFAULT_CONFIG["mcp_port"]   # unverändert
    assert "unbekannt" not in r["config"]
    assert app_mod.load_config()["workers"] == 2                          # persistiert


def test_http_config_schalter_und_ordner(server, sandbox):
    a, port = server
    code, r = call(port, "POST", "/api/config",
                   {"embed_images": False, "include_hidden": True,
                    "skip_folders": "Archiv\nDrafts", "index_batch": 16})
    assert code == 200
    cfg = r["config"]
    assert cfg["embed_images"] is False and cfg["include_hidden"] is True
    assert cfg["skip_folders"] == ["archiv", "drafts"]
    assert cfg["index_batch"] == 16
    assert app_mod.load_config()["skip_folders"] == ["archiv", "drafts"]


@pytest.mark.parametrize("key,eingabe,erwartet", [
    ("workers", 99, 8),          # Graph erlaubt 4 gleichzeitig, mehr ist Drosselung
    ("workers", 0, 1),
    ("mcp_port", 80, 1024),      # privilegierte Ports gehören nicht dazu
    ("mcp_port", 99999, 65535),
    ("index_batch", 9999, 512),
    ("index_batch", -3, 1),
])
def test_http_config_begrenzt_zahlen(server, key, eingabe, erwartet):
    """Eine vertippte Zahl darf den nächsten Lauf nicht lahmlegen."""
    code, r = call(server[1], "POST", "/api/config", {key: eingabe})
    assert r["config"][key] == erwartet


def test_http_config_ignoriert_unsinnige_zahlen(server):
    vorher = call(server[1], "GET", "/api/status")[1]["config"]["workers"]
    r = call(server[1], "POST", "/api/config", {"workers": "vier"})[1]
    assert r["config"]["workers"] == vorher


def test_status_nennt_die_ordner_vorgabe(server):
    """Der Zurücksetzen-Knopf in der Oberfläche füllt sich daraus."""
    s = call(server[1], "GET", "/api/status")[1]
    assert s["skip_folders_default"] == sorted(app_mod.SKIP_FOLDERS_DEFAULT)


def test_http_zeitplan_speichern(server, sandbox):
    a, port = server
    code, r = call(port, "POST", "/api/schedule",
                   {"enabled": True, "interval_minutes": 1, "teams": False})
    assert code == 200
    assert r["schedule"]["enabled"] is True
    assert r["schedule"]["interval_minutes"] == 5          # Untergrenze greift
    assert r["schedule"]["teams"] is False
    assert app_mod.load_config()["schedule"]["enabled"] is True
    assert a.scheduler.last_run is not None                # Abstand zählt ab jetzt


def test_http_mcp_ohne_index(server):
    _, port = server
    code, r = call(port, "POST", "/api/mcp", {"action": "start"})
    assert not r["ok"] and schluessel(r["message"]) == "srv.mcp.noindex"
    assert call(port, "POST", "/api/mcp", {"action": "quatsch"})[1]["ok"] is False


def test_http_log(server):
    a, port = server
    a.jobs.log("hallo")
    code, r = call(port, "GET", "/api/log?since=0")
    assert code == 200 and r["lines"][-1]["text"] == "hallo"
    assert call(port, "GET", f"/api/log?since={r['seq']}")[1]["lines"] == []


def test_http_kalender_fehlt(server):
    code, r = call(server[1], "GET", "/api/calendar")
    assert code == 404 and r["recs"] == [] and schluessel(r["error"]) == "cal.missing"


def test_http_kalender_wird_gepackt_ausgeliefert(server, sandbox):
    """Rund 5 MB JSON – ungepackt wäre das bei jedem Tab-Wechsel Verschwendung."""
    a, port = server
    daten = {"generated": "2026-08-07T10:00:00", "counts": {"kalender": 1},
             "recs": [{"src": "kalender", "title": "Regelrunde", "ts": 1.0,
                       "st": "deleted", "root": "outlook", "rel": "E-Mail/x.eml"}]}
    ziel = app_mod.calendar_file(a.cfg)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    ziel.write_text(json.dumps(daten, ensure_ascii=False), encoding="utf-8")

    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    con.request("GET", "/api/calendar", None, {"Accept-Encoding": "gzip"})
    r = con.getresponse()
    roh = r.read()
    con.close()
    assert r.status == 200 and r.getheader("Content-Encoding") == "gzip"
    assert json.loads(gzip.decompress(roh))["recs"][0]["title"] == "Regelrunde"

    # Ohne Accept-Encoding: unverändert durchreichen
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    con.request("GET", "/api/calendar", None, {"Accept-Encoding": "identity"})
    r2 = con.getresponse()
    klar = r2.read()
    con.close()
    assert r2.getheader("Content-Encoding") is None
    assert json.loads(klar)["counts"]["kalender"] == 1


def test_kalender_puffer_erkennt_neue_daten(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    ziel = app_mod.calendar_file(a.cfg)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    ziel.write_text('{"recs": [], "counts": {"kalender": 1}}', encoding="utf-8")
    erst, _ = a.calendar_payload()
    assert a.calendar_payload()[0] is erst          # gepuffert, nicht neu gelesen
    time.sleep(0.01)
    ziel.write_text('{"recs": [], "counts": {"kalender": 2}}', encoding="utf-8")
    zweit, _ = a.calendar_payload()
    assert b'"kalender": 2' in zweit                # neu eingelesen


def test_http_suche_ohne_index_meldet_das(server):
    _, port = server
    code, r = call(port, "GET", "/api/search?q=test")
    assert code == 200 and r["hits"] == [] and schluessel(r["error"]) == "srv.noindex"


def test_http_ollama_recheck(server):
    _, port = server
    code, r = call(port, "POST", "/api/ollama-recheck")
    assert code == 200 and r["running"] is True


def test_http_kaputter_body_wird_toleriert(server):
    _, port = server
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    con.request("POST", "/api/config", "{kein json",
                {"Content-Type": "application/json"})
    r = con.getresponse()
    r.read()
    con.close()
    assert r.status == 200          # leerer Body -> nichts geändert, kein Absturz


def test_http_suche_und_quelldatei(sandbox, with_ollama, store):
    """Suche und Quelldatei-Auslieferung über den Server, gegen den echten Store."""
    a = app_mod.App(app_mod.load_config())
    httpd = app_mod.make_server(a, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        code, r = call(port, "GET", "/api/search?q=Rechnung&k=5")
        assert code == 200 and r["count"] >= 1
        assert r["semantic"] is False                      # ohne vectors.npy
        uri = r["results"][0]["uri"]
        assert uri.startswith("o365://teams/")

        code, r2 = call(port, "GET", "/api/people?limit=5")
        assert "Alice Example" in [p["name"] for p in r2["people"]]

        code, r3 = call(port, "GET", "/api/document?uid=" + r["results"][0]["uid"])
        assert "4711" in json.dumps(r3)

        con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        con.request("GET", "/source?root=teams&path=1on1/alice__abc.html")
        resp = con.getresponse()
        body = resp.read().decode("utf-8")
        assert resp.status == 200
        assert resp.getheader("Content-Security-Policy") == "sandbox"
        assert "4711" in body
        # Teams-Exporte sind zum Lesen gemacht und bleiben im Browser.
        assert resp.getheader("Content-Disposition") is None
        assert resp.getheader("Content-Type").startswith("text/html")
        con.close()

        # Ausbruch aus dem Export-Ordner wird abgewiesen
        con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        con.request("GET", "/source?root=teams&path=../../etc/passwd")
        resp = con.getresponse()
        resp.read()
        assert resp.status == 404
        con.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


# --------------------------------------------------------------------------
# Gebündelter Betrieb (PyInstaller): Selbstaufruf statt .py-Dateien
# --------------------------------------------------------------------------
@pytest.fixture
def frozen(monkeypatch, tmp_path):
    """Tut so, als liefe app.py als gebündelte Datei."""
    monkeypatch.setattr(app_mod, "FROZEN", True)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Munimentum"))
    return tmp_path


def test_script_argv_als_skript(sandbox):
    argv = app_mod.script_argv("teams_export", "ordner")
    assert argv[0] == sys.executable
    assert argv[1].endswith("teams_export.py")
    assert argv[2] == "ordner"


def test_script_argv_gebuendelt(sandbox, frozen):
    """Im Bündel gibt es weder Interpreter noch .py-Dateien – die ausführbare
    Datei ruft sich selbst mit --run auf."""
    assert app_mod.script_argv("teams_export", "ordner") == \
        [sys.executable, "--run", "teams_export", "ordner"]


def test_script_argv_wandelt_argumente_in_text(sandbox):
    assert app_mod.script_argv("rag_index", "--port", 8365)[-1] == "8365"


def test_script_argv_lehnt_unbekanntes_teilprogramm_ab(sandbox):
    with pytest.raises(ValueError, match="Unbekanntes Teilprogramm"):
        app_mod.script_argv("rm", "-rf")


def test_build_steps_gebuendelt(sandbox, frozen):
    steps = app_mod.build_steps(app_mod.load_config(), outlook=True, index=True,
                                token="tok")
    assert steps[0]["argv"][:3] == [sys.executable, "--run", "outlook_export"]
    assert steps[1]["argv"][:3] == [sys.executable, "--run", "rag_index"]


def test_run_bundled_startet_teilprogramm(sandbox, monkeypatch):
    gesehen = {}
    monkeypatch.setattr(app_mod.importlib, "import_module",
                        lambda name: gesehen.update(name=name)
                        or type("M", (), {"main": staticmethod(
                            lambda: gesehen.update(argv=list(sys.argv)))}))
    app_mod.run_bundled("rag_index", ["--store", "s"])
    assert gesehen["name"] == "rag_index"
    assert gesehen["argv"] == ["rag_index.py", "--store", "s"]


def test_run_bundled_lehnt_unbekanntes_ab(sandbox):
    with pytest.raises(SystemExit, match="Unbekanntes Teilprogramm"):
        app_mod.run_bundled("boese", [])


def test_main_leitet_run_weiter(monkeypatch):
    gesehen = {}
    monkeypatch.setattr(app_mod, "run_bundled",
                        lambda name, argv: gesehen.update(name=name, argv=argv))
    monkeypatch.setattr(app_mod, "serve", lambda *a, **k: pytest.fail("darf nicht"))
    app_mod.main(["--run", "mcp_server", "--transport", "stdio"])
    assert gesehen == {"name": "mcp_server", "argv": ["--transport", "stdio"]}


def test_main_run_ohne_namen(monkeypatch):
    with pytest.raises(SystemExit, match="braucht einen Namen"):
        app_mod.main(["--run"])


def test_main_data_dir_haengt_die_pfade_um(monkeypatch, tmp_path):
    monkeypatch.setattr(app_mod, "serve", lambda *a, **k: None)
    ziel = tmp_path / "woanders"
    app_mod.main(["--data-dir", str(ziel), "--no-browser"])
    assert app_mod.BASE == ziel.resolve()
    assert app_mod.TOKEN_FILE == ziel.resolve() / "gx_token.txt"
    assert ziel.is_dir()                                    # wird angelegt


def test_data_dir_je_betriebssystem(monkeypatch):
    monkeypatch.setattr(app_mod, "FROZEN", True)
    for _n in ("MUNIMENTUM_DATA_DIR", "OFFICE365_DATA_DIR"):
        monkeypatch.delenv(_n, raising=False)
    # Ohne diese Zeile läse der Test den ECHTEN Zeiger im Benutzerordner: auf
    # einem Rechner, auf dem die App je einen Datenordner gesetzt hat, schlug er
    # deshalb fehl – auf der CI nie. Geprüft wird hier die Vorgabe je System,
    # nicht der Zeiger; der hat eigene Tests.
    monkeypatch.setattr(app_mod, "lies_zeiger", lambda: None)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert app_mod.data_dir().parts[-3:] == ("Library", "Application Support",
                                             app_mod.APP_DIRNAME)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\x\\AppData\\Local")
    assert app_mod.data_dir().name == app_mod.APP_DIRNAME
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdg")
    assert app_mod.data_dir() == Path("/tmp/xdg") / app_mod.APP_DIRNAME


def test_data_dir_per_umgebungsvariable(monkeypatch, tmp_path):
    monkeypatch.setenv("MUNIMENTUM_DATA_DIR", str(tmp_path))
    assert app_mod.data_dir() == tmp_path.resolve()


def test_data_dir_als_skript_ist_der_projektordner(monkeypatch):
    monkeypatch.setattr(app_mod, "FROZEN", False)
    for _n in ("MUNIMENTUM_DATA_DIR", "OFFICE365_DATA_DIR"):
        monkeypatch.delenv(_n, raising=False)
    assert app_mod.data_dir() == Path(app_mod.__file__).resolve().parent


def test_mcp_client_config_nennt_absolute_pfade(sandbox):
    cfg = app_mod.load_config()
    conf = app_mod.mcp_client_config(cfg, 8365)
    assert conf["http"]["mcpServers"]["munimentum"]["url"] \
        == "http://127.0.0.1:8365/mcp"
    args = conf["stdio"]["mcpServers"]["munimentum"]["args"]
    assert "--transport" in args and "stdio" in args
    # Ein Ordner statt drei Pfaden: die Unterordner heißen fest.
    assert "--data-dir" in args
    # Claude startet den Befehl in einem unbekannten Arbeitsverzeichnis
    ordner = args[args.index("--data-dir") + 1]
    assert Path(ordner).is_absolute() and ordner.startswith(str(sandbox))


def test_mcp_client_config_gebuendelt(sandbox, frozen):
    conf = app_mod.mcp_client_config(app_mod.load_config(), 8365)
    eintrag = conf["stdio"]["mcpServers"]["munimentum"]
    assert eintrag["command"] == sys.executable          # die App selbst
    assert eintrag["args"][:2] == ["--run", "mcp_server"]


def test_ensure_streams_faengt_fehlende_konsole_ab(sandbox, monkeypatch):
    """Windows-Bündel ohne Konsole: sys.stdout ist None, jedes print() flöge."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    f = app_mod.ensure_streams()
    try:
        assert sys.stdout is not None and sys.stderr is not None
        print("Testzeile")
    finally:
        f.close()
    assert "Testzeile" in (sandbox / "app.log").read_text(encoding="utf-8")


def test_ensure_streams_laesst_vorhandene_konsole_in_ruhe(sandbox):
    vorher = sys.stdout
    assert app_mod.ensure_streams() is None
    assert sys.stdout is vorher


def test_make_server_weicht_auf_den_naechsten_port_aus(sandbox, with_ollama):
    """Zweiter Start bzw. belegter Port: ein Doppelklick soll nicht mit einem
    Traceback enden, den in einer fensterlosen App niemand sieht."""
    a = app_mod.App(app_mod.load_config())
    erster = app_mod.make_server(a, 0)
    port = erster.server_address[1]
    try:
        zweiter = app_mod.make_server(a, port)
        try:
            assert zweiter.server_address[1] == port + 1
        finally:
            zweiter.server_close()
    finally:
        erster.server_close()


# --------------------------------------------------------------------------
# Oberfläche: das eingebettete JavaScript in node ausführen
# --------------------------------------------------------------------------
DOM_STUMMEL = """
process.on('unhandledRejection', function(){});
// Beim Laden ruft die Seite einmal /api/status. Kaeme dort {} zurueck, wuerde
// renderStatus mittendrin scheitern und ein halb gesetztes S hinterlassen -
// ein Zustand, den es im Betrieb nicht gibt. Also ein vollstaendiger Status.
global.fetch = function(){
  return Promise.resolve({json: function(){
    return Promise.resolve(typeof statusGeruest === 'function' ? statusGeruest() : {});
  }});
};
var knoten = {};
function mk(id){ return {id: id, innerHTML: '', textContent: '', className: '', value: '',
  scrollTop: 0, clientHeight: 0, scrollHeight: 0, childElementCount: 0, dataset: {},
  // classList merkt sich wirklich etwas. Eine Attrappe, die nur nickt, laesst
  // genau die Fehler durch, um die es hier geht ("Knopf bleibt sichtbar").
  classList: (function(){
    var drin = {};
    return {
      add: function(c){ drin[c] = true; },
      remove: function(c){ delete drin[c]; },
      contains: function(c){ return !!drin[c]; },
      toggle: function(c, an){
        if(an === undefined) an = !drin[c];
        if(an) drin[c] = true; else delete drin[c];
        return !!drin[c];
      }};
  })(),
  appendChild: function(){}, removeChild: function(){}, firstChild: null,
  style: {},                     // reicht: der Code setzt darauf nur Werte
  addEventListener: function(){}, scrollIntoView: function(){},
  // Attribute wirklich merken: ein Stummel, der nur nickt, liesse genau die
  // Fehler durch, um die es hier geht ("aria-expanded folgt dem Zustand nicht").
  attrs: {},
  setAttribute: function(k, v){ this.attrs[k] = String(v); },
  getAttribute: function(k){ return k in this.attrs ? this.attrs[k] : null; },
  focus: function(){ global.document.activeElement = this; },
  select: function(){},          // der Rückfall beim Kopieren markiert das Feld
  querySelector: function(){ return null; },
  querySelectorAll: function(){ return []; }}; }

// Der Assistent liegt in #modal. Ein Zuweisen von innerHTML ersetzt im Browser
// samtliche Kindknoten - das Textfeld #tok ist danach ein NEUES, leeres
// Element. Ohne dieses Verhalten koennte der Test gar nicht zeigen, ob eine
// Eingabe verloren geht, und waere wertlos.
global.zaehlerNeuzeichnen = 0;
var modalRoh = mk('modal');
var modal = {
  get innerHTML(){ return modalRoh.innerHTML; },
  set innerHTML(v){ modalRoh.innerHTML = v; global.zaehlerNeuzeichnen++;
                    delete knoten['tok']; },
  classList: modalRoh.classList,
};
knoten['modal'] = modal;

// Der Assistent baut sein Inneres als HTML-Zeichenkette. Fuer Tastatur und
// Fokus braucht es daraus echte Knoten - sonst koennte kein Test zeigen, dass
// ESC schliesst oder Tab im Fenster bleibt. Gemerkt je Zeichenkette, damit
// zwei Abfragen dieselben Objekte liefern (im Browser ist es derselbe Knoten;
// ohne das schluege jeder Vergleich mit activeElement fehl).
var knotenCache = {};
function ausHtml(html){
  if(knotenCache[html]) return knotenCache[html];
  var out = [], re = /<(button|textarea|a|summary|input|select)\\b([^>]*)>/g, m;
  while((m = re.exec(html))){
    (function(tag, attr){
      function A(name){
        var tr = new RegExp(name + '="([^"]*)"').exec(attr);
        return tr ? tr[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&') : '';
      }
      out.push({tag: tag, className: A('class'), id: A('id'), href: A('href'),
                onclickCode: A('onclick'),
                focus: function(){ global.document.activeElement = this; },
                click: function(){ (0, eval)(this.onclickCode); }});
    })(m[1], m[2]);
  }
  knotenCache[html] = out;
  return out;
}
function passt(n, sel){
  sel = sel.trim();
  if(sel === '[href]') return n.tag === 'a' && !!n.href;
  if(sel.indexOf('[tabindex]') === 0) return false;
  var teile = sel.split('.'), tag = teile.shift();
  if(tag && n.tag !== tag) return false;
  return teile.every(function(c){ return (' ' + n.className + ' ').indexOf(' ' + c + ' ') >= 0; });
}
modal.querySelectorAll = function(sel){
  var teile = String(sel).split(',');
  return ausHtml(modalRoh.innerHTML).filter(function(n){
    return teile.some(function(s){ return passt(n, s); });
  });
};
modal.querySelector = function(sel){ return modal.querySelectorAll(sel)[0] || null; };

global.document = {
  documentElement: {},
  title: '',
  activeElement: null,
  // Die Seite haengt ihre Tastaturbehandlung hier ein; `taste()` loest sie aus.
  handler: {},
  addEventListener: function(art, fn){ (this.handler[art] = this.handler[art] || []).push(fn); },
  getElementById: function(id){
    // Die Seite liest ihre Texte aus diesem eingebetteten JSON-Block.
    if(id === 'i18n') return {textContent: global.I18N_ROH};
    // Kindknoten des Assistenten gibt es nur, solange sie in dessen HTML stehen.
    if(id === 'tok' && !knoten['tok']){
      if(modalRoh.innerHTML.indexOf('id="tok"') < 0) return null;
      knoten['tok'] = mk('tok');
    }
    return knoten[id] || (knoten[id] = mk(id));
  },
  // Welcher Reiter offen ist, liest der Code ueber 'nav [data-tab].on'.
  // Sonst null: der Code prueft damit, ob ein Element schon existiert
  // ('#kalBox [data-rb]'), und ein immer wahrer Stummel liesse ihn den Aufbau
  // ueberspringen. Was es wirklich gibt, steht in `vorhanden`.
  querySelector: function(sel){
    sel = String(sel);
    if(sel.indexOf('data-tab') >= 0){
      var n = mk('tabbtn'); n.dataset = {tab: global.aktiverTab || 'export'}; return n;
    }
    for(var muster in global.vorhanden){
      if(sel.indexOf(muster) >= 0) return global.vorhanden[muster];
    }
    return null;
  },
  querySelectorAll: function(){ return []; },
  createElement: function(){ return mk('x'); },
  // Der Rueckfall beim Kopieren haengt ein Feld voruebergehend in die Seite.
  body: mk('body'),
};
// Einen Tastendruck ausloesen - wie im Browser, samt preventDefault.
global.taste = function(key, opt){
  var e = Object.assign({key: key, shiftKey: false, metaKey: false, ctrlKey: false,
                         verhindert: false}, opt || {});
  e.preventDefault = function(){ e.verhindert = true; };
  (global.document.handler.keydown || []).forEach(function(fn){ fn(e); });
  return e;
};
global.aktiverTab = 'export';
global.vorhanden = {'.rbcount': mk('rbcount'), 'main': mk('main'),
                    'nav': mk('nav'), '.balken': mk('balken')};
global.setInterval = function(){ return 0; };
// setTimeout echt lassen: die Kalenderpruefung wartet auf Promises.
global.alert = function(){};
"""

GRUNDZUSTAND = """
S = {token: {present: true, valid: true, expired: false, missing: [],
             account: 'a@example.com', expires_in_minutes: 620},
     ollama: {running: true, has_model: false, has_chat_model: false,
              model: 'bge-m3', chat_model: 'qwen2.5:7b', models: []},
     ollama_hint: {os: 'macOS', steps: ['Schritt eins'], brew: 'brew install ollama'},
     scopes_needed: ['Mail.Read', 'User.Read'],
     scope_queries: {'Mail.Read': 'https://graph.microsoft.com/v1.0/me/messages'},
     graph_explorer: 'https://example.invalid'};
var modal = document.getElementById('modal');
function pruefe(bedingung, text){ if(!bedingung) throw new Error(text); }
"""

# Der Token-Assistent darf die halb fertige Eingabe nicht wegwerfen.
PRUEFUNG_EINGABE = GRUNDZUSTAND + """
openWizard('token');
pruefe(modal.innerHTML.indexOf('id="tok"') >= 0, 'Assistent nicht gezeichnet');
pruefe(modal.innerHTML.indexOf('me/messages') >= 0, 'Beispielabfrage fehlt');
pruefe(zaehlerNeuzeichnen === 1, 'Erwartet: einmal gezeichnet');

// Jemand fuegt den Token ein. Der Statusabruf alle 2,5 Sekunden ruft
// openWizard erneut auf - ohne Zustandsaenderung darf dabei nichts passieren.
document.getElementById('tok').value = 'EINGEFUEGTER-TOKEN';
openWizard('token');
pruefe(zaehlerNeuzeichnen === 1, 'Ohne Aenderung neu gezeichnet');
pruefe(document.getElementById('tok').value === 'EINGEFUEGTER-TOKEN',
       'Eingabe wurde beim Statusabruf geloescht');

// Aendert sich der Zustand, MUSS neu gezeichnet werden - die Eingabe darf
// trotzdem nicht verloren gehen.
S.token.missing = ['Mail.Read'];
openWizard('token');
pruefe(zaehlerNeuzeichnen === 2, 'Zustandswechsel loeste kein Neuzeichnen aus');
pruefe(modal.innerHTML.indexOf('fehlen noch Berechtigungen') >= 0,
       'Zustandswechsel kam im Text nicht an');
pruefe(document.getElementById('tok').value === 'EINGEFUEGTER-TOKEN',
       'Eingabe ging beim Neuzeichnen verloren');

closeWizard('token');
openWizard('token');
pruefe(modal.innerHTML.indexOf('id="tok"') >= 0, 'Nach Schliessen nicht gezeichnet');
console.log('OK');
"""

# Der Ollama-Assistent muss merken, wenn nebenher "ollama pull" durchlief.
PRUEFUNG_OLLAMA = GRUNDZUSTAND + """
openWizard('ollama');
pruefe(modal.innerHTML.indexOf('fehlt noch') >= 0, 'Fehlendes Modell nicht gemeldet');

// Nichts geaendert: nicht neu zeichnen (sonst flackert es im Sekundentakt).
modal.innerHTML = 'UNVERAENDERT';
openWizard('ollama');
pruefe(modal.innerHTML === 'UNVERAENDERT', 'Ohne Aenderung neu gezeichnet');

// Modell ist da. Der Server verlangt jetzt KEINEN Assistenten mehr
// (wizard === null) – renderStatus muss den offenen trotzdem auffrischen.
S.ollama.has_model = true;
renderStatus(Object.assign({}, statusGeruest(), {wizard: null}));
pruefe(modal.innerHTML.indexOf('Ollama ist bereit') >= 0,
       'Offener Assistent blieb auf altem Stand: ' + modal.innerHTML.slice(0, 80));
console.log('OK');
"""

# Kalenderdaten werden erst beim Oeffnen des Reiters geholt und nach einem
# Neuaufbau verworfen. Der Stand kommt aus dem Status (Dateizeit) – wuerde
# stattdessen das "generated" aus dem JSON gemerkt, waeren die beiden Werte nie
# gleich und die Daten wuerden bei jedem Statusabruf neu geladen.
PRUEFUNG_KALENDER = GRUNDZUSTAND + """
var geholt = 0;
global.fetch = function(pfad){
  if(String(pfad).indexOf('/api/calendar') >= 0){
    geholt++;
    return Promise.resolve({json: function(){ return Promise.resolve(
      {generated: '2020-01-01T00:00:00', counts: {kalender: 1, rekonstruiert: 1},
       recs: [{src:'kalender', ts: 1750000000, te: 1750003600, st:'deleted', ad:0,
               title:'Jour Fixe', who:'Alice', d:'2025-06-15 14:00', ctx:'rekonstruiert',
               ppl:'alice', x:'', root:'outlook', rel:'E-Mail/absage.eml'},
              {src:'kontakte', title:'Alice Example', em:['a@example.com'], tel:[],
               org:'Firma', role:'Chefin', root:'outlook', rel:'kontakte/a.vcf'}]}); }});
  }
  return Promise.resolve({json: function(){ return Promise.resolve({}); }});
};

var status = statusGeruest();
status.calendar = {exists: true, built_at: '2026-08-07T10:00:00'};

// Erst den Start abwarten: die Seite ruft beim Laden selbst /api/status auf.
setTimeout(function(){
aktiverTab = 'suche';
offeneSicht = 'kalender';
renderStatus(status);
ladeKalender('kalender');
setTimeout(function(){
  pruefe(geholt === 1, 'Kalenderdaten nicht geholt');
  pruefe(REBUILT.length === 1, 'Rekonstruierter Termin fehlt');
  pruefe(contacts.length === 1, 'Kontakt fehlt');

  // Unveraenderter Stand: nicht erneut holen.
  renderStatus(status);
  renderStatus(status);
  pruefe(geholt === 1, 'Ohne Neuaufbau erneut geholt (Stand falsch gemerkt)');

  // Neuer Aufbau: Daten verwerfen und neu holen.
  var neu = Object.assign({}, status, {calendar: {exists: true, built_at: '2026-08-07T12:00:00'}});
  renderStatus(neu);
  setTimeout(function(){
    pruefe(geholt === 2, 'Nach Neuaufbau nicht neu geholt');
    console.log('OK');
  }, 10);
}, 10);
}, 0);
"""

# Adressbuch und die Liste der rekonstruierten Termine wirklich zeichnen –
# beide filtern über Suchbegriffe UND übersetzen dabei. Genau dort verdeckte
# einmal eine lokale Variable `t` die Übersetzungsfunktion t(), und die Ansicht
# blieb stumm auf "Wird geladen…" stehen, weil das Promise den Fehler schluckte.
PRUEFUNG_ANSICHTEN = GRUNDZUSTAND + """
global.fetch = function(pfad){
  if(String(pfad).indexOf('/api/calendar') >= 0){
    return Promise.resolve({json: function(){ return Promise.resolve(
      {generated: '2026-08-07T09:00:00', counts: {kalender: 2, rekonstruiert: 1},
       recs: [
        {src:'kontakte', title:'Alice Example', em:['alice@example.com'], tel:['+49 1'],
         org:'Firma GmbH', role:'Chefin', root:'outlook', rel:'kontakte/a.vcf'},
        {src:'kontakte', title:'Bob Builder', em:['bob@example.com'], tel:[],
         org:'Bau AG', role:'', root:'outlook', rel:'kontakte/b.vcf'},
        {src:'kalender', ts: 1750000000, te: 1750003600, ad:0, st:'confirmed',
         title:'Regelrunde', who:'Alice', d:'2025-06-15 14:00', ctx:'Kalender',
         loc:'Raum 7', att:['Bob'], root:'outlook', rel:'kalender/x.ics'},
        {src:'kalender', ts: 1750086400, te: 1750090000, ad:0, st:'deleted',
         title:'Jour Fixe', who:'Alice', d:'2025-06-16 14:00', ctx:'rekonstruiert',
         ppl:'alice', x:'Agenda', root:'outlook', rel:'E-Mail/absage.eml'}]}); }});
  }
  return Promise.resolve({json: function(){ return Promise.resolve(statusGeruest()); }});
};

setTimeout(function(){
  var status = statusGeruest();
  status.calendar = {exists: true, built_at: '2026-08-07T10:00:00'};
  renderStatus(status);
  aktiverTab = 'suche';
  offeneSicht = 'adressbuch';
  ladeKalender('adressbuch');

  setTimeout(function(){
    var buch = document.getElementById('kbBox').innerHTML;
    pruefe(buch.indexOf('Alice Example') >= 0, 'Adressbuch leer: ' + JSON.stringify(buch.slice(0,90)));
    pruefe(buch.indexOf('alice@example.com') >= 0, 'Mailadresse fehlt');
    pruefe(document.getElementById('kbStats').textContent.indexOf('2') >= 0, 'Zaehlung fehlt');

    // Suche im Adressbuch: derselbe Pfad, der die Uebersetzung verdeckt hatte
    document.getElementById('kbQ').value = 'Bau';
    drawBook();
    var gefiltert = document.getElementById('kbBox').innerHTML;
    pruefe(gefiltert.indexOf('Bob Builder') >= 0, 'Filter fand Bob nicht');
    pruefe(gefiltert.indexOf('Alice Example') < 0, 'Filter liess Alice stehen');

    // Rekonstruierte Termine: eigene Liste, ebenfalls mit Suche
    calMode = 'rebuilt';
    drawCal();
    var rahmen = document.getElementById('kalBox').innerHTML;
    pruefe(rahmen.indexOf('data-rb') >= 0, 'Rahmen der Liste fehlt');
    var liste = document.getElementById('rblist').innerHTML;
    pruefe(liste.indexOf('Jour Fixe') >= 0, 'Rekonstruierter Termin fehlt: ' +
           JSON.stringify(liste.slice(0, 90)));
    pruefe(liste.indexOf('Gelöscht') >= 0, 'Zustand fehlt');
    document.getElementById('rbQ').value = 'gibtsnicht';
    rbList();
    pruefe(document.getElementById('rblist').innerHTML.indexOf('Keine Treffer') >= 0,
           'Leermeldung fehlt');

    // Wochenansicht zeichnet den normalen Termin
    calMode = 'week';
    cursor = new Date(1750000000 * 1000);
    drawCal();
    pruefe(document.getElementById('kalBox').innerHTML.indexOf('Regelrunde') >= 0,
           'Wochenansicht leer');
    console.log('OK');
  }, 20);
}, 0);
"""

# Scheitert das Laden, muss das zu sehen sein. Ohne catch bleibt die Ansicht
# stumm auf "Wird geladen…" stehen – und niemand weiß, warum.
PRUEFUNG_LADEFEHLER = GRUNDZUSTAND + """
global.fetch = function(pfad){
  if(String(pfad).indexOf('/api/calendar') >= 0)
    return Promise.reject(new Error('Netz weg'));
  return Promise.resolve({json: function(){ return Promise.resolve(statusGeruest()); }});
};
setTimeout(function(){
  ladeKalender('adressbuch');
  setTimeout(function(){
    var buch = document.getElementById('kbBox').innerHTML;
    pruefe(buch.indexOf('Netz weg') >= 0,
           'Ladefehler wird verschluckt, Ansicht bleibt leer: ' + JSON.stringify(buch));
    pruefe(kalGeladen === false, 'Nach dem Fehler wird kein zweiter Versuch erlaubt');
    console.log('OK');
  }, 20);
}, 0);
"""

# Die Checkbox für die formulierte Antwort darf es nur geben, wenn auch ein
# Modell sie erzeugen kann – sonst verspricht die Oberfläche etwas, das nicht
# kommt. Und der Antwortkasten muss sich sichtbar von den Treffern abheben.
PRUEFUNG_KI = GRUNDZUSTAND + """
function zustand(chat, index){
  var st = statusGeruest();
  st.ollama = {running: true, has_model: true, has_chat_model: chat,
               model: 'bge-m3', chat_model: 'qwen2.5:7b', models: []};
  st.store = {exists: index, chunks: 5, semantic: true, built_at: null, model: null};
  return st;
}
// Die beiden hinteren Suchvarianten haengen an Ollama UND an einem Index mit
// Embeddings. Fehlt eines davon, sind sie ausgegraut - aber sichtbar.
function gesperrt(){
  return document.getElementById('m-aehnlich').disabled &&
         document.getElementById('m-ki').disabled;
}
renderStatus(zustand(false, true));
pruefe(gesperrt(), 'Varianten trotz fehlendem Sprachmodell waehlbar');
renderStatus(zustand(true, false));
pruefe(gesperrt(), 'Varianten trotz fehlendem Index waehlbar');
renderStatus(zustand(true, true));
pruefe(!gesperrt(), 'Varianten gesperrt, obwohl alles da ist');
pruefe(document.getElementById('modus-fehlt').classList.contains('hide'),
       'Hinweis auf Ollama steht da, obwohl es laeuft');

// Wer in einer gesperrten Variante steht, faellt auf die Textsuche zurueck.
suchmodus('ki');
renderStatus(zustand(false, true));
pruefe(SUCHMODUS === 'text', 'Gesperrte Variante blieb aktiv: ' + SUCHMODUS);

// Fussnoten: [1] verweist auf den ersten Treffer, Unbekanntes bleibt Text
kiQuellen = [{n: 1, uid: 'a'}, {n: 2, uid: 'b'}];
var h = mitFussnoten('Bezahlt [1], offen [2], erfunden [9].');
pruefe(h.indexOf('href="#treffer-1"') >= 0, 'Fussnote 1 verweist nicht');
pruefe(h.indexOf('href="#treffer-2"') >= 0, 'Fussnote 2 verweist nicht');
pruefe(h.indexOf('href="#treffer-9"') < 0, 'Erfundene Fussnote wurde verlinkt');
pruefe(h.indexOf('[9]') >= 0, 'Erfundene Fussnote verschwand ganz');

// Welche Treffer zitiert wurden – fuer die Hervorhebung in der Liste
pruefe(zitierte('a [2] b [1] c [2]').join(',') === '2,1', 'Zitate falsch erkannt');
pruefe(zitierte('ohne').length === 0, 'Zitate erfunden');
console.log('OK');
"""

# Drei Reiter oben, drei Sichten darunter. Der Test schaltet durch und schaut,
# was sichtbar ist – und ob die zuletzt gewählte Sicht einen Reiterwechsel
# übersteht.
PRUEFUNG_NAV = GRUNDZUSTAND + """
var sichtbar = {};
['export','suche','einstellungen'].forEach(function(t){
  document.getElementById('tab-'+t).classList.toggle = function(c, an){ sichtbar[t] = !an; };
});
['treffer','kalender','adressbuch'].forEach(function(v){
  document.getElementById('sicht-'+v).classList.toggle = function(c, an){
    sichtbar['sicht:'+v] = !an; };
});
function offen(){ return Object.keys(sichtbar).filter(function(k){ return sichtbar[k]; }); }
function hat(x){ return offen().indexOf(x) >= 0; }

tab('export');
pruefe(hat('export') && !hat('suche') && !hat('einstellungen'), 'Export: ' + offen());

tab('suche');
pruefe(hat('suche') && !hat('export'), 'Suche: ' + offen());
pruefe(hat('sicht:treffer'), 'Suche zeigt nicht die Trefferliste: ' + offen());

sicht('kalender');
pruefe(hat('sicht:kalender') && !hat('sicht:treffer'), 'Kalender: ' + offen());
sicht('adressbuch');
pruefe(hat('sicht:adressbuch') && !hat('sicht:kalender'), 'Adressbuch: ' + offen());

// Reiter wechseln und zurueck: die gewaehlte Sicht bleibt
tab('einstellungen');
pruefe(hat('einstellungen') && !hat('suche'), 'Einstellungen: ' + offen());
tab('suche');
pruefe(hat('sicht:adressbuch'), 'Sicht nach Reiterwechsel vergessen: ' + offen());

// Die Kachel im Kopf fuehrt weiter zu ihrem Thema, jetzt in den Einstellungen
zeigeEinstellung('mcp-karte');
pruefe(hat('einstellungen'), 'MCP-Kachel fuehrt nicht in die Einstellungen');
console.log('OK');
"""

# Der Balken hat zwei Ebenen: Schritt i von n, und darin so genau, wie das
# Skript es weiss. Wo keine Gesamtzahl vorliegt (Outlook entdeckt seine Mails
# erst im Laufen), darf keine Prozentzahl erfunden werden.
PRUEFUNG_BALKEN = GRUNDZUSTAND + """
var breite = null, unbekannt = null;
document.getElementById('balken-fuell').style = {set width(v){ breite = v; }};
global.vorhanden['.balken'].classList.toggle = function(c, an){
  if(c === 'unbekannt') unbekannt = an; };

function lauf(index, n, progress){
  return {busy: true, last: null, token_expired: false, seq: 0,
          job: {label: 'job.export', step: 'job.step.teams', index: index,
                steps: new Array(n), progress: progress}};
}

// Schritt 1 von 4, darin 25 % -> 6 %
zeigeFortschritt(lauf(0, 4, {done: 25, total: 100, what: 'chats'}));
pruefe(unbekannt === false, 'Balken als unbekannt markiert, obwohl Gesamtzahl da');
pruefe(breite === '6%', 'Breite bei Schritt 1/4 und 25%: ' + breite);
pruefe(document.getElementById('fortschritt-text').textContent.indexOf('25') >= 0,
       'Zahl fehlt in der Zeile');

// Schritt 3 von 4, darin halb -> (2 + 0,5) / 4 = 63 %
zeigeFortschritt(lauf(2, 4, {done: 50, total: 100, what: 'chats'}));
pruefe(breite === '63%', 'Breite bei Schritt 3/4 und 50%: ' + breite);

// Ohne Gesamtzahl: gestreift, keine erfundene Breite
breite = null;
zeigeFortschritt(lauf(1, 4, {done: 1234, what: 'mails'}));
pruefe(unbekannt === true, 'Ohne Gesamtzahl nicht als unbekannt markiert');
pruefe(breite === null, 'Ohne Gesamtzahl wurde eine Breite gesetzt: ' + breite);
var zeile = document.getElementById('fortschritt-text').textContent;
pruefe(zeile.indexOf('1.234') >= 0 || zeile.indexOf('1,234') >= 0,
       'Zahl fehlt: ' + zeile);

// Fertig: Balken weg, letzte Meldung in die Protokollleiste
var versteckt = null;
document.getElementById('fortschritt').classList.toggle = function(c, an){ versteckt = an; };
zeigeFortschritt({busy: false, job: null, seq: 0, token_expired: false,
                  last: {label: 'job.export', ok: true, detail: '',
                         finished: '2026-08-08T10:00:00'}});
pruefe(versteckt === true, 'Balken bleibt nach dem Lauf stehen');
pruefe(document.getElementById('log-letzte').textContent.length > 3,
       'Letzte Meldung fehlt in der Protokollleiste');
console.log('OK');
"""

# renderStatus liest viel mehr aus dem Status als die Assistenten – ein
# vollstaendiges Geruest, damit der Aufruf oben durchlaeuft.
STATUS_GERUEST = """
function statusGeruest(){
  return {token: S.token, ollama: S.ollama, ollama_hint: S.ollama_hint,
          scopes_needed: S.scopes_needed, scope_queries: S.scope_queries,
          graph_explorer: S.graph_explorer, data_dir: '/tmp/daten', frozen: false,
          store: {exists: true, chunks: 5, messages: 2, semantic: false,
                  built_at: null, model: null, features: ['thread', 'gone']},
          auth: {mode: 'token', signed_in: false, account: null, device: null,
                 own_registration: false, client_id: 'std', tenant: 'organizations',
                 default_client_id: 'std'},
          update: {status: 'off', current: '1.0.1', latest: null, url: null,
                   newer: false, error: null, releases_url: 'https://x'},
          exports: {teams: {last_run: null}, outlook: {last_run: null}},
          jobs: {busy: false, job: null, last: null, token_expired: false, seq: 0},
          mcp: {running: false, url: 'http://127.0.0.1:8365/mcp', error: null,
                config: {http: {}, stdio: {}}},
          config: {outlook_categories: [], teams_categories: [], store_dir: 'rag_store',
                   language: 'auto',
                   schedule: {enabled: false, interval_minutes: 60,
                              outlook: true, teams: true, index: true}},
          calendar: {exists: false, built_at: null},
          schedule_enabled: false, schedule_next: null, wizard: null};
}
"""


def _seiten_js():
    treffer = re.search(r"<script>(.*?)</script>", app_mod.PAGE, re.S)
    assert treffer, "Kein <script>-Block in der Seite"
    return treffer.group(1)


PRUEFUNG_BEENDEN = GRUNDZUSTAND + """
var gesendet = [];
global.fetch = function(pfad, opt){
  gesendet.push(String(pfad));
  return Promise.resolve({json: function(){ return Promise.resolve(statusGeruest()); }});
};
global.confirm = function(text){ global.gefragt = text; return true; };

renderStatus(statusGeruest());
beenden();
pruefe(String(global.gefragt).length > 10, 'Es wurde nicht rueckgefragt');
pruefe(gesendet.indexOf('/api/quit') >= 0, 'Kein Beenden an den Server: ' + gesendet.join(','));
pruefe(beendet === true, 'Zustand nicht gesetzt');

// Danach darf nicht weiter abgefragt werden – sonst Fehler ohne Ende.
var vorher = gesendet.length;
refresh(); pullLog();
pruefe(gesendet.length === vorher, 'Fragt nach dem Beenden weiter');
console.log('OK');
"""


def test_beenden_fragt_zurueck_und_hoert_auf_zu_fragen():
    """Ohne Knopf bliebe nur die Aktivitätsanzeige – die App hat kein Fenster."""
    _in_node(PRUEFUNG_BEENDEN)


PRUEFUNG_BEENDEN_LAUF = GRUNDZUSTAND + """
global.confirm = function(text){ global.gefragt = text; return false; };
var st = statusGeruest();
st.jobs = {busy: true, job: {label: 'job.export', step: 'job.step.outlook', index: 0,
                             steps: ['a']}, last: null, token_expired: false, seq: 0};
renderStatus(st);
beenden();
pruefe(String(global.gefragt).indexOf('abgebrochen') >= 0,
       'Warnt nicht vor dem Abbruch: ' + global.gefragt);
pruefe(beendet === false, 'Trotz Abbruch der Rueckfrage beendet');
console.log('OK');
"""


def test_beenden_warnt_bei_laufendem_auftrag():
    _in_node(PRUEFUNG_BEENDEN_LAUF)


# Die vier Kacheln standen anfangs für ihre Bauteile: „Token“, „Ollama“,
# „269.744 Chunks“, „MCP läuft“. Für jemanden, der die Wörter nicht kennt, war
# das vier Mal keine Auskunft. Der Test hält beide Hälften der Lösung fest –
# Alltagssprache auf der Kachel, Fachbegriff im Tooltip.
PRUEFUNG_KACHELN = GRUNDZUSTAND + """
function kachel(id){ return document.getElementById('p-' + id + '-t').textContent; }
function hinweis(id){ return document.getElementById('pill-' + id).title || ''; }

var st = statusGeruest();
st.store = {exists: true, chunks: 269744, messages: 238408, semantic: true,
            built_at: '2026-08-07T09:00:00', model: 'bge-m3'};
st.ollama = {running: true, has_model: true, has_chat_model: true,
             model: 'bge-m3', chat_model: 'q', models: []};
st.mcp = {running: true, url: 'http://127.0.0.1:8365/mcp', error: null,
          config: {http: {}, stdio: {}}};
renderStatus(st);

var SYSTEMWORT = ['Chunk', 'chunk', 'MCP', 'Token', 'token', 'Ollama', 'Index'];
['token', 'ollama', 'mcp'].forEach(function(id){
  var text = kachel(id);
  pruefe(text.length > 0, 'Kachel ' + id + ' ist leer');
  SYSTEMWORT.forEach(function(w){
    pruefe(text.indexOf(w) < 0,
           'Kachel ' + id + ' spricht Systemsprache: "' + text + '"');
  });
});

// Der Zustand des Index steht im Analytics-Reiter, nicht im Kopf: zweimal
// dieselbe Zahl an zwei Orten widerspricht sich irgendwann.
pruefe(document.getElementById('pill-index') === null ||
       modal.innerHTML.indexOf('pill-index') < 0, 'Kachel wieder im Kopf');

// Der Fachbegriff bleibt erreichbar - eine Mausbewegung entfernt.
pruefe(hinweis('token').indexOf('Access Token') >= 0, 'Tooltip nennt den Token nicht');
pruefe(hinweis('token').indexOf('a@example.com') >= 0, 'Tooltip nennt das Konto nicht');
pruefe(hinweis('ollama').indexOf('Ollama') >= 0, 'Tooltip nennt Ollama nicht');
pruefe(hinweis('mcp').indexOf('MCP') >= 0, 'Tooltip nennt MCP nicht');

// Ohne Index darf der Kopf nicht stolpern - er zeigt den Zustand nicht mehr,
// aber renderStatus rechnet weiter damit (KI-Kasten, Sicht "Geloeschtes").
st.store = {exists: false, chunks: 0, messages: 0, semantic: false,
            built_at: null, model: null, features: []};
renderStatus(st);
pruefe(document.getElementById('m-ki').disabled,
       'KI-Variante trotz fehlendem Index waehlbar');
console.log('OK');
"""


def test_kacheln_sagen_die_bedeutung_und_nennen_den_begriff_im_tooltip():
    _in_node(PRUEFUNG_KACHELN)


def test_navigation_drei_reiter_drei_sichten():
    """Kalender und Adressbuch liegen unter der Suche, nicht daneben – und die
    zuletzt gewählte Sicht übersteht einen Reiterwechsel."""
    _in_node(PRUEFUNG_NAV)


def test_fortschrittsbalken_zwei_ebenen():
    """Schritt i von n mal Fortschritt im Schritt – und ohne Gesamtzahl keine
    erfundene Prozentangabe, sondern ein gestreifter Balken mit der Zahl."""
    _in_node(PRUEFUNG_BALKEN)


def test_ki_checkbox_und_fussnoten():
    """Die Checkbox erscheint nur mit Modell UND Index; Fußnoten verweisen in
    die Trefferliste, erfundene Nummern bleiben unverlinkter Text."""
    _in_node(PRUEFUNG_KI)


def test_jeder_reiter_liegt_im_hauptbereich():
    """Regression: der Einstellungen-Abschnitt stand hinter </main> und bekam
    damit weder Innenabstand noch Maximalbreite – seine Karten klebten am
    Fensterrand, anders als bei allen anderen Reitern."""
    seite = app_mod.PAGE
    haupt = seite[seite.index("<main>"):seite.index("</main>")]
    for reiter in ("export", "suche", "analytics", "einstellungen"):
        assert f'<section id="tab-{reiter}"' in haupt, f"{reiter} liegt außerhalb <main>"


def test_die_reiterzeile_bleibt_kurz():
    """Daten holen, Daten ansehen, Bestand beurteilen, einstellen. Mehr Ebenen
    oben verwirren mehr, als sie ordnen.

    Analytics kam als vierter dazu, weil es eine eigene Frage beantwortet –
    „was ist da und ist es vollständig?“ statt „wo steht dieses eine?“. Kalender
    und Adressbuch dagegen sind Sichten auf denselben Bestand wie die Suche und
    liegen darunter; Zeitplan und MCP sind Einstellungen.

    Die Zahl steht hier als Bremse: wer einen fünften anlegt, soll sich diese
    Begründung ansehen müssen."""
    seite = app_mod.PAGE
    nav = seite[seite.index("<nav>"):seite.index("</nav>")]
    assert nav.count("data-tab=") == 4, "Die Reiterzeile ist wieder gewachsen"
    for weg in ("kalender", "adressbuch", "zeitplan", "mcp"):
        assert f'data-tab="{weg}"' not in nav, f"{weg} ist wieder ein eigener Reiter"
    for sicht in ("treffer", "kalender", "adressbuch"):
        assert f'id="sicht-{sicht}"' in seite, f"Sicht {sicht} fehlt unter der Suche"
    # Zeitplan und MCP müssen in den Einstellungen gelandet sein, nicht verschwunden
    einst = seite[seite.index('<section id="tab-einstellungen"'):seite.index("</main>")]
    assert 'data-i18n="sched.title"' in einst and 'data-i18n="mcp.title"' in einst


def test_seite_enthaelt_gueltiges_javascript():
    node = shutil.which("node")
    if not node:
        pytest.skip("node nicht vorhanden")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(_seiten_js())
        pfad = f.name
    try:
        r = subprocess.run([node, "--check", pfad], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    finally:
        os.unlink(pfad)


def _in_node(pruefung, sprache="de"):
    """Das eingebettete JavaScript samt Prüfcode in node ausführen.

    Mit den echten Sprachdaten: so prüfen die Tests denselben Weg, den der
    Browser geht – Texte kommen aus lang/*.json, nicht aus dem Quelltext.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node nicht vorhanden")
    kopf = ("global.I18N_ROH = " + json.dumps(json.dumps(
        {"lang": sprache, "strings": i18n.strings(sprache),
         "languages": i18n.available()}, ensure_ascii=False)) + ";\n")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(kopf + DOM_STUMMEL + _seiten_js() + STATUS_GERUEST + pruefung)
        pfad = f.name
    try:
        r = subprocess.run([node, pfad], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert "OK" in r.stdout
    finally:
        os.unlink(pfad)


def test_assistent_ueberschreibt_die_eingabe_nicht():
    """Regression: der Assistent wurde bei jedem Statusabruf neu gezeichnet und
    löschte dabei den gerade eingefügten Token wieder aus dem Textfeld."""
    _in_node(PRUEFUNG_EINGABE)


def test_assistent_merkt_wenn_das_modell_nachgeladen_wurde():
    """Regression: nach 'ollama pull' wurde die Ampel im Kopf grün, im offenen
    Assistenten stand aber weiter 'Modell fehlt'. Sobald das Modell da ist,
    verlangt der Server gar keinen Assistenten mehr – ein bereits offener muss
    trotzdem aufgefrischt werden."""
    _in_node(PRUEFUNG_OLLAMA)


# Der Berechtigungsblock ist der technischste Teil des Dialogs – Namen wie
# Contacts.Read samt Graph-Adressen. Meist ist er längst erledigt und stand
# dann nur im Weg; fehlt aber wirklich eine Berechtigung, ist er das Thema.
PRUEFUNG_RECHTE = GRUNDZUSTAND + """
S.token.missing = [];
openWizard('token');
var html = modal.innerHTML;
pruefe(html.indexOf('<details class="rechte">') >= 0,
       'Berechtigungen stehen nicht in einem einklappbaren Block');
pruefe(html.indexOf('Mail.Read') >= 0, 'Berechtigungen fehlen ganz');

// Eingeklappt heisst: die Schritte kommen ohne sie aus. Drei statt vier.
var liste = html.split('<ol>')[1].split('</ol>')[0];
var schritte = liste.split('<li>').length - 1;
pruefe(schritte === 3, 'Erwartet drei Schritte, gezaehlt: ' + schritte);
pruefe(liste.indexOf('Mail.Read') < 0, 'Berechtigungen stehen noch in den Schritten');

// Fehlt wirklich etwas, muss der Block von selbst offen stehen.
S.token.missing = ['Mail.Read'];
openWizard('token');
pruefe(modal.innerHTML.indexOf('<details class="rechte" open>') >= 0,
       'Fehlende Berechtigung, Block aber zugeklappt');

// Und der Dialog spricht nicht mehr von Graph oder Tenant - ausser im Link
// auf die Seite, die tatsaechlich so heisst.
S.token.missing = [];
openWizard('token');
var ohneLink = modal.innerHTML.replace(/<a [^>]*>.*?<\\/a>/g, '');
['Tenant', 'Microsoft Graph', 'Access Token holen'].forEach(function(w){
  pruefe(ohneLink.indexOf(w) < 0, 'Dialog sagt noch "' + w + '"');
});
console.log('OK');
"""


def test_berechtigungen_sind_eingeklappt_solange_sie_nicht_fehlen():
    _in_node(PRUEFUNG_RECHTE)


# Vorher hatte jeder Assistent eine andere Knopfzahl - zwei, drei -, und im
# fertigen Ollama-Fenster war ausgerechnet "Schliessen" der primaere Knopf,
# waehrend die eigentliche Handlung blass daneben stand.
PRUEFUNG_MODALE = GRUNDZUSTAND + """
function zaehle(html, muster){ return html.split(muster).length - 1; }
// Tut der Knopf mehr, als den Dialog zu schliessen?
function handelt(knopf){
  return knopf.onclickCode.replace(/closeWizard\\([^)]*\\);?\\s*/g, '').length > 0;
}

// Alle drei Zustaende, die es gibt.
var faelle = [
  ['token',  function(){ S.token.present = false; }],
  ['ollama', function(){ S.ollama.running = true; S.ollama.has_model = false; }],
  ['ollama', function(){ S.ollama.running = true; S.ollama.has_model = true; }]
];
faelle.forEach(function(f, i){
  S.token = {present: true, valid: true, expired: false, missing: [],
             account: 'a@example.com', expires_in_minutes: 620};
  f[1]();
  closeWizard(f[0]);
  openWizard(f[0]);
  var html = modal.innerHTML, wo = 'Fall ' + i + ': ';

  pruefe(zaehle(html, 'class="modal-zu"') === 1, wo + 'kein oder mehrfaches Schliesskreuz');
  pruefe(zaehle(html, 'class="act"') === 1, wo + 'nicht genau ein primaerer Knopf');
  pruefe(zaehle(html, 'class="ghost"') <= 1, wo + 'mehr als ein sekundaerer Knopf');

  // Es gibt genau einen Ausgang: das Kreuz. Jeder andere Knopf muss etwas tun -
  // ein zweiter Knopf, der nur schliesst, ist derselbe Ausgang zweimal.
  modal.querySelectorAll('button.act, button.ghost').forEach(function(k){
    pruefe(handelt(k), wo + 'Knopf schliesst nur: "' + k.onclickCode + '"');
  });

  // Und wo es einen sekundaeren gibt, steht der primaere davor.
  if(zaehle(html, 'class="ghost"'))
    pruefe(html.indexOf('class="act"') < html.indexOf('class="ghost"'),
           wo + 'sekundaerer Knopf steht vor dem primaeren');
});
console.log('OK');
"""


def test_alle_assistenten_tragen_dieselben_knoepfe():
    _in_node(PRUEFUNG_MODALE)


# Ein modales Fenster nimmt die Seite in Beschlag. Wer keine Maus benutzt, muss
# trotzdem hinein, herum und wieder heraus.
PRUEFUNG_TASTATUR = GRUNDZUSTAND + """
var ausloeser = {focus: function(){ document.activeElement = this; }, name: 'Kachel'};
document.activeElement = ausloeser;

S.token.present = false;
openWizard('token');
pruefe(document.activeElement !== ausloeser, 'Fokus blieb ausserhalb des Dialogs');
pruefe(document.activeElement.id === 'tok', 'Fokus nicht im Textfeld');

// Neuzeichnen darf den Fokus nicht aus dem Textfeld reissen.
var drin = document.activeElement;
S.token.missing = ['Mail.Read'];
openWizard('token');
pruefe(document.activeElement === drin, 'Neuzeichnen riss den Fokus weg');

// Tab am Ende springt an den Anfang, Shift+Tab am Anfang ans Ende.
var liste = modal.querySelectorAll(
  'button, [href], textarea, input, select, summary, [tabindex]:not([tabindex="-1"])');
pruefe(liste.length >= 4, 'Zu wenige fokussierbare Elemente: ' + liste.length);
liste[liste.length - 1].focus();
pruefe(taste('Tab').verhindert, 'Tab am Ende nicht abgefangen');
pruefe(document.activeElement === liste[0], 'Tab am Ende verliess den Dialog');
pruefe(taste('Tab', {shiftKey: true}).verhindert, 'Shift+Tab am Anfang nicht abgefangen');
pruefe(document.activeElement === liste[liste.length - 1], 'Shift+Tab verliess den Dialog');

// Strg+Enter loest die primaere Handlung aus, ohne dorthin tabben zu muessen.
global.gespeichert = false;
global.saveToken = function(){ global.gespeichert = true; };
taste('Enter', {ctrlKey: true});
pruefe(global.gespeichert === true, 'Strg+Enter speicherte nicht');

// ESC schliesst - und gibt den Fokus zurueck, wo er herkam.
pruefe(taste('Escape').verhindert, 'ESC nicht abgefangen');
pruefe(wizardOffen === null, 'ESC schloss den Dialog nicht');
pruefe(document.activeElement === ausloeser, 'Fokus kam nicht zurueck');

// Ist keiner offen, darf ESC nichts anfassen.
document.activeElement = ausloeser;
taste('Escape');
pruefe(document.activeElement === ausloeser, 'ESC wirkte ohne offenen Dialog');
console.log('OK');
"""


def test_assistent_ist_mit_der_tastatur_bedienbar():
    _in_node(PRUEFUNG_TASTATUR)


# Der Assistent bietet beide Wege an – der Schlüssel bleibt vorausgewählt,
# weil er ohne Rückfrage bei der IT funktioniert.
PRUEFUNG_ANMELDEWAHL = GRUNDZUSTAND + """
S.auth = {mode: 'token', signed_in: false, account: null, own_registration: false,
          client_id: 'std', tenant: 'organizations', default_client_id: 'std',
          device: null};
S.token.present = false;
closeWizard('token'); openWizard('token');
var html = modal.innerHTML;
pruefe(html.indexOf('name="authmode"') >= 0, 'Keine Auswahl der Anmeldewege');
pruefe(html.indexOf('value="token" checked') >= 0, 'Schluessel ist nicht vorausgewaehlt');
pruefe(html.indexOf('id="tok"') >= 0, 'Textfeld fuer den Schluessel fehlt');
pruefe(html.indexOf('Graph Explorer') >= 0, 'Der Schluesselweg wird nicht erklaert');

// Umschalten: derselbe Assistent, anderer Inhalt.
S.auth.mode = 'login';
openWizard('token');
html = modal.innerHTML;
pruefe(html.indexOf('value="login" checked') >= 0, 'Login nicht vorausgewaehlt');
pruefe(html.indexOf('id="tok"') < 0, 'Textfeld steht noch da');
pruefe(html.indexOf('id="au-client"') >= 0, 'Eigene Registrierung nicht erreichbar');

// Ein laufender Gerätecode ist das Einzige, was dann zaehlt.
S.auth.device = {code: 'ABCD-1234', url: 'https://ms.example/dev', done: false};
openWizard('token');
pruefe(modal.innerHTML.indexOf('ABCD-1234') >= 0, 'Der Code wird nicht angezeigt');

// Angemeldet: die Abmeldung ist der sekundaere Knopf, nicht der primaere.
S.auth.device = null; S.auth.signed_in = true; S.auth.account = 'a@b.c';
openWizard('token');
pruefe(modal.innerHTML.indexOf('a@b.c') >= 0, 'Konto wird nicht genannt');
var act = modal.querySelector('button.act'), ghost = modal.querySelector('button.ghost');
pruefe(act.onclickCode.indexOf('starteLogin') >= 0, 'Primaer ist nicht das Anmelden');
pruefe(ghost && ghost.onclickCode.indexOf('abmelden') >= 0, 'Abmelden fehlt');

// Eigene Registrierung: der Block steht offen, wenn eine eingetragen ist.
S.auth.own_registration = true; S.auth.client_id = 'eigene-id';
openWizard('token');
pruefe(modal.innerHTML.indexOf('value="eigene-id"') >= 0, 'Eigene Client-ID fehlt');
console.log('OK');
"""


def test_assistent_bietet_beide_anmeldewege():
    _in_node(PRUEFUNG_ANMELDEWAHL)


def test_adressbuch_und_rekonstruierte_termine_zeichnen():
    """Regression: eine lokale Variable `t` verdeckte die Übersetzungsfunktion,
    drawBook warf, und weil das Promise keinen catch hatte, blieb das Adressbuch
    für immer bei „Wird geladen…“."""
    _in_node(PRUEFUNG_ANSICHTEN)


def test_ladefehler_wird_angezeigt_statt_verschluckt():
    """Regression: das Promise hatte kein catch – ein Fehler beim Laden ließ die
    Ansicht für immer bei „Wird geladen…“ stehen, ohne jeden Hinweis."""
    _in_node(PRUEFUNG_LADEFEHLER)


def test_kalender_wird_erst_bei_bedarf_und_nach_neuaufbau_geholt():
    """Die Kalenderdaten sind einige Megabyte: einmal holen, danach nur wieder,
    wenn der Aufbau-Schritt sie tatsächlich neu geschrieben hat."""
    _in_node(PRUEFUNG_KALENDER)


# --------------------------------------------------------------------------
# main()
# --------------------------------------------------------------------------
def test_serve_oeffnet_den_browser_und_raeumt_auf(sandbox, with_ollama, monkeypatch):
    """serve() bindet, startet Zeitplan und MCP, öffnet den Browser und räumt
    beim Beenden wieder auf."""
    geoeffnet = []
    monkeypatch.setattr(app_mod.webbrowser, "open", lambda url: geoeffnet.append(url))
    a = app_mod.App(app_mod.load_config())

    def stop_gleich():
        ende = time.time() + 5
        while time.time() < ende and not geoeffnet:
            time.sleep(0.02)
        httpd_box[0].shutdown()

    httpd_box = []
    echtes_make = app_mod.make_server
    monkeypatch.setattr(app_mod, "make_server",
                        lambda app, port, host="127.0.0.1":
                        httpd_box.append(echtes_make(app, port, host)) or httpd_box[0])
    threading.Timer(0.05, stop_gleich).start()
    app_mod.serve(a, 0, open_browser=True)

    assert geoeffnet and geoeffnet[0].startswith("http://127.0.0.1:")
    assert a.scheduler.ident is not None                    # Zeitplan-Thread lief
    assert a.scheduler.stop_event.is_set()                  # shutdown() hat aufgeräumt


# --------------------------------------------------------------------------
# Nur eine Instanz – und ein Weg, sie zu beenden
# --------------------------------------------------------------------------
def test_laeuft_bereits_erkennt_die_eigene_instanz(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    httpd = app_mod.make_server(a, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        assert app_mod.laeuft_bereits(port) is True
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_laeuft_bereits_bei_freiem_port(sandbox):
    import socket as _s
    with _s.socket() as sock:            # Port ermitteln und sofort freigeben
        sock.bind(("127.0.0.1", 0))
        frei = sock.getsockname()[1]
    assert app_mod.laeuft_bereits(frei, timeout=0.5) is False


def test_laeuft_bereits_bei_fremdem_dienst(sandbox):
    """Auf dem Port kann etwas anderes horchen – das ist keine zweite Instanz."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Fremd(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"hallo": "ich bin etwas anderes"}')

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Fremd)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        assert app_mod.laeuft_bereits(httpd.server_address[1], timeout=2) is False
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_serve_startet_keine_zweite_instanz(sandbox, with_ollama, monkeypatch):
    """Regression: jeder weitere Doppelklick legte eine zweite Instanz auf dem
    nächsten Port an – unsichtbar, weil die App kein Fenster hat."""
    a = app_mod.App(app_mod.load_config())
    erste = app_mod.make_server(a, 0)
    threading.Thread(target=erste.serve_forever, daemon=True).start()
    port = erste.server_address[1]
    geoeffnet = []
    monkeypatch.setattr(app_mod.webbrowser, "open", lambda url: geoeffnet.append(url))
    monkeypatch.setattr(app_mod, "make_server",
                        lambda *a, **k: pytest.fail("zweite Instanz gestartet"))
    try:
        zweite = app_mod.App(app_mod.load_config())
        assert app_mod.serve(zweite, port, open_browser=True) is None
        assert geoeffnet == [f"http://127.0.0.1:{port}/"]
    finally:
        erste.shutdown()
        erste.server_close()


def test_serve_mit_port_null_prueft_nicht(sandbox, with_ollama, monkeypatch):
    """Port 0 heißt "irgendein freier" – da gibt es nichts zu erkennen."""
    monkeypatch.setattr(app_mod, "laeuft_bereits",
                        lambda *a, **k: pytest.fail("darf nicht gefragt werden"))
    monkeypatch.setattr(app_mod.webbrowser, "open", lambda url: None)
    a = app_mod.App(app_mod.load_config())
    box = []
    echtes = app_mod.make_server
    monkeypatch.setattr(app_mod, "make_server",
                        lambda app, port, host="127.0.0.1":
                        box.append(echtes(app, port, host)) or box[0])
    threading.Timer(0.05, lambda: box[0].shutdown()).start()
    app_mod.serve(a, 0, open_browser=False)
    assert box


def test_main_reicht_argumente_an_serve_weiter(monkeypatch):
    gesehen = {}
    monkeypatch.setattr(app_mod, "serve",
                        lambda a, port, open_browser=True: gesehen.update(
                            port=port, browser=open_browser))
    monkeypatch.setattr(sys, "argv", ["app.py", "--port", "9001", "--no-browser"])
    app_mod.main()
    assert gesehen == {"port": 9001, "browser": False}


# --------------------------------------------------------------------------
# Anmeldemodus über HTTP
# --------------------------------------------------------------------------
def test_http_status_nennt_den_anmeldemodus(server):
    _, port = server
    code, r = call(port, "GET", "/api/status")
    assert code == 200
    au = r["auth"]
    assert au["mode"] == "token"                     # Vorgabe bleibt der Schlüssel
    assert au["own_registration"] is False
    assert au["client_id"] == app_mod.auth.STANDARD_CLIENT_ID


def test_http_modus_umschalten(server):
    a, port = server
    code, r = call(port, "POST", "/api/config", {"auth_mode": "login"})
    assert code == 200 and r["config"]["auth_mode"] == "login"
    assert call(port, "GET", "/api/status")[1]["auth"]["mode"] == "login"

    # Unbekanntes fällt auf den Weg zurück, der immer funktioniert.
    call(port, "POST", "/api/config", {"auth_mode": "quatsch"})
    assert a.cfg["auth_mode"] == "token"


def test_http_eigene_registrierung_speichern(server):
    a, port = server
    code, r = call(port, "POST", "/api/config",
                   {"client_id": " eigene-id ", "tenant": "contoso.example"})
    assert code == 200 and r["config"]["client_id"] == "eigene-id"
    st = call(port, "GET", "/api/status")[1]["auth"]
    assert st["own_registration"] is True and st["tenant"] == "contoso.example"
    assert a.cfg["tenant"] == "contoso.example"


def test_http_abmelden_loescht_den_cache(server, sandbox, monkeypatch):
    a, port = server
    geleert = []
    monkeypatch.setattr(app_mod.auth, "cache_leeren",
                        lambda: geleert.append(True) or True)
    a.device_login = {"code": "X", "done": False}
    code, r = call(port, "POST", "/api/logout")
    assert code == 200 and r["ok"]
    assert geleert == [True]
    assert a.device_login is None, "abgebrochene Anmeldung blieb stehen"


def test_login_starten_meldet_code_und_wartet(sandbox, monkeypatch, no_ollama):
    """Der Code muss sofort da sein – das Warten läuft daneben, sonst stünde
    die Oberfläche still, bis jemand am Handy fertig ist."""
    fertig = threading.Event()

    class FakeDevice:
        def __init__(self, scopes, client=None, mandant=None):
            self.scopes = scopes

        def start(self):
            return {"code": "ABCD-1234", "url": "https://ms.example/dev",
                    "expires_in": 900}

        def warten(self):
            fertig.wait(5)
            return True, ""

    monkeypatch.setattr(app_mod.auth, "DeviceLogin", FakeDevice)
    a = app_mod.App()
    ok, daten = a.login_starten()
    assert ok and daten["code"] == "ABCD-1234" and daten["done"] is False
    assert a.auth_status()["device"]["code"] == "ABCD-1234"

    fertig.set()
    ende = time.time() + 5
    while not (a.device_login or {}).get("done") and time.time() < ende:
        time.sleep(0.02)
    assert a.device_login["done"] and a.device_login["ok"]


def test_login_fordert_nur_noetige_rechte(sandbox, monkeypatch, no_ollama):
    """Mehr zu verlangen, als der Export braucht, wäre schlechter Stil
    gegenüber dem, der zustimmen soll."""
    gesehen = {}

    class FakeDevice:
        def __init__(self, scopes, client=None, mandant=None):
            gesehen["scopes"] = scopes

        def start(self):
            return {"code": "X", "url": "u", "expires_in": 60}

        def warten(self):
            return True, ""

    monkeypatch.setattr(app_mod.auth, "DeviceLogin", FakeDevice)
    a = app_mod.App()
    a.cfg["outlook_categories"] = ["mail"]
    a.cfg["teams_categories"] = []
    a.login_starten()
    kurz = {s.rsplit("/", 1)[-1] for s in gesehen["scopes"]}
    assert kurz == {"Mail.Read", "User.Read"}


def test_anmeldemodus_geht_an_die_unterprozesse(sandbox):
    """Sonst führte die App eine Einstellung, von der der Export nichts weiß."""
    cfg = app_mod.load_config()
    cfg.update(auth_mode="login", client_id="eigene-id", tenant="contoso.example")
    env = app_mod.build_steps(cfg, outlook=True)[0]["env"]
    assert env["GRAPH_AUTH"] == "login"
    assert env["GRAPH_CLIENT_ID"] == "eigene-id"
    assert env["GRAPH_TENANT"] == "contoso.example"


def test_leere_registrierung_wird_nicht_weitergereicht(sandbox):
    """Ein leeres Feld heißt „Microsofts Anwendung“, nicht „Client-ID ist ''“."""
    cfg = app_mod.load_config()
    env = app_mod.build_steps(cfg, outlook=True)[0]["env"]
    assert env["GRAPH_AUTH"] == "token"
    assert "GRAPH_CLIENT_ID" not in env and "GRAPH_TENANT" not in env


def test_login_modus_laeuft_ohne_eingefuegten_schluessel(sandbox, no_ollama, monkeypatch):
    """Im Login-Modus trägt der Cache – das Fehlen eines Schlüssels darf keinen
    Lauf mehr verhindern."""
    a = app_mod.App()
    monkeypatch.setattr(a.jobs, "start", lambda steps, label: True)
    monkeypatch.setattr(app_mod, "read_token", lambda *x, **kw: "")

    ok, why = a.launch(outlook=True, label="job.export")
    assert not ok and schluessel(why) == "srv.notoken"

    a.cfg["auth_mode"] = "login"
    ok, _ = a.launch(outlook=True, label="job.export")
    assert ok, "Login-Modus verlangt weiterhin einen Schlüssel"


# --------------------------------------------------------------------------
# Verlauf: ein Treffer allein sagt oft zu wenig
# --------------------------------------------------------------------------
def test_http_thread_reicht_die_auswertung_durch(server, monkeypatch):
    a, port = server

    class FakeSuche:
        STATE = {"semantic": False}

        @staticmethod
        def get_thread(thread, limit=50):
            return {"thread": thread, "count": 2, "limit": limit,
                    "messages": [{"uid": "u1"}, {"uid": "u2"}]}

    monkeypatch.setattr(a.search, "ensure", lambda cfg: FakeSuche)
    code, r = call(port, "GET", "/api/thread?key=tix:abc")
    assert code == 200 and r["count"] == 2 and r["thread"] == "tix:abc"

    # Eine überzogene Grenze darf nicht die halbe Datenbank holen.
    assert call(port, "GET", "/api/thread?key=x&limit=9999")[1]["limit"] == 200


def test_http_thread_ohne_index(server, monkeypatch):
    a, port = server
    monkeypatch.setattr(a.search, "ensure", lambda cfg: None)
    a.search.error = {"k": "cal.missing", "v": {}}
    code, r = call(port, "GET", "/api/thread?key=x")
    assert code == 200 and r["messages"] == [] and r["error"]


PRUEFUNG_VERLAUF = GRUNDZUSTAND + """
KANN_VERLAUF = true;      // wird sonst aus store.features gesetzt
// Ein Treffer mit Gespraechskennung bietet den Verlauf an, einer ohne nicht.
global.fetch = function(pfad){
  return Promise.resolve({json: function(){
    return Promise.resolve(String(pfad).indexOf('/api/thread') === 0
      ? global.ANTWORT : statusGeruest());
  }});
};
renderHits({results: [
  {uid: 'a', title: 'Frage', who: 'Alice', date: '2025-06-01', source_label: 'Mail',
   preview: 'Text', uri: 'o365://outlook/a.eml', thread: 'tix:abc'},
  {uid: 'b', title: 'Einzeln', who: 'Bob', date: '2025-06-02', source_label: 'Mail',
   preview: 'Text', uri: 'o365://outlook/b.eml', thread: null}
], count: 2, backend: 'bm25'});
var html = document.getElementById('results').innerHTML;
pruefe(html.indexOf('zeigeVerlauf(1') >= 0, 'Kein Verlauf beim ersten Treffer');
pruefe(html.indexOf('zeigeVerlauf(2') < 0, 'Verlauf ohne Gespraech angeboten');

// Aufklappen holt die Nachrichten und zeigt sie chronologisch untereinander.
global.ANTWORT = {count: 3, messages: [
  {date: '2025-06-01', who: 'Alice', title: 'Frage', uri: 'o365://outlook/a.eml'},
  {date: '2025-06-02', who: 'Bob', title: 'RE: Frage', uri: 'o365://outlook/b.eml'},
  {date: '2025-06-03', who: 'Alice', title: 'AW: Frage', uri: 'o365://outlook/c.eml'}]};
zeigeVerlauf(1, 'tix:abc');
// Das Nachladen laeuft ueber ein Promise – erst danach steht der Kasten.
setTimeout(function(){
  var kasten = document.getElementById('verlauf-1').innerHTML;
  pruefe(kasten.indexOf('3 Nachrichten') >= 0, 'Anzahl fehlt: ' + kasten.slice(0, 120));
  pruefe(kasten.indexOf('RE: Frage') >= 0, 'Antwort fehlt im Verlauf');
  pruefe(kasten.indexOf('2025-06-01') < kasten.indexOf('2025-06-03'),
         'Verlauf steht nicht in zeitlicher Reihenfolge');
  console.log('OK');
}, 20);
"""


def test_verlauf_klappt_unter_dem_treffer_auf():
    _in_node(PRUEFUNG_VERLAUF)


PRUEFUNG_GELOESCHT = GRUNDZUSTAND + """
// Gelöschtes ist am Treffer erkennbar, ohne die Liste zu erschlagen.
renderHits({results: [
  {uid: 'a', title: 'Weg', who: 'Alice', date: '2025-06-01', source_label: 'Mail',
   preview: 'Text', uri: 'o365://outlook/a.eml', gone: '2026-03-12T09:00:00'},
  {uid: 'b', title: 'Da', who: 'Bob', date: '2025-06-02', source_label: 'Mail',
   preview: 'Text', uri: 'o365://outlook/b.eml', gone: null}
], count: 2, backend: 'bm25'});
var html = document.getElementById('results').innerHTML;
var marken = html.split('tag weg').length - 1;
pruefe(marken === 1, 'Erwartet genau eine Markierung, gezaehlt: ' + marken);
pruefe(html.indexOf('12.03.26') >= 0, 'Der Zeitpunkt fehlt im Tooltip: ' + html.slice(0,300));
console.log('OK');
"""


def test_geloeschtes_ist_am_treffer_erkennbar():
    _in_node(PRUEFUNG_GELOESCHT)


def test_http_search_reicht_den_filter_durch(server, monkeypatch):
    a, port = server
    gesehen = {}

    class FakeSuche:
        STATE = {"semantic": False}

        @staticmethod
        def browse_messages(**kw):
            gesehen.update(kw)
            return {"count": 0, "results": []}

    monkeypatch.setattr(a.search, "ensure", lambda cfg: FakeSuche)
    call(port, "GET", "/api/search?gone=1")
    assert gesehen["only_gone"] is True
    call(port, "GET", "/api/search")
    assert gesehen["only_gone"] is False


PRUEFUNG_ALTER_INDEX = GRUNDZUSTAND + """
// Ein Index aus einer aelteren Fassung kennt Verlauf und Loeschungen nicht.
// Dann bietet die Oberflaeche sie gar nicht erst an, statt in einen Fehler
// laufen zu lassen.
var st = statusGeruest();
st.store.features = [];
renderStatus(st);
pruefe(document.getElementById('chip-geloescht').classList.contains('hide'),
       'Sicht wird trotz altem Index angeboten');
pruefe(KANN_VERLAUF === false, 'Verlauf gilt trotz altem Index als moeglich');

st.store.features = ['gone', 'thread'];
renderStatus(st);
pruefe(!document.getElementById('chip-geloescht').classList.contains('hide'),
       'Filter fehlt trotz passendem Index');
pruefe(KANN_VERLAUF === true, 'Verlauf fehlt trotz passendem Index');
console.log('OK');
"""


def test_alter_index_bietet_die_neuen_filter_nicht_an():
    _in_node(PRUEFUNG_ALTER_INDEX)


# --------------------------------------------------------------------------
# Quelldateien: was in den Browser gehört und was ins Programm daneben
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name,inhalt,ctype", [
    ("mail.eml", b"From: a@b.c\nSubject: X\n\nText\n", "message/rfc822"),
    ("termin.ics", b"BEGIN:VCALENDAR\nEND:VCALENDAR\n", "text/calendar; charset=utf-8"),
    ("alice.vcf", b"BEGIN:VCARD\nEND:VCARD\n", "text/vcard; charset=utf-8"),
])
def test_quelldatei_wird_heruntergeladen(sandbox, monkeypatch, name, inhalt, ctype):
    """Eine .eml als roher Text im Browserfenster ist für niemanden zu
    gebrauchen – im Mailprogramm dagegen eine Mail mit Anhängen."""
    ordner = sandbox / "outlook_export"
    ordner.mkdir()
    (ordner / name).write_bytes(inhalt)

    class FakeSuche:
        STATE = {}

        @staticmethod
        def _resolve_source(root, pfad):
            return (ordner / pfad), None

    a = app_mod.App(app_mod.load_config())
    monkeypatch.setattr(a.search, "ensure", lambda cfg: FakeSuche)
    httpd = app_mod.make_server(a, 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        con = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
        con.request("GET", f"/source?root=outlook&path={name}")
        resp = con.getresponse()
        koerper = resp.read()
        assert resp.status == 200 and koerper == inhalt
        assert resp.getheader("Content-Type") == ctype
        disp = resp.getheader("Content-Disposition")
        assert disp == f'attachment; filename="{name}"', disp
        con.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.parametrize("roh,erwartet", [
    ('a"b.eml', "a_b.eml"),
    ("x\r\ny.eml", "x__y.eml"),
    ("../../etc/passwd", "passwd"),
    ("", "datei"),
    ("normal.eml", "normal.eml"),
])
def test_dateiname_im_header_ist_unbedenklich(roh, erwartet):
    """Der Name landet in einem Header – ein Anführungszeichen oder ein
    Zeilenumbruch darin liesse ihn aufbrechen."""
    assert app_mod._sicherer_name(roh) == erwartet


# --------------------------------------------------------------------------
# Treffer je Seite
# --------------------------------------------------------------------------
def test_http_trefferzahl_wird_begrenzt(server, monkeypatch):
    a, port = server
    gesehen = {}

    class FakeSuche:
        STATE = {"semantic": False}

        @staticmethod
        def browse_messages(**kw):
            gesehen.update(kw)
            return {"count": 0, "results": []}

    monkeypatch.setattr(a.search, "ensure", lambda cfg: FakeSuche)
    call(port, "GET", "/api/search?k=50")
    assert gesehen["k"] == 50
    call(port, "GET", "/api/search?k=99999")     # nicht die halbe Datenbank
    assert gesehen["k"] == 100


@pytest.mark.parametrize("wert,erwartet", [
    (50, 50), (5, 5), (100, 100),
    (1, 5), (500, 100),          # außerhalb: auf den Rand gezogen
    ("quatsch", 20),             # unbrauchbar: Vorgabe bleibt
])
def test_config_trefferzahl(server, wert, erwartet):
    a, port = server
    call(port, "POST", "/api/config", {"search_results": wert})
    assert a.cfg["search_results"] == erwartet


PRUEFUNG_SEITENGROESSE = GRUNDZUSTAND + """
KANN_VERLAUF = false;
var gefragt = [];
global.fetch = function(pfad){
  gefragt.push(String(pfad));
  return Promise.resolve({json: function(){
    return Promise.resolve(String(pfad).indexOf('/api/search') === 0
      ? {results: [], count: 0, backend: 'bm25'} : statusGeruest());
  }});
};
S.config = {search_results: 50};
doSearch(0);
pruefe(gefragt[0].indexOf('k=50') >= 0, 'Einstellung wirkt nicht: ' + gefragt[0]);

// Und das Blaettern springt genauso weit – sonst uebersprungen oder doppelt.
var treffer = [];
for(var i = 0; i < 50; i++) treffer.push({uid: 'u' + i, title: 'T', who: 'W',
  date: '2025-06-01', source_label: 'Mail', preview: 'x', uri: 'o365://outlook/a.eml'});
offset = 50;
renderHits({results: treffer, count: 50, backend: 'bm25'});
var pager = document.getElementById('pager').innerHTML;
pruefe(pager.indexOf('doSearch(0)') >= 0, 'Zurueck springt falsch: ' + pager);
pruefe(pager.indexOf('doSearch(100)') >= 0, 'Weiter springt falsch: ' + pager);

// Ohne Angabe bleibt es bei 20.
S.config = {};
pruefe(trefferProSeite() === 20, 'Vorgabe ist nicht 20');
console.log('OK');
"""


def test_seitengroesse_wirkt_auf_suche_und_blaettern():
    _in_node(PRUEFUNG_SEITENGROESSE)


# --------------------------------------------------------------------------
# Datenordner umhängen
#
# Er lässt sich nicht in app_config.json einstellen – die Datei liegt selbst
# darin. Deshalb ein Zeiger am Standardort.
# --------------------------------------------------------------------------
@pytest.fixture
def standardort(tmp_path, monkeypatch):
    """standard_data_dir() in den Sandkasten biegen."""
    ort = tmp_path / "standard"
    ort.mkdir()
    monkeypatch.setattr(app_mod, "standard_data_dir", lambda: ort)
    for _n in ("MUNIMENTUM_DATA_DIR", "OFFICE365_DATA_DIR"):
        monkeypatch.delenv(_n, raising=False)
    return ort


def test_ohne_zeiger_gilt_der_standardort(standardort):
    assert app_mod.data_dir() == standardort


def test_zeiger_biegt_den_ordner_um(standardort, tmp_path):
    woanders = tmp_path / "platte"
    woanders.mkdir()
    app_mod.schreibe_zeiger(woanders)
    assert app_mod.data_dir() == woanders.resolve()


def test_zeiger_ins_leere_haelt_die_app_nicht_auf(standardort, tmp_path):
    """Externe Platte ab: dann eben wieder der Standardort, statt gar nicht
    zu starten."""
    (standardort / app_mod.ZEIGER_DATEI).write_text(
        str(tmp_path / "gibtsnicht"), encoding="utf-8")
    assert app_mod.data_dir() == standardort


def test_umgebung_schlaegt_den_zeiger(standardort, tmp_path, monkeypatch):
    app_mod.schreibe_zeiger(tmp_path)
    anders = tmp_path / "env"
    anders.mkdir()
    monkeypatch.setenv("MUNIMENTUM_DATA_DIR", str(anders))
    assert app_mod.data_dir() == anders.resolve()


def test_zeiger_auf_den_standardort_wird_geloescht(standardort, tmp_path):
    """Sonst bliebe eine Datei liegen, die nichts mehr aussagt."""
    app_mod.schreibe_zeiger(tmp_path)
    assert app_mod.zeiger_datei().exists()
    app_mod.schreibe_zeiger(standardort)
    assert not app_mod.zeiger_datei().exists()


def test_ordner_ohne_schreibrecht_wird_abgelehnt(tmp_path):
    """Lieber jetzt ablehnen als beim nächsten Start: die Einstellung, mit der
    man es zurücknähme, läge genau dort."""
    gesperrt = tmp_path / "gesperrt"
    gesperrt.mkdir()
    gesperrt.chmod(0o500)
    try:
        ziel, fehler = app_mod.pruefe_datenordner(gesperrt / "unten")
        assert ziel is None and fehler
    finally:
        gesperrt.chmod(0o700)


def test_leerer_ordner_wird_abgelehnt():
    assert app_mod.pruefe_datenordner("  ")[0] is None


def test_ordner_wird_angelegt(tmp_path):
    ziel, fehler = app_mod.pruefe_datenordner(tmp_path / "neu" / "tiefer")
    assert fehler is None and ziel.is_dir()
    assert not (ziel / ".schreibprobe").exists(), "Probe blieb liegen"


def test_http_datenordner_setzen(server, standardort, tmp_path):
    a, port = server
    ziel = tmp_path / "extern"
    code, r = call(port, "POST", "/api/data-dir", {"path": str(ziel)})
    assert code == 200 and r["ok"] and r["restart"] is True
    assert app_mod.lies_zeiger() == ziel.resolve()
    # Die App hängt sich NICHT im Betrieb um – BASE geht als Arbeitsverzeichnis
    # an jeden Unterprozess, womöglich mitten in einem Export.
    assert call(port, "GET", "/api/status")[1]["data_dir"] != str(ziel)


def test_http_datenordner_ablehnen(server, standardort):
    _, port = server
    code, r = call(port, "POST", "/api/data-dir", {"path": ""})
    assert code == 400 and not r["ok"]
    assert app_mod.lies_zeiger() is None, "kaputter Wert wurde trotzdem gemerkt"


def test_vorhandener_ordner_ohne_schreibrecht_wird_abgelehnt(tmp_path):
    """Den Fall deckt erst die Schreibprobe ab: mkdir(exist_ok=True) gelingt
    bei einem vorhandenen Ordner auch dann, wenn niemand darin schreiben darf.
    Ohne die Probe zeigte der Zeiger dorthin und die App könnte beim nächsten
    Start nichts mehr speichern."""
    gesperrt = tmp_path / "nur_lesen"
    gesperrt.mkdir()
    gesperrt.chmod(0o500)
    try:
        ziel, fehler = app_mod.pruefe_datenordner(gesperrt)
        assert ziel is None and fehler, "Ordner ohne Schreibrecht durchgewinkt"
    finally:
        gesperrt.chmod(0o700)


# --------------------------------------------------------------------------
# Die Antwort spricht die Sprache der Oberfläche
#
# Sie wird genauso ausgehandelt wie die Seite selbst: Einstellung schlägt
# Browsersprache. Ohne Test bräche das lautlos – die Antwort käme weiterhin,
# nur eben auf Deutsch für jemanden, der Französisch liest.
# --------------------------------------------------------------------------
def _antwort_sprache(server, monkeypatch, accept=None, eingestellt="auto"):
    a, port = server
    a.cfg["language"] = eingestellt
    gesehen = {}

    class FakeSuche:
        STATE = {"semantic": True}

        @staticmethod
        def search_messages(**kw):
            return {"results": [{"uid": "u1", "title": "T", "who": "W",
                                 "date": "2025-06-01", "uri": "o365://outlook/a.eml"}]}

        @staticmethod
        def get_document(uid):
            return {"text": "Inhalt"}

    monkeypatch.setattr(a.search, "ensure", lambda cfg: FakeSuche)
    monkeypatch.setattr(a, "ollama", lambda: {"running": True, "has_chat_model": True})

    def fake_stream(query, quellen, model, ollama, lang="de", *rest, **kw):
        gesehen["lang"] = lang
        yield {"text": "ok"}

    monkeypatch.setattr(app_mod.answer, "stream", fake_stream)

    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    kopf = {"Content-Type": "application/json"}
    if accept:
        kopf["Accept-Language"] = accept
    con.request("POST", "/api/answer", json.dumps({"q": "Frage"}), kopf)
    con.getresponse().read()
    con.close()
    return gesehen.get("lang")


@pytest.mark.parametrize("accept,erwartet", [
    ("fr-CH,fr;q=0.9", "fr"),
    ("en-US,en;q=0.9", "en"),
    ("de-DE,de;q=0.9", "de"),
    (None, "de"),                      # ohne Angabe die Vorgabe
    ("kl-GL", "de"),                   # unbekannt: nicht raten
])
def test_antwort_folgt_der_browsersprache(server, monkeypatch, accept, erwartet):
    assert _antwort_sprache(server, monkeypatch, accept) == erwartet


def test_eingestellte_sprache_schlaegt_den_browser(server, monkeypatch):
    """Wer die Oberfläche auf Französisch stellt, will die Antwort nicht auf
    Englisch, nur weil der Browser das meldet."""
    assert _antwort_sprache(server, monkeypatch, "en-US,en;q=0.9", "fr") == "fr"


def test_prompt_verlangt_die_passende_sprache():
    """Die Regel steht in derselben Sprache wie die gewünschte Antwort – ein
    kleines Modell folgt ihr dann zuverlässiger."""
    import answer
    assert "Deutsch" in answer.system_prompt("de")
    assert "English" in answer.system_prompt("en")
    assert "français" in answer.system_prompt("fr")


# --------------------------------------------------------------------------
# Jeder ID-Selektor muss ein Element treffen
#
# Aus der Praxis gemeldet: „Monat“ und „Rekonstruiert“ im Kalender liessen sich
# nicht mehr klicken. Ursache war der Reiter-Umbau – aus #tab-kalender wurde
# #sicht-kalender, aber zwei querySelectorAll blieben auf dem alten Namen. Die
# Auswahl traf nichts, es wurde nie ein Klick-Empfänger gesetzt, und „Woche“ sah
# nur deshalb aktiv aus, weil die Klasse im Markup steht. Kein Fehler in der
# Konsole, keine Meldung: der Knopf tat einfach nichts.
# --------------------------------------------------------------------------
def test_jeder_id_selektor_trifft_ein_element():
    quelle = Path(app_mod.__file__).read_text(encoding="utf-8")
    # Alle im JavaScript benutzten '#id'-Selektoren. Nur die vollständig
    # ausgeschriebenen: '#cat-' + name wird erst zur Laufzeit fertig, dazu
    # liesse sich hier nichts sagen.
    selektoren = set(re.findall(
        r"""querySelector(?:All)?\('#([\w-]+)[^']*'\s*\)""", quelle))
    # el('x') ist der häufigere Zugriff und war bisher nicht geprüft. Genau
    # dort ist es passiert: nach dem Umbau der Suchmaske zeigte el('search-sub')
    # auf ein Element, das es nicht mehr gab. Im Browser wirft das, und
    # renderStatus bricht mitten im Aufbau ab – die DOM-Attrappe der Tests legt
    # dagegen jede ID auf Anfrage an und merkte nichts.
    selektoren |= set(re.findall(r"""\bel\('([\w-]+)'\)""", quelle))
    assert selektoren, "keine ID-Selektoren gefunden – Muster kaputt?"
    # … gegen die IDs im Markup.
    vorhanden = set(re.findall(r'id="([\w-]+)"', quelle))
    fehlt = sorted(selektoren - vorhanden)
    # Statisch geprüft und nicht im DOM-Stummel nachgestellt: der müsste dafür
    # echtes Markup parsen, und diese Prüfung deckt ohnehin jeden Selektor der
    # Seite ab statt nur die drei Kalenderknöpfe.
    assert not fehlt, (
        f"Selektor trifft kein Element: {fehlt}. Der zugehörige Knopf tut dann "
        f"nichts, ohne dass irgendwo ein Fehler auftaucht.")


def test_seite_bringt_ihr_eigenes_symbol_mit():
    """Ohne das holt sich jeder Browser ein 404 auf /favicon.ico ab – und im
    Bündel gäbe es keine Datei, die man stattdessen ausliefern könnte."""
    seite = app_mod.PAGE
    assert 'rel="icon"' in seite
    assert "data:image/svg+xml" in seite, "Symbol als Datei statt eingebettet"


# --------------------------------------------------------------------------
# MCP-Eintrag: kopieren, und die Pfade folgen dem Datenordner
# --------------------------------------------------------------------------
def test_mcp_eintrag_folgt_dem_datenordner(sandbox, monkeypatch, tmp_path):
    """Der Eintrag trägt absolute Pfade – Claude startet ihn in einem
    unbekannten Arbeitsverzeichnis. Sie müssen also mitwandern."""
    for ordner in (tmp_path / "platte-a", tmp_path / "platte-b"):
        app_mod.set_data_dir(ordner)
        conf = app_mod.mcp_client_config(app_mod.load_config(), 8365)
        args = conf["stdio"]["mcpServers"]["munimentum"]["args"]
        genannt = args[args.index("--data-dir") + 1]
        assert Path(genannt).is_absolute()
        assert Path(genannt) == ordner.resolve(), args


def test_mcp_programmpfad_folgt_dem_datenordner_nicht(sandbox, tmp_path):
    """Das Programm liegt, wo es liegt – nur die Daten wandern."""
    app_mod.set_data_dir(tmp_path / "woanders")
    conf = app_mod.mcp_client_config(app_mod.load_config(), 8365)
    eintrag = conf["stdio"]["mcpServers"]["munimentum"]
    alles = " ".join([eintrag["command"], *eintrag["args"]])
    assert "mcp_server" in alles
    assert str(tmp_path / "woanders") not in alles.split("--data-dir")[0]


PRUEFUNG_KOPIEREN = GRUNDZUSTAND + """
var kopiert = [];
// node bringt ein eigenes navigator mit, und zwar nur mit Getter - eine
// schlichte Zuweisung liefe ins Leere.
var zwischenablage = {writeText: function(t){
  kopiert.push(t); return Promise.resolve();
}};
Object.defineProperty(global, 'navigator',
  {value: {clipboard: zwischenablage}, configurable: true, writable: true});
var kasten = document.getElementById('mcp-json');
kasten.textContent = '{"mcpServers": {}}';
var knopf = {textContent: 'Kopieren'};
kopiere('mcp-json', knopf);
pruefe(kopiert.length === 1, 'Nichts kopiert');
pruefe(kopiert[0] === '{"mcpServers": {}}', 'Falscher Inhalt: ' + kopiert[0]);

setTimeout(function(){
  pruefe(knopf.textContent === 'Kopiert', 'Keine Rueckmeldung: ' + knopf.textContent);

  // Schlaegt die Zwischenablage fehl, wird der alte Weg versucht statt still
  // nichts zu tun.
  var versucht = false;
  zwischenablage.writeText = function(){ return Promise.reject(new Error('nein')); };
  document.execCommand = function(){ versucht = true; return true; };
  kopiere('mcp-json', knopf);
  setTimeout(function(){
    pruefe(versucht, 'Kein Rueckfall auf den alten Weg');
    console.log('OK');
  }, 20);
}, 20);
"""


def test_mcp_eintrag_laesst_sich_kopieren():
    _in_node(PRUEFUNG_KOPIEREN)


# --------------------------------------------------------------------------
# Adressbuch: zwei Quellen für dieselbe Frage „wer ist das?“
# --------------------------------------------------------------------------
PRUEFUNG_ADRESSBUCH = GRUNDZUSTAND + """
contacts = [
  {title: 'Alice Example', org: 'Contoso', em: ['a@contoso.test'], tel: ['+49 1'],
   src: 'kontakte', root: 'outlook', rel: 'kontakte/a.vcf'},
  {title: 'Nur im Buch', org: 'Fabrikam', em: [], tel: [],
   src: 'kontakte', root: 'outlook', rel: 'kontakte/n.vcf'}
];
personen = [{name: 'Alice Example', messages: 42},
            {name: 'Bob Ausdemchat', messages: 7}];
personenGeladen = true;

function namen(){
  return (document.getElementById('kbBox').innerHTML.match(/Alice Example|Nur im Buch|Bob Ausdemchat/g) || [])
    .filter(function(v, i, a){ return a.indexOf(v) === i; }).sort();
}

bookF = 'all'; drawBook();
pruefe(namen().join(',') === 'Alice Example,Bob Ausdemchat,Nur im Buch',
       'Alle: ' + namen());
// Wer in beidem vorkommt, erscheint EINMAL – sonst stuende Alice doppelt da.
var html = document.getElementById('kbBox').innerHTML;
pruefe(html.split('Alice Example').length - 1 <= 2, 'Alice mehrfach im Buch');
pruefe(html.indexOf('42') >= 0, 'Nachrichtenzahl fehlt bei Alice');

bookF = 'contacts'; drawBook();
pruefe(namen().join(',') === 'Alice Example,Nur im Buch', 'Kontakte: ' + namen());

bookF = 'comm'; drawBook();
pruefe(namen().join(',') === 'Alice Example,Bob Ausdemchat', 'Kommunikation: ' + namen());
// Nur wer ausschliesslich aus der Kommunikation kommt, wird als solcher markiert.
var nurKomm = document.getElementById('kbBox').innerHTML;
pruefe(nurKomm.indexOf('herkunft') >= 0, 'Herkunft nicht gekennzeichnet');

// Klick auf eine Person fuehrt zu ihrer Kommunikation.
global.gesucht = [];
doSearch = function(off){ gesucht.push([document.getElementById('f-person').value, off]); };
zeigeKommunikation('Bob Ausdemchat');
pruefe(gesucht.length === 1 && gesucht[0][0] === 'Bob Ausdemchat',
       'Personenfilter nicht gesetzt: ' + JSON.stringify(gesucht));
pruefe(offeneSicht === 'treffer', 'Nicht zur Trefferliste gewechselt');
console.log('OK');
"""


def test_adressbuch_trennt_die_beiden_quellen():
    _in_node(PRUEFUNG_ADRESSBUCH)


# --------------------------------------------------------------------------
# Analytics: was im Archiv steckt – und was fehlt
# --------------------------------------------------------------------------
def _analytics_db(sandbox, spalten_neu=True):
    store = sandbox / "rag_store"
    store.mkdir(exist_ok=True)
    app_mod._ZAEHLUNG.clear()
    app_mod._GROESSE.clear()
    con = sqlite3.connect(store / "corpus.db")
    extra = ", thread TEXT, gone TEXT, att TEXT" if spalten_neu else ""
    con.execute(f"CREATE TABLE chunks(uid TEXT, seq INTEGER, src TEXT, ts REAL{extra})")
    con.execute("CREATE TABLE people(src TEXT, who TEXT, messages INTEGER, ppl TEXT)")
    werte = [("outlook:a:0", 0, "outlook", 1_000_000),
             ("outlook:a:0", 1, "outlook", 1_000_000),
             ("outlook:b:0", 0, "outlook", 2_000_000),
             ("teams:c:0", 0, "teams", 1_500_000)]
    if spalten_neu:
        con.executemany("INSERT INTO chunks VALUES (?,?,?,?,?,?,?)",
                        [(*w, "tix:1" if i < 3 else "chat:x",
                          "2026-01-01" if i == 2 else None,
                          "Vertrag.pdf" if i == 0 else "")
                         for i, w in enumerate(werte)])
    else:
        con.executemany("INSERT INTO chunks VALUES (?,?,?,?)", werte)
    con.executemany("INSERT INTO people VALUES (?,?,?,?)",
                    [("outlook", "Alice", 3, ""), ("teams", "Bob", 1, "")])
    con.commit()
    con.close()
    return store


def test_kennzahlen_zaehlen_nachrichten_nicht_textstellen(sandbox):
    _analytics_db(sandbox)
    k = app_mod.kennzahlen(app_mod.load_config())
    assert k["exists"] and k["nachrichten"] == 3        # nicht 4 Textstellen
    assert {q["src"]: q["nachrichten"] for q in k["quellen"]} == {"outlook": 2, "teams": 1}
    assert k["personen"] == 2
    assert k["gespraeche"] == 2
    assert k["mit_anhang"] == 1
    assert k["verschwunden"] == 1
    assert k["von"] == 1_000_000 and k["bis"] == 2_000_000


def test_kennzahlen_sagen_weiss_ich_nicht_statt_null(sandbox):
    """Ein Index aus einer älteren Fassung kennt die Spalten nicht. „0 mit
    Anhang“ wäre eine Behauptung, None ist eine Auskunft."""
    _analytics_db(sandbox, spalten_neu=False)
    k = app_mod.kennzahlen(app_mod.load_config())
    assert k["nachrichten"] == 3                        # das geht weiterhin
    assert k["gespraeche"] is None
    assert k["mit_anhang"] is None
    assert k["verschwunden"] is None


def test_kennzahlen_ohne_index(sandbox):
    k = app_mod.kennzahlen(app_mod.load_config())
    assert k["exists"] is False and k["nachrichten"] == 0


def test_ordnergroesse_wird_gepuffert(sandbox):
    ordner = sandbox / "gross"
    ordner.mkdir()
    (ordner / "a.bin").write_bytes(b"x" * 1000)
    app_mod._GROESSE.clear()
    assert app_mod.ordner_groesse(ordner) == 1000
    (ordner / "b.bin").write_bytes(b"y" * 500)
    assert app_mod.ordner_groesse(ordner) == 1000, "Puffer greift nicht"
    assert app_mod.ordner_groesse(ordner, ttl=0) == 1500


def test_ordnergroesse_ohne_ordner(sandbox):
    assert app_mod.ordner_groesse(sandbox / "gibtsnicht") == 0


def test_pruefschritt_braucht_einen_zugang(sandbox, no_ollama, monkeypatch):
    """Die Prüfung fragt das Postfach ab – ohne Zugang darf sie nicht starten."""
    a = app_mod.App()
    monkeypatch.setattr(a.jobs, "start", lambda steps, label: True)
    monkeypatch.setattr(app_mod, "read_token", lambda *x, **kw: "")
    ok, why = a.launch(check=True, label="job.check")
    assert not ok and schluessel(why) == "srv.notoken"


def test_pruefschritt_ruft_outlook_mit_check(sandbox):
    steps = app_mod.build_steps(app_mod.load_config(), check=True)
    assert [s["key"] for s in steps] == ["check"]
    assert "--check" in steps[0]["argv"]


PRUEFUNG_ANALYTICS = GRUNDZUSTAND + """
zeigeAnalytics({exists: true, nachrichten: 238408,
  quellen: [{src: 'teams', nachrichten: 196668}, {src: 'outlook', nachrichten: 36827}],
  gespraeche: 16545, mit_anhang: null, personen: 3860, verschwunden: 12,
  von: 1568851200, bis: 1789603200,
  groesse: {teams: 3758096384, outlook: 28879134720, index: 966367641},
  vollstaendigkeit: {geprueft: '2026-08-10T20:00:00', erwartet: 40000,
    vorhanden: 39994, geloescht: 12, fehlt: 6, ausgelassen: 15000,
    ausgelassene_ordner: ['Archiv', 'Junk-E-Mail'],
    ordner: [{ordner: 'E-Mail/Gesendete Elemente', erwartet: 100, vorhanden: 97,
              geloescht: 0, fehlt: 3, ausgelassen: false},
             {ordner: 'E-Mail/Archiv', erwartet: 14000, vorhanden: 0,
              geloescht: 0, fehlt: 0, ausgelassen: true}]}});

var kpi = document.getElementById('ana-kpi').innerHTML;
pruefe(kpi.indexOf('238.408') >= 0, 'Nachrichtenzahl fehlt');
pruefe(kpi.indexOf('16.545') >= 0, 'Gespraeche fehlen');
// null heisst „weiss ich nicht“ – keinesfalls 0.
pruefe(kpi.indexOf('>0<') < 0, 'Unbekanntes wurde als 0 gezeigt');
pruefe(kpi.indexOf('–') >= 0, 'Unbekanntes nicht als Strich gezeigt');
pruefe(kpi.indexOf('GB') >= 0, 'Groesse fehlt: ' + kpi.slice(0, 200));

var pruef = document.getElementById('ana-check-box').innerHTML;
pruefe(pruef.indexOf('15.000') >= 0, 'Ausgelassene Ordner nicht erklaert');
pruefe(pruef.indexOf('Archiv') >= 0, 'Ausgelassene Ordner nicht benannt');
// Der ausgelassene Ordner darf NICHT in der Luecken-Tabelle stehen.
var tab = pruef.split('<tbody>')[1] || '';
pruefe(tab.indexOf('Gesendete Elemente') >= 0, 'Echte Luecke fehlt in der Tabelle');
pruefe(tab.indexOf('Archiv') < 0, 'Ausgelassener Ordner steht als Luecke da');
console.log('OK');
"""


def test_analytics_zeigt_kennzahlen_und_trennt_ausgelassenes():
    _in_node(PRUEFUNG_ANALYTICS)


# --------------------------------------------------------------------------
# Exportliste: was der nächste Lauf täte, ohne ihn zu starten
# --------------------------------------------------------------------------
def _baum(sandbox, cfg, eintraege, mails=()):
    ordner = sandbox / app_mod.OUTLOOK_DIR
    folders_mod.speichere(ordner, eintraege)
    for pfad, anzahl in mails:
        (ordner / pfad).mkdir(parents=True, exist_ok=True)
        for i in range(anzahl):
            (ordner / pfad / f"m{i}.eml").write_text("x", encoding="utf-8")
    return ordner


def test_exportliste_rechnet_mit_den_regeln_aus_dem_formular(server, sandbox):
    """Wer eine Regel tippt, will sie prüfen, bevor er sie speichert – die
    Vorschau darf nicht den zuletzt gespeicherten Stand zeigen."""
    a, port = server
    _baum(sandbox, a.cfg,
          [{"id": "1", "pfad": "E-Mail/Posteingang", "name": "P", "elemente": 100},
           {"id": "2", "pfad": "E-Mail/Archiv", "name": "A", "elemente": 14000}],
          [("E-Mail/Archiv", 3), ("E-Mail/Weg", 4)])

    code, r = call(port, "POST", "/api/folder-plan",
                   {"folder_rules": "- E-Mail/Archiv/**", "skip_folders": ""})
    assert code == 200 and r["ok"]
    assert [z["pfad"] for z in r["an"]] == ["E-Mail/Posteingang"]
    assert [z["pfad"] for z in r["aus"]] == ["E-Mail/Archiv"]
    assert r["aus"][0]["regel"] == "- E-Mail/Archiv/**"
    assert r["aus"][0]["archiv"] == 3            # ausgelassen heißt nicht leer
    assert r["weg"] == [{"pfad": "E-Mail/Weg", "archiv": 4}]
    # Gespeichert wurde nichts – die Vorschau ist eine Frage, keine Änderung.
    assert not a.cfg["folder_rules"]


def test_exportliste_faellt_auf_die_alte_namensliste_zurueck(server, sandbox):
    """Ohne Regeln gilt weiter, was in der Ordnerliste steht – genau wie im
    Export (outlook_export.aktuelle_regeln)."""
    a, port = server
    _baum(sandbox, a.cfg,
          [{"id": "1", "pfad": "E-Mail/Posteingang", "name": "P", "elemente": 1},
           {"id": "2", "pfad": "E-Mail/Archiv/Alt", "name": "Alt", "elemente": 2}])
    r = call(port, "POST", "/api/folder-plan",
             {"folder_rules": "", "skip_folders": "Archiv"})[1]
    assert [z["pfad"] for z in r["aus"]] == ["E-Mail/Archiv/Alt"]
    # Kleingeschrieben, weil case-insensitive verglichen wird – die angezeigte
    # Regel ist damit genau die, die auch entschieden hat.
    assert r["aus"][0]["regel"] == "- E-Mail/archiv/**"


def test_exportliste_ohne_abgeglichenen_baum(server):
    a, port = server
    code, r = call(port, "POST", "/api/folder-plan", {})
    assert code == 200 and r == {"ok": False, "leer": True}


# --------------------------------------------------------------------------
# Kalender: dieselbe Mechanik wie die Postfach-Ordner
# --------------------------------------------------------------------------
def _kalenderliste(sandbox, eintraege, termine=()):
    ordner = sandbox / app_mod.OUTLOOK_DIR
    folders_mod.speichere(ordner, eintraege, datei=folders_mod.KALENDER)
    for pfad, anzahl in termine:
        (ordner / pfad).mkdir(parents=True, exist_ok=True)
        for i in range(anzahl):
            (ordner / pfad / f"t{i}.ics").write_text("x", encoding="utf-8")
    return ordner


KALENDER = [{"id": "a", "pfad": "kalender/Privat", "name": "Privat", "standard": False,
             "elemente": 0},
            {"id": "b", "pfad": "kalender/Arbeit", "name": "Arbeit", "standard": True,
             "elemente": 0}]


def test_kalenderregeln_ohne_eintrag_nur_der_standard(sandbox):
    """Ein Postfach hat oft Geburtstage und fremde Freigaben – die hat niemand gemeint."""
    daten = {"ordner": KALENDER}
    regeln = app_mod.kalenderregeln({"calendar_rules": ""}, daten)
    assert [e["name"] for e in folders_mod.gewaehlt(daten, regeln)] == ["Arbeit"]
    eigene = app_mod.kalenderregeln({"calendar_rules": "- kalender/**\n+ kalender/Privat"}, daten)
    assert [e["name"] for e in folders_mod.gewaehlt(daten, eigene)] == ["Privat"]


def test_kalenderliste_rechnet_mit_den_regeln_aus_dem_formular(server, sandbox):
    a, port = server
    _kalenderliste(sandbox, KALENDER, [("kalender/Privat", 3), ("kalender/Weg", 2)])

    code, r = call(port, "POST", "/api/folder-plan",
                   {"quelle": "calendar", "calendar_rules": "- kalender/**\n+ kalender/Privat"})
    assert code == 200 and r["ok"]
    assert [z["pfad"] for z in r["an"]] == ["kalender/Privat"]
    assert [z["pfad"] for z in r["aus"]] == ["kalender/Arbeit"]
    # Gezählt wird, was auf der Platte liegt: Termine zählt Graph beim Auflisten nicht.
    assert r["an"][0]["archiv"] == 3
    assert r["weg"] == [{"pfad": "kalender/Weg", "archiv": 2}]
    assert not a.cfg["calendar_rules"]


def test_kalenderstand_nennt_die_gewaehlten_namen(server, sandbox):
    a, port = server
    _kalenderliste(sandbox, KALENDER)
    c = call(port, "GET", "/api/status")[1]["calendars"]
    assert (c["gesamt"], c["gewaehlt"], c["namen"]) == (2, 1, ["Arbeit"])
    assert c["abgeglichen"]


def test_kalenderstand_ohne_liste(server):
    c = call(server[1], "GET", "/api/status")[1]["calendars"]
    assert c == {"abgeglichen": None, "gesamt": 0, "gewaehlt": 0, "namen": [], "neu": []}


def test_kalenderregeln_werden_gespeichert_und_weitergereicht(server, sandbox):
    a, port = server
    call(port, "POST", "/api/config", {"calendar_rules": "kalender/Privat"})
    # Ohne Vorzeichen heißt einschließen – gespeichert wird die ausgeschriebene Regel.
    assert a.cfg["calendar_rules"] == "+ kalender/Privat"
    schritt = [s for s in app_mod.build_steps(a.cfg, outlook=True) if s["key"] == "outlook"][0]
    assert schritt["env"]["CALENDAR_RULES"] == "+ kalender/Privat"


def test_build_steps_kalenderabgleich(sandbox):
    steps = app_mod.build_steps(app_mod.load_config(), sync_calendars=True)
    assert [s["key"] for s in steps] == ["calendars"]
    assert "--calendars" in steps[0]["argv"]


def test_auswahlregeln_regeln_schlagen_die_namensliste():
    cfg = {"folder_rules": "+ E-Mail/Nur/**", "skip_folders": ["archiv"]}
    assert app_mod.auswahlregeln(cfg) == [(True, "E-Mail/Nur/**")]
    assert app_mod.auswahlregeln({"folder_rules": "", "skip_folders": ["archiv"]}) \
        == [(False, "E-Mail/archiv/**")]


# Die Exportliste im Browser: drei Gruppen, ein Filter – und ein Fenster, das
# der Statusabruf alle 2,5 Sekunden NICHT wegwischen darf.
PRUEFUNG_EXPORTLISTE = GRUNDZUSTAND + """
var gesendet = [];
global.fetch = function(pfad, opt){
  gesendet.push({pfad: String(pfad), body: opt && opt.body});
  if(String(pfad).indexOf('/api/folder-plan') < 0){
    return Promise.resolve({json: function(){ return Promise.resolve(statusGeruest()); }});
  }
  return Promise.resolve({json: function(){ return Promise.resolve({
    ok: true, abgeglichen: '2026-08-10T09:33:51+00:00',
    an:  [{pfad: 'E-Mail/Posteingang', elemente: 1, archiv: 1, regel: null},
          {pfad: 'E-Mail/Kunden/Beispiel AG', elemente: 240, archiv: 238,
           regel: '+ E-Mail/Kunden/Beispiel AG/**'}],
    aus: [{pfad: 'E-Mail/Archiv', elemente: 19650, archiv: 4711,
           regel: '- E-Mail/Archiv/**'}],
    weg: [{pfad: 'E-Mail/Alter Kunde', archiv: 812}],
    mails_an: 241, mails_aus: 19650, mails_weg: 812}); }});
};

document.getElementById('c-folder_rules').value = '- E-Mail/Archiv/**';
document.getElementById('c-skip_folders').value = 'junk';
zeigeExportliste();

setTimeout(function(){
  // Gefragt wird mit dem, was IM FELD steht - nicht mit dem Gespeicherten.
  var frage = gesendet.filter(function(g){ return g.pfad.indexOf('folder-plan') >= 0; })[0];
  pruefe(frage, 'Keine Anfrage an /api/folder-plan');
  pruefe(JSON.parse(frage.body).folder_rules === '- E-Mail/Archiv/**',
         'Regeln aus dem Feld nicht mitgeschickt: ' + frage.body);
  pruefe(JSON.parse(frage.body).skip_folders === 'junk', 'Ordnerliste fehlt');

  var h = document.getElementById('plan-listen').innerHTML;
  pruefe(h.indexOf('E-Mail/Posteingang') >= 0, 'Gewaehlter Ordner fehlt');
  pruefe(h.indexOf('E-Mail/Archiv') >= 0, 'Ausgelassener Ordner fehlt');
  pruefe(h.indexOf('E-Mail/Alter Kunde') >= 0, 'Nur-noch-im-Archiv fehlt');
  pruefe(h.indexOf('- E-Mail/Archiv/**') >= 0, 'Entscheidende Regel nicht genannt');
  pruefe(h.indexOf('19.650') >= 0, 'Zahlen nicht lesbar gruppiert');
  // Ausgelassen heisst nicht leer: der Bestand muss dabeistehen.
  pruefe(h.indexOf('4.711') >= 0, 'Bestand des ausgelassenen Ordners fehlt');
  // Beim Gewaehlten waere dieselbe Zahl nur Laerm.
  pruefe(h.indexOf('238') < 0 || h.indexOf('im Archiv') > 0, 'unerwartet');

  // Filter: nur noch die passenden Zeilen, die Gruppen bleiben stehen.
  document.getElementById('plan-filter').value = 'beispiel';
  planListen();
  h = document.getElementById('plan-listen').innerHTML;
  pruefe(h.indexOf('Beispiel AG') >= 0, 'Treffer weggefiltert');
  pruefe(h.indexOf('E-Mail/Posteingang') < 0, 'Filter greift nicht');
  pruefe(h.split('plangruppe').length - 1 === 3, 'Nicht mehr drei Gruppen');

  // Der Statusabruf darf das Fenster nicht ersetzen - es hat keine Kennung,
  // die er vergleichen koennte, und wuerde sonst im Sekundentakt verschwinden.
  var vorher = modal.innerHTML;
  renderStatus(Object.assign({}, statusGeruest(), {wizard: 'token'}));
  pruefe(modal.innerHTML === vorher, 'Statusabruf hat die Exportliste ueberschrieben');

  // ESC schliesst - und meldet dem Server KEINEN gesehenen Assistenten.
  var vorZahl = gesendet.length;
  taste('Escape');
  pruefe(wizardOffen === null, 'ESC schliesst nicht');
  pruefe(gesendet.slice(vorZahl).every(function(g){
           return g.pfad.indexOf('wizard-seen') < 0; }),
         'Eigenes Fenster als Assistent quittiert');
  console.log('OK');
}, 20);
"""


def test_exportliste_zeigt_drei_gruppen_und_ueberlebt_den_statusabruf():
    _in_node(PRUEFUNG_EXPORTLISTE)


# Kalender sind dieselbe Liste mit derselben Vorschau - nur zaehlt hier, was
# schon auf der Platte liegt, weil Graph beim Auflisten keine Termine zaehlt.
PRUEFUNG_KALENDERLISTE = GRUNDZUSTAND + """
var gesendet = [];
global.fetch = function(pfad, opt){
  gesendet.push({pfad: String(pfad), body: opt && opt.body});
  if(String(pfad).indexOf('/api/folder-plan') < 0){
    return Promise.resolve({json: function(){ return Promise.resolve(statusGeruest()); }});
  }
  return Promise.resolve({json: function(){ return Promise.resolve({
    ok: true, abgeglichen: '2026-08-10T09:33:51+00:00',
    an:  [{pfad: 'kalender/Arbeit', elemente: 0, archiv: 1200, regel: null}],
    aus: [{pfad: 'kalender/Geburtstage', elemente: 0, archiv: 0,
           regel: '- kalender/**'}],
    weg: [{pfad: 'kalender/Alt', archiv: 40}],
    mails_an: 0, mails_aus: 0, mails_weg: 40}); }});
};

// Der Stand in den Einstellungen nennt die Kalender beim Namen: bei einer
// Handvoll sagt das mehr als jede Zahl.
zeigeKalenderstand({abgeglichen: '2026-08-10T09:33:51+00:00', gesamt: 3,
                    gewaehlt: 1, namen: ['Arbeit'], neu: []});
var stand = document.getElementById('cal-state').textContent;
pruefe(stand.indexOf('Arbeit') >= 0, 'Kalendername fehlt: ' + stand);
pruefe(stand.indexOf('3') >= 0, 'Gesamtzahl fehlt: ' + stand);

document.getElementById('c-calendar_rules').value = '- kalender/**';
zeigeExportliste('calendar');

setTimeout(function(){
  var frage = gesendet.filter(function(g){ return g.pfad.indexOf('folder-plan') >= 0; })[0];
  pruefe(frage, 'Keine Anfrage an /api/folder-plan');
  var b = JSON.parse(frage.body);
  pruefe(b.quelle === 'calendar', 'Quelle nicht mitgeschickt: ' + frage.body);
  pruefe(b.calendar_rules === '- kalender/**', 'Regeln aus dem Feld nicht mitgeschickt');

  var h = document.getElementById('plan-listen').innerHTML;
  pruefe(h.indexOf('kalender/Arbeit') >= 0, 'Gewaehlter Kalender fehlt');
  pruefe(h.indexOf('kalender/Geburtstage') >= 0, 'Ausgelassener Kalender fehlt');
  pruefe(h.indexOf('kalender/Alt') >= 0, 'Nur-noch-im-Archiv fehlt');
  // Gezaehlt wird der Bestand, nicht die immer leere Elementzahl.
  pruefe(h.indexOf('1.200') >= 0, 'Bestand des Kalenders fehlt');
  pruefe(h.split('plangruppe').length - 1 === 3, 'Nicht drei Gruppen');
  console.log('OK');
}, 20);
"""


def test_kalenderliste_zeigt_bestand_statt_leerer_zahlen():
    _in_node(PRUEFUNG_KALENDERLISTE)


# Die Quelle vorne bestimmt, was hinten zur Wahl steht. Eine Auswahl mit einem
# einzigen Eintrag ist keine Auswahl - dann steht sie ausgegraut da.
PRUEFUNG_ORDNERAUSWAHL = GRUNDZUSTAND + r"""
S.store = {exists: true, built_at: '2026-08-12T10:00:00+00:00'};
var ORDNER = {
  outlook:  [{path:'E-Mail/Posteingang', messages:12480},
             {path:'E-Mail/Kunden', messages:8102}],
  kalender: [{path:'kalender/Arbeit', messages:4854},
             {path:'kalender/Privat', messages:912}],
  teams:    [{path:'1on1', messages:31204}, {path:'channels', messages:15302}],
  kontakte: [{path:'kontakte/Team', messages:64}]
};
var gefragt = [];
global.fetch = function(pfad){
  gefragt.push(String(pfad));
  var m = String(pfad).match(/source=(\w+)/);
  return Promise.resolve({json: function(){
    return Promise.resolve({folders: ORDNER[m ? m[1] : 'all'] || []}); }});
};

var feld = document.getElementById('f-folder');
function optionen(){
  return (feld.innerHTML.match(/<option[^>]*>[^<]*/g) || []).map(function(o){
    return o.replace(/^<option[^>]*>/, ''); });
}
function warte(schritte, fertig){
  if(schritte <= 0) return fertig();
  setTimeout(function(){ warte(schritte - 1, fertig); }, 0);
}
function waehle(quelle, dann){
  document.getElementById('f-source').value = quelle;
  ladeOrdner();
  warte(4, dann);
}

waehle('outlook', function(){
  pruefe(gefragt.some(function(p){ return p.indexOf('source=outlook') >= 0; }),
         'Quelle nicht mitgefragt: ' + gefragt.join(' '));
  pruefe(optionen().length === 3, 'Erwartet: Vorgabe + zwei Ordner, ist ' + optionen());
  pruefe(!feld.disabled, 'Zwei Ordner sind eine Wahl');
  feld.value = 'E-Mail/Kunden';

  waehle('kalender', function(){
    // Die Wahl von vorhin gibt es in dieser Quelle nicht - sie faellt weg,
    // sonst suchte man in einem Ordner, den diese Quelle nicht kennt.
    pruefe(feld.value === '', 'Unpassende Ordnerwahl blieb stehen: ' + feld.value);
    pruefe(optionen()[0] === 'Alle Kalender', 'Falsche Beschriftung: ' + optionen()[0]);

    waehle('teams', function(){
      var namen = optionen().map(function(o){ return o.split(' (')[0]; });
      pruefe(namen.indexOf('Kan\u00e4le') > 0, 'Kanaele nicht lesbar benannt: ' + namen);
      // Ohne das gezaehlte Wort im Namen: "1:1-Chats (89.273)" liest sich als
      // Zahl der Chats, gezaehlt werden aber die Nachrichten - wie ueberall
      // sonst in dieser Liste auch.
      pruefe(namen.indexOf('1:1') > 0, 'Chatart nicht lesbar benannt: ' + namen);
      pruefe(namen.join(' ').indexOf('Chats') < 0,
             'Name zaehlt etwas anderes als die Zahl: ' + namen);
      pruefe(namen[0] === 'Alle Chatarten', 'Falsche Beschriftung: ' + namen[0]);
      // Gefiltert wird weiter mit dem Ablagepfad, nicht mit dem Anzeigenamen.
      pruefe(feld.innerHTML.indexOf('value="channels"') >= 0, 'Falscher Filterwert');

      waehle('kontakte', function(){
        // Ein einziger Ordner filtert nichts weg. Ausgegraut sah das aus, als
        // sei etwas kaputt - also verschwindet das Feld.
        pruefe(feld.classList.contains('hide'),
               'Feld bleibt stehen, obwohl es nichts zu waehlen gibt');
        feld.value = 'kontakte/Team';   // von Hand gesetzt: darf nicht bleiben

        // Zweimal dieselbe Quelle fragt den Server nicht noch einmal.
        var vorher = gefragt.length;
        waehle('kontakte', function(){
          pruefe(gefragt.length === vorher, 'Ordnerliste ohne Not neu geholt');
          pruefe(feld.value === '', 'Versteckte Wahl filtert weiter mit');

          // Zurueck zu einer Quelle mit echter Auswahl: das Feld kommt wieder.
          waehle('kalender', function(){
            pruefe(!feld.classList.contains('hide'), 'Feld kommt nicht zurueck');
            console.log('OK');
          });
        });
      });
    });
  });
});
"""


def test_ordnerauswahl_folgt_der_quelle():
    _in_node(PRUEFUNG_ORDNERAUSWAHL)


# --------------------------------------------------------------------------
# OneDrive in der Oberfläche
# --------------------------------------------------------------------------
def test_onedrive_ist_aus_bis_jemand_es_einschaltet(sandbox):
    """Ein Laufwerk kann zweistellige Gigabyte haben – das zieht niemand
    versehentlich mit dem ersten Klick."""
    assert app_mod.load_config()["onedrive_enabled"] is False


def test_onedrive_schritt_bekommt_regeln_und_grenze(sandbox):
    cfg = app_mod.load_config()
    cfg["onedrive_rules"] = "- Dateien/Fotos/**"
    cfg["onedrive_max_mb"] = 50
    schritt = next(s for s in app_mod.build_steps(cfg, onedrive=True)
                   if s["key"] == "onedrive")
    assert "onedrive_export" in " ".join(str(a) for a in schritt["argv"])
    assert schritt["env"]["ONEDRIVE_RULES"] == "- Dateien/Fotos/**"
    assert schritt["env"]["ONEDRIVE_MAX_MB"] == "50"


def test_onedrive_regeln_werden_beim_speichern_normalisiert(server):
    a, port = server
    call(port, "POST", "/api/config",
         {"onedrive_rules": "Dateien/A\n\n# Kommentar\n- Dateien/B"})
    assert a.cfg["onedrive_rules"] == "+ Dateien/A\n- Dateien/B"


def test_onedrive_leerer_schalter_setzt_die_variable_trotzdem(sandbox):
    """Leer heißt „alles mitnehmen". Nicht gesetzt hieße „nimm, was in
    app_config.json steht" – und das Skript liefe anders als die App anzeigt."""
    schritt = next(s for s in app_mod.build_steps(app_mod.load_config(), onedrive=True)
                   if s["key"] == "onedrive")
    assert schritt["env"]["ONEDRIVE_RULES"] == ""
    assert schritt["env"]["ONEDRIVE_MAX_MB"] == "0"


def test_onedrive_braucht_einen_zugang(sandbox, no_ollama, monkeypatch):
    a = app_mod.App()
    monkeypatch.setattr(a.jobs, "start", lambda steps, label: True)
    monkeypatch.setattr(app_mod, "read_token", lambda *x, **kw: "")
    ok, why = a.launch(onedrive=True, label="job.export")
    assert not ok and schluessel(why) == "srv.notoken"


def test_index_sieht_den_onedrive_ordner(sandbox):
    schritt = next(s for s in app_mod.build_steps(app_mod.load_config(), index=True)
                   if s["key"] == "index")
    argv = [str(x) for x in schritt["argv"]]
    assert "onedrive_export" in argv, "der Index findet die Dateien sonst nicht"


PRUEFUNG_ONEDRIVE = GRUNDZUSTAND + """
var gesendet = [];
global.fetch = function(pfad, opt){
  gesendet.push({pfad: String(pfad), body: opt && opt.body});
  return Promise.resolve({json: function(){ return Promise.resolve(statusGeruest()); }});
};
var status = statusGeruest();
status.config.outlook_categories = ['mail'];
status.config.teams_categories = [];
status.config.onedrive_enabled = true;
// Die Auswahl wird NUR beim ersten Aufbau gesetzt - danach wuerde der Status
// alle 2,5 Sekunden ein gerade gesetztes Haekchen wieder wegnehmen.
S = null;
renderStatus(status);

pruefe(document.getElementById('c-onedrive_enabled').checked === true,
       'Haekchen nicht aus dem Status gesetzt');

// Genau das darf der naechste Statusabruf nicht rueckgaengig machen.
document.getElementById('c-onedrive_enabled').checked = false;
renderStatus(status);
pruefe(document.getElementById('c-onedrive_enabled').checked === false,
       'Statusabruf hat das Haekchen ueberschrieben');
document.getElementById('c-onedrive_enabled').checked = true;

// Der Lauf muss OneDrive mitschicken.
runExport();
var lauf = gesendet.filter(function(g){ return g.pfad.indexOf('/api/run') >= 0; })[0];
pruefe(lauf, 'Kein Lauf gestartet');
pruefe(JSON.parse(lauf.body).onedrive === true, 'OneDrive fehlt im Lauf: ' + lauf.body);

// Nur OneDrive, ohne Outlook und Teams: muss trotzdem starten.
gesendet = [];
document.querySelectorAll = function(){ return []; };   // keine Outlook-Haken mehr
var gemeckert = false;
global.alert = function(){ gemeckert = true; };
runExport();
pruefe(!gemeckert, 'Nur OneDrive wurde als "nichts gewaehlt" abgelehnt');
var nur = gesendet.filter(function(g){ return g.pfad.indexOf('/api/run') >= 0; })[0];
pruefe(JSON.parse(nur.body).onedrive === true && JSON.parse(nur.body).outlook === false,
       'Falscher Lauf: ' + nur.body);

// Gar nichts gewaehlt: kein Lauf.
gesendet = [];
document.getElementById('c-onedrive_enabled').checked = false;
gemeckert = false;
runExport();
pruefe(gemeckert, 'Ohne Auswahl wurde nicht gewarnt');
pruefe(gesendet.filter(function(g){ return g.pfad.indexOf('/api/run') >= 0; }).length === 0,
       'Ohne Auswahl trotzdem gestartet');
console.log('OK');
"""


def test_onedrive_haekchen_startet_den_lauf():
    _in_node(PRUEFUNG_ONEDRIVE)


def test_onedrive_abgleich_ist_ein_eigener_schritt(sandbox):
    schritt = next(s for s in app_mod.build_steps(app_mod.load_config(), sync_onedrive=True)
                   if s["key"] == "onedrive_folders")
    argv = [str(x) for x in schritt["argv"]]
    assert "--folders" in argv and "onedrive_export" in " ".join(argv)
    # Ohne Zugang darf er nicht starten – er fragt das Laufwerk ab.
    assert "ONEDRIVE_RULES" in schritt["env"]


def test_onedrive_abgleich_braucht_einen_zugang(sandbox, no_ollama, monkeypatch):
    a = app_mod.App()
    monkeypatch.setattr(a.jobs, "start", lambda steps, label: True)
    monkeypatch.setattr(app_mod, "read_token", lambda *x, **kw: "")
    ok, why = a.launch(sync_onedrive=True, label="job.folders")
    assert not ok and schluessel(why) == "srv.notoken"


def test_exportliste_kennt_beide_quellen(server, sandbox):
    """Dieselbe Auswertung, zwei Ordner – beim Postfach zählen die .eml, beim
    Spiegel alle Dateien."""
    a, port = server
    _baum(sandbox, a.cfg,
          [{"id": "1", "pfad": "Dateien/Kunden", "name": "Kunden", "elemente": 3}])
    od = sandbox / app_mod.ONEDRIVE_DIR
    folders_mod.speichere(od, [{"id": "1", "pfad": "Dateien/Kunden",
                                "name": "Kunden", "elemente": 3}])
    (od / "Dateien" / "Kunden").mkdir(parents=True, exist_ok=True)
    for name in ("a.pdf", "b.docx"):
        (od / "Dateien" / "Kunden" / name).write_text("x", encoding="utf-8")

    r = call(port, "POST", "/api/folder-plan",
             {"quelle": "onedrive", "onedrive_rules": ""})[1]
    assert r["ok"] and [z["pfad"] for z in r["an"]] == ["Dateien/Kunden"]
    assert r["an"][0]["archiv"] == 2, "beim Spiegel zählen alle Dateien, nicht nur .eml"

    r = call(port, "POST", "/api/folder-plan",
             {"quelle": "onedrive", "onedrive_rules": "- Dateien/Kunden/**"})[1]
    assert [z["pfad"] for z in r["aus"]] == ["Dateien/Kunden"]
    assert r["aus"][0]["regel"] == "- Dateien/Kunden/**"


PRUEFUNG_OD_ORDNER = GRUNDZUSTAND + """
var gesendet = [];
global.fetch = function(pfad, opt){
  gesendet.push({pfad: String(pfad), body: opt && opt.body});
  if(String(pfad).indexOf('/api/folder-plan') < 0)
    return Promise.resolve({json: function(){ return Promise.resolve(statusGeruest()); }});
  return Promise.resolve({json: function(){ return Promise.resolve({
    ok: true, abgeglichen: '2026-08-10T12:00:00',
    an: [{pfad: 'Dateien/Kunden', elemente: 3, archiv: 3, regel: null}],
    aus: [], weg: [], mails_an: 3, mails_aus: 0, mails_weg: 0}); }});
};
document.getElementById('c-onedrive_rules').value = '- Dateien/Fotos/**';

// Abgleich: eigener Lauf, eigene Meldung.
gleicheOrdnerAb('onedrive');
var lauf = gesendet.filter(function(g){ return g.pfad.indexOf('/api/run') >= 0; })[0];
pruefe(JSON.parse(lauf.body).sync_onedrive === true, 'Falscher Abgleich: ' + lauf.body);
pruefe(document.getElementById('od-folders-msg').textContent.length > 0,
       'Meldung steht nicht an der OneDrive-Karte');
pruefe(document.getElementById('folders-msg').textContent === '',
       'Meldung landete beim Postfach');

// Exportliste: mit den OneDrive-Regeln, nicht mit denen des Postfachs.
gesendet = [];
zeigeExportliste('onedrive');
setTimeout(function(){
  var frage = gesendet.filter(function(g){ return g.pfad.indexOf('folder-plan') >= 0; })[0];
  var b = JSON.parse(frage.body);
  pruefe(b.quelle === 'onedrive', 'Quelle fehlt: ' + frage.body);
  pruefe(b.onedrive_rules === '- Dateien/Fotos/**', 'Regeln aus dem falschen Feld');
  pruefe(b.folder_rules === undefined, 'Postfach-Regeln mitgeschickt');
  pruefe(document.getElementById('plan-listen').innerHTML.indexOf('Dateien/Kunden') >= 0,
         'Liste nicht gezeichnet');
  // Der Knopf im Fenster muss den OneDrive-Abgleich starten, nicht den anderen.
  pruefe(modal.innerHTML.indexOf('planAbgleichen(&quot;onedrive&quot;)') >= 0,
         'Abgleich im Fenster zeigt auf die falsche Quelle');

  // Beim Spiegel sind es Dateien. "Mails" waere schlicht falsch.
  var h = document.getElementById('plan-listen').innerHTML;
  pruefe(h.indexOf('Mails') < 0 && h.indexOf('mails') < 0,
         'Gruppenkopf spricht von Mails: ' + h.slice(0, 200));
  pruefe(h.indexOf('Dateien') >= 0, 'Gruppenkopf nennt die Einheit nicht');
  console.log('OK');
}, 20);
"""


def test_onedrive_ordnerknoepfe_wirken_auf_die_eigene_quelle():
    _in_node(PRUEFUNG_OD_ORDNER)


PRUEFUNG_VORABVERSION = GRUNDZUSTAND + """
function lage(u){
  // Beide Felder zuruecksetzen: der Browser ersetzt beim Setzen von innerHTML
  // auch den Text, die Attrappe hier nicht - sonst schleppte ein Fall den
  // Inhalt des vorigen mit.
  var b = document.getElementById('update-banner');
  b.textContent = ''; b.innerHTML = '';
  zeigeUpdate(Object.assign({current: '4.0.0', releases_url: 'https://r'}, u));
  return {text: document.getElementById('update-state').textContent, banner: b,
          text_inhalt: b.textContent, html_inhalt: b.innerHTML};
}

// 1) Eigene Version ist hoeher als das neueste Release.
var a = lage({status: 'ok', latest: '3.5.0', newer: false, ahead: true});
pruefe(a.text.indexOf('3.5.0') >= 0, 'Nennt die veroeffentlichte Version nicht: ' + a.text);
pruefe(a.text.toLowerCase().indexOf('latest version.') < 0,
       'Behauptet weiterhin "auf dem neuesten Stand": ' + a.text);
pruefe(!a.banner.classList.contains('hide'), 'Kein Hinweis eingeblendet');
pruefe(a.banner.classList.contains('warn'), 'Hinweis ist nicht als Warnung erkennbar');
pruefe(a.text_inhalt.length > 40, 'Hinweistext fehlt');

// 2) Normales Update: unveraendert, und KEINE Warnfarbe.
var b = lage({status: 'ok', latest: '5.0.0', newer: true, ahead: false});
pruefe(!b.banner.classList.contains('hide'), 'Update-Hinweis fehlt');
pruefe(!b.banner.classList.contains('warn'), 'Update faelschlich als Warnung');
pruefe(b.html_inhalt.indexOf('5.0.0') >= 0, 'Neue Version nicht genannt');
pruefe(b.html_inhalt.indexOf('<a href') >= 0, 'Link zum Release fehlt');

// 3) Gleichstand: kein Hinweis, und der alte Text bleibt.
var c = lage({status: 'ok', latest: '4.0.0', newer: false, ahead: false});
pruefe(c.banner.classList.contains('hide'), 'Hinweis bei Gleichstand');
pruefe(c.text.length > 0, 'Zustand gar nicht gemeldet');

// 4) Kein Netz: nichts behaupten.
var d = lage({status: 'error', latest: null, newer: false, ahead: false, error: 'weg'});
pruefe(d.banner.classList.contains('hide'), 'Hinweis trotz Fehler');
console.log('OK');
"""


def test_vorabversion_wird_nicht_als_aktuell_ausgegeben():
    _in_node(PRUEFUNG_VORABVERSION)


def test_onedrive_pruefschritt(sandbox):
    schritt = next(s for s in app_mod.build_steps(app_mod.load_config(), check_onedrive=True)
                   if s["key"] == "check_onedrive")
    argv = [str(x) for x in schritt["argv"]]
    assert "--check" in argv and "onedrive_export" in " ".join(argv)


def test_ein_pruefknopf_prueft_beides_in_einem_lauf(sandbox):
    """Das Postfach zuerst: es ist die Hauptquelle und gehört im Protokoll
    nach oben."""
    keys = [s["key"] for s in app_mod.build_steps(app_mod.load_config(),
                                                  check=True, check_onedrive=True)]
    assert keys == ["check", "check_onedrive"]


def test_vollstaendigkeit_hat_genau_einen_knopf():
    """Zwei Knöpfe zwangen den Nutzer erst zu einer Entscheidung darüber, was
    er eigentlich wissen will – „prüfen" ist eine Frage an das Archiv, nicht
    an eine Quelle."""
    assert 'onclick="pruefeVollstaendigkeit()"' in app_mod.PAGE
    assert "pruefeVollstaendigkeit('onedrive')" not in app_mod.PAGE
    assert app_mod.PAGE.count("pruefeVollstaendigkeit(") == 2   # Aufruf + Definition


def test_analytics_liefert_beide_berichte(server, sandbox):
    a, port = server
    for ordner, inhalt in ((app_mod.OUTLOOK_DIR, {"erwartet": 5, "fehlt": 1, "ordner": []}),
                           (app_mod.ONEDRIVE_DIR, {"erwartet": 9, "fehlt": 0, "ordner": []})):
        ziel = sandbox / ordner
        ziel.mkdir(parents=True, exist_ok=True)
        (ziel / "vollstaendigkeit.json").write_text(json.dumps(inhalt), encoding="utf-8")
    r = call(port, "GET", "/api/analytics")[1]
    assert r["vollstaendigkeit"]["erwartet"] == 5
    assert r["vollstaendigkeit_onedrive"]["erwartet"] == 9
    assert "onedrive" in r["groesse"], "Belegter Platz für den Spiegel fehlt"


PRUEFUNG_PRUEFKNOPF = GRUNDZUSTAND + """
var gesendet = [];
global.fetch = function(pfad, opt){
  gesendet.push({pfad: String(pfad), body: opt && opt.body});
  return Promise.resolve({json: function(){ return Promise.resolve(statusGeruest()); }});
};
function starte(){ gesendet = []; pruefeVollstaendigkeit();
  return JSON.parse(gesendet.filter(function(g){
    return g.pfad.indexOf('/api/run') >= 0; })[0].body); }

// OneDrive wird nicht benutzt: nur das Postfach pruefen, keine Netzanfrage
// fuer eine Antwort, die niemanden interessiert.
S.config = {onedrive_enabled: false};
S.folders_onedrive = {};
var a = starte();
pruefe(a.check === true && a.check_onedrive === false,
       'Ohne OneDrive trotzdem geprueft: ' + JSON.stringify(a));

// Haekchen gesetzt: beides.
S.config.onedrive_enabled = true;
pruefe(starte().check_onedrive === true, 'Mit Haekchen nicht mitgeprueft');

// Haekchen aus, aber schon einmal gespiegelt: der Bericht ist weiter sinnvoll.
S.config.onedrive_enabled = false;
S.folders_onedrive = {abgeglichen: '2026-08-10T12:00:00'};
pruefe(starte().check_onedrive === true, 'Vorhandener Spiegel wird uebergangen');
console.log('OK');
"""


def test_ein_pruefknopf_entscheidet_selbst_ueber_onedrive():
    _in_node(PRUEFUNG_PRUEFKNOPF)


PRUEFUNG_BERICHT_EINHEIT = GRUNDZUSTAND + """
var b = {geprueft: '2026-08-10T18:46:00', erwartet: 421, vorhanden: 420,
         geloescht: 0, fehlt: 1, ausgelassen: 208,
         ausgelassene_ordner: ['Dateien/Fotos'],
         ordner: [{ordner: 'Dateien/Fotos', erwartet: 5, vorhanden: 4, fehlt: 1}]};
zeigeBericht(b, 'ana-check-box-od');
var h = document.getElementById('ana-check-box-od').innerHTML;
pruefe(h.indexOf('Mails') < 0 && h.indexOf('mails') < 0,
       'Bericht des Spiegels spricht von Mails: ' + h.slice(0, 220));
pruefe(h.indexOf('Dateien fehlen') >= 0 || h.indexOf('files missing') >= 0,
       'Einheit fehlt in der Luecken-Zeile');

// Der Postfachbericht bleibt, wie er war.
zeigeBericht(b);
var m = document.getElementById('ana-check-box').innerHTML;
pruefe(m.indexOf('Mails') >= 0, 'Postfachbericht spricht nicht mehr von Mails');
console.log('OK');
"""


def test_bericht_nennt_die_richtige_einheit():
    _in_node(PRUEFUNG_BERICHT_EINHEIT)


def test_export_status_kennt_onedrive(sandbox):
    """Der Bestand, nicht der Delta-Zeiger: der wird auch nach einem Lauf ohne
    Änderung neu geschrieben und behauptete dann einen Abgleich, bei dem nichts
    geholt wurde."""
    cfg = app_mod.load_config()
    od = sandbox / app_mod.ONEDRIVE_DIR
    od.mkdir(parents=True, exist_ok=True)
    assert app_mod.export_status(cfg)["onedrive"]["last_run"] is None
    (od / "dateien.tsv").write_text("a\tb\tc\t1\n", encoding="utf-8")
    assert app_mod.export_status(cfg)["onedrive"]["last_run"]


def test_export_reiter_zeigt_weder_zeiten_noch_datenordner():
    """Beides steht woanders: die Zeiten in Analytics, der Ordner in den
    Einstellungen. Zweimal dasselbe an zwei Orten veraltet an einem."""
    kopf = app_mod.PAGE.split('<section id="tab-suche"')[0]
    assert 'id="export-state"' not in kopf, "Zeiten stehen noch im Export-Reiter"
    assert 'id="data-dir"' not in kopf
    assert 'id="export-state"' in app_mod.PAGE, "Zeiten sind ganz verschwunden"
    assert 'id="data-dir2"' in app_mod.PAGE, "Datenordner fehlt in den Einstellungen"


PRUEFUNG_SCHRITTNAME = GRUNDZUSTAND + """
var status = statusGeruest();
status.jobs = {busy: true, seq: 1, token_expired: false, last: null,
               job: {label: 'Export', steps: ['job.step.outlook', 'job.step.index'],
                     step: 'job.step.index', index: 1, progress: null}};
S = null;
renderStatus(status);
var zeile = document.getElementById('fortschritt-text').textContent;
pruefe(zeile.indexOf('job.step.') < 0, 'Schluessel statt Text: ' + zeile);
pruefe(zeile.indexOf('Index') >= 0, 'Schrittname fehlt: ' + zeile);
pruefe(zeile.indexOf('Export') >= 0, 'Etikett des Laufs fehlt: ' + zeile);
console.log('OK');
"""


def test_schrittname_wird_uebersetzt():
    _in_node(PRUEFUNG_SCHRITTNAME)


def test_erklaerung_am_startknopf_ist_ein_tooltip_kein_fliesstext():
    """Fließtext neben jedem Knopf macht die Oberfläche unruhig. Die Erklärung
    steckt jetzt im title-Attribut eines (i) – sichtbar auf Abruf, vorlesbar,
    und ohne eigenes Fenster, das aufgehen und wieder zugehen muss."""
    kopf = app_mod.PAGE.split('<section id="tab-suche"')[0]
    assert 'data-i18n="export.start.hint"' not in kopf, "steht wieder als Text da"
    assert 'data-i18n-title="export.start.hint"' in kopf
    # Der Text selbst bleibt erhalten – nur seine Form ändert sich.
    assert i18n.strings("de")["export.start.hint"]


ERKLAERUNGEN_ALS_INFO = ["export.start.hint", "export.what.sub",
                         "export.index.only.when", "export.calendar.build.when",
                         "export.page.build.when", "search.gone.note"]


@pytest.mark.parametrize("schluessel", ERKLAERUNGEN_ALS_INFO)
def test_erklaerungen_im_exportreiter_stehen_am_infozeichen(schluessel):
    """Absätze neben Knöpfen machen die Oberfläche unruhig; die Erklärung
    gehört auf Abruf. Der Text selbst bleibt – nur seine Form ändert sich."""
    assert f'data-i18n="{schluessel}"' not in app_mod.PAGE, "steht wieder als Fließtext da"
    assert f'data-i18n-title="{schluessel}"' in app_mod.PAGE
    assert i18n.strings("de")[schluessel]


def test_jedes_infozeichen_ist_erreichbar():
    """Ein (i), das nur die Maus kennt, ist für die Tastatur ein Buchstabe
    ohne Bedeutung."""
    zeichen = app_mod.PAGE.count('class="info"')
    assert zeichen >= 5, f"nur {zeichen} (i) gefunden"
    for stueck in app_mod.PAGE.split('<span class="info"')[1:]:
        block = stueck[:220]
        assert 'tabindex="0"' in block and "aria-label=" in block


def test_infozeichen_ist_erreichbar_und_erklaert_sich():
    """Ein (i), das nur die Maus kennt, ist für die Tastatur ein Buchstabe
    ohne Bedeutung."""
    i = app_mod.PAGE.index('data-i18n-title="export.start.hint"')
    block = app_mod.PAGE[i - 200:i + 200]
    assert 'tabindex="0"' in block, "mit der Tastatur nicht erreichbar"
    assert 'aria-label=' in block, "ohne Namen für den Screenreader"
    assert ".info{" in app_mod.PAGE and "cursor:help" in app_mod.PAGE


PRUEFUNG_ANALYTICS_KACHELN = GRUNDZUSTAND + """
var a = {exists: true, nachrichten: 123456, gespraeche: 12000, mit_anhang: 3400,
  personen: 2500, verschwunden: 18, von: 1551398400, bis: 1788134400,
  quellen: [{src:'teams',nachrichten:100000},{src:'outlook',nachrichten:23000},
            {src:'datei',nachrichten:629}],
  groesse: {teams: 1000, outlook: 2000, onedrive: 3000, index: 500},
  vollstaendigkeit: null, vollstaendigkeit_onedrive: null};
zeigeAnalytics(a);
var h = document.getElementById('ana-kpi').innerHTML;

// Neue Kachel, aus dem Index gerechnet.
pruefe(h.indexOf('OneDrive-Dateien') >= 0, 'Kachel fehlt');
pruefe(h.indexOf('>629<') >= 0, 'Dateizahl fehlt: ' + h.slice(0, 200));

// Der Knopf ist weg; die Kachel selbst fuehrt zur Suche.
pruefe(h.indexOf('kpi-fuss') < 0, '"Show these" steht noch da');
pruefe((h.match(/klickbar/g) || []).length === 1, 'Genau eine Kachel soll klickbar sein');
pruefe(h.indexOf('role="button"') >= 0 && h.indexOf('onkeydown=') >= 0,
       'Klickbare Kachel ist nicht mit der Tastatur bedienbar');

// Erklaerungen am (i), Zahlen sichtbar.
pruefe(h.indexOf('Related mails') < 0, 'Erklaerung steht noch als Text da');
pruefe((h.match(/class="info"/g) || []).length === 4, 'Falsche Zahl an Infozeichen');
// Ohne Tausendertrennzeichen geprueft: das haengt an der Sprache.
pruefe(h.indexOf('Teams 100') >= 0, 'Aufteilung nach Quellen ist verschwunden');
pruefe(h.indexOf('kpi-hint') >= 0, 'Sichtbare Zahlenzeile ganz weg');

// Ohne Verschwundenes fuehrt die Kachel nirgendwohin – eine Sackgasse waere schlechter.
a.verschwunden = 0;
// Und ohne Spiegel gibt es die Dateikachel gar nicht: "0" waere fuer alle,
// die OneDrive nicht nutzen, eine Zeile, die nichts sagt.
a.quellen = [{src:'teams',nachrichten:1}];
zeigeAnalytics(a);
var h2 = document.getElementById('ana-kpi').innerHTML;
pruefe((h2.match(/klickbar/g) || []).length === 0, 'Kachel ohne Treffer trotzdem klickbar');
pruefe(h2.indexOf('OneDrive-Dateien') < 0, 'Leere Dateikachel wird gezeigt');
console.log('OK');
"""


def test_analytics_kacheln_zeigen_zahlen_und_erklaeren_am_infozeichen():
    _in_node(PRUEFUNG_ANALYTICS_KACHELN)


def test_kein_stylesheet_zieht_ein_infozeichen_auseinander():
    """Regression: `.schritt span{flex:1;min-width:240px}` stammte vom
    Erklärungstext, der dort einmal stand. Nach dem Umbau traf sie das (i) –
    aus dem Kreis wurde eine 240 Pixel breite Ellipse quer durch die Zeile.

    Geprüft wird deshalb allgemein: keine Regel, die *jedes* span in einem
    Behälter breitzieht, darf auf einen Behälter treffen, in dem ein (i) sitzt.
    """
    # Kommentare erst weg: dieser hier zitiert die alte Regel im Wortlaut, und
    # der Test soll auf das Stylesheet schauen, nicht auf seine Begründung.
    css = re.sub(r"/\*.*?\*/", "",
                 app_mod.PAGE.split("<style>")[1].split("</style>")[0], flags=re.S)
    markup = app_mod.PAGE.split("</style>")[1]
    gefaehrlich = re.findall(r"\.([\w-]+) span\{([^}]*)\}", css)
    for klasse, regel in gefaehrlich:
        if not re.search(r"flex:\s*1|min-width|width:", regel):
            continue
        for stueck in markup.split(f'class="{klasse}"')[1:]:
            bis_ende = stueck.split("</div>")[0]
            assert 'class="info"' not in bis_ende, (
                f'.{klasse} span{{{regel}}} trifft das (i) darin')


def test_infozeichen_behaelt_seine_groesse():
    css = re.sub(r"/\*.*?\*/", "",
                 app_mod.PAGE.split("<style>")[1].split("</style>")[0], flags=re.S)
    regel = re.search(r"\.info\{([^}]*)\}", css).group(1)
    assert "width:17px" in regel and "height:17px" in regel
    assert "flex:0 0 auto" in regel, "sonst zieht der nächste Flex-Behälter daran"


def test_kopfleiste_zeigt_nur_was_eine_handlung_verlangt():
    """Der Zustand des Index stand als Kachel im Kopf und steht jetzt in
    Analytics. Zweimal dieselbe Zahl an zwei Orten hilft niemandem – sie
    widersprechen sich irgendwann. Im Kopf bleibt, was etwas von einem will:
    Zugang, KI-Suche, Claude."""
    kopf = app_mod.PAGE.split("<nav")[0]
    assert 'id="pill-index"' not in kopf
    for erwartet in ('id="pill-token"', 'id="pill-ollama"', 'id="pill-mcp"'):
        assert erwartet in kopf, f"{erwartet} ist mit verschwunden"
    # Die Zahl steht weiterhin irgendwo – nur eben in den Kennzahlen.
    assert 'id="ana-kpi"' in app_mod.PAGE


PRUEFUNG_SUCHMASKE = GRUNDZUSTAND + """
var gesucht = [];
global.fetch = function(pfad){
  gesucht.push(String(pfad));
  return Promise.resolve({json: function(){ return Promise.resolve(
    String(pfad).indexOf('/api/search') >= 0 ? {results: [], total: 0}
                                             : statusGeruest()); }});
};

// Der Schalter zaehlt, was eingestellt ist - sonst waere zugeklappt eine Falle.
document.getElementById('f-person').value = 'Alice';
document.getElementById('f-source').value = 'outlook';
zeigeFilterstand();
var text = document.getElementById('filter-auf').textContent;
pruefe(/2/.test(text), 'Zahl der Filter fehlt: ' + text);
pruefe(!document.getElementById('filter-weg').classList.contains('hide'),
       '"Zuruecksetzen" fehlt trotz Filter');

// "Alle Quellen" ist kein Filter.
document.getElementById('f-source').value = 'all';
zeigeFilterstand();
pruefe(/1/.test(document.getElementById('filter-auf').textContent),
       '"Alle Quellen" wurde mitgezaehlt');

filterLeeren();
pruefe(document.getElementById('f-person').value === '', 'Nicht geleert');
pruefe(document.getElementById('filter-auf').textContent.indexOf('2') < 0, 'Zaehler bleibt');

// Auf- und zuklappen sagt der Tastatur, was es tut. Der Ausgangszustand wird
// hier gesetzt: die Attrappe liest die class-Attribute des Markups nicht.
document.getElementById('filter').classList.add('hide');
filterUmschalten();
pruefe(document.getElementById('filter-auf').getAttribute('aria-expanded') === 'true',
       'aria-expanded folgt dem Aufklappen nicht');
filterUmschalten();
pruefe(document.getElementById('filter-auf').getAttribute('aria-expanded') === 'false',
       'aria-expanded folgt dem Zuklappen nicht');

// Geloeschtes ist eine Sicht, kein Filter neben fuenf anderen.
gesucht = [];
sicht('geloescht');
pruefe(document.getElementById('f-gone').checked === true, 'Filter nicht gesetzt');
pruefe(!document.getElementById('sicht-treffer').classList.contains('hide'),
       'Trefferliste nicht sichtbar');
pruefe(gesucht.filter(function(u){ return u.indexOf('gone=1') >= 0; }).length === 1,
       'Es wurde nicht mit gone=1 gesucht: ' + gesucht.join(' '));

// Zurueck auf Treffer nimmt den Filter wieder weg.
gesucht = [];
sicht('treffer');
pruefe(document.getElementById('f-gone').checked === false, 'Filter blieb haengen');
pruefe(gesucht.filter(function(u){ return u.indexOf('gone=1') >= 0; }).length === 0,
       'Weiterhin nach Geloeschtem gesucht');

// Zweimal dieselbe Sicht sucht nicht doppelt.
gesucht = [];
sicht('treffer');
pruefe(gesucht.length === 0, 'Ueberfluessige Suche bei gleicher Sicht');
console.log('OK');
"""


def test_suchmaske_filter_und_geloeschtes_als_sicht():
    _in_node(PRUEFUNG_SUCHMASKE)


def test_filter_beginnen_zugeklappt():
    """Wer nichts filtert – der Normalfall – sieht ein Suchfeld und einen Knopf.
    Im Markup geprüft: die DOM-Attrappe der JS-Tests liest keine class-Attribute."""
    i = app_mod.PAGE.index('id="filter"')
    assert 'class="row hide"' in app_mod.PAGE[i - 60:i], "Filter stehen offen da"
    j = app_mod.PAGE.index('id="filter-weg"')
    assert 'class="mini hide"' in app_mod.PAGE[j - 60:j], "„Zurücksetzen“ ohne Filter sichtbar"


def test_suchkarte_hat_weder_ueberschrift_noch_systemsprache():
    """Der Reiter sagt schon, wo man ist; der Zustand des Index steht in
    Analytics. „BM25“ und „Embeddings“ gehören ohnehin nicht in die Maske."""
    i = app_mod.PAGE.index('class="suchzeile"')
    karte = app_mod.PAGE[i - 300:i]
    assert 'data-i18n="nav.search"' not in karte, "Überschrift wiederholt den Reiter"
    assert 'id="search-sub"' not in app_mod.PAGE, "Statuszeile in der Maske"


def test_kein_feld_sucht_von_selbst():
    """Gesucht wird, wenn jemand danach fragt. Man soll in Ruhe Begriff,
    Person, Zeitraum und Ordner eingeben können, ohne dass nach jeder Änderung
    eine Suche losläuft – vorher taten das die Filter, und die Trefferliste
    gehörte dann zu einem halb ausgefüllten Formular."""
    i = app_mod.PAGE.index('id="filter"')
    block = app_mod.PAGE[i:i + 1600]
    for feld in ('id="f-person"', 'id="f-source"', 'id="f-from"',
                 'id="f-to"', 'id="f-folder"'):
        j = block.index(feld)
        # max(0, …): ein negativer Anfang rutscht in Python ans Ende der
        # Zeichenkette und prüfte dann irgendetwas.
        umfeld = block[max(0, j - 120):j + 200]
        assert "doSearch" not in umfeld, f"{feld} sucht von selbst"
        # Die Quelle lädt zusätzlich die Ordnerliste nach – gesucht wird auch
        # dann nicht, gezählt aber schon.
        assert "zeigeFilterstand()" in umfeld, f"{feld} zählt nicht mit"
    q = app_mod.PAGE[app_mod.PAGE.index('id="q"'):][:260]
    assert "oninput" not in q, "das Suchfeld sucht beim Tippen"


PRUEFUNG_KI_UND_PAGER = GRUNDZUSTAND + """
var st = statusGeruest();
st.store = {exists: true, chunks: 5, messages: 2, semantic: true,
            built_at: '2026-08-10T09:00:00', model: 'bge-m3', features: ['gone','thread']};
st.ollama = {running: true, has_model: true, has_chat_model: true,
             model: 'bge-m3', chat_model: 'q', models: []};
S = null;
renderStatus(st);

// renderStatus muss BIS ANS ENDE laufen. Zeigt eine Zeile darin auf ein
// Element, das es nicht gibt, wirft der Browser - und alles danach unterbleibt.
pruefe(!document.getElementById('m-ki').disabled,
       'KI-Variante gesperrt, obwohl ein Modell da ist');
pruefe(document.getElementById('mcp-json').textContent.length > 0,
       'renderStatus ist vorher abgebrochen');

// Ohne Modell wird sie wieder gesperrt.
st.ollama.has_chat_model = false;
renderStatus(st);
pruefe(document.getElementById('m-ki').disabled,
       'KI-Variante trotz fehlendem Modell waehlbar');

// Der Blaetterbereich nennt kein "Ranking" mehr - bei einer Suche ohne
// Begriff gibt es keines, und "hybrid" ist ein Wort fuer Entwickler.
renderHits({results: [], total: 0, backend: 'hybrid'});
var p = document.getElementById('pager').innerHTML;
pruefe(p.toLowerCase().indexOf('ranking') < 0, 'Ranking steht wieder da: ' + p);
pruefe(p.indexOf('hybrid') < 0, 'Systemwort im Blaetterbereich');
console.log('OK');
"""


def test_ki_kasten_erscheint_und_pager_bleibt_stumm():
    _in_node(PRUEFUNG_KI_UND_PAGER)


PRUEFUNG_SUCHFELD_LOEST_AUS = GRUNDZUSTAND + """
var gesucht = [];
global.fetch = function(pfad){
  gesucht.push(String(pfad));
  return Promise.resolve({json: function(){ return Promise.resolve(
    String(pfad).indexOf('/api/search') >= 0 ? {results: [], total: 0}
                                             : statusGeruest()); }});
};
// Filter setzen und tippen loest KEINE Suche aus - man soll in Ruhe alles
// eingeben koennen.
document.getElementById('f-from').value = '2026-08-10';
document.getElementById('f-person').value = 'Alice';
zeigeFilterstand();
document.getElementById('q').value = 'Betriebsrat';
pruefe(gesucht.filter(function(u){ return u.indexOf('/api/search') >= 0; }).length === 0,
       'Es wurde beim Ausfuellen schon gesucht: ' + gesucht.join(' '));

// Erst der Knopf sucht - und zwar mit allem, was im Formular steht.
gesucht = [];
sofortSuchen();
var u = gesucht.filter(function(x){ return x.indexOf('/api/search') >= 0; });
pruefe(u.length === 1, 'Knopf sucht nicht');
pruefe(u[0].indexOf('Betriebsrat') >= 0 && u[0].indexOf('Alice') >= 0 &&
       u[0].indexOf('2026-08-10') >= 0, 'Formular nicht vollstaendig uebernommen: ' + u[0]);

// "Zuruecksetzen" leert nur - gesucht wird auch dann erst auf Wunsch.
gesucht = [];
filterLeeren();
pruefe(gesucht.filter(function(x){ return x.indexOf('/api/search') >= 0; }).length === 0,
       'Zuruecksetzen hat gesucht');

// Der Begriff wird in der Vorschau markiert - sonst sieht man nicht, warum
// ein Treffer einer ist.
var markiert = hervor('Hier steht Betriebsrat mittendrin');
pruefe(markiert.indexOf('<mark>Betriebsrat</mark>') >= 0, 'Nicht markiert: ' + markiert);
// Maskiert wird trotzdem: sonst waere die Vorschau ein Einfallstor.
pruefe(hervor('<b>x</b>').indexOf('&lt;b&gt;') >= 0, 'Vorschau nicht maskiert');
console.log('OK');
"""


def test_suchfeld_loest_aus_und_markiert():
    _in_node(PRUEFUNG_SUCHFELD_LOEST_AUS)


def test_suchfeld_und_markierung_sind_verdrahtet():
    """Die Funktionen einzeln zu prüfen genügt nicht: die Gegenproben liefen
    durch, weil der Test sie direkt aufrief statt über die Seite. Geprüft wird
    deshalb die Verdrahtung selbst."""
    i = app_mod.PAGE.index('id="q"')
    feld = app_mod.PAGE[i:i + 260]
    assert "sofortSuchen()" in feld, "Enter sucht nicht"
    assert 'onclick="sofortSuchen()"' in app_mod.PAGE, "Der Knopf wartet auf die Verzögerung"
    # Die Vorschau geht durch hervor(), nicht an ihm vorbei.
    j = app_mod.PAGE.index('class="prev"')
    assert "hervor(h.preview" in app_mod.PAGE[j:j + 120], "Begriff wird nicht markiert"


@pytest.mark.parametrize("wert,erwartet", [
    (60, 60), (0, 0), (95, 95),
    (200, 95),      # über den Rand: auf den Rand gezogen
    (-5, 0),
])
def test_untergrenze_ist_einstellbar(server, wert, erwartet):
    a, port = server
    call(port, "POST", "/api/config", {"semantic_min": wert})
    assert a.cfg["semantic_min"] == erwartet


def test_unbrauchbare_untergrenze_laesst_den_wert_stehen(server):
    """Nicht auf die Vorgabe zurückfallen: wer 60 eingestellt hat und sich
    vertippt, soll nicht unbemerkt wieder bei 45 landen."""
    a, port = server
    call(port, "POST", "/api/config", {"semantic_min": 60})
    call(port, "POST", "/api/config", {"semantic_min": "unsinn"})
    assert a.cfg["semantic_min"] == 60


def test_untergrenze_wird_erklaert():
    """Eine Zahl ohne Erklärung stellt niemand um – und wer sie doch umstellt,
    soll wissen, was zu hoch und was zu niedrig ist."""
    # Die Erklärung steht jetzt im (i) der Zeile statt als Fließtext darunter –
    # ausführlich bleibt sie trotzdem: diese eine Zahl verändert, was die Suche
    # überhaupt zeigt.
    text = i18n.strings("de")["settings.semantic_min.i"]
    assert len(text) > 400, "zu knapp für eine Einstellung, die die Suche verändert"
    for stichwort in ("45", "0", "Volltextsuche"):
        assert stichwort in text, f"„{stichwort}“ fehlt in der Erklärung"
    for code in ("de", "en", "fr"):
        assert i18n.strings(code)["settings.semantic_min.i"] != text or code == "de"


# --------------------------------------------------------------------------
# Fehlerbericht
#
# Der Bestand dieser App ist Post und Chat. Ein Bericht, der auf einer
# öffentlichen Seite landet, darf deshalb nicht mitnehmen, wer mit wem
# schreibt und wie der Anwender heißt. Zwei Vorkehrungen, beide geprüft:
# was maschinell erkennbar ist, wird ersetzt – und der Rest liegt vor dem
# Absenden offen zum Ändern (siehe die JS-Prüfungen weiter unten).
# --------------------------------------------------------------------------
def test_anonymisiere_nimmt_mailadressen_heraus():
    text = app_mod.anonymisiere(
        "Access found for vorname.nachname@example.com, valid for 11 h.")
    assert "vorname.nachname" not in text and "example.com" not in text
    assert "Access found for" in text, "der Rest der Zeile muss lesbar bleiben"


def test_anonymisiere_nimmt_die_domaene_mit():
    """Nur den Teil vor dem @ zu ersetzen reichte nicht: die Domäne ist der
    Arbeitgeber, und der ist mindestens so verräterisch wie der Name."""
    assert "contoso" not in app_mod.anonymisiere("a@contoso.example").lower()


def test_anonymisiere_nimmt_den_benutzernamen_aus_pfaden():
    """Der Anmeldename steckt in fast jedem Pfad, den ein Protokoll nennt."""
    aus = app_mod.anonymisiere(
        r"OneDrive-Spiegel: C:\Users\pmustermann\AppData\Local\Archiv\onedrive")
    assert "pmustermann" not in aus
    # Was danach kommt, ist technisch und muss bleiben – sonst wäre der Pfad
    # als Angabe wertlos.
    assert r"AppData\Local\Archiv\onedrive" in aus


@pytest.mark.parametrize("pfad, weg", [
    ("/Users/pmustermann/Library/Archiv", "pmustermann"),
    ("/home/pmustermann/.local/share/Archiv", "pmustermann"),
])
def test_anonymisiere_kennt_auch_die_unix_pfade(pfad, weg):
    assert weg not in app_mod.anonymisiere(f"Datenordner: {pfad}")
    assert "Archiv" in app_mod.anonymisiere(f"Datenordner: {pfad}")


def test_anonymisiere_vertraegt_leeres():
    assert app_mod.anonymisiere(None) == ""


def test_gekuerzt_behaelt_das_ende():
    """Vorne steht der Start der App, hinten der Absturz. Wer kürzt, kürzt vorne."""
    text = "\n".join(f"zeile {i}" for i in range(500))
    aus = app_mod.gekuerzt(text, zeilen=10, zeichen=10_000)
    assert "zeile 499" in aus and "zeile 490" in aus
    assert "zeile 100" not in aus
    assert "ausgelassen" in aus, "das Kürzen muss sichtbar sein"


def test_gekuerzt_laesst_kurzes_unangetastet():
    assert app_mod.gekuerzt("a\nb") == "a\nb"


def test_gekuerzt_haelt_die_zeichengrenze():
    aus = app_mod.gekuerzt("x" * 9000, zeilen=100, zeichen=1000)
    assert len(aus) < 1200 and "ausgelassen" in aus


def test_systemangaben_nennen_was_zur_einordnung_noetig_ist(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    angaben = {z["k"]: z["v"] for z in app_mod.systemangaben(a.status(), "de")}
    # Genau die Fragen, die man sonst per Rückfrage stellen müsste.
    assert {"version", "os", "python", "cores", "lang", "auth",
            "categories", "index", "model", "ollama"} <= set(angaben)
    assert app_mod.version.VERSION in angaben["version"]
    assert "Skript" in angaben["version"], "gebündelt oder nicht ist die halbe Miete"
    assert angaben["lang"] == "de"
    assert angaben["cores"] == str(os.cpu_count())


def test_systemangaben_nennen_den_datenordner_nur_wenn_er_abweicht(sandbox, with_ollama):
    """Der Standardordner steht ohnehin fest und trüge bloß einen Benutzernamen
    mit sich – der abweichende dagegen erklärt eine ganze Klasse von Fehlern."""
    a = app_mod.App(app_mod.load_config())
    st = a.status()
    st["data_dir"] = st["data_dir_default"]
    assert "datadir" not in {z["k"] for z in app_mod.systemangaben(st)}

    st["data_dir"] = r"C:\Users\pmustermann\Woanders"
    zeilen = {z["k"]: z["v"] for z in app_mod.systemangaben(st)}
    assert "Woanders" in zeilen["datadir"]
    assert "pmustermann" not in zeilen["datadir"], "auch hier wird ersetzt"


def test_systemangaben_sind_reine_schluesselruempfe(sandbox, with_ollama):
    """Übersetzt wird in der Oberfläche – hier darf kein fertiger Satz stehen."""
    a = app_mod.App(app_mod.load_config())
    for z in app_mod.systemangaben(a.status()):
        assert "." not in z["k"] and z["k"].islower(), z


def test_fehlerbericht_reicht_nichts_ungefiltert_durch(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    b = app_mod.fehlerbericht(
        a.status(),
        log_text="Access found for chef@contoso.example\n"
                 r"Spiegel: C:\Users\pmustermann\AppData",
        hint="Fehler bei chef@contoso.example")
    assert "chef@contoso.example" not in b["log"]
    assert "pmustermann" not in b["log"]
    assert "chef@contoso.example" not in b["title"], "auch der Betreff"
    assert b["url"].startswith("https://github.com/")
    assert b["url"].endswith("/issues/new")
    assert app_mod.version.REPO in b["url"]


def test_fehlerbericht_kappt_einen_endlosen_betreff(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    b = app_mod.fehlerbericht(a.status(), hint="x" * 400)
    assert len(b["title"]) <= 120


def test_http_report(server):
    """Der Weg, den die Oberfläche geht."""
    a, port = server
    code, b = call(port, "POST", "/api/report",
                   {"log": "09:00:00  Hallo welt@example.com", "hint": "Absturz"})
    assert code == 200
    assert b["title"] == "Absturz"
    assert "welt@example.com" not in b["log"] and "Hallo" in b["log"]
    assert [z["k"] for z in b["system"]][:2] == ["version", "os"]


def test_http_report_ohne_angaben(server):
    """Der Knopf in den Einstellungen wird auch bei leerem Protokoll gedrückt."""
    _, port = server
    code, b = call(port, "POST", "/api/report", {})
    assert code == 200 and b["log"] == "" and b["title"] == ""
    assert b["system"], "die Systemangaben stehen immer zur Verfügung"


def test_http_report_folgt_der_browsersprache(server):
    """Die Sprache steht im Bericht, weil sie erklärt, welche Texte der Melder
    gesehen hat."""
    _, port = server
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    con.request("POST", "/api/report", "{}",
                {"Content-Type": "application/json", "Accept-Language": "fr-CH,fr;q=0.9"})
    b = json.loads(con.getresponse().read())
    con.close()
    assert {z["k"]: z["v"] for z in b["system"]}["lang"] == "fr"


# --------------------------------------------------------------------------
# Protokoll kopieren
#
# Der Kasten hat je Zeile ein eigenes Kind. textContent klebte sie ohne
# Umbruch aneinander – ein Protokoll, das als eine einzige Zeile in der
# Zwischenablage landet, ist als Fehlermeldung wertlos.
# --------------------------------------------------------------------------
PRUEFUNG_LOG_KOPIEREN = GRUNDZUSTAND + """
var kopiert = [];
Object.defineProperty(global, 'navigator', {configurable: true, writable: true,
  value: {clipboard: {writeText: function(t){ kopiert.push(t);
                                              return Promise.resolve(); }}}});
var kasten = document.getElementById('log');
kasten.children = [{textContent: '09:00:00  erste Zeile'},
                   {textContent: '09:00:01  zweite Zeile'}];
kopiere('log', {textContent: 'Kopieren'});
pruefe(kopiert.length === 1, 'Nichts kopiert');
pruefe(kopiert[0] === '09:00:00  erste Zeile\\n09:00:01  zweite Zeile',
       'Zeilen nicht getrennt: ' + JSON.stringify(kopiert[0]));
console.log('OK');
"""


def test_protokoll_laesst_sich_zeilenweise_kopieren():
    _in_node(PRUEFUNG_LOG_KOPIEREN)


def test_kopierknopf_klappt_das_protokoll_nicht_zu():
    """Die Knöpfe liegen in der Kopfzeile, die selbst auf- und zuklappt. Ohne
    stopPropagation klappte das Protokoll bei jedem Kopieren zu – man sähe das
    Ergebnis genau in dem Moment nicht mehr, in dem man es braucht."""
    seite = app_mod.PAGE
    kopf = seite.split('class="pkopf"')[1].split("</div>")[0]
    for knopf in ("kopiere('log', this)", "fehlerMelden()"):
        assert knopf in kopf, f"{knopf} steht nicht in der Protokollkopfzeile"
    assert kopf.count("event.stopPropagation()") == 2, (
        "Nicht jeder Knopf in der Kopfzeile hält sein Klickereignis an")


# --------------------------------------------------------------------------
# Fehler melden – die Seite dieses Vorgangs, die im Browser läuft
# --------------------------------------------------------------------------
BERICHT_GERUEST = GRUNDZUSTAND + """
global.geoeffnet = [];
// Im Browser IST window das globale Objekt; node bringt keines mit.
global.window = {open: function(u){ geoeffnet.push(u); }};
// Zwei Endpunkte, zwei Antworten: erst das Protokoll, dann der Bericht.
global.gesendet = [];
global.fetch = function(pfad, opt){
  var antwort;
  if(String(pfad).indexOf('/api/log') === 0){
    antwort = {seq: 3, lines: [
      {n: 1, level: 'info', t: '09:00:00', text: 'Export gestartet'},
      {n: 2, level: 'err',  t: '09:24:36', text: 'BrokenProcessPool: abrupt beendet'}]};
  } else if(String(pfad) === '/api/report'){
    gesendet.push(JSON.parse(opt.body));
    antwort = {system: [{k: 'version', v: '4.0.0 (Skript)'},
                        {k: 'cores', v: '8'}],
               log: '09:00:00  Export gestartet\\n09:24:36  BrokenProcessPool',
               title: 'BrokenProcessPool: abrupt beendet',
               url: 'https://github.com/beispiel/repo/issues/new'};
  } else {
    antwort = statusGeruest();
  }
  return Promise.resolve({json: function(){ return Promise.resolve(antwort); }});
};
"""

PRUEFUNG_BERICHT = BERICHT_GERUEST + """
fehlerMelden();
// Solange nichts da ist, steht der Dialog trotzdem schon offen.
pruefe(wizardOffen === 'report', 'Fenster nicht geoeffnet');

setTimeout(function(){
  var html = modal.innerHTML;
  pruefe(html.indexOf('id="rep-text"') >= 0, 'Kein Textfeld: ' + html.slice(0, 200));
  pruefe(html.indexOf('<textarea') >= 0, 'Der Bericht ist nicht aenderbar');

  // Was mitgeschickt wurde: das uebersetzte Protokoll und die letzte
  // Fehlerzeile als Betreffvorschlag.
  pruefe(gesendet.length === 1, 'Kein Bericht angefordert');
  pruefe(gesendet[0].log.indexOf('Export gestartet') >= 0, 'Protokoll fehlt');
  pruefe(gesendet[0].hint.indexOf('BrokenProcessPool') >= 0,
         'Betreffvorschlag kommt nicht aus der Fehlerzeile: ' + gesendet[0].hint);

  // Der Text, den der Mensch vor sich hat: Angaben und Protokoll, beides drin.
  // Geprueft wird am gezeichneten HTML - der DOM-Stummel zerlegt innerHTML
  // nicht in Knoten, im Browser steht genau dieser Text im Feld.
  pruefe(html.indexOf('4.0.0 (Skript)') >= 0, 'Systemangaben fehlen');
  pruefe(html.indexOf('BrokenProcessPool') >= 0, 'Protokoll fehlt im Bericht');
  pruefe(html.indexOf('| Kerne | 8 |') >= 0,
         'Die Angaben sind nicht uebersetzt: ' + html);
  pruefe(html.indexOf('value="BrokenProcessPool: abrupt beendet"') >= 0,
         'Betreff nicht vorbelegt');

  // Geaendert wird vor dem Absenden - und die Aenderung muss ankommen.
  document.getElementById('rep-text').value = 'Von Hand umgeschrieben';
  document.getElementById('rep-titel').value = 'Eigener Betreff';
  berichtOeffnen();
  pruefe(geoeffnet.length === 1, 'Kein Formular geoeffnet');
  var u = geoeffnet[0];
  pruefe(u.indexOf('https://github.com/beispiel/repo/issues/new?') === 0, 'Falsches Ziel: ' + u);
  pruefe(u.indexOf('title=Eigener%20Betreff') >= 0, 'Eigener Betreff fehlt: ' + u);
  pruefe(u.indexOf(encodeURIComponent('Von Hand umgeschrieben')) >= 0,
         'Die Aenderung wurde nicht uebernommen: ' + u);
  pruefe(u.indexOf('BrokenProcessPool') < 0,
         'Der ersetzte Text steht trotzdem in der Adresse');
  console.log('OK');
}, 30);
"""


def test_fehlerbericht_zeigt_alles_und_laesst_es_aendern():
    _in_node(PRUEFUNG_BERICHT)


PRUEFUNG_BERICHT_LEER = BERICHT_GERUEST + """
fehlerMelden();
setTimeout(function(){
  document.getElementById('rep-titel').value = '   ';
  berichtOeffnen();
  // Ein Entwurf ganz ohne Betreff waere auf GitHub nicht abzuschicken.
  pruefe(geoeffnet[0].indexOf('title=') >= 0 &&
         geoeffnet[0].indexOf('title=&') < 0, 'Leerer Betreff: ' + geoeffnet[0]);
  console.log('OK');
}, 30);
"""


def test_fehlerbericht_faellt_auf_einen_betreff_zurueck():
    _in_node(PRUEFUNG_BERICHT_LEER)


PRUEFUNG_BERICHT_LANG = BERICHT_GERUEST + """
fehlerMelden();
setTimeout(function(){
  // Jemand fuegt ein sehr langes Protokoll ein. GitHub bekommt den Entwurf in
  // der Adresse; zu lange Adressen weist der Server ab - mit einer leeren
  // Seite, nicht mit einer Erklaerung.
  var zeilen = [];
  for(var i = 0; i < 900; i++) zeilen.push('09:00:00  Zeile ' + i + ' mit etwas Text');
  zeilen.push('09:59:59  DAS HIER IST DER ABSTURZ');
  document.getElementById('rep-text').value =
    '### System\\n\\n| Version | 4.0.0 |\\n\\n### Protokoll\\n\\n```\\n' +
    zeilen.join('\\n') + '\\n```\\n';
  berichtOeffnen();

  var u = geoeffnet[0];
  pruefe(u.length <= 7000, 'Adresse zu lang: ' + u.length);
  // Gekuerzt wird VORNE: die letzten Zeilen sind die, um die es geht.
  pruefe(u.indexOf(encodeURIComponent('DAS HIER IST DER ABSTURZ')) >= 0,
         'Der Absturz fehlt im gekuerzten Bericht');
  pruefe(u.indexOf(encodeURIComponent('Zeile 0 mit')) < 0,
         'Die aeltesten Zeilen stehen noch drin');
  pruefe(u.indexOf(encodeURIComponent('| Version | 4.0.0 |')) >= 0,
         'Die Systemangaben wurden mit weggekuerzt');
  // Und es wird gesagt, statt es stillschweigend zu tun.
  pruefe(document.getElementById('rep-hinweis').textContent.length > 0,
         'Kein Hinweis auf das Kuerzen');
  console.log('OK');
}, 30);
"""


def test_fehlerbericht_kuerzt_vorne_und_sagt_es():
    _in_node(PRUEFUNG_BERICHT_LANG)


# --------------------------------------------------------------------------
# Die drei Suchvarianten
#
# Bis 4.2.0 mischte jede Suche BM25 und Vektoren (hybrid) und die KI hing an
# einer Checkbox, die bei JEDER Suche ein Modell anwarf. Beides ist ersetzt:
# Textsuche ist die Vorgabe und rein lexikalisch, die anderen beiden sind eine
# bewusste Abzweigung.
# --------------------------------------------------------------------------
PRUEFUNG_MODI = GRUNDZUSTAND + """
var gesucht = [], gefragt = 0;
global.fetch = function(pfad, opt){
  var s = String(pfad);
  if(s.indexOf('/api/search') >= 0){ gesucht.push(s); }
  if(s.indexOf('/api/answer') >= 0){ gefragt++; return Promise.resolve({ok: false,
    json: function(){ return Promise.resolve({error: 'x'}); }}); }
  return Promise.resolve({json: function(){ return Promise.resolve(
    s.indexOf('/api/search') >= 0
      ? {count: 1, results: [{uid: 'u:1', cid: 7, title: 'T', who: 'Alice',
                              date: '2026-03-04', source_label: 'Mail',
                              preview: 'p', uri: 'o365://outlook/a.eml'}]}
      : statusGeruest()); }});
};
document.getElementById('q').value = 'Rechnung';

// Vorgabe ist die Textsuche, und die geht als lexical an den Server.
pruefe(SUCHMODUS === 'text', 'Vorgabe ist nicht die Textsuche: ' + SUCHMODUS);
sofortSuchen();
pruefe(gesucht[0].indexOf('mode=lexical') >= 0, 'Textsuche nicht lexical: ' + gesucht[0]);

// Jede Variante hat ihren eigenen Server-Modus.
gesucht = []; suchmodus('aehnlich');
pruefe(gesucht[0].indexOf('mode=semantic') >= 0, 'Aehnliche nicht semantic: ' + gesucht[0]);
gesucht = []; suchmodus('ki');
pruefe(gesucht[0].indexOf('mode=hybrid') >= 0, 'KI nicht hybrid: ' + gesucht[0]);

// Der Platzhalter wechselt mit - er ist die einzige Erklaerung, die es gibt.
var platz = {};
['text','aehnlich','ki'].forEach(function(a){
  suchmodus(a); platz[a] = document.getElementById('q').placeholder;
});
pruefe(platz.text && platz.aehnlich && platz.ki, 'Platzhalter fehlt');
pruefe(platz.text !== platz.aehnlich && platz.aehnlich !== platz.ki,
       'Platzhalter unterscheiden sich nicht');
console.log('OK');
"""


def test_suchvarianten_gehen_an_den_richtigen_backend():
    _in_node(PRUEFUNG_MODI)


PRUEFUNG_KI_NUR_AUF_WUNSCH = GRUNDZUSTAND + """
var gefragt = 0;
global.fetch = function(pfad){
  var s = String(pfad);
  if(s.indexOf('/api/answer') >= 0){ gefragt++;
    return Promise.resolve({ok: false, json: function(){
      return Promise.resolve({error: 'x'}); }}); }
  return Promise.resolve({json: function(){ return Promise.resolve(
    s.indexOf('/api/search') >= 0
      ? {count: 1, results: [{uid: 'u:1', cid: 7, title: 'T', who: 'A',
                              date: '2026-03-04', source_label: 'Mail', preview: 'p'}]}
      : statusGeruest()); }});
};
document.getElementById('q').value = 'Rechnung';

// Das war die Klage: die Suche wartete auf das Modell. Text und Aehnliche
// fragen es gar nicht erst.
suchmodus('text'); sofortSuchen();
suchmodus('aehnlich'); sofortSuchen();
pruefe(gefragt === 0, 'Das Modell lief ungefragt: ' + gefragt);

setTimeout(function(){
  pruefe(gefragt === 0, 'Das Modell lief verspaetet doch: ' + gefragt);
  console.log('OK');
}, 20);
"""


def test_die_ki_laeuft_nur_in_ihrer_eigenen_variante():
    _in_node(PRUEFUNG_KI_NUR_AUF_WUNSCH)


PRUEFUNG_TREFFERZEILE = GRUNDZUSTAND + """
// „Ähnliche finden" haengt an Vektoren im Index – ohne die waere der Eintrag
// zu Recht gesperrt, und dieser Test prueft die Zeile, nicht die Sperre.
S = statusGeruest(); S.store.semantic = true;
renderHits({count: 2, results: [
  {uid: 'u:1', cid: 7, title: 'Rechnung 4711', who: 'Alice', date: '2026-03-04',
   source_label: 'Mail', preview: 'Text', uri: 'o365://outlook/a.eml', thread: 'x'},
  {uid: 'u:2', cid: 8, title: 'Notiz.pdf', who: '', date: '2026-03-01',
   source_label: 'Datei', preview: 'Pfad'}
]});
var h = document.getElementById('results').innerHTML;

// Datum in eigener Spalte: nur so stehen die Daten untereinander.
pruefe(h.indexOf('class="wann"') >= 0, 'Datum hat keine eigene Spalte');
pruefe(h.indexOf('2026-03-04') >= 0, 'Datum fehlt');

// Ein Menue je Treffer statt Knopfreihen.
pruefe((h.match(/punkte-knopf/g) || []).length === 2, 'Nicht je Treffer ein Menue');
pruefe(h.indexOf('aehnlicheZu(') >= 0, 'Aehnliche finden fehlt im Menue');

// Was fuer diesen Treffer nicht geht, steht ausgegraut drin statt zu fehlen -
// sonst wandern die Eintraege je Treffer an andere Stellen.
var zweites = h.split('id="menu-1"')[1];
pruefe(zweites.indexOf('disabled') >= 0, 'Unmoegliches fehlt statt ausgegraut zu sein');
console.log('OK');
"""


def test_trefferzeile_ist_kompakt_und_hat_ein_menue():
    _in_node(PRUEFUNG_TREFFERZEILE)


PRUEFUNG_MARKIERUNG = GRUNDZUSTAND + """
document.getElementById('q').value = 'Rechnung';
suchmodus('text');
pruefe(hervor('Die Rechnung liegt vor').indexOf('<mark>') >= 0,
       'Textsuche markiert die Fundstelle nicht');
// Bei der Bedeutungssuche waere eine Markierung eine Behauptung: dort passt
// der Sinn, nicht das Wort.
SUCHMODUS = 'aehnlich';
pruefe(hervor('Die Rechnung liegt vor').indexOf('<mark>') < 0,
       'Bedeutungssuche markiert woertlich');
console.log('OK');
"""


def test_markiert_wird_nur_wo_woertlich_getroffen_wurde():
    _in_node(PRUEFUNG_MARKIERUNG)


# --------------------------------------------------------------------------
# Ollama ist optional – im ganzen System
#
# Bis 5.0.0 war Ollama eine stille Voraussetzung: fehlte es, fragte die App
# trotzdem alle zehn Sekunden nach, der Assistent drängte zur Installation, und
# der Indexlauf entschied selbst. Jetzt ist es eine Entscheidung.
# --------------------------------------------------------------------------
def test_abgeschaltet_wird_gar_nicht_erst_gefragt(sandbox, monkeypatch):
    """Der eigentliche Gewinn: ohne Ollama lief bisher dauerhaft alle zehn
    Sekunden ein Verbindungsversuch ins Leere."""
    gefragt = []
    monkeypatch.setattr(app_mod, "check_ollama",
                        lambda *a, **k: gefragt.append(1) or {})
    a = app_mod.App(app_mod.load_config())
    a.cfg["ollama_enabled"] = False
    for _ in range(3):
        zustand = a.ollama(force=True)
    assert gefragt == [], "es wurde trotzdem nach Ollama gesucht"
    assert zustand["disabled"] is True
    assert zustand["running"] is False and zustand["has_model"] is False


def test_abgeschaltet_kein_assistent(sandbox, with_ollama):
    """Wer Ollama abwählt, will nicht bei jedem Start gefragt werden, ob er es
    nicht doch installieren möchte."""
    a = app_mod.App(app_mod.load_config())
    a.cfg["ollama_enabled"] = False
    assert a.status()["wizard"] != "ollama"


def test_abgeschaltet_baut_den_volltextindex(sandbox, with_ollama, monkeypatch, no_ollama):
    """Auch wenn Ollama liefe: abgewählt ist abgewählt."""
    a = app_mod.App(app_mod.load_config())
    a.cfg["ollama_enabled"] = False
    assert a.semantisch_gewollt() is False


def test_volltext_auch_mit_laufendem_ollama(sandbox, with_ollama):
    """Einbetten kostet auf einem echten Bestand eine Stunde. Wer nur exakt
    sucht, soll sie nicht zahlen müssen."""
    a = app_mod.App(app_mod.load_config())
    a.cfg["index_semantic"] = False
    assert a.semantisch_gewollt() is False
    a.cfg["index_semantic"] = True
    assert a.semantisch_gewollt() is True


def test_mcp_bekommt_den_verzicht_mitgeteilt(sandbox):
    """Sonst versucht der Server es bei jeder Anfrage neu und läuft jedes Mal
    in denselben Fehler."""
    cfg = app_mod.load_config()
    cfg["ollama_enabled"] = False
    args = app_mod.mcp_client_config(cfg, 8365)["stdio"]["mcpServers"]["munimentum"]["args"]
    assert "--no-ollama" in args
    cfg["ollama_enabled"] = True
    args = app_mod.mcp_client_config(cfg, 8365)["stdio"]["mcpServers"]["munimentum"]["args"]
    assert "--no-ollama" not in args


def test_schalter_werden_gespeichert(server):
    _, port = server
    code, r = call(port, "POST", "/api/config",
                   {"ollama_enabled": False, "index_semantic": False})
    assert code == 200
    code, s = call(port, "GET", "/api/status")
    assert s["config"]["ollama_enabled"] is False
    assert s["config"]["index_semantic"] is False
    assert s["ollama"]["disabled"] is True, "die Prüfung von vorher wirkt nach"


PRUEFUNG_OLLAMA_AUS = GRUNDZUSTAND + """
var st = statusGeruest();
st.ollama.disabled = true;
st.ollama.running = false; st.ollama.has_model = false; st.ollama.has_chat_model = false;
renderStatus(st);

// Abgeschaltet ist kein Fehler, sondern eine Entscheidung: grau statt rot.
var punkt = document.getElementById('p-ollama');
pruefe(!punkt.className.match(/err|warn/), 'Abgeschaltet wird als Fehler gezeigt: ' + punkt.className);
pruefe(document.getElementById('p-ollama-t').textContent.length > 0, 'Kachel ohne Text');

// Und die Suchvarianten nennen die Ursache, nicht nur die Bedingung.
pruefe(document.getElementById('m-ki').disabled, 'KI-Variante trotz Abschaltung waehlbar');
var hinweis = document.getElementById('modus-fehlt').textContent;
pruefe(hinweis.indexOf('Ollama') >= 0, 'Hinweis nennt Ollama nicht: ' + hinweis);
console.log('OK');
"""


def test_kopfzeile_zeigt_abgeschaltet_nicht_als_fehler():
    _in_node(PRUEFUNG_OLLAMA_AUS)


PRUEFUNG_EINSTELLUNGEN = GRUNDZUSTAND + """
// Der Schalter graut aus, was ohne ihn keine Wirkung hat - versteckt es aber
// nicht: wer die Moeglichkeit nie sieht, vermisst sie auch nie.
document.getElementById('c-ollama_enabled').checked = true;
indexart(true); ollamaSchalter();
pruefe(!document.getElementById('ollama-kinder').classList.contains('aus'),
       'Untergruppen ausgegraut, obwohl Ollama an ist');
pruefe(INDEX_SEMANTISCH === true, 'Indexart nicht gesetzt');

document.getElementById('c-ollama_enabled').checked = false;
ollamaSchalter();
pruefe(document.getElementById('ollama-kinder').classList.contains('aus'),
       'Untergruppen bleiben bedienbar, obwohl Ollama aus ist');
pruefe(document.getElementById('ix-beides').disabled, 'Bedeutung trotz Abschaltung waehlbar');
pruefe(INDEX_SEMANTISCH === false, 'Indexart nicht auf Volltext gefallen');
pruefe(document.getElementById('ollama-folgen').textContent.length > 20,
       'Es wird nicht gesagt, was das Abschalten bedeutet');
console.log('OK');
"""


def test_einstellungen_ollama_schalter():
    _in_node(PRUEFUNG_EINSTELLUNGEN)


def test_jede_einstellung_hat_eine_erklaerung():
    """Die Seite lebt jetzt vom (i): eine Zeile ohne Erklärung ist eine Zahl,
    die niemand anfasst – oder schlimmer, blind umstellt."""
    seite = app_mod.PAGE
    abschnitt = seite[seite.index('<section id="tab-einstellungen"'):seite.index("</section>\n</main>")]
    zeilen = abschnitt.count('class="feldzeile')
    mit_info = abschnitt.count('class="info"')
    assert zeilen >= 25, f"nur {zeilen} Einstellungszeilen gefunden"
    assert mit_info >= zeilen, f"{zeilen} Zeilen, aber nur {mit_info} Erklärungen"


# --------------------------------------------------------------------------
# Auswertungen für die Analytics-Seite
# --------------------------------------------------------------------------
def _index_mit_zeitpunkten(sandbox, monate):
    """Ein kleiner Store, dessen Nachrichten auf bestimmte Monate fallen."""
    from datetime import UTC, datetime

    import corpus
    import rag_index
    chunks = []
    for i, (monat, quelle) in enumerate(monate):
        ts = datetime.strptime(monat + "-15", "%Y-%m-%d").replace(tzinfo=UTC).timestamp()
        c = {"uid": f"u:{i}", "cid": f"u:{i}#0", "src": quelle, "root": quelle,
             "rel": f"{i}.eml", "who": f"Person {i % 2}", "ppl": "p",
             "ts": ts, "date": monat, "title": f"T{i}", "ctx": "x",
             "text": "Inhalt", "att": "vertrag.pdf bild.png" if i % 3 == 0 else None}
        c["hash"] = corpus.chunk_hash(c)
        chunks.append(c)
    rag_index.write_db(sandbox / app_mod.STORE_DIR, chunks)
    return chunks


def test_verlauf_enthaelt_auch_die_leeren_monate(sandbox):
    """Sonst fiele eine Lücke gar nicht auf – sie stünde einfach nicht da."""
    (sandbox / app_mod.STORE_DIR).mkdir(parents=True, exist_ok=True)
    _index_mit_zeitpunkten(sandbox, [("2025-01", "teams"), ("2025-04", "outlook")])
    k = app_mod.kennzahlen(app_mod.load_config())
    monate = [r["m"] for r in k["verlauf"]]
    assert monate == ["2025-01", "2025-02", "2025-03", "2025-04"]
    assert k["verlauf"][1]["gesamt"] == 0
    # Aufsummiert – das ist die Wachstumskurve.
    assert [r["summe"] for r in k["verlauf"]] == [1, 1, 1, 2]


def test_luecken_nur_innerhalb_des_bestands(sandbox):
    """Vor der ersten und nach der letzten Nachricht ist nichts zu vermissen."""
    (sandbox / app_mod.STORE_DIR).mkdir(parents=True, exist_ok=True)
    _index_mit_zeitpunkten(sandbox, [("2025-01", "teams"), ("2025-05", "teams")])
    k = app_mod.kennzahlen(app_mod.load_config())
    assert k["luecken"] == [{"von": "2025-02", "bis": "2025-04", "monate": 3}]


def test_verlauf_trennt_die_quellen(sandbox):
    (sandbox / app_mod.STORE_DIR).mkdir(parents=True, exist_ok=True)
    _index_mit_zeitpunkten(sandbox, [("2025-01", "teams"), ("2025-01", "outlook"),
                                     ("2025-01", "kalender")])
    zeile = app_mod.kennzahlen(app_mod.load_config())["verlauf"][0]
    assert (zeile["teams"], zeile["outlook"], zeile["andere"]) == (1, 1, 1)
    assert zeile["gesamt"] == 3, "die Summe muss die Stapel tragen"


def test_anhangstypen_werden_gezaehlt(sandbox):
    (sandbox / app_mod.STORE_DIR).mkdir(parents=True, exist_ok=True)
    _index_mit_zeitpunkten(sandbox, [("2025-01", "outlook")] * 3)
    typen = {x["typ"]: x["n"] for x in app_mod.kennzahlen(app_mod.load_config())["anhang_typen"]}
    assert typen.get("pdf") == 1 and typen.get("png") == 1


def test_auswertung_wird_gepuffert(sandbox, monkeypatch):
    """Der Gang über den Index kostet auf einem echten Bestand Sekunden – er
    darf nicht bei jedem Öffnen des Reiters neu laufen."""
    (sandbox / app_mod.STORE_DIR).mkdir(parents=True, exist_ok=True)
    _index_mit_zeitpunkten(sandbox, [("2025-01", "teams")])
    cfg = app_mod.load_config()
    app_mod.kennzahlen(cfg)
    laeufe = []
    echt = app_mod.auswertung
    monkeypatch.setattr(app_mod, "auswertung",
                        lambda con, k: laeufe.append(1) or echt(con, k))
    app_mod.kennzahlen(cfg)
    assert len(laeufe) == 1, "die Auswertung wurde aufgerufen"
    assert app_mod._AUSWERTUNG, "aber nichts gepuffert"


# --------------------------------------------------------------------------
# Der Vertrag zwischen Oberfläche und Formularfeldern
#
# Die Einstellungsseite wurde einmal komplett neu geschrieben. Genau dabei
# passiert der Fehler, den kein anderer Test sieht: eine vertippte Kennung, und
# eine Einstellung lässt sich still nicht mehr füllen oder speichern – bemerkt
# wird es erst, wenn jemand sie umstellt und der Wert nach dem Neuladen wieder
# dasteht wie vorher.
# --------------------------------------------------------------------------
def _feldlisten():
    """Die drei Listen, aus denen die Oberfläche Formularfelder liest."""
    quelle = app_mod.PAGE
    listen = {}
    for name in ("SCHALTER", "ZAHLEN", "TEXTE"):
        m = re.search(rf"var {name}\s*=\s*\[(.*?)\];", quelle, re.S)
        assert m, f"{name} nicht gefunden"
        listen[name] = re.findall(r"'([\w_]+)'", m.group(1))
    return listen


def test_jedes_gelistete_feld_gibt_es_auch(sandbox):
    """Jede Kennung in SCHALTER/ZAHLEN/TEXTE muss ein Element haben."""
    fehlt = [k for liste in _feldlisten().values() for k in liste
             if f'id="c-{k}"' not in app_mod.PAGE]
    assert not fehlt, f"kein Bedienelement für: {fehlt}"


def test_jedes_feld_ist_auch_gelistet():
    """Und umgekehrt: ein Element, das in keiner Liste steht, wird nie
    gespeichert – es sieht bedienbar aus und ist es nicht."""
    gelistet = {k for liste in _feldlisten().values() for k in liste}
    # Von Hand behandelt, jeweils mit eigenem Grund.
    ausnahmen = {"skip_folders",      # mehrzeiliger Text, eigene Behandlung
                 "language",          # eigenes Auswahlfeld, fuelleSprachen()
                 "data-dir",          # kein Konfigurationswert, eigener Knopf
                 "ollama_enabled",    # Kippschalter, siehe ollamaSchalter()
                 "onedrive_enabled"}  # steht im Reiter „Exportieren", saveCats()
    im_markup = set(re.findall(r'id="c-([\w_-]+)"', app_mod.PAGE))
    verwaist = im_markup - gelistet - ausnahmen
    assert not verwaist, f"Bedienelemente, die niemand speichert: {sorted(verwaist)}"


def test_jedes_gelistete_feld_wird_auch_serverseitig_angenommen(sandbox, server):
    """Der Weg endet nicht im Browser: was die Oberfläche schickt, muss die
    Konfiguration auch übernehmen."""
    _, port = server
    listen = _feldlisten()
    body = {}
    for k in listen["SCHALTER"]:
        body[k] = False
    for k in listen["ZAHLEN"]:
        body[k] = 7 if k != "mcp_port" else 8400
    code, _ = call(port, "POST", "/api/config", body)
    assert code == 200
    cfg = call(port, "GET", "/api/status")[1]["config"]
    nicht_uebernommen = [k for k in listen["SCHALTER"] if cfg.get(k) is not False]
    assert not nicht_uebernommen, f"Schalter ignoriert: {nicht_uebernommen}"
    nicht_uebernommen = [k for k in listen["ZAHLEN"]
                         if cfg.get(k) not in (7, 8400)]
    assert not nicht_uebernommen, f"Zahlen ignoriert: {nicht_uebernommen}"


# --------------------------------------------------------------------------
# Der Ollama-Schalter, den ganzen Weg entlang
# --------------------------------------------------------------------------
def test_indexschritt_bekommt_ohne_ollama_den_volltextschalter(sandbox, with_ollama,
                                                               monkeypatch):
    """Nicht nur die Absicht zählt – der Unterprozess muss den Schalter tragen.
    Ollama LÄUFT in diesem Test; abgewählt ist abgewählt."""
    monkeypatch.setattr(app_mod, "read_token", lambda: make_jwt(exp=time.time() + 3600, scp='Mail.Read User.Read'))
    a = app_mod.App(app_mod.load_config())
    a.cfg["ollama_enabled"] = False
    schritte = app_mod.build_steps(a.cfg, index=True,
                                   embeddings=a.semantisch_gewollt())
    index = [s for s in schritte if s["key"] == "index"][0]
    assert "--no-embeddings" in index["argv"]


def test_indexschritt_mit_ollama_bettet_ein(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    schritte = app_mod.build_steps(a.cfg, index=True,
                                   embeddings=a.semantisch_gewollt())
    index = [s for s in schritte if s["key"] == "index"][0]
    assert "--no-embeddings" not in index["argv"]


def test_zeitplan_laeuft_auch_ohne_ollama(sandbox, with_ollama, monkeypatch):
    """Ein nächtlicher Lauf soll den Volltextindex bauen statt zu scheitern."""
    monkeypatch.setattr(app_mod, "read_token", lambda: make_jwt(exp=time.time() + 3600, scp='Mail.Read User.Read'))
    gestartet = {}
    a = app_mod.App(app_mod.load_config())
    a.cfg["ollama_enabled"] = False
    a.cfg["schedule"].update(enabled=True, interval_minutes=5, index=True,
                             outlook=False, teams=False, calendar=False)
    monkeypatch.setattr(a.jobs, "start",
                        lambda steps, label: gestartet.update(steps=steps) or True)
    a.scheduler.letzter = 0
    a.scheduler._tick()
    index = [s for s in gestartet.get("steps", []) if s["key"] == "index"]
    assert index, "der Zeitplan hat gar nicht indiziert"
    assert "--no-embeddings" in index[0]["argv"]


PRUEFUNG_AUSGEGRAUT = GRUNDZUSTAND + """
// Ausgegraut, nicht versteckt: wer die Moeglichkeit nie sieht, erfaehrt auch
// nie, dass es sie gibt - und sucht sie beim naechsten Mal woanders.
document.getElementById('c-ollama_enabled').checked = false;
ollamaSchalter();
['ollama-kinder', 'ix-beides', 'c-embed_model', 'c-chat_model'].forEach(function(id){
  var e = document.getElementById(id);
  pruefe(!e.classList.contains('hide'), id + ' wurde versteckt statt ausgegraut');
});
pruefe(document.getElementById('ollama-kinder').classList.contains('aus'),
       'nicht ausgegraut');

// Und wieder an: alles zurueck, ohne Neuladen.
document.getElementById('c-ollama_enabled').checked = true;
ollamaSchalter();
pruefe(!document.getElementById('ollama-kinder').classList.contains('aus'),
       'bleibt ausgegraut, obwohl Ollama wieder an ist');
pruefe(!document.getElementById('ix-beides').disabled, 'Bedeutung bleibt gesperrt');
console.log('OK');
"""


def test_ausgegraut_statt_versteckt():
    _in_node(PRUEFUNG_AUSGEGRAUT)


PRUEFUNG_KACHEL_ZIEL = GRUNDZUSTAND + """
// Abgeschaltet gibt es nichts einzurichten - die Kachel fuehrt dann dorthin,
// wo man es wieder anschalten kann, statt zum Installations-Assistenten.
var gewechselt = [];
tab = function(n){ gewechselt.push(n); };
S = statusGeruest(); S.ollama.disabled = true;
ollamaKachel();
pruefe(gewechselt.indexOf('einstellungen') >= 0,
       'Kachel fuehrt nicht zu den Einstellungen: ' + gewechselt.join(','));
pruefe(!wizardOffen, 'Assistent ging trotzdem auf');

// Laeuft Ollama nur gerade nicht, ist der Assistent richtig.
S.ollama.disabled = false;
ollamaKachel();
pruefe(wizardOffen === 'ollama', 'Assistent fehlt, obwohl Ollama nur fehlt');
console.log('OK');
"""


def test_ollama_kachel_fuehrt_ans_richtige_ziel():
    _in_node(PRUEFUNG_KACHEL_ZIEL)


PRUEFUNG_RUNDREISE = GRUNDZUSTAND + """
// Eine Konfiguration hineingeben und wieder herausholen: was die Oberfléche
// nicht zurueckgibt, kann der Nutzer nicht speichern.
var cfg = statusGeruest().config;
cfg.ollama_enabled = true; cfg.index_semantic = false;
cfg.workers = 6; cfg.embed_model = 'bge-m3'; cfg.chat_model = 'qwen3.6:27b';
cfg.ollama = 'http://x:1'; cfg.search_results = 25; cfg.semantic_min = 55;
cfg.answer_sources = 3; cfg.index_batch = 32; cfg.mcp_port = 8400;
cfgGefuellt = false;
fuelleEinstellungen(cfg);

// Der DOM-Stummel wandelt beim Zuweisen nicht in Text um, der Browser schon.
pruefe(String(document.getElementById('c-workers').value) === '6', 'Zahl nicht gefuellt');
pruefe(document.getElementById('c-embed_model').value === 'bge-m3', 'Text nicht gefuellt');
pruefe(document.getElementById('c-ollama_enabled').checked === true, 'Schalter nicht gefuellt');
pruefe(INDEX_SEMANTISCH === false, 'Indexart nicht uebernommen: ' + INDEX_SEMANTISCH);
pruefe(document.getElementById('ix-text').classList.contains('on'),
       'Umschalter zeigt die falsche Seite');

// Und zurueck: speichern muss jeden Wert mitschicken.
var geschickt = null;
post = function(pfad, body){ geschickt = body; return Promise.resolve({ok: true}); };
S = statusGeruest();
speichereEinstellungen();
['workers','embed_model','ollama_enabled','index_semantic','mcp_port','semantic_min']
  .forEach(function(k){
    pruefe(geschickt[k] !== undefined, 'nicht mitgeschickt: ' + k);
  });
pruefe(geschickt.index_semantic === false, 'Indexart falsch gespeichert');
console.log('OK');
"""


def test_einstellungen_hin_und_zurueck():
    _in_node(PRUEFUNG_RUNDREISE)


PRUEFUNG_AEHNLICHE_GESPERRT = GRUNDZUSTAND + """
function zeichne(semantisch){
  S = statusGeruest();
  S.store.semantic = semantisch;
  renderHits({count: 1, results: [{uid: 'u:1', cid: 7, title: 'T', who: 'A',
    date: '2026-03-04', source_label: 'Datei', preview: 'p'}]});
  return document.getElementById('results').innerHTML;
}

// Mit Vektoren im Index ist der Eintrag bedienbar - auch wenn Ollama gerade
// abgeschaltet ist: eingebettet wird dabei nichts, der Vektor liegt schon da.
var mit = zeichne(true);
pruefe(mit.indexOf('aehnlicheZu(') >= 0, 'Aehnliche finden fehlt trotz Vektoren');

// Ohne Vektoren liefe der Aufruf ins Leere. Ausgegraut statt verschwunden -
// sonst sucht man den Eintrag beim naechsten Mal an anderer Stelle.
var ohne = zeichne(false);
var menue = ohne.split('id="menu-0"')[1].split('</div>')[0];
pruefe(menue.indexOf('aehnlicheZu(') < 0, 'Aehnliche finden ist noch anklickbar');
pruefe(menue.indexOf('Find similar') >= 0 || menue.indexOf('hnliche finden') >= 0,
       'Der Eintrag verschwand ganz statt auszugrauen');
pruefe(menue.indexOf('disabled') >= 0, 'nicht gesperrt');
pruefe(menue.indexOf('title=') >= 0, 'kein Grund genannt');
console.log('OK');
"""


def test_aehnliche_finden_haengt_an_den_vektoren():
    """Nicht an Ollama: der Vektor der Textstelle liegt im Index. Ohne
    Vektoren – ein reiner Volltextindex – liefe der Eintrag ins Leere."""
    _in_node(PRUEFUNG_AEHNLICHE_GESPERRT)
