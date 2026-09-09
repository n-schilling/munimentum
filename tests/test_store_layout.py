"""Tests for store_layout.py – which vector file is valid and when it goes.

The background is a bug from the field: on Windows, vectors.npy could not
be replaced while any reader held it open via mmap – the MCP server, the
search in the app, a server started by Claude. The index run died there on
its last line after embedding everything.

So nothing is replaced any more: every run writes a new file and info.json
says which one is valid. This file asserts that the mapping holds in all
three cases – new store, run without embeddings, legacy store.
"""

import json
from pathlib import Path

import pytest

import store_layout


def schreibe_info(ordner, **eintraege):
    (ordner / "info.json").write_text(json.dumps(eintraege), encoding="utf-8")


def lege_an(ordner, *namen):
    for n in namen:
        (ordner / n).write_bytes(b"x")


# --------------------------------------------------------------------------
# Which file is valid
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
    """A run without embeddings retires the vectors. If the old file were
    still there because it could not be deleted, it must NOT become valid
    again – its rows no longer fit the freshly written DB."""
    lege_an(tmp_path, store_layout.LEGACY, "vectors-3.npy")
    schreibe_info(tmp_path, vectors=None)
    assert store_layout.vectors_path(tmp_path) is None


def test_store_von_frueher_behaelt_seine_vektoren(tmp_path):
    """An index built before the changeover does not know the entry.
    Without this fallback it would stand there without embeddings after the
    update, and search would silently fall back to plain BM25."""
    lege_an(tmp_path, store_layout.LEGACY)
    schreibe_info(tmp_path, model="bge-m3")            # info.json without "vectors"
    assert store_layout.vectors_path(tmp_path).name == store_layout.LEGACY


def test_ganz_ohne_info_und_ohne_datei(tmp_path):
    assert store_layout.vectors_path(tmp_path) is None


def test_eintrag_zeigt_ins_leere(tmp_path):
    """Named but not there: no crash, just no vector part."""
    schreibe_info(tmp_path, vectors="vectors-9.npy")
    assert store_layout.vectors_path(tmp_path) is None


# --------------------------------------------------------------------------
# The next name
# --------------------------------------------------------------------------
def test_erster_name(tmp_path):
    assert store_layout.next_vectors_path(tmp_path).name == "vectors-1.npy"


def test_zaehlt_hoch(tmp_path):
    lege_an(tmp_path, "vectors-1.npy", "vectors-2.npy")
    assert store_layout.next_vectors_path(tmp_path).name == "vectors-3.npy"


def test_zaehlt_nach_dem_ordner_nicht_nach_info(tmp_path):
    """A file that did not go during clean-up is still held open by a
    reader. Overwriting it would be exactly the bug this is all about."""
    lege_an(tmp_path, "vectors-4.npy", "vectors-5.npy")
    schreibe_info(tmp_path, vectors="vectors-4.npy")   # 5 is orphaned
    assert store_layout.next_vectors_path(tmp_path).name == "vectors-6.npy"


def test_ignoriert_fremde_namen(tmp_path):
    lege_an(tmp_path, "vectors-alt.npy", "vectors-2.npy.tmp", "vectors-3.npy")
    assert store_layout.next_vectors_path(tmp_path).name == "vectors-4.npy"


# --------------------------------------------------------------------------
# Cleaning up
# --------------------------------------------------------------------------
def test_raeumt_die_vorigen_weg(tmp_path):
    lege_an(tmp_path, "vectors-1.npy", "vectors-2.npy", "vectors-3.npy")
    assert store_layout.prune_vectors(tmp_path, tmp_path / "vectors-3.npy") == 2
    assert [p.name for p in sorted(tmp_path.glob("vectors-*.npy"))] == ["vectors-3.npy"]


def test_raeumt_auch_den_alten_festen_namen(tmp_path):
    """After the first run on a legacy store, vectors.npy would otherwise
    lie around forever – several hundred megabytes on a real corpus."""
    lege_an(tmp_path, store_layout.LEGACY, "vectors-1.npy")
    store_layout.prune_vectors(tmp_path, tmp_path / "vectors-1.npy")
    assert not (tmp_path / store_layout.LEGACY).exists()


def test_ohne_zu_behaltende_datei_geht_alles(tmp_path):
    lege_an(tmp_path, store_layout.LEGACY, "vectors-1.npy")
    assert store_layout.prune_vectors(tmp_path) == 2


def test_was_sich_nicht_loeschen_laesst_bleibt_liegen(tmp_path, monkeypatch):
    """The normal case on Windows right after a run: the still-running MCP
    server holds the previous version. That is not an error and must not
    hold up the run – the file costs space until next time."""
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


def test_db_path():
    assert store_layout.db_path("rag_store") == Path("rag_store") / "corpus.db"
