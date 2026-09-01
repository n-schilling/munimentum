#!/usr/bin/env python3
"""
migrate_state.py – one-off move of the historical loose state files into the
per-folder state.db (see state_db.py).

Until 6.1 Outlook, Teams and OneDrive kept their resume data as loose files
in their output folders; the SharePoint exports started on state.db right
away. app.py runs this at startup when it finds the old layout, keeps every
export blocked until it is done, and says so in the log.

The order makes a crash harmless: each folder is written into its state.db
first and the legacy files are renamed to *.bak only after that – until the
rename, the loose files remain the source of truth, and running the
migration again simply overwrites the half-written database.
"""

import json
from pathlib import Path

import state_db

# The legacy files per export root. OneDrive additionally carries the
# checkpointed-walk staging introduced in 6.1.
OUTLOOK_DATEIEN = ("exported.tsv", "verschwunden.tsv", "folders.json",
                   "calendars.json", "vollstaendigkeit.json")
TEAMS_DATEIEN = ("export_state.json",)
ONEDRIVE_DATEIEN = ("dateien.tsv", "delta.txt", "verschwunden.tsv",
                    "folders.json", "vollstaendigkeit.json", "walk.jsonl",
                    "walk_cursor.txt", "walk_fertig.txt")


def _vorhanden(ordner, namen):
    ordner = Path(ordner)
    return [n for n in namen if (ordner / n).is_file()]


def noetig(outlook, teams, onedrive):
    """Which export roots still carry the old layout – [] when none do."""
    faellig = []
    for name, ordner, dateien in (("outlook", outlook, OUTLOOK_DATEIEN),
                                  ("teams", teams, TEAMS_DATEIEN),
                                  ("onedrive", onedrive, ONEDRIVE_DATEIEN)):
        if _vorhanden(ordner, dateien):
            faellig.append((name, Path(ordner)))
    return faellig


def _lies_json(pfad):
    try:
        return json.loads(Path(pfad).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _lies_tsv(pfad, felder):
    """Lines of `felder` tab-separated columns; short lines are skipped."""
    zeilen = []
    try:
        text = Path(pfad).read_text(encoding="utf-8")
    except OSError:
        return zeilen
    for zeile in text.splitlines():
        teile = zeile.split("\t")
        if len(teile) >= felder:
            zeilen.append(teile[:felder])
    return zeilen


def _lies_verschwunden(pfad):
    """The pre-6.2 tombstone file: rel<TAB>gone-since, one line each."""
    out = {}
    try:
        for zeile in Path(pfad).read_text(encoding="utf-8").splitlines():
            if "\t" in zeile:
                rel, wann = zeile.split("\t", 1)
                out[rel] = wann
    except OSError:
        pass
    return out


def _lies_text(pfad):
    try:
        return Path(pfad).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _wegraeumen(ordner, namen):
    for n in namen:
        alt = Path(ordner) / n
        if alt.is_file():
            alt.replace(alt.with_name(n + ".bak"))


def _outlook(ordner):
    db = state_db.StateDb(ordner)
    n = 0
    done = _lies_tsv(ordner / "exported.tsv", 2)
    if done:
        db.done_ersetzen(done)
        n += len(done)
    weg = _lies_verschwunden(ordner / "verschwunden.tsv")
    if weg:
        db.verschwunden_ersetzen(weg)
        n += len(weg)
    for datei, key in (("folders.json", "baum"), ("calendars.json", "kalender"),
                       ("vollstaendigkeit.json", "bericht")):
        daten = _lies_json(ordner / datei)
        if daten is not None:
            db.kv_schreiben(key, json.dumps(daten, ensure_ascii=False))
            n += 1
    _wegraeumen(ordner, OUTLOOK_DATEIEN)
    return n


def _teams(ordner):
    db = state_db.StateDb(ordner)
    n = 0
    daten = _lies_json(ordner / "export_state.json")
    if daten is not None:
        db.kv_schreiben("state", json.dumps(daten, ensure_ascii=False))
        n += len((daten or {}).get("conversations") or {}) or 1
    _wegraeumen(ordner, TEAMS_DATEIEN)
    return n


def _onedrive(ordner):
    db = state_db.StateDb(ordner)
    n = 0
    bestand = {kennung: {"rel": rel, "ctag": ctag,
                         "size": int(groesse) if groesse.isdigit() else -1}
               for kennung, rel, ctag, groesse
               in _lies_tsv(ordner / "dateien.tsv", 4)}
    if bestand:
        db.bestand_schreiben(bestand, delta_link=_lies_text(ordner / "delta.txt"))
        n += len(bestand)
    weg = _lies_verschwunden(ordner / "verschwunden.tsv")
    if weg:
        db.verschwunden_ersetzen(weg)
        n += len(weg)
    for datei, key in (("folders.json", "baum"),
                       ("vollstaendigkeit.json", "bericht")):
        daten = _lies_json(ordner / datei)
        if daten is not None:
            db.kv_schreiben(key, json.dumps(daten, ensure_ascii=False))
            n += 1
    # The walk staging of an interrupted 6.1 run rides along – the resumed
    # walk then continues from the database instead of starting over.
    seiten = []
    try:
        for zeile in (ordner / "walk.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                seiten.append(json.loads(zeile))
            except ValueError:
                continue
    except OSError:
        pass
    if seiten:
        db.walk_ergaenzen(seiten, _lies_text(ordner / "walk_cursor.txt"))
        fertig = _lies_text(ordner / "walk_fertig.txt")
        if fertig:
            # Only then: walk_abschliessen would otherwise drop the resume
            # cursor of a walk that was still in progress.
            db.walk_abschliessen(fertig)
        n += len(seiten)
    _wegraeumen(ordner, ONEDRIVE_DATEIEN)
    return n


_LAEUFER = {"outlook": _outlook, "teams": _teams, "onedrive": _onedrive}


def lauf(faellig, melde=None):
    """Migrate every listed root; returns {name: entry count}.

    `melde(name, n)` is called after each finished root – app.py turns that
    into one log line per store."""
    ergebnis = {}
    for name, ordner in faellig:
        n = _LAEUFER[name](ordner)
        ergebnis[name] = n
        if melde:
            melde(name, n)
    return ergebnis
