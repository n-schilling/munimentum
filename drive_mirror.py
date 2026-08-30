#!/usr/bin/env python3
"""
drive_mirror.py – the shared mirror core for Graph drives.

To Graph, OneDrive and a SharePoint document library are the same thing: a
drive with a delta feed. This module holds everything a mirror needs – delta
walk, download, inventory (dateien.tsv), tombstones (verschwunden.tsv),
folder tree, completeness check – parametrised by three things the callers
(onedrive_export.py, sharepoint_export.py) supply:

  * the drive base URL (``/me/drive`` or ``/drives/{id}``) on the client,
  * the output folder (one per drive; state files live inside it),
  * a Selection – path rules, size limit, extension include/exclude.

The mirror promise is the same everywhere: the CURRENT version of every
file is kept, deleted files stay here with a tombstone entry. The German
docstrings on the moved functions are original OneDrive history – the
reasoning applies to every drive.
"""

import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote
from concurrent.futures import ThreadPoolExecutor, as_completed

import export_util
import folders
import graph_client
import progress

GRAPH = graph_client.GRAPH

DATEI_DIR = "Dateien"           # Wurzel im Ausgabeordner; die Regeln greifen darauf
BESTAND_DATEI = "dateien.tsv"
DELTA_DATEI = "delta.txt"
GONE_FILE = export_util.GONE_FILE
BERICHT_DATEI = export_util.BERICHT_DATEI

# Netz, Drosselung, Retry und Paging liegen in graph_client.py; eigen bleibt
# nur der Download-Timeout – eine große Datei braucht länger als eine Seite.
TIMEOUT_BYTES = (30, 600)
SEITE = 999                     # $top: eine Seite statt vieler kleiner


class Selection:
    """What the mirror takes: path rules, a size cap, extension filters.

    Everything optional – the empty Selection takes the whole drive. The
    extension lists hold bare lowercase suffixes ("pdf", "docx"); exclude
    wins over include, and an empty include list means "every type".
    """

    def __init__(self, rules=None, max_bytes=0, include_ext=None,
                 exclude_ext=None):
        self.rules = rules or []
        self.max_bytes = max(0, int(max_bytes or 0))
        self.include_ext = {e.lower().lstrip(".") for e in (include_ext or ())
                            if str(e).strip()}
        self.exclude_ext = {e.lower().lstrip(".") for e in (exclude_ext or ())
                            if str(e).strip()}

    def _ext_ok(self, rel):
        ext = rel.rsplit(".", 1)[-1].lower() if "." in rel.rsplit("/", 1)[-1] else ""
        if ext in self.exclude_ext:
            return False
        return not self.include_ext or ext in self.include_ext

    def takes(self, rel, size):
        """One verdict per file – used identically by plan, check and preview."""
        if not folders.gilt(rel, self.rules):
            return False
        if self.max_bytes and int(size or 0) > self.max_bytes:
            return False
        return self._ext_ok(rel)


