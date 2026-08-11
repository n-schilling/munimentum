# macOS: signieren und beglaubigen

> **In Betrieb seit dem Release nach 4.1.1.** Der Build signiert und beglaubigt
> die macOS-Bündel bei Tag-Läufen; Pushes auf `main` bauen weiter unsigniert.
> Diese Anleitung beschreibt, wie es eingerichtet wurde und was zu tun ist, wenn
> das Zertifikat abläuft oder etwas hakt.

Was Nutzer vorher sahen: *„Apple konnte nicht überprüfen, ob … frei von
Schadsoftware ist"*, dazu ein blauer Knopf **„In den Papierkorb legen"**. Das
verschwindet erst, wenn das Bündel **signiert** *und* von Apple **beglaubigt**
(notarisiert) ist. Beides zusammen, eines allein reicht nicht.

Diese Anleitung ist auf dieses Projekt zugeschnitten: PyInstaller-Bündel,
Auslieferung als DMG, Bau auf GitHub Actions.

---

## 1. Das richtige Zertifikat

Es gibt mehrere Sorten. Gebraucht wird genau eine:

| Sorte | wofür | hier |
|---|---|---|
| **Developer ID Application** | Programme außerhalb des App Store | **das hier** |
| Developer ID Installer | `.pkg`-Installer | nein |
| Apple Development / Distribution | Xcode-Tests, App Store | nein |

**Anlegen** (am einfachsten über Xcode, weil der Schlüssel dann gleich richtig
im Schlüsselbund liegt):

1. Xcode → *Settings* → *Accounts* → Apple-ID hinzufügen
2. Team auswählen → *Manage Certificates…*
3. **+** → **Developer ID Application**

