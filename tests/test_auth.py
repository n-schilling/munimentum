"""Tests für auth.py – die Anmeldung, die sich beide Export-Skripte teilen.

Nie ins Netz und nie ein Anmeldefenster: msal wird durchweg ersetzt.

Zwei Zusagen stehen im Mittelpunkt, weil an ihnen Läufe hängen:

  * Der Rückfall geht nur in eine Richtung. Ist „login“ eingestellt und kein
    Cache da, darf ein hinterlegter Schlüssel einspringen. Umgekehrt nie – wer
    den Schlüssel-Modus wählt, soll nicht überraschend ein Anmeldefenster sehen.
  * Der Cache landet wirklich auf der Platte. Genau daran hängt, ob ein
    Zeitplan einen Neustart überlebt; ohne ihn wäre der Login-Modus nur eine
    umständlichere Variante des Schlüssels.
"""

import json
import sys
import types

import pytest

import auth
import settings


@pytest.fixture(autouse=True)
def sauber(tmp_path, monkeypatch):
    """Eigener Datenordner, leerer Puffer, keine geerbten Variablen."""
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
# Modus und Ziel
# --------------------------------------------------------------------------
def test_modus_ist_ohne_alles_der_schluessel(sauber):
    """Der Weg, der ohne Rückfrage bei der IT funktioniert, bleibt die Vorgabe."""
    assert auth.modus() == "token"


@pytest.mark.parametrize("wert,erwartet", [
    ("login", "login"), ("LOGIN", "login"), (" login ", "login"),
    ("token", "token"), ("quatsch", "token"), ("", "token"),
])
def test_modus_aus_der_konfiguration(sauber, wert, erwartet):
    """Ein Tippfehler fällt auf den Weg zurück, der immer geht."""
    konfig(sauber, auth_mode=wert)
    assert auth.modus() == erwartet


def test_modus_umgebung_schlaegt_datei(sauber, monkeypatch):
    konfig(sauber, auth_mode="token")
    monkeypatch.setenv("GRAPH_AUTH", "login")
    assert auth.modus() == "login"


def test_ohne_angabe_microsofts_oeffentliche_anwendung(sauber):
    """Für die braucht es keine Registrierung – deshalb ist sie die Vorgabe."""
    assert auth.client_id() == auth.STANDARD_CLIENT_ID
    assert auth.tenant() == auth.STANDARD_TENANT
    assert auth.eigene_registrierung() is False


def test_eigene_registrierung_wird_erkannt(sauber):
    konfig(sauber, client_id="11111111-2222-3333-4444-555555555555",
           tenant="contoso.onmicrosoft.com")
    assert auth.eigene_registrierung() is True
    assert auth.authority().endswith("contoso.onmicrosoft.com")


def test_leere_angaben_zaehlen_als_keine(sauber):
    """Ein geleertes Feld in der Oberfläche darf nicht in eine kaputte
    Authority münden."""
    konfig(sauber, client_id="", tenant="")
    assert auth.client_id() == auth.STANDARD_CLIENT_ID
    assert auth.eigene_registrierung() is False


# --------------------------------------------------------------------------
# Schlüssel lesen
# --------------------------------------------------------------------------
def test_schluessel_aus_der_umgebung(sauber, monkeypatch):
    monkeypatch.setenv("GRAPH_TOKEN", '  "Bearer eyJ0abc"  ')
    assert auth.load_pasted_token() == "eyJ0abc"


