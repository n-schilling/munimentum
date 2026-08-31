# Security Policy

## Reporting a vulnerability

Please report security issues **privately**, not as a public issue.

Use GitHub's private reporting — [**Report a
vulnerability**](https://github.com/n-schilling/munimentum/security/advisories/new).
It is enabled on this repository and goes straight to me. If you would rather
not use it, write to <mail@nschilling.de>.

This is a spare-time project with one maintainer, so please expect a first reply
within about a week rather than within a day. If the report holds up, I will
tell you what I intend to do and roughly when, and credit you in the release
notes unless you prefer otherwise. Please give me the chance to ship a fix
before you publish.

## Supported versions

Only the latest release. There are no maintenance branches — fixes go into the
next release, which is usually days rather than months away.

| Version | Supported |
|---|---|
| Latest release | Yes |
| Anything older | No — please update first |

## The security model, in short

Worth reading before you report: a few properties of this app look alarming and
are deliberate.

- **The app serves your entire mail and chat archive over HTTP without
  authentication.** It binds to `127.0.0.1` only, and every request is checked
  against its `Host` header, so a web page you happen to visit cannot reach it
  through your browser (DNS rebinding). "The API has no login" is a documented
  decision, not a finding.
- **The MCP server works the same way** — loopback, no authentication, `Host`
  and `Origin` validated by the SDK. Binding it to any other address requires
  naming the allowed hostnames explicitly; it refuses to start otherwise.
- **The Windows binaries are not code-signed.** SmartScreen warns on first
  launch, and the release notes explain the way past it. The macOS builds are
  signed and notarized by Apple.
- **The redaction in the bug reporter is best-effort.** It replaces e-mail
  addresses and user names in paths; folder names and subject lines can remain.
  The whole text is shown for editing, and the app sends nothing by itself — you
  submit the form.
- **The access token lives in your data folder** with restricted file
  permissions. Anyone who can already read that folder has the archive anyway.

## What I would very much like to hear about

- Anything that lets a web page, another local user, or a program without access
  to the data folder reach the archive, the access token, or Microsoft Graph.
- Path traversal or unintended file reads through the HTTP API or the MCP
  resource handlers — both serve files out of the export folders by design, so
  the boundary matters.
- Code execution triggered by exported content: a crafted `.eml`, `.html`,
  `.ics` or `.vcf`, a file mirrored from OneDrive or SharePoint, or a rendered
  SharePoint page, that runs code while being parsed or while being shown in
  the interface.
- Anything that moves archive content, tokens or telemetry off the machine. The
  only outbound connections are Microsoft Graph, your local Ollama, and — unless
  you switch it off — one update check against `api.github.com` at startup.
