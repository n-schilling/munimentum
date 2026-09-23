## New in 13.7.1

- **Teams files** – a link to a SharePoint page is no file and is no
  longer fetched: the Teams card has *Never these extensions*, preset to
  `aspx`, for both file switches; what it leaves out keeps its online
  link. A file Microsoft will not hand out is noted once and asked for
  again only by a full sync, instead of failing on every run.
- **Archive and bookkeeping** – the app's own bookkeeping next to an
  export (the calendar's `kalender.db` among it) is no longer reported
  as changed outside the app.

## Upgrading

**From 13.6.x or older:** the first run that exports begins the chain of
checksums and reads every file once, as the notes of
[13.7.0](https://github.com/n-schilling/munimentum/releases/tag/v13.7.0)
describe.

**From 13.5.x or older:** the first run that updates the index reads
every file once more, as the notes of
[13.6.1](https://github.com/n-schilling/munimentum/releases/tag/v13.6.1)
describe.

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
