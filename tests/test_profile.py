"""Profiles – every one its own archive.

The app folder is the first profile, the one every install already has;
every further one lives in profiles/<name>/ with its own configuration,
key, run history, exports and index. Nothing is ever copied or moved
between them. These tests cover the folder rules (settings.py), the model,
the chooser and the switch (app.py), the guard against two profiles in one
folder, and the MCP server's --profile.
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

import app as app_mod
import mcp_server
import settings
from hilfen import call


def profil_mit_config(wurzel, name, **cfg):
    """A further profile below the app folder – with a configuration when
    the test needs one."""
    ordner = wurzel / settings.PROFIL_ORDNER / name
    ordner.mkdir(parents=True)
    if cfg:
        (ordner / settings.CONFIG_NAME).write_text(json.dumps(cfg), encoding="utf-8")
    return ordner


@pytest.fixture
def wurzel(tmp_path, monkeypatch):
    """The app folder in tmp_path, running profiles/standard/, no others yet."""
    for n in ("MUNIMENTUM_DATA_DIR", "OFFICE365_DATA_DIR", "MUNIMENTUM_PROFILE"):
        monkeypatch.delenv(n, raising=False)
    heim = tmp_path / settings.PROFIL_ORDNER / settings.STANDARD_PROFIL
    heim.mkdir(parents=True)
    monkeypatch.setenv("MUNIMENTUM_HOME", str(heim))
    monkeypatch.setattr(app_mod, "WURZEL", tmp_path)
    monkeypatch.setattr(app_mod, "HEIM", heim)
    monkeypatch.setattr(app_mod, "BASE", heim / "data")
    monkeypatch.setattr(app_mod, "STORE_PFAD", heim / app_mod.STORE_DIR)
    monkeypatch.setattr(app_mod, "CONFIG_FILE", heim / settings.CONFIG_NAME)
    monkeypatch.setattr(app_mod, "TOKEN_FILE", heim / "gx_token.txt")
    monkeypatch.setattr(app_mod, "PROFIL", settings.STANDARD_PROFIL)
    monkeypatch.setattr(app_mod, "_UMZUG", {})
    settings.reset()
    yield tmp_path
    settings.reset()


def heim_von(wurzel, name=settings.STANDARD_PROFIL):
    return wurzel / settings.PROFIL_ORDNER / name


@pytest.fixture
def server(wurzel, with_ollama):
    (heim_von(wurzel) / "data").mkdir(exist_ok=True)
    a = app_mod.App(app_mod.load_config())
    httpd = app_mod.make_server(a, 0)
    threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.05), daemon=True).start()
    yield a, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


# --------------------------------------------------------------------------
# settings.py – where a profile lives
# --------------------------------------------------------------------------
def test_profil_ordner_liegt_immer_unter_profiles(tmp_path):
    """No profile is the app folder itself – not even the first one."""
    std = tmp_path / "profiles" / "standard"
    assert settings.profil_ordner("", tmp_path) == std
    assert settings.profil_ordner(None, tmp_path) == std
    assert settings.profil_ordner(" Standard ", tmp_path) == std
    assert settings.profil_ordner("nordwind", tmp_path) == tmp_path / "profiles" / "nordwind"
    assert settings.profil_ordner("Nordwind", tmp_path) == tmp_path / "profiles" / "nordwind"


@pytest.mark.parametrize("name", ["nord wind", "alice@example.com", "-x", "a" * 33,
                                  "../x", "nord/wind", "nord.wind", "ünder"])
def test_profil_ordner_verlangt_einen_slug(tmp_path, name):
    """Names land in paths and on command lines – nothing but a slug."""
    with pytest.raises(ValueError):
        settings.profil_ordner(name, tmp_path)


def test_profil_namen_alphabetisch_oder_das_erste(tmp_path):
    """Nothing there yet: the implied first profile, which the first start
    creates. Otherwise exactly the folders, by name."""
    assert settings.profil_namen(tmp_path) == ["standard"]
    (tmp_path / "profiles").mkdir()
    assert settings.profil_namen(tmp_path) == ["standard"]
    for n in ("zeta", "alpha"):
        (tmp_path / "profiles" / n).mkdir(parents=True)
    (tmp_path / "profiles" / "Nicht Gueltig").mkdir()
    (tmp_path / "profiles" / "datei.txt").write_text("x", encoding="utf-8")
    assert settings.profil_namen(tmp_path) == ["alpha", "zeta"]


def test_app_wurzel_als_skript_ist_der_projektordner(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert settings.app_wurzel() == Path(settings.__file__).resolve().parent


# --------------------------------------------------------------------------
# app.py – the model
# --------------------------------------------------------------------------
def test_profil_status_mit_einem_profil(wurzel):
    st = app_mod.profil_status()
    assert st["name"] == "standard"
    assert st["moeglich"] is True and st["mehrere"] is False
    assert st["ohne_nachfrage"] is False and st["zuletzt"] is None
    assert [p["name"] for p in st["alle"]] == ["standard"]
    p = st["alle"][0]
    assert p["aktiv"] is True and p["ordner"] == str(heim_von(wurzel))
    assert p["konto"] is None and p["index"] is False and p["last_run"] is None


def test_profil_info_nennt_konto_stand_und_index(wurzel, monkeypatch):
    heim = profil_mit_config(wurzel, "nordwind")
    (heim / "gx_token.txt").write_text("irgendein-token\n", encoding="utf-8")
    monkeypatch.setattr(app_mod, "token_status",
                        lambda token, now=None, needed=(): {"account": "alice@example.com"})
    (heim / "runs.db").write_bytes(b"")
    (heim / app_mod.STORE_DIR).mkdir()
    app_mod.store_layout.db_path(heim / app_mod.STORE_DIR).write_bytes(b"")
    info = app_mod.profil_info("nordwind")
    assert info["konto"] == "alice@example.com"
    assert info["index"] is True and info["last_run"]
    assert info["aktiv"] is False
    assert info["ordner"] == str(heim)


def test_profil_konto_aus_der_anmeldung(wurzel, monkeypatch):
    """A profile signed in by device login has no pasted key – its account
    comes from the sign-in cache of THAT folder, not this process's."""
    heim = profil_mit_config(wurzel, "nordwind", client_id="abc", tenant="org")
    gefragt = []
    monkeypatch.setattr(app_mod.auth, "angemeldet",
                        lambda client=None, mandant=None, heim=None:
                        gefragt.append((client, mandant, heim)) or "bob@example.com")
    assert app_mod.profil_konto(heim) == "bob@example.com"
    assert gefragt == [("abc", "org", heim)]
    assert app_mod.profil_info("nordwind")["konto"] == "bob@example.com"
    assert len(gefragt) == 2
    # A pasted key wins, and the sign-in is not even asked.
    (heim / "gx_token.txt").write_text("irgendein-token\n", encoding="utf-8")
    monkeypatch.setattr(app_mod, "token_status",
                        lambda token, now=None, needed=(): {"account": "alice@example.com"})
    assert app_mod.profil_konto(heim) == "alice@example.com"
    assert len(gefragt) == 2


