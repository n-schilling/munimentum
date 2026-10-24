## New in 14.7.0

- **The organization as CSV**: *CSV…* in *Organization* downloads the people
  of a version – the whole tenant, the part below a person or the list shown
  – or the changes over a span of versions, ready for Excel. Every file names
  each person's company; mail addresses come along only when you tick them.

## Upgrading

Nothing happens once after this update.

Skipping releases is fine: whatever an older release does once after an
update, the first run does too. What changes for scripts against the
HTTP interface is in the notes of the release that changed it – see
[all releases](https://github.com/n-schilling/munimentum/releases).
Coming from far back? A quick look through the notes you skipped shows
whether any of them asks something of you.

## Which file?

| File | For |
|---|---|
| `Munimentum-macos-arm64.dmg` | Mac with Apple Silicon (M1 or newer) |
| `Munimentum-windows-x64.zip` | Windows 10/11 (64-bit) |
| `Munimentum-linux-x64.tar.gz` | Linux (64-bit, glibc 2.35+) |
| `Munimentum-skill-research.zip`, `…-case.zip`, `…-evidence.zip` | the three skills for MCP clients, to install by hand – the same files *Download skills* gives |


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
