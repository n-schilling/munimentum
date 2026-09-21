## New in 13.3.0

**A case that fills itself.** A saved search attached to a case can
now be switched to *automatic* – when you save it, or later in the
case. From then on every run that updates the index runs the search
and files what the case does not hold yet into the folder you chose,
marked *automatic* with the search that found it; *Collect now* on a
search does the same right away, as a run of its own. Only a text
search collects, and what you removed from the case stays out – the
look that *Check for new hits* gives leaves it out as well. A chip
above the case shows only what was collected.

**One dialog for a saved search.** Saving asks for name, case, the
folder it files into (when the case has folders – an existing one, or a
new one made on the spot) and the switch; *Edit* on a saved search opens
the same dialog later, under *Explore archive* and in the case alike.
Rename, attach and detach went into it; a row keeps *Run*, *Edit* and,
in the case, *Collect now*. *Check for new hits* is now *Show new hits*,
which is what it does.

**Scripts and Claude:** `auto` on a saved search (`POST` and `PATCH
/api/v1/searches/saved`), `POST /api/v1/cases/{id}/collect`, and the
MCP tool `collect_case` behind *Claude may change cases*; an item says
`via: auto` and which `search` filed it. The spec now says `202` for
the case export, which is what the server answered all along.

## Upgrading

**From 13.x:** nothing to do.

**From 12.x or older:** the first run after the update rebuilds the
search index once, and every script against the HTTP interface has to
move to `/api/v1` – both are spelled out in the notes of
[13.0.0](https://github.com/n-schilling/munimentum/releases/tag/v13.0.0).
From 9.x or older see
[10.0.0](https://github.com/n-schilling/munimentum/releases/tag/v10.0.0)
first.

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
