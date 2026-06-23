"""
Tests for shunt.pretool — pure logic only; no real SSH / no real sockets.
Drives: resolve_host, ssh_command, daemon_command, REWRITE_MARKER guard,
shunt-CLI passthrough guard, and @-switch parsing.
"""

import json
import os
import sys
import tempfile
import unittest

# Make the src tree importable when run without installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class _PretoolBase(unittest.TestCase):
    """Create a tmp SHUNT_CONF dir with a minimal hosts file, then import the module."""

    HOSTS_CONTENT = ""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        os.environ["SHUNT_CONF"] = self._tmpdir
        with open(os.path.join(self._tmpdir, "hosts"), "w") as f:
            f.write(self.HOSTS_CONTENT)
        # Force reimport so CONF is re-evaluated.
        import importlib
        import shunt.pretool as _pt

        importlib.reload(_pt)
        self.pt = _pt

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)
        # Clean the env so other tests start fresh.
        os.environ.pop("SHUNT_CONF", None)


class TestResolveHost(_PretoolBase):
    HOSTS_CONTENT = (
        "# comment line\n"
        "host-a daemon 203.0.113.84:8766 key=~/.ssh/id_ed25519\n"
        "raspi  ssh   pi@203.0.113.99\n"
    )

    def test_known_daemon_host(self):
        h = self.pt.resolve_host("host-a")
        self.assertIsNotNone(h)
        self.assertEqual(h["alias"], "host-a")
        self.assertEqual(h["transport"], "daemon")
        self.assertEqual(h["target"], "203.0.113.84:8766")
        self.assertIn("key=~/.ssh/id_ed25519", h["opts"])

    def test_known_ssh_host(self):
        h = self.pt.resolve_host("raspi")
        self.assertIsNotNone(h)
        self.assertEqual(h["transport"], "ssh")
        self.assertEqual(h["target"], "pi@203.0.113.99")
        self.assertEqual(h["opts"], [])

    def test_unknown_host_returns_none(self):
        self.assertIsNone(self.pt.resolve_host("nonexistent"))

    def test_comment_line_skipped(self):
        # "# comment line" must not be interpreted as an alias
        self.assertIsNone(self.pt.resolve_host("#"))

    def test_empty_alias_not_matched(self):
        self.assertIsNone(self.pt.resolve_host(""))

    def test_partial_prefix_not_matched(self):
        # "host" must not match "host-a"
        self.assertIsNone(self.pt.resolve_host("host"))


class TestRewriteMarkerGuard(_PretoolBase):
    """A command that already starts with the rewrite marker must not be re-processed."""

    HOSTS_CONTENT = "remote ssh user@host\n"

    def _make_hook_input(self, command, session_id="sess1"):
        return {
            "tool_name": "Bash",
            "session_id": session_id,
            "tool_input": {"command": command},
        }

    def test_marker_at_start_causes_passthrough(self):
        # Write a target so we would normally redirect.
        tf = os.path.join(self._tmpdir, "target.sess1")
        with open(tf, "w") as f:
            f.write("remote")

        marker = self.pt.REWRITE_MARKER
        cmd = marker + "ssh -o ... 'user@host' 'some cmd'"

        # main() calls sys.exit(); catch that.
        import io

        with self.assertRaises(SystemExit) as cm:
            sys.stdin = io.StringIO(json.dumps(self._make_hook_input(cmd)))
            self.pt.main()
        # Exit code 0 means passthrough (no rewrite).
        self.assertEqual(cm.exception.code, 0)

    def test_plain_command_starts_with_hash_but_not_marker_is_not_guarded(self):
        # "# a bash comment" is NOT the rewrite marker.
        marker = self.pt.REWRITE_MARKER
        self.assertFalse("# a bash comment\n".lstrip().startswith("#shunt-rewritten"))
        self.assertTrue(marker.startswith("#shunt-rewritten"))


class TestShuntCliPassthrough(_PretoolBase):
    """shunt / shunt <sub> must never be redirected."""

    HOSTS_CONTENT = "remote ssh user@host\n"

    def _run_main_with(self, command, session_id="sess1"):
        tf = os.path.join(self._tmpdir, "target." + session_id)
        with open(tf, "w") as f:
            f.write("remote")  # remote mode active
        import io

        payload = {
            "tool_name": "Bash",
            "session_id": session_id,
            "tool_input": {"command": command},
        }
        sys.stdin = io.StringIO(json.dumps(payload))
        with self.assertRaises(SystemExit) as cm:
            self.pt.main()
        return cm.exception.code

    def test_bare_shunt_passes_through(self):
        code = self._run_main_with("shunt")
        self.assertEqual(code, 0)

    def test_shunt_status_passes_through(self):
        code = self._run_main_with("shunt status")
        self.assertEqual(code, 0)

    def test_shunt_switch_passes_through(self):
        code = self._run_main_with("shunt @host-a")
        self.assertEqual(code, 0)


