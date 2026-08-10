Fertige App zum Herunterladen — Python oder sonstige Installationen sind nicht nötig.

## Neu in 2.0.1

**Fehlerbehebung:** Stand die Anmeldung auf *Anmelden*, riss ein Export sofort
das Anmeldefenster auf — auch dann, wenn ein gültiger Zugangsschlüssel bereitlag.
Der Rückfall auf den Schlüssel gab es zwar, er griff aber erst, nachdem jemand
das Fenster weggeklickt hatte. Jetzt wird zuerst still gefragt, dann der
Schlüssel genommen, und das Fenster kommt nur, wenn es sonst keinen Weg gibt.

## Neu in 2.0.0

**Anmelden statt Schlüssel einfügen — wahlweise.** Der Assistent bietet oben
zwei gleichwertige Wege an. Der bisherige bleibt vorausgewählt: Zugangsschlüssel
aus dem Graph Explorer, ohne Rückfrage bei der IT, gültig für ein paar Stunden.
Neu daneben: eine richtige Anmeldung. Die App merkt sich danach nur die
Erlaubnis, sich selbst neue Schlüssel auszustellen — **damit läuft der Zeitplan
zum ersten Mal unbeaufsichtigt weiter, auch über einen Neustart hinweg.** Dein
Passwort sieht sie nie. Wer eine eigene App-Registrierung braucht, trägt
Client-ID und Tenant ein; ohne Eintrag genügt Microsofts öffentliche Anwendung,
für die es keine Registrierung braucht.

**Verlauf: zusammengehörende Mails als Gespräch.** Ein Treffer allein sagt oft
zu wenig — „Ja, machen wir so" ist erst mit der Frage davor eine Aussage. Unter
jedem Treffer klappt jetzt der ganze Mailwechsel auf. Auf einem Testordner mit
1.181 Mails ergab das 556 Gespräche, das längste mit 19 Nachrichten über zehn
Tage. Dafür ist **kein Neu-Export nötig**, nur einmal neu indizieren.

**Gelöschte Mails bleiben auffindbar.** Ein Archiv, das nur wächst, beantwortet
die wichtigste Frage nicht: was war hier einmal und ist jetzt weg? Jeder
Exportlauf vergleicht nun, was im Postfach wirklich noch liegt, und vermerkt
Verschwundenes — die Datei bleibt selbstverständlich erhalten. In der Suche gibt
es dafür **„Nur Gelöschtes"** und eine Markierung am Treffer. Verschobenes wird
nicht mit Gelöschtem verwechselt: jeder Verdacht wird einzeln bei Microsoft
nachgefragt.

**Mails öffnen sich im Mailprogramm.** Ein Klick auf einen Treffer lud bisher
den MIME-Rohtext ins Browserfenster. Jetzt kommt die `.eml` als Datei — und
damit als richtige Mail samt Anhängen. Ebenso Termine und Kontakte.

**Datenordner und Trefferzahl einstellbar.** Beides steht jetzt in den
*Einstellungen*, statt nur über Kommandozeilenschalter erreichbar zu sein. Der
Datenordner wirkt nach einem Neustart und verschiebt nichts.

## Neu in 1.1.1

**Die App hat ein eigenes Symbol.** Bis hierher zeigten Dock, Taskleiste und
Finder PyInstallers Standardbild — ein Python-Logo auf einer Diskette. Jetzt
steht dort ein Archivkasten in der Farbe der Oberfläche, in allen Größen bis
hinunter zu 16 Pixeln.

## Neu in 1.1.0

**Läufe, die nichts zu tun haben, tun auch nichts.** Bringt ein Export keine
neuen Inhalte, entfallen Indizierung und Kalenderaufbau — statt zweier Minuten
für ein unverändertes Ergebnis. Der Kalender wird außerdem nur noch aufgebaut,
wenn *Kalender* oder *Kontakte* im Export stehen, und liest die Mails nur, wenn
auch Mail dabei war.

**Wiederherstellung gelöschter Termine abschaltbar.** Sie holt Termine zurück,
die es im Kalender nicht mehr gibt (auf einem Testarchiv 2.687 Stück), liest
dafür aber jede Mail — dort gemessen 288 s gegen 1,6 s ohne. Standardmäßig an,
umschaltbar in *Einstellungen → Outlook-Export*.