def test_schluessel_neben_dem_aufruf_schlaegt_datenordner(sauber, tmp_path):
    """„gx_token.txt neben dieses Skript legen“ ist die dokumentierte Regel –
    wer das tut, soll damit auch gewinnen."""
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
# msal-Ersatz
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
    """Verhält sich wie msal.PublicClientApplication, nur ohne Netz."""

    letzte = None

    def __init__(self, client_id, authority=None, token_cache=None):
        self.client_id = client_id
        self.authority = authority
        self.cache = token_cache
        self.konten = []
        self.still = None            # Antwort auf acquire_token_silent
        self.interaktiv = None       # Antwort auf acquire_token_interactive
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
# Anmeldung
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
    """Der Zeitplan läuft nachts – ein Anmeldefenster wartete bis zum Morgen."""
    anmeldung = auth.Login(["S"])
    anmeldung.app.interaktiv = {"access_token": "darf-nicht"}
    assert anmeldung.anmelden(nur_still=True) is False
    assert "interaktiv" not in [art for art, _ in anmeldung.app.gesehen]


def test_weich_meldet_misserfolg_statt_abzubrechen(sauber, fake_msal):
    """Der Teams-Export braucht das: erst Kanalrechte fragen, bei Ablehnung
    mit reinem Chat-Zugriff weiter."""
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
# Cache auf der Platte – daran hängt der unbeaufsichtigte Zeitplan
# --------------------------------------------------------------------------
def test_cache_wird_geschrieben(sauber, fake_msal, tmp_path):
    anmeldung = auth.Login(["S"])
    anmeldung.app.interaktiv = {"access_token": "tok"}
    anmeldung.anmelden()
    datei = tmp_path / auth.CACHE_DATEI
    assert datei.exists(), "ohne Datei überlebt der Zeitplan keinen Neustart"
    assert not datei.with_name(datei.name + ".tmp").exists()


def test_cache_gehoert_nur_dem_besitzer(sauber, fake_msal, tmp_path):
    """Darin steht das Refresh Token – wochenlang gültig."""
    if sys.platform.startswith("win"):
        pytest.skip("Windows kennt den Dateimodus nicht")
    anmeldung = auth.Login(["S"])
    anmeldung.app.interaktiv = {"access_token": "tok"}
    anmeldung.anmelden()
    assert (tmp_path / auth.CACHE_DATEI).stat().st_mode & 0o077 == 0


def test_kaputter_cache_haelt_nicht_auf(sauber, fake_msal, tmp_path):
    (tmp_path / auth.CACHE_DATEI).write_text("kaputt", encoding="utf-8")
    anmeldung = auth.Login(["S"])          # wirft nicht
    anmeldung.app.interaktiv = {"access_token": "tok"}
    assert anmeldung.anmelden() is True


def test_abmelden_verwirft_den_cache(sauber, fake_msal, tmp_path):
    (tmp_path / auth.CACHE_DATEI).write_text("{}", encoding="utf-8")
    assert auth.cache_leeren() is True
    assert not (tmp_path / auth.CACHE_DATEI).exists()
    assert auth.cache_leeren() is True      # zweimal ist auch in Ordnung


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
# Den Weg wählen – hier entscheidet sich, ob ein Lauf startet
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
    """Kein Fenster aus einem Unterprozess: die App zeigt den Token-Assistenten,
    sobald das strukturierte Ereignis kommt."""
    gesehen, k, ell = wege()
    with pytest.raises(SystemExit):
        auth.waehle_zugang(k, ell, ausgabe=lambda *_: None)
    assert gesehen == []
    assert "token_expired" in capsys.readouterr().out


def test_loginmodus_versucht_zuerst_still(sauber, monkeypatch):
    """Aus der Praxis gemeldet: der Export riss sofort den Browser auf, obwohl
    ein gültiger Schlüssel bereitlag. Der Rückfall hing an einem SystemExit –
    und das kommt erst, nachdem das Fenster offen war. Also erst still fragen.
    """
    monkeypatch.setenv("GRAPH_AUTH", "login")
    monkeypatch.setenv("GRAPH_TOKEN", "abc")
    gesehen, k, ell = wege()
    assert auth.waehle_zugang(k, ell, ausgabe=lambda *_: None) == "L"
    assert gesehen == [("login", True)], "nicht still versucht"


