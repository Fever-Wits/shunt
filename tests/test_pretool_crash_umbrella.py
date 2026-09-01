"""
Tests for pretool.py - what happens when the HOOK ITSELF crashes.

The finding this file exists for is not a bug in shunt; it is what the harness does with
one. A hook that raises exits non-zero-but-not-2, and that is a NON-BLOCKING error: the
message is shown and the ORIGINAL command runs. On a session routed to a server, that is
`rm -rf /srv/old` - written for the far machine - deleting the local tree, reached through
shunt's own traceback rather than through anything the user did wrong.

Two fixes already in this tree have exactly that shape, each found after the fact and each
closing ONE path:

  - `os.makedirs(CONF)` stood outside the guard the write below it sat in, so `@web-01`
    against a broken config dir went to bash as a command (test_pretool_target_state.py);
  - the routing file was written with `open(..., "w")`, which truncates, so a write dying in
    between left a file that existed and named no host.

The umbrella closes the class instead of the next instance: `main()` is now a roof over
`decide()`, and an exception that reaches it is answered the way the hook-input branches
answer their own blindness - by WHO can still repair the hook.

  Bash            -> BLOCKED, exit 2, the reason and the traceback on stderr.
  any other tool  -> ALLOWED and TOLD, traceback inside the message. Read/Edit/Grep cannot
                    act on another machine, and they are the door out: a deterministic bug
                    that blocked them too would cost the session, every session.
  tool unknown    -> BLOCKED, all of it. A crash before `tool_name` was read cannot tell a
                    bash command from a file read.

⚠ These tests run the hook IN-PROCESS. Everything else in this suite spawns it the way the
harness does, and should; an injected failure cannot cross that boundary - and no input or
filesystem shape reaches these paths, which was verified before the roof was written (a
config dir that cannot be written to, a session id with a NUL byte, a non-dict tool_input:
the hook answers all of them cleanly). That is the point: the roof is for what has not
been thought of, so its test has to inject what has not been thought of.

Coverage:
  - a crash on the REMOTE path (the dangerous one) blocks with exit 2 and runs nothing
  - a crash EARLIER, before the routing is even read, does the same
  - the caller's command survives nowhere in the reply
  - the reason and the traceback reach stderr - "fix the hook" needs an address
  - exit code is exactly 2: 1 would be non-blocking, which is the whole defect
  - the repairing hands stay open: a file tool runs, and is told, with the traceback
  - ...unless the tool's name is what the crash came before - then nothing goes through
  - the roof does NOT swallow the deliberate exits - a rewrite, a refusal and a switch all
    still come out the way they always did
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

# Make the src tree importable when run without installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shunt import pretool


class HookConf:
    """A temp SHUNT_CONF with one host, optionally routing a session to it."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-umbrella-")
        with open(os.path.join(self.dir, "shunt.toml"), "w") as f:
            f.write('[hosts]\nh1 = "root@203.0.113.11"\n')
        return self

    def __exit__(self, *_):
        shutil.rmtree(self.dir, ignore_errors=True)

    def route_to(self, alias, sid="s1"):
        with open(os.path.join(self.dir, "target." + sid), "w") as f:
            f.write(alias)


DANGEROUS = "rm -rfv /srv/old-release"


def run_hook(conf, payload, break_fn=None, exc=None):
    """Run the hook in-process, optionally with one function replaced by a failure.

    Returns (exit code, stdout, stderr). The failure is injected at a real call site on a
    real path - not at decide() itself - so what is being proved is that a crash ANYWHERE
    inside comes out as a refusal.
    """
    out, err = io.StringIO(), io.StringIO()
    patches = [
        patch.object(pretool, "CONF", conf.dir),
        patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
        patch.object(sys, "stdout", out),
        patch.object(sys, "stderr", err),
    ]
    if break_fn:
        patches.append(patch.object(pretool, break_fn, side_effect=exc or RuntimeError("a bug nobody foresaw")))
    code = None
    try:
        for p in patches:
            p.start()
        try:
            pretool.main()
        except SystemExit as e:
            code = e.code
    finally:
        for p in reversed(patches):
            p.stop()
    return code, out.getvalue(), err.getvalue()


