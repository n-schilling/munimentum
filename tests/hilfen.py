"""Helpers shared by the test modules – one copy, imported where needed."""

import http.client
import json


def call(port, method, path, body=None, host=None):
    """One request against a test server; (status, json or text)."""
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if host:
        headers["Host"] = host
    con.request(method, path, json.dumps(body) if body is not None else None, headers)
    r = con.getresponse()
    raw = r.read()
    con.close()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, raw.decode("utf-8", "replace")