def test_loginmodus_faellt_auf_den_schluessel_zurueck(sauber, monkeypatch):
    """Cache weg, aber ein Schlüssel liegt bereit: dann läuft der Export – und
    zwar ohne dass jemand ein Anmeldefenster wegklicken muss."""
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
    """Kein Cache, kein Schlüssel – kein Fenster, sondern das Ereignis, auf das
    die App mit ihrem Token-Assistenten reagiert."""
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
# Die Skripte nutzen wirklich den gemeinsamen Weg
# --------------------------------------------------------------------------
@pytest.mark.parametrize("modul", ["outlook_export", "teams_export"])
def test_skript_nutzt_auth(modul):
    """Sonst liefe die Anmeldung wieder auseinander – genau das war der Anlass."""
    from pathlib import Path
    quelle = (Path(__file__).resolve().parent.parent / f"{modul}.py").read_text(
        encoding="utf-8")
    assert "import auth" in quelle
    assert "auth.waehle_zugang(" in quelle
    assert "msal.PublicClientApplication" not in quelle, "eigene Anmeldung übrig"
    assert "def load_pasted_token" not in quelle, "eigene Schlüsselfunktion übrig"


# --------------------------------------------------------------------------
# Login-Modus mit bereitliegendem Schlüssel
#
# Aus der Praxis gemeldet: „python3 outlook_export.py -default“ riss sofort den
# Browser auf, obwohl ein gültiger Schlüssel in gx_token.txt lag. Der Rückfall
# gab es zwar, aber er hing an einem SystemExit – und das kommt erst, nachdem
# das Anmeldefenster offen war und jemand es weggeklickt hat.
# --------------------------------------------------------------------------
class _Protokoll:
    def __init__(self):
        self.zeilen = []

    def __call__(self, *args):
        self.zeilen.append(" ".join(str(a) for a in args))

    def __contains__(self, text):
        return any(text in z for z in self.zeilen)


def _wege(cache_taugt, interaktiv_erlaubt=True):
    """(mit_schluessel, mit_login, gesehen) – mit_login merkt sich das Wie."""
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
    """Der gemeldete Fall. Zuerst still versuchen; scheitert das und liegt ein
    Schlüssel bereit, wird der genommen – ohne Browser."""
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
    """Nie ein Fenster aus einem Unterprozess – die App übernimmt von hier."""
    monkeypatch.setenv("GRAPH_AUTH", "login")
    monkeypatch.delenv("GRAPH_TOKEN", raising=False)
    mit_schluessel, mit_login, gesehen = _wege(cache_taugt=False)
    with pytest.raises(SystemExit):
        auth.waehle_zugang(mit_schluessel, mit_login, ausgabe=_Protokoll())
    assert gesehen == ["still"]


def test_zeitplan_reisst_nie_ein_fenster_auf(monkeypatch):
    """Auch der Zeitplan endet still mit dem Ereignis – niemand sitzt davor."""
    monkeypatch.setenv("GRAPH_AUTH", "login")
    monkeypatch.delenv("GRAPH_TOKEN", raising=False)
    mit_schluessel, mit_login, gesehen = _wege(cache_taugt=False,
                                               interaktiv_erlaubt=False)
    with pytest.raises(SystemExit):
        auth.waehle_zugang(mit_schluessel, mit_login, ausgabe=_Protokoll())
    assert gesehen == ["still"]


def test_schluesselmodus_oeffnet_nie_ungefragt_ein_fenster(monkeypatch):
    """Die Gegenrichtung: wer den Schlüssel wählt, soll nicht überrascht
    werden."""
    monkeypatch.setenv("GRAPH_AUTH", "token")
    monkeypatch.setenv("GRAPH_TOKEN", "eyJ0gueltig")
    mit_schluessel, mit_login, gesehen = _wege(cache_taugt=True,
                                               interaktiv_erlaubt=False)
    assert auth.waehle_zugang(mit_schluessel, mit_login,
                              ausgabe=_Protokoll()) == "KEY:eyJ0gueltig"
    assert gesehen == [], "im Schlüssel-Modus wurde die Anmeldung angefasst"
