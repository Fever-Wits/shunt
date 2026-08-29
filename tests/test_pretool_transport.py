"""
Tests for pretool.py - the local epilogue that reads ssh's own exit code.

Everything else this hook says is written BEFORE anything runs: it builds a command and
never sees a result. The epilogue is the one thing it can say about what came BACK, and it
can say it because the rewritten command is the hook's own string - a line of shell after
the ssh call, running locally, looking at the number ssh handed back.

The number is 255, the code ssh reserves for its own failures. Read without help it is a
verdict from the caller's own program: a session whose host went down mid-work sees
`exit 255` out of `make`, or `grep`, or whatever they believed they were running, and goes
looking for a bug in it. One sentence is the whole fix - the command never left.

Two things it must not do, and both are tested here:

  change the exit code. The caller's 0, 1 or 42 goes on untouched; the epilogue only
  speaks. A guard that edits what it reports is worse than no guard.

  speak out of turn. This rides on EVERY remote command of every session, so any code but
  255 must leave both streams exactly as they were - noise here is noise everywhere, and
  wallpaper is silent precisely when it needs to be read.

WARNING: The shell decides here, not the assertion: these run the epilogue through a real bash
and read the real exit code and the real stderr. A test that only grepped the string would
pass just as happily on shell that does not parse.
"""

import os
import shlex
import subprocess
import sys
import unittest

# Make the src tree importable when run without installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shunt import pretool

HOST = {"alias": "web-01", "target": "root@203.0.113.9", "key": None}


def run_epilogue(code, alias="web-01"):
    """`(exit CODE)` standing in for the ssh call, then the REAL epilogue, through bash.

    The subshell is the honest stand-in: ssh is a command that leaves an exit code behind,
    and that is the entire surface the epilogue touches.
    """
    script = f"(exit {code}){pretool._transport_epilogue(alias)}"
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return done.returncode, done.stdout, done.stderr


class TestTransportFailureIsNamed(unittest.TestCase):
    def test_255_says_the_command_never_ran(self):
        _, _, err = run_epilogue(255)
        self.assertIn("[shunt]", err)
        self.assertIn("exit 255 = ssh transport failure", err)
        self.assertIn("almost certainly never ran", err)

    def test_it_names_the_host_the_session_is_routed_to(self):
        """A generic "the host" is not a host. The session may have switched three times."""
        _, _, err = run_epilogue(255, alias="db-7")
        self.assertIn("@db-7", err)

    def test_it_points_at_the_way_out(self):
        """A reader told only that the transport failed has nowhere to go next."""
        _, _, err = run_epilogue(255)
        self.assertIn("@local", err)

    def test_it_does_not_claim_what_it_did_not_check(self):
        """A remote command is free to exit 255 on its own account - rare, and the hook
        may not overwrite the caller's own judgement with a certainty it does not have."""
        _, _, err = run_epilogue(255)
        self.assertIn("almost certainly", err)
        self.assertNotIn("did not run", err)

    def test_the_code_survives_the_sentence(self):
        rc, _, _ = run_epilogue(255)
        self.assertEqual(rc, 255)

    def test_it_speaks_on_stderr_and_leaves_stdout_alone(self):
        """stdout is the caller's DATA - a sentence in it lands in whatever they piped
        the command into, and a guard that corrupts output is a bug wearing a hat."""
        _, out, err = run_epilogue(255)
        self.assertEqual(out, "")
        self.assertNotEqual(err, "")


class TestEveryOtherCodeIsLeftAlone(unittest.TestCase):
    """This rides on every remote command there is. Silence is the default, not a case."""

    def test_success_is_silent(self):
        self.assertEqual(run_epilogue(0), (0, "", ""))

    def test_an_ordinary_failure_is_silent(self):
        self.assertEqual(run_epilogue(1), (1, "", ""))

    def test_an_arbitrary_code_is_silent_and_arrives_whole(self):
        self.assertEqual(run_epilogue(42), (42, "", ""))


class TestItRidesOnTheRealCommand(unittest.TestCase):
    def test_the_rewritten_command_carries_it(self):
        cmd = pretool.ssh_command(HOST, "ls", "sess-1")
        self.assertIn(pretool._transport_epilogue("web-01"), cmd)

    def test_it_is_the_last_thing_in_the_string(self):
        """Anything after `exit` would never run - the position is the contract."""
        cmd = pretool.ssh_command(HOST, "ls", "sess-1")
        self.assertTrue(cmd.endswith(pretool._transport_epilogue("web-01")))

    def test_it_names_the_alias_of_the_host_it_was_built_for(self):
        cmd = pretool.ssh_command(HOST, "ls", "sess-1")
        self.assertIn("@web-01 is down or unreachable", cmd)

    def test_the_whole_rewritten_command_is_valid_shell(self):
        for housekeeping in (False, True):
            cmd = pretool.ssh_command(HOST, "ls", "sess-1", housekeeping)
            check = subprocess.run(["bash", "-n"], input=cmd, capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)

    def test_the_far_side_payload_is_untouched(self):
        """The epilogue is LOCAL shell and may not leak into the string the far side is
        handed: over there it would run on the machine that, when this fires, is missing.
        The payload is asserted whole, and the variable name is asserted absent."""
        remote = pretool._remote_script("ls", "sess-1")
        cmd = pretool.ssh_command(HOST, "ls", "sess-1")
        self.assertIn(shlex.quote(remote), cmd)
        self.assertNotIn("__shunt_rc", remote)


if __name__ == "__main__":
    unittest.main()
