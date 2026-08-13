# Was als Nächstes kommt

Kurze Liste — was ansteht und was bewusst fehlt, damit später niemand rätselt,
ob etwas vergessen wurde oder gewollt draußen ist. Wie es gebaut wird, steht
nicht hier, sondern im Code.

## Als Nächstes: Inhalte durchsuchen

Heute stehen **Namen** im Index: der Dateityp-Filter findet die Mails mit einem
PDF, `Vertrag_Musterkunde.pdf` die eine, und seit dem OneDrive-Spiegel gilt
dasselbe für die Dateien auf dem Laufwerk. Was fehlt, ist der **Inhalt** — ein
Vertrag liegt im Archiv, sein Text ist unsichtbar.

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
einen Abhängigkeitsbaum nach, der das Bündel von 27 MB auf ein Vielfaches
brächte. Die schlanke Alternative aus einzelnen Bibliotheken ist deutlich
kleiner, dafür pflegt man die Vollständigkeit selbst. Entschieden wird das an
einem echten Bündel, nicht am Schreibtisch.

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

**Ein zweiter Modellserver käme über die Schnittstelle, nicht über den Namen.**
Also ein Feld *Art des Servers* mit `openai-kompatibel`, das LM Studio,
`llama-server`, Jan, LocalAI und vLLM auf einmal öffnet — Ollama spricht diese
API unter `/v1/` ebenfalls. Betroffen sind drei Aufrufe (`/api/embed` in
`rag_index.py` und `mcp_server.py`, `/api/chat` in `answer.py`). Nur `/api/tags`
lässt sich nicht übersetzen: Die Hilfe beim Nachladen eines fehlenden Modells
bliebe Ollama vorbehalten. Der Einstellungsabschnitt heißt seit 5.3.0 **KI**,
damit der Schalter darin später eine Auswahl werden kann.

**MLX lohnt nicht** — gemessen, nicht vermutet: Einbetten 0,66× (also langsamer
als Ollama), Chat 1,1× bei freier Generierung und ±0 bei langem Kontext, dem
Muster der KI-Antwort. `mlx_lm.server` hat gar keinen Embeddings-Endpunkt;
eingebettet stünden 408 MB Abhängigkeiten gegen ein 27-MB-Bündel, dazu ein
selbstgebautes Modellmanagement.

**Beim Einbetten ist nichts mehr zu holen.** Mehr Parallelität, kleinerer
Kontext, Stapel über 128, eine kleinere Quantisierung von bge-m3: gemessen und
ohne Gewinn oder von Ollama abgelehnt. Was ging, ist drin — kurze Chunks ohne
Vektor, Stapel nach Länge, 128 als Vorgabe. Der Boden ist das Modell selbst; ein
kleineres wäre doppelt so schnell und schlechter im Deutschen.
