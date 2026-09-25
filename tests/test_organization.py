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
            out[url] = (200, hit) if hit is not None else (404, {"error": {"code": "Request_ResourceNotFound"}})
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
    assert code == 200 and len(r["items"]) == 2
    assert r["items"][0]["current"] and not r["items"][1]["current"]
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
    code, r = call(port, "GET", f"/api/v1/organization/changes?version={earlier}")
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
    assert r["person"]["id"] == sources.ORG_ME and r["versions_in_archive"] == 2
    assert [c["name"] for c in r["chain"]] == ["Carla Chef", "Bob Baumeister"]
    assert r["peers"] == 1
    r = mcp_server.get_org_chart(person="nina")
    assert r["person"]["title"] == "Network Architect"
    r = mcp_server.get_org_chart(person="Hanna")
    assert len(r["matches"]) == 3 and "person" not in r
    assert r["matches"][0]["path"].startswith("Carla Chef/")
    assert "error" in mcp_server.get_org_chart(as_of="2000-01-01")


def test_mcp_lists_the_changes_between_versions(mcp_state):
    r = mcp_server.org_changes()
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
    assert any(e["k"] == "run.org.managers" and e["v"]["found"] == 2 for e in events)
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
