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


def test_jede_methode_steht_in_der_spec():
    """Not just the path – the method too. `GET /api/v1/profiles/{name}`
    hid behind a path item that already had a PATCH: the route answered,
    and the file said nothing about it. Both directions, so a described
    operation that no route serves fails here as well."""
    import app as app_mod
    handler = {(m, muster) for m, muster, _ in app_mod.ROUTEN_V1}
    handler |= {(m, pfad) for pfad, methoden in app_mod.Handler.ROUTEN.items()
                for m in methoden if pfad != "/index.html"}      # die Seite selbst
    beschrieben = {(m.upper(), p) for p, m, _ in _operationen()}
    fehlt = handler - beschrieben
    zuviel = beschrieben - handler
    assert not fehlt, f"in app.py, aber nicht in openapi.yaml: {sorted(fehlt)}"
    assert not zuviel, f"in openapi.yaml, aber nicht in app.py: {sorted(zuviel)}"


def test_no_key_appears_twice_in_a_mapping():
    """PyYAML keeps the last of two equal keys and says nothing; every
    JavaScript viewer refuses the file ("Map keys must be unique"). The
    sign-in operation carried "500" twice until 13.3.0 – found by a
    viewer, not by a test."""
    import yaml

    class Strict(yaml.SafeLoader):
        pass

    duplicates = []

    def mapping(loader, node, deep=False):
        seen = set()
        for k, _ in node.value:
            key = loader.construct_object(k, deep=deep)
            if key in seen:
                duplicates.append((node.start_mark.line + 1, key))
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    yaml.load(SPEC.read_text(encoding="utf-8"), Loader=Strict)
    assert not duplicates, f"duplicate keys (line, key): {duplicates}"


def test_spec_ist_gueltiges_yaml():
    yaml = pytest.importorskip("yaml")
    daten = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    # 3.2 or newer: `query` is a path item's own field only from there on.
    assert daten["openapi"].startswith("3.") and daten["openapi"] >= "3.2"
    assert set(daten["paths"]) == spec_pfade()


def test_spec_wird_ausgeliefert_und_gebuendelt():
    import app as app_mod
    assert '"/api/v1/openapi"' in (WURZEL / "app.py").read_text(encoding="utf-8")
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


# Where an operation can sit in a path item. `query` is one of them since
# OpenAPI 3.2, which added the field for QUERY (RFC 10008) – the read that
# carries its question in the body.
VERBEN = ("get", "post", "put", "delete", "patch", "query")


def _operationen():
    """Every operation in the file as (path, method, operation), the method
    lower case the way a path item spells it."""
    for pfad, item in _spec()["paths"].items():
        for schluessel, op in item.items():
            if schluessel in VERBEN:
                yield pfad, schluessel, op


def _antworten():
    for pfad, methode, op in _operationen():
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


def test_jede_operation_traegt_ihre_tuer_und_ihr_verb():
    """Two groupings, and every operation is in both: the app's doors –
    Archive, Explore, Cases, App – say where a route belongs, the HTTP verb
    says what it does, so a viewer can list all GETs as easily as one
    door's routes. Every declared tag is used, none is invented."""
    spec = _spec()
    erklaert = [t["name"] for t in spec["tags"]]
    tueren = ["Archive", "Explore", "Cases", "App"]
    verben = ["GET", "QUERY", "POST", "PATCH", "PUT", "DELETE"]
    assert erklaert == tueren + verben
    benutzt = set()
    for pfad, methode, op in _operationen():
        reiter = op.get("tags") or []
        assert len(reiter) == 2, f"{methode} {pfad}: Tuer und Verb, nicht {reiter}"
        assert reiter[0] in tueren, f"{methode} {pfad}: unbekannte Tuer {reiter[0]}"
        assert reiter[1] == methode.upper(), f"{methode} {pfad}: falsches Verb {reiter[1]}"
        benutzt.update(reiter)
    assert benutzt == set(erklaert)


