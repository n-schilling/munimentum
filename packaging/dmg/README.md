# The macOS disk image

`layout.py` is the [dmgbuild](https://dmgbuild.readthedocs.io/) description
of the window a user sees after double-clicking the DMG: the background
picture, the app on the left, the *Applications* link on the right, icon size
and window size. The workflow runs it in *Package (macOS)*.

`background.svg` is the source of the picture; `background.png` (720 × 460)
and `background@2x.png` (1440 × 920, Retina) are what ships – dmgbuild folds
both into one TIFF inside the image. After changing the SVG, render both again
on a Mac (QuickLook renders with the system fonts; it pads the thumbnail to a
square, hence the crop):

```sh
qlmanage -t -s 720  -o /tmp/dmg1 packaging/dmg/background.svg
qlmanage -t -s 1440 -o /tmp/dmg2 packaging/dmg/background.svg
magick /tmp/dmg1/background.svg.png -crop 720x460+0+0  +repage -strip packaging/dmg/background.png
magick /tmp/dmg2/background.svg.png -crop 1440x920+0+0 +repage -strip packaging/dmg/background@2x.png
```

The version and the build id are not in the SVG: `stempel.py` draws them at
build time – *Version 13.1.0 · build 59b24c3* – into the foot at the left,
in the footer's grey and size, and writes both sizes into `build/dmg/`; the
workflow hands that copy to dmgbuild (`-D background=`) and names the volume
after the version, which is the window's title. Keep that corner of the SVG
empty. To try it: `python packaging/dmg/stempel.py 13.1.0 abc1234 /tmp/dmg`.

Icon positions in `layout.py` are the centres of the icons in points from the
top-left corner of the window content; they must match the drawing. The
window height in `layout.py` is the picture height plus the title bar.

To look at the result without a full build, any folder named `Munimentum.app`
with an `Info.plist` and the icon will do:

```sh
pip install "dmgbuild[badge-icons]"
dmgbuild -s packaging/dmg/layout.py -D app=path/to/Munimentum.app "Munimentum" preview.dmg
open preview.dmg
```
