"""
Tests for shunt.cli — the `bg` subcommand (long jobs on a host, via systemd-run).

Two silences met here, of the same shape: a first step whose failure nobody looks at,
and a second step that speaks as if it had succeeded.

  --stop  ran `systemctl stop JOB; …; echo stopped`. The `;` let the stop fail and the
          `echo` say "stopped" anyway — and since `echo` was the last command, ssh came
          back 0, so a script reading the exit code carried on too. A mistyped unit name
          answered exactly like a real one while the job kept running: the tool stated
          what it had not verified.

  --name  with no label after it was left in the argument list and joined into the
          command, so `shunt bg @h "deploy.sh" --name` shipped `deploy.sh --name` to the
          far machine. The two hands beside it (--status, --stop) already refuse a flag
          without its argument; this one fell to a default, and the default was a command
          nobody typed.

Coverage:
  - --stop ties both the word and the exit code to what systemctl actually did
  - a stop that fails says nothing about having stopped anything
  - the fire-and-forget cleanup (reset-failed) stays fire-and-forget, inside the success
  - --name without a label is refused, and the refusal says what it wants
  - --name with a label still works, and the label still gets sanitised
  - the ordinary shapes (--list, --status, starting a job) are untouched

ssh is stubbed everywhere — no connection is attempted. SHUNT_CONF points at a temp dir
with one fake host.
"""

import io
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


# ── helpers ────────────────────────────────────────────────────────────────────


class TmpHosts:
    """Context manager: temp CONF holding one host in shunt.toml."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-bg-")
        with open(os.path.join(self.dir, "shunt.toml"), "w") as f:
            f.write('[hosts]\nh1 = "root@10.0.0.1"\n')
        self._orig = shunt_mod.CONF
        shunt_mod.CONF = self.dir
        return self

    def __exit__(self, *_):
        shunt_mod.CONF = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)


def bg_with_stubbed_ssh(argv, returncode=0):
    """Call cmd_bg with ssh stubbed. Returns (remote command string, exit code)."""
    seen = {}

    def fake_run(a, *args, **kwargs):
        seen["argv"] = a
        return MagicMock(returncode=returncode)

    with patch.object(shunt_mod.subprocess, "run", fake_run):
        rc = shunt_mod.cmd_bg(argv)
    return seen["argv"][-1], rc


def run_far_side(script, stop_succeeds):
    """Run the generated remote script under a bash with a FAKE systemctl.

    The point of the fix is what the far shell does with the chain, so the chain is
    executed rather than pattern-matched: a stub `systemctl` on PATH answers success or
    failure, and what comes back is what a caller would actually see.
    """
    binn = tempfile.mkdtemp(prefix="shunt-test-bg-bin-")
    try:
        stub = os.path.join(binn, "systemctl")
        with open(stub, "w") as f:
            f.write(
                "#!/bin/sh\n"
                'if [ "$1" = stop ]; then\n'
                '  echo "Failed to stop $2: Unit $2 not loaded." >&2\n'
                f"  exit {0 if stop_succeeds else 5}\n"
                "fi\n"
                "exit 0\n"
            )
        os.chmod(stub, 0o755)
        return subprocess.run(
            ["bash", "-c", script],
            env={"PATH": binn + os.pathsep + os.environ.get("PATH", ""), "HOME": binn},
            capture_output=True,
            text=True,
        )
    finally:
        shutil.rmtree(binn, ignore_errors=True)


# ── --stop: the word and the exit code must follow the deed ────────────────────


class TestStopIsHonest(unittest.TestCase):
    def _script(self, job="shunt-nightly"):
        with TmpHosts():
            script, _ = bg_with_stubbed_ssh(["@h1", "--stop", job])
        return script

    def test_a_failed_stop_does_not_say_stopped(self):
        r = run_far_side(self._script(), stop_succeeds=False)
        self.assertNotIn("stopped", r.stdout)

    def test_a_failed_stop_comes_back_non_zero(self):
        """The exit code is what a script reads; `echo` used to make it 0."""
        r = run_far_side(self._script(), stop_succeeds=False)
        self.assertNotEqual(r.returncode, 0)

    def test_a_failed_stop_still_says_WHY(self):
        """systemctl's own stderr was never the problem — it must survive the fix."""
        r = run_far_side(self._script(), stop_succeeds=False)
        self.assertIn("not loaded", r.stderr)

    def test_a_real_stop_still_reports_and_succeeds(self):
        r = run_far_side(self._script(), stop_succeeds=True)
        self.assertIn("stopped", r.stdout)
        self.assertEqual(r.returncode, 0)

    def test_the_job_is_named_in_what_comes_back(self):
        r = run_far_side(self._script(), stop_succeeds=True)
        self.assertIn("shunt-nightly", r.stdout)

    def test_the_cleanup_stays_fire_and_forget(self):
        """reset-failed is housekeeping after a stop that happened: silenced, and its
        result read by nobody — but it may not run when the stop did not."""
        script = self._script()
        self.assertIn("reset-failed", script)
        self.assertIn("2>/dev/null", script)
        self.assertNotIn(";", script.split("&&")[0])  # nothing rides on the stop's `;`

    def test_the_job_name_is_still_quoted(self):
        script = self._script("weird; rm -rf /")
        self.assertIn("'weird; rm -rf /'", script)


