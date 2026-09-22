#!/usr/bin/env python3
"""
stempel.py – write the version and the build id into the disk image's
background picture: `python packaging/dmg/stempel.py <version> <build> <out dir>`.

The committed pictures (background.png, background@2x.png) are the template,
rendered once from background.svg by hand (see the README here). This draws
one grey line into their foot – "Version 13.5.1 · build 59b24c3" at the
left, where the SVG leaves room, in the colour and size of the footer at
the right – and writes both sizes into <out dir>. dmgbuild takes the copy
(layout.py, `-D background=`), so the repository stays as it is.
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HIER = Path(__file__).resolve().parent
FARBE = (0x9A, 0xA4, 0xAE)          # the footer's grey – #9aa4ae in background.svg
X, Y, GROESSE = 40, 449, 11         # left margin, baseline, size – in points, as the SVG
# The picture's face where it is (macOS); a plain sans where it is not.
SCHRIFTEN = ("/System/Library/Fonts/HelveticaNeue.ttc",
             "/System/Library/Fonts/Helvetica.ttc",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")


def schrift(groesse):
    for pfad in SCHRIFTEN:
        try:
            return ImageFont.truetype(pfad, groesse)
        except OSError:
            continue
    return ImageFont.load_default(groesse)


def zeile(version, build):
    return f"Version {version} · build {build}" if build else f"Version {version}"


def stempeln(version, build, ziel, vorlage=HIER):
    """Both sizes, stamped, written under `ziel`; returns their paths."""
    ziel = Path(ziel)
    ziel.mkdir(parents=True, exist_ok=True)
    text, aus = zeile(version, build), []
    for name, faktor in (("background.png", 1), ("background@2x.png", 2)):
        bild = Image.open(vorlage / name)
        if bild.mode not in ("RGB", "RGBA"):
            bild = bild.convert("RGB")
        ImageDraw.Draw(bild).text((X * faktor, Y * faktor), text, fill=FARBE,
                                  font=schrift(GROESSE * faktor), anchor="ls")
        bild.save(ziel / name)
        aus.append(ziel / name)
    return aus


def main():
    if len(sys.argv) != 4:
        raise SystemExit("usage: stempel.py <version> <build> <out dir>")
    for pfad in stempeln(sys.argv[1], sys.argv[2], sys.argv[3]):
        print(pfad)


if __name__ == "__main__":
    main()
