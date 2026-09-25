# Privacy

Munimentum has no backend. There is no account, no sign-up, no telemetry and no
analytics, and nothing you export ever reaches the author of this software.

## What leaves your machine

| To | Why | Optional |
|---|---|---|
| Microsoft Graph | fetching your own data — this *is* the export | no |
| `api.github.com` | one check at startup for a newer release | yes, *Settings* |
| your local Ollama | similar search, the AI answer, a name for a search you save | it never leaves the machine |
| the time-stamp service you name | after a run, the 32-byte checksum of the evidence chain (and of a closed case's list), to have it signed – no names, no contents | yes, off until you name one under *Settings › Evidence* |

That is the complete list. No crash reports, no usage statistics, no error
tracking, no fonts or scripts loaded from a CDN.

## What stays here

Settings, access token and run history live in a fixed folder in your user
profile; exports and the search index live below it by default and can each
point elsewhere — all paths are shown in *Settings*. While the app runs, an
`instance.json` in the profile's folder names the port it answers on, so a
citation Claude quotes can link into it; quitting removes it. Every archive is
a profile folder of its own below that one, with its own token, exports and
index; nothing crosses between profiles, and the only things outside them are
a small file naming the one opened last, the start-up log, and what the update
check remembers of GitHub's last answer so it need not ask again. Each
export's bookkeeping — change tokens, inventories, for Teams the message texts
the pages are rendered from — sits in a `state.db` inside its export folder,
nowhere else. With *Organization* ticked the archive also holds what the
directory says of your colleagues — names, titles, departments, offices, mail
addresses and who reports to whom — as one file below the Teams folder, every
earlier state of it kept; its `state.db` holds every account the directory
named, guests and disabled ones included, so a changed rule needs no second
reading. Beside the export folders lie two more of the archive's own:
`versions/` with the earlier version of every file a run replaced, and
`evidence/` with the chain of checksums the archive keeps of itself, the time
stamps a service signed and the list a closed case wrote down. The search
history, saved searches and cases sit in a `faelle.db` next to the profile's
settings: the history keeps a search's criteria — words, kind of search,
filters — never its hits, for as long as *Settings › App* says (or not at
all), and a case keeps pointers into the archive plus what a hit said when it
was added, the remarks you write on its items, and the keys of items you
removed by hand, so that a search set to collect automatically does not put
them back. The one thing the app writes outside its folders is a **case
export**, and only when you ask for one: a ZIP of copied originals under the
path you chose (your Documents folder by default) — from there on it is yours
to move, share or delete. The app serves its interface on `127.0.0.1` and is
not reachable from your network. System notifications are posted locally
through the operating system. Delete those folders and nothing of it remains.

## Two places worth knowing about

**Report a problem** builds a GitHub issue from the log and some system
details — optionally including the kind of your last interface actions (tab,
search, run; never content, kept only in the memory of the open page). The app
sends nothing: e-mail addresses and user names in paths are replaced, the
full text is shown for you to edit, and you submit the form yourself.
Folder names and subject lines are beyond what a pattern can catch, so read it
before posting.

**The MCP server** hands your archive — and what the app knows about it:
coverage, the run history with the logs those runs wrote, the completeness
balance against Microsoft, the analytics figures, your cases and saved
searches, the earlier versions of items and their checksums, the organization
in every version the archive holds — to Claude
on `127.0.0.1`. It reads; it changes a case only when
*Claude may change cases* is on under *Settings › Claude (MCP)*, and then
only adds. What Claude does with the passages it reads is governed by your
agreement with Anthropic, not by this app.