# ---------------------------------------------------------------------------
# HTTP – der Client kommt aus graph_client.py, eigen ist nur Drive-Spezifik
# ---------------------------------------------------------------------------
class DriveOps:
    """Drive specifics on top of the shared client: download and delta.

    ``drive_base`` names the drive – callers set it (``…/me/drive`` for
    OneDrive, ``…/drives/{id}`` for a SharePoint library).
    """

    drive_base = f"{GRAPH}/me/drive"

    def lade(self, item_id, ziel, geaendert=None):
        """Inhalt einer Datei nach `ziel` schreiben – stückweise, nicht am Stück.

        Eine große Datei komplett in den Speicher zu holen, nur um sie danach
        auf die Platte zu schreiben, wäre auf einem kleinen Rechner der einzige
        Grund, warum ein Lauf scheitert. Geschrieben wird daneben und erst zum
        Schluss umbenannt: ein Abbruch hinterlässt dann keine halbe Datei, die
        beim nächsten Lauf als fertig gälte.
        """
        url = f"{self.drive_base}/items/{item_id}/content"
        r = self.stream(url, timeout=TIMEOUT_BYTES, label=" (Inhalt)")
        ziel.parent.mkdir(parents=True, exist_ok=True)
        tmp = ziel.with_name(ziel.name + ".teil")
        groesse = 0
        with open(tmp, "wb") as f:
            for stueck in r.iter_content(chunk_size=1 << 20):
                if stueck:
                    f.write(stueck)
                    groesse += len(stueck)
        os.replace(tmp, ziel)
        # Die Änderungszeit aus dem Laufwerk übernehmen. Ohne das trüge jede
        # Datei den Zeitpunkt ihres Downloads, und im Index stünden hunderte
        # Dateien mit demselben Datum – eine Sortierung nach Datum wäre
        # wertlos, und ein Neuaufbau des Spiegels änderte sie erneut.
        if geaendert:
            try:
                os.utime(ziel, (geaendert, geaendert))
            except OSError:
                pass
        return groesse

    def delta(self, weiter=None):
        """Alle Delta-Einträge, Seite für Seite. Gibt am Ende den neuen Link."""
        url = weiter or f"{self.drive_base}/root/delta?$top={SEITE}"
        while True:
            d = self.get(url)
            for eintrag in d.get("value", []):
                yield eintrag, None
            nxt = d.get("@odata.nextLink")
            if not nxt:
                yield None, d.get("@odata.deltaLink")
                return
            url = nxt


# ---------------------------------------------------------------------------
# Pfade
# ---------------------------------------------------------------------------
kuerzel = export_util.kuerzel


def safe(name, maxlen=120, kennung=None):
    """Ein Namensstück, dem das Dateisystem trauen kann.

    Das Abschneiden ist die heikle Stelle: es fraß am echten Laufwerk die
    Endung zweier Dateien, deren Namen sich erst nach 120 Zeichen
    unterschieden – beide landeten auf demselben Pfad, und der zweite
    Download scheiterte an der Teildatei, die der erste schon weggeräumt
    hatte. Deshalb bleibt die Endung erhalten und ein Kürzel aus der ID
    macht den Namen wieder eindeutig.
    """
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name or "").strip().strip(". ")
    name = re.sub(r"\s+", " ", name) or "unbenannt"
    if len(name) <= maxlen:
        return name
    stamm, punkt, endung = name.rpartition(".")
    if not punkt or len(endung) > 12 or not stamm:
        stamm, endung = name, ""
    marke = "__" + kuerzel(kennung or name)
    schwanz = ("." + endung) if endung else ""
    platz = max(1, maxlen - len(marke) - len(schwanz))
    return stamm[:platz] + marke + schwanz


def rel_pfad(eintrag):
    """Der Pfad, unter dem ein Eintrag hier landet – "Dateien/Ordner/Datei.pdf".

    Graph liefert den Elternpfad als "/drive/root:/Ordner/Unter", prozentkodiert.
    Jedes Stück wird einzeln entschärft: sonst könnte ein Name mit "/" oder ".."
    darin aus dem Ausgabeordner herausführen.
    """
    roh = (eintrag.get("parentReference") or {}).get("path") or ""
    roh = unquote(roh)
    _, _, rest = roh.partition("root:")
    stuecke = [safe(s) for s in rest.strip("/").split("/") if s not in ("", ".", "..")]
    name = safe(eintrag.get("name") or "unbenannt", kennung=eintrag.get("id"))
    return "/".join([DATEI_DIR, *stuecke, name])


def geaendert_am(eintrag):
    """Wann die Datei zuletzt geändert wurde – als Zeitstempel.

    fileSystemInfo trägt die Zeit des Clients, der sie hochgeladen hat, und ist
    damit die ehrlichere Angabe; lastModifiedDateTime am Eintrag ist der
    Rückfall.
    """
    for roh in ((eintrag.get("fileSystemInfo") or {}).get("lastModifiedDateTime"),
                eintrag.get("lastModifiedDateTime")):
        dt = export_util.graph_zeit(roh)
        if dt is not None:
            return dt.timestamp()
    return None


def ist_ordner(eintrag):
    return "folder" in eintrag and "file" not in eintrag