def test_die_query_routen_stehen_unter_query():
    """The three reads that carry their question in the body are QUERY in
    the handler and `query` in the file – and nowhere does a `post` still
    stand next to one of them. A route that changes its method without its
    description would otherwise promise the old one."""
    import app as app_mod
    handler = {muster for m, muster, _ in app_mod.ROUTEN_V1 if m == "QUERY"}
    beschrieben = {pfad for pfad, methode, _ in _operationen() if methode == "query"}
    assert handler == beschrieben, handler ^ beschrieben
    for pfad in handler:
        assert "post" not in _spec()["paths"][pfad], f"{pfad}: POST steht noch in der Spec"


def test_jede_operation_hat_eine_eindeutige_operation_id():
    """Generators and deep links need one; a duplicate breaks both silently."""
    kennungen = [op.get("operationId") for _, _, op in _operationen()]
    assert all(kennungen), "Operation ohne operationId"
    assert len(kennungen) == len(set(kennungen)), "operationId doppelt vergeben"
    assert all(re.fullmatch(r"[a-z][A-Za-z0-9]*", k) for k in kennungen), kennungen


def test_jede_komponente_wird_auch_benutzt():
    """The other direction: a schema nobody references is documentation
    that no route promises – it drifts away from the code unnoticed, and a
    reader cannot tell which of the two is current. Eleven of them had
    piled up by 13.0, next to routes that answered a free-form object."""
    text = SPEC.read_text(encoding="utf-8")
    verwiesen = {(art, name) for art, name
                 in re.findall(r'\$ref: "#/components/([A-Za-z]+)/(\w+)"', text)}
    tot = {art: sorted(n for n in eintraege if (art, n) not in verwiesen)
           for art, eintraege in (_spec().get("components") or {}).items()}
    tot = {art: namen for art, namen in tot.items() if namen}
    assert not tot, f"definiert, aber nie referenziert: {tot}"


def test_keine_antwort_ist_ein_freier_sack():
    """A 200 that promises nothing but `object` is not a contract. Where a
    shape is genuinely open (a step's own detail, a report), it sits in a
    named schema that says so – not as the whole answer."""
    schlecht = []
    for pfad, methode, op in _operationen():
        inhalt = ((op.get("responses") or {}).get("200") or {}).get("content") or {}
        schema = (inhalt.get("application/json") or {}).get("schema") or {}
        if (schema.get("type") == "object" and schema.get("additionalProperties") is True
                and not schema.get("properties")):
            schlecht.append((methode, pfad))
    assert not schlecht, f"Antworten ohne Zusage: {schlecht}"


def test_jede_operation_sagt_was_sie_tut():
    """A summary names it, a description says what it does with what – and
    every operation has both. 17 were missing at 13.0."""
    ohne = [(m, p) for p, m, op in _operationen()
            if not op.get("summary") or not op.get("description")]
    assert not ohne, f"ohne summary oder description: {ohne}"


def test_jeder_verweis_zeigt_auf_eine_komponente():
    """A `$ref` to a component that does not exist renders as an empty box
    in every viewer – nothing warns."""
    text = SPEC.read_text(encoding="utf-8")
    spec = _spec()
    for verweis in set(re.findall(r'\$ref: "#/components/([A-Za-z]+)/([A-Za-z0-9]+)"', text)):
        art, name = verweis
        assert name in (spec.get("components") or {}).get(art, {}), f"#/components/{art}/{name} fehlt"


def test_die_quellen_der_spec_sind_die_des_codes():
    """Three vocabularies for `{source}`, each pinned to the table the
    handler resolves it against: the eight sources of the archive check,
    the ten rows of the balance, the six of the folder plan. An enum
    the handler does not enforce is a promise nobody keeps."""
    import app as app_mod
    import archive_check
    import steps
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    params = spec["components"]["parameters"]
    assert params["SourcePath"]["schema"]["enum"] == [
        q for q in app_mod.EXPORT_ORDNER if q in archive_check.PRUEFER]
    assert params["BalanceRow"]["schema"]["enum"] == [e["quelle"] for e in steps.PRUEFUNGEN]
    for pfad in ("/api/v1/balance/{row}", "/api/v1/balance/{row}/fetch"):
        assert spec["paths"][pfad]["parameters"] == [{"$ref": "#/components/parameters/BalanceRow"}], pfad
    plan = spec["paths"]["/api/v1/sources/{source}/folder-plan"]["parameters"][0]
    assert plan["schema"]["enum"] == list(app_mod.PLAN_QUELLEN)


