"""Tests for settings.py – app_config.json as the default layer of the scripts.

The core promise secured here:

    environment variable  >  app_config.json  >  built-in default

Plus: a missing or broken file never prevents a run, and report() only
reports what actually took effect.
"""

import json
import os
import sys
from pathlib import Path

import pytest

import settings


@pytest.fixture(autouse=True)
def sauber(tmp_path, monkeypatch):
    """Every test gets its own data folder and an empty cache."""
    monkeypatch.setenv("MUNIMENTUM_DATA_DIR", str(tmp_path))
    settings.reset()
    yield tmp_path
    settings.reset()


def schreibe(tmp_path, **werte):
    (tmp_path / settings.CONFIG_NAME).write_text(
        json.dumps(werte, ensure_ascii=False), encoding="utf-8")
    settings.reset()


# --------------------------------------------------------------------------
# Finding and reading the file
# --------------------------------------------------------------------------
def test_config_path_folgt_dem_datenordner(sauber):
    assert settings.config_path() == sauber / settings.CONFIG_NAME


def test_config_path_ohne_datenordner_liegt_neben_dem_modul(monkeypatch):
    """Check against settings.__file__, not against a folder name: what the
    working directory is called is chance – different locally than in CI."""
    for _n in ("MUNIMENTUM_DATA_DIR", "OFFICE365_DATA_DIR"):
        monkeypatch.delenv(_n, raising=False)
    settings.reset()
    erwartet = Path(settings.__file__).resolve().parent / settings.CONFIG_NAME
    assert settings.config_path() == erwartet


def test_load_ohne_datei(sauber):
    assert settings.load() == {}


@pytest.mark.parametrize("inhalt", ["{kein json", "", "[1, 2]", '"nur ein Text"'])
def test_load_bei_unbrauchbarer_datei(sauber, inhalt):
    """A broken app_config.json must never prevent an export – after all,
    it only supplies defaults."""
    (sauber / settings.CONFIG_NAME).write_text(inhalt, encoding="utf-8")
    settings.reset()
    assert settings.load() == {}


def test_load_puffert(sauber):
    schreibe(sauber, workers=2)
    assert settings.load()["workers"] == 2
    (sauber / settings.CONFIG_NAME).write_text('{"workers": 8}', encoding="utf-8")
    assert settings.load()["workers"] == 2          # cached
    settings.reset()
    assert settings.load()["workers"] == 8


# --------------------------------------------------------------------------
# Precedence: environment > file > default
# --------------------------------------------------------------------------
def test_flag_vorgabe_ohne_alles(sauber):
    assert settings.flag("X_FLAG", "x_flag", True) is True
    assert settings.flag("X_FLAG", "x_flag", False) is False


def test_flag_aus_der_datei(sauber):
    schreibe(sauber, x_flag=False)
    assert settings.flag("X_FLAG", "x_flag", True) is False


def test_flag_umgebung_sticht_die_datei_aus(sauber, monkeypatch):
    """So a single run can override the file – and so the app, which passes
    everything as environment variables, stays unambiguous."""
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
    """The difference from "not set": the app must be able to express that
    really all folders are to be exported."""
    schreibe(sauber, x_ord=["archiv"])
    monkeypatch.setenv("X_ORD", "")
    assert settings.folders("X_ORD", "x_ord", {"a"}) == set()


def test_folders_ignoriert_unpassenden_typ(sauber):
    schreibe(sauber, x_ord="Archiv")          # text instead of a list
    assert settings.folders("X_ORD", "x_ord", {"a"}) == {"a"}




