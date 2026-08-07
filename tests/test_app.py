"""Tests für app.py – Oberfläche, Assistenten, Läufe, Zeitplan, MCP, Suche.

Es geht nie ins Netz: Graph wird gar nicht angesprochen (die App startet nur
die Export-Skripte als Unterprozesse, hier durch kurze python -c-Aufrufe
ersetzt), Ollama wird gemockt. Der Suchteil läuft gegen einen echten kleinen
Store, den rag_index.py schreibt – damit stimmt das Schema garantiert.

app.BASE, app.CONFIG_FILE und app.TOKEN_FILE zeigen in jedem Test auf tmp_path,
damit nichts im Projektordner landet.
"""

import base64
import http.client
import json
import sqlite3
import sys
import threading
import time

import pytest

import app as app_mod
import corpus
import rag_index


# --------------------------------------------------------------------------
# Hilfen
# --------------------------------------------------------------------------
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
    assert "nur Volltext" in steps[0]["label"]


def test_build_steps_ohne_token_setzt_keine_variable(sandbox):
    steps = app_mod.build_steps(app_mod.load_config(), index=True)
    assert "GRAPH_TOKEN" not in steps[0]["env"]


def test_build_steps_leere_auswahl(sandbox):
    assert app_mod.build_steps(app_mod.load_config()) == []


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
    text = "\n".join(ln["text"] for ln in r.lines)
    assert "eins" in text and "zwei" in text
    assert r.last["ok"] and r.last["label"] == "Lauf"
    assert text.index("eins") < text.index("zwei")


def test_jobrunner_bricht_bei_fehler_ab(sandbox):
    r = app_mod.JobRunner()
    r.start([_py_step("raise SystemExit(3)", "Kaputt"), _py_step("print('nie')", "B")], "Lauf")
    _warte(r)
    text = "\n".join(ln["text"] for ln in r.lines)
    assert "nie" not in text                              # zweiter Schritt lief nicht
    assert not r.last["ok"] and "Code 3" in r.last["detail"]


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
    assert any("Start fehlgeschlagen" in ln["text"] for ln in r.lines)


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
    assert not ok and "Token" in why


def test_launch_ohne_auswahl(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    ok, why = a.launch()
    assert not ok and "Nichts" in why


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
    assert any("Ollama nicht verfügbar" in ln["text"] for ln in a.jobs.lines)


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
    assert not ok and "bereits" in why


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
# Status und Assistenten-Steuerung
# --------------------------------------------------------------------------
def test_status_zeigt_token_assistenten_ohne_token(sandbox, with_ollama):
    a = app_mod.App(app_mod.load_config())
    assert a.status()["wizard"] == "token"


def test_status_zeigt_assistenten_bei_jedem_start(sandbox, with_ollama):
    """Anforderung: bei jedem Start einmal aufpoppen, auch mit gültigem Token."""
    app_mod.write_token(make_jwt(exp=time.time() + 3600))
    a = app_mod.App(app_mod.load_config())
    assert a.wizard_pending is True
    assert a.status()["wizard"] == "token"
    a.wizard_pending = False
    assert a.status()["wizard"] is None


def test_status_zeigt_ollama_assistenten_wenn_token_passt(sandbox, no_ollama):
    app_mod.write_token(make_jwt(exp=time.time() + 3600,
                                 scp="Mail.Read Calendars.Read Contacts.Read Chat.Read"))
    a = app_mod.App(app_mod.load_config())
    a.wizard_pending = False
    assert a.status()["wizard"] == "ollama"


def test_status_zeigt_token_assistenten_nach_abgelaufenem_lauf(sandbox, with_ollama):
    app_mod.write_token(make_jwt(exp=time.time() + 3600,
                                 scp="Mail.Read Calendars.Read Contacts.Read Chat.Read"))
    a = app_mod.App(app_mod.load_config())
    a.wizard_pending = False
    assert a.status()["wizard"] is None
    a.jobs.token_expired = True
    assert a.status()["wizard"] == "token"


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
    assert gestartet["index"] is True and gestartet["label"] == "Geplanter Lauf"


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
    assert any("kein gültiger Token" in ln["text"] for ln in a.jobs.lines)


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
    assert not ok and "Kein Index" in why
    assert a.mcp.status(a.cfg)["running"] is False


def test_mcp_status_nennt_die_url(sandbox):
    a = app_mod.App(app_mod.load_config())
    a.cfg["mcp_port"] = 8899
    assert a.mcp.status(a.cfg)["url"] == "http://127.0.0.1:8899/mcp"


def test_autostart_mcp_meldet_fehlenden_index(sandbox):
    a = app_mod.App(app_mod.load_config())
    a.autostart_mcp()
    assert any("MCP nicht gestartet" in ln["text"] for ln in a.jobs.lines)


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
    assert ok and why == "gestartet"
    assert a.mcp.running and a.mcp.status(a.cfg)["port"] == a.cfg["mcp_port"]

    argv = fake_popen[0].argv
    assert argv[1].endswith("mcp_server.py")
    assert "--store" in argv and "rag_store" in argv
    assert "--port" in argv

    assert a.mcp.start(a.cfg) == (True, "läuft bereits")    # kein zweiter Prozess
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
    assert not ok and "Start fehlgeschlagen" in why


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
    assert "Kein Index" in b.error


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
    assert a.wizard_pending is False


def test_http_token_abgelaufen_wird_gemeldet(server):
    _, port = server
    code, r = call(port, "POST", "/api/token", {"token": make_jwt(exp=time.time() - 10)})
    assert not r["ok"] and "abgelaufen" in r["message"]


def test_http_token_fehlende_rechte_werden_benannt(server):
    _, port = server
    code, r = call(port, "POST", "/api/token",
                   {"token": make_jwt(exp=time.time() + 3600, scp="Mail.Read")})
    assert r["ok"] and "Calendars.Read" in r["message"]


def test_http_token_muell_wird_abgelehnt(server):
    _, port = server
    assert call(port, "POST", "/api/token", {"token": ""})[1]["ok"] is False
    assert call(port, "POST", "/api/token", {"token": "zu-kurz"})[1]["ok"] is False


def test_http_wizard_seen(server):
    a, port = server
    a.jobs.token_expired = True
    call(port, "POST", "/api/wizard-seen")
    assert a.wizard_pending is False and a.jobs.token_expired is False


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
    assert not r["ok"] and "Kein Index" in r["message"]
    assert call(port, "POST", "/api/mcp", {"action": "quatsch"})[1]["ok"] is False


def test_http_log(server):
    a, port = server
    a.jobs.log("hallo")
    code, r = call(port, "GET", "/api/log?since=0")
    assert code == 200 and r["lines"][-1]["text"] == "hallo"
    assert call(port, "GET", f"/api/log?since={r['seq']}")[1]["lines"] == []


def test_http_suche_ohne_index_meldet_das(server):
    _, port = server
    code, r = call(port, "GET", "/api/search?q=test")
    assert code == 200 and r["hits"] == [] and "Kein Index" in r["error"]


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


def test_main_reicht_argumente_an_serve_weiter(monkeypatch):
    gesehen = {}
    monkeypatch.setattr(app_mod, "serve",
                        lambda a, port, open_browser=True: gesehen.update(
                            port=port, browser=open_browser))
    monkeypatch.setattr(sys, "argv", ["app.py", "--port", "9001", "--no-browser"])
    app_mod.main()
    assert gesehen == {"port": 9001, "browser": False}
