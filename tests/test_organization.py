"""The organization: the file org_export.py writes (organization.py), the
export against a stubbed users/delta feed, the four routes of /api/v1 and
the two MCP tools – the last two against the synthetic archive, whose
organization has an earlier version (testdata/history.py)."""

import json
import shutil
import threading
import time

import pytest

import app as app_mod
import graph_contract
import mcp_server
import org_export
import organization
import progress
import settings
import state_db
from hilfen import call
from testdata import build as testdata_build
from testdata import history, sources


def _user(uid, name, manager=None, **extra):
    return {"id": uid, "displayName": name, "jobTitle": "Role", "accountEnabled": True,
            "userType": "Member", "manager": manager, **extra}


def _fed(uid, name, manager=None, **extra):
    """The same user as users/delta answers it: the manager as manager@delta."""
    user = _user(uid, name, **extra)
    del user["manager"]
    user["manager@delta"] = [{"@odata.type": "#microsoft.graph.user", "id": manager}] if manager else []
    return user


# ---------------------------------------------------------------------------
# The file and its reading
# ---------------------------------------------------------------------------
def test_only_enabled_members_with_a_place_in_the_tree_are_kept():
    users = {u["id"]: u for u in (
        _user("top", "Carla Chef"), _user("bob", "Bob Baumeister", "top"),
        _user("gone", "Olaf Organisation", "top", accountEnabled=False),
        _user("guest", "Greta Gast", "bob", userType="Guest"),
        _user("svc", "Scanner Service"))}
    org = organization.build(users, me="bob")
    assert [p["id"] for p in org["people"]] == ["bob", "top"]
    assert org["me"] == "bob"


def test_a_disabled_manager_keeps_the_line_together():
    """Seen on a real tenant: a manager who had left was out of the file,
    and everyone below stood at the top – Teams shows them in their line.
    The disabled manager stays as a link, marked and counted as nobody."""
    users = {u["id"]: u for u in (
        _user("top", "Carla Chef"),
        _user("gone", "Adam Abwesend", "top", accountEnabled=False),
        _user("a", "Alice", "gone"), _user("b", "Bob", "a"),
        _user("idle", "Olaf Organisation", "top", accountEnabled=False))}
    built = organization.build(users)
    assert {p["id"] for p in built["people"]} == {"top", "gone", "a", "b"}, \
        "a disabled account nobody hangs on stays out"
    org = organization.Org(built)
    assert org.roots() == ["top"] and org.chain("b") == ["top", "gone", "a"]
    assert org.card("gone")["disabled"] is True and org.total["top"] == 2
    assert org.active == 3
    assert organization.changes(org, org) == []
    # Adam's account coming back is someone joining; Alice's leaving too.
    users["gone"]["accountEnabled"] = True
    users["a"]["accountEnabled"] = False
    later = organization.Org(organization.build(users))
    kinds = {(c["kind"], c["id"]) for c in organization.changes(org, later)}
    assert kinds == {("joined", "gone"), ("left", "a")}
    assert later.card("a")["disabled"] is True, "Alice now holds Bob's line"


def test_the_text_is_canonical_one_person_a_line():
    users = {u["id"]: u for u in (_user("b", "Bob", "a"), _user("a", "Carla"))}
    text = organization.text_of(organization.build(users))
    assert text == organization.text_of(organization.build(dict(reversed(users.items()))))
    lines = text.splitlines()
    assert lines[1].startswith('{"id":"a"') and lines[2].startswith('{"id":"b"')
    assert json.loads(text)["people"][1]["manager"] == "a"


def test_rules_leave_out_a_part_of_the_tree_by_its_path():
    users = {u["id"]: u for u in (
        _user("c", "Carla Chef"), _user("b", "Bob Baumeister", "c"),
        _user("a", "Alice Beispiel", "b"), _user("f", "Frida Finanz", "c"),
        _user("i", "Ines Innendienst", "f"))}
    rules = [(False, "Carla Chef/Frida Finanz/**")]
    kept = {p["id"] for p in organization.build(users, rules=rules)["people"]}
    assert kept == {"c", "b", "a"}
    rules.append((True, "Carla Chef/Frida Finanz/Ines Innendienst"))
    kept = {p["id"] for p in organization.build(users, rules=rules)["people"]}
    assert "i" not in kept, "Ines stands alone once Frida is out"


def test_the_tree_counts_everyone_below_and_survives_a_circle():
    org = organization.Org({"people": [
        {"id": "a", "name": "A", "manager": "c"}, {"id": "b", "name": "B", "manager": "a"},
        {"id": "c", "name": "C", "manager": "b"}, {"id": "d", "name": "D", "manager": "c"}]})
    top = org.roots()
    assert top == ["a"], "the circle is cut at its smallest id"
    assert org.total["a"] == 3 and org.chain("d") == ["a", "b", "c"]
    assert org.card("a") == {"id": "a", "name": "A", "title": None, "department": None,
                             "reports": 1, "total": 3}


def test_changes_name_joined_moved_changed_and_left():
    old = organization.Org({"people": [
        {"id": "c", "name": "Carla"}, {"id": "a", "name": "Alice", "manager": "c"},
        {"id": "b", "name": "Bob", "manager": "c", "title": "Engineer"},
        {"id": "o", "name": "Olaf", "manager": "c"}]})
    new = organization.Org({"people": [
        {"id": "c", "name": "Carla"}, {"id": "b", "name": "Bob", "manager": "c", "title": "Lead"},
        {"id": "a", "name": "Alice", "manager": "b"}, {"id": "k", "name": "Kai", "manager": "a"}]})
    found = {(c["kind"], c["id"]): c for c in organization.changes(old, new)}
    assert set(found) == {("joined", "k"), ("moved", "a"), ("changed", "b"), ("left", "o")}
    assert found[("moved", "a")]["fields"]["manager"] == ["Carla", "Bob"]
    assert found[("changed", "b")]["fields"] == {"title": ["Engineer", "Lead"]}
    assert organization.changes(None, new) == []


# ---------------------------------------------------------------------------
# The export against a stubbed feed
# ---------------------------------------------------------------------------
LINK1 = "https://graph.microsoft.com/v1.0/users/delta?$deltatoken=abc"
LINK2 = "https://graph.microsoft.com/v1.0/users/delta?$deltatoken=def"


class _HttpError(Exception):
    def __init__(self, status, text=""):
        super().__init__(f"HTTP {status}")
        self.response = type("R", (), {"status_code": status, "text": text})()


def _selected(url, body):
    """What Graph answers under `$select`: the named properties only – not
    even the id unless it is named (14.0.3 asked for managers without it,
    and every answer read as refused)."""
    from urllib.parse import parse_qs, urlsplit
    names = parse_qs(urlsplit(url).query).get("$select")
    if not names or not isinstance(body, dict):
        return body
    keep = set(names[0].split(","))
    return {k: v for k, v in body.items() if k in keep or k.startswith("@odata")}


class _Graph:
    """URL fragment -> answer (an exception is raised); records every call."""

    def __init__(self, answers, me="a"):
        self.answers = {"/me?": {"id": me}, **answers}
        self.calls, self.heads = [], []

    def batch_get(self, urls, extra_headers=None, parallel=1):
        """Direct questions: an answer whose fragment the URL holds, else
        404 – nobody above."""
        self.batched = getattr(self, "batched", []) + list(urls)
        for url in urls:
            graph_contract.check(url)
        out = {}
        for url in urls:
            hit = next((a for f, a in self.answers.items() if f in url and "/manager?" in f
                        or f in url and f.startswith("/users/") and "/manager" not in f
                        and "delta" not in f), None)
            out[url] = (200, _selected(url, hit)) if hit is not None \
                else (404, {"error": {"code": "Request_ResourceNotFound"}})
        return out

    def get(self, url, params=None, extra_headers=None):
        self.calls.append(url)
        graph_contract.check(url, params)
        self.heads.append(extra_headers)
        for frag, answer in self.answers.items():
            if frag in url:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f"unexpected {url}")


