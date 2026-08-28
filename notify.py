#!/usr/bin/env python3
"""
notify.py – native system notifications, fire-and-forget.

The one caller is the JobRunner in app.py: it tells the user that a run
finished or failed while the browser tab may well be closed – which is the
normal state when the scheduler does the work. Everything stays on the
machine; nothing is sent anywhere.

Only macOS is implemented, deliberately in two tiers:

  * Bundled app (Munimentum.app has a bundle identifier): the notification
    center API (UNUserNotificationCenter via PyObjC). Proper attribution,
    app icon, and the system asks the user for permission once.
  * Run from source: `osascript` – no dependencies, generic icon. Good
    enough for development; the DMG never takes this path.

Windows and Linux are conscious no-ops for now: the plumbing (setting,
translations, JobRunner hook) is platform-neutral, only this module needs
a backend per OS.

send() never raises – a missed notification must never break a run.
"""

import subprocess
import sys

# Lazily resolved once: a UNUserNotificationCenter, or None after a failed
# attempt (not bundled, PyObjC missing) – then osascript takes over.
_center = "unresolved"


def _mac_center():
    global _center
    if _center == "unresolved":
        _center = None
        try:
            import Foundation
            import UserNotifications as UN
            if Foundation.NSBundle.mainBundle().bundleIdentifier():
                center = UN.UNUserNotificationCenter.currentNotificationCenter()
                # One-time permission dialog; if the user declines, adds are
                # silently dropped by the system, which is the wish expressed.
                center.requestAuthorizationWithOptions_completionHandler_(
                    UN.UNAuthorizationOptionAlert | UN.UNAuthorizationOptionSound,
                    lambda granted, error: None)
                _center = center
        except Exception:
            _center = None
    return _center


def _mac_native(title, body):
    """Post via the notification center; True when handed to the system."""
    center = _mac_center()
    if center is None:
        return False
    try:
        import Foundation
        import UserNotifications as UN
        content = UN.UNMutableNotificationContent.alloc().init()
        content.setTitle_(title)
        content.setBody_(body)
        request = UN.UNNotificationRequest.requestWithIdentifier_content_trigger_(
            Foundation.NSUUID.UUID().UUIDString(), content, None)
        center.addNotificationRequest_withCompletionHandler_(request, None)
        return True
    except Exception:
        return False


def _mac_osascript(title, body):
    # Arguments travel as argv, not interpolated into the script – titles
    # and bodies can then contain quotes without becoming AppleScript.
    script = ("on run argv\n"
              "display notification (item 2 of argv) with title (item 1 of argv)\n"
              "end run")
    subprocess.run(["osascript", "-e", script, title, body],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=10, check=False)


def send(title, body):
    """Show one system notification; silently does nothing where unsupported."""
    try:
        if sys.platform != "darwin":
            return          # Windows/Linux: see module docstring
        title, body = str(title), str(body)
        if not _mac_native(title, body):
            _mac_osascript(title, body)
    except Exception:
        pass