def test_profil_anlegen_prueft_den_namen(wurzel):
    for schlecht in ("", None, "Alice Beispiel", "alice@example.com", "-a",
                     "con", "nul", "com1"):                  # Windows device names
        info, fehler = app_mod.profil_anlegen(schlecht)
        assert info is None and fehler["k"] == "srv.profile.badname", schlecht
    info, fehler = app_mod.profil_anlegen("standard")      # exists: the running one
    assert info is None and fehler["k"] == "srv.profile.exists"
    info, fehler = app_mod.profil_anlegen(" Nordwind ")
    assert fehler is None and info["name"] == "nordwind"
    assert (wurzel / "profiles" / "nordwind").is_dir()
    assert info["aktiv"] is False
    assert app_mod.profil_namen() == ["nordwind", "standard"]
    info, fehler = app_mod.profil_anlegen("nordwind")
    assert info is None and fehler == {"k": "srv.profile.exists", "v": {"name": "nordwind"}}
    # Nothing was copied: the new profile starts empty.
    assert list((wurzel / "profiles" / "nordwind").iterdir()) == []


def test_ohne_profile_unter_festem_datenordner(wurzel, monkeypatch):
    """--data-dir / MUNIMENTUM_DATA_DIR is the all-in-one override: one
    archive, no profiles – even when profile folders exist."""
    profil_mit_config(wurzel, "nordwind")
    monkeypatch.setenv("MUNIMENTUM_DATA_DIR", str(wurzel))
    assert app_mod.profile_moeglich() is False
    assert app_mod.profil_namen() == ["standard"]
    info, fehler = app_mod.profil_anlegen("beratung")
    assert info is None and fehler["k"] == "srv.profile.impossible"
    st = app_mod.profil_status()
    assert st["moeglich"] is False and st["mehrere"] is False


def test_profil_geteilt_erkennt_gemeinsame_ordner(wurzel):
    heim = heim_von(wurzel)
    gemeinsam = wurzel / "gemeinsam"
    profil_mit_config(wurzel, "nordwind", data_dir=str(gemeinsam))
    assert app_mod.profil_geteilt("standard", gemeinsam, heim / "rag_store") == "nordwind"
    # The index folder into the other one's data folder is just as wrong.
    assert app_mod.profil_geteilt("standard", heim / "data", gemeinsam) == "nordwind"
    # The other profile's default index folder counts too.
    assert app_mod.profil_geteilt(
        "standard", heim / "data", wurzel / "profiles" / "nordwind" / "rag_store") == "nordwind"
    assert app_mod.profil_geteilt("standard", heim / "data", heim / "rag_store") is None
    # Nested is shared too: an export tree inside another one's would be
    # walked by that one's exporter.
    assert app_mod.profil_geteilt("standard", gemeinsam / "unten", heim / "rag_store") == "nordwind"
    assert app_mod.profil_geteilt(
        "standard", heim / "data", wurzel / "profiles" / "nordwind" / "rag_store" / "tief") == "nordwind"
    assert app_mod.profil_geteilt("standard", gemeinsam.parent, heim / "rag_store") == "nordwind"
    # A profile never collides with itself.
    assert app_mod.profil_geteilt("nordwind", gemeinsam, wurzel / "x") is None


def test_profil_register(wurzel):
    assert app_mod.profil_register_lesen() == {}
    (wurzel / "profiles.json").write_text("kaputt", encoding="utf-8")
    assert app_mod.profil_register_lesen() == {}
    app_mod.profil_register_schreiben(zuletzt="nordwind")
    app_mod.profil_register_schreiben(ohne_nachfrage=True)
    assert app_mod.profil_register_lesen() == {"zuletzt": "nordwind", "ohne_nachfrage": True}
    # Outside every profile: the register is the app folder's, not a profile's.
    assert (wurzel / "profiles.json").exists()


def test_set_profil_zeigt_auf_den_profilordner(wurzel):
    profil_mit_config(wurzel, "nordwind", index_dir=str(wurzel / "anderswo"))
    heim = app_mod.set_profil("nordwind")
    assert heim == wurzel / "profiles" / "nordwind"
    assert app_mod.PROFIL == "nordwind" and app_mod.HEIM == heim
    assert app_mod.CONFIG_FILE == heim / settings.CONFIG_NAME
    assert app_mod.TOKEN_FILE == heim / "gx_token.txt"
    assert app_mod.BASE == heim / "data"
    assert app_mod.STORE_PFAD == (wurzel / "anderswo").resolve()
    # In-process readers and every subprocess find the profile's files.
    assert os.environ["MUNIMENTUM_HOME"] == str(heim)
    assert settings.config_path() == heim / settings.CONFIG_NAME
    # Back to the first profile: its own folder, never the app folder.
    assert app_mod.set_profil("standard") == heim_von(wurzel)
    assert app_mod.PROFIL == "standard" and app_mod.BASE == heim_von(wurzel) / "data"


# --------------------------------------------------------------------------
# Which profile a start runs
# --------------------------------------------------------------------------
def test_profil_waehlen_ohne_zweites_profil_fragt_nicht(wurzel, monkeypatch):
    monkeypatch.setattr(app_mod, "waehle_profil_im_browser",
                        lambda *a: pytest.fail("gefragt, obwohl es nur eines gibt"))
    assert app_mod.profil_waehlen(None, 8700, True) == ("standard", False)


def test_profil_waehlen_mit_namen(wurzel, monkeypatch):
    profil_mit_config(wurzel, "nordwind")
    monkeypatch.setattr(app_mod, "waehle_profil_im_browser",
                        lambda *a: pytest.fail("gefragt trotz Namen"))
    assert app_mod.profil_waehlen(" Nordwind", 8700, True) == ("nordwind", False)
    assert app_mod.profil_register_lesen()["zuletzt"] == "nordwind"
    with pytest.raises(SystemExit):
        app_mod.profil_waehlen("fremd", 8700, True)


def test_profil_waehlen_fragt_bei_mehreren(wurzel, monkeypatch):
    profil_mit_config(wurzel, "nordwind")
    gefragt = []
    monkeypatch.setattr(app_mod, "waehle_profil_im_browser",
                        lambda port, browser:
                        gefragt.append((port, browser)) or ("nordwind", True))
    assert app_mod.profil_waehlen(None, 8700, False) == ("nordwind", True)
    assert gefragt == [(8700, False)]
    # "Open without asking" plus a last one: no question.
    app_mod.profil_register_schreiben(zuletzt="nordwind", ohne_nachfrage=True)
    assert app_mod.profil_waehlen(None, 8700, False) == ("nordwind", False)
    assert len(gefragt) == 1
    # ... unless the last one is gone.
    app_mod.profil_register_schreiben(zuletzt="weg")
    assert app_mod.profil_waehlen(None, 8700, False) == ("nordwind", True)
    assert len(gefragt) == 2