@pytest.fixture(autouse=True)
def _plain_env(monkeypatch):
    monkeypatch.setenv("ORG_RULES", "")
    monkeypatch.setenv("SYNC_CADENCE", "{}")
    for key in ("SYNC_NOW", "RESYNC", "FULL_SYNC"):
        monkeypatch.delenv(key, raising=False)


def _file(out):
    return json.loads((out / organization.ORG_DIR / organization.FILE).read_text(encoding="utf-8"))


def _result(capsys):
    return [r for r in (progress.lies_ergebnis(z) for z in capsys.readouterr().out.splitlines()) if r][-1]


def test_first_round_reads_everyone_then_only_changes(tmp_path, capsys):
    g = _Graph({"/users/delta?$select=": {
        "value": [_fed("a", "Alice", "c"),
                  {"id": "c", "displayName": "Carla", "accountEnabled": True,
                   "manager@delta": []}],
        "@odata.deltaLink": LINK1}})
    assert org_export.run(g, tmp_path) == (2, 2, 0)
    assert "manager" in g.calls[0] and "$top" not in g.calls[0]
    assert g.heads[0] == {"Prefer": f"odata.maxpagesize={org_export.DELTA_PAGE}"}
    assert _file(tmp_path)["me"] == "a"
    db = state_db.StateDb(tmp_path / organization.ORG_DIR)
    assert db.kv_lesen("delta") == LINK1
    # A second round names a move through manager@delta and a new person;
    # only the fields it names change.
    g2 = _Graph({"deltatoken=abc": {
        "value": [{"id": "b", "displayName": "Bob", "accountEnabled": True,
                   "manager@delta": [{"id": "c"}]},
                  {"id": "a", "manager@delta": [{"id": "c", "@removed": {"reason": "changed"}},
                                                {"id": "b"}]}],
        "@odata.deltaLink": LINK2}})
    capsys.readouterr()
    changes, people, errors = org_export.run(g2, tmp_path)
    assert (people, errors) == (3, 0) and changes == 2
    assert g2.calls[0] == LINK1
    alice = next(p for p in _file(tmp_path)["people"] if p["id"] == "a")
    assert alice == {"id": "a", "name": "Alice", "title": "Role", "manager": "b"}
    assert db.kv_lesen("delta") == LINK2
    # Nothing moved: nothing written, nothing new.
    before = (tmp_path / organization.ORG_DIR / organization.FILE).stat().st_mtime_ns
    capsys.readouterr()
    assert org_export.run(_Graph({"deltatoken=def": {"value": [], "@odata.deltaLink": LINK2}}),
                          tmp_path)[0] == 0
    assert _result(capsys)["new"] == 0
    assert (tmp_path / organization.ORG_DIR / organization.FILE).stat().st_mtime_ns == before


def test_a_removed_user_leaves_and_a_forgotten_link_reads_in_full(tmp_path, capsys):
    full = {"value": [_fed("a", "Alice", "c"), _fed("c", "Carla")], "@odata.deltaLink": LINK1}
    org_export.run(_Graph({"/users/delta?$select=": full}), tmp_path)
    org_export.run(_Graph({"deltatoken=abc": {
        "value": [{"id": "a", "@removed": {"reason": "deleted"}}], "@odata.deltaLink": LINK2}}),
        tmp_path)
    assert _file(tmp_path)["people"] == [], "Carla alone has no place in the tree"
    g = _Graph({"deltatoken=def": _HttpError(410),
                "/users/delta?$select=": full})
    capsys.readouterr()
    assert org_export.run(g, tmp_path)[1] == 2
    assert any(e["k"] == "run.org.delta_reset" for e in
               (progress.lies_event(z) for z in capsys.readouterr().out.splitlines()) if e)


def test_a_failed_round_changes_nothing(tmp_path, capsys):
    org_export.run(_Graph({"/users/delta?$select=": {
        "value": [_fed("a", "Alice", "c"), _fed("c", "Carla")], "@odata.deltaLink": LINK1}}),
        tmp_path)
    text = (tmp_path / organization.ORG_DIR / organization.FILE).read_text(encoding="utf-8")
    capsys.readouterr()
    assert org_export.run(_Graph({"deltatoken=abc": _HttpError(503)}), tmp_path) == (0, 0, 1)
    assert _result(capsys)["errors"] == 1
    db = state_db.StateDb(tmp_path / organization.ORG_DIR)
    assert db.kv_lesen("delta") == LINK1
    assert (tmp_path / organization.ORG_DIR / organization.FILE).read_text(encoding="utf-8") == text


def test_cadence_holds_and_rules_narrow_without_reading_again(tmp_path, monkeypatch):
    feed = {"/users/delta?$select=": {"value": [_fed("a", "Alice", "c"), _fed("c", "Carla"),
                                                _fed("f", "Frida", "c")],
                                      "@odata.deltaLink": LINK1}}
    org_export.run(_Graph(feed), tmp_path)
    monkeypatch.setenv("SYNC_CADENCE", json.dumps({"organization": "daily"}))
    g = _Graph({})
    assert org_export.run(g, tmp_path) == (0, 0, 0) and g.calls == []
    monkeypatch.setenv("SYNC_NOW", "1")
    monkeypatch.setenv("ORG_RULES", "- Carla/Frida")
    org_export.run(_Graph({"deltatoken=abc": {"value": [], "@odata.deltaLink": LINK1}}), tmp_path)
    assert {p["id"] for p in _file(tmp_path)["people"]} == {"a", "c"}


def test_a_full_sync_reads_the_directory_again(tmp_path, monkeypatch):
    feed = {"/users/delta?$select=": {"value": [_fed("a", "Alice", "c"), _fed("c", "Carla")],
                                      "@odata.deltaLink": LINK1}}
    org_export.run(_Graph(feed), tmp_path)
    monkeypatch.setenv("FULL_SYNC", "1")
    g = _Graph(feed)
    org_export.run(g, tmp_path)
    assert "/users/delta?$select=" in g.calls[0]


# ---------------------------------------------------------------------------
# /api/v1/organization and the MCP tools, against the synthetic archive
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return testdata_build.build(tmp_path_factory.mktemp("org-archive"), flat=True, index=False)


@pytest.fixture
def served(built, tmp_path, monkeypatch):
    home = tmp_path / "archive"
    shutil.copytree(built["home"], home)
    for name, value in (("WURZEL", home), ("HEIM", home), ("BASE", home),
                        ("STORE_PFAD", home / app_mod.STORE_DIR),
                        ("CONFIG_FILE", home / "app_config.json"),
                        ("TOKEN_FILE", home / "gx_token.txt")):
        monkeypatch.setattr(app_mod, name, value)
    settings.reset()
    a = app_mod.App(app_mod.load_config())
    httpd = app_mod.make_server(a, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], home
    httpd.shutdown()
    httpd.server_close()


