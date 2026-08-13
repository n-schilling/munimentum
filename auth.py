#!/usr/bin/env python3
"""
auth.py – die Anmeldung an Microsoft Graph, einmal für alle Skripte.

Es gibt zwei Wege, und sie unterscheiden sich in genau einer Eigenschaft, die
für den Zeitplan alles bedeutet:

  Zugangsschlüssel  Ein fertiger Access Token, von Hand aus dem Graph Explorer
  (Vorgabe)         geholt. Braucht niemanden in der IT, gilt aber nur wenige
                    Stunden und lässt sich nicht erneuern. Läuft er ab, steht
                    jeder Lauf, bis jemand einen neuen einfügt.

  Anmelden          Richtige Anmeldung über MSAL. Der Cache auf der Platte hält
                    ein Refresh Token, aus dem sich wochenlang neue Access
                    Tokens ausstellen lassen – der Zeitplan überlebt damit auch
                    einen Neustart. Voreingestellt ist Microsofts eigene
                    öffentliche Anwendung „Graph Command Line Tools“, für die es
                    keine Registrierung braucht; wer eine eigene App-Registrie-
                    rung hat, trägt deren Client-ID und Tenant ein.

Beides ist über settings.py konfigurierbar, gilt also gleichermaßen für die App
und für einen Aufruf von Hand im Terminal. Dieses Modul kennt app.py nicht und
darf es nie kennen – die Skripte müssen ohne die App laufen.

Was hier NICHT liegt: die HTTP-Schicht. Die Skripte haben unterschiedliche
Timeouts, Drosselungsregeln und Wiederholungszähler, und die zusammenzulegen
hieße, echte Unterschiede zu verstecken. Geteilt wird, was wirklich dasselbe
war: das Einlesen des Schlüssels und die Anmeldung.
"""

import os
import sys
from pathlib import Path

import settings

# Microsofts eigene öffentliche Anwendung. Sie ist in praktisch jedem Tenant
# vorab zugelassen – deshalb kommt der Login-Weg ohne eine einzige Rückfrage bei
# der IT aus. Eine eigene Registrierung ist nur nötig, wenn der Tenant sie
# ausdrücklich verlangt.
STANDARD_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
STANDARD_TENANT = "organizations"

TOKEN_DATEI = "gx_token.txt"
CACHE_DATEI = "msal_cache.bin"

RES = "https://graph.microsoft.com/"


class TokenExpired(RuntimeError):
    """Ein 401 im Schlüssel-Modus – dort ist keine Erneuerung möglich."""


# --------------------------------------------------------------------------
# Konfiguration
# --------------------------------------------------------------------------
def modus():
    """„token“ (Vorgabe) oder „login“.

    Unbekanntes ergibt „token“: der Weg, der immer funktioniert, ist auch der,
    auf den ein Tippfehler zurückfallen soll.
    """
    roh = (os.environ.get("GRAPH_AUTH")
           or settings.value("auth_mode") or "token")
    roh = str(roh).strip().lower()
    return "login" if roh == "login" else "token"


def client_id():
    return (os.environ.get("GRAPH_CLIENT_ID")
            or settings.value("client_id") or STANDARD_CLIENT_ID)


def tenant():
    return (os.environ.get("GRAPH_TENANT")
            or settings.value("tenant") or STANDARD_TENANT)


def authority():
    return f"https://login.microsoftonline.com/{tenant()}"


def eigene_registrierung():
    """Zeigt die Anmeldung auf eine eigene App-Registrierung?"""
    return (client_id(), tenant()) != (STANDARD_CLIENT_ID, STANDARD_TENANT)


def device_code():
    """Code-Login statt Browser – für Rechner ohne Anzeige."""
    return settings.flag("GRAPH_DEVICE_CODE", "device_code")


# --------------------------------------------------------------------------
# Schlüssel-Modus
# --------------------------------------------------------------------------
def token_datei():
    return settings.config_path().parent / TOKEN_DATEI


