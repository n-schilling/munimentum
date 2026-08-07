"""Tests für settings.py – app_config.json als Vorgabeschicht der Einzelskripte.

Kernzusage, die hier abgesichert wird:

    Umgebungsvariable  >  app_config.json  >  eingebaute Vorgabe

Dazu, dass eine fehlende oder kaputte Datei nie einen Lauf verhindert, und dass
report() nur meldet, was auch wirklich gegriffen hat.
"""

import json
import os
import sys

import pytest

import settings


@pytest.fixture(autouse=True)
def sauber(tmp_path, monkeypatch):
    """Jeder Test bekommt einen eigenen Datenordner und einen leeren Puffer."""
    monkeypatch.setenv("OFFICE365_DATA_DIR", str(tmp_path))
    settings.reset()
    yield tmp_path
    settings.reset()


def schreibe(tmp_path, **werte):
    (tmp_path / settings.CONFIG_NAME).write_text(
        json.dumps(werte, ensure_ascii=False), encoding="utf-8")
    settings.reset()


# --------------------------------------------------------------------------
# Datei finden und lesen
# --------------------------------------------------------------------------
def test_config_path_folgt_dem_datenordner(sauber):
    assert settings.config_path() == sauber / settings.CONFIG_NAME


def test_config_path_ohne_datenordner_liegt_neben_dem_modul(monkeypatch):
    monkeypatch.delenv("OFFICE365_DATA_DIR", raising=False)
    settings.reset()
    assert settings.config_path().parent.name == "office365-export"


def test_load_ohne_datei(sauber):
    assert settings.load() == {}


@pytest.mark.parametrize("inhalt", ["{kein json", "", "[1, 2]", '"nur ein Text"'])
def test_load_bei_unbrauchbarer_datei(sauber, inhalt):
    """Ein kaputtes app_config.json darf einen Export niemals verhindern –
    es liefert ja nur Vorgaben."""
    (sauber / settings.CONFIG_NAME).write_text(inhalt, encoding="utf-8")
    settings.reset()
    assert settings.load() == {}


def test_load_puffert(sauber):
    schreibe(sauber, workers=2)
    assert settings.load()["workers"] == 2
    (sauber / settings.CONFIG_NAME).write_text('{"workers": 8}', encoding="utf-8")
    assert settings.load()["workers"] == 2          # gepuffert
    settings.reset()
    assert settings.load()["workers"] == 8


# --------------------------------------------------------------------------
# Rangfolge: Umgebung > Datei > Vorgabe
# --------------------------------------------------------------------------
def test_flag_vorgabe_ohne_alles(sauber):
    assert settings.flag("X_FLAG", "x_flag", True) is True
    assert settings.flag("X_FLAG", "x_flag", False) is False


def test_flag_aus_der_datei(sauber):
    schreibe(sauber, x_flag=False)
    assert settings.flag("X_FLAG", "x_flag", True) is False


def test_flag_umgebung_sticht_die_datei_aus(sauber, monkeypatch):
    """Damit ein einzelner Lauf die Datei übergehen kann – und damit die App,
    die alles als Umgebungsvariable mitgibt, eindeutig bleibt."""
    schreibe(sauber, x_flag=False)
    monkeypatch.setenv("X_FLAG", "1")
    assert settings.flag("X_FLAG", "x_flag", True) is True


@pytest.mark.parametrize("roh,erwartet", [
    ("0", False), ("false", False), ("no", False), ("nein", False),
    ("off", False), ("", False), ("1", True), ("true", True), ("ja", True),
    ("  1  ", True), ("FALSE", False),
])
def test_flag_schreibweisen(sauber, monkeypatch, roh, erwartet):
    monkeypatch.setenv("X_FLAG", roh)
    assert settings.flag("X_FLAG", "x_flag", True) is erwartet


def test_flag_ignoriert_unpassenden_typ_in_der_datei(sauber):
    schreibe(sauber, x_flag="vielleicht")
    assert settings.flag("X_FLAG", "x_flag", True) is True


def test_number_rangfolge(sauber, monkeypatch):
    assert settings.number("X_ZAHL", "x_zahl", 4) == 4
    schreibe(sauber, x_zahl=2)
    assert settings.number("X_ZAHL", "x_zahl", 4) == 2
    monkeypatch.setenv("X_ZAHL", "7")
    assert settings.number("X_ZAHL", "x_zahl", 4) == 7


@pytest.mark.parametrize("roh", ["vier", "", None, True])
def test_number_ignoriert_unbrauchbares(sauber, roh):
    schreibe(sauber, x_zahl=roh)
    assert settings.number("X_ZAHL", "x_zahl", 4) == 4


def test_number_haelt_die_untergrenze(sauber):
    schreibe(sauber, x_zahl=0)
    assert settings.number("X_ZAHL", "x_zahl", 4) == 1


def test_value_liest_nur_die_datei(sauber, monkeypatch):
    assert settings.value("x_text", "vorgabe") == "vorgabe"
    schreibe(sauber, x_text="aus datei")
    monkeypatch.setenv("X_TEXT", "aus umgebung")
    assert settings.value("x_text", "vorgabe") == "aus datei"


def test_folders_rangfolge(sauber, monkeypatch):
    assert settings.folders("X_ORD", "x_ord", {"a"}) == {"a"}
    schreibe(sauber, x_ord=["Archiv", " Drafts "])
    assert settings.folders("X_ORD", "x_ord", {"a"}) == {"archiv", "drafts"}
    monkeypatch.setenv("X_ORD", "Junk, Outbox")
    assert settings.folders("X_ORD", "x_ord", {"a"}) == {"junk", "outbox"}


