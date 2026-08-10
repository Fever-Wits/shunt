"""
Tests for shunt.cli — `edit`, and the payload it hands to edit_helper.

Why this file exists: `--dry-run` was computed only on the OLD/NEW path, so
`shunt edit @h file --stdin --dry-run` sent a payload with no `dry_run` in it and the
helper WROTE — on someone else's machine, with a flag on the command line asking it not
to. The docs offer both flags under one command, so the combination is the natural one
to reach for.

The second defect in the same command was the EXIT CODE: the helper answers in JSON and
always exits 0 — `not_found` and `ambiguous` included — and cmd_edit passed ssh's code
straight back. So `shunt edit … && deploy` deployed a file that had not been edited.

Coverage:
  - --stdin + --dry-run does not write (the real helper runs against a temp file)
  - the control: the same payload WITHOUT --dry-run does write (so the test above is
    testing the flag, not the helper's mood)
  - the flag reaches the payload on both paths — stdin and OLD/NEW
  - a payload that already asks for a dry run is never turned into a write
  - the file comes from the argument, not from the JSON
  - the exit code follows the helper's status, not the transport's: 0 only for `ok`
  - the helper's answer still reaches the caller's stdout, so a non-zero code can be read

ssh is stubbed; the helper is then run locally, which is exactly what it does remotely.
"""

import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Make the src tree importable when run without installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import shunt.cli as shunt_mod

# The helper as a file: it is deployed to a remote `python3 -`, so it is driven by path.
HELPER = os.path.join(os.path.dirname(shunt_mod.__file__), "edit_helper.py")


class TmpHosts:
    """Context manager: temp CONF holding one host."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-edit-")
        with open(os.path.join(self.dir, "shunt.toml"), "w") as f:
            f.write('[hosts]\nh1 = "root@203.0.113.1"\n')
        self._orig = shunt_mod.CONF
        shunt_mod.CONF = self.dir
        return self

    def __exit__(self, *_):
        shunt_mod.CONF = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)


def payload_of(argv, stdin_payload=None):
    """Run cmd_edit with ssh stubbed; return the payload it meant to send."""
    seen = {}

    def fake_run(a, *args, **kwargs):
        seen["argv"] = a
        # what a real ssh hands back: the helper's JSON on stdout, as bytes
        return MagicMock(returncode=0, stdout=b'{"status": "ok"}', stderr=b"")

    stdin = io.StringIO(json.dumps(stdin_payload) if stdin_payload else "")
    with (
        patch.object(shunt_mod.subprocess, "run", fake_run),
        patch.object(sys, "stdin", stdin),
        patch.object(sys, "stdout", io.StringIO()),
    ):
        shunt_mod.cmd_edit(argv)
    return json.loads(base64.b64decode(seen["argv"][-1]))


def apply_locally(payload):
    """Hand the payload to the real edit_helper — the same run as on the far side."""
    b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    r = subprocess.run([sys.executable, HELPER, b64], capture_output=True)
    return json.loads(r.stdout.decode())


def edit_rc(argv, ssh_returncode=0):
    """cmd_edit's exit code, with ssh replaced by a LOCAL run of the real edit_helper.

    The helper decides the status; the test must not invent its answers — that is the
    whole point of an exit code that follows it. Returns (rc, what_was_printed).
    """
    real_run = subprocess.run  # bound before the patch — the stub must not call itself

    def fake_run(a, *args, **kwargs):
        r = real_run([sys.executable, HELPER, a[-1]], capture_output=True)
        return MagicMock(returncode=ssh_returncode, stdout=r.stdout, stderr=r.stderr)

    printed = io.StringIO()
    with patch.object(shunt_mod.subprocess, "run", fake_run), patch.object(sys, "stdout", printed):
        rc = shunt_mod.cmd_edit(argv)
    return rc, printed.getvalue()


class TestStdinDryRun(unittest.TestCase):
    """The defect, and its control."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-edit-target-")
        self.target = os.path.join(self.dir, "config.txt")
        with open(self.target, "w") as f:
            f.write("listen 80;\n")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _stdin_payload(self):
        return {"old": "listen 80;", "new": "listen 8080;", "expected": 1}

    def test_stdin_with_dry_run_does_not_write(self):
        with TmpHosts():
            payload = payload_of(["@h1", self.target, "--stdin", "--dry-run"], self._stdin_payload())
        result = apply_locally(payload)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result.get("dry_run"))
        with open(self.target) as f:
            self.assertEqual(f.read(), "listen 80;\n")  # untouched

    def test_the_same_payload_without_the_flag_does_write(self):
        """The control: without it the file changes, so the test above tests the flag."""
        with TmpHosts():
            payload = payload_of(["@h1", self.target, "--stdin"], self._stdin_payload())
        result = apply_locally(payload)
        self.assertEqual(result["status"], "ok")
        with open(self.target) as f:
            self.assertEqual(f.read(), "listen 8080;\n")