def test_the_versions_and_the_top(served):
    port, _home = served
    code, r = call(port, "GET", "/api/v1/organization/versions")
    assert code == 200 and len(r["items"]) == history.ORG_TIMELINE_LEN + 1
    assert r["items"][0]["current"] and not any(v["current"] for v in r["items"][1:])
    code, top = call(port, "GET", "/api/v1/organization")
    assert code == 200 and top["me"] == sources.ORG_ME
    assert [c["id"] for c in top["roots"]] == [sources.ORG_TOP]
    assert top["people"] == len(sources.ORG_USERS) - len(sources.ORG_LEFT_OUT) - len(sources.ORG_LINKS)
    assert top["roots"][0]["total"] == top["people"] - 1


def test_one_person_with_the_chain_above_and_the_reports_below(served):
    port, _home = served
    code, r = call(port, "GET", f"/api/v1/organization/people/{sources.ORG_ME}")
    assert code == 200
    assert [c["id"] for c in r["chain"]] == [sources.ORG_TOP, "org-bob"]
    assert r["person"]["mail"] == "alice.beispiel@nordwind.example"
    assert {c["id"] for c in r["reports"]} == {"org-kai", "org-lena"}
    code, r = call(port, "GET", "/api/v1/organization/people/org-olaf")
    assert code == 404 and r["error"]["k"] == "srv.org.noperson"


def test_an_earlier_version_and_what_the_current_one_changed(served):
    port, _home = served
    _c, versions = call(port, "GET", "/api/v1/organization/versions")
    earlier = versions["items"][1]["sha256"]
    code, r = call(port, "GET", f"/api/v1/organization/people/{sources.ORG_ME}?version={earlier[:16]}")
    assert code == 200 and [c["id"] for c in r["chain"]] == [sources.ORG_TOP]
    code, r = call(port, "GET", "/api/v1/organization/changes")
    assert code == 200 and r["from"]["sha256"] == earlier
    kinds = {}
    for c in r["items"]:
        kinds.setdefault(c["kind"], []).append(c["id"])
    assert kinds == history.ORG_CHANGES
    oldest = versions["items"][-1]["sha256"]
    code, r = call(port, "GET", f"/api/v1/organization/changes?version={oldest}")
    assert code == 200 and r["from"] is None and r["items"] == []
    code, r = call(port, "GET", "/api/v1/organization?version=" + "0" * 16)
    assert code == 404 and r["error"]["k"] == "srv.org.noversion"
    code, _r = call(port, "GET", "/api/v1/organization?version=nope")
    assert code == 400


def test_no_organization_yet_is_a_404_and_no_versions(served):
    port, home = served
    shutil.rmtree(home / settings.TEAMS_DIR / organization.ORG_DIR)
    assert call(port, "GET", "/api/v1/organization/versions") == (200, {"items": []})
    code, r = call(port, "GET", "/api/v1/organization")
    assert code == 404 and r["error"]["k"] == "srv.org.none"


@pytest.fixture
def mcp_state(built):
    old = dict(mcp_server.STATE)
    mcp_server.STATE.clear()
    home = built["home"]
    mcp_server.STATE.update(teams_dir=str(home / settings.TEAMS_DIR),
                            outlook_dir=str(home / settings.OUTLOOK_DIR))
    mcp_server._ORG.clear()
    yield
    mcp_server.STATE.clear()
    mcp_server.STATE.update(old)
    mcp_server._ORG.clear()


def test_mcp_names_where_someone_stands_now_and_before(mcp_state):
    r = mcp_server.get_org_chart()
    assert r["person"]["id"] == sources.ORG_ME and r["versions_in_archive"] == history.ORG_TIMELINE_LEN + 1
    assert [c["name"] for c in r["chain"]] == ["Carla Chef", "Bob Baumeister"]
    assert r["peers"] == 1
    r = mcp_server.get_org_chart(person="nina")
    assert r["person"]["title"] == "Network Architect"
    r = mcp_server.get_org_chart(person="Hanna")
    assert len(r["matches"]) == 3 and "person" not in r
    assert r["matches"][0]["path"].startswith("Carla Chef/")
    assert "error" in mcp_server.get_org_chart(as_of="2000-01-01")


def test_mcp_lists_the_changes_between_versions(mcp_state):
    today = mcp_server._org_day(mcp_server._org_archive().versions()[0])
    r = mcp_server.org_changes(date_from=today)
    assert r["versions_compared"] == 1
    assert {(c["kind"], c["id"]) for c in r["changes"]} == \
        {(k, i) for k, ids in history.ORG_CHANGES.items() for i in ids}
    moved = mcp_server.org_changes(person="alice")["changes"]
    assert moved[0]["fields"]["manager"] == ["Carla Chef", "Bob Baumeister"]


def test_mcp_says_when_there_is_no_organization(tmp_path):
    old = dict(mcp_server.STATE)
    mcp_server.STATE.clear()
    mcp_server.STATE.update(teams_dir=str(tmp_path / "teams_export"),
                            outlook_dir=str(tmp_path / "outlook_export"))
    mcp_server._ORG.clear()
    try:
        assert "error" in mcp_server.get_org_chart()
        assert "error" in mcp_server.org_changes()
    finally:
        mcp_server.STATE.clear()
        mcp_server.STATE.update(old)
        mcp_server._ORG.clear()


# ---------------------------------------------------------------------------
# Roles, managers and teams: "whose boss", "who works for", "everyone
# with a role", "every role"
# ---------------------------------------------------------------------------
def _story():
    return organization.Org(organization.build(sources.org_users(), me=sources.ORG_ME))


def _specialists():
    return [u for u in sources.ORG_USERS.values() if u["jobTitle"] == "Specialist"]


def test_roles_count_their_holders_and_narrow():
    org = _story()
    roles = org.roles()
    assert roles[0] == {"title": "Specialist", "count": len(_specialists())}
    assert {r["title"] for r in org.roles("head")} == {"Head of Projects", "Head of Finance",
                                                       "Head of Marketing"}
    assert org.roles("head", department="finance") == [{"title": "Head of Finance", "count": 1}]
    assert [org.people[i]["name"] for i in org.with_role("head of projects", exact=True)] == \
        ["Bob Baumeister"]
    assert len(org.with_role("HEAD")) == 3


def test_below_walks_every_level_and_find_resolves_names():
    org = _story()
    below = org.below("org-bob")
    assert {i for i, lv in below if lv == 1} == {sources.ORG_ME, "org-nina"}
    assert {i for i, lv in below if lv == 2} >= {"org-kai", "org-lena"}
    assert len(below) == org.total["org-bob"]
    assert org.find("alice beispiel") == (sources.ORG_ME, None)
    pid, several = org.find("Hanna")
    assert pid is None and len(several) == 3
    assert org.find("nobody at all") == (None, [])


def test_the_role_routes(served):
    port, _home = served
    code, r = call(port, "GET", "/api/v1/organization/roles")
    assert code == 200 and r["items"][0] == {"title": "Specialist", "count": len(_specialists())}
    assert r["total"] == len(r["items"]) and r["has_more"] is False
    code, r = call(port, "GET", "/api/v1/organization/roles?contains=head&limit=2")
    assert code == 200 and len(r["items"]) == 2 and r["total"] == 3 and r["has_more"]
    assert r["people"] == 3, "everyone holding a matching title, whatever the limit cut"
    # A title held once is found by any part of it, however many titles
    # are held more often.
    code, r = call(port, "GET", "/api/v1/organization/roles?contains=desk%20le&limit=1")
    assert code == 200 and r["items"] == [{"title": "Service Desk Lead", "count": 1}]
    code, r = call(port, "GET", "/api/v1/organization/people?role=specialist&limit=10")
    assert code == 200 and r["total"] == len(_specialists()) and len(r["items"]) == 10 and r["has_more"]
    assert all(p["manager"] for p in r["items"])
    code, r = call(port, "GET", "/api/v1/organization/people?role=Head%20of%20Projects&exact=true")
    assert code == 200 and [p["id"] for p in r["items"]] == ["org-bob"]
    assert r["items"][0]["manager"] == "Carla Chef"
    code, r = call(port, "GET", "/api/v1/organization/people")
    assert code == 400 and r["error"]["v"]["name"] == "role"