def test_profilwahl_im_browser(wurzel, monkeypatch):
    """The chooser is a small server of its own on the app's port: it lists,
    creates, and ends with the choice – the app takes the port after it."""
    profil_mit_config(wurzel, "nordwind")
    httpd = app_mod.chooser_server(0)
    port = httpd.server_address[1]
    ergebnis = {}

    def lauf():
        ergebnis["wahl"] = app_mod._wahl_abwarten(httpd, False)

    faden = threading.Thread(target=lauf, daemon=True)
    faden.start()
    # The app's own guard: only our loopback address gets an answer.
    code, _ = call(port, "GET", "/", host="evil.example.com")
    assert code == 403
    code, html = call(port, "GET", "/")
    assert code == 200 and html.lstrip().lower().startswith("<!doctype html>")
    assert "/*__PROFIL__*/" not in html and "__TITEL__" not in html
    assert "<title>Welches Archiv?</title>" in html
    code, st = call(port, "GET", "/api/status")
    assert st == {"chooser": True}                    # not the app – yet
    code, pr = call(port, "GET", "/api/profiles")
    assert [p["name"] for p in pr["alle"]] == ["nordwind", "standard"]
    # The chooser chooses; creating lives in the app's settings.
    code, r = call(port, "POST", "/api/profiles", {"name": "beratung"})
    assert code == 404 and not (wurzel / "profiles" / "beratung").exists()
    code, r = call(port, "POST", "/api/profile-open", {"name": "fremd"})
    assert code == 400 and r["message"]["k"] == "srv.profile.unknown"
    assert faden.is_alive()
    code, r = call(port, "POST", "/api/profile-open",
                    {"name": "nordwind", "ohne_nachfrage": True})
    assert code == 200 and r == {"ok": True}
    faden.join(10)
    assert not faden.is_alive()
    assert ergebnis["wahl"] == ("nordwind", True)     # the browser is already open
    assert app_mod.profil_register_lesen() == {"zuletzt": "nordwind", "ohne_nachfrage": True}
    st = app_mod.profil_status()
    assert st["zuletzt"] == "nordwind" and st["ohne_nachfrage"] is True
    # A last one that no longer exists is not offered.
    app_mod.profil_register_schreiben(zuletzt="weg")
    assert app_mod.profil_status()["zuletzt"] is None
    # With the profile open in another instance the page is sent there.
    monkeypatch.setattr(app_mod, "eigene_instanz", lambda port, host="127.0.0.1", profil=None, spanne=12: 8765)
    httpd = app_mod.chooser_server(0)
    threading.Thread(target=lambda: app_mod._wahl_abwarten(httpd, False), daemon=True).start()
    code, r = call(httpd.server_address[1], "POST", "/api/profile-open", {"name": "standard"})
    assert code == 200 and r == {"ok": True, "url": "http://127.0.0.1:8765/"}


def test_profilwahl_beendet_jede_verbindung(wurzel, monkeypatch):
    """Regression: the browser kept its connection to the chooser alive and
    polled /api/status on it. shutdown() only stops accepting – the handler
    thread on that connection answered {"chooser": true} for ever while
    the app already ran on the port, and the page never moved on."""
    import socket
    profil_mit_config(wurzel, "nordwind")
    httpd = app_mod.chooser_server(0)
    port = httpd.server_address[1]
    faden = threading.Thread(target=lambda: app_mod._wahl_abwarten(httpd, False), daemon=True)
    faden.start()
    wirt = f"Host: 127.0.0.1:{port}\r\n".encode()

    def roh(anfrage):
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.sendall(anfrage)
        teile = []
        while True:                      # until the server closes the socket
            teil = sock.recv(65536)
            if not teil:
                break
            teile.append(teil)
        sock.close()
        return b"".join(teile).decode("utf-8", "replace")

    antwort = roh(b"GET /api/status HTTP/1.1\r\n" + wirt + b"\r\n")
    assert "connection: close" in antwort.lower()
    assert '{"chooser": true}' in antwort
    # The choice itself, on a connection the client would like to keep:
    # the answer comes, then the socket is closed – nothing more is served
    # on it, so the next poll has to knock at the door again.
    body = b'{"name": "nordwind"}'
    antwort = roh(b"POST /api/profile-open HTTP/1.1\r\n" + wirt +
                  b"Content-Type: application/json\r\nContent-Length: "
                  + str(len(body)).encode() + b"\r\n\r\n" + body)
    assert '{"ok": true}' in antwort and "connection: close" in antwort.lower()
    faden.join(5)
    assert not faden.is_alive(), "the chooser did not hand over"
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()


def test_profil_seite_traegt_die_daten(wurzel):
    profil_mit_config(wurzel, "nordwind")
    html = app_mod.profil_seite("en")
    assert "/*__PROFIL__*/" not in html and "__TITEL__" not in html
    assert "<title>Which archive?</title>" in html
    marke = '<script type="application/json" id="daten">'
    anfang = html.index(marke) + len(marke)
    daten = json.loads(html[anfang:html.index("</script>", anfang)])
    assert daten["lang"] == "en"
    assert [p["name"] for p in daten["profile"]["alle"]] == ["nordwind", "standard"]
    assert daten["texte"] and all(k.startswith(("profile.", "srv.profile."))
                                  for k in daten["texte"])
    assert "srv.profile.badname" in daten["texte"]    # the chooser's own errors
    assert daten["texte"]["profile.title"] == "Which archive?"


def test_profil_seite_maskiert_das_json(wurzel, monkeypatch):
    """The JSON sits inside a script element – a '<' in it must not be able
    to close that element."""
    monkeypatch.setattr(app_mod, "profil_status", lambda: {"name": "</script><b>"})
    html = app_mod.profil_seite("de")
    assert "</script><b>" not in html and "\\u003c/script>" in html


def test_kopfzeile_fuehrt_zum_wechselfenster():
    """The profile state in the header opens the switch window, not the
    settings; the window's gear leads there."""
    seite = app_mod.seite()
    kopf = seite.split("</header>")[0]
    assert 'id="pill-profil" onclick="profilWechselnFenster()"' in kopf
    assert "function profilEinstellungen()" in seite
    chooser = (Path(app_mod.RES) / "profil.html").read_text(encoding="utf-8")
    assert "/api/profiles" not in chooser and "profile.flag" not in chooser
    assert "onclick=\"oeffnen(" in chooser                # the whole card


def test_profilseite_liegt_als_datei_neben_dem_code():
    """Like page.html: a data file read from RES, shipped by the bundle."""
    datei = Path(app_mod.RES) / "profil.html"
    assert datei.exists()
    text = datei.read_text(encoding="utf-8")
    assert text.lstrip().lower().startswith("<!doctype html>")
    assert "/*__PROFIL__*/" in text and "__TITEL__" in text
    # Nothing of the app's own page in it: every text comes from the JSON.
    assert "data-i18n" not in text and "/*__I18N__*/" not in text
    spec = (Path(app_mod.RES) / "packaging" / "app.spec").read_text(encoding="utf-8")
    assert '"profil.html"' in spec


