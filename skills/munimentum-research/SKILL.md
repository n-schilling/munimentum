---
name: munimentum-research
description: Use when the user asks about their own Microsoft 365 history held in a Munimentum archive – past mail, Teams chats, appointments, contacts, OneDrive or SharePoint files, Planner, To Do, OneNote, the org chart – or whether that archive is current and complete. Picks the right Munimentum tool, searches well, cites.
---

# Researching a Munimentum archive

This guide belongs to Munimentum 15.3.0. Installed by hand – under
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
2. `corpus_stats` – whose archive this is (`account`), coverage (first and
   last item), months without a single item, last successful export per
   source, and the ranking backend (`default_backend`: "hybrid" or
   "lexical").

Keep both results in mind for the rest of the conversation, and name the
account in the first answer: the user may keep several archives.

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
| Phone, e-mail, organisation, job title | `lookup_contact` |
| Whom the user exchanges mail with, how often, since when | `list_correspondents` |
| What a person's mail footer says (title, company, numbers) | `get_signatures` |
| Files by folder | `list_files` (contents are not indexed) |
| Who reports to whom, on any day | `get_org_chart`, `get_manager`, `list_reports`, `find_by_role`, `list_roles`, `org_changes` (all take `as_of`) |
| The organization's health: spans, layers, seniority | `org_health` (`as_of`, `below` a person, `history`) |
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

## "How healthy is the organization?"

`org_health` gives numbers from the tree and the user's role model, never
job titles: the span of control (direct reports per leader – median, max,
how many lead one, how many more than twelve), the share of leaders,
people per leader, the layers, the seniority pyramid with the people not
assigned or outside the organization beside it, the widest spans and
`history` – every version's figures. `below` a person gives one part of the
tree: compare the parts `list_reports` names under the top to find the
top-heavy ones; `get_org_chart` shows where a leader stands, `org_changes`
who joined, left or moved between two versions. Say what the numbers rest
on: a short history, many people not assigned, a role model that is still
the default.

## "Which contacts belong in my CRM?" – a contact list

1. `list_correspondents` for the period the user means (`date_from`,
   `date_to` or `days`), usually with `party` "external". Each row counts
   an address's mail in both directions: received from it, sent to it. The
   default `min_sent` of 1 keeps newsletters and ticket systems out – they
   write, the user never answers. Check `me` in the answer: those must be
   the user's own addresses; if not, pass the right ones as `me`.
2. Decide who is in, and say by which rule ("at least five mails each way
   since January"). Follow `next_offset` to the end before counting;
   `exclude_domains` drops what does not belong, `address_book` "no" lists
   only people the address book lacks. A row marked `ignored` is an
   address the user set to be ignored in Munimentum's address book (a
   shared mailbox, a newsletter, somebody they do not want listed): leave
   it out, and say how many you left out for that reason.
3. A row with a contact card has name parts, organisation, job title and
   numbers from the address book. A card with `origin` "munimentum" was
   added by the user in Munimentum, not exported from Outlook – it is an
   address book contact all the same; say where it comes from when that
   matters. For everyone else, and for what a card
   lacks, `get_signatures` reads the footers under their mails – up to 25
   addresses a call. Take numbers from its phones, and name, job title,
   company and postal address from its lines; leave a field empty rather
   than guess, and treat a footer read from a single mail as less sure.
   A footer is read by rules and can be incomplete or carry a line that
   is not the person's: present it as read from mail footers, to check.
   Asked for one person's signature, number or job title, call
   `get_signatures` with the name or the address – never search and read
   the person's mails for it: where Munimentum has read the footer
   already, the answer comes from there and opens no mail.
4. Write the file in the template's columns, one row per person, and say
   what came from the address book, what from footers, and who has neither.

Mail only: someone the user only chats with in Teams is not in this list.

## Citing

Quote every statement with the item's `get_document` cite.label, plus
cite.link when it is not null. An item shown on its own – quoted, summarised
or described as one message – was read with `get_document` and carries its
cite.label and cite.link; a list of hits carries labels only. A null link
means Munimentum is not running: say once that the links work while it
runs. Answer in the user's language.