def test_mcp_manager_team_role_and_roles(mcp_state):
    r = mcp_server.get_manager(person="Alice Beispiel")
    assert r["manager"]["name"] == "Bob Baumeister" and r["manager"]["mail"]
    assert [c["name"] for c in r["chain"]] == ["Carla Chef", "Bob Baumeister"]
    top = mcp_server.get_manager(person="Carla")
    assert top["manager"] is None and "top" in top["note"]
    before = mcp_server.get_manager(person="alice", as_of="2999-01-01")
    assert before["manager"]["name"] == "Bob Baumeister"
    team = mcp_server.list_reports(person="Bob")
    assert {p["name"] for p in team["reports"]} == {"Alice Beispiel", "Nina Netzwerk"}
    everyone = mcp_server.list_reports(person="Bob", all_levels=True, limit=5)
    assert everyone["total"] == team["person"]["total"] and everyone["next_offset"] == 5
    assert {p["level"] for p in everyone["reports"]} == {1, 2}
    mine = mcp_server.list_reports()
    assert mine["person"]["id"] == sources.ORG_ME
    found = mcp_server.find_by_role(role="head", department="market")
    assert [p["name"] for p in found["people"]] == ["Malte Marketing"]
    assert found["people"][0]["path"] == "Carla Chef/Malte Marketing"
    assert mcp_server.find_by_role(role="specialist")["total"] == len(_specialists())
    assert "error" in mcp_server.find_by_role(role=" ")
    roles = mcp_server.list_roles(contains="lead")
    assert {r["title"] for r in roles["roles"]} == {"Project Lead", "Service Desk Lead"}
    assert mcp_server.list_roles(limit=1)["note"]


def test_a_manager_the_feed_left_out_is_asked_for_directly(tmp_path, capsys):
    """Seen on a real tenant: users/delta named no manager for dozens of
    people Teams shows in a proper line, and they stood at the top. Who is
    left without one is asked directly; a manager nobody described comes
    along; a 404 is kept for a week before the question comes again."""
    feed = {"/users/delta?$select=": {
        "value": [_fed("c", "Carla"), _fed("b", "Bob", "c"), _fed("a", "Alice")],
        "@odata.deltaLink": LINK1},
        "/users/a/manager?": {"id": "x", "displayName": "Xaver", "accountEnabled": True,
                              "userType": "Member"},
        "/users/x?": {"id": "x", "displayName": "Xaver", "accountEnabled": True},
        "/users/x/manager?": {"id": "c"}}
    g = _Graph(feed)
    assert org_export.run(g, tmp_path)[2] == 0
    people = {p["id"]: p for p in _file(tmp_path)["people"]}
    assert people["a"]["manager"] == "x", "asked directly"
    assert people["x"]["manager"] == "c", "the manager nobody described came along, with his own"
    assert any("/users/c/manager" in u for u in g.batched), "the top is asked too"
    events = [e for e in (progress.lies_event(z) for z in capsys.readouterr().out.splitlines()) if e]
    (asked,) = [e for e in events if e["k"] == "run.org.managers"]
    assert asked["v"]["found"] == 2
    assert asked["v"]["n"] == asked["v"]["asked"], "the count that leads the line picks its singular"
    # Carla's 404 holds for a week: the next round does not ask her again.
    g2 = _Graph({"deltatoken=abc": {"value": [], "@odata.deltaLink": LINK1}})
    org_export.run(g2, tmp_path)
    assert not getattr(g2, "batched", [])
    later = time.time() + org_export.RECHECK_S + 1
    users = {"c": {**_user("c", "Carla"), "manager": None, "manager_checked": 1.0}}
    g3 = _Graph({})
    org_export.settle_managers(g3, users, now=later)
    assert g3.batched and "/users/c/manager" in g3.batched[0]


def test_the_archive_keeps_a_departed_managers_line(served):
    """Hanno reports to Petra, whose account is off: he stands in his line
    under her, not at the top, and she is marked and counted as nobody."""
    port, _home = served
    code, r = call(port, "GET", "/api/v1/organization/people/org-hanno")
    assert code == 200
    assert [c["id"] for c in r["chain"]] == [sources.ORG_TOP, "org-malte", "org-petra"]
    assert r["chain"][2]["disabled"] is True and "disabled" not in r["chain"][1]
    _c, top = call(port, "GET", "/api/v1/organization")
    assert [c["id"] for c in top["roots"]] == [sources.ORG_TOP]
    _c, roles = call(port, "GET", "/api/v1/organization/roles?contains=head%20of%20service")
    assert roles["items"] == [], "a disabled account holds no role"


# ---------------------------------------------------------------------------
# The overview: people and changes per version
# ---------------------------------------------------------------------------
def _series_store(out):
    return organization.read_series_store(state_db.StateDb(out / organization.ORG_DIR))


def test_the_export_stores_each_version_s_numbers_and_marks_a_rules_change(tmp_path, monkeypatch, capsys):
    """Each written version gets its numbers while both versions are at
    hand – the first has nothing to compare with; a version written under
    other rules is marked, since its joins and leaves come from the rules."""
    first = {"/users/delta?$select=": {
        "value": [_fed("a", "Alice", "c"), _fed("b", "Bob", "c"), _fed("c", "Carla")],
        "@odata.deltaLink": LINK1}}
    org_export.run(_Graph(first), tmp_path)
    (only,) = _series_store(tmp_path).values()
    assert only["people"] == 3 and only["counts"] is None and only["rules_changed"] is False
    # Bob moves under Alice, Dana joins.
    org_export.run(_Graph({"deltatoken=abc": {
        "value": [{"id": "b", "manager@delta": [{"id": "c", "@removed": {"reason": "changed"}},
                                                {"id": "a"}]},
                  _fed("d", "Dana", "c")],
        "@odata.deltaLink": LINK2}}), tmp_path)
    import hashlib
    store = _series_store(tmp_path)
    path = tmp_path / organization.ORG_DIR / organization.FILE
    second = store[hashlib.sha256(path.read_bytes()).hexdigest()]
    assert len(store) == 2
    assert second["counts"] == {"joined": 1, "moved": 1, "changed": 0, "left": 0}
    assert second["people"] == 4 and second["rules_changed"] is False
    # A rule leaves Dana out: written as a version, marked as the rules'.
    monkeypatch.setenv("ORG_RULES", "- **/Dana")
    org_export.run(_Graph({"deltatoken=def": {"value": [], "@odata.deltaLink": LINK2}}), tmp_path)
    third = [e for e in _series_store(tmp_path).values() if e["rules_changed"]]
    assert len(third) == 1 and third[0]["counts"]["left"] == 1


