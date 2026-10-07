"""
The skills in skills/ against the server they describe – and the two ways
the server hands them out.

A skill is prose the server cannot check, so nothing else notices when a
tool is renamed, a parameter goes or a prompt gains a step. These tests
do: every tool, parameter and verdict a skill names in backticks must
exist; every tool a prompt names must be in the skill that follows it;
and every tool must be in some skill.

The server serves the skills per SEP-2640 (skill:// resources, skills/list,
skills/get) and through get_guide, whose token the gated tools ask for.
Both are spoken here over real MCP, as a client would.
"""

import ast
import hashlib
import inspect
import json
import re
import shutil
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.client.client import Client
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, Request
from pydantic import TypeAdapter

import mcp_server

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills"

# Which prompts each skill follows step by step (skills/README.md).
FOLLOWS = {"munimentum-research": ["archive_health"],
         "munimentum-case": ["case_brief", "collect_into_case"],
         "munimentum-evidence": ["who_knew_what", "timeline"]}
PROMPT_ARGS = {"archive_health": {},
               "case_brief": {"case": "Nordwind"},
               "collect_into_case": {"case": "Nordwind", "question": "x"},
               "who_knew_what": {"topic": "x", "until": "2025-06-30"},
               "timeline": {"topic": "x"}}

# Result fields a skill may name besides tools and parameters. Only what a
# tool really returns – grep mcp_server.py before adding one here.
FIELDS = {"default_backend", "not_in_archive", "next_offset",
          "more_in_thread", "item_sha256", "guide_token",
          # the one tool the server offers when MCP access is switched off
          "archive_unavailable"}