def test_folders_leere_umgebung_heisst_nichts_auslassen(sauber, monkeypatch):
    """Der Unterschied zu "nicht gesetzt": die App muss ausdrücken können, dass
    wirklich alle Ordner exportiert werden sollen."""
    schreibe(sauber, x_ord=["archiv"])
    monkeypatch.setenv("X_ORD", "")
    assert settings.folders("X_ORD", "x_ord", {"a"}) == set()


def test_folders_ignoriert_unpassenden_typ(sauber):
    schreibe(sauber, x_ord="Archiv")          # Text statt Liste
    assert settings.folders("X_ORD", "x_ord", {"a"}) == {"a"}


# --------------------------------------------------------------------------
# report(): nur melden, was wirklich gegriffen hat
# --------------------------------------------------------------------------
def test_report_ohne_datei_ist_leer(sauber):
    settings.flag("X_FLAG", "x_flag", True)
    settings.number("X_ZAHL", "x_zahl", 4)
    assert settings.report() == ""


def test_report_nennt_die_uebernommenen_werte(sauber):
    schreibe(sauber, x_flag=False, x_zahl=2, x_ord=["archiv"])
    settings.flag("X_FLAG", "x_flag", True)
    settings.number("X_ZAHL", "x_zahl", 4)
    settings.folders("X_ORD", "x_ord", set())
    text = settings.report()
    assert text.startswith("Aus app_config.json übernommen:")
    assert "x_flag=aus" in text and "x_zahl=2" in text and "x_ord=1 Ordner" in text


def test_report_schweigt_bei_werten_wie_die_vorgabe(sauber):
    """Eine Datei, die nur die Vorgaben wiederholt, ändert nichts – dann ist die
    Meldung nur Rauschen."""
    schreibe(sauber, x_flag=True, x_zahl=4, x_ord=["a"])
    settings.flag("X_FLAG", "x_flag", True)
    settings.number("X_ZAHL", "x_zahl", 4)
    settings.folders("X_ORD", "x_ord", {"a"})
    assert settings.report() == ""


def test_report_schweigt_wenn_die_umgebung_gewinnt(sauber, monkeypatch):
    """Genau der Fall der App: sie setzt alles als Umgebungsvariable, also darf
    kein Skript behaupten, es habe etwas aus der Datei übernommen."""
    schreibe(sauber, x_flag=False, x_zahl=2)
    monkeypatch.setenv("X_FLAG", "1")
    monkeypatch.setenv("X_ZAHL", "8")
    settings.flag("X_FLAG", "x_flag", True)
    settings.number("X_ZAHL", "x_zahl", 4)
    assert settings.report() == ""


def test_value_taucht_nie_im_report_auf(sauber):
    """value() liefert nur Vorgaben für Argumente, die die Kommandozeile
    aussticht – "übernommen" wäre dort falsch, sobald jemand sie mitgibt."""
    schreibe(sauber, x_text="anders")
    assert settings.value("x_text", "vorgabe") == "anders"
    assert settings.report() == ""


# --------------------------------------------------------------------------
# Die eigentliche Zusage: die Datei gilt auch beim direkten Aufruf
# --------------------------------------------------------------------------
SKRIPT = """
import json, os, sys
sys.path.insert(0, {repo!r})
import settings
settings.reset()
import {modul} as m
print(json.dumps({{{felder}}}))
"""


def _lies_konstanten(tmp_path, modul, felder, umgebung=None):
    """Ein Skript frisch in einem eigenen Prozess importieren und seine
    Konstanten auslesen – nur so greift die Auswertung beim Import wirklich."""
    import subprocess
    from pathlib import Path
    repo = str(Path(__file__).resolve().parent.parent)
    code = SKRIPT.format(repo=repo, modul=modul,
                         felder=", ".join(f'"{f}": m.{f}' for f in felder))
    env = {**os.environ, "OFFICE365_DATA_DIR": str(tmp_path), **(umgebung or {})}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_teams_export_liest_die_datei(sauber):
    """Ohne app_config.json die Vorgaben, mit ihr deren Werte."""
    vorher = _lies_konstanten(sauber, "teams_export",
                              ["EMBED_IMAGES", "CACHE_IMAGES", "OUT_ROOT"])
    assert vorher == {"EMBED_IMAGES": True, "CACHE_IMAGES": True,
                      "OUT_ROOT": "teams_export"}

    schreibe(sauber, embed_images=False, cache_images=False, teams_dir="mein_archiv")
    nachher = _lies_konstanten(sauber, "teams_export",
                               ["EMBED_IMAGES", "CACHE_IMAGES", "OUT_ROOT"])
    assert nachher == {"EMBED_IMAGES": False, "CACHE_IMAGES": False,
                       "OUT_ROOT": "mein_archiv"}


def test_outlook_export_liest_die_datei(sauber):
    schreibe(sauber, include_hidden=True, skip_folders=["archiv"],
             outlook_dir="postfach")
    werte = _lies_konstanten(sauber, "outlook_export",
                             ["INCLUDE_HIDDEN", "OUT_ROOT"])
    assert werte == {"INCLUDE_HIDDEN": True, "OUT_ROOT": "postfach"}


def test_umgebung_sticht_die_datei_auch_im_skript_aus(sauber):
    """So ruft app.py die Skripte auf – die Datei darf dort nichts verändern."""
    schreibe(sauber, embed_images=False, cache_images=False)
    werte = _lies_konstanten(sauber, "teams_export", ["EMBED_IMAGES", "CACHE_IMAGES"],
                             umgebung={"EMBED_IMAGES": "1", "CACHE_IMAGES": "1"})
    assert werte == {"EMBED_IMAGES": True, "CACHE_IMAGES": True}
