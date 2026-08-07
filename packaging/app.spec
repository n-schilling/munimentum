# PyInstaller-Beschreibung für die gebündelte App.
#
#     pyinstaller packaging/app.spec --noconfirm
#
# Ergebnis: dist/Microsoft365-Archiv/ (onedir) und auf macOS zusätzlich
# dist/Microsoft365-Archiv.app. Onedir statt onefile mit Absicht: onefile
# entpackt bei jedem Start ~80 MB in ein Temp-Verzeichnis (spürbar langsam)
# und fällt Virenscannern häufiger auf.
#
# Die Teilprogramme (outlook_export.py, …) sind reine Skripte, die niemand
# importiert – ohne hiddenimports landeten sie nicht im Bündel und
# app.run_bundled() fände sie nicht.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent          # noqa: F821  (SPECPATH setzt PyInstaller)

# Versionsnummer aus version.py – nicht hier noch einmal pflegen.
_v = {}
exec((ROOT / "version.py").read_text(encoding="utf-8"), _v)
VERSION = _v["VERSION"]

TEILPROGRAMME = ["outlook_export", "teams_export", "rag_index",
                 "combined_search", "mcp_server", "rag_server", "corpus",
                 "settings", "i18n", "updates", "version"]

def ohne_cli(name):
    """mcp.cli braucht typer – ein optionales Extra, das wir nicht mitliefern.
    Beim Einsammeln wird jedes Untermodul importiert, und dieses eine würde den
    ganzen Build mit ModuleNotFoundError abbrechen."""
    return not name.startswith("mcp.cli")


hidden = list(TEILPROGRAMME)
# Der MCP-Server läuft über uvicorn/starlette; deren Protokoll- und
# Lifecycle-Module werden erst zur Laufzeit nach Namen geladen.
for paket in ("uvicorn", "mcp", "anyio", "sse_starlette"):
    hidden += collect_submodules(paket, filter=ohne_cli)

# Einige Pakete lesen ihre eigene Version über importlib.metadata – ohne die
# .dist-info-Ordner bricht der Import im Bündel ab.
# Die Sprachdateien der Oberfläche. i18n.py sucht sie neben der ausführbaren
# Datei bzw. im entpackten Bündel – ohne sie spräche die App nur Schlüssel.
datas = [(str(p), "lang") for p in sorted((ROOT / "lang").glob("*.json"))]
assert datas, "lang/ ist leer – die Oberfläche hätte keine Texte"

for paket in ("mcp", "uvicorn", "starlette", "pydantic", "msal", "requests"):
    try:
        datas += copy_metadata(paket)
    except Exception:                 # Paket nicht installiert: dann auch nicht nötig
        pass

a = Analysis(                          # noqa: F821
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    # Nur für die Tests bzw. gar nicht gebraucht – spart deutlich Platz.
    excludes=["tkinter", "pytest", "coverage", "ruff", "matplotlib",
              "PIL", "scipy", "pandas", "IPython", "setuptools", "pip"],
    noarchive=False,
)
pyz = PYZ(a.pure)                      # noqa: F821

exe = EXE(                             # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Microsoft365-Archiv",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                         # UPX macht Windows-Virenscanner nervös
    # Windows: kein Konsolenfenster beim Doppelklick. app.ensure_streams()
    # leitet stdout/stderr dann in app.log um, sonst wäre ein Fehlstart stumm.
    console=(sys.platform not in ("darwin", "win32")),
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(                        # noqa: F821
    exe, a.binaries, a.datas,
    strip=False, upx=False, name="Microsoft365-Archiv",
)

if sys.platform == "darwin":
    app = BUNDLE(                      # noqa: F821
        coll,
        name="Microsoft365-Archiv.app",
        icon=None,
        bundle_identifier="de.nschilling.office365export",
        info_plist={
            "CFBundleName": "Microsoft365-Archiv",
            "CFBundleDisplayName": "Microsoft 365 Archiv",
            "CFBundleShortVersionString": VERSION,
            "NSHighResolutionCapable": True,
            # false = normales Programm. Ein Dock-Symbol erscheint trotzdem
            # nur kurz: die App öffnet kein Fenster, sondern bedient den
            # Browser, und spricht deshalb nie mit dem Fenstersystem. Beendet
            # wird sie über den Knopf in der Oberfläche; ein erneuter Start
            # öffnet die laufende Instanz, statt eine zweite anzulegen.
            "LSUIElement": False,
        },
    )
