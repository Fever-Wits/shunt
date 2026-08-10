"""
Tests for shunt.cli — the `bg` subcommand (long jobs on a host, via systemd-run).

Four silences met here, of the same shape: a first step whose failure nobody looks at,
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

  --list  ended in `|| true`. `systemctl list-units` already exits 0 when the glob
          matches nothing, so the guard bought the empty list nothing and paid out only
          when the question could not be answered at all — no systemd, no permission, a
          bad invocation — handing back exit 0 and no output, which is indistinguishable
          from "this host has no jobs". No motive for the guard was ever recorded: it
          arrived with the first commit that tracked this file, and later silent-failure
          passes walked past it. It was measured instead of guessed at, and removed.

  --status ran `systemctl show`, which INVENTS an answer for a unit it has never heard
          of: every property at its default — Result=success, SubState=dead,
          ExecMainStatus=0 — at exit 0. A mistyped job name was therefore indistinguishable
          from a job that had finished cleanly, in the one hand here that runs with nobody
          watching the screen. LoadState is what tells them apart.

Coverage:
  - --status refuses to dress a unit that is not there as a job that succeeded, names it,
    and still shows what systemd said — contradicted rather than hidden
  - a host that cannot answer at all (no systemd, no permission) says THAT instead
  - --stop ties both the word and the exit code to what systemctl actually did
  - a stop that fails says nothing about having stopped anything
  - the fire-and-forget cleanup (reset-failed) stays fire-and-forget, inside the success
  - --name without a label is refused, and the refusal says what it wants
  - --name with a label still works, and the label still gets sanitised
  - --list hands back what systemctl actually did, and an empty list is still a success
  - the ordinary shapes (--status, starting a job) are untouched

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
            f.write('[hosts]\nh1 = "root@203.0.113.1"\n')
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


def run_far_side_with_systemctl(script, stub_body):
    """Run the generated remote script under a bash whose `systemctl` is a stub.

    The point of these fixes is what the far shell does with the chain, so the chain is
    executed rather than pattern-matched: a stub `systemctl` on PATH answers success or
    failure, and what comes back is what a caller would actually see. `stub_body` is the
    /bin/sh body of that stub, so each caller spells out the far side it is asking about.
    """
    binn = tempfile.mkdtemp(prefix="shunt-test-bg-bin-")
    try:
        stub = os.path.join(binn, "systemctl")
        with open(stub, "w") as f:
            f.write("#!/bin/sh\n" + stub_body)
        os.chmod(stub, 0o755)
        return subprocess.run(
            ["bash", "-c", script],
            env={"PATH": binn + os.pathsep + os.environ.get("PATH", ""), "HOME": binn},
            capture_output=True,
            text=True,
        )
    finally:
        shutil.rmtree(binn, ignore_errors=True)


def run_far_side(script, stop_succeeds):
    """A far side where `systemctl stop` succeeds or fails, and everything else works."""
    return run_far_side_with_systemctl(
        script,
        'if [ "$1" = stop ]; then\n'
        '  echo "Failed to stop $2: Unit $2 not loaded." >&2\n'
        f"  exit {0 if stop_succeeds else 5}\n"
        "fi\n"
        "exit 0\n",
    )


# far sides for `--list`, each one thing that can happen when the question is asked
LIST_CANNOT_ANSWER = (
    'if [ "$1" = list-units ]; then\n'
    '  echo "Failed to list units: Connection reset by peer" >&2\n'
    "  exit 1\n"
    "fi\n"
    "exit 0\n"
)
LIST_MATCHES_NOTHING = "exit 0\n"  # what real systemctl does: says nothing, succeeds
LIST_HAS_JOBS = 'echo "shunt-A1B2C3D4.service loaded active running /bin/sh -c nightly"\nexit 0\n'


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


# ── --list: a question that could not be answered is not an empty answer ───────


class TestListIsHonest(unittest.TestCase):
    """`|| true` used to ride on the end of the --list command.

    It never bought the empty list anything — `systemctl list-units` already exits 0
    when the glob matches nothing — and it cost the only case that mattered: a systemctl
    that could NOT answer came back 0 with no output, which reads exactly like "this host
    has no jobs". The caller cannot tell the two apart, and the wrong one is silent.
    """

    def _script(self):
        with TmpHosts():
            script, _ = bg_with_stubbed_ssh(["@h1", "--list"])
        return script

    def test_a_list_that_could_not_run_comes_back_non_zero(self):
        """The exit code is what a script reads; `true` used to answer for systemctl."""
        r = run_far_side_with_systemctl(self._script(), LIST_CANNOT_ANSWER)
        self.assertNotEqual(r.returncode, 0)

    def test_the_guard_is_not_back(self):
        """Named so a future hand that re-adds `|| true` hears about it here."""
        self.assertNotIn("|| true", self._script())

    def test_a_failed_list_still_says_WHY(self):
        """systemctl's own stderr was never the problem — it must survive the fix."""
        r = run_far_side_with_systemctl(self._script(), LIST_CANNOT_ANSWER)
        self.assertIn("Connection reset", r.stderr)

    def test_nothing_matching_is_still_a_success(self):
        """What the guard was believed to be for. It was already true without it."""
        r = run_far_side_with_systemctl(self._script(), LIST_MATCHES_NOTHING)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_a_host_with_jobs_still_prints_them_and_succeeds(self):
        r = run_far_side_with_systemctl(self._script(), LIST_HAS_JOBS)
        self.assertEqual(r.returncode, 0)
        self.assertIn("shunt-A1B2C3D4", r.stdout)


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


