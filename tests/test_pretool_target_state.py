"""
Tests for shunt.pretool — `target.<sid>`, the file that says where a session is routed.

Two defects met in the middle here. The write used `open(…, "w")`, which TRUNCATES before
it writes, so a write that died in between left a file that exists and names no host. The
read handed exactly that back as "" — the same answer it gives for a file that was never
created — and "" means local. A session that believed itself on a server then ran its next
command here, silently. For a tool whose default is "run it here", every quiet fall to the
default is a fall to the wrong machine, so the failure has to land on refusal instead.

So the write goes through the config module's atomic writer — temp file, then os.replace:
what is on disk is the whole new alias or the whole old one, never a blank — and the read
tells three states apart where it used to tell two:

    absent            → the ordinary local session. Nothing to say, nothing to do.
    a host name       → remote, as before.
    present and empty → an anomaly. Say it; assume nothing.

Coverage:
  - an empty (or whitespace-only) routing file no longer runs the command here in silence
  - and does not send it to a host either — refused means neither machine runs it
  - a MISSING file is still the ordinary local session, untouched and unremarked
  - @status answers UNKNOWN rather than the one thing it has not verified
  - @local and @<alias> keep working on a state nobody can read: they are its remedy
  - the switch REPLACES the file instead of truncating it — a new inode, no leftovers
  - a HALF-written alias stays on the older path: it names a host that does not resolve,
    and that refusal already existed
  - the tools that ignore @host mode do not claim a mode either — once per session
  - the sentinel that carries "unreadable" never reaches a message
  - a switch that cannot be WRITTEN says so, and names the routing still in force
  - an Agent spawned into the unreadable state is warned on EVERY spawn, off the shared
    budget the file tools draw on

The hook is run as the harness runs it — a subprocess fed JSON on stdin, started BY PATH
with PYTHONPATH stripped — and SHUNT_CONF points at a temp directory, so no real config or
session state is touched.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

# Make the src tree importable when run without installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shunt import pretool

# The hook as a file: it runs in its own process, so it is invoked by path.
PRETOOL = pretool.__file__


class HookConf:
    """A temp SHUNT_CONF with one host, plus the session files the hook reads.

    It also carries a STUB `ssh`, because `@alias` now probes the host it switches to. No
    test may open a real connection: a suite that reaches the network is a suite whose
    speed and result depend on somebody else's router. The stub answers instantly and, by
    default, successfully — which is what keeps every test below about the state file
    rather than about a host being up.
    """

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-target-")
        with open(os.path.join(self.dir, "shunt.toml"), "w") as f:
            f.write('[hosts]\nh1 = "root@10.0.0.1"\n')
        self.bin = os.path.join(self.dir, "_bin")
        os.makedirs(self.bin)
        self.stub_ssh()
        return self

    def stub_ssh(self, returncode=0):
        """The `ssh` the hook will find on PATH — see _run, which puts it there."""
        path = os.path.join(self.bin, "ssh")
        with open(path, "w") as f:
            f.write("#!/bin/sh\nexit %d\n" % returncode)
        os.chmod(path, 0o755)

    def __exit__(self, *_):
        shutil.rmtree(self.dir, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.dir, name)

    def exists(self, name):
        return os.path.exists(self.path(name))

    def route_to(self, alias, sid="s1"):
        self.write_routing(alias, sid)

    def write_routing(self, text, sid="s1"):
        with open(self.path("target." + sid), "w") as f:
            f.write(text)

    def break_routing(self, sid="s1"):
        """What a torn write leaves behind: the file is there and names no host."""
        open(self.path("target." + sid), "w").close()


def _run(conf, payload):
    """PYTHONPATH is stripped for the same reason tests/test_pretool_warnings.py strips it:
    settings.json names pretool.py by absolute path, so the field never has the package on
    sys.path, and the test runner's PYTHONPATH would hide that."""
    env = dict(os.environ, SHUNT_CONF=conf.dir, PATH=conf.bin + os.pathsep + os.environ["PATH"])
    env.pop("PYTHONPATH", None)
    r = subprocess.run([sys.executable, PRETOOL], input=json.dumps(payload).encode(), capture_output=True, env=env)
    return r.returncode, r.stdout.decode()


