"""
Tests for shunt.pretool - where the far side remembers this session's directory.

The design in one line: the state file is a path for the REMOTE shell, not for os.path.
That is the whole reason this file exists. `$HOME` names the account we LAND IN over there,
which is routinely not the one running the hook (local user -> remote root), so the path is
assembled by that shell and never by python. Every way of getting this wrong fails in
SILENCE - a state file written somewhere nobody reads, or not written at all, looks exactly
like a session that simply starts in the login directory.

Coverage:
  - the state lives under $HOME/.cache/shunt, and no longer in world-writable /tmp
  - `$HOME` is left for the far shell, DOUBLE-quoted - no local home baked into a remote
    path, and no single quotes (which would send it over as five literal characters)
  - the directory is created before anything writes the file, at mode 700; reading needs
    no directory at all
  - a session id from outside cannot break out of the path into a command
  - a home directory with a space in it survives
  - END TO END: run the payload twice and the second run starts where the first ended
  - an unwritable home costs the memory, not the command: the group is SILENT on every
    ordinary command (a line there would be wallpaper in the caller's own stderr) and the
    exit code is the command's own
  - the once-per-switch housekeeping: the sweep of dead sessions' files and the ONE probe
    that says the memory is lost - both only on the first command after `@alias`
  - the marker's whole life through the HOOK: `@alias` arms it, the first command spends
    it, `@local` clears it
  - a marker that CANNOT be spent (a config dir that cannot delete): the reminder keeps
    speaking, the housekeeping does not ride, and the failure is said with path + reason
    on every command - the two riders of one marker, told apart
  - the ControlMaster socket deliberately did NOT move (a local path, ~104 chars to live in)
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

# Make the src tree importable when run without installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shunt import pretool

# The hook as a file: it runs in its own process, so it is invoked by path.
PRETOOL = pretool.__file__
HOST = {"alias": "h", "target": "root@203.0.113.1", "key": None}


class FakeHome:
    """Context manager: an empty directory to play the far side's $HOME."""

    def __init__(self, leaf=""):
        self.leaf = leaf

    def __enter__(self):
        self.root = tempfile.mkdtemp(prefix="shunt-test-cwd-")
        self.home = os.path.join(self.root, self.leaf) if self.leaf else self.root
        os.makedirs(self.home, exist_ok=True)
        return self

    def __exit__(self, *_):
        os.chmod(self.home, 0o700)  # a read-only-home test must still clean up
        shutil.rmtree(self.root, ignore_errors=True)

    def dir(self):
        return os.path.join(self.home, ".cache", "shunt")

    def state(self, sid):
        return os.path.join(self.dir(), "cwd-" + sid)

    def run(self, cmd, sid, housekeeping=False):
        """Execute the payload the way the far shell would, with our $HOME."""
        return subprocess.run(
            ["bash", "-c", pretool._remote_script(cmd, sid, housekeeping)],
            env={"HOME": self.home, "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            capture_output=True,
            text=True,
        )


# -- the path itself ------------------------------------------------------------


class TestThePath(unittest.TestCase):
    """The three invariants are asserted on the SSH COMMAND, not only on the payload:
    that string is what leaves this machine, and the outer quoting is a second chance to
    get `$HOME` wrong."""

    def _both(self, sid="sess-1"):
        """The payload and the full ssh command around it - both must hold."""
        return (pretool._remote_script("ls", sid), pretool.ssh_command(HOST, "ls", sid))

    def test_the_state_moved_out_of_tmp(self):
        for text in self._both():
            self.assertNotIn("/tmp/shunt-cwd", text)
            self.assertIn("$HOME", text)
            self.assertIn(".cache/shunt", text)

    def test_home_is_double_quoted_for_the_far_shell(self):
        """Quoted so a home with a space stays one word; expanded THERE, not here."""
        for text in self._both():
            self.assertIn('"$HOME"', text)

    def test_home_is_not_single_quoted(self):
        """`shlex.quote` on the whole path buys exactly this failure: the far shell would
        take `$HOME` as five literal characters and make a directory by that name wherever
        the command happened to land."""
        for text in self._both():
            self.assertNotIn("'$HOME'", text)

    def test_no_local_home_is_baked_into_a_remote_path(self):
        """It would point at a directory that is not on the far machine - and be swallowed
        whole, since the write is silenced: the session would forget every `cd` and say
        nothing about it."""
        for text in self._both():
            self.assertNotIn(os.path.expanduser("~"), text)

    def test_the_script_is_valid_shell(self):
        for housekeeping in (False, True):
            script = pretool._remote_script("echo hi", "sess-1", housekeeping)
            check = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)

    def test_ssh_command_still_carries_the_script(self):
        """The payload is built in one place; ssh_command only wraps it.

        Asserted so the extraction cannot drift into a second, stale copy - and for both
        shapes, since the switch shape is the one with something to forget.
        """
        import shlex

        for housekeeping in (False, True):
            cmd = pretool.ssh_command(HOST, "ls", "sess-1", housekeeping)
            self.assertIn(shlex.quote(pretool._remote_script("ls", "sess-1", housekeeping)), cmd)


