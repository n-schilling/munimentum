## New in 15.1.0

- **Ignore**: an address without a card – a shared mailbox, a newsletter,
  somebody you do not want listed – can be set to *Ignore* in the card's menu.
  It leaves *Not in the address book*, stays among all contacts tagged
  *ignored*, and the same menu takes it back.
- **Signatures are read ahead**: each index run reads the signatures of the
  people *Contacts* lists and keeps them in your profile. A card says *footer
  extracted* where one was found, and the window names when it was read – it
  may have changed since.
- **For MCP clients**: an address you set to be ignored is marked as such, and
  the research skill tells the client to leave it out of a contact list.

## Upgrading

The first index run after the update reads the signatures once for everyone
*Contacts* lists; later runs read only who has sent new mail. Until then a
signature is read when you open a person's *Additional information*.

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
| `Munimentum-linux-x64.tar.gz` | Linux (64-bit, glibc 2.39+) |
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