def test_a_version_after_an_unreadable_file_keeps_its_rules_change_until_counted(tmp_path, monkeypatch):
    """The file before could not be read: the version waits for the
    one-time job – but whether its rules changed is known only now, while
    the kv `rules` still holds the old checksum. Kept as `pending`, and the
    job counts it with that mark."""
    first = {"/users/delta?$select=": {
        "value": [_fed("a", "Alice", "c"), _fed("b", "Bob", "c"), _fed("c", "Carla")],
        "@odata.deltaLink": LINK1}}
    org_export.run(_Graph(first), tmp_path)
    path = tmp_path / organization.ORG_DIR / organization.FILE
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("ORG_RULES", "- **/Bob")
    org_export.run(_Graph({"deltatoken=abc": {"value": [], "@odata.deltaLink": LINK2}}), tmp_path)
    import hashlib
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    db = state_db.StateDb(tmp_path / organization.ORG_DIR)
    entry = organization.read_series_store(db)[sha]
    assert entry["pending"] is True and entry["rules_changed"] is True
    # The overview says it is still to be counted, never "no change".
    monkeypatch.setattr(organization, "versions_of", lambda data, teams: [
        {"sha256": sha, "captured": "2026-06-09T06:00:00Z", "current": True}])
    assert organization.series(tmp_path, tmp_path, db)["missing"] == 1
    monkeypatch.setattr(organization.Archive, "readable", lambda self, v: organization.Org(path.read_bytes()))
    monkeypatch.setattr(organization.Archive, "previous", lambda self, found, i: (None, None))
    assert organization.fill_series(tmp_path, tmp_path, db) == 1
    done = organization.read_series_store(db)[sha]
    assert "pending" not in done and done["rules_changed"] is True and done["figures"]["people"] == 2
    assert organization.series(tmp_path, tmp_path, db)["items"][0]["rules_changed"] is True


def test_the_one_time_job_counts_the_versions_from_before_the_overview(served):
    """An archive from before the overview has versions but no numbers:
    the next organization run computes them once, oldest first, each
    against the one before – and then finds nothing left to do."""
    port, home = served
    teams = home / settings.TEAMS_DIR
    db = state_db.StateDb(teams / organization.ORG_DIR)
    db.kv_loeschen(organization.SERIES_KEY)
    code, r = call(port, "GET", "/api/v1/organization/changes/series")
    versions_held = history.ORG_TIMELINE_LEN + 1
    assert code == 200 and r == {"items": [], "missing": versions_held}
    assert organization.fill_series(home, teams, db) == versions_held
    assert organization.fill_series(home, teams, db) == 0
    code, r = call(port, "GET", "/api/v1/organization/changes/series")
    assert code == 200 and r["missing"] == 0
    first, current = r["items"][0], r["items"][-1]
    assert len(r["items"]) == versions_held
    assert first["joined"] is None and first["people"] and not first["current"]
    assert current["current"] and current["rules_changed"] is False
    assert {k: current[k] for k in organization.KINDS} == {
        k: len(history.ORG_CHANGES.get(k, [])) for k in organization.KINDS}
    before = r["items"][-2]
    assert current["people"] == before["people"] + current["joined"] - current["left"]
    # The history's weeks: every step names at least one change.
    assert all(sum(it[k] for k in organization.KINDS) for it in r["items"][1:])
    # Numbers only: no name, no id of a person in the answer.
    raw = json.dumps(r)
    assert sources.ORG_ME not in raw and "org-" not in raw


def test_the_series_route_answers_an_archive_without_an_organization(served):
    port, home = served
    shutil.rmtree(home / settings.TEAMS_DIR / organization.ORG_DIR)
    code, r = call(port, "GET", "/api/v1/organization/changes/series")
    assert code == 200 and r == {"items": [], "missing": 0}


def test_the_export_runs_the_one_time_job_whatever_its_cadence(tmp_path, monkeypatch, capsys):
    """Versions from before the overview are counted on the next run –
    also one the cadence lets pass – said once in the log."""
    calls = []
    monkeypatch.setattr(org_export.versions, "data_root", lambda: tmp_path)
    monkeypatch.setattr(organization, "fill_series", lambda data, out, db: calls.append(data) or 2)
    monkeypatch.setattr(org_export.export_util, "einheit_faellig", lambda db, cadence: False)
    org_export.run(_Graph({}), tmp_path)
    assert calls == [tmp_path]
    events = [e for e in (progress.lies_event(z) for z in capsys.readouterr().out.splitlines()) if e]
    assert [e["v"] for e in events if e["k"] == "run.org.series"] == [{"n": 2}]


def test_an_unreadable_version_is_said_once_and_the_next_compares_past_it(served, monkeypatch):
    """A version whose bytes are gone or no JSON: recorded as unreadable –
    not "missing" for ever –, and the version after it is compared with
    the nearest readable one before, never taken for the first."""
    port, home = served
    teams = home / settings.TEAMS_DIR
    db = state_db.StateDb(teams / organization.ORG_DIR)
    db.kv_loeschen(organization.SERIES_KEY)
    found = organization.versions_of(home, teams)
    broken = found[5]["sha256"]
    real = organization.Archive.org

    def org(self, version):
        if version["sha256"] == broken:
            raise ValueError("not JSON")
        return real(self, version)
    monkeypatch.setattr(organization.Archive, "org", org)
    assert organization.fill_series(home, teams, db) == len(found)
    code, r = call(port, "GET", "/api/v1/organization/changes/series")
    assert code == 200 and r["missing"] == 0
    by_version = {it["version"]: it for it in r["items"]}
    assert by_version[broken[:16]]["people"] is None and by_version[broken[:16]]["joined"] is None
    after = by_version[found[4]["sha256"][:16]]
    assert after["joined"] is not None and sum(after[k] for k in organization.KINDS) > 0


def test_a_version_that_came_in_another_way_is_counted_too(served):
    """No one-shot flag: whatever version has no numbers – one restored or
    changed outside the app – is counted on the next run; with nothing to
    count, nothing is read."""
    port, home = served
    teams = home / settings.TEAMS_DIR
    db = state_db.StateDb(teams / organization.ORG_DIR)
    store = organization.read_series_store(db)
    current = organization.versions_of(home, teams)[0]["sha256"]
    del store[current]
    organization.write_series_store(db, store)
    assert call(port, "GET", "/api/v1/organization/changes/series")[1]["missing"] == 1
    assert organization.fill_series(home, teams, db) == 1
    assert organization.fill_series(home, teams, db) == 0
    assert call(port, "GET", "/api/v1/organization/changes/series")[1]["missing"] == 0


def test_an_odd_old_version_is_unreadable_not_a_stop(served, monkeypatch):
    """Any error reading one version – not only bad JSON – marks that
    version and lets the job go on; what was counted is kept."""
    port, home = served
    teams = home / settings.TEAMS_DIR
    db = state_db.StateDb(teams / organization.ORG_DIR)
    db.kv_loeschen(organization.SERIES_KEY)
    found = organization.versions_of(home, teams)
    real = organization.Archive.org
    monkeypatch.setattr(organization.Archive, "org", lambda self, v: (
        (_ for _ in ()).throw(KeyError("people")) if v["sha256"] == found[3]["sha256"] else real(self, v)))
    assert organization.fill_series(home, teams, db) == len(found)
    r = call(port, "GET", "/api/v1/organization/changes/series")[1]
    odd = next(it for it in r["items"] if it["version"] == found[3]["sha256"][:16])
    assert odd["unreadable"] is True and odd["people"] is None
    # The changes a later version shows compare with the same one as its numbers.
    later = found[2]["sha256"]
    code, ch = call(port, "GET", f"/api/v1/organization/changes?version={later[:16]}")
    assert code == 200 and ch["from"]["sha256"] == found[4]["sha256"]
    numbers = next(it for it in r["items"] if it["version"] == later[:16])
    assert len(ch["items"]) == sum(numbers[k] for k in organization.KINDS)


