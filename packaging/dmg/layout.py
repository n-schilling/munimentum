# Layout of the macOS disk image, read by dmgbuild (see build.yml,
# "Package (macOS)"):
#
#     dmgbuild -s packaging/dmg/layout.py -D app=dist/Munimentum.app \
#              "Munimentum" Munimentum-macos-arm64.dmg
#
# The Finder window shows background.png (background@2x.png on Retina;
# dmgbuild folds both into one TIFF) with the app on the left and the
# Applications link on the right. Coordinates are points from the top-left
# corner of the window content and name the centre of each icon – they must
# match the drawing in background.svg. Paths are relative to the repository
# root, where the workflow runs; dmgbuild executes this file without
# `__file__`. Rendering the SVG: see the README in this folder.
import os.path

application = defines.get("app", "dist/Munimentum.app")  # noqa: F821  (dmgbuild injects `defines`)

files = [application]
symlinks = {"Applications": "/Applications"}

# The volume shows the app icon badged onto the standard disk-image icon.
badge_icon = "packaging/icon/icon.icns"
background = "packaging/dmg/background.png"

# The bounds describe the window frame, title bar included – 32 pt on
# macOS 26 (28 on older systems, which then show a 4 pt strip of window
# colour below the picture). The image itself is 720 × 460.
window_rect = ((200, 120), (720, 492))
default_view = "icon-view"
icon_size = 128
text_size = 13
show_icon_preview = False
icon_locations = {
    os.path.basename(application): (200, 216),
    "Applications": (520, 216),
}

format = "UDZO"
