#!/usr/bin/env python3
"""
updates.py – nachsehen, ob es ein neueres Release gibt.

Fragt beim Start einmal die GitHub-API und hinterlässt bei Bedarf eine Notiz.
Mehr nicht: nichts wird heruntergeladen, nichts ersetzt. Wer aktualisieren
will, holt sich die Datei selbst von der Releases-Seite – bei einer nicht
signierten App ist ein stiller Selbstaustausch ohnehin nichts, was man wollen
sollte.

Abschaltbar (Einstellung "update_check"). Das ist keine Förmlichkeit: die App
spricht sonst mit nichts außer Microsoft Graph und dem lokalen Ollama, und
diese eine Verbindung nach draußen soll niemand ungefragt bekommen.

Vier Ausgänge, alle vier normal:

    ok      Release gefunden. "newer" sagt, ob es neuer ist als die eigene;
            "ahead" sagt das Gegenteil – die eigene ist HÖHER als alles
            Veröffentlichte, man läuft also auf einem selbstgebauten Stand.
    none    Es gibt noch gar kein Release (GitHub antwortet dann mit 404)
    off     Prüfung ist abgeschaltet
    error   Kein Netz, Sperre wegen zu vieler Anfragen, o. ä.

Meldenswert sind zwei Fälle: newer=True („es gibt etwas Neueres") und
ahead=True („du bist voraus"). Der zweite ist kein Fehler, aber „du bist auf
dem neuesten Stand" wäre dort schlicht unwahr – und wer eine unveröffentlichte
Fassung benutzt, sollte das wissen.
"""

import re

API = "https://api.github.com/repos/{repo}/releases/latest"

_TEIL = re.compile(r"\d+")


def parse_version(text):
    """"v1.12.0" -> (1, 12, 0). Unlesbares ergibt ein leeres Tupel.

    Bewusst nachsichtig: Vorabversionen wie "1.2.0-beta.1" werden auf ihre
    Zahlen reduziert. Ein Tag, aus dem sich gar keine Zahl lesen lässt, gilt
    als unvergleichbar – dann wird lieber nichts gemeldet als etwas Falsches.
    """
    kern = str(text or "").strip().lstrip("vV").split("+", 1)[0].split("-", 1)[0]
    return tuple(int(x) for x in _TEIL.findall(kern)[:4])


def is_newer(latest, current):
    """Ist `latest` eine höhere Version als `current`?"""
    a, b = parse_version(latest), parse_version(current)
    if not a or not b:
        return False                     # unvergleichbar -> nicht behaupten
    laenge = max(len(a), len(b))
    a += (0,) * (laenge - len(a))        # 1.2 und 1.2.0 sind dieselbe Version
    b += (0,) * (laenge - len(b))
    return a > b


def check(current, repo, timeout=4.0, enabled=True):
    """Einmal nachsehen. Wirft nie – ein Fehler hier darf nichts aufhalten.

    Deshalb ein einziger Fangzweig um alles: nicht nur um die Anfrage. Eine
    unerwartet geformte Antwort ist genauso wenig ein Grund, den Start der App
    scheitern zu lassen, wie ein fehlendes Netz.
    """
    out = {"status": "off", "current": current, "latest": None,
           "url": None, "newer": False, "ahead": False, "error": None}
    if not enabled:
        return out
    try:
        import requests
        r = requests.get(API.format(repo=repo), timeout=timeout,
                         headers={"Accept": "application/vnd.github+json"})
        if r.status_code == 404:
            # Noch kein Release veröffentlicht – oder nur Entwürfe und Vorab-
            # versionen, die dieser Endpunkt nicht mitzählt. Kein Fehlerfall.
            out["status"] = "none"
            return out
        if r.status_code != 200:
            out["status"], out["error"] = "error", f"HTTP {r.status_code}"
            return out
        daten = r.json()
        tag = (daten.get("tag_name") or daten.get("name") or "").strip()
        if not tag:
            out["status"] = "none"
            return out
        out["status"] = "ok"
        out["latest"] = tag.lstrip("vV")
        out["url"] = daten.get("html_url")
        out["newer"] = is_newer(tag, current)
        # Umgekehrt gefragt – und bewusst nicht als "nicht newer" abgeleitet:
        # bei gleicher Version und bei unvergleichbaren Nummern sind beide
        # falsch, und das ist richtig so.
        out["ahead"] = is_newer(current, tag)
    except Exception as e:
        out.update(status="error", latest=None, url=None, newer=False,
                   ahead=False, error=f"{type(e).__name__}: {e}")
    return out
