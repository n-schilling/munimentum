#!/usr/bin/env python3
"""
completeness.py – the one balance sheet every check writes.

The check answers a single question: would a run with today's settings
fetch anything now that does not lie here yet? Everything else follows.
"Expected" is what such a run would fetch – nothing the settings leave
out counts towards it, so an excluded folder can never produce a gap.
And the honest word for the difference is "not fetched yet", not
"missing": the check compares with Microsoft's state of now, not with
the state of the last run.

Four numbers per source, no more:

    da              here and current – from the export's own bookkeeping
    offen           expected but not here: new since the last run, changed
                    since, or failed then; the check does not tell apart
    ausgeschlossen  deliberately left out by the settings – a number only,
                    never a name (the export list is where names live)
    behalten        gone at Microsoft, kept here (tombstones, markers)

The mirrors add a fifth, `wartend`: files whose folder cadence is not due
yet. The balance always adds up – erwartet = da + offen + wartend – and
nothing is capped at zero. A source that is not ticked is not checked and
not counted, not even as excluded.

Rows (`zeilen`) name only the units with something open – a folder, a
calendar, a board, a list, a notebook, a site – with their `da` and
`offen`. `fehler` names units the check could not judge (a site that did
not answer); `stand` then says "teilweise". A check that could not run at
all ("nicht": OneNote's hour spent, no access) carries a `grund` – a text
key the page renders – instead of inventing a gap.

Every check gets the same environment as its export (rules, start days,
size and type filters) and writes nothing but this report, under
`pruefung:<quelle>` in the export folder's state.db. It never advances a
pointer, never fetches a file, never deletes anything.
"""

from datetime import datetime, UTC

import progress

GANZ, TEILWEISE, NICHT = "ganz", "teilweise", "nicht"
KEY = "pruefung:"


def zeile(pfad, da, offen):
    """One unit's line – kept only while something is open."""
    return {"pfad": str(pfad), "da": int(da), "offen": int(offen)}


def fehler(pfad, grund):
    """A unit the check could not judge; `grund` is a run.* text key."""
    return {"pfad": str(pfad), "grund": str(grund)}


def bilanz(quelle, einheit, *, da=0, offen=0, ausgeschlossen=0, behalten=0,
           wartend=0, ausgeschlossen_einheit=None, zeilen=(), fehler=(),
           stand=None, grund=None, extra=None):
    """Build one report. `zeilen` may carry every unit – only those with
    something open survive, sorted by what is open, then by path."""
    offene = sorted((z for z in zeilen if z.get("offen")),
                    key=lambda z: (-z["offen"], z["pfad"]))
    fehlende = list(fehler)
    if stand is None:
        stand = TEILWEISE if fehlende else GANZ
    bericht = {
        "quelle": quelle,
        "einheit": einheit,
        "geprueft": datetime.now(UTC).isoformat(timespec="seconds"),
        "stand": stand,
        "grund": grund,
        "da": int(da), "offen": int(offen),
        "ausgeschlossen": int(ausgeschlossen),
        "ausgeschlossen_einheit": ausgeschlossen_einheit or einheit,
        "behalten": int(behalten), "wartend": int(wartend),
        "zeilen": offene, "fehler": fehlende,
    }
    bericht.update(extra or {})
    return bericht


def nicht_geprueft(quelle, einheit, grund):
    """The check could not run: the report says why, and nothing else."""
    return bilanz(quelle, einheit, stand=NICHT, grund=grund)


def schreiben(db, bericht):
    db.bericht_schreiben(bericht, key=KEY + bericht["quelle"])


def lesen(db, quelle):
    return db.bericht_lesen(key=KEY + quelle)


OFFENE_GRENZE = 2000        # open items a mirror's report names by id


