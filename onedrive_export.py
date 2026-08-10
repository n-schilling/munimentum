#!/usr/bin/env python3
"""
OneDrive-Export: das eigene Laufwerk als lokaler Spiegel.

Was das heißt, steht in ROADMAP.md und sei hier wiederholt, weil man es beim
Lesen des Codes sonst falsch erwartet:

  Gehalten wird die JEWEILS AKTUELLE Fassung jeder Datei. Ändert sie sich,
  wird sie überschrieben – frühere Fassungen bewahrt dieser Spiegel nicht.

  Wird eine Datei in OneDrive gelöscht, BLEIBT sie hier liegen und bekommt
  einen Vermerk in verschwunden.tsv. Dieselbe Zusage wie beim Postfach: ein
  Archiv, das nur wächst, beantwortet die wichtigste Frage nicht – was war
  hier einmal und ist jetzt weg?

Warum Delta und nicht Auflisten: /me/drive/root/delta liefert Änderungen UND
Löschungen mit einem Token, und beim nächsten Lauf nur noch das Neue. Auf einem
echten Laufwerk mit 629 Dateien war die vollständige Aufzählung eine einzige
Seite in gut zwei Sekunden; danach kostet ein Lauf ohne Änderungen fast nichts.

Die Löschung ist der Grund für das Bestandsverzeichnis (dateien.tsv): Graph
meldet zu einer gelöschten Datei nur ihre ID, keinen Namen und keinen Pfad. Ohne
die Zuordnung ID -> Pfad wüsste niemand, WAS da verschwunden ist.

Setup:   pip install msal requests
Start:   python3 onedrive_export.py [ausgabe-ordner]
         --folders  nur die Ordnerstruktur abgleichen, nichts herunterladen

Schalter (Umgebung schlägt app_config.json schlägt Vorgabe, siehe settings.py):
    ONEDRIVE_RULES    Include/Exclude-Regeln auf Pfaden, eine je Zeile, wie beim
                      Postfach – "- Dateien/Fotos/**" lässt die Fotos aus.
    ONEDRIVE_MAX_MB   Dateien darüber werden übersprungen (0 = ohne Grenze).
    EXPORT_WORKERS    parallele Downloads.

Resume: dateien.tsv und delta.txt im Ausgabeordner. Bricht ein Lauf ab, wird
    delta.txt NICHT fortgeschrieben – der nächste Lauf zählt noch einmal auf und
    überspringt anhand des cTag alles, was schon liegt. Ein abgebrochener Lauf
    darf keine Änderung verschlucken.
"""

import os
import re
import sys
import time
import hashlib
import threading
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote
from concurrent.futures import ThreadPoolExecutor, as_completed

import auth
import folders
import progress
import settings

try:
    import msal  # noqa: F401
    import requests
except ImportError:
    print("Fehlende Pakete. Bitte installieren:  pip install msal requests")
    raise SystemExit(1) from None

GRAPH = "https://graph.microsoft.com/v1.0"
RES = "https://graph.microsoft.com/"
SCOPES = [RES + "Files.Read.All", RES + "User.Read"]

OUT_ROOT = settings.value("onedrive_dir", "onedrive_export")
DATEI_DIR = "Dateien"           # Wurzel im Ausgabeordner; die Regeln greifen darauf
BESTAND_DATEI = "dateien.tsv"
DELTA_DATEI = "delta.txt"
GONE_FILE = "verschwunden.tsv"

TIMEOUT_JSON = (30, 120)
TIMEOUT_BYTES = (30, 600)
HTTP_RETRIES = 6
NET_RETRIES = 5
SEITE = 999                     # $top: eine Seite statt vieler kleiner

SESSION = requests.Session()


def workers():
    """Parallele Downloads. settings.number liest Umgebung > Datei > Vorgabe –
    settings.value täte das NICHT, und ein Schalter, der still nichts tut, ist
    schlimmer als keiner."""
    return max(1, min(settings.number("EXPORT_WORKERS", "workers", 4), 8))


