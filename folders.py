#!/usr/bin/env python3
"""
folders.py – der Ordnerbaum des Postfachs als eigenes Ding.

Bis 2.x lief beides in einem: der Export las bei jedem Lauf die komplette
Ordnerstruktur und entschied dabei, was er holt. Auf einem echten Postfach sind
das **rund zwei Minuten für über 400 Ordner**, bevor eine einzige Mail geladen wird – und
die Auswahl konnte damit fast nichts anfangen, weil sie nur auf oberster Ebene
und nur über den Anzeigenamen griff. „Kunden“ mit fast 300 Unterordnern war eine
Entscheidung: ganz oder gar nicht.

Hier liegt deshalb beides getrennt:

  Der Baum   wird auf Wunsch abgerufen und als folders.json abgelegt. Er ändert
             sich selten; ein Export liest ihn von der Platte.

  Die Regeln sind eine geordnete Liste aus Include und Exclude auf Pfaden mit
             Platzhaltern. Die LETZTE zutreffende gewinnt – dasselbe Prinzip wie
             in .gitignore. Damit ist sagbar, was vorher nicht ging:

                 - E-Mail/Archiv/**
                 + E-Mail/Archiv/Wichtig/**

Warum je Ordner Pfad UND ID gespeichert werden: Ordner-IDs sind stabil,
Anzeigenamen nicht. Wer in Outlook umbenennt, würde bei reiner Pfadhaltung den
Ordner still aus dem Export verlieren – die ID erkennt ihn wieder.
"""

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

DATEI = "folders.json"

# Was frühere Fassungen als Namensliste hatten. Wird beim ersten Lauf in Regeln
# übersetzt (siehe aus_namensliste) – niemand soll seine Auswahl neu eintippen.
BUILTIN_SKIP = [
    "archive", "archiv",
    "entwürfe", "drafts",
    "erneut erinnern aktiviert",
    "gelöschte elemente", "deleted items",
    "junk-e-mail", "junk email", "junk-email",
    "postausgang", "outbox",
]


# --------------------------------------------------------------------------
# Regeln
# --------------------------------------------------------------------------
def _segment(stueck):
    """Ein Pfadstück: * und ? bleiben innerhalb der Ebene, alles andere wörtlich."""
    out = []
    for ch in stueck:
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def _als_regex(muster):
    """Ein Pfadmuster als regulärer Ausdruck.

    `*` bleibt innerhalb einer Ebene, `**` überspringt beliebig viele. Ein
    Muster, das auf `/**` endet, meint den Ordner selbst *und* alles darunter –
    „E-Mail/Archiv/**“ soll nicht ausgerechnet das Archiv selbst auslassen.
    Deshalb zwei Varianten in einer Alternative.
    """
    muster = (muster or "").strip().strip("/")
    if not muster:
        return re.compile(r"(?!)")            # trifft nie
    varianten = [muster]
    if muster.endswith("/**"):
        varianten.append(muster[:-3])
    teile = []
    for v in varianten:
        teile.append("/".join(".*" if st == "**" else _segment(st)
                              for st in v.split("/")))
    return re.compile("(?:" + "|".join(teile) + r")\Z", re.IGNORECASE)


def passt(pfad, muster):
    """Trifft das Muster diesen Pfad?"""
    return bool(_als_regex(muster).match((pfad or "").strip("/")))


def lies_regel(zeile):
    """„- E-Mail/Archiv/**“ -> (False, "E-Mail/Archiv/**"). None bei Unfug."""
    roh = (zeile or "").strip()
    if not roh or roh.startswith("#"):
        return None
    zeichen, rest = roh[0], roh[1:].strip()
    if zeichen == "+":
        return (True, rest) if rest else None
    if zeichen == "-":
        return (False, rest) if rest else None
    # Ohne Vorzeichen: einschließen. Wer eine Liste von Ordnern hinschreibt,
    # meint fast immer „diese“ – nicht „diese nicht“.
    return (True, roh)


def lies_regeln(text):
    """Mehrere Zeilen (oder eine Liste) in geordnete Regeln übersetzen."""
    if isinstance(text, str):
        zeilen = text.splitlines()
    else:
        zeilen = list(text or [])
    return [r for r in (lies_regel(z) for z in zeilen) if r]


