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
                        lambda url, model, timeout=1.5: {
                            "running": False, "models": [], "has_model": False,
                            "error": "ConnectionError", "model": model, "url": url})


@pytest.fixture
def with_ollama(monkeypatch):
    monkeypatch.setattr(app_mod, "check_ollama",
                        lambda url, model, timeout=1.5: {
                            "running": True, "models": [model], "has_model": True,
                            "error": None, "model": model, "url": url})


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
    assert cfg["teams_dir"] == "teams_export"


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
    out = app_mod.check_ollama("http://x", "bge-m3")
    assert out["running"] and out["has_model"]
    assert out["models"] == ["bge-m3:latest", "qwen:7b"]


def test_check_ollama_modell_fehlt(monkeypatch):
    class R:
        def raise_for_status(self): pass
        def json(self): return {"models": [{"name": "qwen:7b"}]}
    monkeypatch.setattr("requests.get", lambda *a, **k: R())
    out = app_mod.check_ollama("http://x", "bge-m3")
    assert out["running"] and not out["has_model"]


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
    assert steps[0]["argv"][2] == "outlook_export"        # Ausgabeordner
    assert "--no-embeddings" not in steps[2]["argv"]


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
    return {"key": "t", "label": label, "argv": [sys.executable, "-c", code], "env": {}}


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
        self.stdout = io.BytesIO(b"office365-export MCP: 3 chunks\n")
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
    assert "--store" in argv and "rag_store" in argv
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
    assert any("[MCP] office365-export MCP" in ln["text"] for ln in a.jobs.lines)
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
    assert "Microsoft-365-Archiv" in body


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
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Microsoft365-Archiv"))
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
    monkeypatch.delenv("OFFICE365_DATA_DIR", raising=False)
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
    monkeypatch.setenv("OFFICE365_DATA_DIR", str(tmp_path))
    assert app_mod.data_dir() == tmp_path.resolve()


def test_data_dir_als_skript_ist_der_projektordner(monkeypatch):
    monkeypatch.setattr(app_mod, "FROZEN", False)
    monkeypatch.delenv("OFFICE365_DATA_DIR", raising=False)
    assert app_mod.data_dir() == Path(app_mod.__file__).resolve().parent


def test_mcp_client_config_nennt_absolute_pfade(sandbox):
    cfg = app_mod.load_config()
    conf = app_mod.mcp_client_config(cfg, 8365)
    assert conf["http"]["mcpServers"]["office365-export"]["url"] \
        == "http://127.0.0.1:8365/mcp"
    args = conf["stdio"]["mcpServers"]["office365-export"]["args"]
    assert "--transport" in args and "stdio" in args
    # Claude startet den Befehl in einem unbekannten Arbeitsverzeichnis
    store = args[args.index("--store") + 1]
    assert Path(store).is_absolute() and store.startswith(str(sandbox))


def test_mcp_client_config_gebuendelt(sandbox, frozen):
    conf = app_mod.mcp_client_config(app_mod.load_config(), 8365)
    eintrag = conf["stdio"]["mcpServers"]["office365-export"]
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
  classList: {add: function(){}, remove: function(){}, toggle: function(){}},
  appendChild: function(){}, removeChild: function(){}, firstChild: null,
  addEventListener: function(){}, querySelector: function(){ return null; },
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

global.document = {
  documentElement: {},
  title: '',
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
    if(sel.indexOf('[data-tab]') >= 0){
      var n = mk('tabbtn'); n.dataset = {tab: global.aktiverTab || 'export'}; return n;
    }
    for(var muster in global.vorhanden){
      if(sel.indexOf(muster) >= 0) return global.vorhanden[muster];
    }
    return null;
  },
  querySelectorAll: function(){ return []; },
  createElement: function(){ return mk('x'); },
};
global.aktiverTab = 'export';
global.vorhanden = {'.rbcount': mk('rbcount'), 'main': mk('main'), 'nav': mk('nav')};
global.setInterval = function(){ return 0; };
// setTimeout echt lassen: die Kalenderpruefung wartet auf Promises.
global.alert = function(){};
"""

GRUNDZUSTAND = """
S = {token: {present: true, valid: true, expired: false, missing: [],
             account: 'a@example.com', expires_in_minutes: 620},
     ollama: {running: true, has_model: false, model: 'bge-m3', models: []},
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
pruefe(modal.innerHTML.indexOf('Access Token') >= 0, 'Assistent nicht gezeichnet');
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
pruefe(modal.innerHTML.indexOf('fehlen diese Berechtigungen') >= 0,
       'Zustandswechsel kam im Text nicht an');
pruefe(document.getElementById('tok').value === 'EINGEFUEGTER-TOKEN',
       'Eingabe ging beim Neuzeichnen verloren');

closeWizard('token');
openWizard('token');
pruefe(modal.innerHTML.indexOf('Access Token') >= 0, 'Nach Schliessen nicht gezeichnet');
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
aktiverTab = 'kalender';
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
  aktiverTab = 'adressbuch';
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

# renderStatus liest viel mehr aus dem Status als die Assistenten – ein
# vollstaendiges Geruest, damit der Aufruf oben durchlaeuft.
STATUS_GERUEST = """
function statusGeruest(){
  return {token: S.token, ollama: S.ollama, ollama_hint: S.ollama_hint,
          scopes_needed: S.scopes_needed, scope_queries: S.scope_queries,
          graph_explorer: S.graph_explorer, data_dir: '/tmp/daten', frozen: false,
          store: {exists: true, chunks: 5, semantic: false, built_at: null, model: null},
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


def test_jeder_reiter_liegt_im_hauptbereich():
    """Regression: der Einstellungen-Abschnitt stand hinter </main> und bekam
    damit weder Innenabstand noch Maximalbreite – seine Karten klebten am
    Fensterrand, anders als bei allen anderen Reitern."""
    seite = app_mod.PAGE
    haupt = seite[seite.index("<main>"):seite.index("</main>")]
    for reiter in ("export", "suche", "kalender", "adressbuch", "zeitplan",
                   "mcp", "einstellungen"):
        assert f'<section id="tab-{reiter}"' in haupt, f"{reiter} liegt außerhalb <main>"


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
