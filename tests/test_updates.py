"""Tests für updates.py – nachsehen, ob es ein neueres Release gibt.

Kein Netz: requests.get wird immer ersetzt. Wichtigster Fall ist der, den es
heute tatsächlich gibt – es wurde noch nichts veröffentlicht. GitHub antwortet
dann mit 404, und das ist kein Fehler, sondern der Normalzustand.
"""

import pytest

import updates
import version


class Antwort:
    def __init__(self, status=200, payload=None, roh=None):
        self.status_code = status
        self._payload = payload
        self._roh = roh

    def json(self):
        if self._roh is not None:
            raise ValueError("keine gültige Antwort")
        return self._payload


@pytest.fixture
def github(monkeypatch):
    """requests.get durch eine feste Antwort ersetzen; liefert die Aufrufe."""
    aufrufe = []

    def setze(antwort):
        def fake(url, **kw):
            aufrufe.append({"url": url, **kw})
            if isinstance(antwort, Exception):
                raise antwort
            return antwort
        monkeypatch.setattr("requests.get", fake)
        return aufrufe
    return setze


# --------------------------------------------------------------------------
# Versionen vergleichen
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text,erwartet", [
    ("1.2.3", (1, 2, 3)),
    ("v1.2.3", (1, 2, 3)),
    ("V1.2.3", (1, 2, 3)),
    ("1.2", (1, 2)),
    ("1.2.0-beta.1", (1, 2, 0)),        # Vorabkennzeichen fällt weg
    ("1.2.0+build7", (1, 2, 0)),
    ("  v2.0.0  ", (2, 0, 0)),
    ("", ()),
    ("irgendwas", ()),
    (None, ()),
])
def test_parse_version(text, erwartet):
    assert updates.parse_version(text) == erwartet


@pytest.mark.parametrize("neu,alt,erwartet", [
    ("1.0.1", "1.0.0", True),
    ("1.1.0", "1.0.9", True),
    ("2.0.0", "1.9.9", True),
    ("v1.1.0", "1.0.0", True),
    ("1.0.0", "1.0.0", False),
    ("1.0", "1.0.0", False),            # 1.0 und 1.0.0 sind dieselbe Version
    ("1.0.0", "1.0", False),
    ("1.0.0", "1.0.1", False),          # älter -> keine Meldung
    ("0.9.0", "1.0.0", False),
    ("1.10.0", "1.9.0", True),          # nicht alphabetisch vergleichen
])
def test_is_newer(neu, alt, erwartet):
    assert updates.is_newer(neu, alt) is erwartet


@pytest.mark.parametrize("neu,alt", [("unsinn", "1.0.0"), ("1.0.0", "unsinn"),
                                     ("", ""), (None, "1.0.0")])
def test_is_newer_bei_unvergleichbarem(neu, alt):
    """Lieber nichts melden als etwas Falsches behaupten."""
    assert updates.is_newer(neu, alt) is False


# --------------------------------------------------------------------------
# Die vier Ausgänge
# --------------------------------------------------------------------------
def test_check_findet_neueres_release(github):
    github(Antwort(200, {"tag_name": "v1.4.0",
                         "html_url": "https://github.com/x/y/releases/tag/v1.4.0"}))
    out = updates.check("1.2.0", "x/y")
    assert out["status"] == "ok" and out["newer"] is True
    assert out["latest"] == "1.4.0"          # ohne führendes v
    assert out["url"].endswith("v1.4.0")
    assert out["current"] == "1.2.0"


def test_check_bei_aktueller_version(github):
    github(Antwort(200, {"tag_name": "v1.2.0", "html_url": "u"}))
    out = updates.check("1.2.0", "x/y")
    assert out["status"] == "ok" and out["newer"] is False


def test_check_ohne_release_ist_kein_fehler(github):
    """Der Fall von heute: es wurde noch nichts veröffentlicht. GitHub antwortet
    auf /releases/latest mit 404 – auch dann, wenn es nur Entwürfe oder
    Vorabversionen gibt."""
    github(Antwort(404, {"message": "Not Found"}))
    out = updates.check("1.0.0", "x/y")
    assert out["status"] == "none"
    assert out["newer"] is False and out["error"] is None


def test_check_ohne_tag_im_release(github):
    github(Antwort(200, {"html_url": "u"}))
    assert updates.check("1.0.0", "x/y")["status"] == "none"


def test_check_bei_netzfehler(github):
    github(OSError("connection refused"))
    out = updates.check("1.0.0", "x/y")
    assert out["status"] == "error" and "connection refused" in out["error"]
    assert out["newer"] is False


def test_check_bei_sperre_wegen_zu_vieler_anfragen(github):
    github(Antwort(403))
    out = updates.check("1.0.0", "x/y")
    assert out["status"] == "error" and out["error"] == "HTTP 403"


def test_check_bei_unlesbarer_antwort(github):
    github(Antwort(200, roh="kein json"))
    assert updates.check("1.0.0", "x/y")["status"] == "error"


def test_check_abgeschaltet_fragt_gar_nicht(github):
    aufrufe = github(Antwort(200, {"tag_name": "v9.9.9", "html_url": "u"}))
    out = updates.check("1.0.0", "x/y", enabled=False)
    assert out["status"] == "off" and out["newer"] is False
    assert aufrufe == []                     # keine Verbindung nach draußen


def test_check_fragt_die_richtige_adresse(github):
    aufrufe = github(Antwort(404))
    updates.check("1.0.0", "n-schilling/office_365_exporter")
    assert aufrufe[0]["url"] == \
        "https://api.github.com/repos/n-schilling/office_365_exporter/releases/latest"
    assert aufrufe[0]["timeout"] == 4.0      # der Start darf nicht daran hängen


def test_check_wirft_niemals(github):
    """Ein Fehler in der Prüfung darf den Start nicht aufhalten."""
    class Boese:
        status_code = 200

        def json(self):
            raise RuntimeError("kaputt")
    github(Boese())
    with pytest.raises(RuntimeError):
        Boese().json()                       # der Fehler ist echt …
    assert updates.check("1.0.0", "x/y")["status"] in ("error", "none")   # … wird aber gefangen


# --------------------------------------------------------------------------
# Versionsnummer: eine Quelle
# --------------------------------------------------------------------------
def test_version_hat_das_erwartete_format():
    assert updates.parse_version(version.VERSION), "VERSION ist nicht lesbar"
    assert not version.VERSION.startswith("v")      # das "v" trägt nur der Tag


def test_mcp_server_meldet_dieselbe_version():
    import mcp_server
    assert mcp_server.mcp.version == version.VERSION


def test_spec_nimmt_die_version_aus_der_datei():
    from pathlib import Path
    spec = (Path(__file__).resolve().parent.parent / "packaging" / "app.spec")
    text = spec.read_text(encoding="utf-8")
    assert '"CFBundleShortVersionString": VERSION' in text
    assert '"1.0.0"' not in text                    # nicht doppelt gepflegt
