"""The Graph contract (tests/graph_contract.py) and the code held against
it: every request the exports spell out in the source – an endpoint with
its options in the URL or in a literal params dict – must name a known
endpoint and only options that endpoint takes. The fakes of the export
tests hold the requests they see at run time against the same table."""

import ast
from pathlib import Path

import pytest

import graph_contract

WURZEL = Path(__file__).resolve().parents[1]
CALLS = {"get", "paged", "get_bytes", "stream"}


def _template(node):
    """A URL argument as text, every interpolation but GRAPH as `{}`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                out.append(v.value)
            else:
                out.append(graph_contract.GRAPH_ROOT if ast.unparse(v.value) == "GRAPH" else "x")
        return "".join(out)
    return None


def _requests_in_source():
    """(where, url, params keys) of every Graph request spelled out in the
    app's modules."""
    found = []
    for path in sorted(WURZEL.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr in CALLS and n.args):
                continue
            url = _template(n.args[0])
            if not url or not url.startswith(graph_contract.GRAPH_ROOT):
                continue
            p = n.args[1] if len(n.args) > 1 else next(
                (k.value for k in n.keywords if k.arg == "params"), None)
            keys = ({k.value for k in p.keys if isinstance(k, ast.Constant)}
                    if isinstance(p, ast.Dict) else set())
            found.append((f"{path.name}:{n.lineno}", url, keys))
    return found


def test_the_source_spells_out_requests_to_check():
    found = _requests_in_source()
    assert len(found) > 30, "the scan no longer finds the requests"
    assert any("/me/chats/x/members" in url for _w, url, _k in found)


@pytest.mark.parametrize("where, url, keys", _requests_in_source(),
                         ids=lambda v: v if isinstance(v, str) and ".py:" in v else "")
def test_every_request_in_the_source_keeps_to_the_contract(where, url, keys):
    graph_contract.check(url, {k: "" for k in keys})


def test_the_contract_refuses_what_graph_refuses():
    """The three options that reached Graph and came back as a 400."""
    for url, params in ((f"{graph_contract.GRAPH_ROOT}/me/chats/c1/members", {"$top": 50}),
                        (f"{graph_contract.GRAPH_ROOT}/me/joinedTeams", {"$top": 50}),
                        (f"{graph_contract.GRAPH_ROOT}/teams/t1/channels?$top=50", None)):
        with pytest.raises(graph_contract.GraphRefusal) as e:
            graph_contract.check(url, params)
        assert e.value.response.status_code == 400
    with pytest.raises(AssertionError, match="not in tests/graph_contract.py"):
        graph_contract.check(f"{graph_contract.GRAPH_ROOT}/me/somethingNew")
    graph_contract.check(f"{graph_contract.GRAPH_ROOT}/users/delta?$deltatoken=abc")
    assert graph_contract.endpoint(f"{graph_contract.GRAPH_ROOT}/users/delta") == "/users/delta"