# --------------------------------------------------------------------------
# The running app: status, routes, the switch
# --------------------------------------------------------------------------
def test_status_nennt_das_profil_knapp(server):
    """The poll every 2.5 s carries the header's needs only; the list with
    accounts, folders and last runs of every profile is fetched when it
    is looked at."""
    _a, port = server
    code, st = call(port, "GET", "/api/status")
    assert code == 200
    assert st["profile"] == {"name": "standard", "moeglich": True, "mehrere": False}
    code, pr = call(port, "GET", "/api/profiles")
    assert code == 200 and [p["name"] for p in pr["alle"]] == ["standard"]
    assert pr["alle"][0]["aktiv"] is True and pr["zuletzt"] is None


def test_eigene_instanz_findet_die_nachbarn(server, monkeypatch):
    """A bumped start (make_server took port+1) is still THE instance of
    its profile: found from the port asked for, and only for its profile."""
    _a, port = server
    assert app_mod.eigene_instanz(port, profil="standard") == port
    assert app_mod.eigene_instanz(port + 3, profil="standard") == port
    assert app_mod.eigene_instanz(port - 2, profil="standard") == port
    assert app_mod.eigene_instanz(port, profil="nordwind") is None
    assert app_mod.eigene_instanz(0, profil="standard") is None


def test_laeuft_bereits_unterscheidet_profile(server):
    """Another profile's instance keeps its port; this one takes the next."""
    _a, port = server
    assert app_mod.laeuft_bereits(port) is True
    assert app_mod.laeuft_bereits(port, profil="standard") is True
    assert app_mod.laeuft_bereits(port, profil="nordwind") is False


def test_api_profiles_legt_an(server, wurzel):
    _a, port = server
    code, r = call(port, "POST", "/api/profiles", {"name": "nordwind"})
    assert code == 200 and r["ok"] and r["profile"]["name"] == "nordwind"
    assert r["profiles"]["mehrere"] is True and r["profiles"]["name"] == "standard"
    assert (wurzel / "profiles" / "nordwind").is_dir()
    code, r = call(port, "POST", "/api/profiles", {"name": "nordwind"})
    assert code == 400 and r["message"]["k"] == "srv.profile.exists"
    code, r = call(port, "POST", "/api/profiles", {"name": "Bob Baumeister"})
    assert code == 400 and r["message"]["k"] == "srv.profile.badname"


def test_api_profile_switch(server, wurzel, monkeypatch):
    a, port = server
    gestartet = []
    monkeypatch.setattr(app_mod, "neustart_mit_profil",
                        lambda httpd, name, p: gestartet.append((name, p)))
    code, r = call(port, "POST", "/api/profile-switch", {"name": "nordwind"})
    assert code == 400 and r["message"]["k"] == "srv.profile.unknown"
    # The open one: nothing to restart, the page just reloads.
    code, r = call(port, "POST", "/api/profile-switch", {"name": "standard"})
    assert code == 200 and r == {"ok": True, "name": "standard", "url": "/"}
    profil_mit_config(wurzel, "nordwind")
    # Not while a job runs: the restart would tear it down halfway.
    monkeypatch.setattr(type(a.jobs), "busy", property(lambda self: True))
    code, r = call(port, "POST", "/api/profile-switch", {"name": "nordwind"})
    assert code == 409 and r["message"]["k"] == "srv.busy"
    assert not gestartet
    monkeypatch.setattr(type(a.jobs), "busy", property(lambda self: False))
    code, r = call(port, "POST", "/api/profile-switch", {"name": "Nordwind"})
    assert code == 200 and r == {"ok": True, "name": "nordwind"}
    assert gestartet == [("nordwind", port)]
    assert app_mod.profil_register_lesen()["zuletzt"] == "nordwind"
    # Open in a second instance next door: the page is sent there instead.
    monkeypatch.setattr(app_mod, "eigene_instanz",
                        lambda port, host="127.0.0.1", profil=None, spanne=12: 8765)
    code, r = call(port, "POST", "/api/profile-switch", {"name": "nordwind"})
    assert code == 200 and r["url"] == "http://127.0.0.1:8765/"
    assert gestartet == [("nordwind", port)]
    # No profiles under the override: no restart into a different archive.
    monkeypatch.setenv("MUNIMENTUM_DATA_DIR", str(wurzel))
    code, r = call(port, "POST", "/api/profile-switch", {"name": "standard"})
    assert code == 400 and r["message"]["k"] == "srv.profile.impossible"


def test_api_profile_prefs(server):
    _a, port = server
    code, r = call(port, "POST", "/api/profile-prefs", {"ohne_nachfrage": True})
    assert code == 200 and r == {"ok": True, "ohne_nachfrage": True}
    assert app_mod.profil_register_lesen()["ohne_nachfrage"] is True
    code, r = call(port, "POST", "/api/profile-prefs", {"ohne_nachfrage": False})
    assert r["ohne_nachfrage"] is False


def test_api_data_dir_verweigert_den_ordner_eines_anderen_profils(server, wurzel):
    """Two profiles in one data or index folder would be one archive nobody
    asked for – refused before anything is saved."""
    a, port = server
    gemeinsam = wurzel / "gemeinsam"
    profil_mit_config(wurzel, "nordwind", data_dir=str(gemeinsam))
    code, r = call(port, "POST", "/api/data-dir", {"path": str(gemeinsam)})
    assert code == 400
    assert r["message"] == {"k": "srv.datadir.shared", "v": {"profile": "nordwind"}}
    assert not a.cfg.get("data_dir")
    code, r = call(port, "POST", "/api/data-dir", {"index": str(gemeinsam)})
    assert code == 400 and r["message"]["k"] == "srv.datadir.shared"
    assert not a.cfg.get("index_dir")
    code, r = call(port, "POST", "/api/data-dir", {"path": str(wurzel / "eigen")})
    assert code == 200 and r["ok"] is True


def test_mcp_schnipsel_heisst_nach_dem_profil(wurzel, monkeypatch):
    """A name that never flips: the first profile keeps the plain one, so
    a re-copied snippet replaces the old entry; every other profile is
    named after itself. The HTTP endpoint is the app's – it serves the
    open profile – so its entry is always the plain one."""
    eins = app_mod.mcp_client_config({}, 8700)
    assert list(eins["http"]["mcpServers"]) == ["munimentum"]
    assert eins["stdio"]["mcpServers"]["munimentum"]["args"][-2:] == ["--profile", "standard"]
    profil_mit_config(wurzel, "nordwind")
    assert list(app_mod.mcp_client_config({}, 8700)["stdio"]["mcpServers"]) == ["munimentum"]
    monkeypatch.setattr(app_mod, "PROFIL", "nordwind")
    zwei = app_mod.mcp_client_config({}, 8700)
    assert list(zwei["http"]["mcpServers"]) == ["munimentum"]
    eintrag = zwei["stdio"]["mcpServers"]["munimentum-nordwind"]
    assert eintrag["args"][-2:] == ["--profile", "nordwind"]
    assert "env" not in eintrag and "--data-dir" not in eintrag["args"]


