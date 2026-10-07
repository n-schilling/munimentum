---
name: munimentum-evidence
description: Use when the user needs something they can rely on from their Munimentum archive – who knew about a topic and since when, a dated chronology across mail, Teams, calendar and files, or whether an item is still the unchanged original before it is quoted. Every line cited, verified and with the archive's gaps marked.
---

# Evidence from a Munimentum archive

The user wants something that holds up: in a dispute, a handover, a review.
Search technique and the archive's edges are in `munimentum-research` –
follow it. What changes here is the standard: **every line rests on a cited
item, every quoted item is verified, and every gap is said out loud.**

Say what the archive shows, not what it means legally. "Mail from A to B on
3 May names the deadline" is yours to say; "B was therefore informed in
time" is the user's call, or their lawyer's.

## Who knew what, and since when

1. `corpus_stats` first: where the archive begins and ends, which months are
   empty. No evidence inside a gap proves nothing.
2. `search_messages` with the words that would literally stand in the
   texts – several phrasings, both languages, `date_to` when the user gives
   an "up to". Mind the `backend`: with "lexical" a paraphrase misses.
3. `get_document` for every relevant hit; its `facts` name `from`, `to`,
   `cc` (mail) and the author (chat). For replies, `get_thread`.
4. `list_events` for meetings on the topic – organiser and attendees.
5. Per person keep only the **earliest** demonstrable date.

Answer with a table: person · first date they demonstrably knew · how ·
the item. "How" is one of:

| How | Shown by |
|---|---|
| wrote it | sender of the mail, author of the chat message |
| received it | in the To line |
| in cc | in the Cc line |
| in bcc | Bcc line – only in mail the user sent |
| invited | attendee or organiser of a meeting on the topic |

Being invited is not attending, and receiving is not reading – say so when
it matters. Membership of a Teams chat is not in the facts: claim it only
when the person wrote in that chat before the date.

## A chronology of a topic

1. `search_messages` across all sources, several phrasings, `date_from`/
   `date_to` where given; follow `next_offset` while hits still matter.
   Files are found by name and path only.
2. `list_events` for the meetings in the period.
3. `get_document` or `get_thread` where a preview is not enough to date or
   place a hit.
4. `corpus_stats` for gaps in the period.

One line per event: date · what happened · who · the item. Put each gap in
the archive where it falls ("— no items 2025-08 —"), so silence is not read
as nothing happening.

## Before an item is called evidence

`verify_item` with its `uid` (or the `key` from cite.key). It hashes the file
afresh, holds it against the evidence chain and finds the time-stamp. Pass
its `summary` on, and word the item by `verdict`:

| verdict | How to quote the item |
|---|---|
| unchanged | unchanged since it was captured; name the time-stamp, or that none covers it yet |
| unchanged_outside_version | matches the chain, but the chain recorded a change made outside the app – not the version Microsoft handed out; `versions` in `get_document` names the earlier one |
| changed_by_app | the app fetched a newer version; the recorded one is kept – say which you quote |
| changed_outside | changed on disk outside the app – never call it the original |
| not_yet_chained | too new for the chain; it can vouch only after the next run |
| chain_broken | the chain proves nothing right now; the archive check in Munimentum names the line |
| missing | the file is gone from disk; only the recorded checksum remains |
| no_chain | this archive keeps no chain yet; it starts with the next run |

A chat message, Planner card or To Do task is part of a larger file: the
file is checked, and `item_sha256` is the checksum of the item's own words.

When `get_document` lists more than one entry in `versions`, the item
changed after it was archived: say which version a quote comes from.

## Citing and handing over

Every line carries the item's cite.label, and cite.link when not null (null:
Munimentum is not running – say once that the links work when it runs).
Answer in the user's language.

To hand the evidence to someone, suggest filing the items into a case
(`munimentum-case`) and using *Export case…* in Munimentum: one ZIP with
the originals, `SHA256SUMS.txt` and an `evidence/` folder anyone can check
without the app. *Close case* then records every original's checksum.