def test_earlier_versions_none_can_read_say_no_base_not_zero(served, monkeypatch):
    port, home = served
    teams = home / settings.TEAMS_DIR
    db = state_db.StateDb(teams / organization.ORG_DIR)
    db.kv_loeschen(organization.SERIES_KEY)
    found = organization.versions_of(home, teams)
    keep = found[0]["sha256"]
    real = organization.Archive.org
    monkeypatch.setattr(organization.Archive, "org", lambda self, v: real(self, v) if v["sha256"] == keep else None)
    organization.fill_series(home, teams, db)
    r = call(port, "GET", "/api/v1/organization/changes/series")[1]
    current = r["items"][-1]
    assert current["no_base"] is True and current["joined"] is None and current["people"]


# ---------------------------------------------------------------------------
# Seniority: titles read by the role model, health read from the tree
# ---------------------------------------------------------------------------
import seniority  # noqa: E402


@pytest.mark.parametrize("raw,clean", [
    ("Data Analyst III - NL", "Data Analyst III"),
    ("Support Engineer II – AT", "Support Engineer II"),
    ("Office Coordinator-CH", "Office Coordinator"),
    ("Solution Designer IV - FR (Interim)", "Solution Designer IV"),
    ("Analyst  -  Quality IV", "Analyst - Quality IV"),
    ("Accountant - IV", "Accountant - IV"),          # a grade is no country code
    ("Co-CEO", "Co-CEO"),                            # nor is a rule's own word
    ("Co-CTO - SE", "Co-CTO"),
])
def test_a_title_is_cleaned_to_its_group(raw, clean):
    assert seniority.Model().normalize(raw) == clean


@pytest.mark.parametrize("title,leads,level,source", [
    ("Senior Director Sales", True, 2, "rule"),        # more words win: Senior Director
    ("Lead Architect", False, 6, "rule"),              # before Lead
    ("Senior Engineer", False, 7, "rule"),             # as many words: the earlier wins
    ("Junior Consultant", False, 9, "rule"),
    ("Account Manager IV - NL", False, 6, "rule"),     # a professional, grade IV
    ("Data Analyst I - AT", False, 9, "rule"),
    ("Teamleiter (m/w/d)", True, 5, "rule"),
    ("Teamleiter (m/w/d)", False, 8, "tree"),          # leads nobody: a professional
    ("Head of IT", False, 8, "tree"),
    ("Managing Director", False, 1, "rule"),           # no reports needed at the top
    ("Contractor - Marketing - FR", False, None, "outside"),
    ("Shared Mailbox", False, None, "outside"),
    ("Quantum Whisperer", False, None, "unassigned"),
    # The earliest rule wins, more words only from the same start.
    ("Senior Project Manager", False, 7, "rule"),
    ("Senior Account Manager", False, 7, "rule"),
    ("Senior Executive Assistant", False, 7, "rule"),
    ("Senior Consultant", False, 7, "rule"),
    ("Junior Project Manager", False, 9, "rule"),
    ("Executive Vice President", True, 1, "rule"),
    ("Senior Vice President", True, 2, "rule"),
    ("Clean Room Technician", False, 8, "rule"),       # "Room" alone names no room
    ("Meeting Room 4.12", False, None, "outside"),
    ("", False, None, "untitled"),
])
def test_a_title_takes_its_level_by_the_rules_and_the_tree(title, leads, level, source):
    assert seniority.Model().place(title, leads) == (level, source)


def test_a_level_set_by_hand_wins_and_the_model_is_cleaned():
    m = seniority.Model(None, {"quantum whisperer": 6, "data analyst iii": "outside", "x": 99})
    assert m.place("Quantum Whisperer - DE", False) == (6, "hand")
    assert m.place("Data Analyst III - NL", False) == (None, "outside")
    assert "x" not in m.overrides
    # A level set by hand wins over the outside words too.
    assert seniority.Model(None, {"shared mailbox": 8}).place("Shared Mailbox", False) == (8, "hand")
    model = seniority.clean_model({"levels": [{"name": "only one"}], "grade_level": True,
                                   "needs_reports": [4, "x", 0, 11], "preset": "nonsense"})
    assert len(model["levels"]) == seniority.LEVELS and model["grade_level"] == 8
    assert model["needs_reports"] == [4] and model["preset"] == "corporate"
    for name in seniority.PRESETS:
        assert len(seniority.preset_model(name)["levels"]) == seniority.LEVELS
    for bad in ([], {}, 3):                              # never a 500 from the config route
        assert seniority.clean_model({"preset": bad})["preset"] == "corporate"


@pytest.mark.parametrize("preset,title,leads,level,source", [
    # Each preset brings its own ladder: a consulting title is a career
    # step and needs no reports, a public-service officer is no leader.
    ("consulting", "Manager", False, 4, "rule"),
    ("consulting", "Consultant II", False, 6, "rule"),
    ("public", "Oberrat", False, 5, "rule"),
    ("public", "Referatsleiter", False, 6, "tree"),
    ("public", "Referatsleiter", True, 3, "rule"),
    ("startup", "Head of Growth", False, 8, "tree"),
    ("startup", "Engineer IV", False, 6, "rule"),             # the corporate grades
    ("corporate", "Manager", False, 8, "tree"),
])
def test_each_preset_places_by_its_own_ladder(preset, title, leads, level, source):
    assert seniority.Model({"preset": preset}).place(title, leads) == (level, source)


def test_the_pyramid_and_the_figures_of_the_synthetic_organization(built):
    archive = organization.Archive(built["home"], built["home"] / settings.TEAMS_DIR)
    org = archive.org(archive.versions()[0])
    a = seniority.analyse(org)
    assert a["outside"] == 2 and a["untitled"] == 1 and a["unassigned"] >= 1
    assert sum(x["count"] for x in a["levels"]) + a["outside"] + a["untitled"] + a["unassigned"] == org.active
    assert sum(a["tree"]) == org.active
    assert a["levels"][0]["count"] == 2                  # the managing director, the Co-CEO
    assert a["levels"][7]["tree"] >= 1                   # a "Teamleiter" leading nobody
    f = a["figures"]
    reports = seniority.direct_reports(org)
    assert f["people"] == org.active and f["leaders"] == sum(1 for v in reports.values() if v)
    assert f["span_max"] == max(reports.values()) and f["layers"] >= 4
    part = seniority.figures(org, "org-frida")
    assert part["people"] == 1 + len(org.below("org-frida")) and part["leaders"] >= 1
    t = seniority.titles(org)
    odd = next(x for x in t["items"] if x["title"] == "Quantum Whisperer")
    level = {x["title"]: x["level"] for x in t["items"]}
    assert level["Senior Project Manager"] == 7 and level["Clean Room Technician"] == 8
    assert level["Co-CEO"] == 1
    assert odd["source"] == "unassigned" and odd["people"] == 1
    assert any(w["word"] == "quantum" for w in t["words"])


