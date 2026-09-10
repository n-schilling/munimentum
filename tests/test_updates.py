"""Tests for updates.py – checking whether a newer release exists.

No network: requests.get is always replaced. The most important case is
the one that actually exists today – nothing has been published yet.
GitHub then answers with 404, and that is not an error but the normal state.
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
    """Replace requests.get with a fixed response; returns the calls."""
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
# Comparing versions
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text,erwartet", [
    ("1.2.3", (1, 2, 3)),
    ("v1.2.3", (1, 2, 3)),
    ("V1.2.3", (1, 2, 3)),
    ("1.2", (1, 2)),
    ("1.2.0-beta.1", (1, 2, 0)),        # pre-release marker is dropped
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
    ("1.0", "1.0.0", False),            # 1.0 and 1.0.0 are the same version
    ("1.0.0", "1.0", False),
    ("1.0.0", "1.0.1", False),          # older -> no notice
    ("0.9.0", "1.0.0", False),
    ("1.10.0", "1.9.0", True),          # do not compare alphabetically
])
def test_is_newer(neu, alt, erwartet):
    assert updates.is_newer(neu, alt) is erwartet


@pytest.mark.parametrize("neu,alt", [("unsinn", "1.0.0"), ("1.0.0", "unsinn"),
                                     ("", ""), (None, "1.0.0")])
def test_is_newer_bei_unvergleichbarem(neu, alt):
    """Better to report nothing than to claim something wrong."""
    assert updates.is_newer(neu, alt) is False


# --------------------------------------------------------------------------
# The four outcomes
# --------------------------------------------------------------------------
def test_check_findet_neueres_release(github):
    github(Antwort(200, {"tag_name": "v1.4.0",
                         "html_url": "https://github.com/x/y/releases/tag/v1.4.0"}))
    out = updates.check("1.2.0", "x/y")
    assert out["status"] == "ok" and out["newer"] is True
    assert out["latest"] == "1.4.0"          # without the leading v
    assert out["url"].endswith("v1.4.0")
    assert out["current"] == "1.2.0"


def test_check_bei_aktueller_version(github):
    github(Antwort(200, {"tag_name": "v1.2.0", "html_url": "u"}))
    out = updates.check("1.2.0", "x/y")
    assert out["status"] == "ok" and out["newer"] is False


def test_check_ohne_release_ist_kein_fehler(github):
    """Today's case: nothing has been published yet. GitHub answers
    /releases/latest with 404 – even when there are only drafts or
    pre-releases."""
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
    assert aufrufe == []                     # no connection to the outside


def test_check_fragt_die_richtige_adresse(github):
    aufrufe = github(Antwort(404))
    updates.check("1.0.0", "n-schilling/office_365_exporter")
    assert aufrufe[0]["url"] == \
        "https://api.github.com/repos/n-schilling/office_365_exporter/releases/latest"
    assert aufrufe[0]["timeout"] == 4.0      # startup must not hang on this


def test_check_wirft_niemals(github):
    """An error in the check must not hold up startup."""
    class Boese:
        status_code = 200

        def json(self):
            raise RuntimeError("kaputt")
    github(Boese())
    with pytest.raises(RuntimeError):
        Boese().json()                       # the error is real …
    assert updates.check("1.0.0", "x/y")["status"] in ("error", "none")   # … but is caught


# --------------------------------------------------------------------------
# Version number: one source
# --------------------------------------------------------------------------
def test_version_hat_das_erwartete_format():
    assert updates.parse_version(version.VERSION), "VERSION ist nicht lesbar"
    assert not version.VERSION.startswith("v")      # only the tag carries the "v"


def test_mcp_server_meldet_dieselbe_version():
    import mcp_server
    assert mcp_server.mcp.version == version.VERSION


def test_spec_nimmt_die_version_aus_der_datei():
    from pathlib import Path
    spec = (Path(__file__).resolve().parent.parent / "packaging" / "app.spec")
    text = spec.read_text(encoding="utf-8")
    assert '"CFBundleShortVersionString": VERSION' in text
    assert '"1.0.0"' not in text                    # not maintained twice


# --------------------------------------------------------------------------
# Ahead: our own version is higher than anything published
#
# "You are up to date" would be untrue there – and whoever uses an
# unpublished build should know it.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("eigene,neueste,newer,ahead", [
    ("4.0.0", "v3.5.0", False, True),      # pre-release build
    ("3.5.0", "v4.0.0", True,  False),     # normal update
    ("3.5.0", "v3.5.0", False, False),     # equal – neither of the two
    ("3.5.0", "v3.5",   False, False),     # 3.5 and 3.5.0 are the same
    ("4.0.0", "v4.0.1", True,  False),
    ("4.1.0", "v4.0.9", False, True),
    ("wirr",  "v4.0.0", False, False),     # incomparable: claim nothing at all
    ("4.0.0", "wirr",   False, False),
])
def test_ahead_und_newer_schliessen_sich_aus(github, eigene, neueste, newer, ahead):
    github(Antwort(200, {"tag_name": neueste, "html_url": "https://x"}))
    u = updates.check(eigene, "n/x")
    assert u["status"] == "ok"
    assert (u["newer"], u["ahead"]) == (newer, ahead)
    assert not (u["newer"] and u["ahead"]), "beides zugleich ist nie richtig"


def test_ahead_ist_bei_jedem_anderen_ausgang_falsch(github):
    """Without a usable response nothing is claimed – not the opposite either."""
    for code in (404, 500):
        github(Antwort(code, {}))
        assert updates.check("4.0.0", "n/x")["ahead"] is False
    github(RuntimeError("kein Netz"))
    assert updates.check("4.0.0", "n/x")["ahead"] is False
    assert updates.check("4.0.0", "n/x", enabled=False)["ahead"] is False


def test_fehlermeldung_steht_im_protokoll_und_bei_der_app():
    """It belongs to the log – and, since the log lives on the archive page
    now, once more under Settings › App, next to the version. Not a third
    time, and not in the update line, where it was a foreign thing."""
    import app as app_mod
    seite = app_mod.seite()
    assert seite.count('data-i18n="report.button"') == 2
    kopf = seite[seite.index('id="protokoll"'):]
    assert 'data-i18n="report.button"' in kopf[:1200]
    app = seite[seite.index('id="app-karte"'):seite.index('id="expert-karte"')]
    assert 'data-i18n="report.button"' in app
    update = app[app.index('id="update-current"'):app.index('id="update-link"')]
    assert "report.button" not in update


def test_zu_den_releases_sieht_aus_wie_ein_knopf():
    """A link next to one with the same job should look the same too."""
    import app as app_mod
    i = app_mod.seite().index('id="update-link"')
    zeile = app_mod.seite()[app_mod.seite().rindex("<", 0, i):app_mod.seite().index(">", i) + 1]
    assert 'class="mini"' in zeile
    assert 'data-i18n="update.download"' in zeile
    assert "button.mini,a.mini{" in app_mod.seite()
