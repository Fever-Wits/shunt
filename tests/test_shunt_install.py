"""
Tests for shunt.cli - the `install` subcommand's LAST word: the connection test.

`install` ends by reaching the machine and printing "  OK connected to <hostname>". That
tick is the line a human remembers, and the exit code is the line a script reads - and
the code used to be thrown away, so install returned 0 whatever came back. Two ways that
lies: `shunt install ... && <next step>` carries on against a machine nothing has reached,
and a run that printed no tick still looked, to everything downstream, exactly like one
that did.

Coverage:
  - a failing connection test fails the install, carrying ssh's OWN code out
  - the reason names the host and the code, on stderr
  - a clean run still returns 0 (the fix must not turn a working install into a failure)
  - the probe is actually made - an unwired check proves nothing
  - the earlier python3 probe still decides on its own, before any of this

ssh is stubbed everywhere - no connection is attempted - and CONF points at a temp dir,
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
from shunt import edit_helper

# -- helpers --------------------------------------------------------------------


def install_with_stubbed_ssh(
    python3_rc=0, connect_rc=0, argv=("root@203.0.113.1", "--alias", "h1"), python3_version="3.13"
):
    """Run cmd_install with both of its ssh calls answered by us.

    Returns (exit code, stderr, the remote commands ssh was handed, stdout). The exit code
    is the returned one, or the code install died with - the two are the same fact to a
    caller, and this is the fact under test.

    The version probe is recognised by `sys.version_info`, which is what install asks the
    far python for. Matching on the literal command string tied this stub to the exact
    wording of a line that then changed; the thing being ASKED does not change.

    `python3_version` is what that host answers. It is a parameter because the floor
    (edit_helper.MIN_PYTHON) is the point of several tests below, and a host under it must
    be describable without owning one.

    Nothing is patched out around it: this package installs through its entry point, so
    there is no symlink step here to keep away from the user's PATH.
    """
    seen = []

    def fake_run(argv_, *a, **kw):
        remote = argv_[-1]
        seen.append(remote)
        if "sys.version_info" in remote:
            return subprocess.CompletedProcess(argv_, python3_rc, python3_version.encode(), b"")
        return subprocess.CompletedProcess(argv_, connect_rc)

    conf = tempfile.mkdtemp(prefix="shunt-test-install-")
    orig_conf = shunt_mod.CONF
    shunt_mod.CONF = conf
    err, out = io.StringIO(), io.StringIO()
    try:
        with patch.object(shunt_mod.subprocess, "run", fake_run):
            with patch("sys.stdout", out), patch("sys.stderr", err):
                try:
                    code = shunt_mod.cmd_install(list(argv))
                except SystemExit as e:
                    code = e.code
    finally:
        shunt_mod.CONF = orig_conf
        shutil.rmtree(conf, ignore_errors=True)
    return code, err.getvalue(), seen, out.getvalue()


# -- the connection test decides ------------------------------------------------


class TestTheConnectionTestIsTheExitCode(unittest.TestCase):
    def test_a_failed_connection_fails_the_install(self):
        """255 is ssh's own "could not connect". It travels out whole, the way `run` and
        `checkout` already pass a remote code through instead of flattening it."""
        code, _, _, _ = install_with_stubbed_ssh(connect_rc=255)
        self.assertEqual(code, 255)

    def test_another_code_travels_out_too(self):
        """So the test above cannot pass on a hard-coded 255."""
        code, _, _, _ = install_with_stubbed_ssh(connect_rc=1)
        self.assertEqual(code, 1)

    def test_the_reason_names_the_host_and_the_code(self):
        """An install that fails at the last step must say which machine and how badly -
        the tick that would have named the host is precisely the line that did not print."""
        _, err, _, _ = install_with_stubbed_ssh(connect_rc=255)
        self.assertIn("203.0.113.1", err)
        self.assertIn("255", err)
        self.assertIn("FAILED", err)

    def test_a_clean_install_still_returns_zero(self):
        """The other direction: the fix may not turn a working install into a failure."""
        code, err, _, _ = install_with_stubbed_ssh()
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_the_probe_is_actually_made(self):
        """A check on a call nobody makes proves nothing at all."""
        _, _, seen, _ = install_with_stubbed_ssh()
        self.assertTrue(any("hostname" in s for s in seen), seen)


class TestTheEarlierProbeStillDecides(unittest.TestCase):
    """The connection test is the LAST gate, not the only one - step 1 must keep its own.

    But only for the codes that mean the probe never got an answer. 127 is the far SHELL
    answering, which is a different fact - see below."""

    def test_a_probe_that_fails_transport_dies_before_anything_else(self):
        code, err, seen, _ = install_with_stubbed_ssh(python3_rc=1)
        self.assertEqual(code, 2)  # die()'s own code: this is not ssh reporting
        self.assertEqual(len(seen), 1)  # nothing was tried after it

    def test_the_refusal_carries_a_reason_even_when_ssh_is_silent(self):
        """A killed or timed-out ssh says nothing at all, and a refusal ending in an empty
        string reads as though the tool lost its words."""
        _, err, _, _ = install_with_stubbed_ssh(python3_rc=1)
        self.assertIn("no reason given", err)


class TestAHostWithNoPython3IsStillAHost(unittest.TestCase):
    """127 is the remote SHELL saying "command not found": the machine was REACHED and
    simply has no python3. That is the same KIND of fact as a python below the floor, so it
    gets the same answer - registered, with the loss said.

    It did not: the version probe and the connection probe share one ssh call, so `die()`
    took the registration with it, while the comment beside it claimed the opposite."""

    def test_the_host_is_registered(self):
        code, _, _, out = install_with_stubbed_ssh(python3_rc=127)
        self.assertEqual(code, 0)
        self.assertIn("shunt.toml", out)

    def test_the_loss_is_named(self):
        _, _, _, out = install_with_stubbed_ssh(python3_rc=127)
        self.assertIn("NO python3", out)
        self.assertIn("UNAVAILABLE", out)

    def test_what_still_works_is_named_too(self):
        """A refusal without its remainder reads as "this host is useless"."""
        _, _, _, out = install_with_stubbed_ssh(python3_rc=127)
        for hand in ("run", "cp", "bg", "get"):
            self.assertIn(hand, out)

    def test_the_connection_test_still_runs_after_it(self):
        _, _, seen, _ = install_with_stubbed_ssh(python3_rc=127)
        self.assertEqual(len(seen), 2)


# -- which python3, and what that costs -----------------------------------------


class TestTheVersionIsAskedAndSaid(unittest.TestCase):
    """The helpers run on the far machine's python3, not ours: measured on real hosts
    rather than assumed, that span is 3.7 to 3.13 - five minor versions, none of them
    chosen by the tool. Registration is the moment somebody is thinking about this
    machine - a fact given then is context; the same fact mid-task is an anomaly."""

    def test_the_probe_asks_python_for_its_own_version(self):
        """Asked as a formatted pair, not parsed out of a `--version` banner whose wording
        belongs to someone else."""
        _, _, seen, _ = install_with_stubbed_ssh()
        self.assertIn("sys.version_info", seen[0])

    def test_the_version_is_printed(self):
        _, _, _, out = install_with_stubbed_ssh(python3_version="3.13")
        self.assertIn("3.13", out)

    def test_a_host_under_the_floor_is_told_about(self):
        low = "%d.%d" % (edit_helper.MIN_PYTHON[0], edit_helper.MIN_PYTHON[1] - 1)
        _, _, _, out = install_with_stubbed_ssh(python3_version=low)
        self.assertIn("shunt edit", out)
        self.assertIn("REFUSE", out)

    def test_a_host_under_the_floor_is_still_registered(self):
        """The whole point of saying it instead of refusing: bash, run, cp, bg and get do
        not touch the helpers, so the machine is still worth having."""
        low = "%d.%d" % (edit_helper.MIN_PYTHON[0], edit_helper.MIN_PYTHON[1] - 1)
        code, _, seen, out = install_with_stubbed_ssh(python3_version=low)
        self.assertEqual(code, 0)
        self.assertIn("shunt.toml", out)  # the host entry was written
        self.assertEqual(len(seen), 2)  # and the connection test still ran after it

    def test_a_host_at_the_floor_says_nothing_extra(self):
        """The note is for the exception. On every ordinary host it must be absent, or it
        becomes the wallpaper this project keeps refusing to hang."""
        at = "%d.%d" % edit_helper.MIN_PYTHON
        _, _, _, out = install_with_stubbed_ssh(python3_version=at)
        self.assertNotIn("REFUSE", out)

    def test_an_answer_we_did_not_shape_makes_no_claim(self):
        """A python that answers something else - a banner, a warning, nothing at all -
        gets no verdict invented about it (Sec. 2: never state what has not been verified)."""
        _, _, _, out = install_with_stubbed_ssh(python3_version="Python 3.4.10")
        self.assertNotIn("REFUSE", out)


if __name__ == "__main__":
    unittest.main()
