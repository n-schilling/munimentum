# Skills for Claude

Three [Agent Skills](https://agentskills.io/specification) that teach Claude
how to work with the Munimentum MCP server:

| Skill | For |
|---|---|
| `munimentum-research` | Any question about the archive: which tool, how to search, when "no hits" means something, citing. Also "can I rely on the archive?" |
| `munimentum-case` | Briefing on a case, keeping it current, and filing into it – only after the user says yes |
| `munimentum-evidence` | Who knew what and since when, a chronology meant as proof, and checking an item with `verify_item` before quoting it |

## How they reach Claude

The server's instructions and its prompts carry the same know-how, but not
every client passes them on: a client that reaches the server through a
bridge – claude.ai linked to the desktop app, for one – sees the tools and
nothing else. So the server hands the skills out two more ways, both from
this folder:

- **As skills, per [SEP-2640](https://modelcontextprotocol.io/seps/2640-skills-extension).**
  The server declares the extension `io.modelcontextprotocol/skills`, serves
  each `SKILL.md` as the resource `skill://<name>/SKILL.md` and answers
  `skills/list` and `skills/get`. A host that loads skills from servers
  needs nothing else.
- **Through the tools, enforced.** `get_guide` returns a skill's text and a
  token. The tools where a mistake costs most ask for that token as `guide`
  and, without it, answer with the guide instead of a result:

  | Tools | Guide |
  |---|---|
  | `search_messages`, `browse_messages` | research |
  | `add_to_case`, `add_case_note`, `collect_case` | case |
  | `verify_item` | evidence |

  The token is a hash of the guide, not a session: protocol 2026-07-28 has
  none. Whoever holds it was handed the guide; a changed guide is a new
  token. The gate sits at the MCP boundary only – the app's own search calls
  the same functions and never meets it.

The skills are not a second source of truth: they follow the prompts step by
step. `tests/test_skills.py` fails when a prompt names a tool its skill does
not, when a skill names a tool, parameter or verdict the server does not
have, when a tool is in no skill, or when a gated tool is missing from its
guide.

| Skill | Follows |
|---|---|
| `munimentum-research` | the server instructions, `archive_health` |
| `munimentum-case` | `case_brief`, `collect_into_case` |
| `munimentum-evidence` | `who_knew_what`, `timeline` |

## Installing by hand

For a client that does not load skills from servers, so that the know-how
is there before the first tool is picked:

- **Claude Code:** copy the three folders into `~/.claude/skills/` (for you)
  or `.claude/skills/` of a project.
- **claude.ai and Claude Desktop:** zip each folder on its own – the folder
  itself as the single top-level item – and upload it; then switch it on
  under *Customize › Skills*. Skills need code execution to be on.

Installed by hand or not, the gated tools still want their token; the skill
says where to get it.