def test_neustart_mit_profil_stoppt_den_server_und_merkt_den_aufruf(monkeypatch):
    fertig = threading.Event()
    monkeypatch.setattr(app_mod, "_NEUSTART", None)
    monkeypatch.setattr(app_mod.os, "execv", lambda *a: pytest.fail("exec from the helper thread"))

    class Httpd:
        def shutdown(self):
            fertig.set()

    app_mod.neustart_mit_profil(Httpd(), "nordwind", 8701)
    assert fertig.wait(5)
    assert app_mod._NEUSTART[:2] == [sys.executable, str(Path(app_mod.__file__).resolve())]
    assert app_mod._NEUSTART[2:] == ["--profile", "nordwind", "--port", "8701", "--no-browser"]
    # Nothing asked: serve() just returns.
    monkeypatch.setattr(app_mod, "_NEUSTART", None)
    app_mod.neustart_ausfuehren()
    # Windows: a child instead of execv – its argv joining quotes nothing.
    monkeypatch.setattr(app_mod, "_NEUSTART", ["x.exe", "--profile", "nordwind"])
    monkeypatch.setattr(app_mod.sys, "platform", "win32")
    kinder = []
    monkeypatch.setattr(app_mod.subprocess, "Popen", lambda argv, **k: kinder.append(list(argv)))
    app_mod.neustart_ausfuehren()
    assert kinder == [["x.exe", "--profile", "nordwind"]]


def test_profilwechsel_startet_erst_nach_dem_stopp_neu(wurzel, monkeypatch, with_ollama):
    """Regression: the restart was exec'd from a daemon thread while main()
    was already returning – whoever won, the page waited for a process that
    had simply ended. Now serve() itself starts over, after its clean-up."""
    profil_mit_config(wurzel, "nordwind")
    (heim_von(wurzel) / "data").mkdir(exist_ok=True)
    a = app_mod.App(app_mod.load_config())
    reihenfolge = []
    echtes_shutdown = a.shutdown
    monkeypatch.setattr(a, "shutdown", lambda: reihenfolge.append("shutdown") or echtes_shutdown())
    box = []
    echtes = app_mod.make_server
    monkeypatch.setattr(app_mod, "make_server",
                        lambda app, port, host="127.0.0.1": box.append(echtes(app, port, host)) or box[0])
    monkeypatch.setattr(app_mod, "_NEUSTART", None)
    aufruf = {}
    monkeypatch.setattr(app_mod.os, "execv",
                        lambda pfad, argv: (reihenfolge.append("exec"), aufruf.update(pfad=pfad, argv=list(argv))))
    antwort = {}

    def wechseln():
        # The page's request, from the side; serve() itself keeps the main
        # thread, as it does in the app (on a Mac it runs the event loop
        # there, and that loop only stops from the main thread).
        for _ in range(200):
            if box:
                break
            time.sleep(0.02)
        antwort["port"] = box[0].server_address[1]
        antwort["code"], antwort["r"] = call(antwort["port"], "POST", "/api/profile-switch",
                                              {"name": "nordwind"})

    threading.Thread(target=wechseln, daemon=True).start()
    app_mod.serve(a, 0, open_browser=False)          # returns once the switch stopped it
    assert antwort["code"] == 200 and antwort["r"] == {"ok": True, "name": "nordwind"}
    assert reihenfolge == ["shutdown", "exec"]       # clean-up first, then the new start
    assert aufruf["pfad"] == sys.executable
    assert aufruf["argv"][-5:] == ["--profile", "nordwind", "--port", str(antwort["port"]), "--no-browser"]
    assert app_mod.profil_register_lesen()["zuletzt"] == "nordwind"


def test_main_profile_setzt_das_profil(wurzel, monkeypatch):
    profil_mit_config(wurzel, "nordwind")
    gesehen = {}
    monkeypatch.setattr(app_mod, "serve",
                        lambda a, port, open_browser=True:
                        gesehen.update(port=port, browser=open_browser))
    app_mod.main(["--profile", "nordwind", "--port", "9002", "--no-browser"])
    assert app_mod.PROFIL == "nordwind"
    assert app_mod.HEIM == wurzel / "profiles" / "nordwind"
    assert (wurzel / "profiles" / "nordwind" / "data").is_dir()
    assert gesehen == {"port": 9002, "browser": False}
    assert app_mod.profil_register_lesen()["zuletzt"] == "nordwind"


def test_main_profile_aus_der_umgebung(wurzel, monkeypatch):
    profil_mit_config(wurzel, "nordwind")
    monkeypatch.setenv("MUNIMENTUM_PROFILE", "nordwind")
    monkeypatch.setattr(app_mod, "serve", lambda *a, **k: None)
    app_mod.main(["--no-browser"])
    assert app_mod.PROFIL == "nordwind"


def test_main_nach_der_wahl_im_browser_kein_zweiter_tab(wurzel, monkeypatch):
    profil_mit_config(wurzel, "nordwind")
    gesehen = {}
    monkeypatch.setattr(app_mod, "waehle_profil_im_browser",
                        lambda port, browser: ("nordwind", True))
    monkeypatch.setattr(app_mod, "serve",
                        lambda a, port, open_browser=True: gesehen.update(browser=open_browser))
    app_mod.main([])
    assert gesehen == {"browser": False}
    assert app_mod.PROFIL == "nordwind"


def test_main_data_dir_kennt_keine_profile(wurzel, monkeypatch):
    profil_mit_config(wurzel, "nordwind")
    monkeypatch.setattr(app_mod, "waehle_profil_im_browser",
                        lambda *a: pytest.fail("gefragt trotz --data-dir"))
    monkeypatch.setattr(app_mod, "serve", lambda *a, **k: None)
    app_mod.main(["--data-dir", str(wurzel / "eins"), "--no-browser"])
    assert app_mod.PROFIL == "standard"


