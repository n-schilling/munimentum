#!/usr/bin/env python3
"""
folders.py – the mailbox folder tree as a thing of its own.

Export and selection used to run as one: every run read the complete folder
structure and decided along the way what to fetch. On a real mailbox that is
**a couple of minutes for several hundred folders** before a single mail is
loaded – and the selection could do almost nothing with it, because it only
worked at the top level and only on display names. A branch with hundreds of
subfolders was one decision: all or nothing.

So both live separately here:

  The tree   is fetched on demand and stored in the state.db. It changes
             rarely; an export reads it from disk.

  The rules  are an ordered list of includes and excludes on paths with
             wildcards. The LAST matching one wins – the same principle as
             in .gitignore. This makes sayable what was not before:

                 - E-Mail/Archiv/**
                 + E-Mail/Archiv/Wichtig/**

Why path AND ID are stored per folder: folder IDs are stable, display names
are not. Renaming in Outlook would, with pure path bookkeeping, silently
drop the folder from the export – the ID recognizes it again.
"""

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

DATEI = "folders.json"
# A mailbox's calendars are the same kind of list: entries with path and ID
# that ordered rules decide about. Only the file differs, because both live
# in the same output folder.
KALENDER = "calendars.json"
# OneNote notebooks are the same kind of list once more – one entry per
# notebook, the folder it lands in as the path, ordered rules over it.
NOTIZBUECHER = "notebooks.json"

# What earlier versions had as a name list. Translated into rules on the
# first run (see aus_namensliste) – nobody should retype their selection.
BUILTIN_SKIP = [
    "archive", "archiv",
    "entwürfe", "drafts",
    "erneut erinnern aktiviert",
    "gelöschte elemente", "deleted items",
    "junk-e-mail", "junk email", "junk-email",
    "postausgang", "outbox",
]


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------
def _segment(stueck):
    """One path segment: * and ? stay within the level, everything else literal."""
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
    """A path pattern as a regular expression.

    `*` stays within one level, `**` skips any number of them. A pattern
    ending in `/**` means the folder itself *and* everything below –
    "E-Mail/Archiv/**" must not skip the archive itself of all things.
    Hence two variants in one alternation.
    """
    muster = (muster or "").strip().strip("/")
    if not muster:
        return re.compile(r"(?!)")            # never matches
    varianten = [muster]
    if muster.endswith("/**"):
        varianten.append(muster[:-3])
    teile = []
    for v in varianten:
        teile.append("/".join(".*" if st == "**" else _segment(st)
                              for st in v.split("/")))
    return re.compile("(?:" + "|".join(teile) + r")\Z", re.IGNORECASE)


def passt(pfad, muster):
    """Does the pattern match this path?"""
    return bool(_als_regex(muster).match((pfad or "").strip("/")))


def lies_regel(zeile):
    """"- E-Mail/Archiv/**" -> (False, "E-Mail/Archiv/**"). None for nonsense."""
    roh = (zeile or "").strip()
    if not roh or roh.startswith("#"):
        return None
    zeichen, rest = roh[0], roh[1:].strip()
    if zeichen == "+":
        return (True, rest) if rest else None
    if zeichen == "-":
        return (False, rest) if rest else None
    # No sign: include. Whoever writes down a list of folders almost always
    # means "these" – not "not these".
    return (True, roh)


def lies_regeln(text):
    """Translate several lines (or a list) into ordered rules."""
    if isinstance(text, str):
        zeilen = text.splitlines()
    else:
        zeilen = list(text or [])
    return [r for r in (lies_regel(z) for z in zeilen) if r]


def schreibe_regeln(regeln):
    return "\n".join(("+ " if ein else "- ") + muster for ein, muster in regeln)


def gilt(pfad, regeln, vorgabe=True):
    """Is this folder exported?

    The last matching rule wins. Without a match, `vorgabe` applies – and
    that is "yes": whoever configures nothing gets their mailbox, not
    emptiness.
    """
    ergebnis = vorgabe
    for ein, muster in regeln or ():
        if passt(pfad, muster):
            ergebnis = ein
    return ergebnis


def erklaere(pfad, regeln, vorgabe=True):
    """(applies, rule that decided) – for "why is this folder off?"."""
    treffer = None
    ergebnis = vorgabe
    for ein, muster in regeln or ():
        if passt(pfad, muster):
            ergebnis, treffer = ein, (ein, muster)
    return ergebnis, treffer


def nur_standard(eintraege):
    """Rules that select exactly the entries marked as default.

    For lists whose sensible default is not "everything": of a mailbox's
    calendars one wants one's own first, not additionally the birthdays and
    every shared calendar someone once granted access to. As written-out
    rules instead of a special case in the code – this way the interface
    shows the same thing that actually applies, and whoever wants to change
    it sees what to change.

    If nothing is marked as default, the first entry applies. Otherwise the
    default would be "nothing at all" – and an export that silently does
    nothing is the worst of all answers.
    """
    liste = list(eintraege or ())
    an = [e for e in liste if e.get("standard")] or liste[:1]
    return [(False, "**")] + [(True, e["pfad"]) for e in an]