def ist_paket(eintrag):
    """OneNote-Notizbücher meldet Graph als "package".

    Sie sind Ordner mit .one-Dateien darin; die einzelnen Dateien kommen im
    Delta ohnehin vor und werden gesichert. Das Paket selbst ist kein Inhalt.
    """
    return "package" in eintrag


# ---------------------------------------------------------------------------
# Bestand: ID -> Pfad, Fassung, Größe
# ---------------------------------------------------------------------------
class Bestand:
    """Was hier liegt, je Graph-ID.

    Der cTag ändert sich, wenn sich der INHALT ändert (der eTag auch bei reinen
    Metadaten). Genau das ist die Frage vor jedem Download, also wird er
    gespeichert. Die Größe steht daneben, damit eine abgebrochene Datei nicht
    als vollständig durchgeht.
    """

    def __init__(self, pfad):
        self.pfad = Path(pfad)
        self.eintraege = {}
        self._lock = threading.Lock()
        try:
            for zeile in self.pfad.read_text(encoding="utf-8").splitlines():
                teile = zeile.split("\t")
                if len(teile) >= 4:
                    kennung, rel, ctag, groesse = teile[:4]
                    self.eintraege[kennung] = {
                        "rel": rel, "ctag": ctag,
                        "size": int(groesse) if groesse.isdigit() else -1}
        except OSError:
            pass

    def aktuell(self, kennung, ctag, groesse, wurzel):
        """Liegt genau diese Fassung schon hier?"""
        e = self.eintraege.get(kennung)
        if not e or e["ctag"] != ctag:
            return False
        datei = wurzel / e["rel"]
        try:
            return datei.stat().st_size == groesse
        except OSError:
            return False

    def merke(self, kennung, rel, ctag, groesse):
        with self._lock:
            self.eintraege[kennung] = {"rel": rel, "ctag": ctag, "size": groesse}

    def vergiss(self, kennung):
        with self._lock:
            return self.eintraege.pop(kennung, None)

    def schreibe(self):
        """Atomar – ein Abbruch mitten im Schreiben darf den Bestand nicht
        halbieren, sonst lädt der nächste Lauf alles noch einmal."""
        self.pfad.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.pfad.with_name(self.pfad.name + ".tmp")
        tmp.write_text("".join(
            f'{k}\t{e["rel"]}\t{e["ctag"]}\t{e["size"]}\n'
            for k, e in sorted(self.eintraege.items())), encoding="utf-8")
        tmp.replace(self.pfad)


lies_verschwunden = export_util.lies_verschwunden
schreibe_verschwunden = export_util.schreibe_verschwunden
schreibe_bericht = export_util.schreibe_bericht


def lies_delta(out):
    try:
        return (Path(out) / DELTA_DATEI).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def schreibe_delta(out, link):
    if link:
        export_util.schreibe_atomar(Path(out) / DELTA_DATEI, link)


# ---------------------------------------------------------------------------
# Der Lauf
# ---------------------------------------------------------------------------
def sammle(graph, weiter):
    """Delta einmal durchlaufen; liefert (Einträge, neuer Link)."""
    eintraege, link = [], None
    for eintrag, fertig in graph.delta(weiter):
        if eintrag is not None:
            eintraege.append(eintrag)
        else:
            link = fertig
    return eintraege, link


