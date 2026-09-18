"""openapi.yaml – the served API description must match the handler.

The spec is a promise to people scripting against the backend (expert mode).
These tests keep it honest: every route the handler answers appears in the
spec, every path the spec names exists in the handler, and the file stays
parseable. A new endpoint without a spec entry fails here on purpose.
"""

import re
from pathlib import Path

import yaml

import pytest

WURZEL = Path(__file__).resolve().parents[1]
SPEC = WURZEL / "openapi.yaml"


def handler_routen():
    """Every path the handler answers: the app's own, read out of the two
    dispatchers, plus the versioned surface's route table."""
    quelle = (WURZEL / "app.py").read_text(encoding="utf-8")
    routen = set(re.findall(r'u\.path == "([^"]+)"', quelle))
    for treffer in re.findall(r'u\.path in \(([^)]*)\)', quelle):
        routen.update(re.findall(r'"([^"]+)"', treffer))
    import app as app_mod
    routen.update(muster for _, muster, _ in app_mod.ROUTEN_V1)
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


def _spec():
    """The parsed file – read once, not once per test."""
    global _SPEC
    if _SPEC is None:
        _SPEC = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    return _SPEC


_SPEC = None


def _antworten():
    spec = _spec()
    for pfad, item in spec["paths"].items():
        for methode, op in item.items():
            if methode in ("get", "post", "put", "delete", "patch"):
                for code, antwort in (op.get("responses") or {}).items():
                    yield pfad, methode, code, antwort


def test_jede_antwort_hat_eine_beschreibung():
    """A Response object's `description` is REQUIRED by the specification –
    strict viewers (openapi-tui, for one) refuse the whole file without it."""
    ohne = [(p, m, c) for p, m, c, a in _antworten()
            if isinstance(a, dict) and "description" not in a and "$ref" not in a]
    assert not ohne, f"Antworten ohne description: {ohne}"


def test_enum_werte_bleiben_zeichenketten():
    """YAML reads a bare `off`, `on`, `yes` or `no` as a boolean – an enum
    that names the setting value "off" must quote it."""
    spec = _spec()
    schlecht = []

    def gehe(o, pfad):
        if isinstance(o, dict):
            if isinstance(o.get("enum"), list) and any(isinstance(v, bool) for v in o["enum"]):
                schlecht.append(pfad)
            for k, v in o.items():
                gehe(v, pfad + "/" + str(k))
        elif isinstance(o, list):
            for i, v in enumerate(o):
                gehe(v, f"{pfad}[{i}]")
    gehe(spec, "")
    assert not schlecht, f"boolesche enum-Werte (unquoted off/on?): {schlecht}"


def test_jede_operation_traegt_genau_einen_reiter():
    """The operations are grouped like the app's doors – Archive, Explore,
    Cases – plus App for state, access and settings; a viewer lists them
    under those headings. Every declared tag is used, none is invented."""
    spec = _spec()
    erklaert = [t["name"] for t in spec["tags"]]
    assert erklaert == ["Archive", "Explore", "Cases", "App"]
    benutzt = set()
    for pfad, item in spec["paths"].items():
        for methode, op in item.items():
            if methode in ("get", "post", "put", "delete", "patch"):
                assert len(op.get("tags") or []) == 1, f"{methode} {pfad} ohne genau einen Reiter"
                assert op["tags"][0] in erklaert, f"{methode} {pfad}: unbekannter Reiter {op['tags']}"
                benutzt.add(op["tags"][0])
    assert benutzt == set(erklaert)


def _operationen():
    for pfad, item in _spec()["paths"].items():
        for methode, op in item.items():
            if methode in ("get", "post", "put", "delete", "patch"):
                yield pfad, methode, op


def test_jede_operation_hat_eine_eindeutige_operation_id():
    """Generators and deep links need one; a duplicate breaks both silently."""
    kennungen = [op.get("operationId") for _, _, op in _operationen()]
    assert all(kennungen), "Operation ohne operationId"
    assert len(kennungen) == len(set(kennungen)), "operationId doppelt vergeben"
    assert all(re.fullmatch(r"[a-z][A-Za-z0-9]*", k) for k in kennungen), kennungen


def test_jeder_verweis_zeigt_auf_eine_komponente():
    """A `$ref` to a component that does not exist renders as an empty box
    in every viewer – nothing warns."""
    text = SPEC.read_text(encoding="utf-8")
    spec = _spec()
    for verweis in set(re.findall(r'\$ref: "#/components/([a-z]+)/([A-Za-z0-9]+)"', text)):
        art, name = verweis
        assert name in (spec.get("components") or {}).get(art, {}), f"#/components/{art}/{name} fehlt"


def test_jede_operation_nennt_die_gemeinsamen_fehler():
    """403 (wrong Host) and 500 (uncaught error) can answer every route:
    listed once under components/responses, referenced by every operation.
    A run-starting route says so with x-starts-run and is a POST."""
    for pfad, methode, op in _operationen():
        antworten = op["responses"]
        assert "403" in antworten and "500" in antworten, f"{methode} {pfad}: 403/500 fehlen"
        assert "405" in antworten, f"{methode} {pfad}: 405 fehlt"
        if methode in ("post", "patch", "put"):
            assert "415" in antworten, f"{methode} {pfad}: 415 fehlt"
        if op.get("x-starts-run"):
            assert methode == "post" and "409" in antworten, f"{pfad}: ein Lauf ohne 409"


def test_config_schema_kennt_jede_einstellung():
    """The Config schema lists every key of settings.VORGABEN with its
    default – a new setting without a line here drifts out of the spec."""
    import settings
    eigenschaften = _spec()["components"]["schemas"]["Config"]["properties"]
    assert set(eigenschaften) == set(settings.VORGABEN), (
        set(eigenschaften) ^ set(settings.VORGABEN))
    for k, v in settings.VORGABEN.items():
        if isinstance(v, (bool, int, str)):
            assert eigenschaften[k].get("default") == v, f"{k}: default {eigenschaften[k].get('default')!r} != {v!r}"


def test_jede_ablehnung_traegt_das_fehlerschema():
    """One shape for every refusal (11.4): whatever the route and the
    status, a 4xx/5xx answers the problem detail (RFC 9457) as
    `application/problem+json`. Anything else would make a caller read two
    shapes again."""
    falsch = []
    for pfad, methode, code, antwort in _antworten():
        if not code.startswith(("4", "5")) or "$ref" in antwort:
            continue
        inhalt = (antwort.get("content") or {}).get("application/problem+json") or {}
        if (inhalt.get("schema") or {}).get("$ref") != "#/components/schemas/Error":
            falsch.append((pfad, methode, code))
    assert not falsch, f"Ablehnungen ohne Error-Schema: {falsch}"


def test_wer_den_index_braucht_sagt_es_mit_503():
    """x-needs-index means: without a loaded index this route answers 503
    (srv.noindex) – not an empty list with HTTP 200."""
    ohne = [(p, m) for p, m, op in _operationen()
            if op.get("x-needs-index") and "503" not in op["responses"]]
    assert not ohne, f"x-needs-index ohne 503: {ohne}"