# --------------------------------------------------------------------------
# Renaming – a folder rename, never of the open profile
# --------------------------------------------------------------------------
def test_profil_umbenennen(wurzel):
    heim = profil_mit_config(wurzel, "nordwind", data_dir=str(wurzel / "profiles" / "nordwind" / "exporte"),
                             index_dir=str(wurzel / "anderswo"))
    (heim / "gx_token.txt").write_text("x", encoding="utf-8")
    app_mod.profil_register_schreiben(zuletzt="nordwind")
    for alt, neu, erwartet in (("fremd", "x", "srv.profile.unknown"),
                               ("standard", "x", "srv.profile.active"),
                               ("nordwind", "Alice Beispiel", "srv.profile.badname"),
                               ("nordwind", "standard", "srv.profile.exists")):
        name, fehler = app_mod.profil_umbenennen(alt, neu)
        assert name is None and fehler["k"] == erwartet, (alt, neu)
    assert heim.is_dir()                                     # nothing happened
    name, fehler = app_mod.profil_umbenennen("nordwind", " Nordwind-Alt ")
    assert fehler is None and name == "nordwind-alt"
    neu = wurzel / "profiles" / "nordwind-alt"
    assert neu.is_dir() and not heim.exists()
    assert (neu / "gx_token.txt").read_text(encoding="utf-8") == "x"
    cfg = json.loads((neu / settings.CONFIG_NAME).read_text(encoding="utf-8"))
    assert Path(cfg["data_dir"]) == neu / "exporte"          # followed the folder
    assert cfg["index_dir"] == str(wurzel / "anderswo")      # elsewhere: untouched
    assert app_mod.profil_register_lesen()["zuletzt"] == "nordwind-alt"
    assert app_mod.profil_namen() == ["nordwind-alt", "standard"]


def test_api_profile_rename(server, wurzel, monkeypatch):
    a, port = server
    profil_mit_config(wurzel, "nordwind")
    code, r = call(port, "POST", "/api/profile-rename", {"name": "standard", "neu": "x"})
    assert code == 400 and r["message"]["k"] == "srv.profile.active"
    monkeypatch.setattr(type(a.jobs), "busy", property(lambda self: True))
    code, r = call(port, "POST", "/api/profile-rename", {"name": "nordwind", "neu": "beratung"})
    assert code == 409
    monkeypatch.setattr(type(a.jobs), "busy", property(lambda self: False))
    code, r = call(port, "POST", "/api/profile-rename", {"name": "nordwind", "neu": "beratung"})
    assert code == 200 and r["name"] == "beratung"
    assert [p["name"] for p in r["profiles"]["alle"]] == ["beratung", "standard"]
    assert (wurzel / "profiles" / "beratung").is_dir()


# --------------------------------------------------------------------------
# The one move: an archive from before 10.0 goes into profiles/standard/
# --------------------------------------------------------------------------
def altes_archiv(wurzel, **cfg):
    """What a 9.x install leaves in the app folder."""
    (wurzel / settings.CONFIG_NAME).write_text(json.dumps(cfg), encoding="utf-8")
    (wurzel / "gx_token.txt").write_text("token", encoding="utf-8")
    (wurzel / "msal_cache.bin").write_bytes(b"cache")
    (wurzel / "runs.db").write_bytes(b"runs")
    (wurzel / "profiles.json").write_text("{}", encoding="utf-8")
    (wurzel / "app.log").write_text("log", encoding="utf-8")


def test_umzug_bringt_das_archiv_in_sein_profil(wurzel):
    """Default layout: data/ and rag_store/ inside the app folder go along,
    the small files too; the register and the log stay in the app folder."""
    heim_von(wurzel).rmdir()
    altes_archiv(wurzel, data_dir="", index_dir="", language="de")
    (wurzel / "data" / "outlook_export").mkdir(parents=True)
    (wurzel / "data" / "outlook_export" / "state.db").write_bytes(b"x")
    (wurzel / "rag_store").mkdir()
    (wurzel / "rag_store" / "corpus.db").write_bytes(b"y")
    ergebnis = app_mod.layout_umzug(wurzel)
    heim = heim_von(wurzel)
    assert ergebnis["nach"] == str(heim) and "fehler" not in ergebnis
    assert sorted(ergebnis["bewegt"]) == sorted(
        ["app_config.json", "gx_token.txt", "msal_cache.bin", "runs.db", "data", "rag_store"])
    assert (heim / "data" / "outlook_export" / "state.db").read_bytes() == b"x"
    assert (heim / "rag_store" / "corpus.db").read_bytes() == b"y"
    assert (heim / "gx_token.txt").read_text(encoding="utf-8") == "token"
    assert json.loads((heim / settings.CONFIG_NAME).read_text(encoding="utf-8")) == {
        "data_dir": "", "index_dir": "", "language": "de"}
    for name in ("app_config.json", "gx_token.txt", "msal_cache.bin", "runs.db", "data", "rag_store"):
        assert not (wurzel / name).exists(), name
    assert (wurzel / "profiles.json").exists() and (wurzel / "app.log").exists()
    # Once. The second start finds nothing to move.
    assert app_mod.layout_umzug(wurzel) == {}
    assert app_mod._UMZUG == {}


def test_umzug_laesst_eigene_pfade_in_ruhe(wurzel, tmp_path_factory):
    """Data and index folders the user pointed elsewhere stay where they
    are; the configuration keeps naming them."""
    heim_von(wurzel).rmdir()
    anderswo = tmp_path_factory.mktemp("anderswo")
    (anderswo / "exporte").mkdir()
    (anderswo / "index").mkdir()
    altes_archiv(wurzel, data_dir=str(anderswo / "exporte"), index_dir=str(anderswo / "index"))
    ergebnis = app_mod.layout_umzug(wurzel)
    assert "fehler" not in ergebnis
    assert sorted(ergebnis["bewegt"]) == ["app_config.json", "gx_token.txt", "msal_cache.bin", "runs.db"]
    assert (anderswo / "exporte").is_dir() and (anderswo / "index").is_dir()
    cfg = json.loads((heim_von(wurzel) / settings.CONFIG_NAME).read_text(encoding="utf-8"))
    assert cfg == {"data_dir": str(anderswo / "exporte"), "index_dir": str(anderswo / "index")}


def test_umzug_nimmt_das_flache_layout_samt_pin_mit(wurzel):
    """The pinned pre-7.0 layout: the export folders sit in the app folder
    itself and data_dir names it. They move, and the pin follows."""
    heim_von(wurzel).rmdir()
    altes_archiv(wurzel, data_dir=str(wurzel), index_dir="")
    (wurzel / "teams_export").mkdir()
    (wurzel / "outlook_export").mkdir()
    (wurzel / "rag_store").mkdir()
    ergebnis = app_mod.layout_umzug(wurzel)
    heim = heim_von(wurzel)
    assert "fehler" not in ergebnis
    assert (heim / "teams_export").is_dir() and (heim / "outlook_export").is_dir()
    assert (heim / "rag_store").is_dir()
    cfg = json.loads((heim / settings.CONFIG_NAME).read_text(encoding="utf-8"))
    assert cfg["data_dir"] == str(heim) and cfg["index_dir"] == ""
    assert not (wurzel / "teams_export").exists()


def test_umzug_nimmt_einen_ordner_im_app_ordner_mit(wurzel):
    """A path the user set INSIDE the app folder moves along, and the
    configuration follows it into the profile."""
    heim_von(wurzel).rmdir()
    altes_archiv(wurzel, data_dir=str(wurzel / "exporte" / "x"), index_dir="")
    (wurzel / "exporte" / "x").mkdir(parents=True)
    ergebnis = app_mod.layout_umzug(wurzel)
    heim = heim_von(wurzel)
    assert "fehler" not in ergebnis
    assert (heim / "exporte" / "x").is_dir() and not (wurzel / "exporte").exists()
    cfg = json.loads((heim / settings.CONFIG_NAME).read_text(encoding="utf-8"))
    assert Path(cfg["data_dir"]) == heim / "exporte" / "x"