def plane(eintraege, bestand, wurzel, auswahl):
    """Aus den Delta-Einträgen wird eine Aufgabenliste – ohne Netzzugriff.

    Getrennt vom Herunterladen, damit die Entscheidung „was tun wir" ohne
    Anmeldung geprüft werden kann.
    """
    laden, verschoben, geloescht, ausgelassen, baum = [], [], [], 0, []
    entfernt = set()
    for e in eintraege:
        kennung = e.get("id")
        if not kennung:
            continue
        if "deleted" in e:
            entfernt.add(kennung)
            alt = bestand.vergiss(kennung)
            if alt:
                geloescht.append(alt["rel"])
            continue
        if "root" in e:
            # Die Wurzel gehört in den Baum, obwohl Graph sie nicht als Ordner
            # meldet. Fehlt sie, gilt jede Datei, die direkt im Laufwerk liegt,
            # als „nur noch lokal vorhanden" – ein Fehlalarm in der Exportliste.
            baum.append({"id": kennung, "pfad": DATEI_DIR, "name": DATEI_DIR,
                         "elemente": int((e.get("folder") or {}).get("childCount") or 0)})
            continue
        rel = rel_pfad(e)
        if ist_ordner(e) or ist_paket(e):
            baum.append({"id": kennung, "pfad": rel, "name": e.get("name") or "",
                         "elemente": int((e.get("folder") or {}).get("childCount") or 0)})
            continue
        if "file" not in e:
            continue
        groesse = int(e.get("size") or 0)
        if not auswahl.takes(rel, groesse):
            ausgelassen += 1
            continue
        alt = bestand.eintraege.get(kennung)
        if alt and alt["rel"] != rel:
            # Umbenennen und Verschieben behalten die ID; der cTag ändert sich
            # nur beim INHALT. Deshalb reicht es, die Datei lokal mitzuziehen –
            # sie noch einmal zu laden wäre bei einem umbenannten 300-MB-Video
            # der teuerste denkbare Weg, nichts zu gewinnen. Hat sich der Inhalt
            # zugleich geändert, steht sie unten trotzdem in `laden`, und der
            # Download landet dann schon auf dem neuen Pfad.
            verschoben.append((alt["rel"], rel))
        if bestand.aktuell(kennung, e.get("cTag") or "", groesse, wurzel):
            if alt and alt["rel"] != rel:
                bestand.merke(kennung, rel, alt["ctag"], alt["size"])
            continue
        laden.append({"id": kennung, "rel": rel, "ctag": e.get("cTag") or "",
                      "size": groesse, "mtime": geaendert_am(e)})
    return {"laden": laden, "verschoben": verschoben, "geloescht": geloescht,
            "ausgelassen": ausgelassen, "baum": baum, "entfernt": entfernt}


def baum_zusammenfuehren(alt, geaendert, entfernt):
    """Den Ordnerbaum fortschreiben statt ersetzen.

    Ein Delta-Lauf liefert nur die GEÄNDERTEN Ordner. Den Baum damit zu
    überschreiben, hieße ihn beim zweiten Lauf auf eine Handvoll zu kürzen –
    und alle übrigen Ordner als verschwunden zu melden.
    """
    nach_id = {e["id"]: e for e in (alt or {}).get("ordner", [])}
    for e in geaendert:
        nach_id[e["id"]] = e
    for kennung in entfernt or ():
        nach_id.pop(kennung, None)
    return sorted(nach_id.values(), key=lambda e: e["pfad"].lower())


def verschiebe(wurzel, paare):
    """Umbenannte oder verschobene Dateien mitziehen statt neu zu laden."""
    bewegt = 0
    for alt, neu in paare:
        a, n = wurzel / alt, wurzel / neu
        if a.exists() and not n.exists():
            n.parent.mkdir(parents=True, exist_ok=True)
            try:
                a.replace(n)
                bewegt += 1
            except OSError:
                pass
    return bewegt


def hole_alle(graph, wurzel, bestand, aufgaben, arbeiter):
    """Die geplanten Downloads – parallel, mit Fortschritt."""
    fertig = fehler = 0
    gesamt = len(aufgaben)
    if not gesamt:
        return 0, 0
    progress.melde(0, gesamt, "files")
    with ThreadPoolExecutor(max_workers=arbeiter) as pool:
        auftrag = {pool.submit(graph.lade, a["id"], wurzel / a["rel"], a.get("mtime")): a
                   for a in aufgaben}
        for f in as_completed(auftrag):
            a = auftrag[f]
            try:
                geladen = f.result()
                bestand.merke(a["id"], a["rel"], a["ctag"], geladen)
                fertig += 1
            except Exception as e:
                fehler += 1
                progress.event("run.file_failed", "err", name=a["rel"],
                               error=f"{type(e).__name__}: {e}")
            if (fertig + fehler) % 10 == 0 or fertig + fehler == gesamt:
                progress.melde(fertig + fehler, gesamt, "files")
                bestand.schreibe()
    bestand.schreibe()
    return fertig, fehler