# -- what the far shell actually does with it -----------------------------------


class TestOnTheFarSide(unittest.TestCase):
    def test_the_directory_is_created(self):
        with FakeHome() as h:
            h.run("true", "sess-mk")
            self.assertTrue(os.path.isdir(h.dir()))

    def test_the_directory_is_private(self):
        """The file is a trail of where someone works - world-readable in /tmp was half
        the reason it moved."""
        with FakeHome() as h:
            h.run("true", "sess-mode")
            self.assertEqual(os.stat(h.dir()).st_mode & 0o777, 0o700)

    def test_an_existing_directory_keeps_its_permissions(self):
        """`-m 700` applies at creation. We do not re-permission someone's home."""
        with FakeHome() as h:
            os.makedirs(h.dir())
            os.chmod(h.dir(), 0o755)
            h.run("true", "sess-keep")
            self.assertEqual(os.stat(h.dir()).st_mode & 0o777, 0o755)

    def test_the_cwd_is_remembered_between_commands(self):
        with FakeHome() as h:
            h.run("cd /usr", "sess-e2e")
            with open(h.state("sess-e2e")) as f:
                self.assertEqual(f.read().strip(), "/usr")
            second = h.run("pwd", "sess-e2e")
            self.assertEqual(second.stdout.strip(), "/usr")

    def test_the_first_command_starts_at_home(self):
        """No state yet: `cat` fails, the fallback answers, and nothing is said about it."""
        with FakeHome() as h:
            first = h.run("pwd", "sess-first")
            self.assertEqual(first.stdout.strip(), os.path.realpath(h.home))
            self.assertEqual(first.stderr, "")

    def test_reading_needs_no_directory(self):
        """The mkdir rides with the WRITE. Reading a state file that is not there is the
        ordinary first command, and it must cost nothing and say nothing."""
        with FakeHome() as h:
            out = h.run("echo ok", "sess-read")
            self.assertEqual(out.stdout.strip(), "ok")
            self.assertEqual(out.stderr, "")

    def test_a_home_with_a_space_survives(self):
        """`$HOME` is quoted at every use. A split here would put the state somewhere
        nobody looks - and nobody would hear about it."""
        with FakeHome("ho me") as h:
            h.run("cd /usr", "sess-space")
            self.assertTrue(os.path.exists(h.state("sess-space")))

    def test_the_exit_code_survives_the_trap(self):
        with FakeHome() as h:
            self.assertEqual(h.run("exit 7", "sess-rc").returncode, 7)

    def test_the_exit_code_survives_the_switch_housekeeping_too(self):
        """The sweep and the probe run BEFORE the command; neither may become its code."""
        with FakeHome() as h:
            self.assertEqual(h.run("exit 7", "sess-rc2", housekeeping=True).returncode, 7)


# -- the restore has to say when it cannot land ---------------------------------


