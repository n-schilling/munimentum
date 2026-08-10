Fertige App zum Herunterladen — Python oder sonstige Installationen sind nicht nötig.

## Was die App kann

**Exportieren.** Teams-Chats und -Kanäle, Outlook-Mail, Kalender, Kontakte und
**OneDrive-Dateien** über Microsoft Graph — mit deinem eigenen Zugang, ohne dass
jemand in der IT etwas freischalten muss. Jeder Lauf holt nur, was neu ist.
Welche Ordner mitkommen, sagen geordnete Include/Exclude-Regeln; *Exportliste
anzeigen* führt vor, was sie bedeuten.

**Durchsuchen.** Volltext und — mit [Ollama](https://ollama.com) — semantische
Suche über alles Exportierte, zu einer Rangfolge zusammengeführt. Filtern nach
Person, Zeitraum, Quelle und Postfachordner. Dazu der ganze Mailwechsel unter
einem Treffer, ein Kalender samt aus Einladungen zurückgeholter Termine, ein
Adressbuch und ein Filter für Nachrichten, die es im Postfach nicht mehr gibt.

**Auswerten.** Was im Archiv steckt — Nachrichten je Quelle, Gespräche,
Personen, Dateien, Zeitraum, belegter Platz. Und auf Knopfdruck ein Abgleich
gegen Postfach und Laufwerk: Was Microsoft je Ordner zählt gegen das, was hier
liegt.

**Mit Claude arbeiten.** Ein eingebauter MCP-Server macht das Archiv für Claude
durchsuchbar — mit Quellenangaben, Ordnerfilter und einer `days`-Abkürzung für
„die letzten sieben Tage".

Alles bleibt auf deinem Rechner. Die einzigen Verbindungen nach außen sind
Microsoft Graph und dein lokales Ollama.

## Neu in 4.0.0

**OneDrive wird gesichert.** Das eigene Laufwerk kommt als lokaler Spiegel
dazu — mit denselben Include/Exclude-Regeln wie das Postfach, einer
Größengrenze und einem Abgleich der Ordnerstruktur. Umbenanntes und
Verschobenes wird mitgezogen statt neu geladen; was in OneDrive gelöscht wird,
**bleibt hier liegen** und bekommt einen Vermerk. Frühere Fassungen einer
geänderten Datei bewahrt der Spiegel nicht — er hält die aktuelle und merkt
sich, was verschwunden ist.

Durchsuchbar sind **Name und Ordner**, nicht der Inhalt: `att:pdf` findet eine
Datei wie einen Mailanhang. Die Inhalte der Dokumente kommen später (siehe
`ROADMAP.md`).

**macOS kommt als DMG.** Bisher ein ZIP — bei dem das Archivierungsprogramm von
macOS auf manchen Rechnern abbrach und eine unbrauchbare App hinterließ.
Ursache waren die 36 Symlinks, die jedes PyInstaller-Bündel enthält. Beim DMG
wird gar nicht entpackt: Doppelklick, App auf *Programme* ziehen, fertig.

**Die Suche findet, was gemeint ist.** Die Bedeutungssuche hatte keine
Untergrenze und lieferte immer ihre besten Treffer — auch wenn nichts passte.
Wer einen einzelnen Tag eingrenzte, bekam alle Nachrichten dieses Tages. Jetzt
gibt es eine gemessene Untergrenze (45 %, in den *Einstellungen* verstellbar).
Dazu zeigt die Vorschau den Ausschnitt **um die Fundstelle** und hebt den
Begriff hervor — vorher standen dort stur die ersten 200 Zeichen, und ein
richtiger Treffer sah aus wie ein Fehlgriff.

**Die KI-Antwort ist deutlich schneller.** Das Kontextfenster war fest auf
32768 Token eingestellt; Ollama legt den Zwischenspeicher dafür immer an, auch
wenn der Text kürzer ist. Auf einem Rechner mit 24 GB und einem großen Modell
drückte das ins Auslagern. Jetzt richtet es sich nach dem tatsächlichen Text —
gemessen mehr als doppelt so schnell, ohne dass man etwas umstellt.

**Aufgeräumte Oberfläche.** Die Suchmaske ist eine Suchzeile mit Knopf; die
Filter liegen darunter hinter einem Schalter, der zeigt, wie viele gesetzt
sind. Gesucht wird, wenn man danach fragt — nicht beim Tippen. *Gelöschtes* ist
eine eigene Sicht neben Treffer, Kalender und Adressbuch geworden. Erklärungen
stehen an einem **(i)** statt als Absatz neben jedem Knopf, und Zahlen über das
Archiv stehen nur noch in *Analytics*, nicht zusätzlich im Kopf.

**Vorabversionen sagen es.** Wer eine Fassung benutzt, die neuer ist als das
letzte Release, bekam „Du bist auf dem neuesten Stand" — formal wahr,
inhaltlich falsch. Jetzt steht dort ein Hinweis.

## Welche Datei?

| Datei | Für |
|---|---|
| `Microsoft365-Archiv-macos-arm64.dmg` | Mac mit Apple Silicon (M1–M4) |
| `Microsoft365-Archiv-macos-x86_64.dmg` | Mac mit Intel-Prozessor |
| `Microsoft365-Archiv-windows-x64.zip` | Windows 10/11 (64 Bit) |
| `Microsoft365-Archiv-linux-x64.tar.gz` | Linux (64 Bit, glibc ab 2.35) |

Nicht sicher, welcher Mac? Apfel-Menü → „Über diesen Mac“: steht dort *Apple M…*,
dann `arm64`, sonst `x86_64`.

## Starten

**macOS** — DMG doppelklicken, im geöffneten Fenster `Microsoft365-Archiv.app`
auf den Ordner *Programme* daneben ziehen, das Fenster schließen (auswerfen)
und die App aus *Programme* starten.

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

## Was bis hierher entstanden ist

Vor 3.5.0 gab es eine Reihe von Vorstufen, die nie als Download erschienen sind
— der Kürze halber in einem Absatz:

Aus einer festen Namensliste für auszulassende Ordner wurden geordnete Regeln
auf Pfaden, bei denen die letzte zutreffende gewinnt; der Ordnerbaum wurde dabei
vom Export getrennt und liegt seither als Datei bereit, was den Start eines
Laufs von Minuten auf Sekundenbruchteile bringt. Die Suche lernte den Ordner als
Kriterium, die Namen der Anhänge, den Gesprächsverlauf unter jedem Treffer und
einen Filter für Gelöschtes — denn ein Archiv, das nur wächst, beantwortet die
wichtigste Frage nicht: was war hier einmal und ist jetzt weg? Dazu kamen ein
Analytics-Reiter mit Vollständigkeitsprüfung, ein Adressbuch aus zwei Quellen,
die Wahl zwischen eingefügtem Zugangsschlüssel und richtiger Anmeldung (womit
der Zeitplan erstmals unbeaufsichtigt weiterläuft), drei Oberflächensprachen und
Läufe, die nichts tun, wenn es nichts zu tun gibt.

Was als Nächstes ansteht, steht in `ROADMAP.md`.
