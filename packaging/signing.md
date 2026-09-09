# macOS: signing and notarizing

> **In operation.** The build signs and
> notarizes the macOS bundles on tag runs; pushes to `main` still build
> unsigned. This guide describes how it was set up and what to do when the
> certificate expires or something gets stuck.

What users used to see: *"Apple could not verify that … is free of malware"*,
plus a blue **"Move to Trash"** button. That only goes away once the bundle is
**signed** *and* **notarized** by Apple. Both together — one alone is not
enough.

This guide is tailored to this project: PyInstaller bundle, shipped as a DMG,
built on GitHub Actions.

---

## 1. The right certificate

There are several kinds. Exactly one is needed:

| Kind | for what | here |
|---|---|---|
| **Developer ID Application** | programs outside the App Store | **this one** |
| Developer ID Installer | `.pkg` installers | no |
| Apple Development / Distribution | Xcode tests, App Store | no |

**Creating one** (easiest via Xcode, because the key then lands in the keychain
correctly right away):

1. Xcode → *Settings* → *Accounts* → add your Apple ID
2. Select the team → *Manage Certificates…*
3. **+** → **Developer ID Application**

Without Xcode it works via [developer.apple.com](https://developer.apple.com/account/resources/certificates/list):
in Keychain Access, generate a CSR via *Certificate Assistant → Request a
Certificate From a Certificate Authority*, upload it, download the result and
double-click it. On the double-click macOS asks which keychain to use:
**login**, not iCloud. That is where the private key has been sitting since the
CSR, and only when both halves sit in the same keychain do they become an
identity — otherwise the entry cannot be exported as a `.p12` later either.

On this route Apple asks along the way for the intermediate authority ("Select a
Developer ID Certificate Intermediary"). Choose **G2 Sub-CA**, not *Previous*:
certificates issued under the old authority expire on **1 February 2027**
regardless of their own validity period, and they only still exist for Xcode
older than 11.4.1. Nothing here is signed with Xcode at all. With G2 the
certificate is valid for the full five years.

> Only the **Account Holder** may create Developer ID certificates (on an
> individual account that is you). A team has at most five of them at a time.

Check that it is there:

```bash
security find-identity -v -p codesigning
# 1) ABC123…  "Developer ID Application: Firstname Lastname (TEAMID)"
```

The string in quotes is from now on your **signing identity**, `TEAMID` the
ten-character Team ID.

And sign something for real once before you invest in steps 2 to 6 — that
checks in thirty seconds what would otherwise only surface in CI: that the key
is usable, that the timestamp server is reachable and that the hardening takes
effect.

```bash
cp /bin/echo probe
codesign --force --sign "$MACOS_SIGN_IDENTITY" --timestamp --options runtime probe
codesign -dv --verbose=4 probe 2>&1 | grep -E "^Authority|^Timestamp|^Runtime"
rm probe
```

Expected are three `Authority` lines (your certificate, the intermediate
authority, `Apple Root CA`), one `Timestamp` line and a `Runtime Version`. If
macOS asks for the keychain here, click *Always Allow* once — otherwise every
later run stops at this dialog.

## 2. Key for notarization

Notarizing means: upload the finished package to Apple, where it is checked
automatically, and you get a ticket back. The build needs credentials for
that.

Use an **App Store Connect API key**, not your Apple ID with an app-specific
password — the key does not expire, does not trip over two-factor
authentication and can be revoked on its own.

[App Store Connect → Users and Access → Integrations → App Store Connect API](https://appstoreconnect.apple.com/access/integrations/api)
→ **+**, the **Developer** role is enough. You get:

* **Issuer ID** (UUID, shown above the list)
* **Key ID** (ten characters)
* the file **`AuthKey_<KeyID>.p8`** — **downloadable only once**

## 3. Packaging both for GitHub Actions

Export the certificate from the keychain: *Keychain Access* → category *My
Certificates* → expand the **Developer ID Application** entry, select the
certificate **and** the private key → right-click → *Export 2 Items…* → format
`.p12`, set a password.

Then base64 both:

```bash
base64 -i DeveloperID.p12          | pbcopy   # → MACOS_CERT_P12
base64 -i AuthKey_XXXXXXXXXX.p8    | pbcopy   # → AC_API_KEY_P8
```

Create under *Settings → Secrets and variables → Actions*:

| Secret | Content |
|---|---|
| `MACOS_CERT_P12` | the `.p12` as base64 |
| `MACOS_CERT_PASSWORD` | the password from just now |
| `MACOS_SIGN_IDENTITY` | `Developer ID Application: Firstname Lastname (TEAMID)` |
| `AC_API_KEY_P8` | the `.p8` as base64 |
| `AC_API_KEY_ID` | the Key ID |
| `AC_API_ISSUER_ID` | the Issuer ID |

The `.p12` and the `.p8` do **not** belong in the repository — the `.gitignore`
should catch them as well.

## 4. Entitlements

Notarization only works with the **Hardened Runtime**. It switches off things
that CPython needs. Without the right exceptions the app no longer starts after
signing — that is the most common stumbling block.

`packaging/entitlements.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>com.apple.security.cs.allow-jit</key><true/>
  <key>com.apple.security.cs.allow-unsigned-executable-memory</key><true/>
  <key>com.apple.security.cs.disable-library-validation</key><true/>
</dict>
</plist>
```

Each of these three lines weakens the hardening. Start with all three so that
it runs at all, then remove them one at a time and check whether the app still
starts — whatever is not needed should go.

## 5. Signing in the build

PyInstaller can do this itself, and that is the right way here: the bundle
contains over 200 individual `.so` and `.dylib` files, and they have to be
signed **from the inside out**. By hand you are guaranteed to miss one.

In `packaging/app.spec` the two switches are already in place:

```python
exe = EXE(
    …
    codesign_identity=os.environ.get("MACOS_SIGN_IDENTITY") or None,
    entitlements_file=str(ROOT / "packaging" / "entitlements.plist"),
)
```

Before that, in the workflow, the keychain — a separate, temporary one, so that
nothing is left behind on the runner. The condition is the tag, not the
presence of the secret: that way the certificate only reaches a runner on
releases, and pushes to `main` stay fast (notarizing costs minutes per
bundle):

```yaml
- name: Import certificate (macOS)
  if: startsWith(matrix.os, 'macos') && startsWith(github.ref, 'refs/tags/v')
  env:
    MACOS_CERT_P12: ${{ secrets.MACOS_CERT_P12 }}
    MACOS_CERT_PASSWORD: ${{ secrets.MACOS_CERT_PASSWORD }}
  run: |
    echo "$MACOS_CERT_P12" | base64 --decode > cert.p12
    security create-keychain -p bauen build.keychain
    security default-keychain -s build.keychain
    security unlock-keychain -p bauen build.keychain
    security import cert.p12 -k build.keychain -P "$MACOS_CERT_PASSWORD" \
      -T /usr/bin/codesign
    # Without this line codesign asks for the keychain password and the
    # run hangs until the time limit hits.
    security set-key-partition-list -S apple-tool:,apple:,codesign: \
      -s -k bauen build.keychain
    rm cert.p12
```

## 6. Notarizing and stapling the ticket

The order, and it matters:

```bash
# 1) The app is signed (from the build). Zip it for submission.
ditto -c -k --keepParent "dist/Munimentum.app" pruefling.zip

# 2) Submit and wait (usually takes 1–5 minutes)
echo "$AC_API_KEY_P8" | base64 --decode > key.p8
xcrun notarytool submit pruefling.zip \
  --key key.p8 --key-id "$AC_API_KEY_ID" --issuer "$AC_API_ISSUER_ID" \
  --wait

# 3) Staple the ticket to the app – after that it passes even WITHOUT internet
xcrun stapler staple "dist/Munimentum.app"

# 4) Only now build the DMG, from the stapled app
#    (the existing step in the workflow)

# 5) Sign, notarize and staple the DMG itself
codesign --sign "$MACOS_SIGN_IDENTITY" --timestamp Munimentum-*.dmg
xcrun notarytool submit Munimentum-*.dmg \
  --key key.p8 --key-id "$AC_API_KEY_ID" --issuer "$AC_API_ISSUER_ID" --wait
xcrun stapler staple Munimentum-*.dmg
rm key.p8
```

Why notarize twice: the ticket on the DMG helps when opening the DMG. If
someone drags the app out, *it* only has a ticket of its own if it was stapled
beforehand — otherwise macOS asks Apple, and without internet there is a
warning. The second round costs a few minutes of build time and spares exactly
that case.

If the check fails, Apple also tells you why:

```bash
xcrun notarytool log <submission-id> --key key.p8 \
  --key-id "$AC_API_KEY_ID" --issuer "$AC_API_ISSUER_ID"
```

## 7. Verifying

On the runner after the build — and once by hand on a Mac that has just
downloaded the files from the internet:

```bash
codesign --verify --strict --verbose=2 Munimentum.app
spctl -a -t exec -vvv Munimentum.app     # expected: accepted, source=Notarized Developer ID
xcrun stapler validate Munimentum.app
xcrun stapler validate Munimentum-macos-arm64.dmg
```

The real proof, however, is the double-click on a **different** Mac, one on
which the project was never built and which received the file through the
browser. Only there does the quarantine attribute this is all about take
effect.

## What changes afterwards

* **Windows stays unsigned.** A certificate costs considerably more there, and
  SmartScreen additionally demands distribution before it goes quiet.
* The developer account costs $99 a year. If it lapses, already notarized
  bundles remain valid — new ones can no longer be notarized.
* The certificate itself is valid for five years. After that a new one has to
  go into the secrets; old signatures remain valid thanks to the timestamp
  (`--timestamp`).

## Known pitfalls

**The app does not start after signing.** Almost always entitlements are
missing. `Console.app` shows the reason on the launch attempt; typical is a
message about `mmap` or a library that cannot be loaded.

**Notarization is rejected with "The binary is not signed with a valid
Developer ID certificate".** Usually a single file deep inside the bundle that
PyInstaller did not catch. The log from `notarytool log` names it.

**`errSecInternalComponent` when signing in CI.** That is the missing
`set-key-partition-list` line.

**"0 valid identities found", although the certificate is in the keychain.**
Then the intermediate certificate is missing and the chain cannot be built. The
test: `security find-identity -p codesigning` (without `-v`) shows the
identity, with `-v` it does not. To fix:

```bash
security verify-cert -c ~/Downloads/developerID_application.cer
# CSSMERR_TP_NOT_TRUSTED  -> intermediate certificate missing
curl -fsSLO https://www.apple.com/certificateauthority/DeveloperIDG2CA.cer
security import DeveloperIDG2CA.cer -k ~/Library/Keychains/login.keychain-db
```

After that `verify-cert` reports "certificate verification successful". Which
file is the right one is told by the issuer of your own certificate — `OU=G2`
means `DeveloperIDG2CA.cer`:

```bash
openssl x509 -inform DER -in ~/Downloads/developerID_application.cer -noout -issuer
```

**Everything passes, the user still sees the warning.** Then the file was
touched again after notarization — any change, including repackaging,
invalidates the signature. That is why the DMG build comes *before* signing the
DMG, and nothing happens after that.