def run_bash(conf, command, sid="s1"):
    """Run pretool.py on a Bash call, as the harness does. Returns (exit code, stdout)."""
    return _run(conf, {"tool_name": "Bash", "session_id": sid, "tool_input": {"command": command}})


def run_tool(conf, tool, tool_input=None, sid="s1"):
    """Run pretool.py on one of the tools that never follow the mode."""
    return _run(conf, {"tool_name": tool, "session_id": sid, "tool_input": tool_input or {}})


def ran_instead(stdout):
    """The command the hook put in place of the caller's — None when it said nothing.

    None is the answer that matters here: silence means the caller's own command went to
    the shell untouched, on whichever machine that happens to be.
    """
    if not stdout.strip():
        return None
    return json.loads(stdout)["hookSpecificOutput"]["updatedInput"]["command"]


def context_of(stdout):
    """The additionalContext of a warning, or None if the hook said nothing."""
    if not stdout.strip():
        return None
    return json.loads(stdout)["hookSpecificOutput"].get("additionalContext")


# ── the state that used to pass for "local" ────────────────────────────────────


class TestAnEmptyRoutingFileIsNotLocal(unittest.TestCase):
    def test_the_command_is_not_run(self):
        with HookConf() as c:
            c.break_routing()
            _, out = run_bash(c, "ls -la")
            said = ran_instead(out)
            self.assertIsNotNone(said, "the hook said nothing — which means `ls` just ran, here, quietly")
            self.assertIn("NOT run", said)

    def test_whitespace_only_is_the_same_state(self):
        """A file holding a newline names no host either; `.strip()` is what reads it."""
        with HookConf() as c:
            c.write_routing("   \n\n")
            _, out = run_bash(c, "ls -la")
            self.assertIn("NOT run", ran_instead(out))

    def test_it_is_not_sent_to_a_host_either(self):
        """Refusal, not a guess in the other direction: nobody knows which machine was
        meant, so the command goes to neither."""
        with HookConf() as c:
            c.break_routing()
            _, out = run_bash(c, "rm -rf /var/log/*")
            said = ran_instead(out)
            self.assertNotIn("ssh ", said)
            self.assertNotIn("rm -rf", said)

    def test_the_caller_is_told_where_to_look_and_how_to_settle_it(self):
        with HookConf() as c:
            c.break_routing()
            _, out = run_bash(c, "ls -la")
            said = ran_instead(out)
            self.assertIn("target.s1", said)
            self.assertIn("@local", said)

    def test_no_sentinel_leaks_into_the_message(self):
        """The unreadable state is an object; formatted into a message it would arrive as
        `<object object at 0x…>`, which tells a reader nothing at all."""
        with HookConf() as c:
            c.break_routing()
            _, out = run_bash(c, "ls -la")
            self.assertNotIn("object at", ran_instead(out))

    def test_the_refusal_is_still_exit_zero(self):
        """Said through the JSON, like every other refusal in this hook."""
        with HookConf() as c:
            c.break_routing()
            code, _ = run_bash(c, "ls -la")
            self.assertEqual(code, 0)


# ── and the state that must stay silent ────────────────────────────────────────


class TestTheOrdinaryLocalSessionIsUntouched(unittest.TestCase):
    """The file is ABSENT in every session that never switched — far and away the common
    case. A word there would ride in front of every local command ever run."""

    def test_no_file_no_message(self):
        with HookConf() as c:
            code, out = run_bash(c, "ls -la")
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "")

    def test_a_routed_session_still_goes_over_ssh(self):
        with HookConf() as c:
            c.route_to("h1")
            _, out = run_bash(c, "ls -la")
            self.assertIn("root@10.0.0.1", ran_instead(out))


# ── @status may not answer what it has not read ────────────────────────────────


