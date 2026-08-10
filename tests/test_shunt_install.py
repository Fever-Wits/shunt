"""
Tests for shunt.cli — the `install` subcommand's LAST word: the connection test.

`install` ends by reaching the machine and printing "  ✓ connected to <hostname>". That
tick is the line a human remembers, and the exit code is the line a script reads — and
the code used to be thrown away, so install returned 0 whatever came back. Two ways that
lies: `shunt install … && <next step>` carries on against a machine nothing has reached,
and a run that printed no tick still looked, to everything downstream, exactly like one
that did.

Coverage:
  - a failing connection test fails the install, carrying ssh's OWN code out
  - the reason names the host and the code, on stderr
  - a clean run still returns 0 (the fix must not turn a working install into a failure)
  - the probe is actually made — an unwired check proves nothing
  - the earlier python3 probe still decides on its own, before any of this

ssh is stubbed everywhere — no connection is attempted — and CONF points at a temp dir,
so no real host file is touched.
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

# Make the src tree importable when run without installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import shunt.cli as shunt_mod

# ── helpers ────────────────────────────────────────────────────────────────────


def install_with_stubbed_ssh(python3_rc=0, connect_rc=0, argv=("root@203.0.113.1", "--alias", "h1")):
    """Run cmd_install with both of its ssh calls answered by us.

    Returns (exit code, stderr, the remote commands ssh was handed). The exit code is the
    returned one, or the code install died with — the two are the same fact to a caller,
    and this is the fact under test.
    """
    seen = []

    def fake_run(argv_, *a, **kw):
        remote = argv_[-1]
        seen.append(remote)
        if "python3 --version" in remote:
            return subprocess.CompletedProcess(argv_, python3_rc, b"Python 3.11.0", b"")
        return subprocess.CompletedProcess(argv_, connect_rc)

    conf = tempfile.mkdtemp(prefix="shunt-test-install-")
    orig_conf = shunt_mod.CONF
    shunt_mod.CONF = conf
    err = io.StringIO()
    try:
        with patch.object(shunt_mod.subprocess, "run", fake_run):
            with patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", err):
                try:
                    code = shunt_mod.cmd_install(list(argv))
                except SystemExit as e:
                    code = e.code
    finally:
        shunt_mod.CONF = orig_conf
        shutil.rmtree(conf, ignore_errors=True)
    return code, err.getvalue(), seen


# ── the connection test decides ────────────────────────────────────────────────


class TestTheConnectionTestIsTheExitCode(unittest.TestCase):
    def test_a_failed_connection_fails_the_install(self):
        """255 is ssh's own "could not connect". It travels out whole, the way `run` and
        `checkout` already pass a remote code through instead of flattening it."""
        code, _, _ = install_with_stubbed_ssh(connect_rc=255)
        self.assertEqual(code, 255)

    def test_another_code_travels_out_too(self):
        """So the test above cannot pass on a hard-coded 255."""
        code, _, _ = install_with_stubbed_ssh(connect_rc=1)
        self.assertEqual(code, 1)

    def test_the_reason_names_the_host_and_the_code(self):
        """An install that fails at the last step must say which machine and how badly —
        the tick that would have named the host is precisely the line that did not print."""
        _, err, _ = install_with_stubbed_ssh(connect_rc=255)
        self.assertIn("203.0.113.1", err)
        self.assertIn("255", err)
        self.assertIn("FAILED", err)

    def test_a_clean_install_still_returns_zero(self):
        """The other direction: the fix may not turn a working install into a failure."""
        code, err, _ = install_with_stubbed_ssh()
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_the_probe_is_actually_made(self):
        """A check on a call nobody makes proves nothing at all."""
        _, _, seen = install_with_stubbed_ssh()
        self.assertTrue(any("hostname" in s for s in seen), seen)


class TestTheEarlierProbeStillDecides(unittest.TestCase):
    """The connection test is the LAST gate, not the only one — step 1 must keep its own."""

    def test_a_missing_python3_still_dies_before_anything_else(self):
        code, err, seen = install_with_stubbed_ssh(python3_rc=1)
        self.assertEqual(code, 2)  # die()'s own code: this is not ssh reporting
        self.assertIn("python3 missing", err)
        self.assertEqual(len(seen), 1)  # nothing was tried after it


if __name__ == "__main__":
    unittest.main()
