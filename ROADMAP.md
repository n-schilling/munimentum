# Vorgemerkt

Kurze Liste dessen, was bewusst noch nicht drin ist — mit dem Grund, damit
später niemand rätselt, ob es vergessen wurde oder gewollt fehlt.

Die Reihenfolge ist Absicht: **V4 legt die Dateisuche an, V5 hängt OneDrive als
zweiten Lieferanten daran.** Andersherum entstünde eine Sync-Maschinerie für
Inhalte, die man noch nicht durchsuchen kann.

## V4: Dateisuche (markitdown)

Seit 2.1.0 stehen die **Namen** der Anhänge im Index — `att:pdf` findet die
Mail, `Vertrag_Musterkunde.pdf` auch. Was fehlt, ist der **Inhalt**: ein PDF liegt
im Archiv, sein Text ist unsichtbar.

Das Entscheidende dabei: dafür ist keine einzige Graph-Anfrage nötig. Die
`.eml` tragen die vollständige MIME, Anhänge inklusive — der Stoff liegt schon
auf der Platte. An einem echten Archiv gemessen (Stand 10.08.2026, Stichprobe von 500
Mails mit Anhang, echt geparst):

| | |
|---|---|
| Mails mit Anhang | rund 4.300 |
| Anhänge hochgerechnet | ~10.000, davon ~7.000 verschiedene |
| Text-tragend (PDF/Office) | 50 % der Stücke, **86 % der Bytes** |
| Bilder (Signaturlogos usw.) | 40 % der Stücke, 5 % der Bytes |
| **Zu extrahieren** | **~4.800 verschiedene Dokumente** |
| Anhangsinhalt gesamt | gut 5 GB, lokal |

Die 28 % Dubletten über alle Anhänge sind fast ausschließlich Bilder. Unter den
Text-Dokumenten sind es nur **4,5 %** — der Hash-Zwischenspeicher unten spart
also weniger, als die Gesamtzahl vermuten lässt, und bleibt trotzdem richtig
(er trägt in V5 die zweite Quelle mit).