class TestStatusDoesNotClaimWhatItHasNotRead(unittest.TestCase):
    """The dangerous state is not "I do not know where I am" — that one is careful. It is
    the tool SAYING one thing while the truth is another."""

    def test_a_broken_state_answers_unknown(self):
        with HookConf() as c:
            c.break_routing()
            _, out = run_bash(c, "@status")
            said = ran_instead(out)
            self.assertIn("UNKNOWN", said)
            self.assertNotIn("LOCAL", said)  # the verdict it used to give, unearned

    def test_no_file_still_answers_local(self):
        with HookConf() as c:
            _, out = run_bash(c, "@status")
            self.assertIn("LOCAL", ran_instead(out))

    def test_a_routed_session_still_answers_remote(self):
        with HookConf() as c:
            c.route_to("h1")
            _, out = run_bash(c, "@status")
            self.assertIn("REMOTE → h1", ran_instead(out))


# ── the way out must survive the state it fixes ────────────────────────────────


class TestTheRemedyStillWorks(unittest.TestCase):
    """A refusal with no way past it is a wall. Both switches read the same file the
    refusal reads, so both had to be checked."""

    def test_local_clears_it(self):
        with HookConf() as c:
            c.break_routing()
            _, out = run_bash(c, "@local")
            self.assertIn("mode: LOCAL", ran_instead(out))
            self.assertFalse(c.exists("target.s1"))

    def test_local_does_not_print_the_sentinel(self):
        """`@local` answers with the mode and nothing else, so the sentinel has nowhere to
        appear today. Pinned anyway: the moment this branch grows a message that names the
        routing it left — as it should — that message would be the first place an object
        could be rendered into text a caller reads."""
        with HookConf() as c:
            c.break_routing()
            _, out = run_bash(c, "@local")
            self.assertNotIn("object at", ran_instead(out))

    def test_a_switch_routes_again_and_the_next_command_leaves(self):
        with HookConf() as c:
            c.break_routing()
            run_bash(c, "@h1")
            _, out = run_bash(c, "ls")
            self.assertIn("root@10.0.0.1", ran_instead(out))


# ── the write that could leave a blank behind ──────────────────────────────────


class TestTheSwitchReplacesInsteadOfTruncating(unittest.TestCase):
    """The window itself cannot be opened on demand — a torn write wants the machine to
    die between two syscalls. What CAN be pinned is that the window is gone: writing in
    place keeps the file's inode, os.replace brings a new one. If the switch ever goes
    back to `open(…, "w")`, the inode stops changing and this test says so."""

    def _inode(self, c, sid="s1"):
        return os.stat(c.path("target." + sid)).st_ino

    def test_the_file_is_replaced_not_rewritten_in_place(self):
        with HookConf() as c:
            run_bash(c, "@h1")
            before = self._inode(c)
            run_bash(c, "@h1")
            self.assertNotEqual(before, self._inode(c))

    def test_the_alias_still_lands_in_the_file(self):
        with HookConf() as c:
            run_bash(c, "@h1")
            with open(c.path("target.s1")) as f:
                self.assertEqual(f.read().strip(), "h1")

    def test_it_leaves_no_temporary_behind(self):
        """The temp file lives in the SAME directory (os.replace cannot cross a
        filesystem), which is the directory the hook reads its session state from."""
        with HookConf() as c:
            run_bash(c, "@h1")
            leftovers = [n for n in os.listdir(c.dir) if n.startswith(".target.")]
            self.assertEqual(leftovers, [])

    def test_the_switch_still_announces_itself(self):
        with HookConf() as c:
            _, out = run_bash(c, "@h1")
            self.assertIn("REMOTE → h1", ran_instead(out))


# ── the neighbouring failure, which was never this one ─────────────────────────


class TestAHalfWrittenAliasKeepsItsOwnPath(unittest.TestCase):
    """A torn write can also leave a PIECE of an alias, and that is a different state: it
    names a host, just not one that exists. Its refusal is older than this fix, and this
    pins that the new branch did not swallow it."""

    def test_it_is_refused_as_unresolvable(self):
        with HookConf() as c:
            c.write_routing("h")  # `h1`, torn after the first character
            _, out = run_bash(c, "ls")
            said = ran_instead(out)
            self.assertIn("cannot resolve @h", said)
            self.assertNotIn("names no host", said)