# ── --name: a flag without its value is refused, not absorbed ──────────────────


class TestNameNeedsALabel(unittest.TestCase):
    def _refused(self, argv):
        with TmpHosts():
            err = io.StringIO()
            with patch.object(sys, "stderr", err):
                with self.assertRaises(SystemExit) as ctx:
                    bg_with_stubbed_ssh(argv)
            return ctx.exception.code, err.getvalue()

    def test_a_trailing_name_flag_is_refused(self):
        code, _ = self._refused(["@h1", "deploy.sh", "--name"])
        self.assertNotEqual(code, 0)

    def test_the_refusal_says_what_it_wants(self):
        _, err = self._refused(["@h1", "deploy.sh", "--name"])
        self.assertIn("--name", err)
        self.assertIn("label", err)

    def test_the_flag_never_reaches_the_far_machine(self):
        """The old behaviour: `deploy.sh --name` was sent and run over there."""
        sent = {}

        def fake_run(a, *args, **kwargs):
            sent["cmd"] = a[-1]
            return MagicMock(returncode=0)

        with TmpHosts():
            with patch.object(shunt_mod.subprocess, "run", fake_run):
                with patch.object(sys, "stderr", io.StringIO()):
                    with self.assertRaises(SystemExit):
                        shunt_mod.cmd_bg(["@h1", "deploy.sh", "--name"])
        self.assertNotIn("cmd", sent)

    def test_a_label_that_is_present_still_works(self):
        with TmpHosts():
            script, _ = bg_with_stubbed_ssh(["@h1", "sleep 60", "--name", "nightly"])
        self.assertIn("--unit=shunt-nightly", script)
        self.assertNotIn("--name", script)

    def test_an_illegal_label_is_still_sanitised(self):
        with TmpHosts():
            script, _ = bg_with_stubbed_ssh(["@h1", "sleep 60", "--name", "Nightly Build!"])
        self.assertIn("--unit=shunt-nightly-build", script)


# ── the shapes that were already right stay right ──────────────────────────────


class TestUntouchedShapes(unittest.TestCase):
    def test_list_still_asks_systemctl(self):
        with TmpHosts():
            script, _ = bg_with_stubbed_ssh(["@h1", "--list"])
        self.assertIn("list-units", script)

    def test_status_still_needs_a_job(self):
        with TmpHosts():
            with patch.object(sys, "stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    shunt_mod.cmd_bg(["@h1", "--status"])

    def test_stop_still_needs_a_job(self):
        with TmpHosts():
            with patch.object(sys, "stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    shunt_mod.cmd_bg(["@h1", "--stop"])

    def test_starting_a_job_still_announces_its_unit(self):
        with TmpHosts():
            script, _ = bg_with_stubbed_ssh(["@h1", "sleep 60"])
        self.assertIn("systemd-run", script)
        self.assertIn("JOB=shunt-", script)


if __name__ == "__main__":
    unittest.main()