def load_pasted_token():
    """Den eingefügten Zugangsschlüssel lesen – Umgebung schlägt Datei.

    Verträgt, was beim Kopieren aus dem Graph Explorer mitkommt: Anführungs-
    zeichen, ein vorangestelltes „Bearer “, Zeilenumbrüche.
    """
    val = os.environ.get("GRAPH_TOKEN")
    if not val:
        # Erst neben dem Aufruf, dann im Datenordner. Die Reihenfolge ist die
        # dokumentierte: „gx_token.txt neben dieses Skript legen“ – wer das tut,
        # soll damit auch gewinnen.
        for p in (Path(TOKEN_DATEI), token_datei()):
            try:
                if p.exists():
                    val = p.read_text(encoding="utf-8")
                    break
            except OSError:
                continue
    if not val:
        return None
    val = val.strip().strip('"').strip("'").strip()
    if val.lower().startswith("bearer "):
        val = val[7:].strip()
    return val or None


# --------------------------------------------------------------------------
# Login-Modus
# --------------------------------------------------------------------------
def cache_datei():
    return settings.config_path().parent / CACHE_DATEI


class _Cache:
    """MSAL-Cache auf der Platte – der Grund, warum der Zeitplan durchhält.

    Ohne ihn lebt das Refresh Token nur im Arbeitsspeicher: ein Neustart der
    App, und die nächste Anmeldung ist wieder von Hand. Die Datei enthält genau
    dieses Refresh Token und wird deshalb nur für den Besitzer lesbar angelegt.
    """

    def __init__(self, pfad):
        import msal
        self.pfad = Path(pfad)
        self.cache = msal.SerializableTokenCache()
        try:
            if self.pfad.exists():
                self.cache.deserialize(self.pfad.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass                      # kaputter Cache = einmal neu anmelden

    def sichern(self):
        if not self.cache.has_state_changed:
            return
        try:
            self.pfad.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.pfad.with_name(self.pfad.name + ".tmp")
            tmp.write_text(self.cache.serialize(), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass                  # Windows kennt den Modus nicht
            tmp.replace(self.pfad)
        except OSError:
            pass                      # kein Schreibrecht: dann eben je Lauf neu


def cache_leeren():
    """Abmelden: das Refresh Token verwerfen."""
    try:
        cache_datei().unlink(missing_ok=True)
        return True
    except OSError:
        return False


class Login:
    """Anmeldung über MSAL, mit stiller Erneuerung.

    `scopes` gibt das aufrufende Skript vor – Outlook braucht andere Rechte als
    Teams, und mehr anzufordern als nötig wäre schlechter Stil gegenüber dem,
    der zustimmen soll.
    """

    def __init__(self, scopes, ausgabe=print, client=None, mandant=None):
        # `client`/`mandant` reicht die App durch: sie führt ihre Konfiguration
        # selbst und schreibt sie nicht erst in eine Datei, die settings.py
        # danach wieder einliest. Ohne Angabe gilt, was konfiguriert ist – so
        # bleibt der Aufruf von Hand im Terminal unverändert.
        import msal
        self.scopes = list(scopes)
        self.ausgabe = ausgabe
        self._cache = _Cache(cache_datei())
        self.app = msal.PublicClientApplication(
            client or client_id(),
            authority=f"https://login.microsoftonline.com/{mandant or tenant()}",
            token_cache=self._cache.cache)
        self.account = None
        self.token = None
        self.fehler = ""

    # -- innen ------------------------------------------------------------
    def _fertig(self, res):
        if not res or "access_token" not in res:
            return False
        self.token = res["access_token"]
        accs = self.app.get_accounts()
        self.account = accs[0] if accs else self.account
        self._cache.sichern()
        return True

    def _still(self):
        """Aus dem Cache erneuern, ohne den Anwender zu behelligen."""
        for acc in ([self.account] if self.account else []) or self.app.get_accounts():
            if not acc:
                continue
            self.account = acc
            if self._fertig(self.app.acquire_token_silent(self.scopes, account=acc)):
                return True
        return False

    def _interaktiv(self):
        if device_code():
            flow = self.app.initiate_device_flow(scopes=self.scopes)
            if "user_code" not in flow:
                raise RuntimeError("Device-Flow fehlgeschlagen: "
                                   + str(flow.get("error_description")))
            self.ausgabe("\n" + flow["message"] + "\n")
            return self.app.acquire_token_by_device_flow(flow)
        return self.app.acquire_token_interactive(scopes=self.scopes,
                                                  prompt="select_account")

    # -- außen ------------------------------------------------------------
    def anmelden(self, nur_still=False, weich=False):
        """Token besorgen.

        `nur_still` fragt den Anwender nicht – der Zeitplan setzt das: dort
        sitzt niemand vor dem Bildschirm, und ein Anmeldefenster, das um drei
        Uhr nachts aufgeht und bis zum Morgen wartet, hilft niemandem.

        `weich` liefert bei Misserfolg False statt abzubrechen. Der Teams-Export
        braucht das: er fragt erst die Kanalrechte an und meldet sich, wenn die
        niemand gewährt, mit reinem Chat-Zugriff erneut an.
        """
        if self._still():
            return True
        if nur_still:
            return False
        res = self._interaktiv()
        if self._fertig(res):
            return True
        if weich:
            self.fehler = (res or {}).get("error_description", "") or ""
            return False
        raise SystemExit("Anmeldung fehlgeschlagen: "
                         + ((res or {}).get("error_description") or "unbekannt"))

    def erneuern(self):
        """Nach einem 401: still erneuern, sonst neu anmelden."""
        if self._still():
            return
        if not self._fertig(self._interaktiv()):
            raise SystemExit("Token-Erneuerung fehlgeschlagen.")

    def headers(self):
        return {"Authorization": f"Bearer {self.token}"}


class DeviceLogin:
    """Anmeldung über Gerätecode, in zwei Schritten.

    Für die Oberfläche im Browser der einzig sinnvolle Weg: ein natives
    Anmeldefenster (acquire_token_interactive) gehört zu einer Anwendung mit
    eigenem Fenster – diese hier hat keins. Stattdessen zeigt die Seite einen
    Code, den man auf einer Microsoft-Seite eingibt, und wartet.

    Getrennt in `start` und `warten`, weil `warten` blockiert, bis der Anwender
    fertig ist: der Aufrufer legt es in einen eigenen Faden und kann derweil
    den Code anzeigen.
    """

    def __init__(self, scopes, client=None, mandant=None):
        self.login = Login(scopes, ausgabe=lambda *_: None,
                           client=client, mandant=mandant)
        self.flow = None

    def start(self):
        self.flow = self.login.app.initiate_device_flow(scopes=self.login.scopes)
        if "user_code" not in self.flow:
            raise RuntimeError(str(self.flow.get("error_description")
                                   or "Gerätecode nicht erhalten"))
        return {"code": self.flow["user_code"],
                "url": self.flow.get("verification_uri")
                or "https://microsoft.com/devicelogin",
                "expires_in": int(self.flow.get("expires_in") or 900)}

    def warten(self):
        """Blockiert bis zur Zustimmung. Liefert (ok, meldung)."""
        try:
            res = self.login.app.acquire_token_by_device_flow(self.flow)
        except Exception as e:                   # noqa: BLE001 – nie den Faden reißen
            return False, f"{type(e).__name__}: {e}"
        if self.login._fertig(res):
            return True, ""
        return False, ((res or {}).get("error_description") or "abgebrochen")


def angemeldet(client=None, mandant=None):
    """Liegt ein brauchbarer Cache vor? Ohne den Anwender zu fragen.

    Für die Oberfläche: sie soll den Zustand anzeigen können, ohne dabei ein
    Anmeldefenster aufzureißen.
    """
    try:
        import msal                              # noqa: F401
    except ImportError:
        return None
    if not cache_datei().exists():
        return None
    try:
        anmeldung = Login([RES + "User.Read"], ausgabe=lambda *_: None,
                          client=client, mandant=mandant)
        for acc in anmeldung.app.get_accounts():
            return acc.get("username") or True
    except Exception:                            # noqa: BLE001 – nur eine Anzeige
        return None
    return None


# --------------------------------------------------------------------------
# Für die Skripte: einen Weg wählen und sagen, welcher es wurde
# --------------------------------------------------------------------------
def waehle_zugang(mit_schluessel, mit_login, ausgabe=print, nur_still=False):
    """Den konfigurierten Weg gehen – mit einem Rückfall, der Läufe rettet.

    `mit_schluessel(token)` und `mit_login()` bauen den jeweiligen Client; beide
    Skripte tun das unterschiedlich (Teams braucht die Kanal-Frage), deshalb
    kommen sie von dort.

    Der Rückfall geht nur in eine Richtung: ist „login“ eingestellt, aber kein
    Cache da, und liegt ein Schlüssel bereit, wird der genommen. Umgekehrt nicht
    – wer den Schlüssel-Modus wählt, soll nicht überraschend ein Anmeldefenster
    sehen.

    Im Login-Modus wird deshalb ZUERST still versucht. Ein Anmeldefenster ist
    eine Unterbrechung; es aufzureißen, obwohl ein gültiger Schlüssel bereitliegt,
    wäre genau die Überraschung, die der Rückfall verhindern soll.
    """
    gewaehlt = modus()
    if gewaehlt == "login":
        try:
            klient = mit_login(nur_still=True)      # ohne Rückfrage
            beschreibe(ausgabe)
            return klient
        except SystemExit:
            pass
        schluessel = load_pasted_token()
        if schluessel:
            ausgabe("Keine gültige Anmeldung im Zwischenspeicher – nutze den "
                    "hinterlegten Zugangsschlüssel für diesen Lauf.")
            return mit_schluessel(schluessel)
        if nur_still:                               # Zeitplan: niemand sitzt davor
            raise SystemExit("Keine gültige Anmeldung und kein Zugangsschlüssel.")
        beschreibe(ausgabe)
        return mit_login()                          # jetzt erst das Fenster
    schluessel = load_pasted_token()
    if schluessel:
        beschreibe(ausgabe)
        return mit_schluessel(schluessel)
    # Kein Schlüssel im Schlüssel-Modus: von Hand im Terminal ist die Anmeldung
    # das Naheliegende – sonst stünde man vor einer Fehlermeldung ohne Ausweg.
    ausgabe("Kein Zugangsschlüssel hinterlegt – Anmeldung wird geöffnet.")
    return mit_login()



def beschreibe(ausgabe=print):
    """Eine Zeile darüber, wie dieser Lauf sich anmeldet."""
    if modus() == "login":
        wo = ("eigene App-Registrierung" if eigene_registrierung()
              else "Microsofts öffentliche Anwendung")
        ausgabe(f"Anmeldemodus: Login ({wo}, Tenant {tenant()}).")
    else:
        ausgabe("Token-Modus aktiv – nutze Access Token aus Graph Explorer (kein Login).")


def main():
    """Selbstauskunft: welcher Weg gilt, was liegt vor.

    Auch das Gegenstück im Bündel (`--run auth`): dort ist es der einzige Weg,
    ohne Netz zu prüfen, dass dieses Modul und msal wirklich mitgeliefert sind.
    Ein Export würde es sonst erst beim Anwender merken.
    """
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    beschreibe()
    print(f"Client-ID: {client_id()}")
    print(f"Schlüssel: {'gefunden' if load_pasted_token() else 'keiner hinterlegt'}"
          f" ({token_datei()})")
    print(f"Cache:     {'vorhanden' if cache_datei().exists() else 'keiner'}"
          f" ({cache_datei()})")
    konto = angemeldet()
    print(f"Angemeldet: {konto if konto else 'nein'}")


if __name__ == "__main__":
    main()
