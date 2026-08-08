"""
Tests for shunt.cli — the `run` subcommand (one command on a host, no session needed).

Coverage:
  - registered in the subcommand table and reachable by name
  - a single quoted argument passes through VERBATIM (pipes/redirects survive)
  - several arguments are re-quoted (so `echo "a b"` stays two words)
  - the exit code of the remote command is passed through, not swallowed
  - usage error when the command is missing
  - unknown host dies instead of guessing

ssh is stubbed everywhere — no connection is attempted. SHUNT_CONF points at a
temp dir with two fake hosts.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Make the src tree importable when run without installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import shunt.cli as shunt_mod

# ── helpers ────────────────────────────────────────────────────────────────────

class TmpHosts:
    """Context manager: temp CONF holding one host in shunt.toml.

    The legacy format keeps its own tests — tests/test_shunt_config.py.
    """

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-run-")
        with open(os.path.join(self.dir, "shunt.toml"), "w") as f:
            f.write('[hosts]\nh1 = "root@10.0.0.1"\n')
        self._orig = shunt_mod.CONF
        shunt_mod.CONF = self.dir
        return self

    def __exit__(self, *_):
        shunt_mod.CONF = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)


def run_with_stubbed_ssh(argv, returncode=0):
    """Call cmd_run with ssh stubbed. Returns (command handed to ssh, exit code)."""
    seen = {}

    def fake_run(a, *args, **kwargs):
        seen["argv"] = a
        return MagicMock(returncode=returncode)

    with patch.object(shunt_mod.subprocess, "run", fake_run):
        rc = shunt_mod.cmd_run(argv)
    return seen["argv"][-1], rc


# ── registration ───────────────────────────────────────────────────────────────

class TestRegistered(unittest.TestCase):
    """A subcommand nobody can reach by name does not exist."""

    def test_run_is_callable(self):
        self.assertTrue(callable(shunt_mod.cmd_run))

    def test_run_in_usage_line(self):
        self.assertIn("run", shunt_mod.__doc__)

    def test_pre_existing_subcommands_survive(self):
        """Adding one must not drop another."""
        with open(shunt_mod.__file__) as f:
            src = f.read()
        for name in ("hosts", "run", "read", "edit", "cp", "bg", "get",
                     "log", "install", "checkout", "commit"):
            self.assertIn(f'"{name}": cmd_', src)


# ── quoting: the whole reason this is not a one-liner ──────────────────────────

class TestQuoting(unittest.TestCase):

    def test_single_argument_passes_verbatim(self):
        """`shunt run @h "ls | wc -l"` must keep its pipe, not escape it."""
        with TmpHosts():
            cmd, _ = run_with_stubbed_ssh(["@h1", "ls | wc -l"])
            self.assertEqual(cmd, "ls | wc -l")

    def test_several_arguments_are_requoted(self):
        """`shunt run @h echo "a b"` must stay two words on the far side."""
        with TmpHosts():
            cmd, _ = run_with_stubbed_ssh(["@h1", "echo", "a b"])
            self.assertEqual(cmd, "echo 'a b'")

    def test_alias_accepted_without_at_sign(self):
        with TmpHosts():
            cmd, _ = run_with_stubbed_ssh(["h1", "hostname"])
            self.assertEqual(cmd, "hostname")


# ── exit code and errors ───────────────────────────────────────────────────────

class TestExitCode(unittest.TestCase):

    def test_remote_exit_code_passes_through(self):
        """Swallowing the code would make failure look like success."""
        with TmpHosts():
            _, rc = run_with_stubbed_ssh(["@h1", "false"], returncode=1)
            self.assertEqual(rc, 1)

    def test_zero_stays_zero(self):
        with TmpHosts():
            _, rc = run_with_stubbed_ssh(["@h1", "true"], returncode=0)
            self.assertEqual(rc, 0)


class TestErrors(unittest.TestCase):

    def test_missing_command_is_usage_error(self):
        with TmpHosts(), self.assertRaises(SystemExit):
            shunt_mod.cmd_run(["@h1"])

    def test_unknown_host_dies(self):
        with TmpHosts(), self.assertRaises(SystemExit):
            shunt_mod.cmd_run(["@nope", "hostname"])

    def test_legacy_line_of_an_unknown_shape_is_refused(self):
        """A legacy line that does not name ssh must not become a host.

        Whatever such a line carries is not an ssh target — ssh-ing there would connect
        to the wrong thing quietly. The line must simply not resolve.
        """
        conf = tempfile.mkdtemp(prefix="shunt-test-legacy-")
        orig = shunt_mod.CONF
        shunt_mod.CONF = conf
        try:
            with open(os.path.join(conf, "hosts"), "w") as f:
                f.write("bad1 not-ssh 10.0.0.9:8766\n")
            with self.assertRaises(SystemExit):
                shunt_mod.cmd_run(["@bad1", "hostname"])
        finally:
            shunt_mod.CONF = orig
            shutil.rmtree(conf, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