def abgeholt(db, quelle, rels):
    """After a targeted fetch: take the fetched files out of the stored
    report – off the open count of their row and the source, onto the
    "here" side, out of the named open items – so the row is right again
    without a second walk. Rows are matched by the longest path prefix
    (a folder row, or a library row above it). Nothing happens without
    a stored report."""
    bericht = lesen(db, quelle)
    if not bericht or not rels:
        return None
    zeilen = {z["pfad"]: z for z in bericht.get("zeilen") or []}
    weg = set(rels)
    for rel in rels:
        pfad = max((p for p in zeilen if rel.startswith(p + "/") or rel == p),
                   key=len, default=None)
        if pfad is None or zeilen[pfad]["offen"] <= 0:
            continue                    # nothing the report called open
        zeilen[pfad]["offen"] -= 1
        zeilen[pfad]["da"] += 1
        bericht["offen"] = max(0, bericht["offen"] - 1)
        bericht["da"] += 1
    bericht["zeilen"] = sorted((z for z in zeilen.values() if z["offen"] > 0),
                               key=lambda z: (-z["offen"], z["pfad"]))
    if "offene" in bericht:
        bericht["offene"] = [e for e in bericht["offene"] if e.get("rel") not in weg]
    schreiben(db, bericht)
    return bericht


def melden(*berichte):
    """The step's one result event: nothing new, the excluded count, and
    the balance in `extra` – summed over the reports of one step."""
    summe = {"present": 0, "open": 0, "kept": 0, "waiting": 0}
    ausgeschlossen = 0
    for b in berichte:
        summe["present"] += b["da"]
        summe["open"] += b["offen"]
        summe["kept"] += b["behalten"]
        summe["waiting"] += b["wartend"]
        ausgeschlossen += b["ausgeschlossen"]
    if not summe["waiting"]:
        summe.pop("waiting")
    progress.ergebnis(0, excluded=ausgeschlossen, extra=summe)


# ---------------------------------------------------------------------------
# The inward check (archive_check.py): the bookkeeping against the disk
# ---------------------------------------------------------------------------
# A second shape, the same spirit – five numbers per source, all of them
# files, and nothing is ever repaired, moved or deleted:
#
#     stimmig         record and file agree (mirrors: the size as well)
#     fehlt           the bookkeeping knows a file that is not there – the
#                     next run fetches it again
#     fremd           a file lies here that no bookkeeping knows – it stays
#     unvollstaendig  a mirrored file with a size other than recorded
#     verloren        a tombstone says the file was kept, and it is gone
#     vermerkt        the user noted the loss (state.db table verloren): the
#                     record stays, the file is not expected back

def zustandszeile(pfad, **zahlen):
    """One unit's line of the inward check – kept while anything is off."""
    z = {"pfad": str(pfad), "fehlt": 0, "fremd": 0, "unvollstaendig": 0, "verloren": 0}
    z.update({k: int(v) for k, v in zahlen.items()})
    return z


def zustand(quelle, *, stimmig=0, fehlt=0, fremd=0, unvollstaendig=0, verloren=0,
            vermerkt=0, zeilen=(), fehler=(), stand=None, grund=None, befunde=None,
            gekappt=False, beiseite=0, fehlt_seit=None, nachgeholt=None):
    """Build one source's inward report. Rows survive only with something
    off, sorted by how much is off, then by path. `befunde` names the
    files behind the four numbers (kind -> rels, capped – `gekappt` says
    so), `beiseite` counts what an earlier "set aside" moved out,
    `vermerkt` what the user noted as lost. `fehlt_seit` dates the first
    report that found the missing files, `nachgeholt` the last resync of
    the source – together they say whether a fetch was tried since."""
    def gewicht(z):
        return z["fehlt"] + z["fremd"] + z["unvollstaendig"] + z["verloren"]
    auffaellig = sorted((z for z in zeilen if gewicht(z)),
                        key=lambda z: (-gewicht(z), z["pfad"]))
    fehlende = list(fehler)
    if stand is None:
        stand = TEILWEISE if fehlende else GANZ
    return {"quelle": quelle, "einheit": "files", "stand": stand, "grund": grund,
            "stimmig": int(stimmig), "fehlt": int(fehlt), "fremd": int(fremd),
            "unvollstaendig": int(unvollstaendig), "verloren": int(verloren),
            "vermerkt": int(vermerkt), "fehlt_seit": fehlt_seit, "nachgeholt": nachgeholt,
            "zeilen": auffaellig, "fehler": fehlende,
            "befunde": {k: list((befunde or {}).get(k) or ())
                        for k in ("fehlt", "fremd", "unvollstaendig", "verloren")},
            "gekappt": bool(gekappt), "beiseite": int(beiseite)}

