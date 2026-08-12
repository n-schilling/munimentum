# Was als Nächstes kommt

Kurze Liste — was ansteht und was bewusst fehlt, damit später niemand rätselt,
ob etwas vergessen wurde oder gewollt draußen ist. Wie es gebaut wird, steht
nicht hier, sondern im Code.

## Verschobene Mails gelten als gelöscht

Der Filter *Gelöschtes* zeigt heute auch Mails, die nur in einen anderen Ordner
gewandert sind — und zwar den überwiegenden Teil. Die Absicht war eine andere:
Was aus dem Postfach verschwunden scheint, wird bei Microsoft nachgefragt, und
nur ein 404 zählt als Löschung. Der Haken liegt darunter: Exchange vergibt beim
**Verschieben eine neue Nachrichten-ID**. Unter der gespeicherten ist die Mail
danach nicht mehr auffindbar, die Rückfrage antwortet 404, und die Prüfung
kommt zum falschen Schluss.

Die Lösung ist vorgesehen: Graph liefert auf `Prefer: IdType="ImmutableId"`
Kennungen, die ein Verschieben überstehen. Sie anzufordern ist eine Zeile — der
Umbau steckt woanders:

* **Die bereits gespeicherten Schlüssel sind von der alten Sorte.** `exported.tsv`
  führt eine Zeile je fertiger Mail; nach der Umstellung passt keine davon mehr,
  und ein Lauf ohne Übergang lüde das ganze Postfach erneut herunter.
* **Die bisherigen Löschvermerke sind unzuverlässig.** Sie müssten einmal neu
  geprüft werden, statt als Wahrheit stehenzubleiben.

Bis dahin sagt das (i) am Filter, was er wirklich zeigt — eine falsche Zahl
kommentarlos anzuzeigen wäre schlechter als eine erklärte.

## Als Nächstes: Inhalte durchsuchen

Heute stehen **Namen** im Index: der Dateityp-Filter findet die Mails mit einem
PDF, `Vertrag_Musterkunde.pdf` die eine, und seit dem OneDrive-Spiegel gilt
dasselbe für die Dateien auf dem Laufwerk. Was fehlt, ist der **Inhalt** — ein Vertrag
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

## Ein zweiter Modellserver neben Ollama

Ollama ist heute die einzige Art, an Sprachmodelle zu kommen. Das ist keine
Festlegung, sondern der Stand: Der Abschnitt in den Einstellungen heißt seit
5.3.0 **KI** und nicht mehr *Ollama*, damit der Schalter darin später eine
Auswahl werden kann, ohne dass die Überschrift wieder wandert.

Entschieden ist die Richtung: **nicht „Ollama oder X"**, sondern die
Schnittstelle. LM Studio, `llama-server`, Jan, LocalAI und vLLM sprechen alle
die OpenAI-kompatible API — und Ollama tut es unter `/v1/` ebenfalls. Ein
Adapter auf `POST /v1/embeddings` und `POST /v1/chat/completions` öffnet damit
alle auf einmal, statt neben Ollama eine zweite Sonderbehandlung zu bauen. In
den Einstellungen wäre das ein Feld *Art des Servers*: `ollama` (heutiges
Verhalten) oder `openai-kompatibel` (Adresse und optionaler Schlüssel).

Berührt sind genau drei Stellen — `/api/embed` in `rag_index.py` und
`mcp_server.py`, `/api/chat` in `answer.py`. Die vierte ist die, die sich nicht
übersetzen lässt: `/api/tags` beantwortet „ist dieses Modell hier geladen?“,
und OpenAI-seitig gibt es dafür nur `/v1/models`, das auflistet, was der Server
anbietet. Für die Statusanzeige neben den Feldern reicht das; die Hilfe beim
Nachladen eines fehlenden Modells (`ollama pull`) bliebe Ollama vorbehalten und
müsste dort ausgeblendet werden, statt ins Leere zu zeigen.

Nur auf Apple Silicon wäre MLX deutlich schneller. Es steht hier trotzdem nicht
als eigener Punkt: ein Weg, den es auf zwei von drei Plattformen nicht gibt,
kostet mehr an Erklärung, als er an Geschwindigkeit bringt.

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
