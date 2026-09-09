"""openapi.yaml – the served API description must match the handler.

The spec is a promise to people scripting against the backend (expert mode).
These tests keep it honest: every route the handler answers appears in the
spec, every path the spec names exists in the handler, and the file stays
parseable. A new endpoint without a spec entry fails here on purpose.
"""

import re
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parents[1]
SPEC = WURZEL / "openapi.yaml"


def handler_routen():
    quelle = (WURZEL / "app.py").read_text(encoding="utf-8")
    routen = set(re.findall(r'u\.path == "([^"]+)"', quelle))
    for treffer in re.findall(r'u\.path in \(([^)]*)\)', quelle):
        routen.update(re.findall(r'"([^"]+)"', treffer))
    routen.discard("/index.html")        # alias of /
    return routen


def spec_pfade():
    text = SPEC.read_text(encoding="utf-8")
    # Path items are the two-space-indented keys of the top-level "paths:".
    treffer = re.search(r"^paths:\n(.*?)^components:", text, re.S | re.M)
    return set(re.findall(r"^  (/[^\s:]*):$", treffer.group(1), re.M))


def test_jede_handler_route_steht_in_der_spec():
    fehlt = handler_routen() - spec_pfade()
    assert not fehlt, f"in app.py, aber nicht in openapi.yaml: {sorted(fehlt)}"


def test_jeder_spec_pfad_existiert_im_handler():
    zuviel = spec_pfade() - handler_routen()
    assert not zuviel, f"in openapi.yaml, aber nicht in app.py: {sorted(zuviel)}"


def test_spec_ist_gueltiges_yaml():
    yaml = pytest.importorskip("yaml")
    daten = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    assert daten["openapi"].startswith("3.")
    assert set(daten["paths"]) == spec_pfade()


def test_spec_wird_ausgeliefert_und_gebuendelt():
    import app as app_mod
    assert '"/api/openapi"' in (WURZEL / "app.py").read_text(encoding="utf-8")
    assert (Path(app_mod.RES) / "openapi.yaml").exists()
    spec = (WURZEL / "packaging" / "app.spec").read_text(encoding="utf-8")
    assert "openapi.yaml" in spec, "the bundle would serve a 500 instead"