# ── the fourth silence: --status on a unit that does not exist ─────────────────


class TestStatusOfAJobThatIsNotThere(unittest.TestCase):
    """`systemctl show` INVENTS an answer for a unit it has never heard of.

    Every property comes back at its default — `Result=success`, `SubState=dead`,
    `ExecMainStatus=0` — and it exits 0 while doing it. So a mistyped job name read exactly
    like a job that had finished cleanly: the same shape as the three silences above, in
    the hand that runs with nobody watching the screen. LoadState is the one property that
    tells them apart, and it is asked as a QUESTION rather than merely printed, because a
    listing is read by a human and an exit code by a script.
    """

    def script_for(self, job="shunt-nightly"):
        with TmpHosts():
            script, _ = bg_with_stubbed_ssh(["@h1", "--status", job])
        return script

    SYSTEMD_INVENTING_AN_ANSWER = """case "$*" in
  *LoadState*) echo "LoadState=not-found"; echo "ExecMainStatus=0"; echo "Result=success";
               echo "SubState=dead"; exit 0;;
esac
exit 0
"""
    A_REAL_JOB_THAT_FAILED = """case "$*" in
  *LoadState*) echo "LoadState=loaded"; echo "ExecMainStatus=3"; echo "Result=exit-code";
               echo "SubState=failed"; exit 0;;
esac
exit 0
"""

    def test_a_unit_that_does_not_exist_is_not_a_success(self):
        r = run_far_side_with_systemctl(self.script_for(), self.SYSTEMD_INVENTING_AN_ANSWER)
        self.assertNotEqual(r.returncode, 0, "exit 0 here is Result=success theatre")

    def test_it_says_which_job_and_that_the_status_is_about_nothing(self):
        r = run_far_side_with_systemctl(self.script_for("shunt-typo"), self.SYSTEMD_INVENTING_AN_ANSWER)
        self.assertIn("no such job shunt-typo", r.stderr)
        self.assertIn("NOTHING", r.stderr)
        self.assertIn("--list", r.stderr)

    def test_the_invented_properties_are_still_shown(self):
        """Not hidden — contradicted. The reader sees what systemd said AND that it was
        said about a unit that is not there."""
        r = run_far_side_with_systemctl(self.script_for(), self.SYSTEMD_INVENTING_AN_ANSWER)
        self.assertIn("Result=success", r.stdout)

    def test_a_real_job_is_untouched(self):
        """The guard may not cost the ordinary question its answer — including a job that
        ran and failed, which is a successful ANSWER to a status query."""
        r = run_far_side_with_systemctl(self.script_for(), self.A_REAL_JOB_THAT_FAILED)
        self.assertEqual(r.returncode, 0)
        self.assertIn("SubState=failed", r.stdout)
        self.assertEqual(r.stderr.strip(), "")

    def test_a_host_without_systemd_says_so_and_does_not_pass(self):
        """The other way the question goes unanswered: no systemd, no permission, a bad
        invocation. Silence there would read as "no such job" — a different fact."""
        r = run_far_side_with_systemctl(self.script_for(), "exit 127\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("could not ask systemd", r.stderr)

    def test_the_job_name_is_quoted_once_and_used_everywhere(self):
        """Two questions and two messages carry the same name; a variable is what keeps
        them from drifting apart, and what keeps a name with a space in one piece."""
        script = self.script_for("my job")
        self.assertIn("__shunt_job='my job'", script)
        self.assertNotIn("systemctl show my job", script)


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
