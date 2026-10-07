"""
The skills in skills/ against the server they describe.

A skill is prose the server never reads, so nothing else notices when a
tool is renamed, a parameter goes or a prompt gains a step. These tests
do: every tool, parameter and verdict a skill names in backticks must
exist; every tool a prompt names must be in the skill that follows it;
and every tool must be in some skill.
"""

import ast
import re
from pathlib import Path

import anyio
import pytest

import mcp_server

WURZEL = Path(__file__).resolve().parent.parent
SKILLS = WURZEL / "skills"

# Which prompts each skill follows step by step (skills/README.md).
FOLGT = {"munimentum-research": ["archive_health"],
         "munimentum-case": ["case_brief", "collect_into_case"],
         "munimentum-evidence": ["who_knew_what", "timeline"]}
PROMPT_ARGS = {"archive_health": {},
               "case_brief": {"case": "Nordwind"},
               "collect_into_case": {"case": "Nordwind", "question": "x"},
               "who_knew_what": {"topic": "x", "until": "2025-06-30"},
               "timeline": {"topic": "x"}}

# Result fields a skill may name besides tools and parameters. Only what a
# tool really returns – grep mcp_server.py before adding one here.
FELDER = {"default_backend", "not_in_archive", "next_offset",
          "more_in_thread", "item_sha256",
          # the one tool the server offers when MCP access is switched off
          "archive_unavailable"}

# snake_case in backticks: a tool, a parameter, a field or a verdict.
BEZEICHNER = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`")
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def _skills():
    return sorted(p for p in SKILLS.iterdir() if (p / "SKILL.md").is_file())


def _text(name):
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def _frontmatter(text):
    m = FRONTMATTER.match(text)
    assert m, "SKILL.md beginnt nicht mit einem ---Block"
    felder = {}
    for zeile in m.group(1).splitlines():
        schluessel, _, wert = zeile.partition(":")
        felder[schluessel.strip()] = wert.strip()
    return felder


def _tools():
    """Name → parameter names, as the SDK registered them."""
    tools = anyio.run(mcp_server.mcp.list_tools)
    return {t.name: set(t.input_schema.get("properties", {})) for t in tools}


def _verdicts():
    """The verdicts verify_item documents – its docstring is what a client
    reads, so that is where the list lives."""
    doc = mcp_server.verify_item.__doc__
    teil = doc[doc.index("`verdict` is one of"):doc.index("`summary`")]
    return set(re.findall(r'"([a-z_]+)"', teil))


def test_there_are_three_skills():
    """Safeguard against a path error that silently empties every test below."""
    assert [p.name for p in _skills()] == sorted(FOLGT)


@pytest.mark.parametrize("pfad", _skills(), ids=lambda p: p.name)
def test_frontmatter_is_what_clients_accept(pfad):
    """name matches the folder, description says when – within the limits
    claude.ai checks on upload."""
    felder = _frontmatter((pfad / "SKILL.md").read_text(encoding="utf-8"))
    assert felder.get("name") == pfad.name
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", pfad.name) and len(pfad.name) <= 64
    assert "claude" not in pfad.name and "anthropic" not in pfad.name
    beschreibung = felder.get("description", "")
    assert beschreibung.startswith("Use when"), "sagt nicht, wann der Skill greift"
    assert len(beschreibung) <= 1024
    assert "<" not in beschreibung and ">" not in beschreibung


@pytest.mark.parametrize("name", sorted(FOLGT))
def test_skill_names_only_what_the_server_has(name):
    tools = _tools()
    bekannt = set(tools) | set().union(*tools.values()) | _verdicts() | FELDER
    unbekannt = set(BEZEICHNER.findall(_text(name))) - bekannt
    assert not unbekannt, f"{name} nennt, was der Server nicht kennt: {sorted(unbekannt)}"


@pytest.mark.parametrize("name", sorted(FOLGT))
def test_skill_keeps_up_with_its_prompts(name):
    """A step a prompt gains is a step its skill must learn."""
    tools = set(_tools())
    im_skill = set(BEZEICHNER.findall(_text(name))) & tools
    for prompt in FOLGT[name]:
        text = getattr(mcp_server, prompt)(**PROMPT_ARGS[prompt])
        fehlt = (set(re.findall(r"`(\w+)`", text)) & tools) - im_skill
        assert not fehlt, f"{prompt} nutzt {sorted(fehlt)}, {name} kennt sie nicht"


def test_every_tool_is_in_some_skill():
    """A new tool without a word in any skill is a tool Claude meets
    through its docstring alone."""
    tools = set(_tools())
    genannt = set().union(*(BEZEICHNER.findall(_text(n)) for n in FOLGT))
    assert tools - genannt == set()


def test_evidence_skill_words_every_verdict():
    """Each verdict changes how an item may be quoted – one left out is one
    Claude has to guess."""
    text = _text("munimentum-evidence")
    for v in _verdicts():
        mcp_server._verdict_text(v, {}, None)       # a verdict the server knows
        assert re.search(rf"^\| {v} \|", text, re.M), f"{v} fehlt in der Tabelle"


def test_readme_lists_the_prompts_each_skill_follows():
    """skills/README.md and FOLGT say the same, so a reader finds the pairs."""
    zeilen = (SKILLS / "README.md").read_text(encoding="utf-8").splitlines()
    for name, prompts in FOLGT.items():
        assert any(z.startswith(f"| `{name}` |") and all(f"`{p}`" in z for p in prompts)
                   for z in zeilen), f"README nennt {prompts} nicht bei {name}"


def test_every_prompt_is_followed_by_a_skill():
    """A new prompt without a skill is a workflow a bridged client never sees."""
    quelle = (WURZEL / "mcp_server.py").read_text(encoding="utf-8")
    prompts = {n.name for n in ast.parse(quelle).body
               if isinstance(n, ast.FunctionDef)
               and any(ast.unparse(d).startswith("mcp.prompt") for d in n.decorator_list)}
    assert prompts == set(PROMPT_ARGS) == set().union(*FOLGT.values())