# ── the other door: the tools that never follow the mode ───────────────────────


class TestTheOffModeToolsDoNotClaimAModeEither(unittest.TestCase):
    """Read/Grep/… work on the local disk whatever the mode is; the warning exists so
    their output is not read as facts about a far machine. With the mode unreadable there
    is no host to name — and naming one would be worse than saying nothing, while saying
    nothing is the same silence this warning was written to end."""

    def test_a_file_tool_says_the_mode_cannot_be_read(self):
        with HookConf() as c:
            c.break_routing()
            _, out = run_tool(c, "Read", {"file_path": "/etc/hosts"})
            self.assertIn("routing state is unreadable", context_of(out))

    def test_it_invents_no_host(self):
        with HookConf() as c:
            c.break_routing()
            _, out = run_tool(c, "Grep", {"pattern": "TODO"})
            said = context_of(out)
            self.assertNotIn("object at", said)
            self.assertNotIn("you are on @", said)

    def test_it_is_said_once_per_session(self):
        """The same budget the host warning is kept on: Grep is called often enough that a
        line per call would become wallpaper, and wallpaper is silent exactly when it needs
        to speak."""
        with HookConf() as c:
            c.break_routing()
            _, first = run_tool(c, "Grep", {"pattern": "a"})
            _, second = run_tool(c, "Read", {"file_path": "/a"})
            self.assertIsNotNone(context_of(first))
            self.assertIsNone(context_of(second))

    def test_a_local_session_still_says_nothing(self):
        with HookConf() as c:
            code, out = run_tool(c, "Read", {"file_path": "/etc/hosts"})
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "")

    def test_a_routed_session_still_names_its_host(self):
        with HookConf() as c:
            c.route_to("h1")
            _, out = run_tool(c, "Read", {"file_path": "/etc/hosts"})
            self.assertIn("you are on @h1", context_of(out))


# ── the spawn that inherits a state nobody can read ────────────────────────────


class TestASpawnedAgentIsWarnedIntoTheUnreadableState(unittest.TestCase):
    """The tools that ignore the mode share ONE once-per-session budget here, and `Agent`
    used to draw on it: a single Grep spent it, and every agent spawned afterwards was born
    into silence. In the READABLE case Agent has always warned on every spawn — and the
    unreadable state is strictly worse than a wrong disk, since the hook refuses the
    agent's bash outright while it lasts."""

    def test_it_is_warned_after_a_file_tool_spent_the_budget(self):
        with HookConf() as c:
            c.break_routing()
            run_tool(c, "Grep", {"pattern": "a"})  # spends the shared budget
            _, out = run_tool(c, "Agent", {"prompt": "go"})
            said = context_of(out)
            self.assertIsNotNone(said, "the spawn was silent — the budget had been spent")
            self.assertIn("INHERITS", said)

    def test_every_spawn_is_warned(self):
        with HookConf() as c:
            c.break_routing()
            run_tool(c, "Agent", {"prompt": "one"})
            _, out = run_tool(c, "Agent", {"prompt": "two"})
            self.assertIsNotNone(context_of(out))

    def test_it_names_no_host_it_has_not_read(self):
        with HookConf() as c:
            c.break_routing()
            _, out = run_tool(c, "Agent", {"prompt": "go"})
            said = context_of(out)
            self.assertNotIn("object at", said)
            self.assertNotIn("you are on @", said)

    def test_a_local_session_still_spawns_in_silence(self):
        with HookConf() as c:
            code, out = run_tool(c, "Agent", {"prompt": "go"})
            self.assertEqual(code, 0)
            self.assertIsNone(context_of(out))


# ── a switch that could not be written may not pass for one ────────────────────


