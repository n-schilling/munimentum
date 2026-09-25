## New in 14.0.0

- **Your organization** – a chip on the Teams card keeps who reports to
  whom across the tenant, as Teams' profile card shows it, and every change
  of it becomes a version. It needs `User.Read.All`, which some tenants
  grant only with an admin's consent.
- **A fifth way into the archive** – *Organization* walks the tenant from
  the top down by click, in any version, or lists everyone holding a role.
- **The org chart for Claude** – Claude can ask for it on any day the
  archive knows: someone's manager, their team, everyone holding a role.
- **One item, nothing else** – an item opened from Claude's citation fills
  the page on its own, with no search around it; *Explore archive* brings
  the search back as it was.
- **A link to every item** – the detail copies a link to the item in the
  version it shows; Claude's citations carry the same, and a later change
  opens the cited version with a note that it changed since.
- **The list steps aside on request** – with a hit open, a second click on
  *List* hides the list and gives the hit the whole width; one more brings
  it back.

## Upgrading

Nothing happens once after this update. Once *Organization* is ticked, the
next run reads the whole directory – a minute or two on a large tenant –
and every later run asks only for what changed.

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
