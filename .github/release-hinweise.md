## New in 13.0.0

One new thing to search with, and underneath it the local HTTP interface
finished: the app's own routes are gone, everything runs on `/api/v1`.
**The first run after the update rebuilds the search index once** — see
*Upgrading*. **If you have scripts of your own against the interface, read
*Upgrading* too**, because every path has changed.

**Who stood in which line.** The search has a **mail filter**: *From*,
*To*, *Cc* and *Bcc*, each offering the addresses the archive actually
holds. It is one pill with four fields, and it stands there only while the
source is *Mail* — a line only mail has would otherwise quietly turn
every search into a mail search. A hit's detail now shows *Bcc*
wherever it is there, so the detail and the search agree. Claude gets the
same four filters through MCP, and can ask for the addresses behind them.

**Claude can check whether the archive is worth concluding from.** Three
more MCP tools: the run history with what each step brought in, the log a
run wrote, and the completeness balance a source's last check found
against Microsoft. A search cannot tell you that a source's last run
failed — now Claude can ask before it answers from a gap.

**One surface.** Until now the page spoke one language and `/api/v1`
another. The app's own action routes — `/api/run`, `/api/faelle/anlegen`,
`/api/archiv/nachholen` and the rest of them — are gone; the page, the
profile chooser and the packaged smoke test ask nothing outside
`/api/v1`. Nothing about the app changes for you: the same screens, the
same buttons. What changes is that there is one description of it all,
`openapi.yaml`, and it is now complete — every route tagged twice, by its
door and by its HTTP method, so a viewer can list all `GET`s as easily as
one door's routes.