def test_jede_operation_nennt_die_gemeinsamen_fehler():
    """403 (wrong Host) and 500 (uncaught error) can answer every route:
    listed once under components/responses, referenced by every operation.
    A run-starting route says so with x-starts-run and is a POST."""
    for pfad, methode, op in _operationen():
        antworten = op["responses"]
        assert "403" in antworten and "500" in antworten, f"{methode} {pfad}: 403/500 fehlen"
        assert "405" in antworten, f"{methode} {pfad}: 405 fehlt"
        if methode in ("post", "patch", "put", "query"):
            assert "415" in antworten, f"{methode} {pfad}: 415 fehlt"
        if op.get("x-starts-run"):
            # A run is started by a POST, or – where the run is what a
            # change to a thing means – by the PATCH that changes it.
            assert methode in ("post", "patch"), f"{pfad}: ein Lauf hinter {methode}"
            assert "409" in antworten, f"{pfad}: ein Lauf ohne 409"


def _aufgeloest(spec, schema):
    """A schema, with one `$ref` followed – deep enough for a top-level
    key check."""
    if isinstance(schema, dict) and "$ref" in schema:
        art, name = schema["$ref"].rsplit("/", 2)[-2:]
        return (spec.get("components") or {}).get(art, {}).get(name) or {}
    return schema or {}


def test_jedes_beispiel_passt_zu_seinem_schema():
    """An example is the first thing a reader copies, so a key in it that
    the schema does not have is worse than no example at all. This caught
    the facts example, which invented a `facts` array the route never
    sends."""
    spec = _spec()
    falsch = []
    for pfad, methode, op in _operationen():
        for code, antwort in (op.get("responses") or {}).items():
            for inhalt in ((antwort or {}).get("content") or {}).values():
                if not isinstance(inhalt, dict) or "example" not in inhalt:
                    continue
                assert inhalt["example"], f"{methode} {pfad} {code}: leeres Beispiel"
                schema = _aufgeloest(spec, inhalt.get("schema"))
                assert schema, f"{methode} {pfad} {code}: Beispiel ohne Schema"
                erlaubt = set(schema.get("properties") or {})
                if erlaubt and isinstance(inhalt["example"], dict):
                    dazu = sorted(set(inhalt["example"]) - erlaubt)
                    if dazu and not schema.get("additionalProperties"):
                        falsch.append((methode, pfad, code, dazu))
    assert not falsch, f"Beispiel-Schlüssel ohne Schema: {falsch}"


def test_ein_oneof_laesst_sich_entscheiden():
    """`oneOf` means exactly one branch matches. Where a discriminator says
    which property decides, every branch has to pin that property – or all
    of them accept every value and a valid answer matches several, which a
    validator rejects. The Facts schema did exactly that at 13.0."""
    def festgelegt(schema, feld):
        """Does this branch pin `feld` to a value or a small set?"""
        for teil in (schema.get("allOf") or [schema]):
            eigen = (teil.get("properties") or {}).get(feld) or {}
            if "const" in eigen or eigen.get("enum"):
                return True
        return False

    spec = _spec()
    for name, schema in (spec.get("components") or {}).get("schemas", {}).items():
        unterscheider = (schema.get("discriminator") or {}).get("propertyName")
        if not unterscheider:
            continue
        for zweig in schema.get("oneOf") or []:
            ziel = zweig["$ref"].rsplit("/", 1)[1]
            assert festgelegt(spec["components"]["schemas"][ziel], unterscheider), (
                f"{name}: {ziel} legt {unterscheider} nicht fest")


