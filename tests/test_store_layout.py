"""Tests für store_layout.py – welche Vektordatei gilt und wann sie weggeht.

Hintergrund ist ein Fehler aus der Praxis: unter Windows ließ sich vectors.npy
nicht ersetzen, solange irgendein Leser sie per mmap offen hielt – der
MCP-Server, die Suche in der App, ein von Claude gestarteter Server. Der
Indexlauf starb dort in der letzten Zeile, nachdem er alles eingebettet hatte.

Ersetzt wird deshalb nichts mehr: jeder Lauf schreibt eine neue Datei und
info.json sagt, welche gilt. Hier steht, dass diese Zuordnung in allen drei
Fällen stimmt – neuer Store, Lauf ohne Embeddings, Store von früher.
"""

import json

import pytest

import store_layout


def schreibe_info(ordner, **eintraege):
    (ordner / "info.json").write_text(json.dumps(eintraege), encoding="utf-8")


def lege_an(ordner, *namen):
    for n in namen:
        (ordner / n).write_bytes(b"x")


# --------------------------------------------------------------------------
# Welche Datei gilt
# --------------------------------------------------------------------------
def test_info_bei_fehlender_datei(tmp_path):
    assert store_layout.info(tmp_path) == {}


def test_info_bei_kaputter_datei(tmp_path):
    (tmp_path / "info.json").write_text("{kein json", encoding="utf-8")
    assert store_layout.info(tmp_path) == {}


def test_eintrag_bestimmt_die_datei(tmp_path):
    lege_an(tmp_path, "vectors-2.npy", "vectors-7.npy")
    schreibe_info(tmp_path, vectors="vectors-7.npy")
    assert store_layout.vectors_path(tmp_path).name == "vectors-7.npy"


def test_leerer_eintrag_heisst_keine_vektoren(tmp_path):
    """Ein Lauf ohne Embeddings zieht die Vektoren zurück. Läge die alte Datei
    noch da, weil sie sich nicht löschen ließ, dürfte sie NICHT wieder gelten –
    ihre Zeilen passen nicht mehr zur neu geschriebenen DB."""
    lege_an(tmp_path, store_layout.LEGACY, "vectors-3.npy")
    schreibe_info(tmp_path, vectors=None)
    assert store_layout.vectors_path(tmp_path) is None


def test_store_von_frueher_behaelt_seine_vektoren(tmp_path):
    """Ein Index, der vor der Umstellung gebaut wurde, kennt den Eintrag nicht.
    Ohne diesen Rückfall stünde er nach dem Update ohne Embeddings da und die
    Suche fiele stillschweigend auf reines BM25 zurück."""
    lege_an(tmp_path, store_layout.LEGACY)
    schreibe_info(tmp_path, model="bge-m3")            # info.json ohne "vectors"
    assert store_layout.vectors_path(tmp_path).name == store_layout.LEGACY


def test_ganz_ohne_info_und_ohne_datei(tmp_path):
    assert store_layout.vectors_path(tmp_path) is None


def test_eintrag_zeigt_ins_leere(tmp_path):
    """Genannt, aber nicht da: kein Absturz, sondern kein Vektorteil."""
    schreibe_info(tmp_path, vectors="vectors-9.npy")
    assert store_layout.vectors_path(tmp_path) is None


# --------------------------------------------------------------------------
# Der nächste Name
# --------------------------------------------------------------------------
def test_erster_name(tmp_path):
    assert store_layout.next_vectors_path(tmp_path).name == "vectors-1.npy"


def test_zaehlt_hoch(tmp_path):
    lege_an(tmp_path, "vectors-1.npy", "vectors-2.npy")
    assert store_layout.next_vectors_path(tmp_path).name == "vectors-3.npy"


def test_zaehlt_nach_dem_ordner_nicht_nach_info(tmp_path):
    """Eine Datei, die beim Aufräumen nicht wegging, hält noch ein Leser offen.
    Sie zu überschreiben wäre genau der Fehler, um den es hier geht."""
    lege_an(tmp_path, "vectors-4.npy", "vectors-5.npy")
    schreibe_info(tmp_path, vectors="vectors-4.npy")   # 5 ist verwaist
    assert store_layout.next_vectors_path(tmp_path).name == "vectors-6.npy"


def test_ignoriert_fremde_namen(tmp_path):
    lege_an(tmp_path, "vectors-alt.npy", "vectors-2.npy.tmp", "vectors-3.npy")
    assert store_layout.next_vectors_path(tmp_path).name == "vectors-4.npy"


# --------------------------------------------------------------------------
# Aufräumen
# --------------------------------------------------------------------------
def test_raeumt_die_vorigen_weg(tmp_path):
    lege_an(tmp_path, "vectors-1.npy", "vectors-2.npy", "vectors-3.npy")
    assert store_layout.prune_vectors(tmp_path, tmp_path / "vectors-3.npy") == 2
    assert [p.name for p in sorted(tmp_path.glob("vectors-*.npy"))] == ["vectors-3.npy"]


def test_raeumt_auch_den_alten_festen_namen(tmp_path):
    """Nach dem ersten Lauf auf einem Store von früher bleibt vectors.npy sonst
    für immer liegen – bei einem echten Bestand einige hundert Megabyte."""
    lege_an(tmp_path, store_layout.LEGACY, "vectors-1.npy")
    store_layout.prune_vectors(tmp_path, tmp_path / "vectors-1.npy")
    assert not (tmp_path / store_layout.LEGACY).exists()


def test_ohne_zu_behaltende_datei_geht_alles(tmp_path):
    lege_an(tmp_path, store_layout.LEGACY, "vectors-1.npy")
    assert store_layout.prune_vectors(tmp_path) == 2


def test_was_sich_nicht_loeschen_laesst_bleibt_liegen(tmp_path, monkeypatch):
    """Der Normalfall unter Windows direkt nach einem Lauf: der noch laufende
    MCP-Server hält die vorige Fassung. Das ist kein Fehler und darf den Lauf
    nicht aufhalten – die Datei kostet Platz bis zum nächsten Mal."""
    lege_an(tmp_path, "vectors-1.npy", "vectors-2.npy")
    echt = type(tmp_path).unlink

    def stur(self, *a, **k):
        if self.name == "vectors-1.npy":
            raise PermissionError(13, "Zugriff verweigert")
        return echt(self, *a, **k)

    monkeypatch.setattr(type(tmp_path), "unlink", stur)
    assert store_layout.prune_vectors(tmp_path, tmp_path / "vectors-2.npy") == 0
    assert (tmp_path / "vectors-1.npy").exists(), "sie liegt weiter da – gewollt"


@pytest.mark.parametrize("behalten", ["vectors-2.npy", None])
def test_aufraeumen_im_leeren_ordner(tmp_path, behalten):
    assert store_layout.prune_vectors(
        tmp_path, tmp_path / behalten if behalten else None) == 0
