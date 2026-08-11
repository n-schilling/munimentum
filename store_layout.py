#!/usr/bin/env python3
"""
store_layout.py – wo im Store welche Datei liegt.

Ein Index besteht aus drei Teilen: corpus.db (Textstellen und Volltextindex),
einer Vektordatei (Embeddings, Zeile i gehört zu chunks.id = i+1) und info.json
(Modell, Format – und der NAME der Vektordatei).

Warum der Name nicht mehr fest „vectors.npy" ist:

    Jeder Leser öffnet die Vektordatei per mmap und hält sie offen, solange er
    läuft – der MCP-Server, die Suche in der App selbst, ein von Claude Desktop
    gestarteter Server. Unter Windows lässt sich eine so abgebildete Datei nicht
    ersetzen: os.replace endet mit „Zugriff verweigert". Der Indexlauf starb
    deshalb dort in der letzten Zeile, nachdem er alles eingebettet hatte, und
    zwar zuverlässig, solange irgendwer den Index offen hatte. Unter macOS und
    Linux fällt das nicht auf – dort überlebt die alte Datei das Umbenennen als
    Inode, und der Leser liest sie ungestört zu Ende.

    Ein Lauf schreibt darum eine NEUE Datei und trägt ihren Namen hier ein.
    Ersetzt wird nichts mehr; wer noch die alte offen hat, behält sie gültig in
    der Hand. Aufgeräumt wird, was niemand mehr braucht – und was sich nicht
    löschen lässt, bleibt bis zum nächsten Lauf liegen.

Nur Standardbibliothek: mcp_server.py bindet dieses Modul ein und kommt selbst
bewusst ohne numpy aus.
"""

import json
import re
from pathlib import Path

INFO = "info.json"

# Der feste Name bis einschließlich 4.1.0. Ein Store von damals trägt keinen
# Eintrag in info.json – dort gilt weiter diese Datei, sonst stünde nach dem
# Update ein vorhandener Index ohne seine Embeddings da.
LEGACY = "vectors.npy"

_MUSTER = re.compile(r"^vectors-(\d+)\.npy$")


def info(store):
    """info.json als dict; fehlend oder unlesbar ergibt {}."""
    try:
        daten = json.loads((Path(store) / INFO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return daten if isinstance(daten, dict) else {}


def vectors_path(store, daten=None):
    """Die aktuelle Vektordatei – oder None, wenn dieser Index keine hat.

    Drei Fälle, und der mittlere ist der Grund für die Unterscheidung:

        Eintrag da        -> genau diese Datei
        Eintrag ausdrücklich leer -> KEINE Vektoren (ein Lauf ohne Embeddings
                             hat sie zurückgezogen). Hier auf den alten Namen
                             zurückzufallen wäre falsch: die Datei kann noch
                             herumliegen, weil sie sich nicht löschen ließ, und
                             ihre Zeilen passen dann nicht mehr zur DB.
        kein Eintrag      -> Store von 4.1.0 oder älter, fester Name
    """
    sp = Path(store)
    daten = info(store) if daten is None else daten
    if "vectors" in daten:
        name = daten.get("vectors")
        if not name:
            return None
        p = sp / str(name)
        return p if p.exists() else None
    p = sp / LEGACY
    return p if p.exists() else None


def next_vectors_path(store):
    """Ein Name, den es hier noch nicht gibt: vectors-1.npy, vectors-2.npy, …

    Gezählt wird nach dem, was im Ordner liegt, nicht nach dem Eintrag in
    info.json: eine Datei, die beim Aufräumen nicht wegging, darf ein späterer
    Lauf nicht überschreiben – sonst wäre genau der Leser gestört, dessentwegen
    sie liegen blieb.
    """
    sp = Path(store)
    hoechste = 0
    for p in sp.glob("vectors-*.npy"):
        m = _MUSTER.match(p.name)
        if m:
            hoechste = max(hoechste, int(m.group(1)))
    return sp / f"vectors-{hoechste + 1}.npy"


def prune_vectors(store, behalten=None):
    """Vektordateien wegräumen, die niemand mehr braucht.

    Was gerade abgebildet ist, lässt sich unter Windows nicht löschen. Das ist
    kein Fehlerfall, sondern der Normalzustand direkt nach einem Lauf: der noch
    laufende MCP-Server hält die vorige Fassung. Sie kostet Platz bis zum
    nächsten Lauf, und der versucht es erneut.

    Liefert die Zahl der tatsächlich gelöschten Dateien.
    """
    sp = Path(store)
    behalten = Path(behalten).name if behalten else None
    weg = 0
    for p in sorted(sp.glob("vectors-*.npy")) + [sp / LEGACY]:
        if p.name == behalten or not p.exists():
            continue
        try:
            p.unlink()
            weg += 1
        except OSError:
            pass                  # noch offen – beim nächsten Mal wieder
    return weg