def test_the_seniority_routes_answer_numbers_and_the_dialog_its_titles(served):
    port, _home = served
    code, r = call(port, "GET", "/api/v1/organization/seniority")
    assert code == 200 and len(r["levels"]) == seniority.LEVELS and r["outside"] == 2
    assert r["since"] and r["since_figures"]["people"] < r["figures"]["people"]
    raw = json.dumps(r)
    assert "Quantum" not in raw and "Account Manager" not in raw     # numbers only
    code, t = call(port, "GET", "/api/v1/organization/seniority/titles")
    assert code == 200 and any(x["title"] == "Quantum Whisperer" for x in t["items"])
    assert set(t["presets"]) == set(seniority.PRESETS) and t["model"]["preset"] == "corporate"
    # One level's people, by title and by tree.
    code, p = call(port, "GET", "/api/v1/organization/people?level=1")
    assert code == 200 and [c["id"] for c in p["items"]] == [sources.ORG_TOP, "org-x-coceo"]
    code, p = call(port, "GET", "/api/v1/organization/people?depth=0")
    assert code == 200 and p["total"] == r["tree"][0]
    # A level set by hand, through the one door: the pyramid follows.
    code, c = call(port, "PATCH", "/api/v1/config", {"role_levels": {"quantum whisperer": 6, "bad": 0}})
    assert code == 200 and c["config"]["role_levels"] == {"quantum whisperer": 6}
    code, r2 = call(port, "GET", "/api/v1/organization/seniority")
    assert r2["levels"][5]["hand"] == 1 and r2["unassigned"] == r["unassigned"] - 1
    code, c = call(port, "PATCH", "/api/v1/config", {"role_model": {"levels": "nonsense"}})
    assert code == 200 and len(c["config"]["role_model"]["levels"]) == seniority.LEVELS


def test_mcp_names_the_organization_s_health_in_numbers(mcp_state, monkeypatch):
    r = mcp_server.org_health()
    assert len(r["pyramid"]) == seniority.LEVELS and r["beside"]["outside"] == 2
    assert r["figures"]["leaders"] and r["widest_spans"][0]["reports"] >= r["widest_spans"][-1]["reports"]
    assert len(r["history"]) == history.ORG_TIMELINE_LEN + 1
    assert r["history"][0]["people"] < r["history"][-1]["people"]
    raw = json.dumps(r)
    assert "Quantum" not in raw and "Account Manager" not in raw    # never job titles
    # The history comes from the figures each series entry keeps: no
    # version is parsed for it.
    mcp_server._ORG_FIGURES.clear()
    monkeypatch.setattr(organization.Archive, "readable", lambda self, v: pytest.fail("parsed " + v["sha256"]))
    assert mcp_server.org_health()["history"] == r["history"]
    monkeypatch.undo()
    part = mcp_server.org_health(below="Frida")
    assert part["part"]["name"] == "Frida Finanz" and "pyramid" not in part and "history" not in part
    assert set(part["part"]) == {"name", "path"}                    # no title, no department
    assert "error" in mcp_server.org_health(as_of="2000-01-01")


# ---------------------------------------------------------------------------
# The organization as CSV
# ---------------------------------------------------------------------------
import csv  # noqa: E402
import http.client  # noqa: E402
import io  # noqa: E402

import org_csv  # noqa: E402


def _fetch(port, path, accept=None):
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    con.request("GET", path, headers={"Accept": accept} if accept else {})
    r = con.getresponse()
    raw, head = r.read(), dict(r.getheaders())
    con.close()
    return r.status, head, raw


def _rows(raw, delimiter=","):
    text = raw.decode("utf-8")
    return list(csv.reader(io.StringIO(text.removeprefix("﻿")), delimiter=delimiter))


@pytest.mark.parametrize("value,cell", [
    ("=HYPERLINK(1)", "'=HYPERLINK(1)"), ("+49 40", "'+49 40"), ("-Lead", "'-Lead"),
    ("@sum", "'@sum"), ("Head of IT", "Head of IT"), (3, 3), (None, ""), (True, "true"),
])
def test_a_cell_never_becomes_a_formula(value, cell):
    assert org_csv.defuse(value) == cell


def test_the_people_of_a_version_as_a_file(served):
    port, _home = served
    code, head, raw = _fetch(port, "/api/v1/organization/export/people?dialect=excel")
    assert code == 200 and head["Content-Type"].startswith("text/csv")
    assert raw.startswith("﻿".encode()) and b"\r\n" in raw
    name = head["Content-Disposition"]
    assert name.startswith('attachment; filename="organization-') and name.endswith('-people.csv"')
    rows = _rows(raw, ";")
    header, body = rows[0], rows[1:]
    _c, top = call(port, "GET", "/api/v1/organization")
    assert len(body) == top["people"], "everyone active, nobody disabled"
    assert body[0][0] == sources.ORG_TOP, "top down"
    assert "mail" not in header and "disabled" not in header, "mail and disabled off by default"
    assert {"office", "level", "path", "manager_id"} <= set(header)
    # The company – the legal entity of a group – in every file.
    companies = {u["companyName"] for u in sources.ORG_USERS.values() if u.get("companyName")}
    assert header[4] == "company" and companies & {r[4] for r in body}
    row = dict(zip(header, next(r for r in body if r[0] == sources.ORG_ME), strict=True))
    assert row["manager_id"] and row["path"].endswith(sources.ORG_USERS[sources.ORG_ME]["displayName"])


def test_all_groups_are_everything_held_of_a_person(served):
    port, _home = served
    code, _h, raw = _fetch(port, "/api/v1/organization/export/people?columns="
                                 + ",".join(org_csv.GROUPS))
    rows = _rows(raw)
    header = rows[0]
    held = {f for _g, f in organization.FIELDS} | {"id", "manager_id", "disabled"}
    assert code == 200 and held <= set(header)
    disabled = [r for r in rows[1:] if r[header.index("disabled")] == "true"]
    assert {r[0] for r in disabled} >= set(sources.ORG_LINKS), "switched-off accounts as rows"
    assert any("@" in r[header.index("mail")] for r in rows[1:])
    # The base alone, and an unknown group refused.
    _c, _h, raw = _fetch(port, "/api/v1/organization/export/people?columns=none")
    assert _rows(raw)[0] == ["id", "name", "title", "department", "company", "manager_id",
                             "manager_name", "direct_reports", "total_below"]
    assert _fetch(port, "/api/v1/organization/export/people?columns=salary")[0] == 400
    assert _fetch(port, "/api/v1/organization/export/people?dialect=tsv")[0] == 400
    assert _fetch(port, "/api/v1/organization/export/salaries")[0] == 404


def test_a_part_a_list_and_the_same_list_by_content_negotiation(served):
    port, _home = served
    _c, frida = call(port, "GET", "/api/v1/organization/people/org-frida")
    _c, _h, raw = _fetch(port, "/api/v1/organization/export/people?below=org-frida")
    body = _rows(raw)[1:]
    assert body[0][0] == "org-frida" and len(body) == 1 + frida["person"]["total"]
    assert _fetch(port, "/api/v1/organization/export/people?below=nobody")[0] == 404
    _c, level = call(port, "GET", "/api/v1/organization/people?level=8&limit=1000")
    _c, _h, raw = _fetch(port, "/api/v1/organization/export/people?level=8")
    assert [r[0] for r in _rows(raw)[1:]] == [x["id"] for x in level["items"]]
    # The role list the view shows, asked of its own route as a file.
    code, head, raw = _fetch(port, "/api/v1/organization/people?role=specialist&dialect=excel",
                             accept="text/csv")
    _c, role = call(port, "GET", "/api/v1/organization/people?role=specialist&limit=1000")
    assert code == 200 and head["Content-Type"].startswith("text/csv")
    assert [r[0] for r in _rows(raw, ";")[1:]] == [x["id"] for x in role["items"]]