def max_bytes():
    """Obergrenze je Datei; 0 heißt ohne Grenze."""
    return max(0, settings.number("ONEDRIVE_MAX_MB", "onedrive_max_mb", 0)) * 1024 * 1024


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def fetch(url, headers, params=None, timeout=TIMEOUT_JSON, stream=False, label=""):
    """Ein GET; wiederholt NUR bei Netzwerkfehlern. Status bewertet der Aufrufer."""
    for net in range(NET_RETRIES):
        try:
            return SESSION.get(url, headers=headers, params=params,
                               timeout=timeout, stream=stream)
        except requests.exceptions.RequestException as e:
            if net == NET_RETRIES - 1:
                raise
            w = min(2 ** net, 60)
            print(f"    … Netzwerkfehler{label} ({type(e).__name__}), warte {w}s")
            time.sleep(w)
    raise RuntimeError(f"Zu viele Netzwerkfehler: {url}")


class _Basis:
    """Was beide Zugangsarten teilen: Drosselung, Wiederholung, Paging."""

    def _headers(self):
        raise NotImplementedError

    def _erneuern(self):
        return False

    def _warten(self, r, versuch, was=""):
        ra = r.headers.get("Retry-After")
        w = min(int(ra) if ra and ra.isdigit() else 2 ** versuch, 60)
        print(f"    … HTTP {r.status_code}{was}, warte {w}s (Drosselung/Server)")
        time.sleep(w)

    def get(self, url, params=None):
        for versuch in range(HTTP_RETRIES):
            r = fetch(url, self._headers(), params, TIMEOUT_JSON)
            if r.status_code == 401 and self._erneuern():
                continue
            if r.status_code == 429 or 500 <= r.status_code < 600:
                self._warten(r, versuch)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Zu viele Fehlversuche: {url}")

    def lade(self, item_id, ziel, geaendert=None):
        """Inhalt einer Datei nach `ziel` schreiben – stückweise, nicht am Stück.

        Eine 300-MB-Datei komplett in den Speicher zu holen, nur um sie danach
        auf die Platte zu schreiben, wäre auf einem kleinen Rechner der einzige
        Grund, warum ein Lauf scheitert. Geschrieben wird daneben und erst zum
        Schluss umbenannt: ein Abbruch hinterlässt dann keine halbe Datei, die
        beim nächsten Lauf als fertig gälte.
        """
        url = f"{GRAPH}/me/drive/items/{item_id}/content"
        for versuch in range(HTTP_RETRIES):
            r = fetch(url, self._headers(), timeout=TIMEOUT_BYTES,
                      stream=True, label=" (Inhalt)")
            if r.status_code == 401 and self._erneuern():
                continue
            if r.status_code == 429 or 500 <= r.status_code < 600:
                self._warten(r, versuch, " (Inhalt)")
                continue
            r.raise_for_status()
            ziel.parent.mkdir(parents=True, exist_ok=True)
            tmp = ziel.with_name(ziel.name + ".teil")
            groesse = 0
            with open(tmp, "wb") as f:
                for stueck in r.iter_content(chunk_size=1 << 20):
                    if stueck:
                        f.write(stueck)
                        groesse += len(stueck)
            os.replace(tmp, ziel)
            # Die Änderungszeit aus OneDrive übernehmen. Ohne das trüge jede
            # Datei den Zeitpunkt ihres Downloads, und im Index stünden 600
            # Dateien mit demselben Datum – eine Sortierung nach Datum wäre
            # wertlos, und ein Neuaufbau des Spiegels änderte sie erneut.
            if geaendert:
                try:
                    os.utime(ziel, (geaendert, geaendert))
                except OSError:
                    pass
            return groesse
        raise RuntimeError(f"Zu viele Fehlversuche: {url}")

    def delta(self, weiter=None):
        """Alle Delta-Einträge, Seite für Seite. Gibt am Ende den neuen Link."""
        url = weiter or f"{GRAPH}/me/drive/root/delta?$top={SEITE}"
        while True:
            d = self.get(url)
            for eintrag in d.get("value", []):
                yield eintrag, None
            nxt = d.get("@odata.nextLink")
            if not nxt:
                yield None, d.get("@odata.deltaLink")
                return
            url = nxt


