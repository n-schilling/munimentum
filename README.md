# Office 365 Export

Export your Microsoft 365 data (Teams chats/channels, Outlook mail, calendar,
contacts) via Microsoft Graph — delegated access, no admin consent required —
and search the exports offline: as a static HTML page, through **Claude** (MCP),
or with a local RAG web UI.

**Just want to use it?** Grab the ready-made app from
[Releases](../../releases) — no Python, no `pip install`, nothing to set up. See
[Download and run](#download-and-run). Everything below describes the scripts it
is built from.

```
                   app.py  ── browser UI: token, export, search, schedule, MCP
                     │
teams_export.py  ─┐  │                     ┌─ *_search.py      → static search.html
                  ├─ local export folders ─┤
outlook_export.py ┘                        └─ rag_index.py → rag_store/
                                                ├─ mcp_server.py → Claude (MCP tools)
                                                └─ rag_server.py → RAG web UI (Ollama)
```

| Script | Purpose |
|---|---|
| `app.py` | Browser UI that drives everything below (start here) |
| `teams_export.py` | Teams 1:1/group/meeting chats and channels → HTML |
| `outlook_export.py` | Mail (`.eml`), calendar (`.ics`), contacts (`.vcf`) |
| `combined_search.py` | Self-contained offline search page (Teams + Mail + calendar + contacts) |
| `rag_index.py` | Builds the search index (`rag_store/`: SQLite + FTS5 + embeddings) |
| `mcp_server.py` | MCP server — Claude searches and reads the exports itself |
| `rag_server.py` | Local RAG web UI with AI answers (fully offline via Ollama) |
| `corpus.py` | Shared export parsing (used internally) |
| `settings.py` | `app_config.json` as a defaults layer for every script (used internally) |
| `packaging/` | PyInstaller spec + smoke test for the downloadable bundles |

Everything runs on macOS, Windows and Linux. The prebuilt app needs nothing
installed; from source it needs Python 3.11+ (CI tests 3.11 and 3.13). Commands
below use `python3`; on Windows type `python` instead.

---

## Download and run

Prebuilt bundles for macOS, Windows and Linux are attached to every
[release](../../releases). They contain their own Python and every dependency —
download, unpack, double-click, done. The UI opens in your default browser.

| File | For |
|---|---|
| `Microsoft365-Archiv-macos-arm64.zip` | Mac with Apple Silicon (M1–M4) |
| `Microsoft365-Archiv-macos-x86_64.zip` | Mac with an Intel CPU |
| `Microsoft365-Archiv-windows-x64.zip` | Windows 10/11, 64-bit |
| `Microsoft365-Archiv-linux-x64.tar.gz` | Linux, 64-bit (glibc 2.35+) |

The bundles are **not code-signed** — certificates from Apple and Microsoft cost
money — so both systems warn once on first launch. On macOS: open it, dismiss the
warning, then System Settings → *Privacy & Security* → *Open Anyway* (or
`xattr -dr com.apple.quarantine "/Applications/Microsoft365-Archiv.app"`). On
Windows: *More info* → *Run anyway*. Running from source avoids this entirely and
behaves identically.

Data does **not** live inside the app, so updates never touch it:
`~/Library/Application Support/Microsoft365-Archiv` (macOS),
`%LOCALAPPDATA%\Microsoft365-Archiv` (Windows),
`~/.local/share/Microsoft365-Archiv` (Linux). The path is shown in the Export
tab. A mailbox can run to tens of gigabytes — for another disk, start with
`--data-dir FOLDER` or set `OFFICE365_DATA_DIR`.

Ollama stays optional: without it everything works except *semantic* search.

---

## The app — everything in one window

```bash
python3 app.py                         # → opens http://127.0.0.1:8700 in your browser
```

The same thing the bundles run, straight from the source tree. One command, one
window, no terminal work afterwards. The UI is German (like the search page); the
underlying scripts are unchanged and still work on their own.

**Token assistant.** The app never signs you in — you fetch the access token
yourself in the Graph Explorer and paste it in. It reads the token's `exp` and
`scp` claims, so it knows what it has: a still-valid token is left alone and the
assistant stays shut (how long a token lasts is up to your tenant — an hour for
some, most of a day for others). It opens only when there is no token, when the
stored one has expired, or when a run just died on a dead token. Either way the
header pill and the log line at startup say which account the token belongs to
and how much longer it lasts.

When it does open, it links the Graph Explorer, names exactly the permissions
your current selection needs — plus the query that makes each one appear under
*Modify permissions*, which only ever lists permissions for the request
currently in the address bar — and takes the pasted token with or without
`Bearer`, stripping line breaks and quotes. It lands in `gx_token.txt`, readable
only by you. A permission you already hold in a wider form counts: `Mail.ReadWrite`
satisfies `Mail.Read`.

**Export.** Tick what you want; the app passes the selection to the export
scripts through `EXPORT_CATEGORIES`, so they run without a single prompt. Output
streams into the log panel live. Both exports stay resumable — a second run only
fetches what is new.

**Search** is built in and uses the same ranking as the MCP server (BM25 and
embeddings fused with RRF) — `mcp_server.py` is imported as a library rather than
a second search path being maintained. Hits link to their source file, served
with `Content-Security-Policy: sandbox` so an exported Teams page cannot script
against the app.

**Calendar and address book.** The app has the same three calendar views as the
static page — week, month and **Rekonstruiert** — plus the contacts view, and it
gets them from the same code: `combined_search.py --json` runs its readers and
`reconstruct_events()` and writes the result to `rag_store/calendar.json`, which
the app serves. So deleted appointments recovered from invitation, reply and
cancellation mails (see section 4 for how that works, Exchange Global Object IDs
and all) exist once in this project, not twice in slightly different forms.

That step reads every `.eml` to find the invitations, so it takes minutes on a
large mailbox — it is therefore its own pipeline step with a result file, run
after each Outlook export, on the schedule, or from the *Kalender & Kontakte
aufbauen* button. The payload drops `uid` everywhere and the person/description
fields on everything except the reconstructed events (nothing reads them there),
which takes it from 11 MB to under 5 MB, and it goes over the wire gzipped at
roughly 0.75 MB.

**Schedule.** While the app is open, it can re-run export and indexing at a fixed
interval. Deliberately bound to the app's runtime and not to launchd/Task
Scheduler: the schedule only reaches as far as the hand-fetched token stays
valid, and a background service would mostly produce expired-token failures that
nobody sees. When a run does hit an expired token, the app notices, skips the
next one and reopens the assistant.

**No Ollama?** The app checks at start and shows an assistant with the install
steps for your OS. You can also just continue: the MCP server still starts (it
falls back to BM25 by itself) and indexing is skipped — or built as a pure
full-text index via `rag_index.py --no-embeddings`, which keeps search and MCP
working immediately. Embeddings can be added later at any time; a lexical
rebuild sets existing ones aside by content hash instead of discarding them.

**MCP.** Start/stop `mcp_server.py` from the UI and copy the config snippet for
Claude Code or Claude Desktop. Quitting the app also shuts the MCP server down.

**Settings.** The *Einstellungen* tab exposes every option the individual scripts
have — the Teams image and channel switches, Outlook's hidden folders and its
skipped-folder list, parallelism, embedding batch size, Ollama URL and model, MCP
port and autostart, and the three folder paths. Nothing there is app-only: the
app writes `app_config.json` and turns it into the same environment variables and
command-line flags the scripts take on their own, so a setting changed here and
one exported in a shell do exactly the same thing. Numbers are clamped on save
(workers 1–8, MCP port 1024–65535, batch 1–512) so a typo can't wedge the next
run, and changes apply to the next run — never to what is already exported.

Options: `--port 8700` (busy ports are skipped automatically), `--no-browser`,
`--data-dir FOLDER`. Settings live in `app_config.json` next to the data.

<details>
<summary>Building the bundles yourself</summary>

```bash
pip install -r requirements-build.txt
pyinstaller packaging/app.spec --noconfirm
python3 packaging/smoke_test.py dist/Microsoft365-Archiv/Microsoft365-Archiv
```

[`.github/workflows/build.yml`](.github/workflows/build.yml) does exactly this on
macOS (arm64 + Intel), Windows and Linux for every push to `main`; a `v*` tag
additionally publishes a release. Every bundle has to pass
[`packaging/smoke_test.py`](packaging/smoke_test.py) first — it starts the app,
builds an index (for which the bundle launches *itself* as a subprocess), searches
it and starts the MCP server. A bundle with a missing dependency therefore never
reaches a release.

Inside a bundle there is no interpreter and there are no `.py` files, so `app.py`
calls the sub-programs through its own executable: `Microsoft365-Archiv --run
rag_index …` imports the module from the bundle and hands it the arguments. That
is also what the Claude Desktop snippet in the MCP tab points at.

</details>

> ⚠️ Same rule as the MCP server: the app binds to `127.0.0.1`, has no
> authentication and serves your whole mail and chat history. It validates the
> `Host` header so a web page you happen to visit cannot reach it through your
> browser (DNS rebinding), and it has no option to bind anything else.

---

## 1. Setup

Create a virtual environment and install what you need:

```bash
python3 -m venv .venv
source .venv/bin/activate                      # Windows PowerShell: .\.venv\Scripts\Activate.ps1
python3 -m pip install -r requirements.txt     # everything, exact versions
```

The static search tools need no packages at all. If you only want the export
tools, `msal` and `requests` are enough — `mcp` and `numpy` are for the MCP
server and the AI search.

Versions are pinned in [`requirements.txt`](requirements.txt) so an upstream
release cannot break the tools unannounced; `requirements-dev.txt` adds the
test and lint tools and is what CI installs. The pins stay on the newest
release that still runs on Python 3.11 — `numpy` 2.5 would raise that floor to
3.12, so it is held at 2.4.x.

> PowerShell blocks the activation script? Run once:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

<details>
<summary>Windows without install rights: standalone (embeddable) Python</summary>

1. Download the **"Windows embeddable package (64-bit)"** from
   [python.org/downloads/windows](https://www.python.org/downloads/windows/) and
   unzip it, e.g. to `C:\python-standalone`.
2. In that folder, open `python3XX._pth` in a text editor and remove the `#`
   before `import site`.
3. Bootstrap pip and install the packages (PowerShell, in that folder):

   ```powershell
   Invoke-WebRequest https://bootstrap.pypa.io/get-pip.py -OutFile get-pip.py
   .\python.exe get-pip.py
   .\python.exe -m pip install msal requests
   ```

Then run every command with the full path, e.g.
`C:\python-standalone\python.exe teams_export.py`. No virtual environment
needed — packages install into the standalone folder itself.
</details>

---

## 2. Authentication

The export scripts sign you in interactively (a browser window opens). If your
tenant requires admin approval for new apps, paste a token instead:

1. Log in at the [Microsoft Graph Explorer](https://developer.microsoft.com/en-us/graph/graph-explorer)
   and copy the token from the **"Access token"** tab.
2. Save it as `gx_token.txt` next to the scripts, or set it as the `GRAPH_TOKEN`
   environment variable.

Token mode needs the right scopes consented in Graph Explorer:
`Chat.Read` / `ChannelMessage.Read.All` (Teams), `Mail.Read`, plus
`Calendars.Read` / `Contacts.Read` for calendar/contacts.

---

## 3. Export

```bash
python3 teams_export.py                # → teams_export/
python3 outlook_export.py              # → outlook_export/
```

Both tools are interactive (they ask what to export) and **resumable** —
re-running only fetches new/changed items. Deleting the folder forces a full
re-export.

**Common variations:**

```bash
python3 teams_export.py my_archive     # custom output folder
python3 outlook_export.py -default     # no questions, default selection
                                       #   (ideal for cron/scheduled runs)
```

`-default` exports: Teams — 1:1, group and meeting chats (no channels);
Outlook — mail (except Archive, Drafts, Deleted Items, Junk, Outbox), the
default calendar and all contacts. Same as pressing Enter at every prompt.

**Options** (environment variables, e.g. `EXPORT_WORKERS=2 python3 teams_export.py`):

| Variable | Applies to | Default | What it does |
|---|---|---|---|
| `EXPORT_WORKERS` | both | `4` | Parallel downloads. `4` is the sensible max (throttling); use `1` on flaky connections. |
| `GRAPH_TOKEN` | both | — | Pasted Graph token instead of browser login (section 2). |
| `EXPORT_CATEGORIES` | both | — | Pick categories without any prompt — Teams: `1on1,group,meeting,channels`; Outlook: `mail,calendar,contacts`. Wins over `-default`. This is how `app.py` and its scheduler drive the exports; equally useful for cron. |
| `EMBED_IMAGES` | Teams | `1` | `0` = don't embed inline images as base64 (much smaller HTML, no images). |
| `CACHE_IMAGES` | Teams | `1` | `0` = don't cache inline images (saves disk, slower re-export). |
| `REFRESH_CHANNELS` | Teams | `1` | `0` = don't re-check exported channels for new replies. |
| `SKIP_EMPTY_CHATS` | Teams | `1` | `0` = also export chats with only system messages. |
| `INCLUDE_HIDDEN` | Outlook | `0` | `1` = also export hidden system folders (Conversation History, Sync Issues …). |
| `SKIP_FOLDERS` | Outlook | see below | Comma-separated folders the default selection leaves out, compared case-insensitively. Set it empty to export every folder; unset it to keep the built-in list (Archive, Drafts, Deleted Items, Junk, Outbox and their German names). |

Flags accept `0/false/no/nein/off/empty` for off and anything else for on.

**These are not the only source.** Every one of them is also a control in the
app's **Einstellungen** tab, and `app_config.json` counts for direct script runs
too — `python3 outlook_export.py` picks up what you clicked in the app. The
order is:

```
environment variable   >   app_config.json   >   built-in default
```

The environment stays on top so a single run can override the file
(`INCLUDE_HIDDEN=0 python3 outlook_export.py`), and so runs started by the app —
which passes everything as environment variables — stay unambiguous. A script
that took a value from the file says so in one line at startup, and prints its
effective output folder, so nothing changes behind your back.

The file is looked up in `OFFICE365_DATA_DIR`, otherwise next to the scripts; a
missing or broken one is simply ignored. `rag_index.py`, `mcp_server.py` and
`combined_search.py` take their defaults from it as well (store, model, Ollama
URL, batch, port, folders), with command-line flags winning as usual. What it
deliberately does **not** supply is *what* to export — running a script directly
should still ask, otherwise the interactive mode is gone. See
[`settings.py`](settings.py).

`USE_DEVICE_CODE = True` at the top of either script switches the browser login
for a device code (headless machines). It is the one switch the app does not
expose, because the app never signs in — it works from a pasted token only.

---

## 4. Static search page (offline, no install)

`combined_search.py` reads both export folders (either may be missing) and
writes one self-contained search page with person, date and source filters:

```bash
python3 combined_search.py             # → combined_search.html
```

The page has three tabs: **Suche** (full-text search), **Kalender** (week and
month view of the exported `.ics` events, colour-coded by status — confirmed,
tentative, cancelled) and **Adressbuch** (contacts from the `.vcf` files with
mail/phone links). Every entry links to its source file.

**Deleted appointments are recovered from the mailbox.** Meeting invitations,
replies and cancellations carry the full event (including its UID) in a
`text/calendar` part. If that UID is missing from the calendar export, the event
was removed from the calendar — it is rebuilt from the mail and shown dashed, as
*Gelöscht* when a cancellation (`METHOD:CANCEL`) exists, otherwise as *Nicht im
Kalender* (invited/accepted but no longer there). Such entries link to the mail
they were rebuilt from. A cancellation also fixes the status of an event that is
still in the calendar but not marked as cancelled there.

The calendar has a third mode next to *Woche* and *Monat*: **Rekonstruiert**,
a chronological list of exactly those rebuilt events —
filterable by *Gelöscht* / *Nicht im Kalender* and searchable by title, person
or content. Each row links to the mail it was rebuilt from.

Reply mails carry no `ORGANIZER`, only the responding attendee — but a reply
always goes *to* the organizer, so the recipient is used instead (verified
against the calendar export: 1442 of 1456 replies match, 99%). Invitations and
cancellations come from the organizer, so there the sender is used.

Matching mail to calendar needs two guards, both worth knowing about:

* Exchange wraps foreign UIDs (Google, Zoom, …) into its own Global Object ID —
  a hex blob that carries the original UID after a `vCal-Uid` marker. It is
  unwrapped before comparing, otherwise those events would all look deleted.
* If an event with the same title and start minute is still in the calendar, the
  reconstruction is dropped as a duplicate. This catches ID formats that the
  unwrapping does not know about, at the price of hiding a deleted event that
  starts at exactly the same minute as a surviving one with the same title.

Invitations carry Windows time-zone names (`W. Europe Standard Time`); the
common ones are mapped to IANA zones. An unknown name falls back to local time,
as does a missing time zone.

It accepts custom folders (`[teams] [outlook] [-o out.html]`) and writes the
page to the common parent folder of both exports — don't move it afterwards,
the links are relative.

`--json datei.json` writes only this analysis — calendar entries, reconstructed
appointments and contacts — as data, without building a page. That is what
`app.py` uses for its own calendar and address book, so the reconstruction above
lives in exactly one place.

---

## 5. Search index (needed for MCP and RAG UI)

The index lives in `rag_store/`: `corpus.db` (SQLite with an FTS5 full-text
index) and `vectors.npy` (float16 embeddings, built with
[Ollama](https://ollama.com)).

```bash
ollama pull bge-m3                     # embedding model, multilingual (DE/EN)
python3 rag_index.py teams_export outlook_export
```

The build is **incremental** — re-run it after each export; only new/changed
content is re-embedded.

**Without Ollama:** `--no-embeddings` writes only `corpus.db` with its FTS5
index. Search and the MCP server then rank purely lexically (BM25) — the
semantic half is missing, nothing else. Existing embeddings are not thrown away:
because `vectors.npy` is tied row-by-row to `corpus.db`, a lexical rebuild would
break that pairing, so they are set aside hash-indexed in `vectors_stale.npz`
first. A later run with Ollama picks them straight back up and only embeds what
is genuinely new.

```bash
python3 rag_index.py --no-embeddings   # no Ollama needed
```

> Store built before the `ix_chunks_msg_ts` index was added? `browse_messages`
> then scans the whole table (~48 ms at 270k chunks instead of ~0.2 ms). The
> next `rag_index.py` run creates it; to add it in place instead, without
> re-embedding anything:
>
> ```bash
> sqlite3 rag_store/corpus.db \
>   'CREATE INDEX IF NOT EXISTS ix_chunks_msg_ts ON chunks(ts DESC) WHERE seq = 0;'
> ```

---

## 6. MCP server — search with Claude

`mcp_server.py` exposes the exports to Claude (Claude Code / Claude Desktop) as
[MCP](https://modelcontextprotocol.io) tools — Claude searches, reads sources
and answers with citations; no local answer model needed.

**Ranking** is hybrid by default: FTS5/BM25 and semantic cosine search merged
with Reciprocal Rank Fusion — exact tokens (invoice numbers, names) and
paraphrases both hit. If Ollama is down (or `numpy` is missing) it falls back
to pure BM25 automatically.

**Run it** (leave it running; one instance serves all Claude sessions):

```bash
python3 mcp_server.py                  # endpoint: http://127.0.0.1:8365/mcp
```

> ⚠️ The server has **no authentication** and serves your complete mail and
> chat history. Keep it on `127.0.0.1` (the default).
>
> On loopback the Host and Origin headers are validated, so a web page you
> happen to visit cannot reach the server through your browser (DNS
> rebinding). That protection does not extend to other bind addresses, so
> binding one means naming the hostnames clients will use — otherwise the
> server refuses to start:
>
> ```bash
> python3 mcp_server.py --host 0.0.0.0 --allowed-host nas.local
> ```

**Register in Claude Code** — this repo's `.mcp.json` already does it:

```json
{"mcpServers": {"office365-export": {"type": "http", "url": "http://127.0.0.1:8365/mcp"}}}
```

**Register in Claude Desktop** — `claude_desktop_config.json` only accepts
`command` entries, so bridge the HTTP endpoint with
[mcp-proxy](https://github.com/sparfenyuk/mcp-proxy):

```json
{"mcpServers": {"office365-export": {
  "command": "uvx",
  "args": ["mcp-proxy", "--transport", "streamablehttp", "http://127.0.0.1:8365/mcp"]
}}}
```

Prefer the classic auto-launched setup instead of a shared server? Register a
`command`-based entry running `python3 mcp_server.py --transport stdio`.

**Tools:** `search_messages` (hybrid search; person/date/source filters,
pagination), `browse_messages` (filtered listing, newest first), `get_document`
(full message, optionally with neighboring chat messages), `list_people`
(who is in the corpus — valid `person` filter values), `read_source_file`
(raw `.eml`/conversation, windowed for large files), `corpus_stats`. Every hit
carries an `o365://` resource URI through which Claude can fetch the source
file. All tools are read-only.

Then just ask Claude: *"Search my Teams chats with Anna about the Q3 budget."*

---

## 7. RAG web UI — fully offline AI answers

The self-contained alternative to the MCP server: retrieval plus a local answer
model, no Claude involved.

```bash
ollama pull qwen2.5:14b-instruct       # answer model, fits well in 24 GB
python3 rag_server.py --teams teams_export --outlook outlook_export
```

Then open <http://localhost:8000> — semantic search with filters, or full
question answering with source citations.
