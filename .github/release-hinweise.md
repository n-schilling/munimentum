Fertige App zum Herunterladen — Python oder sonstige Installationen sind nicht nötig.

## Welche Datei?

| Datei | Für |
|---|---|
| `Microsoft365-Archiv-macos-arm64.zip` | Mac mit Apple Silicon (M1–M4) |
| `Microsoft365-Archiv-macos-x86_64.zip` | Mac mit Intel-Prozessor |
| `Microsoft365-Archiv-windows-x64.zip` | Windows 10/11 (64 Bit) |
| `Microsoft365-Archiv-linux-x64.tar.gz` | Linux (64 Bit, glibc ab 2.35) |

Nicht sicher, welcher Mac? Apfel-Menü → „Über diesen Mac“: steht dort *Apple M…*,
dann `arm64`, sonst `x86_64`.

## Starten

**macOS** — ZIP entpacken, `Microsoft365-Archiv.app` nach „Programme“ ziehen, doppelklicken.

**Windows** — ZIP entpacken (Rechtsklick → „Alle extrahieren“, nicht nur hineinschauen),
dann `Microsoft365-Archiv.exe` im entpackten Ordner doppelklicken.

**Linux** — `tar -xzf Microsoft365-Archiv-linux-x64.tar.gz` und `./Microsoft365-Archiv/Microsoft365-Archiv` starten.

Danach öffnet sich die Oberfläche von selbst im Standardbrowser. Alles Weitere —
Token holen, exportieren, suchen — steht dort.

## „Nicht überprüft“ / „Windows hat Ihren PC geschützt“

Die Dateien sind **nicht signiert** (Signaturzertifikate von Apple und Microsoft
kosten Geld). Beide Systeme warnen deshalb beim ersten Start. Das ist erwartet
und einmalig:

**macOS** — der Dialog heißt *„Apple konnte nicht überprüfen, ob … frei von
Schadsoftware ist“* und bietet als blauen Knopf **„In den Papierkorb legen“** an.
Nicht darauf klicken:

1. **„Fertig“** wählen (der unauffällige Knopf darunter).
2. Systemeinstellungen → *Datenschutz & Sicherheit* → ganz nach unten scrollen →
   **„Dennoch öffnen“**. Der Knopf erscheint nur für etwa eine Stunde nach dem
   blockierten Versuch; ist er weg, die App noch einmal doppelklicken.
3. Beim erneuten Nachfragen *„Öffnen“* bestätigen. Danach ist Ruhe.

Der oft genannte Weg *Rechtsklick → Öffnen* funktioniert seit macOS 15 nicht
mehr zuverlässig. Schneller und sicher geht es im Terminal:

```bash
xattr -dr com.apple.quarantine "/Applications/Microsoft365-Archiv.app"
```

Was dabei passiert: Der Browser markiert jeden Download mit einem Quarantäne-
Merkmal, und für markierte Programme verlangt macOS eine Beglaubigung durch
Apple. Die App ist nur ad-hoc signiert, nicht beglaubigt – der Befehl entfernt
das Merkmal. Deshalb lief eine Kopie, die nicht über den Browser kam, auch ohne
Nachfrage.

**Windows** — im blauen SmartScreen-Fenster auf *„Weitere Informationen“* und
dann *„Trotzdem ausführen“*.

Wer das nicht möchte, kann stattdessen aus dem Quelltext starten (`python3 app.py`,
siehe README) — Funktion und Ergebnis sind identisch.

## Wo landen die Daten?

Nicht in der App, sondern im Benutzerordner — ein Update überschreibt also nichts:

* macOS: `~/Library/Application Support/Microsoft365-Archiv`
* Windows: `%LOCALAPPDATA%\Microsoft365-Archiv`
* Linux: `~/.local/share/Microsoft365-Archiv`

Der Pfad steht auch im Export-Reiter der Oberfläche. Ein Postfach kann zweistellige
Gigabyte belegen; für eine andere Platte die App mit `--data-dir ORDNER` starten
oder `OFFICE365_DATA_DIR` setzen.

## Sprache

Die Oberfläche gibt es auf Deutsch, Englisch und Französisch und richtet sich
standardmäßig nach der Sprache deines Browsers. Umstellen kannst du sie im
Reiter *Einstellungen*. Exportierte Inhalte bleiben davon unberührt.

## Optional: Ollama

Ohne Ollama funktioniert alles außer der *semantischen* Suche — Export, Volltextsuche
und der MCP-Server für Claude laufen ganz normal. Die App fragt beim Start und
erklärt die Installation, falls gewünscht.

## Aktualisierungen

Die App sieht beim Start einmal bei GitHub nach, ob es eine neuere Version gibt,
und hinterlässt dann nur eine Notiz mit Link – heruntergeladen oder ersetzt wird
nichts. Abschalten kannst du das im Reiter *Einstellungen*; es ist die einzige
Verbindung, die die App außer zu Microsoft Graph und deinem lokalen Ollama
aufbaut.

## Prüfsummen

`SHA256SUMS.txt` liegt bei. Prüfen mit `shasum -a 256 -c SHA256SUMS.txt` (macOS/Linux)
bzw. `Get-FileHash datei.zip` (PowerShell).