def lauf(graph, out, auswahl, arbeiter, still=False):
    """One full mirror pass for one drive; returns the result counts.

    ``still`` suppresses the result event – sharepoint_export mirrors several
    drives in one step and reports one combined result at the end.
    """
    out = Path(out)
    wurzel = out
    bestand = Bestand(out / BESTAND_DATEI)
    weiter = lies_delta(out)
    progress.event("run.drive.delta" if weiter else "run.drive.full")

    eintraege, neuer_link = sammle(graph, weiter)
    progress.event("run.scanned", n=len(eintraege),
                   unit=progress.atom("progress.unit.entries"))
    plan = plane(eintraege, bestand, wurzel, auswahl)

    bewegt = verschiebe(wurzel, plan["verschoben"])
    fertig, fehler = hole_alle(graph, wurzel, bestand, plan["laden"], arbeiter)
    # Immer, nicht nur nach Downloads: auch eine Löschung oder eine Verschiebung
    # ändert den Bestand. Ohne dieses Schreiben stünde eine gelöschte Datei beim
    # nächsten Lauf noch drin und ihr Grabstein würde ein zweites Mal gesetzt.
    bestand.schreibe()

    jetzt = datetime.now(UTC).isoformat(timespec="seconds")
    schreibe_verschwunden(out / GONE_FILE, lies_verschwunden(out / GONE_FILE),
                          plan["geloescht"], jetzt)
    alt_baum = folders.lade(out)
    baum = baum_zusammenfuehren(alt_baum, plan["baum"], plan["entfernt"])
    if baum or alt_baum:
        folders.speichere(out, baum, alt_baum)
    # Erst jetzt: ein abgebrochener Lauf darf den Delta-Zeiger nicht vorrücken.
    if not fehler:
        schreibe_delta(out, neuer_link)

    if fehler:
        progress.event("run.drive.retry_full", "warn")
    zahlen = {"new": fertig, "excluded": plan["ausgelassen"], "errors": fehler,
              "moved": bewegt, "gone": len(plan["geloescht"])}
    if not still:
        progress.ergebnis(fertig, excluded=plan["ausgelassen"], errors=fehler,
                          extra={"moved": bewegt, "gone": len(plan["geloescht"])})
    return zahlen