class TestExitCodeFollowsTheEdit(unittest.TestCase):
    """`shunt edit … && deploy` must not deploy a file that was never edited."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-edit-rc-")
        self.target = os.path.join(self.dir, "config.txt")
        with open(self.target, "w") as f:
            f.write("listen 80;\n")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_successful_edit_returns_zero(self):
        with TmpHosts():
            rc, _ = edit_rc(["@h1", self.target, "listen 80;", "listen 8080;"])
        self.assertEqual(rc, 0)

    def test_an_old_that_is_not_there_returns_non_zero(self):
        """The defect: nothing was changed, and the shell was told it went fine."""
        with TmpHosts():
            rc, out = edit_rc(["@h1", self.target, "listen 443;", "listen 8443;"])
        self.assertNotEqual(rc, 0)
        self.assertIn("not_found", out)  # and the reason is readable

    def test_an_ambiguous_edit_returns_non_zero(self):
        with open(self.target, "w") as f:
            f.write("listen 80;\nlisten 80;\n")
        with TmpHosts():
            rc, out = edit_rc(["@h1", self.target, "listen 80;", "listen 8080;"])
        self.assertNotEqual(rc, 0)
        self.assertIn("ambiguous", out)

    def test_a_dry_run_that_found_its_match_returns_zero(self):
        """A preview that worked is a success — it is not a refused edit."""
        with TmpHosts():
            rc, _ = edit_rc(["@h1", self.target, "listen 80;", "listen 8080;", "--dry-run"])
        self.assertEqual(rc, 0)

    def test_a_transport_failure_keeps_its_own_code(self):
        """ssh could not reach the machine: that code says more than a generic 1."""
        with TmpHosts():
            rc, _ = edit_rc(["@h1", self.target, "listen 80;", "listen 8080;"], ssh_returncode=255)
        self.assertEqual(rc, 255)


class TestFlagReachesThePayload(unittest.TestCase):
    def test_on_the_stdin_path(self):
        with TmpHosts():
            payload = payload_of(["@h1", "/etc/x", "--stdin", "--dry-run"], {"old": "a", "new": "b"})
        self.assertIs(payload["dry_run"], True)

    def test_on_the_old_new_path(self):
        with TmpHosts():
            payload = payload_of(["@h1", "/etc/x", "a", "b", "--dry-run"])
        self.assertIs(payload["dry_run"], True)

    def test_without_the_flag_the_old_new_path_writes(self):
        with TmpHosts():
            payload = payload_of(["@h1", "/etc/x", "a", "b"])
        self.assertIs(payload["dry_run"], False)

    def test_a_payload_that_asks_for_a_preview_is_never_turned_into_a_write(self):
        """The flag may only ADD safety; its absence does not cancel the JSON's own."""
        with TmpHosts():
            payload = payload_of(["@h1", "/etc/x", "--stdin"], {"old": "a", "new": "b", "dry_run": True})
        self.assertIs(payload["dry_run"], True)


class TestStdinPayload(unittest.TestCase):
    def test_the_file_comes_from_the_argument(self):
        """The path is addressed on the command line — the JSON does not get to override."""
        with TmpHosts():
            payload = payload_of(["@h1", "/etc/right", "--stdin"], {"file": "/etc/wrong", "old": "a", "new": "b"})
        self.assertEqual(payload["file"], "/etc/right")

    def test_the_rest_of_the_payload_survives(self):
        with TmpHosts():
            payload = payload_of(
                ["@h1", "/etc/x", "--stdin", "--dry-run"],
                {"old": "a", "new": "b", "expected": 3, "base_sha": "deadbeef"},
            )
        self.assertEqual(payload["expected"], 3)
        self.assertEqual(payload["base_sha"], "deadbeef")


if __name__ == "__main__":
    unittest.main()