class SealedConf(HookConf):
    """A HookConf whose DIRECTORY cannot be written to — and with a SECOND host in it.

    The measured shape of the failure: an existing routing file stays readable and even
    writable; only NEW files cannot be created, which is exactly what the atomic writer
    needs (mkstemp puts its temp file beside the target). Two hosts, because the message
    has to tell the alias that FAILED from the one the session stayed on, and one host
    cannot show that. The permissions go back on the way out, or the temp tree could not
    be removed.
    """

    def __enter__(self):
        super().__enter__()
        with open(self.path("shunt.toml"), "w") as f:
            f.write('[hosts]\nh1 = "root@10.0.0.1"\nh2 = "root@10.0.0.2"\n')
        return self

    def seal(self):
        os.chmod(self.dir, 0o500)

    def __exit__(self, *a):
        os.chmod(self.dir, 0o700)
        return super().__exit__(*a)


class TestAFailedSwitchIsSaidOutLoud(unittest.TestCase):
    """The switch used to `sys.exit(0)` on any write failure: no message, no rewrite. The
    caller reads nothing, believes they are on @alias, and their next command runs where
    they were before. Making the write ATOMIC widened that case instead of closing it —
    mkstemp needs the DIRECTORY writable, where `open(…, "w")` needed only the file."""

    def _said(self, out):
        """What the hook put in place of the switch — silence fails HERE, by name.

        Without this the silence reaches assertIn as a None and the run reports a broken
        test, when what it caught is the defect itself.
        """
        said = ran_instead(out)
        self.assertIsNotNone(said, "the hook said nothing — the switch failed in silence")
        return said

    def test_the_failure_is_said(self):
        with SealedConf() as c:
            c.seal()
            _, out = run_bash(c, "@h1")
            self.assertIn("FAILED", self._said(out))

    def test_it_does_not_announce_a_switch_that_did_not_happen(self):
        with SealedConf() as c:
            c.seal()
            _, out = run_bash(c, "@h1")
            self.assertNotIn("mode: REMOTE", self._said(out))

    def test_it_names_the_routing_that_is_still_in_force(self):
        with SealedConf() as c:
            c.route_to("h1")
            c.seal()
            _, out = run_bash(c, "@h2")
            said = self._said(out)
            self.assertIn("@h2", said)  # the switch that failed
            self.assertIn("h1", said)  # where the session actually stayed

    def test_the_session_really_did_stay_where_it_was(self):
        """A message is worth what the state behind it is: the next command must still
        leave for the OLD host, untouched by the switch that failed."""
        with SealedConf() as c:
            c.route_to("h1")
            c.seal()
            run_bash(c, "@h2")
            _, out = run_bash(c, "ls")
            self.assertIn("root@10.0.0.1", ran_instead(out))

    def test_a_local_session_is_told_it_is_still_local(self):
        with SealedConf() as c:
            c.seal()
            _, out = run_bash(c, "@h1")
            self.assertIn("LOCAL", self._said(out))

    def test_no_sentinel_leaks_when_the_previous_state_was_unreadable(self):
        with SealedConf() as c:
            c.break_routing()
            c.seal()
            _, out = run_bash(c, "@h1")
            self.assertNotIn("object at", self._said(out))

    def test_the_refusal_is_still_exit_zero(self):
        with SealedConf() as c:
            c.seal()
            code, _ = run_bash(c, "@h1")
            self.assertEqual(code, 0)