**Three reads ask with QUERY.** The AI answer, a source's folder plan and
the problem report are reads whose question is too big for a URL. They
used to be a `POST`, which said "this changes something" when it does not.
They now use `QUERY` ([RFC 10008](https://www.rfc-editor.org/info/rfc10008/)):
safe and repeatable like a `GET`, with the question in the body. Nothing
behind them stores anything.

**A refusal has one shape, and only one.** `message`, the second spelling
of the reason that 11.3 introduced and 12.0 announced as going away, is
gone. What is left is the problem detail: `type`, `title`, `status`,
`detail`, `instance`, plus `ok` and `error` with the text key.

## Upgrading

**Using the app:** the first run after the update rebuilds the search
index once, because the index learns who stood in *To*, *Cc* and *Bcc*.
It takes about as long as the first index run did; the exports themselves
are untouched, and nothing is deleted. Everything else is as it was — no
migration, no settings to redo, and the archive stays where it is.

From 10.x the index is rebuilt anyway — see the notes of
[11.0.0](https://github.com/n-schilling/munimentum/releases/tag/v11.0.0)
and [11.1.0](https://github.com/n-schilling/munimentum/releases/tag/v11.1.0).
From 9.x or older the first start moves the archive into
`profiles/standard/` — see
[10.0.0](https://github.com/n-schilling/munimentum/releases/tag/v10.0.0);
from 6.1 or older run the latest 6.x once first — see
[7.0.0](https://github.com/n-schilling/munimentum/releases/tag/v7.0.0).

**Scripting against the HTTP API:** every unversioned `/api/...` route is
gone. Each one has a successor under `/api/v1` where the method says what
happens, so a `POST` that only read is now a `GET`, and one that deleted
is a `DELETE`.

| Before | Now |
|---|---|
| `/api/status`, `POST /api/config` | `GET /api/v1/status`, `GET`/`PATCH /api/v1/config` |
| `/api/data-dir`, `/api/schedule` | `PATCH /api/v1/storage`, `PATCH /api/v1/schedule` |
| `/api/search`, `/api/similar`, `/api/people`, `/api/files`, `/api/filetypes`, `/api/folders`, `/api/calendar` | the same names under `/api/v1/` |
| `/api/detail`, `/api/document`, `/api/thread`, `/source` | `/api/v1/documents/facts`, `/api/v1/documents`, `/api/v1/threads`, `/api/v1/files/content` |
| `/api/suche/*` | `/api/v1/searches/history` and `/api/v1/searches/saved` with real methods |
| `/api/faelle/*` (23 routes) | `/api/v1/cases` and its collections `items`, `folders`, `notes`, `lists` |
| `/api/run`, `/api/cancel`, `/api/log`, `/api/run-log` | `POST /api/v1/runs`, `DELETE /api/v1/runs/current`, `GET /api/v1/log`, `GET /api/v1/runs/{id}/log` |
| `/api/archiv/*` | `/api/v1/sources/{source}/` + `findings`, `refetch`, `rebuild`, `open` |
| `/api/bilanz/holen`, `/api/sharepoint-report` | `POST /api/v1/balance/{row}/fetch`; a row's last report is `GET /api/v1/balance/{row}` – the SharePoint report is `GET /api/v1/balance/sharepoint` |
| `/api/token`, `/api/login`, `/api/logout`, `/api/wizard-seen` | `PUT /api/v1/access/token`, `POST`/`DELETE /api/v1/access/session`, `DELETE /api/v1/access/notice` |
| `/api/profile-open`, `-switch`, `-prefs`, `-rename` | `POST /api/v1/profiles/{name}/open`, `PATCH /api/v1/profiles`, `PATCH /api/v1/profiles/{name}` |
| `/api/mcp`, `/api/ollama-recheck`, `/api/update-check` | `PATCH /api/v1/mcp`, `POST /api/v1/ollama/recheck`, `POST /api/v1/updates/check` |
| `/api/answer`, `/api/folder-plan`, `/api/report` | `QUERY /api/v1/answer`, `QUERY /api/v1/sources/{source}/folder-plan` (the calendars: `outlook` with `unit: calendar`), `QUERY /api/v1/reports` |
| `/api/analytics`, `/api/analytics-refresh`, `/api/quit`, `/api/openapi` | the same names under `/api/v1/` (`POST /api/v1/analytics/refresh`) |

A few more things for a script:

* A `QUERY` needs a body and a `Content-Type` — without either it is a
  `400`, because the body is the question. `curl -X QUERY -H
  'Content-Type: application/json' -d '{"q":"…"}'`. Those routes announce
  themselves with `Accept-Query` in their `OPTIONS` answer.
* `message` is gone from refusals; `error.k` still carries the text key.
  A few successful answers keep one, where the sentence is the point.
* Everything a `Location` names can be fetched: the run that was started
  (`GET /api/v1/runs/current`), and each folder, note, stored list and
  saved search a create answered with.
* The collections that are capped rather than paged – folders, file types,
  people, addresses, a conversation, the runs – now answer the `limit`
  they were built with and say with `has_more` whether the cap cut
  something off. Only `/api/v1/search` pages.
* `store` – whether an index exists, what it can do (`features`),
  `built_at` – left the status for `GET /api/v1/inventory`, next to the
  sources' counts: it changes with a run, not with every poll. The
  mail lines show there as one word, `mail_lines`.
* Two names changed on the way: `/api/v1/log` and `/api/v1/runs/{id}/log`
  answer `items` where `/api/log` and `/api/run-log` said `lines`, and
  `/api/v1/similar` takes `limit` where `/api/similar` took `k`.
* Under `/api/v1` nothing was removed, and two answers of 12.0 changed
  their spelling – the last German words there, and one collision. The
  case marks on a search hit (`cases`) say `folder` and `status:
  open|closed` now, not `ordner` and `offen|zu`. And in the `criteria`
  of a stored list, a saved search or a history entry the case's folder
  is `case_folder`: 12.0 wrote it as `folder`, the mailbox folder's key,
  so one overwrote the other – and a saved search's own folder is
  `case_folder` too, the name a `POST` or `PATCH` takes, where 12.0
  answered `folder`. A 12.0 script reading any of these must change;
  everything else under `/api/v1` is as it was. The
  description is `openapi.yaml`, served at
  `GET /api/v1/openapi`, and it is an OpenAPI 3.2 file now — 3.2 is the
  first version with a field for `QUERY` — and it uses the rest of what
  that version brought: the AI answer's stream is typed line by line
  (`itemSchema`), an item's facts are one schema per kind behind a
  discriminator, every refusal points at one reusable media type, the
  tags say whether they group (`kind: nav`) or label (`kind: badge`), so
  a generator builds four clients rather than ten, and `source` shows
  both the value and how it looks on the wire.

## Which file?

| File | For |
|---|---|
| `Munimentum-macos-arm64.dmg` | Mac with Apple Silicon (M1 or newer) |
| `Munimentum-windows-x64.zip` | Windows 10/11 (64-bit) |
| `Munimentum-linux-x64.tar.gz` | Linux (64-bit, glibc 2.35+) |


## Getting started

**macOS** — double-click the DMG, drag `Munimentum.app` onto the
*Applications* folder shown next to it, close (eject) the window, and start the
app from *Applications*.

**Windows** — unpack the ZIP (right-click → *Extract All*, not just looking
inside), then double-click `Munimentum.exe` in the extracted folder.

**Linux** — `tar -xzf Munimentum-linux-x64.tar.gz`, then run
`./Munimentum/Munimentum`.

The interface then opens by itself in your default browser. Everything else —
fetching a token, exporting, searching — is explained there.

## “Windows protected your PC”

**macOS is signed and notarized by Apple** — it opens on a double-click, with
no warning and nothing to click past.

**Windows is not code-signed.** A certificate costs considerably more there, and
SmartScreen additionally wants to see a download count before it goes quiet. So
on first launch you get the blue window: click *“More info”*, then *“Run
anyway”*. It happens once.

If you would rather not, run it from source instead (`python3 app.py`, see the
README) — the function and the result are identical.

## Checksums

`SHA256SUMS.txt` is attached. Verify with `shasum -a 256 -c SHA256SUMS.txt`
(macOS/Linux) or `Get-FileHash file.zip` (PowerShell).

What comes next is in `ROADMAP.md`.
