## New in 14.1.0

- **Starting the app again opens the one already running** – with the
  browser closed and the app left in the background, a second start no
  longer brings up a second app on the next port; it opens the page of the
  running one. From source, `--profile` naming another profile starts that
  one beside it, and `--port` starts another one on purpose.
- **Fewer calls to Microsoft** – whether you are signed in is read from the
  file on disk instead of asking Microsoft on every look at the state; its
  sign-in is asked only when you sign in and when a run renews the key (see
  [PRIVACY.md](https://github.com/n-schilling/munimentum/blob/main/PRIVACY.md)).
- **Organization: managers the feed leaves out are found again** – since
  14.0.2 they were asked for without their id, so every answer counted as
  refused and the line above ended there.
- **For scripts against the HTTP interface** – `GET /api/v1/access/session`
  is new; the `auth` node of `GET /api/v1/status` is deprecated, announced
  by `Deprecation` and `Link` headers on every status, and goes in 16.0.0.

## Upgrading

The next run that reads the organization asks once more for the managers
the feed left out and puts them into the line – the organization gets one
new version.

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