Werkzeug: **[microsoft/markitdown](https://github.com/microsoft/markitdown)**.
Es deckt in einem Paket ab, wofür sonst ein halbes Dutzend Bibliotheken nötig
wäre — PDF, Word, Excel, PowerPoint, HTML, Bilder mit Text, Audio-Transkripte —
und liefert Markdown, das sich ohne Umweg chunken und einbetten lässt.

Der Anschlusspunkt steht: `corpus.anhaenge()` läuft ohnehin über jeden Anhang.

### Ein Satz je Datei, nicht ein Feld an der Mail

Die heutige `att`-Spalte fügt Namen mit Leerzeichen zusammen — und Dateinamen
enthalten selbst welche, sodass `'Angebot - Wartung und Support.docx'` nicht mehr
als *ein* Name erkennbar ist. Für die Volltextsuche ist das folgenlos (Token
sind Token), für „zeig mir die Anhänge dieser Mail" nicht. Ein eigener Satz je
Datei behebt das nebenbei.

Der Satzbau des Korpus trägt das ohne Schemaänderung — `src` kennt heute schon
vier Werte, ein fünfter ist keine Umstellung:

| Feld | bei einem Anhang | bei einer OneDrive-Datei (V5) |
|---|---|---|
| `src` | `datei` | `datei` |
| `ctx` | die Mail, in der er ankam | der Ordner |
| `ts` | Datum der Mail | zuletzt geändert |
| `ppl` | **Absender und Empfänger** | Besitzer, geteilt mit |
| `title` | Dateiname | Dateiname |
| `text` | extrahiert | extrahiert |

Der Anhang ist dabei der reichere Satz: er weiß, wer ihn wem in welchem
Gespräch geschickt hat. „Wer hat mir diesen Vertrag geschickt" beantwortet nur
diese Seite.

### Laufzeit: gemessen, nicht geschätzt

An 60 echten Anhängen aus einem solchen Archiv (40 PDF, 7 PPTX, 7 DOCX,
5 XLSX, 1 CSV) und mit `bge-m3` auf einem Arbeitsrechner mit Apple Silicon,
10.08.2026:

| Schritt | Messung | Hochgerechnet auf ~4.800 Dokumente |
|---|---|---|
| Extraktion (markitdown) | 0,21 s je Dokument (Median 0,14, längstes 1,4) | **~17 Min** |
| Chunks daraus | 9,8 je Dokument | **~47.000** (+17 % auf rund 270.000) |
| Einbetten | 18 Chunks/s | **~44 Min** |
| Zusammen | | **rund eine Stunde, einmalig** |

Danach kostet ein Neuaufbau nichts mehr, sofern der Zwischenspeicher steht.
Der Index wächst dabei um rund ein Fünftel — bei einem Bestand dieser
Größenordnung gut 150 MB auf knapp ein Gigabyte. Unkritisch.

**Verlässlichkeit:** 0 Fehlschläge bei 60 Dokumenten. Genau eines lieferte
keinen Text — ein eingescanntes PDF. Solche gehören erkannt und vermerkt, nicht
still übergangen.

### Die offene Werkzeugfrage: markitdown oder die Bibliotheken selbst

Der Abhängigkeitsbaum ist der Punkt, an dem es weh tut. Gemessen als
`site-packages`, heute liegt das Projekt bei 72 MB:

| | Größe | je Dokument | Anmerkung |
|---|---|---|---|
| `markitdown[pdf,docx,xlsx,pptx]` | **209 MB** | 0,21 s | eine Schnittstelle, Tabellen und Notizen inklusive |
| pypdfium2 + python-docx/pptx + openpyxl | **46 MB** | 0,06 s | Vollständigkeit schreibt man selbst |

Zwei Versuche, die die Wahl schärfen:

* **`magika` lässt sich nicht abwählen.** markitdown importiert ohne es gar
  nicht — und `magika` bringt **onnxruntime, 70 MB**, mit. Das ist ein
  neuronales Netz, das Dateitypen *errät*; bei einem Mailanhang stehen Typ und
  Name aber ohnehin in der MIME. 70 MB für eine Frage, die schon beantwortet
  ist.
* **Ohne `pandas` (40 MB) fällt XLSX aus.** Auch das ist nicht abwählbar.

Der direkte Weg ist kleiner und dreimal schneller — aber ein schnell
hingeschriebener Extraktor holte in diesem Versuch aus DOCX und PPTX **40–70 %
weniger Text** als markitdown, weil er Tabellen, Notizen und Kopfzeilen
übersprang. Der Unterschied liegt also nicht an den Bibliotheken, sondern
daran, wer die Vollständigkeit pflegt.

Beim mit Abstand häufigsten Typ ist der Abstand klein: bei den 40 PDF lieferte
der direkte Weg 487.000 gegen 532.000 Zeichen (−8 %). Wenn PDF und CSV den
Ausschlag geben, ist der schlanke Weg vertretbar; sobald Office-Dokumente
ernsthaft zählen, spart markitdown die Arbeit, die man sonst selbst macht.

**Noch zu klären**, bevor das entschieden wird: wie sich 209 MB
`site-packages` auf das fertige Bündel auswirken. Heute werden aus 72 MB ein
23,5-MB-Bündel (macOS arm64) — bei gleichem Verhältnis wären es rund 65 MB.

### Was sonst noch Arbeit ist

* **Eigener Schritt.** Eine Stunde läuft nicht nebenbei mit. Das braucht einen
  abschaltbaren Schritt mit Fortschritt, so wie die Wiederherstellung
  gelöschter Termine einen hat.
* **Zwischenspeicher.** Schlüssel ist der **Inhalts-Hash, nicht der Pfad** —
  dann kostet dieselbe PDF in zwölf Mails eines Verlaufs genau eine
  Extraktion, und in V5 teilt OneDrive denselben Speicher.
* **Auswahl.** 40 % der Anhänge sind Bilder, überwiegend Signaturlogos. Sie zu
  extrahieren kostet Zeit und bringt Rauschen; ein Typfilter gehört dazu.

Die Namen im Index waren deshalb bewusst Stufe eins: Sie kosten nichts und
schließen den größten Teil der Lücke. Stufe zwei ist ein eigenes Vorhaben —
aber ein absehbares.

## V5: OneDrive als zweite Quelle

### Entschieden: Spiegel, kein Versionsarchiv

Gehalten wird **die jeweils aktuelle Fassung** jeder Datei. Ändert sie sich,
wird sie überschrieben. Wird sie in OneDrive gelöscht, **bleibt sie lokal
liegen** und bekommt einen Vermerk — dieselbe Zusage wie bei Mails, wo
`verschwunden.tsv` und die Spalte `gone` das seit 2.0 leisten.

Die Kehrseite gehört ausgesprochen, damit sie später niemand für einen Fehler
hält: **frühere Fassungen einer geänderten Datei sind weg.** Das Archiv
beantwortet „was war hier einmal und ist jetzt gelöscht", nicht „wie sah dieses
Dokument im März aus". Versionsverwaltung wäre ein eigenes Vorhaben mit eigener
Speicherrechnung; sie steht bewusst nicht an.

### Warum es nach V4 deutlich billiger ist

Vieles ist seit 3.0/3.1 schon da:

* **Anmeldung, Fortschritt, Einstellungen, Bündelung** teilt sich ein
  `onedrive_export.py` mit den beiden bestehenden Skripten — das war die
  Aufräumarbeit in 3.0 (`auth.py`).
* **Die Ordnerregeln passen unverändert.** `folders.gilt()` ist reines
  Pfad-Matching; nur `aus_namensliste()` kennt das Präfix `E-Mail/`, und das ist
  der Migrationspfad, nicht die Mechanik. Bei einem Datenbestand, der
  hundertmal größer sein kann als ein Postfach, ist eine funktionierende
  Auswahl keine Bequemlichkeit, sondern Voraussetzung.
* **`/me/drive/root/delta`** liefert Änderungen *und Löschungen* mit einem
  Token. Für Mails muss heute jeder Verdacht einzeln per 404 nachgefragt
  werden — hier ist der Grabstein billiger zu haben als im Postfach.
* **Berechtigung.** `Files.Read.All` ist ein Eintrag mehr in `SCOPE_FOR`; im
  Graph-Explorer-Schlüssel ist der Umfang ohnehin schon enthalten.

Bleibt der eigentliche Aufwand: ein Exportskript in der Größenordnung von
`teams_export.py` (~1.100 Zeilen), plus die Umstellung von „einmal geholt, nie
wieder angesehen" (`DoneLog`) auf „geholt und auf Änderung geprüft". Das ist
der Punkt, an dem sich Dateien von Mails unterscheiden — eine Mail ändert sich
nie, eine Datei ständig, unter derselben ID.

### Der Umfang ist gemessen — und viel kleiner als befürchtet

Auf einem geschäftlichen Konto (`GET /me/drive`, 10.08.2026): **gut 3 GB
belegt bei 1 TB Kontingent**.

Das räumt die größte Sorge ab — jedenfalls für diesen Fall. Neben einem
Mail- und Teams-Archiv von gut 30 GB käme OneDrive mit rund einem Zehntel
dazu, nicht mit dem Zehnfachen. Damit gilt dort:

* Die Ordnerauswahl ist **Bequemlichkeit, nicht Voraussetzung** — ein
  vollständiger Spiegel ist bei dieser Größe unbedenklich.
* Ein erster vollständiger Abzug ist eine überschaubare Übertragung, kein
  Vorhaben über Nacht.

Wer das Werkzeug mit einem gefüllten Laufwerk benutzt, steht anders da. Die
Auswahl gehört deshalb trotzdem von Anfang an dazu — sie kostet nichts, weil
`folders.py` sie schon kann.

### Offen

* **Bilder und Rohdaten.** Auch ein kleines OneDrive besteht in Teilen aus
  Material ohne Text. Herunterladen kostet Platz, Indizieren bringt nichts —
  der Typfilter aus V4 muss hier ebenfalls greifen.
* **Änderungserkennung.** Ob `cTag` allein reicht oder Größe und
  Änderungszeitpunkt danebengelegt werden müssen, damit ein Spiegel nicht
  ständig dieselbe Datei neu holt. Das entscheidet sich am lebenden Laufwerk,
  nicht am Schreibtisch.

## Bewusst draußen

* **Mehrere Konten.** Vervielfacht Angriffsfläche und Pflege, ohne das
  Kernversprechen zu verbessern.
* **SharePoint und „Geteilt mit mir".** OneDrive meint hier das *eigene*
  Laufwerk. Fremde Bibliotheken bringen Fragen nach Besitz und Berechtigung
  mit, die das Werkzeug nicht beantworten will.
* **Versionsverwaltung für Dateien.** Siehe die Entscheidung unter V5: der
  Spiegel hält die aktuelle Fassung, das Archiv hält Gelöschtes fest. Frühere
  Fassungen sind eine dritte Zusage und wären eine eigene Speicherrechnung.
* **Cloud-Sync.** Widerspricht dem Punkt, dass nichts den Rechner verlässt.