**Aufgeräumte Oberfläche.** Drei Reiter statt sieben (Exportieren, Durchsuchen,
Einstellungen); Zeitplan und MCP sind in die Einstellungen gewandert. Neu sind
ein Fortschrittsbalken, der zeigt, wie weit ein Lauf ist, und ein Protokoll als
ausklappbare Leiste am unteren Rand — auf jedem Reiter erreichbar.

**Klartext statt Systemsprache.** Die Statuskacheln oben rechts nennen jetzt,
was der Zustand bedeutet (*„238.408 Nachrichten durchsuchbar"* statt *„269.744
chunks"*); der Fachbegriff steht im Tooltip. Auch der Token-Dialog wurde
entschlackt, die Berechtigungen sind eingeklappt und gehen nur auf, wenn
wirklich eine fehlt.

**Assistenten mit der Tastatur bedienbar.** ESC schließt, Tab bleibt im Fenster,
Strg/Cmd+Enter löst die Hauptaktion aus.

**KI-Zusammenfassung im Suchreiter** (nur mit Ollama) — siehe unten.

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

## Starten und Beenden

Die App hat kein eigenes Fenster – sie liefert eine Seite aus und lebt im
Browser. Auf dem Mac bleibt sie deshalb nicht im Dock stehen. Beendet wird sie
oben rechts über **„Beenden“**; der MCP-Server geht mit.

Ein zweiter Start legt keine zweite Kopie an: die App merkt, dass schon eine
läuft, und öffnet nur deren Seite. Das ist auch der Weg zurück, wenn du den Tab
geschlossen hast – einfach die App noch einmal starten.

## Wo landen die Daten?

Nicht in der App, sondern im Benutzerordner — ein Update überschreibt also nichts:

* macOS: `~/Library/Application Support/Microsoft365-Archiv`
* Windows: `%LOCALAPPDATA%\Microsoft365-Archiv`
* Linux: `~/.local/share/Microsoft365-Archiv`

Der Pfad steht auch im Export-Reiter der Oberfläche. Ein Postfach kann zweistellige
Gigabyte belegen; für eine andere Platte lässt er sich seit 2.0.0 direkt in den
*Einstellungen* umstellen (wirkt nach einem Neustart und verschiebt nichts).
Für einen einzelnen Lauf gehen weiterhin `--data-dir ORDNER` und
`OFFICE365_DATA_DIR`.

## Sprache

Die Oberfläche gibt es auf Deutsch, Englisch und Französisch und richtet sich
standardmäßig nach der Sprache deines Browsers. Umstellen kannst du sie im
Reiter *Einstellungen*. Exportierte Inhalte bleiben davon unberührt.

## Optional: Ollama

Ohne Ollama funktioniert alles außer der *semantischen* Suche — Export, Volltextsuche
und der MCP-Server für Claude laufen ganz normal. Die App fragt beim Start und
erklärt die Installation, falls gewünscht.

Mit Ollama und einem geladenen Sprachmodell kommt im Reiter *Suche* zusätzlich
die Checkbox **„KI-Zusammenfassung (Ollama)“** dazu: ein KI-Modell in deinem
Ollama fasst die Treffer zu einem Absatz mit Quellenangaben zusammen. Der Kasten
sagt das auch so — die Zusammenfassung stammt nicht aus deinem Archiv, sondern
von der KI, und stützt sich allein auf die Treffer darunter. Nichts verlässt
dabei den Rechner. Modell und Quellenzahl stehen in den *Einstellungen*.

## Aktualisierungen

Die App sieht beim Start einmal bei GitHub nach, ob es eine neuere Version gibt,
und hinterlässt dann nur eine Notiz mit Link – heruntergeladen oder ersetzt wird
nichts. Abschalten kannst du das im Reiter *Einstellungen*; es ist die einzige
Verbindung, die die App außer zu Microsoft Graph und deinem lokalen Ollama
aufbaut.

## Prüfsummen

`SHA256SUMS.txt` liegt bei. Prüfen mit `shasum -a 256 -c SHA256SUMS.txt` (macOS/Linux)
bzw. `Get-FileHash datei.zip` (PowerShell).
