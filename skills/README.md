# Skills for Claude

Three [Agent Skills](https://claude.com/docs/skills/how-to) that teach Claude
how to work with the Munimentum MCP server:

| Skill | For |
|---|---|
| `munimentum-research` | Any question about the archive: which tool, how to search, when "no hits" means something, citing. Also "can I rely on the archive?" |
| `munimentum-case` | Briefing on a case, keeping it current, and filing into it – only after the user says yes |
| `munimentum-evidence` | Who knew what and since when, a chronology meant as proof, and checking an item with `verify_item` before quoting it |

## Why, when the server already explains itself

The server's instructions and its prompts carry the same know-how, but not
every client passes them on. A client that reaches the server through a
bridge – claude.ai linked to the desktop app, for one – sees the tools and
their docstrings, and neither the instructions nor the prompts. And a prompt
runs only when the user picks it; a skill comes in by itself when the
question matches.

So the skills are not a second source of truth: they follow the prompts
step by step. `tests/test_skills.py` fails when a prompt names a tool its
skill does not, when a skill names a tool, parameter or verdict the server
does not have, or when a tool is in no skill at all.

| Skill | Follows |
|---|---|
| `munimentum-research` | the server instructions, `archive_health` |
| `munimentum-case` | `case_brief`, `collect_into_case` |
| `munimentum-evidence` | `who_knew_what`, `timeline` |

## Installing

- **Claude Code:** copy the three folders into `~/.claude/skills/` (for you)
  or `.claude/skills/` of a project.
- **claude.ai and Claude Desktop:** zip each folder on its own – the folder
  itself as the single top-level item – and upload it; then switch it on
  under *Customize › Skills*. Skills need code execution to be on.

The MCP server still has to be connected; the skills only tell Claude how
to use it.
