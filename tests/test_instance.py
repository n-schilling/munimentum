"""instance.py – where the app of a profile answers, if it runs at all."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import instance


def _server(profile="standard", ours=True):
    """A stand-in answering /api/v1/status the way the app does – or the
    way any other server on a free port would."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"token": {}, "jobs": [], "profile": {"name": profile}}).encode()
            self.send_response(200)
            if ours:
                self.send_header("X-Munimentum-Api", "v1")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


@pytest.fixture
def running():
    servers = []

    def start(**kw):
        servers.append(_server(**kw))
        return servers[-1].server_address[1]
    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


def test_the_file_says_port_process_and_profile(tmp_path):
    assert instance.write(tmp_path, 8712, "nordwind") == tmp_path / instance.FILE
    data = instance.read(tmp_path)
    assert data["port"] == 8712 and data["pid"] == os.getpid() and data["profile"] == "nordwind"
    assert data["started"].endswith("+00:00")
    assert not (tmp_path / (instance.FILE + ".tmp")).exists()


def test_only_the_writing_process_takes_the_file_away(tmp_path):
    instance.write(tmp_path, 8700, "standard")
    data = instance.read(tmp_path)
    (tmp_path / instance.FILE).write_text(json.dumps({**data, "pid": data["pid"] + 1}))
    instance.remove(tmp_path)                 # a second start that found the first one
    assert instance.read(tmp_path) is not None
    instance.write(tmp_path, 8700, "standard")
    instance.remove(tmp_path)
    assert instance.read(tmp_path) is None
    instance.remove(tmp_path)                 # nothing there: nothing happens


def test_a_broken_file_is_no_instance(tmp_path):
    (tmp_path / instance.FILE).write_text("{half")
    assert instance.read(tmp_path) is None
    (tmp_path / instance.FILE).write_text(json.dumps({"port": "8700"}))
    assert instance.read(tmp_path) is None
    assert instance.find(tmp_path) == (None, "not_running")
    assert instance.find(None) == (None, "not_running")


def test_the_file_is_a_hint_the_probe_is_the_proof(tmp_path, running):
    port = running(profile="standard")
    instance.write(tmp_path, port, "standard")
    assert instance.find(tmp_path, timeout=2) == (f"http://127.0.0.1:{port}/", "running")
    # Left behind by a crash: nothing answers on the port any more.
    frei = _server()
    frei_port = frei.server_address[1]
    frei.shutdown()
    frei.server_close()
    instance.write(tmp_path, frei_port, "standard")
    assert instance.find(tmp_path, timeout=0.5) == (None, "not_running")


def test_another_profile_or_another_server_on_the_port_is_no_link(tmp_path, running):
    instance.write(tmp_path, running(profile="nordwind"), "standard")
    assert instance.find(tmp_path, timeout=2) == (None, "other_profile")
    instance.write(tmp_path, running(ours=False), "standard")
    assert instance.find(tmp_path, timeout=2) == (None, "not_running")


def test_answers_is_the_old_running_check(running):
    port = running(profile="nordwind")
    assert instance.answers(port, timeout=2) is True
    assert instance.answers(port, timeout=2, profile="nordwind") is True
    assert instance.answers(port, timeout=2, profile="standard") is False