class TestTheRestoreSpeaksWhenItCannotLand(unittest.TestCase):
    """`cd REMEMBERED || cd ~` is the textbook shape of a cd whose failure nobody looks
    at, and on the next line an arbitrary command - the caller's own, which assumes it is
    where it left off.

    A directory removed on the far side between two commands (a release swapped, a build
    tree cleaned) would move the next command to $HOME without a word: `rm -rf ./*` meant
    for /srv/old-release would then run in a home directory. Said, never refused: the
    session must stay usable when its remembered directory is swept.

    The message says CANNOT BE ENTERED and offers two causes, because that is all that
    was verified: cd fails the same way on a directory that is still there but has
    become unreachable - permissions changed, a mount fell away. "is gone" would name a
    cause nobody checked and send the reader looking in the wrong place.
    """

    def _gone(self, h, sid="sess-gone"):
        """Point the state file at a directory that does not exist."""
        os.makedirs(h.dir(), exist_ok=True)
        with open(h.state(sid), "w") as f:
            f.write(os.path.join(h.home, "release-42-removed") + "\n")

    def test_a_vanished_directory_is_reported(self):
        with FakeHome() as h:
            self._gone(h)
            out = h.run("pwd", "sess-gone")
            self.assertIn("shunt:", out.stderr)

    def test_the_report_names_the_directory(self):
        """A report that cannot say WHICH directory is gone leaves the reader guessing."""
        with FakeHome() as h:
            self._gone(h)
            out = h.run("pwd", "sess-gone")
            self.assertIn("release-42-removed", out.stderr)

    def test_the_command_still_runs(self):
        """Warn, do not block - the far side has no way to ask, and a session whose cache
        was swept must not stop working."""
        with FakeHome() as h:
            self._gone(h)
            out = h.run("echo alive", "sess-gone")
            self.assertEqual(out.stdout.strip(), "alive")

    def test_where_it_ran_is_where_the_message_said(self):
        with FakeHome() as h:
            self._gone(h)
            out = h.run("pwd", "sess-gone")
            self.assertEqual(out.stdout.strip(), os.path.realpath(h.home))

    def test_the_exit_code_is_still_the_command_s_own(self):
        with FakeHome() as h:
            self._gone(h)
            self.assertEqual(h.run("exit 7", "sess-gone").returncode, 7)

    def test_a_directory_that_is_there_says_nothing(self):
        """The whole budget of this message: it fires on a real failure only. A line that
        is always there stops being read."""
        with FakeHome() as h:
            h.run("cd /usr", "sess-fine")
            second = h.run("pwd", "sess-fine")
            self.assertEqual(second.stderr, "")
            self.assertEqual(second.stdout.strip(), "/usr")

    def test_a_directory_that_is_there_but_unreachable_is_reported_too(self):
        """The case the wording exists for: it still EXISTS, cd still fails. A message
        that said "is gone" would send the reader to look for a deleted directory."""
        with FakeHome() as h:
            walled = os.path.join(h.home, "walled-off")
            os.makedirs(walled)
            os.makedirs(h.dir(), exist_ok=True)
            with open(h.state("sess-walled"), "w") as f:
                f.write(walled + "\n")
            os.chmod(walled, 0o000)
            try:
                out = h.run("pwd", "sess-walled")
            finally:
                os.chmod(walled, 0o700)  # or the temp tree cannot be removed
            self.assertIn("walled-off", out.stderr)
            self.assertIn("not accessible", out.stderr)
            self.assertTrue(os.path.isdir(walled))  # it was there the whole time
            self.assertEqual(out.stdout.strip(), os.path.realpath(h.home))

    def test_the_message_does_not_claim_a_cause_it_did_not_check(self):
        """Only the cd's failure is verified; which of the two reasons it was, is not."""
        with FakeHome() as h:
            self._gone(h)
            self.assertNotIn("is gone", h.run("pwd", "sess-gone").stderr)

    def test_the_helper_variable_does_not_leak_into_the_command(self):
        """What follows is the caller's shell; it is not ours to leave things in."""
        with FakeHome() as h:
            out = h.run("echo [${__shunt_cwd-unset}]", "sess-leak")
            self.assertIn("[unset]", out.stdout)


# -- the id comes from outside --------------------------------------------------


class TestHostileSessionId(unittest.TestCase):
    """`session_id` arrives in the hook's stdin JSON; it is quoted for that reason."""

    def test_a_session_id_cannot_run_a_command(self):
        with FakeHome() as h:
            out = h.run("true", "a$(echo OWNED)b")
            self.assertNotIn("OWNED", out.stdout + out.stderr)

    def test_a_session_id_stays_one_file_name(self):
        with FakeHome() as h:
            h.run("cd /usr", "a'; echo OWNED; '")
            self.assertEqual(os.listdir(h.dir()), ["cwd-a'; echo OWNED; '"])

    def test_a_hostile_id_survives_the_switch_shape_as_well(self):
        """The switch shape puts the same path through two more places."""
        with FakeHome() as h:
            out = h.run("true", "a$(echo OWNED)b", housekeeping=True)
            self.assertNotIn("OWNED", out.stdout + out.stderr)


