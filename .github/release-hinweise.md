## New in 13.7.0

**An archive that can prove itself.** A file a run replaces keeps its
**earlier version**, and every file's SHA-256 goes into a **chain of
checksums** that says what lay in the archive when – each line names the
line before it, so nothing can be swapped afterwards unnoticed. Mirrored
files are held against Microsoft's own checksum as they come.

- **Versions** – a hit with more than one opens them in a fold below its
  content, each with its changes to today's; a Teams message keeps what
  it said before an edit, and a deleted one keeps its text.
- **Checksums** – a quiet fold at the bottom of a file's detail holds its
  SHA-256, and for a mirrored one whether Microsoft's checksum confirms
  it; only a disagreement shows on the closed fold.
- **Cases** – an item changed since it came in is marked, and *Compare*
  shows the version it came with. *Close case* records every original's
  checksum; **Export case…** adds `SHA256SUMS.txt` and an `evidence/`
  folder anyone can check without the app, and the earlier versions of
  changed items.
- **Insights** – *Archive and bookkeeping* reads the chain end to end and
  hashes every file afresh; what changed outside the app is named there,
  each file one click from its detail.
- **Settings › Evidence** – how far earlier versions are kept, and, off by
  default, a time stamp for every run from a service you name (RFC 3161).

## Upgrading

**From 13.6.x:** the first run that exports reads every file of the
archive once to begin the chain – with large mirrors that takes a while;
nothing is fetched from Microsoft for it. What was in the archive before
is recorded as found on that day.

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
