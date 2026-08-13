# PyInstaller-Beschreibung für die gebündelte App.
#
#     pyinstaller packaging/app.spec --noconfirm
#
# Ergebnis: dist/Munimentum/ (onedir) und auf macOS zusätzlich
# dist/Munimentum.app. Onedir statt onefile mit Absicht: onefile
# entpackt bei jedem Start ~80 MB in ein Temp-Verzeichnis (spürbar langsam)
# und fällt Virenscannern häufiger auf.
#
# Die Teilprogramme (outlook_export.py, …) sind reine Skripte, die niemand
# importiert – ohne hiddenimports landeten sie nicht im Bündel und
# app.run_bundled() fände sie nicht.

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent          # noqa: F821  (SPECPATH setzt PyInstaller)

# Das App-Symbol. Beide Formate stammen aus packaging/icon/icon.svg und liegen
# fertig im Repo – so braucht der Build kein Zeichenwerkzeug. Neu erzeugen:
#
#   magick -background none packaging/icon/icon.svg \
#          -define icon:auto-resize=256,128,64,48,32,16 packaging/icon/icon.ico
#   (macOS: je Größe ein PNG in ein .iconset legen, dann `iconutil -c icns`)
#
# Windows liest das .ico aus der EXE, macOS das .icns aus dem Bündel. Linux
# kennt kein Symbol in der Binärdatei – dort bleibt es ohne Wirkung.
# Ausnahmen von der Hardened Runtime. Ohne sie startet die signierte App nicht
# (siehe packaging/signieren.md). Wird nur beim Signieren gebraucht, muss aber
# da sein, sobald es losgeht.
ENTITLEMENTS = ROOT / "packaging" / "entitlements.plist"
assert ENTITLEMENTS.exists(), "entitlements.plist fehlt – signierte Bündel starten damit nicht"

ICON_ICO = ROOT / "packaging" / "icon" / "icon.ico"
ICON_ICNS = ROOT / "packaging" / "icon" / "icon.icns"
for _p in (ICON_ICO, ICON_ICNS):
    assert _p.exists(), f"{_p.name} fehlt – ohne es trüge die App PyInstallers Standardsymbol"

# Versionsnummer aus version.py – nicht hier noch einmal pflegen.
_v = {}
exec((ROOT / "version.py").read_text(encoding="utf-8"), _v)
VERSION = _v["VERSION"]

TEILPROGRAMME = ["outlook_export", "teams_export", "onedrive_export", "rag_index",
                 "combined_search", "mcp_server", "corpus",
                 "auth", "export_util", "folders", "graph_client", "settings",
                 "i18n", "updates", "version", "store_layout",
                 "progress", "answer"]

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
    name="Munimentum",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                         # UPX macht Windows-Virenscanner nervös
    # Windows: kein Konsolenfenster beim Doppelklick. app.ensure_streams()
    # leitet stdout/stderr dann in app.log um, sonst wäre ein Fehlstart stumm.
    console=(sys.platform not in ("darwin", "win32")),
    icon=str(ICON_ICO),
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    # Signiert wird nur, wenn der Build die Identität mitgibt (Tag-Läufe, siehe
    # den Workflow). Ohne sie baut alles wie bisher unsigniert durch.
    codesign_identity=os.environ.get("MACOS_SIGN_IDENTITY") or None,
    entitlements_file=str(ENTITLEMENTS),
)

coll = COLLECT(                        # noqa: F821
    exe, a.binaries, a.datas,
    strip=False, upx=False, name="Munimentum",
)

if sys.platform == "darwin":
    app = BUNDLE(                      # noqa: F821
        coll,
        name="Munimentum.app",
        icon=str(ICON_ICNS),
        bundle_identifier="de.nschilling.munimentum",
        info_plist={
            "CFBundleName": "Munimentum",
            "CFBundleDisplayName": "Munimentum",
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
