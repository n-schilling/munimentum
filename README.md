# Office 365 Export

Export your Microsoft 365 data — Teams chats and channels, Outlook mail,
calendar, contacts — via Microsoft Graph, and search it offline: in the app, in
a single portable HTML page, or through **Claude** (MCP).

Delegated access, no admin consent required. Nothing leaves your machine except
the calls to Microsoft Graph.

**Just want to use it?** Grab the ready-made app from
[Releases](../../releases) — no Python, nothing to install. Everything below the
next section describes the scripts it is built from.

---

## Download and run

Bundles for macOS, Windows and Linux are attached to every
[release](../../releases). They contain their own Python and every dependency —
no installation. The UI opens in your default browser.

| File | For |
|---|---|
| `Microsoft365-Archiv-macos-arm64.dmg` | Mac with Apple Silicon |
| `Microsoft365-Archiv-macos-x86_64.dmg` | Mac with an Intel CPU |
| `Microsoft365-Archiv-windows-x64.zip` | Windows 10/11, 64-bit |
| `Microsoft365-Archiv-linux-x64.tar.gz` | Linux, 64-bit (glibc 2.35+) |

On macOS, open the disk image and drag the app to *Applications*; on Windows
and Linux, unpack the archive.

The bundles are **not code-signed**, so both systems warn on first launch. The
release notes walk through it — on macOS: *Done*, then System Settings →
Privacy & Security → *Open Anyway*.

Your data lives in your user folder, not in the app, so an update overwrites
nothing. The path is shown in the UI and can be changed in *Settings*.

---

## What the app does

One browser page with three tabs.

**Export data** — pick what to fetch (mail, calendar, contacts; 1:1, group,
meeting chats, team channels; OneDrive files) and start. Every run fetches only what is new. A
schedule can repeat it while the app is open, and index straight afterwards.