def pruefe_vollstaendigkeit(eintraege, out, auswahl):
    """Was das Laufwerk hat gegen das, was hier liegt – je Ordner.

    Dieselbe Form wie beim Postfach, damit die Oberfläche sie ohne eine zweite
    Ansicht zeichnen kann. Der Unterschied steckt in der Frage: beim Postfach
    zählt Graph die Elemente je Ordner, hier kennt das Delta jede einzelne
    Datei – die Prüfung ist deshalb genauer und weiß auch, ob eine Datei nur
    halb angekommen ist. Die Byte-Summen daneben machen sie zugleich zur
    Größen-Vorschau: was würde ein Spiegel-Lauf holen, was ließe er aus.
    """
    weg = lies_verschwunden(Path(out) / GONE_FILE)
    je = {}
    typen = {}
    ausgelassen = 0
    ausgelassen_bytes = 0
    ausgelassene = set()
    for e in eintraege:
        if "deleted" in e or "file" not in e or "root" in e:
            continue
        rel = rel_pfad(e)
        ordner = rel.rsplit("/", 1)[0] if "/" in rel else DATEI_DIR
        groesse = int(e.get("size") or 0)
        # Counted BEFORE the filters: this list is what include/exclude gets
        # decided on, so it must show what is there, not what survived.
        name = rel.rsplit("/", 1)[-1]
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        z = typen.setdefault(ext, {"ext": ext, "n": 0, "bytes": 0})
        z["n"] += 1
        z["bytes"] += groesse
        if not auswahl.takes(rel, groesse):
            ausgelassen += 1
            ausgelassen_bytes += groesse
            ausgelassene.add(ordner)
            continue
        z = je.setdefault(ordner, {"ordner": ordner, "erwartet": 0, "vorhanden": 0,
                                   "geloescht": 0, "ausgelassen": False, "fehlt": 0,
                                   "bytes": 0})
        z["erwartet"] += 1
        z["bytes"] += groesse
        datei = Path(out) / rel
        try:
            da = datei.stat().st_size == groesse
        except OSError:
            da = False
        if da:
            z["vorhanden"] += 1
    # Grabsteine gehören zur Bilanz: sie erklären, warum hier mehr liegt als
    # das Laufwerk noch kennt – eine Lücke sind sie nicht.
    for rel in weg:
        ordner = rel.rsplit("/", 1)[0] if "/" in rel else DATEI_DIR
        z = je.setdefault(ordner, {"ordner": ordner, "erwartet": 0, "vorhanden": 0,
                                   "geloescht": 0, "ausgelassen": False, "fehlt": 0,
                                   "bytes": 0})
        z["geloescht"] += 1
    for o in ausgelassene:
        je.setdefault(o, {"ordner": o, "erwartet": 0, "vorhanden": 0,
                          "geloescht": 0, "ausgelassen": True, "fehlt": 0,
                          "bytes": 0})
    for z in je.values():
        z["fehlt"] = max(0, z["erwartet"] - z["vorhanden"])
    liste = sorted(je.values(), key=lambda z: (-z["fehlt"], z["ordner"]))
    return {
        "geprueft": datetime.now(UTC).isoformat(timespec="seconds"),
        "ordner": liste,
        "erwartet": sum(z["erwartet"] for z in liste),
        "vorhanden": sum(z["vorhanden"] for z in liste),
        "geloescht": sum(z["geloescht"] for z in liste),
        "fehlt": sum(z["fehlt"] for z in liste),
        "ausgelassen": ausgelassen,
        "ausgelassene_ordner": sorted(ausgelassene)[:20],
        "bytes": sum(z["bytes"] for z in liste),
        "bytes_ausgelassen": ausgelassen_bytes,
        "typen": sorted(typen.values(), key=lambda z: -z["bytes"]),
    }


def nur_pruefen(graph, out, auswahl, still=False):
    """--check: nur melden, was fehlt. Lädt nichts und rührt den Zeiger nicht an."""
    out = Path(out)
    eintraege, _ = sammle(graph, None)
    bericht = pruefe_vollstaendigkeit(eintraege, out, auswahl)
    schreibe_bericht(out, bericht)
    if not still:
        progress.ergebnis(0, excluded=bericht["ausgelassen"],
                          extra={"expected": bericht["erwartet"],
                                 "present": bericht["vorhanden"],
                                 "missing": bericht["fehlt"]})
    return bericht


def nur_ordner(graph, out, auswahl, still=False):
    """--folders: nur die Struktur holen, nichts herunterladen.

    Zählt bewusst VOLLSTÄNDIG auf und ignoriert den gespeicherten Delta-Zeiger:
    ein Abgleich soll den ganzen Baum zeigen, nicht die Handvoll Ordner, die
    sich seit gestern geändert haben. Und er rückt den Zeiger nicht vor – sonst
    hielte der nächste Export die noch nie geholten Dateien für erledigt.
    """
    out = Path(out)
    eintraege, _ = sammle(graph, None)
    bestand = Bestand(out / BESTAND_DATEI)
    plan = plane(eintraege, bestand, out, auswahl)
    alt = folders.lade(out)
    daten = folders.speichere(out, baum_zusammenfuehren(alt, plan["baum"],
                                                        plan["entfernt"]), alt)
    z = folders.zusammenfassung(daten, auswahl.rules)
    progress.event("run.sync.result", total=z["ordner_gesamt"],
                   chosen=z["ordner_gewaehlt"],
                   unit=progress.atom("progress.unit.folders"))
    if daten["neu"] or daten["verschwunden"] or daten["umbenannt"]:
        progress.event("run.sync.changed", new=len(daten["neu"]),
                       gone=len(daten["verschwunden"]),
                       renamed=len(daten["umbenannt"]))
    if not still:
        progress.ergebnis(len(daten["neu"]),
                          extra={"total": z["ordner_gesamt"],
                                 "gone": len(daten["verschwunden"]),
                                 "renamed": len(daten["umbenannt"])})
    return daten