# -- the new failure the move brings with it ------------------------------------


class TestUnwritableHome(unittest.TestCase):
    """/tmp is always there; `~/.cache/shunt` has to be made, and making it can fail.

    Two things must hold at once, and they pull in opposite directions: the loss must be
    KNOWABLE, and it must not put a line in front of every command the caller runs. So the
    write is silent, and the switch pays for one probe.
    """

    def setUp(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores the permissions this test rests on")

    def test_the_command_still_runs_and_keeps_its_code(self):
        with FakeHome() as h:
            os.chmod(h.home, 0o500)
            out = h.run("echo alive; exit 3", "sess-ro")
            self.assertIn("alive", out.stdout)
            self.assertEqual(out.returncode, 3)

    def test_the_ordinary_command_stays_silent(self):
        """The whole point of the braces: `pwd > FILE 2>/dev/null` would let the SHELL's
        "No such file or directory" through, into the stderr of the caller's own command,
        on every single command while the home is broken."""
        with FakeHome() as h:
            os.chmod(h.home, 0o500)
            out = h.run("echo alive", "sess-ro")
            self.assertEqual(out.stderr, "")

    def test_the_switch_says_the_memory_is_lost(self):
        """Once per `@alias` - the moment the session is orienting anyway."""
        with FakeHome() as h:
            os.chmod(h.home, 0o500)
            out = h.run("true", "sess-ro", housekeeping=True)
            self.assertIn("will not remember", out.stderr)
            self.assertIn("shunt", out.stderr)

    def test_the_probe_is_silent_on_a_healthy_home(self):
        """A line on the way in, every switch, would be the wallpaper we are avoiding."""
        with FakeHome() as h:
            out = h.run("echo ok", "sess-ok", housekeeping=True)
            self.assertEqual(out.stderr, "")
            self.assertEqual(out.stdout.strip(), "ok")

    def test_the_switch_command_still_runs_and_keeps_its_code(self):
        with FakeHome() as h:
            os.chmod(h.home, 0o500)
            out = h.run("echo alive; exit 3", "sess-ro", housekeeping=True)
            self.assertIn("alive", out.stdout)
            self.assertEqual(out.returncode, 3)


# -- the sweep: once per switch, never per command ------------------------------


class TestTheSweep(unittest.TestCase):
    """A session id is born and never dies. Without a sweep the directory grows one file
    per session forever; with one on every command it is a `find` over someone's disk,
    several times a minute, to delete nothing."""

    def _age(self, path, days):
        old = time.time() - days * 86400
        os.utime(path, (old, old))

    def test_the_sweep_rides_only_on_the_switch(self):
        self.assertNotIn("-delete", pretool._remote_script("ls", "sess-1"))
        self.assertIn("-delete", pretool._remote_script("ls", "sess-1", housekeeping=True))

    def test_it_drops_the_dead_and_keeps_the_living(self):
        with FakeHome() as h:
            os.makedirs(h.dir())
            for name in ("cwd-dead", "cwd-alive"):
                open(os.path.join(h.dir(), name), "w").close()
            self._age(os.path.join(h.dir(), "cwd-dead"), 40)
            h.run("true", "sess-sweep", housekeeping=True)
            left = sorted(os.listdir(h.dir()))
            self.assertIn("cwd-alive", left)
            self.assertNotIn("cwd-dead", left)

    def test_it_touches_nothing_that_is_not_ours(self):
        """`-maxdepth 1 -name 'cwd-*'` - a cache directory is a shared place."""
        with FakeHome() as h:
            os.makedirs(h.dir())
            stranger = os.path.join(h.dir(), "notes.txt")
            open(stranger, "w").close()
            self._age(stranger, 400)
            h.run("true", "sess-sweep2", housekeeping=True)
            self.assertTrue(os.path.exists(stranger))

    def test_our_own_cwd_is_read_before_the_sweep(self):
        """Ordering, pinned: the restore reads the file, THEN the sweep runs. A session
        idle for over a month still lands where it left off, and its file comes back
        fresh - the reverse order would send it home without a word."""
        with FakeHome() as h:
            os.makedirs(h.dir())
            with open(h.state("sess-old"), "w") as f:
                f.write("/usr\n")
            self._age(h.state("sess-old"), 40)
            out = h.run("pwd", "sess-old", housekeeping=True)
            self.assertEqual(out.stdout.strip(), "/usr")
            self.assertTrue(os.path.exists(h.state("sess-old")))


# -- the one-shot marker, driven through the hook -------------------------------


class HookConf:
    """A temp SHUNT_CONF with one host, plus the session files the hook reads.

    The stub `ssh` rides along because `@alias` probes the host it switches to; no test
    here is about a live machine, and none may knock on one.
    """

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-cwd-conf-")
        with open(os.path.join(self.dir, "shunt.toml"), "w") as f:
            f.write('[hosts]\nh1 = "root@203.0.113.1"\n')
        self.bin = os.path.join(self.dir, "_bin")
        os.makedirs(self.bin)
        with open(os.path.join(self.bin, "ssh"), "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(os.path.join(self.bin, "ssh"), 0o755)
        return self

    def __exit__(self, *_):
        os.chmod(self.dir, 0o700)  # a frozen-dir test must still be able to clean up
        shutil.rmtree(self.dir, ignore_errors=True)

    def exists(self, name):
        return os.path.exists(os.path.join(self.dir, name))

    def route_to(self, alias, sid="s1"):
        with open(os.path.join(self.dir, "target." + sid), "w") as f:
            f.write(alias)

    def arm_switch(self, alias, sid="s1"):
        """What `@alias` leaves behind: the one-shot marker the next command spends."""
        with open(os.path.join(self.dir, "switched." + sid), "w") as f:
            f.write(alias)

    def freeze(self):
        """A config dir nothing can be added to or removed from - a broken disk, in one call.

        The files already in it stay READABLE, which is the state that matters here: the
        marker can still be read and can no longer be spent, exactly as a full or
        read-only filesystem leaves it.
        """
        os.chmod(self.dir, 0o500)


def run_hook(conf, command, sid="s1"):
    """Run pretool.py exactly as the harness does. Returns the parsed hook output.

    PYTHONPATH is stripped for the same reason tests/test_pretool_warnings.py strips it:
    settings.json names pretool.py by absolute path, so the field never has the package
    on sys.path, and the test runner's PYTHONPATH would hide that.
    """
    payload = {"tool_name": "Bash", "session_id": sid, "tool_input": {"command": command}}
    env = dict(os.environ, SHUNT_CONF=conf.dir, PATH=conf.bin + os.pathsep + os.environ["PATH"])
    env.pop("PYTHONPATH", None)
    r = subprocess.run([sys.executable, PRETOOL], input=json.dumps(payload).encode(), capture_output=True, env=env)
    out = r.stdout.decode().strip()
    return json.loads(out)["hookSpecificOutput"] if out else {}


class TestTheSwitchMarkerLifecycle(unittest.TestCase):
    """`switched.<sid>` from birth to death, through main() and nothing hand-placed.

    Everything above drives the far-side script directly, with `housekeeping` passed in as an
    argument - which proves what the marker BUYS and never that the hook writes or removes
    one. If `@alias` stopped arming it, the far side's housekeeping would go unpaid forever
    and every other test in this file would still be green.
    """

    def test_a_switch_arms_the_marker(self):
        with HookConf() as c:
            run_hook(c, "@h1")
            self.assertTrue(c.exists("switched.s1"))

    # The spend of the marker is driven ONE place only - see
    # TestTheHookDoesNotWarnAboutItself.test_the_sweep_is_spent_with_the_switch_marker,
    # where the note and the sweep are shown to be spent together. Two drivers for one
    # fact in one file is knowledge kept twice.

    def test_going_local_takes_the_marker_off_the_host(self):
        """`@local` used to REMOVE the marker; it now re-points it at the local side.

        What must not survive is the HOST's ticket: left naming @h1, it would be spent by
        the first command of the next switch - housekeeping on a host that never armed it,
        and none on the one that did. That is unchanged. What is new is that the way home
        arms a ticket of its own (see tests/test_pretool_local_ticket.py), so the marker
        exists and no longer names a machine.
        """
        with HookConf() as c:
            run_hook(c, "@h1")
            run_hook(c, "@local")
            with open(os.path.join(c.dir, "switched.s1")) as f:
                self.assertEqual(f.read().strip(), pretool.LOCAL_MARK)
            self.assertNotIn("h1", pretool.LOCAL_MARK)


# -- the hook may not warn about a command the caller never wrote ---------------


class TestTheHookDoesNotWarnAboutItself(unittest.TestCase):
    """The sweep is a `find ... -delete` - a shape the hook's own irreversible-check reports.

    It must never fire on it: the check reads the command the CALLER typed, never the
    rewrite. A warning about a command nobody wrote is how a reader learns to skip the
    lines that matter.
    """

    def test_a_harmless_command_after_a_switch_is_not_called_irreversible(self):
        with HookConf() as c:
            c.route_to("h1")
            c.arm_switch("h1")
            out = run_hook(c, "ls -la")
            self.assertIn("-delete", out["updatedInput"]["command"])  # the sweep rode
            context = out.get("additionalContext", "")
            self.assertIn("first command since", context)  # the switch spoke
            self.assertNotIn("cannot be taken back", context)

    def test_the_check_is_still_alive(self):
        """So the test above cannot pass by the warning being broken altogether."""
        with HookConf() as c:
            c.route_to("h1")
            c.arm_switch("h1")
            out = run_hook(c, "rm -rf /var/log/old")
            self.assertIn("cannot be taken back", out.get("additionalContext", ""))

    def test_the_sweep_is_spent_with_the_switch_marker(self):
        """One question, two riders: the note and the sweep ride the same one-shot fact,
        and the second command must carry neither. The marker itself is gone with them -
        this is the one place the spend is driven (see TestTheSwitchMarkerLifecycle)."""
        with HookConf() as c:
            c.route_to("h1")
            c.arm_switch("h1")
            first = run_hook(c, "ls")
            self.assertIn("-delete", first["updatedInput"]["command"])
            out = run_hook(c, "ls")
            self.assertNotIn("-delete", out["updatedInput"]["command"])
            self.assertEqual(out.get("additionalContext"), None)
            self.assertFalse(c.exists("switched.s1"))


# -- when the ticket cannot be punched ------------------------------------------


class TestAMarkerThatCannotBeSpent(unittest.TestCase):
    """The ticket is read but cannot be punched - a config dir that cannot delete.

    Both riders used to sit on ONE boolean taken from the READ, with the removal's
    failure thrown away. So a broken config dir meant the marker never went away and
    every command from then on was treated as the first after a switch: a `find ... -delete`
    over someone ELSE's disk, several times a minute, and nobody told.

    They are told apart now, and each gets what it is owed:
      - the reminder REPEATS - the moment it guards ("I forgot I had switched") lasts
        exactly as long as the marker stands, so the repetition is true, not wallpaper;
      - the housekeeping does NOT run - it is bought by a spent ticket only;
      - the failure is SAID, with the path and the reason, on every command. There is no
        once-per-X budget for it on purpose: every such budget is a file in the very
        directory that is broken.
    """

    def setUp(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores the permissions this test rests on")

    def _stuck(self, c):
        """A session on @h1 whose armed marker can be read and cannot be removed."""
        c.route_to("h1")
        c.arm_switch("h1")
        c.freeze()

    def test_the_command_still_leaves_for_the_host(self):
        """First and loudest: a broken config dir may not cost the rewrite. An
        unrewritten command runs HERE, on the machine the caller believes they left."""
        with HookConf() as c:
            self._stuck(c)
            out = run_hook(c, "ls -la")
            self.assertIn("ssh ", out["updatedInput"]["command"])
            self.assertIn("root@203.0.113.1", out["updatedInput"]["command"])

    def test_the_housekeeping_does_not_ride(self):
        """The sweep is bought by a SPENT ticket. On a marker that stands it would ride
        on this command, and on every command after it - a `find` over a foreign disk."""
        with HookConf() as c:
            self._stuck(c)
            self.assertNotIn("-delete", run_hook(c, "ls")["updatedInput"]["command"])

    def test_it_does_not_ride_on_the_next_command_either(self):
        """The one that used to be free: with the marker still standing, every command
        looked like the first after a switch."""
        with HookConf() as c:
            self._stuck(c)
            run_hook(c, "ls")
            self.assertNotIn("-delete", run_hook(c, "ls")["updatedInput"]["command"])

    def test_the_failure_names_the_path_and_the_reason(self):
        """Neither half can be dropped: the path is which file to remove by hand, the
        reason is whether this is permissions, a full disk, or a filesystem gone."""
        with HookConf() as c:
            self._stuck(c)
            context = run_hook(c, "ls").get("additionalContext", "")
            self.assertIn(os.path.join(c.dir, "switched.s1"), context)
            self.assertIn("Permission denied", context)
            self.assertIn("fix it now", context)

    def test_the_failure_is_said_on_every_command(self):
        """No budget, deliberately: the store a budget would live in is the broken thing.
        For a fault of the class "fix it now", repeating is the correct behaviour."""
        with HookConf() as c:
            self._stuck(c)
            run_hook(c, "ls")
            self.assertIn("fix it now", run_hook(c, "ls").get("additionalContext", ""))

    def test_the_reminder_repeats_while_the_marker_stands(self):
        """It guards the moment a session acts on the wrong machine out of habit. While
        the marker stands that moment has not passed - and the caller has not been told
        once, because the line that would have told them is the one that failed."""
        with HookConf() as c:
            self._stuck(c)
            run_hook(c, "ls")
            self.assertIn("first command since", run_hook(c, "ls").get("additionalContext", ""))

    def test_the_repeated_reminder_does_not_claim_to_be_said_once(self):
        """A line that repeats may not promise it was said once - parentheses that lie
        teach the reader that all of the hook's parentheses are decoration."""
        with HookConf() as c:
            self._stuck(c)
            self.assertNotIn("said once per switch", run_hook(c, "ls").get("additionalContext", ""))

    def test_a_healthy_switch_says_it_once_and_does_not_complain(self):
        """The other direction, so none of the above can pass on a hook that simply
        shouts on every switch. Only the SHAPE of what a healthy switch says is pinned
        here - the spend itself is driven above, and one fact wants one driver."""
        with HookConf() as c:
            c.route_to("h1")
            c.arm_switch("h1")
            context = run_hook(c, "ls")["additionalContext"]
            self.assertIn("said once per switch", context)
            self.assertNotIn("fix it now", context)


class TestTheTwoFactsOfTheMarker(unittest.TestCase):
    """_spend_switch_marker, read directly: the split lives in ONE function, between the
    read and the removal, and the shape of its answer is what keeps the riders apart."""

    def setUp(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores the permissions this test rests on")

    def _spend(self, conf_dir, alias="h1", sid="s1"):
        """Ask the hook's own function, with CONF pointed at the temp dir."""
        orig, pretool.CONF = pretool.CONF, conf_dir
        try:
            return pretool._spend_switch_marker(sid, alias)
        finally:
            pretool.CONF = orig

    def test_no_marker_is_neither_armed_nor_a_complaint(self):
        """The ordinary command - by far the common case, and it must say nothing."""
        with HookConf() as c:
            self.assertEqual(self._spend(c.dir), (False, "", ""))

    def test_an_armed_marker_is_spent_cleanly(self):
        with HookConf() as c:
            c.arm_switch("h1")
            self.assertEqual(self._spend(c.dir), (True, "", ""))
            self.assertFalse(c.exists("switched.s1"))

    def test_a_marker_for_another_host_is_not_ours_to_spend(self):
        with HookConf() as c:
            c.arm_switch("h2")
            self.assertEqual(self._spend(c.dir), (False, "", ""))

    def test_an_unremovable_marker_is_armed_AND_carries_the_reason(self):
        """The two halves of the answer, in one call: still armed (the reminder is owed),
        not spent (the housekeeping is not)."""
        with HookConf() as c:
            c.arm_switch("h1")
            c.freeze()
            armed, unspent, unreadable = self._spend(c.dir)
            self.assertTrue(armed)
            self.assertIn("Permission denied", unspent)
            self.assertEqual(unreadable, "", "a marker that READ fine may not be called unreadable")


# -- what deliberately stayed behind --------------------------------------------


class TestTheSocketDidNotMove(unittest.TestCase):
    def test_controlpath_is_still_in_tmp(self):
        """Pinned so a later sweep of /tmp does not take it along: this path is LOCAL,
        it is meant to die with the machine, and a unix socket has ~104 characters."""
        cmd = pretool.ssh_command(HOST, "ls", "sess-1")
        self.assertIn("ControlPath=/tmp/shunt-cm-", cmd)


if __name__ == "__main__":
    unittest.main()
