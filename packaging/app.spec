# PyInstaller description for the bundled app.
#
#     pyinstaller packaging/app.spec --noconfirm
#
# Result: dist/Munimentum/ (onedir) and on macOS additionally
# dist/Munimentum.app. Onedir instead of onefile on purpose: onefile
# unpacks ~80 MB into a temp directory on every start (noticeably slow)
# and catches virus scanners' attention more often.
#
# The subprograms (outlook_export.py, …) are pure scripts that nobody
# imports – without hiddenimports they would not land in the bundle and
# app.run_bundled() would not find them.

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent          # noqa: F821  (PyInstaller sets SPECPATH)

# The app icon. Both formats come from packaging/icon/icon.svg and sit
# ready-made in the repo – so the build needs no drawing tool. To regenerate:
#
#   magick -background none packaging/icon/icon.svg \
#          -define icon:auto-resize=256,128,64,48,32,16 packaging/icon/icon.ico
#   (macOS: put one PNG per size into an .iconset, then `iconutil -c icns`)
#
# Windows reads the .ico from the EXE, macOS the .icns from the bundle.
# Linux knows no icon in the binary – there it has no effect.
# Exceptions from the hardened runtime. Without them the signed app does not
# start (see packaging/signieren.md). Only needed when signing, but must be
# there once that begins.
ENTITLEMENTS = ROOT / "packaging" / "entitlements.plist"
assert ENTITLEMENTS.exists(), "entitlements.plist fehlt – signierte Bündel starten damit nicht"

ICON_ICO = ROOT / "packaging" / "icon" / "icon.ico"
ICON_ICNS = ROOT / "packaging" / "icon" / "icon.icns"
for _p in (ICON_ICO, ICON_ICNS):
    assert _p.exists(), f"{_p.name} fehlt – ohne es trüge die App PyInstallers Standardsymbol"

# Version number from version.py – do not maintain it here a second time.
_v = {}
exec((ROOT / "version.py").read_text(encoding="utf-8"), _v)
VERSION = _v["VERSION"]

TEILPROGRAMME = ["outlook_export", "teams_export", "onedrive_export", "rag_index",
                 "combined_search", "mcp_server", "corpus",
                 "auth", "export_util", "folders", "graph_client",
                 "ollama_client", "run_history", "settings",
                 "i18n", "updates", "version", "store_layout",
                 "progress", "answer", "notify",
                 "drive_mirror", "sharepoint_export", "state_db",
                 "planner_export", "analytics_db",
                 "steps", "runner"]

def ohne_cli(name):
    """mcp.cli needs typer – an optional extra we do not ship.
    Collecting imports every submodule, and this one would abort the whole
    build with ModuleNotFoundError."""
    return not name.startswith("mcp.cli")


hidden = list(TEILPROGRAMME)
if sys.platform == "darwin":
    # PyObjC resolves its framework bindings by name at runtime.
    hidden += ["objc", "Foundation", "UserNotifications",
               "PyObjCTools.AppHelper"]
# The MCP server runs on uvicorn/starlette; their protocol and lifecycle
# modules are loaded by name only at runtime.
for paket in ("uvicorn", "mcp", "anyio", "sse_starlette"):
    hidden += collect_submodules(paket, filter=ohne_cli)

# Some packages read their own version via importlib.metadata – without the
# .dist-info folders the import aborts in the bundle.
# The interface language files. i18n.py looks for them next to the
# executable or in the unpacked bundle – without them the app would speak
# only keys.
datas = [(str(p), "lang") for p in sorted((ROOT / "lang").glob("*.json"))]
assert datas, "lang/ ist leer – die Oberfläche hätte keine Texte"
# The interface itself and the API description that /api/openapi serves –
# app.py reads both from RES; without them the app would start blank.
datas += [(str(ROOT / "page.html"), "."), (str(ROOT / "openapi.yaml"), ".")]

for paket in ("mcp", "uvicorn", "starlette", "pydantic", "msal", "requests"):
    try:
        datas += copy_metadata(paket)
    except Exception:                 # package not installed: then not needed either
        pass

a = Analysis(                          # noqa: F821
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    # Only needed for the tests or not at all – saves considerable space.
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
    upx=False,                         # UPX makes Windows virus scanners nervous
    # Windows: no console window on double-click. app.ensure_streams() then
    # redirects stdout/stderr into app.log, otherwise a failed start would
    # be mute.
    console=(sys.platform not in ("darwin", "win32")),
    icon=str(ICON_ICO),
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    # Signing happens only when the build provides the identity (tag runs,
    # see the workflow). Without it everything builds unsigned as before.
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
            # false = a normal program. A Dock icon still appears only
            # briefly: the app opens no window of its own but serves the
            # browser, and therefore never talks to the window system. It
            # is quit via the button in the interface; starting it again
            # opens the running instance instead of creating a second one.
            "LSUIElement": False,
        },
    )