def aus_namensliste(namen):
    """Translate the old SKIP_FOLDERS into rules.

    The old list compared display names at the top level. As a rule that is
    "E-Mail/<Name>/**" – an exclusion of the folder and everything below.
    """
    return [(False, f"E-Mail/{n}/**") for n in
            sorted({str(x).strip() for x in (namen or []) if str(x).strip()})]


# --------------------------------------------------------------------------
# The tree on disk – in the export folder's state.db
# --------------------------------------------------------------------------
# The old file names remain the callers' addresses; here they become kv
# keys. state_db is imported late (it itself imports this module for
# baum_diff).
SCHLUESSEL = {DATEI: "baum", KALENDER: "kalender", NOTIZBUECHER: "notizbuecher"}


def _db(ordner):
    import state_db
    return state_db.StateDb(ordner)


def lade(ordner, datei=DATEI):
    """Read the tree. Missing or broken: None, no fuss."""
    try:
        daten = json.loads(_db(ordner).kv_lesen(SCHLUESSEL[datei]) or "")
    except (OSError, ValueError):
        return None
    if not isinstance(daten, dict) or not isinstance(daten.get("ordner"), list):
        return None
    return daten


def baum_diff(eintraege, vorher=None):
    """The tree data plus what changed – pure, for every storage backend."""
    alt = {e["id"]: e for e in (vorher or {}).get("ordner", [])}
    jetzt = {e["id"]: e for e in eintraege}
    # On the very first sync nothing is "new" – there was nothing before.
    # "400 new folders added" would be formally true and still nonsense.
    erster = not alt
    return {
        "abgeglichen": datetime.now(UTC).isoformat(timespec="seconds"),
        "ordner": eintraege,
        "neu": [] if erster else sorted(
            e["pfad"] for k, e in jetzt.items() if k not in alt),
        "verschwunden": sorted(e["pfad"] for k, e in alt.items() if k not in jetzt),
        "umbenannt": sorted(
            f'{alt[k]["pfad"]} -> {e["pfad"]}'
            for k, e in jetzt.items() if k in alt and alt[k]["pfad"] != e["pfad"]),
    }


def speichere(ordner, eintraege, vorher=None, datei=DATEI):
    """Store the tree and report what changed.

    New folders are the reason for the return value: after a sync the
    interface should be able to say "4 new folders", instead of them
    arriving unnoticed and, depending on the rules, riding along or missing.
    """
    daten = baum_diff(eintraege, vorher)
    _db(ordner).kv_schreiben(SCHLUESSEL[datei],
                             json.dumps(daten, ensure_ascii=False))
    return daten


def gewaehlt(daten, regeln):
    """The entries that get exported according to the rules."""
    return [e for e in (daten or {}).get("ordner", []) if gilt(e["pfad"], regeln)]


def zusammenfassung(daten, regeln):
    """How many folders and mails the selection hits – for the interface."""
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


def auf_platte(ordner, wurzeln=(), endung=".eml"):
    """What really lies in the archive: {folder path: number of .eml files}.

    Only below the named roots – otherwise `kalender/` and `kontakte/`
    would count as mailbox folders, which they never were. The roots come
    from the tree itself, so no folder name is hard-wired here.

    `endung` narrows what counts: for the mailbox the `.eml`, for the
    OneDrive mirror everything except half-transferred `.teil` files.

    On a real archive (around 45,000 mails, a good 400 folders) this takes
    0.06 s – cheap
    enough to redo it on every opening of the list instead of maintaining
    an intermediate state that can be wrong.
    """
    gefunden = {}
    basis = Path(ordner)
    for wurzel in dict.fromkeys(wurzeln or ()):
        for verzeichnis, _unter, dateien in os.walk(basis / wurzel):
            anzahl = sum(1 for d in dateien
                         if (d.lower().endswith(endung) if endung
                             else not d.endswith(".teil")))
            if anzahl:
                gefunden[Path(verzeichnis).relative_to(basis).as_posix()] = anzahl
    return gefunden


def plan(ordner, regeln, daten=None, endung=".eml", datei=DATEI, archiv=None):
    """What the next export would do – folder by folder, without starting it.

    The rules are powerful enough that their outcome no longer forms in
    one's head: "- E-Mail/Archiv/**" and two lines later a "+" on a
    subfolder – whoever has to work that out will eventually get it wrong.
    Hence three explicit lists instead of one number:

      an   what comes along
      aus  what is left out, including the rule that decided it
      weg  what only lies in the archive and no longer appears in the mailbox

    The third is the one you see nowhere else: a folder deleted or renamed
    in Outlook silently vanishes from the tree, but its mails remain –
    rightly – on disk.
    """
    daten = lade(ordner, datei) if daten is None else daten
    eintraege = (daten or {}).get("ordner", [])
    # `archiv` may arrive ready-made: a caller whose units hold their files
    # in subfolders (a notebook keeps its pages in section folders) counts
    # per unit itself instead of per directory.
    if archiv is None:
        archiv = auf_platte(ordner, [e["pfad"].split("/")[0] for e in eintraege], endung)
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
    """Shows the stored tree – and what the rules make of it."""
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
        print(f"Kein Ordnerbaum in {ordner} – erst abgleichen "
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