# snake_case in backticks: a tool, a parameter, a field or a verdict.
IDENTIFIER = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`")
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def _skills():
    return sorted(p for p in SKILLS.iterdir() if (p / "SKILL.md").is_file())


def _text(name):
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def _frontmatter(text):
    m = FRONTMATTER.match(text)
    assert m, "SKILL.md beginnt nicht with_guide einem ---Block"
    fields = {}
    for line in m.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _tools():
    """Name → parameter names, as the SDK registered them."""
    tools = anyio.run(mcp_server.mcp.list_tools)
    return {t.name: set(t.input_schema.get("properties", {})) for t in tools}


def _verdicts():
    """The verdicts verify_item documents – its docstring is what a client
    reads, so that is where the list lives."""
    doc = mcp_server.verify_item.__doc__
    part = doc[doc.index("`verdict` is one of"):doc.index("`summary`")]
    return set(re.findall(r'"([a-z_]+)"', part))


def test_there_are_three_skills():
    """Safeguard against a path error that silently empties every test below."""
    assert [p.name for p in _skills()] == sorted(FOLLOWS)


@pytest.mark.parametrize("path", _skills(), ids=lambda p: p.name)
def test_frontmatter_is_what_clients_accept(path):
    """name matches the folder, description says when – within the limits
    claude.ai checks on upload."""
    fields = _frontmatter((path / "SKILL.md").read_text(encoding="utf-8"))
    assert fields.get("name") == path.name
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", path.name) and len(path.name) <= 64
    assert "claude" not in path.name and "anthropic" not in path.name
    description = fields.get("description", "")
    assert description.startswith("Use when"), "does not say when the skill applies"
    assert len(description) <= 1024
    assert "<" not in description and ">" not in description


@pytest.mark.parametrize("name", sorted(FOLLOWS))
def test_skill_names_only_what_the_server_has(name):
    tools = _tools()
    known = set(tools) | set().union(*tools.values()) | _verdicts() | FIELDS
    unknown = set(IDENTIFIER.findall(_text(name))) - known
    assert not unknown, f"{name} names what the server does not have: {sorted(unknown)}"


@pytest.mark.parametrize("name", sorted(FOLLOWS))
def test_skill_keeps_up_with_its_prompts(name):
    """A step a prompt gains is a step its skill must learn."""
    tools = set(_tools())
    in_skill = set(IDENTIFIER.findall(_text(name))) & tools
    for prompt in FOLLOWS[name]:
        text = getattr(mcp_server, prompt)(**PROMPT_ARGS[prompt])
        missing = (set(re.findall(r"`(\w+)`", text)) & tools) - in_skill
        assert not missing, f"{prompt} uses {sorted(missing)}, {name} does not know them"


def test_every_tool_is_in_some_skill():
    """A new tool without a word in any skill is a tool Claude meets
    through its docstring alone."""
    tools = set(_tools())
    named = set().union(*(IDENTIFIER.findall(_text(n)) for n in FOLLOWS))
    assert tools - named == set()


def test_evidence_skill_words_every_verdict():
    """Each verdict changes how an item may be quoted – one left out is one
    Claude has to guess."""
    text = _text("munimentum-evidence")
    for v in _verdicts():
        mcp_server._verdict_text(v, {}, None)       # a verdict the server knows
        assert re.search(rf"^\| {v} \|", text, re.M), f"{v} missing in der Tabelle"


def test_readme_lists_the_prompts_each_skill_follows():
    """skills/README.md and FOLLOWS say the same, so a reader finds the pairs."""
    lines = (SKILLS / "README.md").read_text(encoding="utf-8").splitlines()
    for name, prompts in FOLLOWS.items():
        assert any(z.startswith(f"| `{name}` |") and all(f"`{p}`" in z for p in prompts)
                   for z in lines), f"README does not name {prompts} beside {name}"


def test_every_prompt_is_followed_by_a_skill():
    """A new prompt without a skill is a workflow a bridged client never sees."""
    source = (ROOT / "mcp_server.py").read_text(encoding="utf-8")
    prompts = {n.name for n in ast.parse(source).body
               if isinstance(n, ast.FunctionDef)
               and any(ast.unparse(d).startswith("mcp.prompt") for d in n.decorator_list)}
    assert prompts == set(PROMPT_ARGS) == set().union(*FOLLOWS.values())


def test_every_gated_tool_is_named_in_its_guide():
    """A tool that refuses without a guide must be one that guide covers –
    else the model reads a text that does not mention what it wanted."""
    for tool, topic in mcp_server._GUIDE_FOR.items():
        assert topic in mcp_server._SKILLS, f"{tool}: no skill {topic}"
        assert f"`{tool}`" in _text(f"munimentum-{topic}"), f"{tool} missing in {topic}"


# --------------------------------------------------------------------------
# Served per SEP-2640: skill:// resources, skills/list, skills/get
# --------------------------------------------------------------------------
JSON = TypeAdapter(dict[str, Any])


def _client(fn):
    async def run():
        async with Client(mcp_server.mcp) as c:
            return await fn(c)
    return anyio.run(run)


def _skills_request(c, method, **params):
    """skills/list and skills/get are extension methods – the client has no
    wrapper for them, so they go out as plain requests."""
    p = mcp_server._SkillGetParams(**params) if params else {}
    return c.session.send_request(Request(method=method, params=p), JSON)


def test_the_server_declares_the_skills_extension():
    async def run(c):
        return c.server_capabilities
    assert mcp_server._SKILLS_EXT in (_client(run).extensions or {})


def test_every_skill_is_a_resource_to_read():
    async def run(c):
        listed = {str(r.uri): r for r in (await c.list_resources()).resources}
        return listed, {u: (await c.read_resource(u)).contents[0]
                        for u in listed if u.startswith("skill://")}
    listed, read_back = _client(run)
    for name in FOLLOWS:
        uri = f"skill://{name}/SKILL.md"
        assert listed[uri].name == name and listed[uri].mime_type == "text/markdown"
        # the menu shows the title, prefixed by its kind like every entry there
        assert listed[uri].title == f"Skill: {name.removeprefix('munimentum-').capitalize()}"
        assert listed[uri].description == _frontmatter(_text(name))["description"]
        assert read_back[uri].text == _text(name)


def test_skills_list_carries_frontmatter_digest_and_size():
    res = _client(lambda c: _skills_request(c, "skills/list"))
    entries = {e["uri"]: e for e in res["skills"]}
    assert set(entries) == {f"skill://{n}/SKILL.md" for n in FOLLOWS}
    for name in FOLLOWS:
        raw = (SKILLS / name / "SKILL.md").read_bytes()
        e = entries[f"skill://{name}/SKILL.md"]
        assert e["frontmatter"] == _frontmatter(raw.decode("utf-8"))
        assert e["resources"] == [{"uri": e["uri"], "size": len(raw),
                                   "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}]


def test_skills_get_answers_by_uri_and_refuses_an_unknown_one():
    uri = "skill://munimentum-case/SKILL.md"
    res = _client(lambda c: _skills_request(c, "skills/get", uri=uri))
    assert res["skill"]["uri"] == uri and res["skill"]["frontmatter"]["name"] == "munimentum-case"
    async def unknown(c):
        # caught inside the connection – outside it arrives as a task group's error
        try:
            await _skills_request(c, "skills/get", uri="skill://nope/SKILL.md")
        except MCPError as e:
            return e.code
    assert _client(unknown) == INVALID_PARAMS        # what the SEP prescribes


def test_the_instructions_point_at_the_guides():
    """A host that reads instructions finds the skills without discovery."""
    for name in FOLLOWS:
        assert f"skill://{name}/SKILL.md" in mcp_server._INSTRUCTIONS
    assert "get_guide" in mcp_server._INSTRUCTIONS


# --------------------------------------------------------------------------
# Through the tools: get_guide and the gate
# --------------------------------------------------------------------------
# The fewest arguments each gated tool's schema lets through – the gate
# answers before the function runs, so no archive is needed.
LEAST = {"search_messages": {"query": "x"}, "browse_messages": {},
              "add_to_case": {"case": "x"}, "add_case_note": {"case": "x", "text": "y"},
              "collect_case": {"case": "x"}, "verify_item": {}}


def _call(tool, args):
    res = _client(lambda c: c.call_tool(tool, args))
    return res, json.loads(res.content[0].text)


def test_minimal_args_cover_every_gated_tool():
    assert set(LEAST) == set(mcp_server._GUIDE_FOR)


@pytest.mark.parametrize("tool", sorted(LEAST))
def test_without_the_token_a_gated_tool_answers_with_its_guide(tool):
    topic = mcp_server._GUIDE_FOR[tool]
    skill = mcp_server._SKILLS[topic]
    res, answer = _call(tool, LEAST[tool])
    # An answer, not a protocol error: the model reads it and goes on.
    assert res.is_error is False
    assert answer["guide_token"] == skill["token"]
    assert answer["guide"] == skill["body"] and answer["guide"].startswith("# ")
    assert f'guide="{skill["token"]}"' in answer["error"]


@pytest.mark.parametrize("tool", sorted(LEAST))
def test_another_guides_token_does_not_open_the_gate(tool):
    other = next(s["token"] for t, s in mcp_server._SKILLS.items()
                 if t != mcp_server._GUIDE_FOR[tool])
    _, answer = _call(tool, {**LEAST[tool], "guide": other})
    assert answer.get("guide_token") == mcp_server._SKILLS[mcp_server._GUIDE_FOR[tool]]["token"]


def test_get_guide_hands_out_text_token_and_what_it_opens():
    _, answer = _call("get_guide", {"topic": "case"})
    skill = mcp_server._SKILLS["case"]
    assert answer["guide_token"] == skill["token"] and answer["guide"] == skill["body"]
    assert answer["skill"] == "skill://munimentum-case/SKILL.md"
    assert answer["token_for"] == ["add_case_note", "add_to_case", "collect_case"]
    # the skill's own name works as well
    assert _call("get_guide", {"topic": "munimentum-case"})[1]["guide_token"] == skill["token"]
    unknown = _call("get_guide", {"topic": "nope"})[1]
    assert unknown["error"] and unknown["topics"] == sorted(mcp_server._SKILLS)


def test_only_gated_tools_take_a_guide_and_the_app_never_meets_it():
    """The parameter exists at the MCP boundary; the functions the app
    calls in-process have no such argument."""
    tools = _client(lambda c: c.list_tools()).tools
    with_guide = {t.name for t in tools if "guide" in t.input_schema.get("properties", {})}
    assert with_guide == set(mcp_server._GUIDE_FOR)
    for tool in mcp_server._GUIDE_FOR:
        assert "guide" not in inspect.signature(getattr(mcp_server, tool)).parameters


def test_a_changed_guide_is_a_new_token(tmp_path):
    """The token is the guide's hash: an old one stops working when the
    text changes, so a model cannot pass a gate with a guide it never saw."""
    copy = tmp_path / "skills"
    shutil.copytree(SKILLS, copy)
    before = mcp_server._load_skills(copy)
    file = copy / "munimentum-case" / "SKILL.md"
    file.write_text(file.read_text(encoding="utf-8") + "\nOne more rule.\n", encoding="utf-8")
    after = mcp_server._load_skills(copy)
    assert after["case"]["token"] != before["case"]["token"]
    assert after["research"]["token"] == before["research"]["token"]


def test_a_skill_with_windows_line_endings_is_the_same_skill(tmp_path):
    """Git for Windows checks SKILL.md out with CRLF: the frontmatter still
    parses, and digest, size and token are those of the LF text – the
    Windows bundle must serve what the others serve."""
    copy = tmp_path / "skills"
    shutil.copytree(SKILLS, copy)
    for file in copy.glob("*/SKILL.md"):
        file.write_bytes(file.read_bytes().replace(b"\n", b"\r\n"))
    crlf, lf = mcp_server._load_skills(copy), mcp_server._load_skills(SKILLS)
    assert {t: s["token"] for t, s in crlf.items()} == {t: s["token"] for t, s in lf.items()}
    assert all(crlf[t]["digest"] == lf[t]["digest"] and crlf[t]["size"] == lf[t]["size"] for t in lf)
    assert "\r" not in crlf["case"]["text"]


def test_no_skills_folder_serves_nothing_and_gates_nothing(tmp_path):
    """A bundle that lost skills/ must not refuse every search."""
    assert mcp_server._load_skills(tmp_path / "missing") == {}


def test_frontmatter_the_server_cannot_read_flat_is_an_error():
    with pytest.raises(ValueError):
        mcp_server._skill_frontmatter("---\nname: x\nmetadata:\n  a: b\n---\nbody")
    with pytest.raises(ValueError):
        mcp_server._skill_frontmatter("no frontmatter")