def test_kein_beispiel_widerspricht_seinem_enum():
    """An example is copied before it is read. Two of them named values
    their own schema forbids – a balance `stand` and a search `mode`."""
    spec = _spec()

    def aufloesen(s):
        if isinstance(s, dict) and "$ref" in s:
            art, name = s["$ref"].rsplit("/", 2)[-2:]
            return aufloesen(spec["components"][art][name])
        return s if isinstance(s, dict) else {}

    def felder(s):
        s = aufloesen(s)
        aus = dict(s.get("properties") or {})
        for teil in s.get("allOf") or []:
            aus.update(felder(teil))
        return aus

    def verstoesse(wert, s, wo):
        """Every value that its schema forbids – [] when it fits."""
        s = aufloesen(s)
        zweige = s.get("oneOf") or s.get("anyOf")
        if zweige:
            # Genau einer muss passen: ein Verstoss ist es nur, wenn keiner
            # passt. `type: "null"` zaehlt dabei als Zweig wie jeder andere.
            ergebnisse = [verstoesse(wert, z, wo) for z in zweige]
            return [] if any(not e for e in ergebnisse) else ergebnisse[0]
        if s.get("type") == "null":
            return [] if wert is None else [(wo, wert, "null")]
        if isinstance(wert, list):
            aus = []
            for n, x in enumerate(wert):
                aus += verstoesse(x, s.get("items") or {}, f"{wo}[{n}]")
            return aus
        if isinstance(wert, dict):
            aus, bekannt = [], felder(s)
            for k, v in wert.items():
                if bekannt.get(k):
                    aus += verstoesse(v, bekannt[k], f"{wo}/{k}")
            return aus
        erlaubt = s.get("enum")
        if erlaubt and wert not in erlaubt:
            return [(wo, wert, erlaubt)]
        if "const" in s and wert != s["const"]:
            return [(wo, wert, [s["const"]])]
        return []

    falsch = []
    for pfad, methode, op in _operationen():
        for code, antwort in (op.get("responses") or {}).items():
            for inhalt in ((antwort or {}).get("content") or {}).values():
                if isinstance(inhalt, dict) and "example" in inhalt:
                    falsch += verstoesse(inhalt["example"], inhalt.get("schema") or {},
                                         f"{methode} {pfad} {code}")
    assert not falsch, f"Beispielwerte gegen ihr Enum: {falsch}"


def test_die_beispiele_tragen_keine_echten_daten():
    """A public repository: the examples use the placeholder cast, never a
    real name, address or domain."""
    text = SPEC.read_text(encoding="utf-8")
    import re as _re
    adressen = set(_re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text))
    erlaubt = _re.compile(r"(example\.(com|org|net)|nordwind\.example)$")
    fremd = sorted(a for a in adressen if not erlaubt.search(a))
    assert not fremd, f"Adressen ausserhalb der Platzhalter: {fremd}"


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
    spec = _spec()
    assert (spec["components"]["mediaTypes"]["Problem"]["schema"]["$ref"]
            == "#/components/schemas/Error"), "Problem zeigt nicht auf Error"
    falsch = []
    for pfad, methode, code, antwort in _antworten():
        if not code.startswith(("4", "5")) or "$ref" in antwort:
            continue
        inhalt = (antwort.get("content") or {}).get("application/problem+json") or {}
        # Seit 13.0 steht die Ablehnung einmal unter components/mediaTypes;
        # jede Antwort verweist nur noch darauf.
        if inhalt.get("$ref") != "#/components/mediaTypes/Problem":
            falsch.append((pfad, methode, code))
    assert not falsch, f"Ablehnungen ohne Error-Schema: {falsch}"


def test_wer_den_index_braucht_sagt_es_mit_503():
    """x-needs-index means: without a loaded index this route answers 503
    (srv.noindex) – not an empty list with HTTP 200."""
    ohne = [(p, m) for p, m, op in _operationen()
            if op.get("x-needs-index") and "503" not in op["responses"]]
    assert not ohne, f"x-needs-index ohne 503: {ohne}"