**Search data** — full-text and, with [Ollama](https://ollama.com), semantic
search over everything exported, merged into one ranking. Optional extras: an
AI summary of the hits with source numbers, the whole mail thread under a hit,
and a *Deleted only* filter for messages that are no longer in the mailbox but
still in your archive. Two more views sit here: a calendar (including
appointments recovered from invitation and cancellation mails) and an address
book.

**Settings** — everything the scripts can do: folders, categories, models,
schedule, MCP server, language (German, English, French). Which folders get
exported is a list of include/exclude rules, and *Show export list* spells out
what they currently mean: what comes along, what is left out and why, and what
is only in your archive because it is gone from the mailbox.

### Signing in

Two ways, chosen in the assistant. Pasting a key stays the default.

| | Needs | Lasts |
|---|---|---|
| **Access key** | nothing but the [Graph Explorer](https://developer.microsoft.com/en-us/graph/graph-explorer) | a few hours, then paste again |
| **Sign in** | one sign-in, optionally your own app registration | weeks — the schedule keeps running unattended |

Signing in stores a refresh token in `msal_cache.bin` (mode `0600`); *Sign out*
deletes it. Your password is never seen. Without your own registration it uses
Microsoft's public `Graph Command Line Tools` application, which is pre-approved
in almost every tenant.

---

## From source

Python 3.12 or newer (CI tests 3.12 and 3.13). On Windows type `python`
instead of `python3`.

```bash
python3 -m venv .venv
source .venv/bin/activate          # PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python3 app.py                     # the UI — drives everything below
```

The static search page needs no packages at all; the export scripts need only
`msal` and `requests`.

### The scripts

Each one runs on its own, without the app:

| Script | Purpose |
|---|---|
| `app.py` | Browser UI that drives everything else (start here) |
| `teams_export.py` | Teams 1:1/group/meeting chats and channels → HTML |
| `outlook_export.py` | Mail (`.eml`), calendar (`.ics`), contacts (`.vcf`) |
| `onedrive_export.py` | OneDrive as a local mirror (delta-based, keeps deleted files) |
| `combined_search.py` | Self-contained offline search page, and the calendar/contacts data the app shows |
| `rag_index.py` | Builds the search index (`rag_store/`: SQLite + FTS5 + embeddings) |
| `mcp_server.py` | MCP server — Claude searches and reads the exports itself |
| `rag_server.py` | Local web UI with AI answers, fully offline via Ollama |
| `auth.py` | Signing in — pasted key or MSAL login, shared by every script |
| `folders.py` | The mailbox folder tree and the include/exclude rules |
| `corpus.py` | Shared export parsing |
| `settings.py` | `app_config.json` as a defaults layer for every script |
| `answer.py` | Prompt and streaming for the Ollama summary |
| `progress.py` | Machine-readable progress, for the progress bar |
| `i18n.py`, `lang/` | UI translations |
| `packaging/` | PyInstaller spec, the smoke test that gates every release, and the macOS signing guide |

```bash
python3 teams_export.py            # asks what to export
python3 outlook_export.py --folders # sync the folder tree (once, then rarely)
python3 outlook_export.py -default # no questions, default selection
python3 outlook_export.py --check  # completeness against the mailbox
python3 onedrive_export.py         # mirror OneDrive (only what changed)
python3 onedrive_export.py --folders  # just refresh the folder tree
python3 onedrive_export.py --check    # completeness against the drive
python3 rag_index.py               # build the index
python3 combined_search.py         # → combined_search.html, opens anywhere
```

### Which folders get exported

Ordered rules on paths, **last match wins** — the `.gitignore` idea:

```
- E-Mail/Archiv/**
+ E-Mail/Archiv/Wichtig/**
```

`*` stays within one level, `**` reaches deeper. Set them in *Settings*, or via
`FOLDER_RULES`. With none set, the older folder list still applies, so an
upgrade changes nothing until you say so. The tree itself lives in
`folders.json` next to the export and is refreshed only when you ask —
see [`folders.py`](folders.py).

### Configuration

Every switch in the UI is also an environment variable, and `app_config.json`
carries what you clicked in the app over to a direct script run:

```
environment variable   >   app_config.json   >   built-in default
```

So `INCLUDE_HIDDEN=0 python3 outlook_export.py` overrides a single run, and a
script that took a value from the file says so at startup. The names and
defaults live in [`settings.py`](settings.py) and in each script's header —
that is the one place they cannot drift out of date.

The file is looked up in `OFFICE365_DATA_DIR`, otherwise next to the scripts; a
missing or broken one is ignored. What it deliberately does **not** supply is
*what* to export — running a script directly should still ask.

---

## Search with Claude (MCP)

`mcp_server.py` hands the exports to Claude Code or Claude Desktop as
[MCP](https://modelcontextprotocol.io) tools: Claude searches, reads sources and
answers with citations. Ranking is hybrid (BM25 + embeddings, merged with
Reciprocal Rank Fusion) and falls back to pure full-text if Ollama is away.

```bash
python3 mcp_server.py              # http://127.0.0.1:8365/mcp
```

> The server has **no authentication** and serves your complete mail and chat
> history. Keep it on `127.0.0.1` — the default. On loopback it validates the
> Host and Origin headers, so a web page you visit cannot reach it through your
> browser. Binding anything else means naming the hostnames clients will use
> (`--allowed-host nas.local`), otherwise it refuses to start.

Claude Code: this repo's `.mcp.json` already registers it. Claude Desktop only
accepts `command` entries — the app's *Settings* tab prints the exact snippet.

---

## Offline AI

With [Ollama](https://ollama.com) installed, two things become available: the
semantic half of the search, and an AI summary of the hits.

```bash
ollama pull bge-m3                 # embeddings for semantic search
ollama pull qwen3.6:27b            # answer model (~17 GB)
```

Everything runs on your machine. Without Ollama the export, the full-text search
and the MCP server work exactly as before — the app says so and offers to help
you install it.

The summary is built only from the hits already on screen, with numbered source
references. The prompt is short and lives in [`answer.py`](answer.py); read it
there rather than trusting a paraphrase here.

---

## Tests

```bash
pytest                             # ~840 tests
ruff check .
```

CI runs both on Python 3.12 and 3.13 with a coverage floor. Every release
bundle additionally goes through `packaging/smoke_test.py`, which starts the
built app, indexes, searches, builds the calendar and starts the MCP server —
a bundle with a missing dependency never reaches a release.