class TestSshCommandShape(_PretoolBase):
    HOSTS_CONTENT = "myhost ssh user@198.51.100.1\n"

    def test_starts_with_rewrite_marker(self):
        h = self.pt.resolve_host("myhost")
        result = self.pt.ssh_command(h, "ls -la", "sid123")
        self.assertTrue(result.startswith(self.pt.REWRITE_MARKER))

    def test_contains_ssh_binary(self):
        h = self.pt.resolve_host("myhost")
        result = self.pt.ssh_command(h, "ls -la", "sid123")
        # After the marker, the rest must start with "ssh "
        body = result[len(self.pt.REWRITE_MARKER) :]
        self.assertTrue(body.startswith("ssh "), repr(body[:40]))

    def test_contains_target(self):
        h = self.pt.resolve_host("myhost")
        result = self.pt.ssh_command(h, "ls", "sid123")
        self.assertIn("user@198.51.100.1", result)

    def test_per_session_socket(self):
        h = self.pt.resolve_host("myhost")
        r1 = self.pt.ssh_command(h, "ls", "sess-A")
        r2 = self.pt.ssh_command(h, "ls", "sess-B")
        # ControlPath contains sid — must differ between sessions.
        self.assertIn("sess-A", r1)
        self.assertIn("sess-B", r2)
        self.assertNotIn("sess-B", r1)

    def test_key_option_included_when_present(self):
        hosts = "keyhost ssh user@198.51.100.2 key=~/.ssh/id_rsa\n"
        with open(os.path.join(self._tmpdir, "hosts"), "w") as f:
            f.write(hosts)
        import importlib
        import shunt.pretool as _pt

        importlib.reload(_pt)
        h = _pt.resolve_host("keyhost")
        result = _pt.ssh_command(h, "ls", "s1")
        self.assertIn("-i", result)


class TestDaemonCommandShape(_PretoolBase):
    HOSTS_CONTENT = "dbox daemon 203.0.113.84:9000\n"

    def _write_token(self, token):
        with open(os.path.join(self._tmpdir, "token.dbox"), "w") as f:
            f.write(token)

    def test_starts_with_rewrite_marker(self):
        self._write_token("secret123")
        h = self.pt.resolve_host("dbox")
        result = self.pt.daemon_command(h, "echo hi", "sid1")
        self.assertTrue(result.startswith(self.pt.REWRITE_MARKER))

    def test_contains_python3(self):
        self._write_token("secret123")
        h = self.pt.resolve_host("dbox")
        result = self.pt.daemon_command(h, "echo hi", "sid1")
        self.assertIn("python3", result)

    def test_contains_host_and_port(self):
        self._write_token("tok")
        h = self.pt.resolve_host("dbox")
        result = self.pt.daemon_command(h, "ls", "sid2")
        self.assertIn("203.0.113.84", result)
        self.assertIn("9000", result)

    def test_default_port_when_no_port_in_target(self):
        hosts = "noport daemon 203.0.113.5\n"
        with open(os.path.join(self._tmpdir, "hosts"), "w") as f:
            f.write(hosts)
        with open(os.path.join(self._tmpdir, "token.noport"), "w") as f:
            f.write("t")
        import importlib
        import shunt.pretool as _pt

        importlib.reload(_pt)
        h = _pt.resolve_host("noport")
        result = _pt.daemon_command(h, "pwd", "s")
        self.assertIn("8766", result)

    def test_per_connection_marker_is_unique(self):
        self._write_token("tok")
        h = self.pt.resolve_host("dbox")
        r1 = self.pt.daemon_command(h, "ls", "s")
        r2 = self.pt.daemon_command(h, "ls", "s")
        # The random marker embedded in each call must differ.
        import re

        marks = re.findall(r"__SHUNT_END_[0-9a-f]+__", r1 + r2)
        self.assertEqual(len(marks), 2)
        self.assertNotEqual(marks[0], marks[1])

    def test_shunt_tok_env_assignment_present(self):
        self._write_token("mysecrettoken")
        h = self.pt.resolve_host("dbox")
        result = self.pt.daemon_command(h, "ls", "s")
        self.assertIn("SHUNT_TOK=", result)

    def test_fallback_to_shared_token(self):
        # No per-alias token — fall back to the "token" file.
        with open(os.path.join(self._tmpdir, "token"), "w") as f:
            f.write("sharedtok")
        h = self.pt.resolve_host("dbox")
        result = self.pt.daemon_command(h, "ls", "s")
        self.assertIn("sharedtok", result)


if __name__ == "__main__":
    unittest.main()
