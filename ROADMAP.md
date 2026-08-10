# Was als Nächstes kommt

Kurze Liste — was ansteht und was bewusst fehlt, damit später niemand rätselt,
ob etwas vergessen wurde oder gewollt draußen ist. Wie es gebaut wird, steht
nicht hier, sondern im Code.

## Als Nächstes: Inhalte durchsuchen

Heute stehen **Namen** im Index: `att:pdf` findet die Mail mit dem PDF,
`Vertrag_Musterkunde.pdf` die eine, und seit dem OneDrive-Spiegel gilt dasselbe
für die Dateien auf dem Laufwerk. Was fehlt, ist der **Inhalt** — ein Vertrag
liegt im Archiv, sein Text ist unsichtbar.

Das betrifft beide Quellen gemeinsam und wird deshalb ein Schritt: Anhänge aus
den `.eml` und gespiegelte OneDrive-Dateien laufen durch dieselbe Extraktion.
Vorbereitet ist es — `text` trägt heute den Pfad und ist genau das Feld, das
eine Extraktion später füllt. Ein bestehender Index wird dadurch nicht ungültig,
nur reicher.

Drei Dinge sind daran entschieden:

* **Ein eigener, abschaltbarer Schritt.** Auf einem echten Archiv dauert das
  rund eine Stunde, einmalig. Das läuft nicht nebenbei bei jedem Export mit.
* **Zwischenspeicher nach Inhalts-Hash, nicht nach Pfad.** Dieselbe Datei in
  zwölf Mails kostet dann eine Extraktion, und beide Quellen teilen sich den
  Speicher.
* **Nicht alles.** Rund 40 % der Anhänge sind Bilder, überwiegend
  Signaturlogos. Ein Typfilter gehört dazu.

Offen ist die Werkzeugfrage: [markitdown](https://github.com/microsoft/markitdown)
nimmt einem die Vollständigkeit ab (Tabellen, Notizen, Kopfzeilen), zieht aber
einen Abhängigkeitsbaum nach, der das Bündel von 23 auf geschätzt 65 MB bringen
würde. Die schlanke Alternative aus einzelnen Bibliotheken ist ein Viertel so
groß, dafür pflegt man die Vollständigkeit selbst. Entschieden wird das an
einem echten Bündel, nicht am Schreibtisch.

## Signierte macOS-Bündel

Der Developer-Account ist da, die Anleitung steht in `packaging/signieren.md` —
umgesetzt ist sie noch nicht. Solange bleibt beim ersten Start die Warnung
*„Apple konnte nicht überprüfen …"*, und die Release-Hinweise erklären den Weg
daran vorbei. Der Aufwand steckt weniger im Signieren als in den
Entitlements: Hardened Runtime ist Pflicht für die Beglaubigung und schaltet
genau das ab, was CPython braucht.

## Kleineres

* **Datenordner aufteilen.** Massendaten und Index haben ganz verschiedene
  Ansprüche — die `.eml` dürfen auf einer langsamen Platte liegen, der Index
  nicht. Getrennte Pfade sind heute schon möglich, aber nirgends erklärt.
* **Der Graph-Client steckt dreimal im Projekt**, in jedem Exportskript einmal.
  Die drei sind inzwischen wirklich verschieden; zusammenzulegen ist ein
  eigener Umbau, kein Aufräumen nebenbei.

## Entschieden

**OneDrive ist ein Spiegel, kein Versionsarchiv.** Gehalten wird die jeweils
aktuelle Fassung; in OneDrive Gelöschtes bleibt liegen und bekommt einen
Vermerk. Frühere Fassungen einer geänderten Datei sind weg — das steht hier,
damit es später niemand für einen Fehler hält.