def test_the_changes_over_a_span_as_a_file(served):
    port, home = served
    teams = home / settings.TEAMS_DIR
    found = organization.versions_of(home, teams)
    # The last version alone: the changes fold's own changes, field by field.
    code, head, raw = _fetch(port, "/api/v1/organization/export/changes")
    rows = _rows(raw)
    assert code == 200 and rows[0] == org_csv.CHANGE_HEADER
    assert head["Content-Disposition"].endswith('-changes.csv"')
    _c, fold = call(port, "GET", "/api/v1/organization/changes")
    fields = sum(len(c.get("fields") or {"title": 1}) for c in fold["items"])
    assert len(rows) - 1 == fields
    assert {r[3] for r in rows[1:]} == set(history.ORG_CHANGES)
    # From the oldest: every version after it, oldest first.
    _c, _h, raw = _fetch(port, f"/api/v1/organization/export/changes?from={found[-1]['sha256'][:16]}")
    span = _rows(raw)[1:]
    assert len(span) > len(rows) - 1 and span[0][0] <= span[-1][0]
    assert _fetch(port, f"/api/v1/organization/export/changes?from={found[0]['sha256'][:16]}")[0] == 400
    # A part: only the people below someone.
    _c, _h, raw = _fetch(port, f"/api/v1/organization/export/changes?from={found[-1]['sha256'][:16]}"
                               "&below=org-frida")
    part = _rows(raw)[1:]
    assert len(part) < len(span)


def test_csv_counts_and_reaches_everyone_even_in_a_circle():
    """A manager circle is cut at its smallest id, which then stands at the
    top: the whole-organization file reaches everyone. Both report counts
    are of active people."""
    org = organization.Org({"people": [
        {"id": "a", "name": "A", "manager": "b"}, {"id": "b", "name": "B", "manager": "a"},
        {"id": "c", "name": "C", "manager": "b"}, {"id": "d", "name": "D", "manager": "b", "disabled": True}]})
    assert sorted(org_csv.everyone(org)) == ["a", "b", "c", "d"]
    header, rows = org_csv.people(org, org_csv.everyone(org), groups=())
    b = dict(zip(header, next(r for r in rows if r[0] == "b"), strict=True))
    assert b["direct_reports"] == 1 and b["total_below"] == 1 and len(rows) == 3


def test_csv_reports_as_the_line_stands_through_a_switched_off_manager():
    """Carla leads a switched-off manager with two active reports: the
    line hands them up, so her row counts two, her leadership title holds,
    and the switched-off manager's own row keeps the two below them."""
    org = organization.Org({"people": [
        {"id": "carla", "name": "Carla Chef", "title": "Head of Sales"},
        {"id": "dana", "name": "Dana", "title": "Head of Field", "manager": "carla", "disabled": True},
        {"id": "e1", "name": "E1", "title": "Engineer", "manager": "dana"},
        {"id": "e2", "name": "E2", "title": "Engineer", "manager": "dana"}]})
    header, rows = org_csv.people(org, org_csv.everyone(org), groups=("seniority", "disabled"))
    row = {r[0]: dict(zip(header, r, strict=True)) for r in rows}
    assert row["carla"]["direct_reports"] == 2 and row["carla"]["total_below"] == 2
    assert row["carla"]["level_source"] == "rule"
    assert row["dana"]["direct_reports"] == 2 and row["dana"]["level_source"] == "rule"


def test_csv_by_content_negotiation_says_vary_and_honours_weights(served):
    port, _home = served
    path = "/api/v1/organization/people?role=specialist"
    for accept, csv_wanted in (("text/csv", True), ("*/*", False), (None, False),
                               ("application/json, text/csv;q=0", False),
                               ("text/csv;q=0.9, application/json;q=0.5", True),
                               ("application/json;q=0.5, text/csv;q=0.4", False),
                               ("text/csv, */*", True), ("text/*", True),
                               ("text/csv, application/json", False)):
        code, head, _raw = _fetch(port, path, accept=accept)
        assert code == 200 and head.get("Vary") == "Accept", accept
        assert head["Content-Type"].startswith("text/csv" if csv_wanted else "application/json"), accept


def test_the_changes_never_reach_before_the_span(served, monkeypatch):
    """`from` is the base: an unreadable one gives way to the first
    readable version after it, never to one before; without `from` the
    file holds the shown version's changes and is named after it."""
    port, home = served
    found = organization.versions_of(home, home / settings.TEAMS_DIR)
    code, head, _raw = _fetch(port, "/api/v1/organization/export/changes")
    day = str(found[0]["captured"])[:10]
    assert code == 200 and head["Content-Disposition"].endswith(f'organization-{day}-{day}-changes.csv"')
    broken = found[4]["sha256"]
    real = organization.Archive.org

    def org(self, version):
        if version["sha256"] == broken:
            raise ValueError("not JSON")
        return real(self, version)
    monkeypatch.setattr(organization.Archive, "org", org)
    _c, _h, raw = _fetch(port, f"/api/v1/organization/export/changes?from={broken[:16]}")
    versions_in = {r[1] for r in _rows(raw)[1:]}
    assert found[3]["sha256"][:16] not in versions_in, "the first readable after `from` is the base"
    assert versions_in <= {f["sha256"][:16] for f in found[:3]}


def test_the_export_checks_the_question_before_it_reads_a_version(served, monkeypatch):
    port, _home = served
    monkeypatch.setattr(organization.Archive, "readable",
                        lambda self, v: pytest.fail("read before the question was checked"))
    monkeypatch.setattr(organization.Archive, "org",
                        lambda self, v: pytest.fail("read before the question was checked"))
    for path in ("/api/v1/organization/export/changes?dialect=xlsx",
                 "/api/v1/organization/export/people?columns=salary",
                 "/api/v1/organization/export/changes?from=nothex"):
        assert _fetch(port, path)[0] == 400, path


def test_a_part_whose_head_left_still_has_its_changes(served):
    """Someone who left before the shown version: their part's changes in
    the span are found; a person in no version is refused."""
    port, home = served
    found = organization.versions_of(home, home / settings.TEAMS_DIR)
    gone = history.ORG_CHANGES["left"][0]
    code, _h, raw = _fetch(port, f"/api/v1/organization/export/changes?from={found[-1]['sha256'][:16]}"
                                 f"&below={gone}")
    assert code == 200 and any(r[4] == gone and r[3] == "left" for r in _rows(raw)[1:])
    assert _fetch(port, "/api/v1/organization/export/changes?below=nobody")[0] == 404


def test_a_list_file_keeps_switched_off_accounts_when_asked(served):
    port, _home = served
    disabled = next(u for u in sources.ORG_USERS.values()
                    if u["id"] in sources.ORG_LINKS and u.get("jobTitle"))
    role = disabled["jobTitle"]
    path = f"/api/v1/organization/export/people?role={role.replace(' ', '%20')}&exact=true&columns="
    _c, _h, raw = _fetch(port, path + "none")
    assert disabled["id"] not in {r[0] for r in _rows(raw)[1:]}
    _c, _h, raw = _fetch(port, path + "disabled")
    rows = _rows(raw)
    assert [r[-1] for r in rows[1:] if r[0] == disabled["id"]] == ["true"]


def test_an_unknown_part_is_refused_before_the_span_is_walked(served, monkeypatch):
    port, home = served
    found = organization.versions_of(home, home / settings.TEAMS_DIR)
    reads = []
    real = organization.Archive.readable
    monkeypatch.setattr(organization.Archive, "readable", lambda self, v: reads.append(v) or real(self, v))
    code, _h, _raw = _fetch(port, f"/api/v1/organization/export/changes?from={found[-1]['sha256'][:16]}"
                                  "&below=nobody")
    assert code == 404 and len(reads) <= 1, "only the base was read"
