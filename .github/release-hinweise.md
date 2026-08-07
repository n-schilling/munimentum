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

**macOS** — App doppelklicken, die Warnung wegklicken, dann
Systemeinstellungen → *Datenschutz & Sicherheit* → ganz unten *„Dennoch öffnen“*.
Alternativ einmal im Terminal:

```bash
xattr -dr com.apple.quarantine "/Applications/Microsoft365-Archiv.app"
```

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

## Optional: Ollama

Ohne Ollama funktioniert alles außer der *semantischen* Suche — Export, Volltextsuche
und der MCP-Server für Claude laufen ganz normal. Die App fragt beim Start und
erklärt die Installation, falls gewünscht.

## Prüfsummen

`SHA256SUMS.txt` liegt bei. Prüfen mit `shasum -a 256 -c SHA256SUMS.txt` (macOS/Linux)
bzw. `Get-FileHash datei.zip` (PowerShell).