def what_runs(stdout):
    """The command the harness would run - "" when the hook wrote no reply at all."""
    if not stdout.strip():
        return ""
    return json.loads(stdout)["hookSpecificOutput"].get("updatedInput", {}).get("command", "")


# -- a crash on the path where it costs the most --------------------------------


class TestACrashOnTheRemotePathIsBlocked(unittest.TestCase):
    """The session is routed to @h1 and the rewrite is the very last thing that happens.
    A failure there used to fall out of main() as a traceback - and the command the caller
    wrote for the server then ran on this machine."""

    def _crash(self, conf):
        conf.route_to("h1")
        return run_hook(
            conf,
            {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": DANGEROUS}},
            break_fn="ssh_command",
        )

    def test_it_blocks(self):
        with HookConf() as c:
            code, _, err = self._crash(c)
            self.assertEqual(code, 2, "exit 0/1 is NON-BLOCKING - the harness then runs the original command")
            self.assertIn("CRASHED", err)

    def test_the_command_does_not_run(self):
        with HookConf() as c:
            _, out, _ = self._crash(c)
            self.assertNotIn("rm -rfv", what_runs(out))
            self.assertNotIn("/srv/old-release", what_runs(out))

    def test_stdout_stays_empty(self):
        """Half a reply beside exit 2 would hand the harness two answers."""
        with HookConf() as c:
            _, out, _ = self._crash(c)
            self.assertEqual(out.strip(), "")

    def test_it_says_the_command_was_not_run(self):
        with HookConf() as c:
            _, _, err = self._crash(c)
            self.assertIn("NOT run", err)
            self.assertIn("BLOCKED", err)

    def test_it_names_the_error(self):
        with HookConf() as c:
            c.route_to("h1")
            _, _, err = run_hook(
                c,
                {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": "ls"}},
                break_fn="ssh_command",
                exc=OSError(28, "No space left on device"),
            )
            self.assertIn("OSError", err)
            self.assertIn("No space left on device", err)

    def test_the_traceback_names_the_line(self):
        """Saying "fix the hook" with no address is not an instruction. The traceback is how the
        session that is now without bash still knows where to Edit - from outside it."""
        with HookConf() as c:
            _, _, err = self._crash(c)
            self.assertIn("Traceback", err)
            self.assertIn("pretool.py", err)

    def test_it_points_outside_the_session(self):
        """The last resort is named even when the file tools are still open: a bug that
        breaks the hook for every session needs a door that does not go through one."""
        with HookConf() as c:
            _, _, err = self._crash(c)
            self.assertIn("outside this session", err)

    def test_it_says_the_file_tools_still_work(self):
        """The other half of the same message, and the reason `Edit` is not stopped: this
        session can still repair pretool.py from inside itself."""
        with HookConf() as c:
            _, _, err = self._crash(c)
            self.assertIn("file tools still work", err)


class TestACrashBeforeTheRoutingIsReadIsBlockedToo(unittest.TestCase):
    """Earlier is not safer. Until the routing has been read, "this session is local" is
    precisely the thing that has not been established."""

    def test_a_crash_reading_the_routing_blocks(self):
        with HookConf() as c:
            code, out, err = run_hook(
                c,
                {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": DANGEROUS}},
                break_fn="read_target",
            )
            self.assertEqual(code, 2)
            self.assertIn("CRASHED", err)
            self.assertNotIn("rm -rfv", what_runs(out))

    def test_a_deliberate_fail_open_still_stands(self):
        """A file tool's notice already sits in an `except Exception: pass` whose comment
        reads "never break someone else's tool", and it is right: a `Read` must not die
        because a warning could not be composed. The roof does not overrule a fail-open
        that was chosen on purpose; it catches what escapes one - which is why the tests
        below inject at `_missing_field`, above the tool branch, instead."""
        with HookConf() as c:
            c.route_to("h1")
            code, _, _ = run_hook(
                c,
                {"tool_name": "Read", "session_id": "s1", "tool_input": {"file_path": "/etc/hosts"}},
                break_fn="warn_if_off_mode",
            )
            self.assertEqual(code, 0)


# -- the hands that repair the hook stay open -----------------------------------


class TestTheRepairingHandsSurviveACrash(unittest.TestCase):
    """Blocking every tool on any crash would wall up the one door out: `Edit` on
    pretool.py needs no bash, and a deterministic bug would otherwise cost the session,
    every session, until someone reached a terminal outside it. So the roof asks the same
    question the `missing` branch asks - which tool is this - and answers it the same way.

    The failure is injected at `_missing_field`, which runs AFTER `tool_name` is read and
    BEFORE the tool branch: the roof therefore knows what it is looking at, and the crash
    is still real.
    """

    def _crash_on(self, conf, tool, tool_input):
        return run_hook(
            conf,
            {"tool_name": tool, "session_id": "s1", "tool_input": tool_input},
            break_fn="_missing_field",
        )

    def test_a_file_tool_still_runs(self):
        with HookConf() as c:
            for tool in ("Read", "Edit", "Write", "Grep", "Glob"):
                code, out, _ = self._crash_on(c, tool, {"file_path": "/etc/hosts"})
                self.assertEqual(code, 0, "%s was stopped - the hook cannot be repaired from in here" % tool)
                self.assertNotIn("updatedInput", out, "%s must run as it was asked" % tool)

    def test_and_is_told_what_happened(self):
        with HookConf() as c:
            _, out, _ = self._crash_on(c, "Edit", {"file_path": "/x"})
            said = json.loads(out)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("CRASHED", said)
            self.assertIn("Edit", said)
            self.assertIn("bash commands are REFUSED", said)

    def test_the_traceback_travels_with_it(self):
        """At exit 0 the message is the only channel there is - and it is the message the
        tool that will fix pretool.py is reading."""
        with HookConf() as c:
            _, out, _ = self._crash_on(c, "Edit", {"file_path": "/x"})
            said = json.loads(out)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Traceback", said)
            self.assertIn("pretool.py", said)

    def test_an_agent_is_not_stopped_either(self):
        with HookConf() as c:
            code, _, _ = self._crash_on(c, "Agent", {"prompt": "go"})
            self.assertEqual(code, 0)

    def test_bash_is_still_blocked_in_the_same_state(self):
        """The distinction is the whole point: the same crash, the tool that can act on
        the wrong machine, and the opposite answer."""
        with HookConf() as c:
            c.route_to("h1")
            code, out, err = self._crash_on(c, "Bash", {"command": DANGEROUS})
            self.assertEqual(code, 2)
            self.assertNotIn("rm -rfv", what_runs(out))
            self.assertIn("CRASHED", err)

    def test_it_repeats(self):
        """No budget: the fault is happening NOW, and the budget is a file in a config dir
        the crash may well be about."""
        with HookConf() as c:
            first = self._crash_on(c, "Read", {"file_path": "/x"})[1]
            second = self._crash_on(c, "Read", {"file_path": "/x"})[1]
            for said in (first, second):
                self.assertIn("CRASHED", json.loads(said)["hookSpecificOutput"]["additionalContext"])


class TestACrashBeforeTheToolIsKnownStopsEverything(unittest.TestCase):
    """The one state where the price is paid in full, and it is the state the
    unreadable-input branch already refuses for: with no tool name, a bash command and a
    file read are the same shape, and only one of them is safe to let through.

    Reaching it takes a failure in the handful of lines ABOVE the tool name - where the
    only ordinary failure (`json.load` on bad bytes) is already caught and answered. So it
    is reached the way it would really be reached: reading stdin dies with something that
    is not an `Exception` at all, which no `except Exception` in there catches.
    """

    class DyingStdin:
        def read(self, *a):
            raise KeyboardInterrupt()

    def _crash_before_the_name(self, conf):
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(pretool, "CONF", conf.dir),
            patch.object(sys, "stdin", self.DyingStdin()),
            patch.object(sys, "stdout", out),
            patch.object(sys, "stderr", err),
        ):
            with self.assertRaises(SystemExit) as ctx:
                pretool.main()
        return ctx.exception.code, out.getvalue(), err.getvalue()

    def test_it_blocks(self):
        with HookConf() as c:
            code, _, err = self._crash_before_the_name(c)
            self.assertEqual(code, 2)
            self.assertIn("CRASHED", err)

    def test_it_says_why_everything_is_stopped(self):
        """The price is named rather than dodged - the same way the unreadable-input
        branch names it."""
        with HookConf() as c:
            _, _, err = self._crash_before_the_name(c)
            self.assertIn("Every tool is stopped", err)
            self.assertIn("same shape", err)

    def test_nothing_is_written_to_stdout(self):
        with HookConf() as c:
            _, out, _ = self._crash_before_the_name(c)
            self.assertEqual(out.strip(), "")

    def test_a_name_from_an_earlier_call_is_not_reused(self):
        """The roof reads a module global, and a suite calls this twice in one process.
        A `Read` that crashed a moment ago must not make the NEXT crash - the one with no
        name at all - look like a file tool and be waved through."""
        with HookConf() as c:
            run_hook(
                c,
                {"tool_name": "Read", "session_id": "s1", "tool_input": {"file_path": "/x"}},
                break_fn="_missing_field",
            )
            code, _, err = self._crash_before_the_name(c)
            self.assertEqual(code, 2)
            self.assertIn("Every tool is stopped", err)


class TestAnInterruptedHookIsACrashToo(unittest.TestCase):
    """KeyboardInterrupt and friends are not Exception, and the harness does not care
    about that distinction: an interrupted hook is a hook that decided nothing."""

    def test_a_base_exception_is_caught(self):
        with HookConf() as c:
            c.route_to("h1")
            code, out, err = run_hook(
                c,
                {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": DANGEROUS}},
                break_fn="ssh_command",
                exc=KeyboardInterrupt(),
            )
            self.assertEqual(code, 2)
            self.assertNotIn("rm -rfv", what_runs(out))


# -- and the deliberate exits are NOT swallowed ---------------------------------


class TestTheRoofLetsEveryDeliberateAnswerThrough(unittest.TestCase):
    """emit, echo, warn and block all exit - a roof that caught SystemExit would catch the
    whole hook. This is the line the roof must never cross, and it is asserted at each of
    the four shapes rather than argued once."""

    def test_a_rewrite_still_comes_out(self):
        with HookConf() as c:
            c.route_to("h1")
            code, out, _ = run_hook(c, {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": "ls /srv"}})
            self.assertEqual(code, 0)
            self.assertIn("ssh", what_runs(out))
            self.assertIn("ls /srv", what_runs(out))

    def test_a_refusal_still_comes_out(self):
        with HookConf() as c:
            c.route_to("gone-alias")
            code, out, _ = run_hook(c, {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": "ls"}})
            self.assertEqual(code, 0)
            self.assertIn("NOT run", what_runs(out))

    def test_a_switch_still_comes_out(self):
        with HookConf() as c:
            code, out, _ = run_hook(c, {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": "@status"}})
            self.assertEqual(code, 0)
            self.assertIn("LOCAL", what_runs(out))

    def test_a_block_still_blocks_with_its_own_words(self):
        """The unreadable-input block travels through the roof untouched: its message, not
        the crash message, and no traceback attached to a state nothing went wrong in."""
        with HookConf() as c:
            out, err = io.StringIO(), io.StringIO()
            with (
                patch.object(pretool, "CONF", c.dir),
                patch.object(sys, "stdin", io.StringIO("not json")),
                patch.object(sys, "stdout", out),
                patch.object(sys, "stderr", err),
            ):
                with self.assertRaises(SystemExit) as ctx:
                    pretool.main()
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("UNREADABLE", err.getvalue())
            self.assertNotIn("CRASHED", err.getvalue())

    def test_an_ordinary_local_command_is_untouched(self):
        with HookConf() as c:
            code, out, err = run_hook(c, {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": "ls"}})
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "")
            self.assertEqual(err.strip(), "")


if __name__ == "__main__":
    unittest.main()
