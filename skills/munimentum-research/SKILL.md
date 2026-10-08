---
name: munimentum-research
description: Use when the user asks about their own Microsoft 365 history held in a Munimentum archive – past mail, Teams chats, appointments, contacts, OneDrive or SharePoint files, Planner, To Do, OneNote, the org chart – or whether that archive is current and complete. Picks the right Munimentum tool, searches well, cites.
---

# Researching a Munimentum archive

This guide belongs to Munimentum 14.5.0. Installed by hand – under
Customize › Skills or in `~/.claude/skills/` – it does not change with the
app: `get_guide` (and `get_version`) answer the server's `version`; when
it differs from this one, follow the guide `get_guide` hands you instead.

Munimentum is a local, read-only copy of the user's Microsoft 365 data,
served by an MCP server named `munimentum`. Its tools may carry a client
prefix (`mcp__munimentum__…`, `mcp__remote-devices__munimentum__…`); the
names below are without it. If the client defers tools, load the ones the
task needs in one go.

There is no live mailbox behind it: what was not exported is absent, and the
archive has edges in time. Most wrong answers come from forgetting that.

For work on a case use `munimentum-case`; for "who knew what since when", a
chronology meant as proof, or checking an item before quoting it as evidence
use `munimentum-evidence`. Both build on this one.

If the server answers with `archive_unavailable`, MCP access is switched off
in the app: pass its sentence on and stop – no other path reaches the data.

## Once per conversation, before the first answer

0. **The guide token.** `search_messages` and `browse_messages` take a
   `guide` token and answer without one with this guide instead. If this
   text reached you through `get_guide` or such an answer, the
   `guide_token` beside it is the one; otherwise call `get_guide` with
   topic "research" once. Pass it on every later call.
1. `list_sources` – which sources exist, what each indexes, what `folder`
   and `person` mean there, and `not_in_archive` (never exported: say so
   instead of searching).
2. `corpus_stats` – coverage (first and last item), months without a single
   item, last successful export per source, and the ranking backend
   (`default_backend`: "hybrid" or "lexical").

Keep both results in mind for the rest of the conversation.

## Which tool

| The user wants | Tool |
|---|---|
| Content on a topic | `search_messages` |
| Everything matching filters, no topic ("all from Bob in June") | `browse_messages` |
| One hit in full, with its facts | `get_document` (chat: `context_before`/`context_after`) |
| The conversation around a mail or chat hit | `get_thread` with the hit's `thread` |
| A person's spelling before filtering | `list_people` (`contains`) |
| Which addresses stand in From/To/Cc | `list_addresses` |
| Values for `folder` / `filetype` | `list_folders` / `list_filetypes` |
| Appointments with start, end, attendees | `list_events` – upcoming ones need `date_from`/`date_to`, `days` only looks back |
| Phone, e-mail, organisation | `lookup_contact` |
| Files by folder | `list_files` (contents are not indexed) |
| Who reports to whom, on any day | `get_org_chart`, `get_manager`, `list_reports`, `find_by_role`, `list_roles`, `org_changes` (all take `as_of`) |
| The user's saved searches | `list_saved_searches`, `run_saved_search` |
| The archive about itself (volume, top contacts, file types) | `archive_analytics` |
| The raw .eml/.html/.ics/.vcf | `read_source_file` – last resort; binaries return metadata only |

## Searching well

- **Read the backend.** Every result names it in `backend`. With "lexical"
  only words that literally occur match: search with the words the text
  would contain, not a description of the idea. With "hybrid" paraphrases
  work too, but exact tokens (invoice numbers, names) still win.
- **Several phrasings, both languages.** German and English mixed is normal
  in this data. Try the noun, the verb, the compound and its parts
  ("Kündigungsfrist", "Kündigung Frist", "notice period").
- **Quotes mean exactly that.** A "quoted phrase" must occur adjacent and in
  order; every phrase is required. Use it to cut noise, not by default.
- **Resolve before filtering.** `person` is a substring over names and
  addresses – look the spelling up with `list_people` first. Files and pages
  carry no person; the filter finds nothing there.
- **Mail lines.** `mail_from`, `mail_to`, `mail_cc`, `mail_bcc` restrict to
  mail only; `*` is a wildcard ("*@customer.example"). Bcc exists only in
  mail the user sent.
- **Narrow by place and time.** `source` (one key or comma-separated),
  `folder`, `filetype`, `date_from`/`date_to` or `days`, `party`
  ("internal"/"external"), `with_attachments`, `only_gone` (deleted at
  Microsoft, kept here).
- **Page, don't widen.** Follow `next_offset` instead of raising `k`. A hit
  with `more_in_thread` stands for its conversation – `get_thread` has the rest.
- **Files are found by name, path and type only.** A question about what a
  document says will not find it; its name or `filetype` will.
- Read before you conclude: a preview is 200 characters. `get_document`
  gives the full text and `facts` (from, to, cc, attendees, due dates …) –
  never parse those out of text yourself.

## Before saying "there is nothing"

An empty result means "not in this archive", not "did not happen". Check
in this order and say which applies:

1. Is the period inside the coverage, and is the month not one of the gaps?
2. Was the source exported at all (`not_in_archive`), and recently?
3. With "lexical": did you try the literal words?

Phrase it as "The archive holds nothing on X between A and B" and name the
limit you found.

## "Can I rely on the archive?"

1. `list_runs` – the last runs, their `result`; for one with errors or a
   step with `ok: 0`, `run_log` says why.
2. `source_completeness` – per source what Microsoft holds against what is
   here (`offen` not fetched, `verweigert` refused, `behalten` kept after
   deletion). A row never checked is not "complete".
3. `corpus_stats` – coverage, gaps, backend.

Answer with one line per source – current, stale or incomplete, since when,
and what to do in Munimentum – then one sentence on what can and cannot be
concluded today.

## Citing

Quote every statement with the item's `get_document` cite.label, plus
cite.link when it is not null. An item shown on its own – quoted, summarised
or described as one message – was read with `get_document` and carries its
cite.label and cite.link; a list of hits carries labels only. A null link
means Munimentum is not running: say once that the links work while it
runs. Answer in the user's language.