class TestTheConfigDirectoryFailsLikeTheWrite(unittest.TestCase):
    """`os.makedirs(CONF)` stood OUTSIDE the guard the write below it sits in.

    Its failure was therefore not an event but a traceback — and a hook that raises is a
    non-blocking error to the harness, which then runs the ORIGINAL line: `@web-01` went
    to bash as a command while nobody was told the switch had not happened. Same event as
    a routing file that cannot be written, one honest message and one traceback.

    Two departures from this file's method, both named rather than left to be noticed:

    · the window is a RACE, not an open path. resolve_host() has just read
      CONF/shunt.toml, so the directory IS there when makedirs runs; it fails only if
      something takes it away in between (a parallel session's cleanup, a tmp reaper).
      There is no filesystem SHAPE that reaches it — which is why the failure is
      injected rather than arranged.
    · so this one test runs the hook IN-PROCESS. Everything else here spawns it as the
      harness does, and should; an injected failure cannot cross that boundary.
    """

    def _switch_with_no_config_dir(self, conf_dir, previous=None):
        """`@h1` while the config directory cannot be made. Returns what the hook said."""
        if previous:
            with open(os.path.join(conf_dir, "target.s1"), "w") as f:
                f.write(previous)
        payload = {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": "@h1"}}
        out = io.StringIO()
        with (
            patch.object(pretool, "CONF", conf_dir),
            patch.object(pretool.os, "makedirs", side_effect=PermissionError(13, "Permission denied")),
            patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
            patch.object(sys, "stdout", out),
        ):
            with self.assertRaises(SystemExit):
                pretool.main()
        return ran_instead(out.getvalue())

    def test_it_is_said_instead_of_raised(self):
        with HookConf() as c:
            said = self._switch_with_no_config_dir(c.dir)
            self.assertIsNotNone(said, "the hook said nothing about the failed switch")
            self.assertIn("FAILED", said)

    def test_it_does_not_announce_a_switch_that_did_not_happen(self):
        with HookConf() as c:
            self.assertNotIn("mode: REMOTE", self._switch_with_no_config_dir(c.dir))

    def test_it_names_the_routing_still_in_force(self):
        with HookConf() as c:
            said = self._switch_with_no_config_dir(c.dir, previous="h1")
            self.assertIn("h1", said)

    def test_a_local_session_is_told_it_is_still_local(self):
        with HookConf() as c:
            self.assertIn("LOCAL", self._switch_with_no_config_dir(c.dir))

    def test_the_reason_reaches_the_reader(self):
        with HookConf() as c:
            self.assertIn("Permission denied", self._switch_with_no_config_dir(c.dir))


# ── the way back must survive a DIRECTORY in the routing file's place ──────────


class TestLocalSurvivesADirectoryInItsPlace(unittest.TestCase):
    """A directory named `target.<sid>` reads as the unreadable state, so every bash
    command is refused — and `@local`, the one remedy for that state, could not win:
    os.remove can never take a directory. The session had no way out from inside itself."""

    def _put_a_directory_there(self, c, full=False):
        os.mkdir(c.path("target.s1"))
        if full:
            open(os.path.join(c.path("target.s1"), "keep"), "w").close()

    def test_it_reads_as_the_unreadable_state(self):
        """The premise of the trap, pinned: bash is refused, so `@local` is the only move
        left — which is why it has to work."""
        with HookConf() as c:
            self._put_a_directory_there(c)
            _, out = run_bash(c, "ls")
            self.assertIn("NOT run", ran_instead(out))

    def test_an_empty_one_is_cleared_and_the_session_is_local_again(self):
        with HookConf() as c:
            self._put_a_directory_there(c)
            _, out = run_bash(c, "@local")
            self.assertIn("mode: LOCAL", ran_instead(out))
            self.assertFalse(c.exists("target.s1"))

    def test_a_full_one_is_reported_with_the_path_to_remove_by_hand(self):
        """rmdir cannot take this one either, and that is the end of what the hook can do
        from inside — so the message has to carry the path, or the way out is guesswork."""
        with HookConf() as c:
            self._put_a_directory_there(c, full=True)
            _, out = run_bash(c, "@local")
            said = ran_instead(out)
            self.assertIn("FAILED", said)
            self.assertIn("target.s1", said)

    def test_it_claims_no_machine_it_has_not_read(self):
        """The old text said "Session is STILL REMOTE … runs THERE" here as well — while
        the routing is unreadable and bash is refused, not sent anywhere at all."""
        with HookConf() as c:
            self._put_a_directory_there(c, full=True)
            _, out = run_bash(c, "@local")
            said = ran_instead(out)
            self.assertNotIn("STILL REMOTE", said)
            self.assertNotIn("runs THERE", said)
            self.assertNotIn("object at", said)


if __name__ == "__main__":
    unittest.main()