Ohne Xcode geht es über [developer.apple.com](https://developer.apple.com/account/resources/certificates/list):
im Schlüsselbund *Zertifikatsassistent → Zertifikat von einer
Zertifizierungsinstanz anfordern* eine CSR erzeugen, hochladen, das Ergebnis
laden und doppelklicken. Beim Doppelklick fragt macOS nach dem Schlüsselbund:
**Anmeldung**, nicht iCloud. Dort liegt seit der CSR der private Schlüssel, und
nur wenn beide Hälften im selben Bund liegen, werden sie zu einer Identität —
sonst lässt sich der Eintrag später auch nicht als `.p12` exportieren.

Auf diesem Weg fragt Apple zwischendurch nach der Zwischeninstanz („Select a
Developer ID Certificate Intermediary"). **G2 Sub-CA** wählen, nicht *Previous*:
Zertifikate an der alten Instanz laufen unabhängig von ihrer eigenen Laufzeit am
**01.02.2027** ab, und es gibt sie nur noch für Xcode älter als 11.4.1. Hier
wird gar nicht mit Xcode signiert. Mit G2 gilt das Zertifikat die vollen fünf
Jahre.

> Developer-ID-Zertifikate darf nur der **Account Holder** anlegen (bei einem
> Einzelaccount bist du das). Ein Team hat davon höchstens fünf gleichzeitig.

Prüfen, dass es da ist:

```bash
security find-identity -v -p codesigning
# 1) ABC123…  "Developer ID Application: Vorname Nachname (TEAMID)"
```

Die Zeichenkette in Anführungszeichen ist ab jetzt deine **Signatur-Identität**,
`TEAMID` die zehnstellige Team-ID.

Und einmal wirklich signieren, bevor du in die Schritte 2 bis 6 investierst —
das prüft in dreißig Sekunden, was sonst erst im CI auffällt: dass der
Schlüssel benutzbar ist, dass der Zeitstempel-Server erreichbar ist und dass
die Härtung greift.

```bash
cp /bin/echo probe
codesign --force --sign "$MACOS_SIGN_IDENTITY" --timestamp --options runtime probe
codesign -dv --verbose=4 probe 2>&1 | grep -E "^Authority|^Timestamp|^Runtime"
rm probe
```

Erwartet werden drei `Authority`-Zeilen (dein Zertifikat, die Zwischeninstanz,
`Apple Root CA`), eine `Timestamp`-Zeile und eine `Runtime Version`. Fragt macOS
dabei nach dem Schlüsselbund, einmal *Immer erlauben* — sonst bleibt später
jeder Lauf an diesem Dialog stehen.

## 2. Schlüssel für die Beglaubigung

Notarisieren heißt: das fertige Paket zu Apple hochladen, dort wird es
automatisch geprüft, und du bekommst ein Ticket zurück. Dafür braucht der Build
einen Zugang.

Nimm einen **App Store Connect API-Schlüssel**, nicht deine Apple-ID mit
app-spezifischem Passwort — der Schlüssel läuft nicht ab, stolpert nicht über
Zwei-Faktor und lässt sich einzeln zurückziehen.

[App Store Connect → Users and Access → Integrations → App Store Connect API](https://appstoreconnect.apple.com/access/integrations/api)
→ **+**, Rolle **Developer** genügt. Du bekommst:

* **Issuer ID** (UUID, steht über der Liste)
* **Key ID** (zehn Zeichen)
* die Datei **`AuthKey_<KeyID>.p8`** — **nur einmal herunterladbar**

## 3. Beides für GitHub Actions verpacken

Das Zertifikat aus dem Schlüsselbund exportieren: *Schlüsselbundverwaltung* →
Kategorie *Meine Zertifikate* → den Eintrag **Developer ID Application**
aufklappen, Zertifikat **und** privaten Schlüssel markieren → Rechtsklick →
*2 Objekte exportieren…* → Format `.p12`, ein Passwort vergeben.

Dann beides nach base64:

```bash
base64 -i DeveloperID.p12          | pbcopy   # → MACOS_CERT_P12
base64 -i AuthKey_XXXXXXXXXX.p8    | pbcopy   # → AC_API_KEY_P8
```

Unter *Settings → Secrets and variables → Actions* anlegen:

| Secret | Inhalt |
|---|---|
| `MACOS_CERT_P12` | die `.p12` als base64 |
| `MACOS_CERT_PASSWORD` | das Passwort von eben |
| `MACOS_SIGN_IDENTITY` | `Developer ID Application: Vorname Nachname (TEAMID)` |
| `AC_API_KEY_P8` | die `.p8` als base64 |
| `AC_API_KEY_ID` | die Key ID |
| `AC_API_ISSUER_ID` | die Issuer ID |

Die `.p12` und die `.p8` gehören **nicht** ins Repository — die `.gitignore`
sollte sie zusätzlich abfangen.

## 4. Berechtigungen (Entitlements)

Beglaubigt wird nur mit **Hardened Runtime**. Die schaltet Dinge ab, die
CPython braucht. Ohne die passenden Ausnahmen startet die App nach dem
Signieren nicht mehr — das ist der häufigste Stolperstein.

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

Jede dieser drei Zeilen weicht die Härtung auf. Fang mit allen dreien an, damit
es überhaupt läuft, und nimm dann einzeln eine weg und prüfe, ob die App noch
startet — was nicht gebraucht wird, gehört raus.

## 5. Signieren im Build

PyInstaller kann das selbst, und das ist hier der richtige Weg: das Bündel
enthält über 200 einzelne `.so`- und `.dylib`-Dateien, und die müssen **von
innen nach außen** signiert werden. Von Hand vergisst man garantiert eine.

In `packaging/app.spec` sind die beiden Schalter schon vorgesehen:

```python
exe = EXE(
    …
    codesign_identity=os.environ.get("MACOS_SIGN_IDENTITY") or None,
    entitlements_file=str(ROOT / "packaging" / "entitlements.plist"),
)
```

Im Workflow davor der Schlüsselbund — ein eigener, temporärer, damit nichts am
Runner hängen bleibt. Die Bedingung ist der Tag, nicht das Vorhandensein des
Secrets: so kommt das Zertifikat nur bei Releases auf einen Runner, und Pushes
auf `main` bleiben schnell (Beglaubigen kostet je Bündel Minuten):

```yaml
- name: Zertifikat einspielen (macOS)
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
    # Ohne diese Zeile fragt codesign nach dem Schlüsselbund-Passwort und
    # der Lauf bleibt hängen, bis das Zeitlimit greift.
    security set-key-partition-list -S apple-tool:,apple:,codesign: \
      -s -k bauen build.keychain
    rm cert.p12
```

## 6. Beglaubigen und Ticket anheften

Reihenfolge, und sie ist wichtig:

```bash
# 1) App ist signiert (aus dem Build). Zum Einreichen packen.
ditto -c -k --keepParent "dist/Microsoft365-Archiv.app" pruefling.zip

# 2) Einreichen und warten (dauert meist 1–5 Minuten)
echo "$AC_API_KEY_P8" | base64 --decode > key.p8
xcrun notarytool submit pruefling.zip \
  --key key.p8 --key-id "$AC_API_KEY_ID" --issuer "$AC_API_ISSUER_ID" \
  --wait

# 3) Ticket an die App heften – danach geht es auch OHNE Internet durch
xcrun stapler staple "dist/Microsoft365-Archiv.app"

# 4) Erst jetzt das DMG bauen, aus der gehefteten App
#    (der bestehende Schritt im Workflow)

# 5) Das DMG selbst signieren, beglaubigen und heften
codesign --sign "$MACOS_SIGN_IDENTITY" --timestamp Microsoft365-Archiv-*.dmg
xcrun notarytool submit Microsoft365-Archiv-*.dmg \
  --key key.p8 --key-id "$AC_API_KEY_ID" --issuer "$AC_API_ISSUER_ID" --wait
xcrun stapler staple Microsoft365-Archiv-*.dmg
rm key.p8
```

Warum zweimal beglaubigen: Das Ticket am DMG hilft beim Öffnen des DMG. Zieht
jemand die App heraus, hat *sie* ein eigenes Ticket nur, wenn sie vorher
geheftet wurde — sonst fragt macOS bei Apple nach, und ohne Internet gibt es
eine Warnung. Die zweite Runde kostet ein paar Minuten Bauzeit und erspart
genau diesen Fall.

Geht die Prüfung schief, sagt Apple auch warum:

```bash
xcrun notarytool log <submission-id> --key key.p8 \
  --key-id "$AC_API_KEY_ID" --issuer "$AC_API_ISSUER_ID"
```

## 7. Nachprüfen

Auf dem Runner nach dem Bauen — und einmal von Hand auf einem Mac, der die
Dateien frisch aus dem Netz geladen hat:

```bash
codesign --verify --strict --verbose=2 Microsoft365-Archiv.app
spctl -a -t exec -vvv Microsoft365-Archiv.app     # erwartet: accepted, source=Notarized Developer ID
xcrun stapler validate Microsoft365-Archiv.app
xcrun stapler validate Microsoft365-Archiv-macos-arm64.dmg
```

Der eigentliche Beweis ist aber der Doppelklick auf einem **anderen** Mac, auf
dem das Projekt nie gebaut wurde und der die Datei über den Browser bekommen
hat. Nur dort greift das Quarantäne-Merkmal, um das es geht.

## Was danach anders ist

* Der Abschnitt *„Nicht überprüft"* in den Release-Hinweisen gilt nur noch für
  **Windows**. Der macOS-Teil samt `xattr`-Befehl kann raus.
* **Windows bleibt unsigniert.** Dort kostet ein Zertifikat deutlich mehr, und
  SmartScreen verlangt zusätzlich Verbreitung, bevor es Ruhe gibt.
* Der Developer-Account kostet 99 $ im Jahr. Läuft er aus, bleiben bereits
  beglaubigte Bündel gültig — neue lassen sich nicht mehr beglaubigen.
* Das Zertifikat selbst gilt fünf Jahre. Danach muss ein neues in die Secrets;
  alte Signaturen bleiben durch den Zeitstempel (`--timestamp`) gültig.

## Bekannte Stolpersteine

**Die App startet nach dem Signieren nicht.** Fast immer fehlen Entitlements.
`Console.app` zeigt beim Startversuch den Grund; typisch ist eine Meldung über
`mmap` oder eine nicht ladbare Bibliothek.

**Beglaubigung wird abgelehnt mit „The binary is not signed with a valid
Developer ID certificate".** Meist eine einzelne Datei tief im Bündel, die
PyInstaller nicht erwischt hat. Der Log aus `notarytool log` nennt sie beim
Namen.

**`errSecInternalComponent` beim Signieren im CI.** Das ist die fehlende
`set-key-partition-list`-Zeile.

**„0 valid identities found", obwohl das Zertifikat im Schlüsselbund liegt.**
Dann fehlt das Zwischenzertifikat und die Kette lässt sich nicht bilden. Der
Test: `security find-identity -p codesigning` (ohne `-v`) zeigt die Identität,
`mit -v` nicht. Nachrüsten:

```bash
security verify-cert -c ~/Downloads/developerID_application.cer
# CSSMERR_TP_NOT_TRUSTED  -> Zwischenzertifikat fehlt
curl -fsSLO https://www.apple.com/certificateauthority/DeveloperIDG2CA.cer
security import DeveloperIDG2CA.cer -k ~/Library/Keychains/login.keychain-db
```

Danach meldet `verify-cert` „certificate verification successful". Welche Datei
die richtige ist, sagt der Aussteller des eigenen Zertifikats — `OU=G2` heißt
`DeveloperIDG2CA.cer`:

```bash
openssl x509 -inform DER -in ~/Downloads/developerID_application.cer -noout -issuer
```

**Alles läuft durch, der Nutzer sieht die Warnung trotzdem.** Dann wurde die
Datei nach dem Beglaubigen noch einmal angefasst — jede Änderung, auch das
Umpacken, macht die Signatur ungültig. Deshalb steht der DMG-Bau *vor* dem
Signieren des DMG und danach passiert nichts mehr.
