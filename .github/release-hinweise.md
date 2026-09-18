## New in 12.0.0

Nothing to do when you upgrade: no migration, no rebuild, the archive and
the settings stay as they are. In the app itself one thing moved — access
to your Microsoft 365 account is a settings card now instead of a window.
Everything else happened behind it, in the local HTTP interface, and
thoroughly enough to warrant the major number. **If you have scripts of
your own against it, read *Upgrading* below.**

**The API answers the same way everywhere.** Every refusal now has one
shape and a status code that says what happened. The body is a problem
detail as RFC 9457 describes it — what went wrong, as a type, a title, the
status and an English sentence — plus the text key the page turns into
your language. What used to come back as “HTTP 200 plus an error field” —
a search without an index, an unknown item, a case that is not there — now
answers `503`, `404` or `409`, and the two answers that were plain German
text are JSON like everything else.

**And it behaves like an API.** A method a route does not serve is
answered `405` with `Allow` instead of “no such route”, `OPTIONS` says
what a path allows, a body has to be JSON and has to say so, a number that
is not one is a `400` rather than a `500`, and a `503` carries
`Retry-After`. Every answer names the program in `X-Munimentum-Version`
and the contract in `X-Munimentum-Api`; *Settings › App* shows both next
to the build.

**A versioned surface.** Alongside the app's own routes there is now
`/api/v1`, for everyone who is not the page: resources instead of actions,
the method says what happens, English names throughout. It carries cases
and the search so far and grows per release — its version is the
contract's, not the program's, so the path changes only when the shape
breaks. *Explore archive* already reads through it entirely — the search,
the files, the folders, the file types, the people, a conversation, an
item's facts and the calendar.

**The page asks less often, and asks for less.** While a run is going it
follows as closely as before; idle it asks every thirty seconds instead of
every two and a half, and *Settings › Expert mode* sets that interval. A
tab in the background asks nothing at all, and the log is fetched only
while the run window is open — it is shown nowhere else. The polled answer
now carries only what changes on its own.
Three things that do not have moved out: the settings (`/api/v1/config`,
read with `GET`, changed with `PATCH`), the paths and defaults
(`/api/v1/app`) and what the sources hold, folder names and all
(`/api/v1/inventory`). What is left was then gone through node by node:
every field that nothing in the interface acts on is gone, and everything
that the settings already say — which models Ollama should use, the MCP
port, the sign-in mode, an own registration's id — is not repeated. What
can be decided in the app is decided there and travels as one answer,
such as whether the two models are available. What a header already says
is not repeated in the body either. That is a status of 0.9 KB instead of
8, and six requests in twenty seconds instead of twenty-eight — four of
those six only once, at load. The app binds to `127.0.0.1`, but that is no reason for an
answer to repeat the names of someone's mail folders, the list of their
local models or the path to their home directory every few seconds.

**Access is a setting.** Getting the app to your Microsoft 365 account —
pasting a key or signing in — used to be the one setting that opened a
window over the page. It is now a card at the top of the settings, called
*Microsoft Access* in the navigation beside it, and everything that led
into that window leads to the card instead: the frame in the header, step
one of the archive door, the entry in the navigation. Nothing pops up on
its own any more; the dot next to the entry says when it wants attention.

**One place for a refusal.** When something is refused, the app no longer
opens a browser dialog: the reason appears in the page's own message, in
the error colour, and stays until it is read away.

## Upgrading

**Using the app:** nothing to do, from any 11.x. From 10.x the next run
rebuilds the index once — see the notes of
[11.0.0](https://github.com/n-schilling/munimentum/releases/tag/v11.0.0)
and [11.1.0](https://github.com/n-schilling/munimentum/releases/tag/v11.1.0).
From 9.x or older the first start moves the archive into
`profiles/standard/` — see
[10.0.0](https://github.com/n-schilling/munimentum/releases/tag/v10.0.0);
from 6.1 or older run the latest 6.x once first — see
[7.0.0](https://github.com/n-schilling/munimentum/releases/tag/v7.0.0).

**Scripting against the HTTP API:** four things changed for you.

| What | Before | Now |
|---|---|---|
| A refusal | `{"ok": false, "message": …}` or `{"error": …}` | one problem detail, `error.k` still carries the text key |
| No index, unknown item, unknown case | `200` with an error field | `503`, `404`, `409` |
| `POST` without `Content-Type: application/json` | accepted | `415` — `curl -d` sets a form type, so pass the header |
| Wrong method, unparseable number | `404`, `500` | `405` with `Allow`, `400` |
| The settings | in every `/api/status` | `GET /api/v1/config`, changed with `PATCH` |
| Paths, defaults, the Claude snippet | in every `/api/status` | `GET /api/v1/app` |
| Folders, calendars, lists, notebooks, export state | in every `/api/status` | `GET /api/v1/inventory` |

Every success answer is unchanged. `message` is still sent beside the new
fields and goes away with 13.0. The full description, including
`/api/v1`, is `openapi.yaml`, served at `/api/openapi`.

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
