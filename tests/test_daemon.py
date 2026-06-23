"""
Tests for shunt.daemon — HMAC token verification path.
Binds to 127.0.0.1 on an ephemeral port; no real commands are executed
(we verify rejection/acceptance at the protocol level, not shell execution).
"""

import json
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _start_daemon(token: str, host: str = "127.0.0.1", port: int = 0):
    """
    Start a daemon.Handler inside a ThreadingTCPServer on an ephemeral port.
    Returns (server, actual_port, stop_event).
    The server runs in a daemon thread so it is cleaned up on test exit.
    """
    # Patch the module-level TOKEN before importing Handler.
    import shunt.daemon as dm

    # Override module globals for this test server instance.
    dm.TOKEN = token

    class _Server(dm.Server):
        allow_reuse_address = True
        daemon_threads = True

    srv = _Server((host, port), dm.Handler)
    actual_port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, actual_port


def _send_request(
    port: int, payload: dict, host: str = "127.0.0.1", timeout: float = 3.0
) -> bytes:
    """Open a TCP connection, send the JSON line, read until closed, return raw bytes."""
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall((json.dumps(payload) + "\n").encode())
        s.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            data = s.recv(4096)
            if not data:
                break
            chunks.append(data)
    return b"".join(chunks)


class TestDaemonTokenRejection(unittest.TestCase):
    TOKEN = "correct_token_abc"

    @classmethod
    def setUpClass(cls):
        cls.srv, cls.port = _start_daemon(cls.TOKEN)[:2]
        # Give the server a moment to bind.
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def _request(self, token, mark="__END__"):
        return _send_request(
            self.port,
            {"token": token, "cmd": "echo hello", "sid": "test", "mark": mark},
        )

    def test_valid_token_accepted(self):
        raw = self._request(self.TOKEN, mark="__VALID_MARK__")
        # With a valid token the daemon runs the command — response must NOT contain
        # the invalid-token error message.
        self.assertNotIn(b"invalid token", raw)

    def test_invalid_token_rejected(self):
        raw = self._request("WRONG_TOKEN")
        self.assertIn(b"invalid token", raw)

    def test_empty_token_rejected(self):
        raw = self._request("")
        self.assertIn(b"invalid token", raw)

    def test_none_token_rejected(self):
        raw = _send_request(
            self.port, {"token": None, "cmd": "echo hi", "sid": "t", "mark": "__M__"}
        )
        self.assertIn(b"invalid token", raw)

    def test_almost_correct_token_rejected(self):
        # One character off — must still be rejected (constant-time compare).
        bad = self.TOKEN[:-1] + ("X" if self.TOKEN[-1] != "X" else "Y")
        raw = self._request(bad)
        self.assertIn(b"invalid token", raw)

    def test_invalid_token_response_includes_trailer(self):
        # Even on rejection the daemon sends the end-marker trailer so the
        # inline client can exit cleanly.
        mark = "__REJECT_MARK__"
        raw = _send_request(
            self.port, {"token": "bad", "cmd": "echo hi", "sid": "s", "mark": mark}
        )
        self.assertIn(mark.encode(), raw)

    def test_bad_json_does_not_crash_server(self):
        # Sending garbage must not kill the server; subsequent valid request works.
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as s:
            s.sendall(b"NOT JSON AT ALL\n")
            # drain without crashing
            try:
                s.recv(1024)
            except Exception:
                pass
        # Server must still be alive.
        raw = self._request(self.TOKEN, mark="__AFTER_GARBAGE__")
        self.assertNotIn(b"invalid token", raw)


class TestDaemonTokenComparisonLogic(unittest.TestCase):
    """Unit-test the HMAC comparison directly without a network round-trip."""

    def _compare(self, given: str, expected: str) -> bool:
        import hmac

        return hmac.compare_digest(str(given), expected)

    def test_identical_tokens_match(self):
        self.assertTrue(self._compare("abc123", "abc123"))

    def test_different_tokens_do_not_match(self):
        self.assertFalse(self._compare("abc123", "abc124"))

    def test_empty_vs_nonempty_does_not_match(self):
        self.assertFalse(self._compare("", "secret"))

    def test_none_stringified_vs_secret(self):
        # daemon casts tok to str: str(None) == "None" != real token
        self.assertFalse(self._compare(str(None), "secret"))

    def test_both_empty_match(self):
        # Two empty strings compare equal — but daemon guards against empty TOKEN separately.
        self.assertTrue(self._compare("", ""))


class TestDaemonEmptyServerToken(unittest.TestCase):
    """When SHUNT_TOKEN is empty the daemon rejects all requests."""

    @classmethod
    def setUpClass(cls):
        cls.srv, cls.port = _start_daemon("")[:2]
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_empty_server_token_rejects_any_request(self):
        raw = _send_request(
            self.port,
            {"token": "anything", "cmd": "echo hi", "sid": "s", "mark": "__M__"},
        )
        self.assertIn(b"invalid token", raw)

    def test_empty_server_token_rejects_empty_client_token(self):
        raw = _send_request(
            self.port, {"token": "", "cmd": "echo hi", "sid": "s", "mark": "__M__"}
        )
        self.assertIn(b"invalid token", raw)


if __name__ == "__main__":
    unittest.main()
