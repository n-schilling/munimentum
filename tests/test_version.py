"""
version.py – the version and the build id, and the stamp that writes the
id into a bundle.
"""

import subprocess
import sys
import importlib.util
from pathlib import Path

import version

WURZEL = Path(__file__).resolve().parent.parent
# By path: "packaging" is also the name of a PyPI package on sys.path.
_spec = importlib.util.spec_from_file_location("stamp_build", WURZEL / "packaging" / "stamp_build.py")
stamp_build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stamp_build)


def test_build_kommt_aus_git_oder_dem_stempel(monkeypatch):
    monkeypatch.setattr(version, "_BUILD", None)
    monkeypatch.setattr(version, "BUILD", "")
    kurz = version.build()
    # this test runs in a checkout: git answers, seven hex characters
    assert len(kurz) == 7 and all(c in "0123456789abcdef" for c in kurz)
    assert version.build() is version.build()          # asked once
    monkeypatch.setattr(version, "_BUILD", None)
    monkeypatch.setattr(version, "BUILD", "abc1234")
    assert version.build() == "abc1234"                # the stamp wins
    monkeypatch.setattr(version, "_BUILD", None)
    monkeypatch.setattr(version, "BUILD", "")
    monkeypatch.setattr(version, "_aus_git", lambda: "")
    assert version.build() == ""                       # no git, no stamp: nothing


def test_stempel_schreibt_den_kurzen_hash(tmp_path):
    p = tmp_path / "version.py"
    p.write_text((WURZEL / "version.py").read_text(encoding="utf-8"), encoding="utf-8")
    assert stamp_build.stempeln(p, "E72756783A1ECF5D094BF70B06E2B4DCFC1BB976") == "e727567"
    assert 'BUILD = "e727567"' in p.read_text(encoding="utf-8")
    # a second stamp finds no empty line – the bundle is stamped once
    import pytest
    with pytest.raises(SystemExit):
        stamp_build.stempeln(p, "abc")
    with pytest.raises(SystemExit):
        stamp_build.stempeln(tmp_path / "version.py", "not-a-hash")


def test_stempel_als_skript(tmp_path):
    kopie = tmp_path / "packaging"
    kopie.mkdir()
    (tmp_path / "version.py").write_text((WURZEL / "version.py").read_text(encoding="utf-8"), encoding="utf-8")
    (kopie / "stamp_build.py").write_text((WURZEL / "packaging" / "stamp_build.py").read_text(encoding="utf-8"),
                                          encoding="utf-8")
    r = subprocess.run([sys.executable, str(kopie / "stamp_build.py"), "0123456789abcdef"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout.strip() == "build 0123456"
    assert 'BUILD = "0123456"' in (tmp_path / "version.py").read_text(encoding="utf-8")
    # the repository's own version.py stays unstamped
    assert 'BUILD = ""' in (WURZEL / "version.py").read_text(encoding="utf-8")