def schreibe_regeln(regeln):
    return "\n".join(("+ " if ein else "- ") + muster for ein, muster in regeln)


def gilt(pfad, regeln, vorgabe=True):
    """Wird dieser Ordner exportiert?

    Die letzte zutreffende Regel gewinnt. Ohne Treffer gilt `vorgabe` – und die
    ist „ja“: wer nichts einstellt, bekommt sein Postfach, nicht Leere.
    """
    ergebnis = vorgabe
    for ein, muster in regeln or ():
        if passt(pfad, muster):
            ergebnis = ein
    return ergebnis


def erklaere(pfad, regeln, vorgabe=True):
    """(gilt, Regel die entschied) – für „warum ist der Ordner aus?“."""
    treffer = None
    ergebnis = vorgabe
    for ein, muster in regeln or ():
        if passt(pfad, muster):
            ergebnis, treffer = ein, (ein, muster)
    return ergebnis, treffer


def aus_namensliste(namen):
    """Alte SKIP_FOLDERS in Regeln übersetzen.

    Die alte Liste verglich Anzeigenamen auf oberster Ebene. Als Regel ist das
    „E-Mail/<Name>/**“ – ein Ausschluss des Ordners samt allem darunter.
    """
    return [(False, f"E-Mail/{n}/**") for n in
            sorted({str(x).strip() for x in (namen or []) if str(x).strip()})]


# --------------------------------------------------------------------------
# Der Baum auf der Platte
# --------------------------------------------------------------------------
def pfad(ordner):
    return Path(ordner) / DATEI


