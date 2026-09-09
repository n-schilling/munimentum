"""steps.py – the registry itself, checked as data.

The registry replaced four hand-maintained layers plus two side registers
(RUNNABLE, the bundle module list). What used to go wrong is exactly what
these tests pin down: a script the bundle cannot run, a label or source name
no language file knows, a schedule toggle the settings never offer. Every
new registry entry is covered without writing a test for it.
"""

import re
import json
from pathlib import Path

import app as app_mod
import steps
import settings

WURZEL = Path(__file__).resolve().parents[1]


def _de():
    return json.loads((WURZEL / "lang" / "de.json").read_text(encoding="utf-8"))


def test_jeder_schritt_hat_die_pflichtfelder():
    pflicht = {"key", "anfrage", "script", "label", "argv", "env",
               "corpus", "zugang", "schedule", "master", "quelle"}
    for e in steps.REGISTRY:
        fehlt = pflicht - set(e)
        assert not fehlt, f"{e.get('key')}: {sorted(fehlt)} fehlt"


def test_schluessel_und_anfragen_sind_eindeutig():
    keys = [e["key"] for e in steps.REGISTRY]
    anfragen = [e["anfrage"] for e in steps.REGISTRY]
    assert len(keys) == len(set(keys))
    assert len(anfragen) == len(set(anfragen))


def test_jedes_skript_ist_startbar():
    """The Planner export showed up with an empty alert: its script was
    missing from RUNNABLE. Now no registry entry can slip past this."""
    for e in steps.REGISTRY:
        assert e["script"] in app_mod.RUNNABLE, e["key"]


def test_jedes_skript_ist_im_buendel():
    spec = (WURZEL / "packaging" / "app.spec").read_text(encoding="utf-8")
    treffer = re.search(r"TEILPROGRAMME = \[(.*?)\]", spec, re.S)
    gebuendelt = set(re.findall(r'"([^"]+)"', treffer.group(1)))
    for e in steps.REGISTRY:
        assert e["script"] in gebuendelt, e["key"]
    # The new modules of the split itself must come along as well.
    for modul in ("page", "steps", "runner"):
        assert modul in gebuendelt, modul


def test_jede_beschriftung_ist_uebersetzt():
    de = _de()
    for e in steps.REGISTRY:
        label = e["label"]
        # The dynamic labels depend only on these two switches.
        kandidaten = ([label] if not callable(label) else
                      [label({"embeddings": an, "reconstruct": an})
                       for an in (True, False)])
        for k in kandidaten:
            assert k in de, f"{e['key']}: {k}"


def test_jede_quelle_ist_uebersetzt_oder_klarname():
    de = _de()
    for e in steps.REGISTRY:
        q = e["quelle"]
        if q and "." in q:
            assert q in de, f"{e['key']}: {q}"


def test_jeder_zeitplan_schalter_existiert():
    plan = settings.VORGABEN["schedule"]
    for e in steps.REGISTRY:
        if e["schedule"]:
            assert e["schedule"] in plan, e["key"]
        if e["master"]:
            assert e["master"] in settings.VORGABEN, e["key"]


def test_plan_anfrage_achtet_die_hauptschalter():
    cfg = {"onedrive_enabled": False, "sharepoint_enabled": True,
           "sharepoint_pages_enabled": True, "planner_enabled": True}
    anfrage = steps.plan_anfrage({}, cfg)
    assert anfrage["onedrive"] is False       # master switch off
    assert anfrage["sharepoint"] is True      # empty plan means: everything on
    assert "sync_folders" not in anfrage      # no schedule, no entry


def test_braucht_zugang_nur_fuer_graph_schritte():
    assert steps.braucht_zugang({"outlook": True}) is True
    assert steps.braucht_zugang({"index": True, "calendar": True}) is False
    assert steps.braucht_zugang({}) is False


def test_build_steps_nimmt_die_anfrage_als_dict():
    """The request travels as one dict from the API body to the registry:
    every key the registry knows is honoured, an unknown one is ignored –
    so a new entry needs no new parameter anywhere in between."""
    cfg = dict(settings.VORGABEN)
    cfg["outlook_categories"] = ["mail"]
    keys = [s["key"] for s in app_mod.build_steps(cfg, {"index": True,
                                                        "unbekannt": True})]
    assert keys == ["index"]
    alle = {e["anfrage"]: True for e in steps.REGISTRY}
    gebaut = {s["key"] for s in app_mod.build_steps(cfg, alle)}
    assert gebaut >= {e["key"] for e in steps.REGISTRY
                      if "aktiv" not in e or e["aktiv"](cfg, {"cats_outlook": ["mail"], "cats_teams": []})}


def test_ui_metadaten_nennen_nur_quellen():
    meta = steps.ui_metadaten()
    assert set(meta) == {e["key"] for e in steps.REGISTRY if e["quelle"]}
    for wert in meta.values():
        assert wert["quelle"]