class Graph(_Basis):
    """Angemeldeter Zugriff; die Anmeldung selbst steckt in auth.Login."""

    def __init__(self, nur_still=False):
        self.anmeldung = auth.Login(SCOPES)
        self._lock = threading.Lock()
        if not self.anmeldung.anmelden(nur_still=nur_still):
            raise SystemExit("Keine gültige Anmeldung im Zwischenspeicher.")

    @property
    def account(self):
        return self.anmeldung.account

    def _headers(self):
        return self.anmeldung.headers()

    def _erneuern(self):
        with self._lock:
            self.anmeldung.erneuern()
        return True


class TokenClient(_Basis):
    """Fertiger Bearer-Token aus dem Graph Explorer; keine Erneuerung möglich."""

    def __init__(self, token):
        self.token = token
        self.account = None

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}


# ---------------------------------------------------------------------------
# Pfade
# ---------------------------------------------------------------------------
def kuerzel(s):
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()[:8]


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
        if not roh:
            continue
        try:
            return datetime.fromisoformat(str(roh).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
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


def lies_verschwunden(pfad):
    out = {}
    try:
        for zeile in Path(pfad).read_text(encoding="utf-8").splitlines():
            if "\t" in zeile:
                rel, wann = zeile.split("\t", 1)
                out[rel] = wann
    except OSError:
        pass
    return out


def schreibe_verschwunden(pfad, bekannt, neue, jetzt):
    zusammen = dict(bekannt)
    for rel in neue:
        zusammen.setdefault(rel, jetzt)
    pfad = Path(pfad)
    pfad.parent.mkdir(parents=True, exist_ok=True)
    tmp = pfad.with_name(pfad.name + ".tmp")
    tmp.write_text("".join(f"{rel}\t{wann}\n" for rel, wann in sorted(zusammen.items())),
                   encoding="utf-8")
    tmp.replace(pfad)
    return zusammen


def lies_delta(out):
    try:
        return (Path(out) / DELTA_DATEI).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def schreibe_delta(out, link):
    if not link:
        return
    ziel = Path(out) / DELTA_DATEI
    ziel.parent.mkdir(parents=True, exist_ok=True)
    tmp = ziel.with_name(ziel.name + ".tmp")
    tmp.write_text(link, encoding="utf-8")
    tmp.replace(ziel)


# ---------------------------------------------------------------------------
# Auswahl
# ---------------------------------------------------------------------------
def aktuelle_regeln():
    """Include/Exclude auf Pfaden – dieselbe Mechanik wie beim Postfach.

    Ohne Regeln kommt alles mit: wer nichts einstellt, will sein Laufwerk, nicht
    Leere.
    """
    roh = os.environ.get("ONEDRIVE_RULES")
    if roh is None:
        roh = settings.value("onedrive_rules", None)
    return folders.lies_regeln(roh or "")


def zu_gross(groesse, grenze=None):
    grenze = max_bytes() if grenze is None else grenze
    return bool(grenze) and groesse > grenze


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


def plane(eintraege, bestand, wurzel, regeln, grenze=None):
    """Aus den Delta-Einträgen wird eine Aufgabenliste – ohne Netzzugriff.

    Getrennt vom Herunterladen, damit die Entscheidung „was tun wir" ohne
    Anmeldung geprüft werden kann.
    """
    grenze = max_bytes() if grenze is None else grenze
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
        if not folders.gilt(rel, regeln):
            ausgelassen += 1
            continue
        groesse = int(e.get("size") or 0)
        if zu_gross(groesse, grenze):
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


def hole_alle(graph, wurzel, bestand, aufgaben):
    """Die geplanten Downloads – parallel, mit Fortschritt."""
    fertig = fehler = 0
    gesamt = len(aufgaben)
    if not gesamt:
        return 0, 0
    progress.melde(0, gesamt, "files")
    with ThreadPoolExecutor(max_workers=workers()) as pool:
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
                print(f"    ! {a['rel']}: {type(e).__name__}: {e}")
            if (fertig + fehler) % 10 == 0 or fertig + fehler == gesamt:
                progress.melde(fertig + fehler, gesamt, "files")
                bestand.schreibe()
    bestand.schreibe()
    return fertig, fehler


def lauf(graph, out):
    out = Path(out)
    wurzel = out
    regeln = aktuelle_regeln()
    bestand = Bestand(out / BESTAND_DATEI)
    weiter = lies_delta(out)
    print(f"OneDrive-Spiegel: {out.resolve()}")
    print("  Aufzählung: " + ("nur Änderungen seit dem letzten Lauf"
                              if weiter else "vollständig (erster Lauf)"))

    eintraege, neuer_link = sammle(graph, weiter)
    print(f"  {len(eintraege)} Einträge von Graph.")
    plan = plane(eintraege, bestand, wurzel, regeln)

    bewegt = verschiebe(wurzel, plan["verschoben"])
    fertig, fehler = hole_alle(graph, wurzel, bestand, plan["laden"])
    # Immer, nicht nur nach Downloads: auch eine Löschung oder eine Verschiebung
    # ändert den Bestand. Ohne dieses Schreiben stünde eine gelöschte Datei beim
    # nächsten Lauf noch drin und ihr Grabstein würde ein zweites Mal gesetzt.
    bestand.schreibe()

    jetzt = datetime.now(UTC).isoformat(timespec="seconds")
    weg = schreibe_verschwunden(out / GONE_FILE, lies_verschwunden(out / GONE_FILE),
                                plan["geloescht"], jetzt)
    alt_baum = folders.lade(out)
    baum = baum_zusammenfuehren(alt_baum, plan["baum"], plan["entfernt"])
    if baum or alt_baum:
        folders.speichere(out, baum, alt_baum)
    # Erst jetzt: ein abgebrochener Lauf darf den Delta-Zeiger nicht vorrücken.
    if not fehler:
        schreibe_delta(out, neuer_link)

    print(f"\n{fertig} Dateien geladen, {bewegt} verschoben, "
          f"{len(plan['geloescht'])} nicht mehr in OneDrive, "
          f"{plan['ausgelassen']} ausgelassen, {fehler} Fehler.")
    if plan["geloescht"]:
        print(f"  Vermerkt in {GONE_FILE} ({len(weg)} insgesamt) – "
              f"die Dateien bleiben liegen.")
    if fehler:
        print("  Wegen der Fehler wird beim nächsten Lauf noch einmal "
              "vollständig aufgezählt.")
    progress.ergebnis(fertig, uebersprungen=plan["ausgelassen"], fehler=fehler)
    return fertig


def nur_ordner(graph, out):
    """--folders: nur die Struktur holen, nichts herunterladen.

    Zählt bewusst VOLLSTÄNDIG auf und ignoriert den gespeicherten Delta-Zeiger:
    ein Abgleich soll den ganzen Baum zeigen, nicht die Handvoll Ordner, die
    sich seit gestern geändert haben. Und er rückt den Zeiger nicht vor – sonst
    hielte der nächste Export die noch nie geholten Dateien für erledigt.
    """
    out = Path(out)
    print(f"Gleiche die OneDrive-Ordner ab: {out.resolve()}")
    eintraege, _ = sammle(graph, None)
    bestand = Bestand(out / BESTAND_DATEI)
    plan = plane(eintraege, bestand, out, aktuelle_regeln())
    alt = folders.lade(out)
    daten = folders.speichere(out, baum_zusammenfuehren(alt, plan["baum"],
                                                        plan["entfernt"]), alt)
    z = folders.zusammenfassung(daten, aktuelle_regeln())
    print(f"\n{z['ordner_gesamt']} Ordner im Laufwerk, "
          f"{z['ordner_gewaehlt']} nach den Regeln gewählt.")
    for art, liste in (("neu", daten["neu"]), ("nicht mehr da", daten["verschwunden"]),
                       ("umbenannt", daten["umbenannt"])):
        if liste:
            print(f"  {len(liste)} {art}: " + ", ".join(liste[:5])
                  + (" …" if len(liste) > 5 else ""))
    print(f"Abgelegt: {folders.pfad(out)}")


def _hilfe_gewuenscht(argv):
    return any(a in ("-h", "--help", "help") for a in argv)


def main():
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    argv = sys.argv[1:]
    if _hilfe_gewuenscht(argv):
        print(__doc__)
        return
    hinweis = settings.report()
    if hinweis:
        print(hinweis)
    struktur = "--folders" in argv
    argv = [a for a in argv if not a.startswith("--")]
    out = Path(argv[0]) if argv else Path(OUT_ROOT)
    graph = auth.waehle_zugang(lambda tok: TokenClient(tok), Graph)
    (nur_ordner if struktur else lauf)(graph, out)


if __name__ == "__main__":
    main()