def test_umzug_nicht_wenn_es_schon_profile_gibt(wurzel):
    """Regression: a stray runs.db in the app folder turned into a new,
    empty "standard" profile on the next start. With a profile in place
    the layout is the new one – nothing moves, nothing is created."""
    profil_mit_config(wurzel, "nordwind")
    heim_von(wurzel).rmdir()                      # only nordwind is left
    (wurzel / "runs.db").write_bytes(b"stray")
    (wurzel / settings.CONFIG_NAME).write_text("{}", encoding="utf-8")
    assert app_mod.layout_umzug(wurzel) == {}
    assert not heim_von(wurzel).exists()
    assert (wurzel / "runs.db").exists()
    assert app_mod.profil_namen() == ["nordwind"]


def test_umzug_ohne_archiv_tut_nichts(wurzel):
    """A fresh install: nothing to move, no folder created by the move."""
    heim_von(wurzel).rmdir()
    (wurzel / "profiles").rmdir()
    (wurzel / "profiles.json").write_text("{}", encoding="utf-8")
    assert app_mod.layout_umzug(wurzel) == {}
    assert not (wurzel / "profiles").exists()


def test_umzug_alles_oder_nichts(wurzel, monkeypatch):
    """A rename that fails puts back what was moved before it; the archive
    stays whole in the app folder, and the start runs it from there."""
    heim_von(wurzel).rmdir()
    (wurzel / "profiles").rmdir()
    altes_archiv(wurzel, data_dir="", index_dir="")
    (wurzel / "data").mkdir()
    (wurzel / "rag_store").mkdir()
    echt = os.rename

    def kaputt(quelle, nach):
        if Path(quelle).name == "rag_store":
            raise PermissionError("in use")
        return echt(quelle, nach)

    monkeypatch.setattr(app_mod.os, "rename", kaputt)
    ergebnis = app_mod.layout_umzug(wurzel)
    assert "in use" in ergebnis["fehler"]
    for name in ("app_config.json", "gx_token.txt", "msal_cache.bin", "runs.db", "data", "rag_store"):
        assert (wurzel / name).exists(), name
    assert not (wurzel / "profiles").exists()
    # This start: the archive where it lies, as ONE archive – no profiles
    # to create, rename or switch to, and the MCP snippet names the paths,
    # not a profile folder that does not exist.
    app_mod.set_profil("standard")
    assert app_mod.HEIM == wurzel
    assert app_mod.profile_moeglich() is False
    st = app_mod.profil_status()
    assert st["moeglich"] is False and st["alle"][0]["ordner"] == str(wurzel)
    assert app_mod.profil_anlegen("beratung")[1]["k"] == "srv.profile.impossible"
    assert not (wurzel / "profiles").exists()
    args = app_mod.mcp_client_config({}, 8700)["stdio"]["mcpServers"]["munimentum"]["args"]
    assert "--profile" not in args and args[args.index("--data-dir") + 1] == str(app_mod.BASE)
    # The next start tries again – and succeeds once nothing is in use.
    monkeypatch.setattr(app_mod.os, "rename", echt)
    ergebnis = app_mod.layout_umzug(wurzel)
    assert "fehler" not in ergebnis
    assert (heim_von(wurzel) / "runs.db").read_bytes() == b"runs"


def test_main_datenordner_aus_der_umgebung_ist_wie_der_schalter(wurzel, monkeypatch):
    """MUNIMENTUM_DATA_DIR alone (no --data-dir) is the same override: one
    archive at that folder, no profiles, no move – as in 9.x."""
    heim_von(wurzel).rmdir()
    (wurzel / "profiles").rmdir()
    altes_archiv(wurzel)
    monkeypatch.setenv("MUNIMENTUM_DATA_DIR", str(wurzel))
    monkeypatch.setattr(app_mod, "serve", lambda *a, **k: None)
    app_mod.main(["--no-browser"])
    assert app_mod.HEIM == wurzel.resolve() and app_mod.BASE == wurzel.resolve()
    assert (wurzel / settings.CONFIG_NAME).exists() and not (wurzel / "profiles").exists()
    assert app_mod.profil_status()["moeglich"] is False


def test_umzug_pinnt_nie_gepinnte_flache_ordner(wurzel):
    """Flat export folders next to the app with no data_dir key at all (an
    upgrade straight from before 7.0): they move, and the pin is written
    with them – so the log never says 'nothing was moved' right after."""
    heim_von(wurzel).rmdir()
    altes_archiv(wurzel, language="de")
    (wurzel / "teams_export").mkdir()
    ergebnis = app_mod.layout_umzug(wurzel)
    heim = heim_von(wurzel)
    assert "fehler" not in ergebnis and (heim / "teams_export").is_dir()
    cfg = json.loads((heim / settings.CONFIG_NAME).read_text(encoding="utf-8"))
    assert cfg["data_dir"] == str(heim) and cfg["language"] == "de"
    app_mod.set_profil("standard")
    assert app_mod.altbestand_pinnen() is False                # nothing left to pin


def test_main_zieht_um_bevor_es_losgeht(wurzel, monkeypatch):
    """The move runs first thing in main() – before the streams, before the
    arguments – and the app then serves the profile folder; serve() logs
    what happened."""
    heim_von(wurzel).rmdir()
    altes_archiv(wurzel, data_dir="", index_dir="")
    (wurzel / "data").mkdir()
    gesehen = {}
    monkeypatch.setattr(app_mod, "serve", lambda a, port, open_browser=True: gesehen.update(heim=app_mod.HEIM))
    app_mod.main(["--no-browser"])
    assert gesehen["heim"] == heim_von(wurzel)
    assert app_mod._UMZUG["nach"] == str(heim_von(wurzel))
    assert (heim_von(wurzel) / "data").is_dir() and not (wurzel / "data").exists()
    # With --data-dir there is one archive and no move.
    altes_archiv(wurzel)
    monkeypatch.setattr(app_mod, "serve", lambda *a, **k: None)
    app_mod.main(["--data-dir", str(wurzel / "eins"), "--no-browser"])
    assert (wurzel / settings.CONFIG_NAME).exists()