def lade(ordner):
    """folders.json lesen. Fehlt sie oder ist kaputt: None, kein Krach."""
    try:
        daten = json.loads(pfad(ordner).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(daten, dict) or not isinstance(daten.get("ordner"), list):
        return None
    return daten


def speichere(ordner, eintraege, vorher=None):
    """Baum atomar ablegen und melden, was sich geändert hat.

    Neue Ordner sind der Grund für die Rückgabe: nach einem Abgleich soll die
    Oberfläche sagen können „4 neue Ordner“, statt dass sie unbemerkt
    dazukommen und je nach Regel mitlaufen oder fehlen.
    """
    alt = {e["id"]: e for e in (vorher or {}).get("ordner", [])}
    jetzt = {e["id"]: e for e in eintraege}
    # Beim allerersten Abgleich ist nichts „neu“ – es war ja vorher nichts da.
    # „400 Ordner neu dazugekommen“ wäre formal wahr und trotzdem Unsinn.
    erster = not alt
    daten = {
        "abgeglichen": datetime.now(UTC).isoformat(timespec="seconds"),
        "ordner": eintraege,
        "neu": [] if erster else sorted(
            e["pfad"] for k, e in jetzt.items() if k not in alt),
        "verschwunden": sorted(e["pfad"] for k, e in alt.items() if k not in jetzt),
        "umbenannt": sorted(
            f'{alt[k]["pfad"]} -> {e["pfad"]}'
            for k, e in jetzt.items() if k in alt and alt[k]["pfad"] != e["pfad"]),
    }
    ziel = pfad(ordner)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    tmp = ziel.with_name(ziel.name + ".tmp")
    tmp.write_text(json.dumps(daten, ensure_ascii=False), encoding="utf-8")
    tmp.replace(ziel)
    return daten


def gewaehlt(daten, regeln):
    """Die Einträge, die nach den Regeln exportiert werden."""
    return [e for e in (daten or {}).get("ordner", []) if gilt(e["pfad"], regeln)]


def zusammenfassung(daten, regeln):
    """Wie viele Ordner und Mails die Auswahl trifft – für die Oberfläche."""
    alle = (daten or {}).get("ordner", [])
    an = gewaehlt(daten, regeln)
    return {
        "abgeglichen": (daten or {}).get("abgeglichen"),
        "ordner_gesamt": len(alle),
        "ordner_gewaehlt": len(an),
        "mails_gesamt": sum(int(e.get("elemente") or 0) for e in alle),
        "mails_gewaehlt": sum(int(e.get("elemente") or 0) for e in an),
        "neu": (daten or {}).get("neu", []),
        "verschwunden": (daten or {}).get("verschwunden", []),
    }


def auf_platte(ordner, wurzeln=()):
    """Was im Archiv wirklich liegt: {Ordnerpfad: Zahl der .eml-Dateien}.

    Nur unterhalb der genannten Wurzeln – sonst zählten `kalender/` und
    `kontakte/` als Postfachordner, die sie nie waren. Die Wurzeln kommen aus
    dem Baum selbst, damit hier kein Ordnername fest verdrahtet ist.

    Auf einem echten Archiv (rund 45.000 Mails, gut 400 Ordner) dauert das
    0,06 s – billig
    genug, um es bei jedem Öffnen der Liste frisch zu machen statt einen
    Zwischenstand zu pflegen, der falsch sein kann.
    """
    gefunden = {}
    basis = Path(ordner)
    for wurzel in dict.fromkeys(wurzeln or ()):
        for verzeichnis, _unter, dateien in os.walk(basis / wurzel):
            anzahl = sum(1 for d in dateien if d.lower().endswith(".eml"))
            if anzahl:
                gefunden[Path(verzeichnis).relative_to(basis).as_posix()] = anzahl
    return gefunden


def plan(ordner, regeln, daten=None):
    """Was der nächste Export täte – Ordner für Ordner, ohne ihn zu starten.

    Die Regeln sind mächtig genug, dass ihr Ergebnis nicht mehr im Kopf
    entsteht: „- E-Mail/Archiv/**“ und zwei Zeilen später ein „+“ auf einen
    Unterordner – wer das nachrechnen muss, rechnet irgendwann falsch. Deshalb
    drei ausdrückliche Listen statt einer Zahl:

      an   was mitkommt
      aus  was ausgelassen wird, samt der Regel, die es entschied
      weg  was nur noch im Archiv liegt und im Postfach nicht mehr auftaucht

    Die dritte ist die, die man sonst nirgends sieht: ein in Outlook gelöschter
    oder umbenannter Ordner verschwindet still aus dem Baum, seine Mails bleiben
    aber – zu Recht – auf der Platte liegen.
    """
    daten = lade(ordner) if daten is None else daten
    eintraege = (daten or {}).get("ordner", [])
    archiv = auf_platte(ordner, [e["pfad"].split("/")[0] for e in eintraege])
    an, aus = [], []
    for e in eintraege:
        ja, regel = erklaere(e["pfad"], regeln)
        (an if ja else aus).append({
            "pfad": e["pfad"],
            "elemente": int(e.get("elemente") or 0),
            "archiv": archiv.get(e["pfad"], 0),
            "regel": (("+ " if regel[0] else "- ") + regel[1]) if regel else None,
        })
    bekannt = {e["pfad"] for e in eintraege}
    weg = [{"pfad": p, "archiv": n} for p, n in sorted(archiv.items())
           if p not in bekannt]
    return {
        "abgeglichen": (daten or {}).get("abgeglichen"),
        "an": an, "aus": aus, "weg": weg,
        "mails_an": sum(z["elemente"] for z in an),
        "mails_aus": sum(z["elemente"] for z in aus),
        "mails_weg": sum(z["archiv"] for z in weg),
    }


def main():
    """Zeigt, was in folders.json steht – und was die Regeln daraus machen."""
    import sys
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ordner = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
        "OUTLOOK_DIR", "outlook_export")
    daten = lade(ordner)
    if not daten:
        print(f"Kein Ordnerbaum in {pfad(ordner)} – erst abgleichen "
              f"(outlook_export.py --folders).")
        return
    regeln = lies_regeln(os.environ.get("FOLDER_RULES", ""))
    z = zusammenfassung(daten, regeln)
    p = plan(ordner, regeln, daten)
    print(f"Stand: {z['abgeglichen']}")
    print(f"{z['ordner_gewaehlt']} von {z['ordner_gesamt']} Ordnern gewählt, "
          f"{z['mails_gewaehlt']} von {z['mails_gesamt']} Mails.\n")
    for titel, liste, zahl in (
            ("Wird exportiert", p["an"], "elemente"),
            ("Wird ausgelassen", p["aus"], "elemente"),
            ("Nur noch im Archiv (nicht mehr im Postfach)", p["weg"], "archiv")):
        print(f"{titel}: {len(liste)}")
        for e in liste:
            grund = f"   {e['regel']}" if e.get("regel") else ""
            print(f"  {e['pfad'][:64]:66}{e[zahl]:>7}{grund}")
        print()


if __name__ == "__main__":
    main()
