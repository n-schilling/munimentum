---
name: munimentum-case
description: Use when the user works with a case in their Munimentum archive – asks for a briefing or summary of one, what is new or missing in it, or wants hits, result lists, notes or remarks filed into it. Covers reading a case and adding to it only after the user confirms.
---

# Working with Munimentum cases

A case collects what belongs to one matter – items from every source,
result lists and saved searches – without copying anything: it points at
items by a key that survives a rename or a move. It has a casebook of dated
notes, folders one level deep, and a remark on every item saying why it is
there. Closed cases are read-only.

Search technique, backends and the archive's edges are in
`munimentum-research`; follow it for every search here. Before an item of a
case is called unchanged evidence, check it with `verify_item` and word it
by its verdict as `munimentum-evidence` says.

## Find the case

`list_cases` first. The user often names a case loosely – match on name
and description, and ask when two fit. Every other case tool takes the
case's `name` or `id`.

## Brief me on a case

1. `get_case` – description, casebook, folders, remarks, attached searches.
   A large case comes as a summary; `case_timeline` pages through the items
   in order (`next_offset`).
2. `case_timeline` – the chronology with excerpts. Where one message says
   too little, `get_thread` for the conversation around it.
3. `case_people` – who is involved, and how often.
4. `corpus_stats` and `source_completeness` – how far the archive reaches,
   and whether a source the case depends on has gaps.
5. `case_new_hits` – what the case's saved searches find today that the
   case does not hold yet.

Then write, in this order: the state of the matter in three sentences; a
dated chronology; the people and their part; open points; what the archive
cannot tell (gaps, sources never exported). Cite every statement with
`get_document`'s cite.label and cite.link.

## Search inside a case

`search_messages` and `browse_messages` take `case` and `case_folder`.
"Everything in the case, newest first" is `browse_messages` with `case`.
A hit that already sits in a case says so in `cases`, folder included.

## Keep a case current

- `case_new_hits` shows what the attached searches find that is missing.
  What the user once took out is not offered again.
- `collect_case` runs the case's automatic searches now (or one attached
  search with `search`) and files the result – a write, see below.
- `list_saved_searches` / `run_saved_search` run the user's searches exactly
  as saved.

## Adding to a case – only after a yes

The tools that write are `add_to_case`, `add_case_note` and `collect_case`.
Everything they add is marked "via MCP" in Munimentum. They take the case
guide's token as `guide` – the `guide_token` that came with this text, or
`get_guide` with topic "case" once – and answer without it with this
guide instead.

1. `get_case` for what the case holds already, its folders and searches.
2. Search with several phrasings; leave out hits whose `cases` already
   name this case.
3. Show the candidates as a numbered list, grouped by the folder each
   would go into: cite.label and one line on why it belongs.
4. **Wait.** Add only what the user picked, by number or name. "Sounds
   good" to a list counts for that list, nothing beyond it.
5. `add_to_case` with the picked `uids` (or `keys`), the `folder`, and a
   `remark` of one or two sentences saying why – in the user's language.
   Folders are made by the user in Munimentum; an unknown folder is a
   question for the user, not a reason to drop the folder.
6. `add_case_note` only for a note the user asked for or approved, worded
   as they would want to read it later.

When a write answers that changes through MCP are switched off, tell the
user it can be allowed in Munimentum under *Settings › Claude (MCP)* –
"Claude may change cases" – and stop. When a case is closed, say so; do not
look for a way around it. Nothing here ever removes an item or a note.