# --------------------------------------------------------------------------
# The actual promise: the file also holds when called directly
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
    """Import a script fresh in its own process and read out its constants
    – only then does the import-time evaluation really take effect."""
    import subprocess
    repo = str(Path(__file__).resolve().parent.parent)
    code = SKRIPT.format(repo=repo, modul=modul,
                         felder=", ".join(f'"{f}": m.{f}' for f in felder))
    env = {**os.environ, "MUNIMENTUM_DATA_DIR": str(tmp_path), **(umgebung or {})}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_teams_export_liest_die_datei(sauber):
    """Without app_config.json the defaults, with it its values."""
    vorher = _lies_konstanten(sauber, "teams_export",
                              ["EMBED_IMAGES", "CACHE_IMAGES"])
    assert vorher == {"EMBED_IMAGES": True, "CACHE_IMAGES": True}

    schreibe(sauber, embed_images=False, cache_images=False)
    nachher = _lies_konstanten(sauber, "teams_export",
                               ["EMBED_IMAGES", "CACHE_IMAGES"])
    assert nachher == {"EMBED_IMAGES": False, "CACHE_IMAGES": False}


def test_outlook_export_liest_die_datei(sauber):
    schreibe(sauber, include_hidden=True, skip_folders=["archiv"])
    werte = _lies_konstanten(sauber, "outlook_export", ["INCLUDE_HIDDEN"])
    assert werte == {"INCLUDE_HIDDEN": True}


def test_umgebung_sticht_die_datei_auch_im_skript_aus(sauber):
    """This is how app.py calls the scripts – the file must change nothing there."""
    schreibe(sauber, embed_images=False, cache_images=False)
    werte = _lies_konstanten(sauber, "teams_export", ["EMBED_IMAGES", "CACHE_IMAGES"],
                             umgebung={"EMBED_IMAGES": "1", "CACHE_IMAGES": "1"})
    assert werte == {"EMBED_IMAGES": True, "CACHE_IMAGES": True}


def test_alter_name_des_datenordners_gilt_weiter(tmp_path, monkeypatch):
    """OFFICE365_DATA_DIR is the switch's old name. Whoever still has it in
    a script or a shortcut should not suddenly land in an empty archive
    after the rename."""
    monkeypatch.delenv("MUNIMENTUM_DATA_DIR", raising=False)
    monkeypatch.setenv("OFFICE365_DATA_DIR", str(tmp_path))
    (tmp_path / "app_config.json").write_text('{"workers": 7}', encoding="utf-8")
    settings.reset()
    assert settings.value("workers", 4) == 7


def test_neuer_name_sticht_den_alten(tmp_path, monkeypatch):
    neu = tmp_path / "neu"
    neu.mkdir()
    (neu / "app_config.json").write_text('{"workers": 9}', encoding="utf-8")
    (tmp_path / "app_config.json").write_text('{"workers": 1}', encoding="utf-8")
    monkeypatch.setenv("OFFICE365_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MUNIMENTUM_DATA_DIR", str(neu))
    settings.reset()
    assert settings.value("workers", 4) == 9


# --------------------------------------------------------------------------
# The schema (VORGABEN): one source for app and scripts
# --------------------------------------------------------------------------
def test_vorgaben_gelten_ohne_eigenen_default(tmp_path, monkeypatch):
    """Without a third parameter the default comes from the schema – the
    call sites no longer carry a copy of their own."""
    monkeypatch.setenv("MUNIMENTUM_DATA_DIR", str(tmp_path))
    settings.reset()
    try:
        assert settings.flag("EMBED_IMAGES", "embed_images") is True
        assert settings.number("EXPORT_WORKERS", "workers") == 4
        assert settings.value("embed_model") == settings.VORGABEN["embed_model"]
        assert settings.folders("SKIP_FOLDERS", "skip_folders") == \
            set(settings.VORGABEN["skip_folders"])
    finally:
        settings.reset()


def test_ausdrueckliches_none_bleibt_none(tmp_path, monkeypatch):
    """value(key, None) means "detect not set" – the rule keys use this to
    tell the file fallback apart from an empty rule."""
    monkeypatch.setenv("MUNIMENTUM_DATA_DIR", str(tmp_path))
    settings.reset()
    try:
        assert settings.value("folder_rules", None) is None
        assert settings.value("folder_rules") == ""     # schema default
    finally:
        settings.reset()


def test_unbekannter_schluessel_ohne_default_schlaegt_fehl():
    """A typo in the key should fail loudly, not quietly return None."""
    import pytest
    with pytest.raises(KeyError):
        settings.value("gibt_es_nicht")
