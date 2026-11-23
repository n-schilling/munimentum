## New in 15.0.0

- **Contacts know your mail**: a card marks a *frequent contact*, and
  *Additional information* in its menu shows the mails received from and sent
  to the person. Three switches narrow the list to an exchange both ways, to
  frequent contacts and to who the address book lacks.
- **Mail signatures**: the same window reads what stands under a person's
  mails – job title, company, numbers – with the mail it came from one click
  away; it is read automatically and can be incomplete or wrong. The card
  itself names job title, company, department and mobile numbers apart.
- **Add to the address book**: who writes to you and has no Outlook card gets
  a card in Munimentum with one click – begun from the mail and its
  signature's numbers, completed by hand, marked *added here*. It stays in
  your profile; Outlook's card wins.
- **Versions of a card**: *Versions* in a card's menu shows what it said
  before – for an Outlook card every version the archive keeps, for your own
  what each change replaced.
- **One person, one address**: two people of the same name are two entries in
  *Contacts*, each with the address they are and their own mail.
- **Frequent contact is yours to define**: from how many mails in how many
  months someone counts is set under *Settings › App*, twelve in twelve by
  default – as are your own addresses and how many people *Contacts* lists, 500.
- **For MCP clients**: a client can ask whom you exchange mail with and what
  their signatures say – what a contact list needs and the address book lacks –,
  reads the contacts you added as address book contacts, marked as yours, and
  is told whose archive it searches.
- **Linux**: the bundle now needs glibc 2.39 or newer.

## Upgrading

Run *Build calendar & contacts* under *Settings › Expert mode* once: the
contact cards then name department and mobile numbers. The next Outlook
export that brings something new does the same by itself.

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