def test_serve_sagt_was_der_umzug_tat(wurzel, monkeypatch, with_ollama):
    zeilen = []
    a = app_mod.App(app_mod.load_config())
    monkeypatch.setattr(a.jobs, "logk", lambda k, level="info", **v: zeilen.append((k, level, v)))
    monkeypatch.setattr(app_mod, "_UMZUG", {"nach": "/x/profiles/standard", "bewegt": ["data"]})
    box = []
    echtes = app_mod.make_server
    monkeypatch.setattr(app_mod, "make_server",
                        lambda app, port, host="127.0.0.1": box.append(echtes(app, port, host)) or box[0])
    threading.Timer(0.05, lambda: box[0].shutdown()).start()
    app_mod.serve(a, 0, open_browser=False)
    assert ("srv.layout.moved", "info", {"path": "/x/profiles/standard"}) in zeilen
    zeilen.clear()
    monkeypatch.setattr(app_mod, "_UMZUG", {"nach": "/x/profiles/standard", "fehler": "in use"})
    a = app_mod.App(app_mod.load_config())          # a fresh one: the first is shut down
    monkeypatch.setattr(a.jobs, "logk", lambda k, level="info", **v: zeilen.append((k, level, v)))
    box.clear()
    threading.Timer(0.05, lambda: box[0].shutdown()).start()
    app_mod.serve(a, 0, open_browser=False)
    assert ("srv.layout.movefail", "warn", {"path": "/x/profiles/standard", "detail": "in use"}) in zeilen


# --------------------------------------------------------------------------
# mcp_server.py – --profile
# --------------------------------------------------------------------------
@pytest.fixture
def mcp_wurzel(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_wurzel", lambda: tmp_path)
    for n in ("MUNIMENTUM_HOME", "MUNIMENTUM_DATA_DIR", "OFFICE365_DATA_DIR"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("MCP_ENABLED", "1")
    settings.reset()
    yield tmp_path
    settings.reset()


def test_mcp_profile_liest_die_pfade_aus_dem_profil(mcp_wurzel, monkeypatch):
    heim = profil_mit_config(mcp_wurzel, "nordwind", data_dir=str(mcp_wurzel / "exporte"))
    monkeypatch.setenv("MUNIMENTUM_HOME", "")
    gesehen = []
    monkeypatch.setattr(mcp_server.store_layout, "db_path",
                        lambda s: gesehen.append(str(s)) or (mcp_wurzel / "nichts"))
    monkeypatch.setattr(sys, "argv", ["mcp_server.py", "--profile", "nordwind",
                                      "--transport", "stdio", "--no-ollama"])
    with pytest.raises(SystemExit, match="No store"):
        mcp_server.main()
    assert gesehen == [str(heim / settings.STORE_DIR)]
    assert os.environ["MUNIMENTUM_HOME"] == str(heim)


def test_mcp_profile_ist_die_ganze_adresse(mcp_wurzel, monkeypatch):
    """--profile alone: folders, model, Ollama address, port and the Ollama
    switch come from that profile's settings; explicit flags still win."""
    import argparse
    heim = profil_mit_config(mcp_wurzel, "nordwind", data_dir=str(mcp_wurzel / "exporte"),
                             embed_model="x-model", ollama="http://127.0.0.1:1",
                             mcp_port=9911, ollama_enabled=False)
    monkeypatch.setenv("MUNIMENTUM_HOME", "")

    def roh(**extra):
        felder = dict(profile="nordwind", data_dir=None, store=None, embed_model=None,
                      ollama=None, port=None, no_ollama=False)
        felder.update(extra)
        return argparse.Namespace(**felder)

    a, auskunft = mcp_server._argumente(roh())
    assert a.data_dir == str((mcp_wurzel / "exporte").resolve())
    assert a.store == str(heim / settings.STORE_DIR)
    assert (a.embed_model, a.ollama, a.port) == ("x-model", "http://127.0.0.1:1", 9911)
    assert a.no_ollama is True and auskunft is None
    assert os.environ["MUNIMENTUM_HOME"] == str(heim)
    b, _ = mcp_server._argumente(roh(data_dir="/x", embed_model="y", port=1))
    assert (b.data_dir, b.embed_model, b.port) == ("/x", "y", 1)


def test_mcp_profile_unbekannt_oder_kein_slug(mcp_wurzel, monkeypatch):
    for name in ("fremd", "Alice Beispiel"):
        monkeypatch.setattr(sys, "argv", ["mcp_server.py", "--profile", name,
                                          "--transport", "stdio"])
        with pytest.raises(SystemExit, match="profile"):
            mcp_server.main()


def test_mcp_ohne_profil_bei_mehreren_gibt_nur_die_auskunft(mcp_wurzel, monkeypatch):
    """Several archives, none named: one answer instead of a guess – over
    both transports, and the answer names the way out."""
    profil_mit_config(mcp_wurzel, "nordwind")
    profil_mit_config(mcp_wurzel, "standard")
    echt, aus = [], []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda **kw: echt.append(kw))

    class FakeServer:
        def __init__(self, text=None):
            self.text = text

        def run(self, **kw):
            aus.append((self.text, kw))

    monkeypatch.setattr(mcp_server, "_abgeschaltet_server", FakeServer)
    for transport in ("stdio", "http"):
        aus.clear()
        monkeypatch.setattr(sys, "argv", ["mcp_server.py", "--transport", transport,
                                          "--no-ollama"])
        mcp_server.main()
        assert not echt
        assert aus and "nordwind" in aus[0][0] and "--profile" in aus[0][0]
        assert aus[0][1]["transport"] == ("stdio" if transport == "stdio" else "streamable-http")


def test_mcp_alter_schnipsel_bekommt_die_auskunft(mcp_wurzel, monkeypatch):
    """A stdio entry from before 10.0 names folders in the app folder that
    the archive has left. It does not fail at every start – it answers,
    once, with what to do."""
    profil_mit_config(mcp_wurzel, "standard")
    monkeypatch.setenv("MUNIMENTUM_HOME", str(mcp_wurzel))
    aus = []

    class FakeServer:
        def __init__(self, text=None):
            self.text = text

        def run(self, **kw):
            aus.append(self.text)

    monkeypatch.setattr(mcp_server, "_abgeschaltet_server", FakeServer)
    # Never the real index: the store lookup shows which folder the server
    # would have taken, and it does not exist.
    gesehen = []
    monkeypatch.setattr(mcp_server.store_layout, "db_path",
                        lambda s: gesehen.append(str(s)) or (mcp_wurzel / "nichts"))
    monkeypatch.setattr(sys, "argv", ["mcp_server.py", "--transport", "stdio", "--no-ollama",
                                      "--data-dir", str(mcp_wurzel / "data"),
                                      "--store", str(mcp_wurzel / "rag_store")])
    mcp_server.main()
    assert gesehen == [str(mcp_wurzel / "rag_store")]
    assert aus and "before version 10.0" in aus[0] and "copy the stdio snippet" in aus[0]
    # A home that still holds its own configuration is not that case: the
    # missing store is a plain error, as before.
    (mcp_wurzel / settings.CONFIG_NAME).write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="No store"):
        mcp_server.main()


def test_auskunft_server_traegt_den_text():
    aus = mcp_server._abgeschaltet_server(mcp_server._profil_text(["standard", "nordwind"]))
    assert "nordwind" in aus.instructions and "--profile" in aus.instructions
