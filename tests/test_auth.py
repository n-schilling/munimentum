"""Tests for auth.py – the sign-in that both export scripts share.

Never touches the network and never opens a sign-in window: msal is
replaced throughout.

Two promises take centre stage, because runs depend on them:

  * The fallback goes in one direction only. With "login" configured and no
    cache present, a stored key may step in. Never the other way round –
    whoever picks key mode should not be surprised by a sign-in window.
  * The cache really lands on disk. That is exactly what decides whether a
    schedule survives a reboot; without it, login mode would just be a more
    cumbersome variant of the key.
"""

import json
import sys
import types

import pytest

import auth
import settings


@pytest.fixture(autouse=True)
def sauber(tmp_path, monkeypatch):
    """Own data folder, empty cache, no inherited variables."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MUNIMENTUM_DATA_DIR", str(tmp_path))
    for v in ("GRAPH_TOKEN", "GRAPH_AUTH", "GRAPH_CLIENT_ID", "GRAPH_TENANT",
              "GRAPH_DEVICE_CODE"):
        monkeypatch.delenv(v, raising=False)
    settings.reset()
    yield tmp_path
    settings.reset()


def konfig(tmp_path, **werte):
    (tmp_path / settings.CONFIG_NAME).write_text(
        json.dumps(werte, ensure_ascii=False), encoding="utf-8")
    settings.reset()


# --------------------------------------------------------------------------
# Mode and target
# --------------------------------------------------------------------------
def test_modus_ist_ohne_alles_der_schluessel(sauber):
    """The path that works without asking IT for help remains the default."""
    assert auth.modus() == "token"


@pytest.mark.parametrize("wert,erwartet", [
    ("login", "login"), ("LOGIN", "login"), (" login ", "login"),
    ("token", "token"), ("quatsch", "token"), ("", "token"),
])
def test_modus_aus_der_konfiguration(sauber, wert, erwartet):
    """A typo falls back to the path that always works."""
    konfig(sauber, auth_mode=wert)
    assert auth.modus() == erwartet


def test_modus_umgebung_schlaegt_datei(sauber, monkeypatch):
    konfig(sauber, auth_mode="token")
    monkeypatch.setenv("GRAPH_AUTH", "login")
    assert auth.modus() == "login"


def test_ohne_angabe_microsofts_oeffentliche_anwendung(sauber):
    """It needs no app registration – which is why it is the default."""
    assert auth.client_id() == auth.STANDARD_CLIENT_ID
    assert auth.tenant() == auth.STANDARD_TENANT
    assert auth.eigene_registrierung() is False


def test_eigene_registrierung_wird_erkannt(sauber):
    konfig(sauber, client_id="11111111-2222-3333-4444-555555555555",
           tenant="contoso.onmicrosoft.com")
    assert auth.eigene_registrierung() is True
    assert auth.authority().endswith("contoso.onmicrosoft.com")


def test_leere_angaben_zaehlen_als_keine(sauber):
    """A field cleared in the UI must not end up in a broken
    authority."""
    konfig(sauber, client_id="", tenant="")
    assert auth.client_id() == auth.STANDARD_CLIENT_ID
    assert auth.eigene_registrierung() is False


# --------------------------------------------------------------------------
# Reading the key
# --------------------------------------------------------------------------
def test_schluessel_aus_der_umgebung(sauber, monkeypatch):
    monkeypatch.setenv("GRAPH_TOKEN", '  "Bearer eyJ0abc"  ')
    assert auth.load_pasted_token() == "eyJ0abc"


def test_schluessel_neben_dem_aufruf_schlaegt_datenordner(sauber, tmp_path):
    """"Put gx_token.txt next to this script" is the documented rule –
    whoever does that should win with it too."""
    unter = tmp_path / "woanders"
    unter.mkdir()
    (tmp_path / auth.TOKEN_DATEI).write_text("aus-dem-datenordner", encoding="utf-8")
    (unter / auth.TOKEN_DATEI).write_text("daneben", encoding="utf-8")
    import os
    os.chdir(unter)
    assert auth.load_pasted_token() == "daneben"


def test_schluessel_aus_dem_datenordner(sauber, tmp_path, monkeypatch):
    unter = tmp_path / "leer"
    unter.mkdir()
    monkeypatch.chdir(unter)
    (tmp_path / auth.TOKEN_DATEI).write_text(" eyJ0datei \n", encoding="utf-8")
    assert auth.load_pasted_token() == "eyJ0datei"


def test_kein_schluessel(sauber):
    assert auth.load_pasted_token() is None


def test_leere_datei_zaehlt_nicht(sauber, tmp_path):
    (tmp_path / auth.TOKEN_DATEI).write_text("   \n", encoding="utf-8")
    assert auth.load_pasted_token() is None


# --------------------------------------------------------------------------
# msal stand-in
# --------------------------------------------------------------------------
class FakeCache:
    def __init__(self):
        self.inhalt = ""
        self.has_state_changed = False

    def deserialize(self, text):
        if text == "kaputt":
            raise ValueError("unlesbar")
        self.inhalt = text

    def serialize(self):
        return self.inhalt or '{"RefreshToken": {}}'


class FakeApp:
    """Behaves like msal.PublicClientApplication, just without the network."""

    letzte = None

    def __init__(self, client_id, authority=None, token_cache=None):
        self.client_id = client_id
        self.authority = authority
        self.cache = token_cache
        self.konten = []
        self.still = None            # response to acquire_token_silent
        self.interaktiv = None       # response to acquire_token_interactive
        self.device = None
        self.gesehen = []
        FakeApp.letzte = self

    def get_accounts(self):
        return list(self.konten)

    def acquire_token_silent(self, scopes, account=None):
        self.gesehen.append(("silent", tuple(scopes)))
        return self.still

    def acquire_token_interactive(self, scopes=None, prompt=None):
        self.gesehen.append(("interaktiv", tuple(scopes)))
        if self.cache is not None:
            self.cache.has_state_changed = True
        return self.interaktiv

    def initiate_device_flow(self, scopes=None):
        return self.device or {"user_code": "ABC", "message": "Gib ABC ein"}

    def acquire_token_by_device_flow(self, flow):
        self.gesehen.append(("device", ()))
        return self.interaktiv


@pytest.fixture
def fake_msal(monkeypatch):
    modul = types.ModuleType("msal")
    modul.PublicClientApplication = FakeApp
    modul.SerializableTokenCache = FakeCache
    monkeypatch.setitem(sys.modules, "msal", modul)
    return modul


# --------------------------------------------------------------------------
# Signing in
# --------------------------------------------------------------------------
def test_stille_erneuerung_fragt_niemanden(sauber, fake_msal):
    anmeldung = auth.Login(["S"])
    anmeldung.app.konten = [{"username": "a@b.c"}]
    anmeldung.app.still = {"access_token": "tok"}
    assert anmeldung.anmelden() is True
    assert anmeldung.token == "tok"
    assert [art for art, _ in anmeldung.app.gesehen] == ["silent"]


def test_ohne_cache_wird_gefragt(sauber, fake_msal):
    anmeldung = auth.Login(["S"])
    anmeldung.app.interaktiv = {"access_token": "neu"}
    assert anmeldung.anmelden() is True
    assert ("interaktiv", ("S",)) in anmeldung.app.gesehen


def test_nur_still_reisst_kein_fenster_auf(sauber, fake_msal):
    """The schedule runs at night – a sign-in window would wait until morning."""
    anmeldung = auth.Login(["S"])
    anmeldung.app.interaktiv = {"access_token": "darf-nicht"}
    assert anmeldung.anmelden(nur_still=True) is False
    assert "interaktiv" not in [art for art, _ in anmeldung.app.gesehen]


def test_weich_meldet_misserfolg_statt_abzubrechen(sauber, fake_msal):
    """The Teams export needs this: ask for channel rights first, and on
    refusal carry on with chat-only access."""
    anmeldung = auth.Login(["Voll"])
    anmeldung.app.interaktiv = {"error_description": "Admin consent required\nZeile2"}
    assert anmeldung.anmelden(weich=True) is False
    assert "Admin consent" in anmeldung.fehler


def test_ohne_weich_bricht_es_ab(sauber, fake_msal):
    anmeldung = auth.Login(["S"])
    anmeldung.app.interaktiv = {"error_description": "nein"}
    with pytest.raises(SystemExit, match="nein"):
        anmeldung.anmelden()


def test_device_code_statt_browser(sauber, fake_msal, capsys):
    konfig(sauber, device_code=True)
    anmeldung = auth.Login(["S"])
    anmeldung.app.interaktiv = {"access_token": "tok"}
    assert anmeldung.anmelden() is True
    assert "device" in [art for art, _ in anmeldung.app.gesehen]
    assert "Gib ABC ein" in capsys.readouterr().out


def test_erneuern_nutzt_zuerst_den_cache(sauber, fake_msal):
    anmeldung = auth.Login(["S"])
    anmeldung.app.konten = [{"username": "a@b.c"}]
    anmeldung.app.still = {"access_token": "frisch"}
    anmeldung.erneuern()
    assert anmeldung.token == "frisch"
    assert "interaktiv" not in [art for art, _ in anmeldung.app.gesehen]


def test_headers_tragen_den_token(sauber, fake_msal):
    anmeldung = auth.Login(["S"])
    anmeldung.token = "abc"
    assert anmeldung.headers() == {"Authorization": "Bearer abc"}


def test_eigene_registrierung_kommt_bei_msal_an(sauber, fake_msal):
    konfig(sauber, client_id="eigene-id", tenant="contoso.example")
    auth.Login(["S"])
    assert FakeApp.letzte.client_id == "eigene-id"
    assert FakeApp.letzte.authority.endswith("contoso.example")


# --------------------------------------------------------------------------
# Cache on disk – the unattended schedule depends on it
# --------------------------------------------------------------------------
def test_cache_wird_geschrieben(sauber, fake_msal, tmp_path):
    anmeldung = auth.Login(["S"])
    anmeldung.app.interaktiv = {"access_token": "tok"}
    anmeldung.anmelden()
    datei = tmp_path / auth.CACHE_DATEI
    assert datei.exists(), "ohne Datei überlebt der Zeitplan keinen Neustart"
    assert not datei.with_name(datei.name + ".tmp").exists()


def test_cache_gehoert_nur_dem_besitzer(sauber, fake_msal, tmp_path):
    """It holds the refresh token – valid for weeks."""
    if sys.platform.startswith("win"):
        pytest.skip("Windows kennt den Dateimodus nicht")
    anmeldung = auth.Login(["S"])
    anmeldung.app.interaktiv = {"access_token": "tok"}
    anmeldung.anmelden()
    assert (tmp_path / auth.CACHE_DATEI).stat().st_mode & 0o077 == 0


def test_kaputter_cache_haelt_nicht_auf(sauber, fake_msal, tmp_path):
    (tmp_path / auth.CACHE_DATEI).write_text("kaputt", encoding="utf-8")
    anmeldung = auth.Login(["S"])          # does not raise
    anmeldung.app.interaktiv = {"access_token": "tok"}
    assert anmeldung.anmelden() is True


def test_abmelden_verwirft_den_cache(sauber, fake_msal, tmp_path):
    (tmp_path / auth.CACHE_DATEI).write_text("{}", encoding="utf-8")
    assert auth.cache_leeren() is True
    assert not (tmp_path / auth.CACHE_DATEI).exists()
    assert auth.cache_leeren() is True      # twice is fine as well


def test_angemeldet_ohne_cache(sauber, fake_msal):
    assert auth.angemeldet() is None


def test_angemeldet_nennt_das_konto(sauber, fake_msal, tmp_path):
    (tmp_path / auth.CACHE_DATEI).write_text("{}", encoding="utf-8")
    orig = FakeApp.__init__

    def mit_konto(self, *a, **kw):
        orig(self, *a, **kw)
        self.konten = [{"username": "nico@example.com"}]
    FakeApp.__init__ = mit_konto
    try:
        assert auth.angemeldet() == "nico@example.com"
    finally:
        FakeApp.__init__ = orig


# --------------------------------------------------------------------------
# Choosing the path – this is where a run starts or does not
# --------------------------------------------------------------------------
def wege():
    gesehen = []
    return gesehen, (lambda tok: gesehen.append(("schluessel", tok)) or "K"), \
        (lambda nur_still=False: gesehen.append(("login", nur_still)) or "L")


def test_schluesselmodus_nimmt_den_schluessel(sauber, monkeypatch):
    monkeypatch.setenv("GRAPH_TOKEN", "abc")
    gesehen, k, ell = wege()
    assert auth.waehle_zugang(k, ell, ausgabe=lambda *_: None) == "K"
    assert gesehen == [("schluessel", "abc")]


def test_schluesselmodus_ohne_schluessel_bricht_ab(sauber, capsys):
    """No window from a subprocess: the app shows the token assistant as
    soon as the structured event arrives."""
    gesehen, k, ell = wege()
    with pytest.raises(SystemExit):
        auth.waehle_zugang(k, ell, ausgabe=lambda *_: None)
    assert gesehen == []
    assert "token_expired" in capsys.readouterr().out


def test_loginmodus_versucht_zuerst_still(sauber, monkeypatch):
    """Reported from the field: the export immediately tore open the browser
    although a valid key was at hand. The fallback hung on a SystemExit –
    which only comes after the window was already open. So ask silently first.
    """
    monkeypatch.setenv("GRAPH_AUTH", "login")
    monkeypatch.setenv("GRAPH_TOKEN", "abc")
    gesehen, k, ell = wege()
    assert auth.waehle_zugang(k, ell, ausgabe=lambda *_: None) == "L"
    assert gesehen == [("login", True)], "nicht still versucht"


def test_loginmodus_faellt_auf_den_schluessel_zurueck(sauber, monkeypatch):
    """Cache gone, but a key is at hand: then the export runs – without
    anyone having to click away a sign-in window."""
    monkeypatch.setenv("GRAPH_AUTH", "login")
    monkeypatch.setenv("GRAPH_TOKEN", "abc")
    gesehen = []

    def login(nur_still=False):
        gesehen.append(("login", nur_still))
        if nur_still:
            raise SystemExit("kein Cache")
        raise AssertionError("Anmeldefenster trotz vorhandenem Schlüssel")
    zeilen = []
    ergebnis = auth.waehle_zugang(
        lambda tok: gesehen.append(("schluessel", tok)) or "K",
        login, ausgabe=zeilen.append)
    assert ergebnis == "K"
    assert gesehen == [("login", True), ("schluessel", "abc")]
    assert any("Zugangsschlüssel" in z for z in zeilen)


def test_loginmodus_ohne_ausweg_bricht_ab(sauber, monkeypatch, capsys):
    """No cache, no key – no window, but the event the app reacts to with
    its token assistant."""
    monkeypatch.setenv("GRAPH_AUTH", "login")
    gesehen = []

    def login(nur_still=False):
        gesehen.append(("login", nur_still))
        raise SystemExit("kein Cache")
    k = lambda tok: gesehen.append(("schluessel", tok)) or "K"   # noqa: E731
    with pytest.raises(SystemExit):
        auth.waehle_zugang(k, login, ausgabe=lambda *_: None)
    assert gesehen == [("login", True)], "es wurde mehr als still versucht"
    assert "token_expired" in capsys.readouterr().out


def test_beschreibe_nennt_den_weg(sauber, monkeypatch):
    zeilen = []
    auth.beschreibe(zeilen.append)
    assert "Token-Modus" in zeilen[0]
    monkeypatch.setenv("GRAPH_AUTH", "login")
    zeilen.clear()
    auth.beschreibe(zeilen.append)
    assert "Login" in zeilen[0] and "öffentliche" in zeilen[0]


def test_beschreibe_nennt_die_eigene_registrierung(sauber, monkeypatch):
    monkeypatch.setenv("GRAPH_AUTH", "login")
    monkeypatch.setenv("GRAPH_CLIENT_ID", "eigene")
    zeilen = []
    auth.beschreibe(zeilen.append)
    assert "eigene App-Registrierung" in zeilen[0]


# --------------------------------------------------------------------------
# The scripts really use the shared path
# --------------------------------------------------------------------------
@pytest.mark.parametrize("modul", ["outlook_export", "teams_export"])
def test_skript_nutzt_auth(modul):
    """Otherwise sign-in would drift apart again – exactly what prompted this."""
    from pathlib import Path
    quelle = (Path(__file__).resolve().parent.parent / f"{modul}.py").read_text(
        encoding="utf-8")
    assert "import auth" in quelle
    assert "auth.waehle_zugang(" in quelle
    assert "msal.PublicClientApplication" not in quelle, "eigene Anmeldung übrig"
    assert "def load_pasted_token" not in quelle, "eigene Schlüsselfunktion übrig"


# --------------------------------------------------------------------------
# Login mode with a key at hand
#
# Reported from the field: "python3 outlook_export.py -default" immediately
# tore open the browser although a valid key sat in gx_token.txt. The
# fallback existed, but it hung on a SystemExit – which only comes after the
# sign-in window was open and someone had clicked it away.
# --------------------------------------------------------------------------
class _Protokoll:
    def __init__(self):
        self.zeilen = []

    def __call__(self, *args):
        self.zeilen.append(" ".join(str(a) for a in args))

    def __contains__(self, text):
        return any(text in z for z in self.zeilen)


def _wege(cache_taugt, interaktiv_erlaubt=True):
    """(mit_schluessel, mit_login, gesehen) – mit_login records the how."""
    gesehen = []

    def mit_login(nur_still=False):
        gesehen.append("still" if nur_still else "fenster")
        if nur_still and not cache_taugt:
            raise SystemExit("Keine gültige Anmeldung im Zwischenspeicher.")
        if not nur_still and not interaktiv_erlaubt:
            raise AssertionError("Anmeldefenster wurde geöffnet")
        return "LOGIN"

    return (lambda tok: f"KEY:{tok}"), mit_login, gesehen


def test_login_modus_nimmt_den_schluessel_ohne_fenster(monkeypatch, tmp_path):
    """The reported case. Try silently first; if that fails and a key is at
    hand, it is taken – without a browser."""
    monkeypatch.setenv("GRAPH_AUTH", "login")
    monkeypatch.setenv("GRAPH_TOKEN", "eyJ0gueltig")
    mit_schluessel, mit_login, gesehen = _wege(cache_taugt=False,
                                               interaktiv_erlaubt=False)
    log = _Protokoll()
    klient = auth.waehle_zugang(mit_schluessel, mit_login, ausgabe=log)
    assert klient == "KEY:eyJ0gueltig"
    assert gesehen == ["still"], "es wurde mehr als still versucht"
    assert "Zugangsschlüssel" in log


def test_login_modus_nutzt_den_cache_wenn_er_traegt(monkeypatch):
    monkeypatch.setenv("GRAPH_AUTH", "login")
    monkeypatch.setenv("GRAPH_TOKEN", "eyJ0gueltig")
    mit_schluessel, mit_login, gesehen = _wege(cache_taugt=True,
                                               interaktiv_erlaubt=False)
    assert auth.waehle_zugang(mit_schluessel, mit_login, ausgabe=_Protokoll()) == "LOGIN"
    assert gesehen == ["still"]


def test_ohne_cache_und_ohne_schluessel_bricht_ab(monkeypatch):
    """Never a window from a subprocess – the app takes over from here."""
    monkeypatch.setenv("GRAPH_AUTH", "login")
    monkeypatch.delenv("GRAPH_TOKEN", raising=False)
    mit_schluessel, mit_login, gesehen = _wege(cache_taugt=False)
    with pytest.raises(SystemExit):
        auth.waehle_zugang(mit_schluessel, mit_login, ausgabe=_Protokoll())
    assert gesehen == ["still"]


def test_zeitplan_reisst_nie_ein_fenster_auf(monkeypatch):
    """The schedule too ends quietly with the event – nobody is watching."""
    monkeypatch.setenv("GRAPH_AUTH", "login")
    monkeypatch.delenv("GRAPH_TOKEN", raising=False)
    mit_schluessel, mit_login, gesehen = _wege(cache_taugt=False,
                                               interaktiv_erlaubt=False)
    with pytest.raises(SystemExit):
        auth.waehle_zugang(mit_schluessel, mit_login, ausgabe=_Protokoll())
    assert gesehen == ["still"]


def test_schluesselmodus_oeffnet_nie_ungefragt_ein_fenster(monkeypatch):
    """The opposite direction: whoever picks the key should not be
    surprised."""
    monkeypatch.setenv("GRAPH_AUTH", "token")
    monkeypatch.setenv("GRAPH_TOKEN", "eyJ0gueltig")
    mit_schluessel, mit_login, gesehen = _wege(cache_taugt=True,
                                               interaktiv_erlaubt=False)
    assert auth.waehle_zugang(mit_schluessel, mit_login,
                              ausgabe=_Protokoll()) == "KEY:eyJ0gueltig"
    assert gesehen == [], "im Schlüssel-Modus wurde die Anmeldung angefasst"
