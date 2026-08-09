"""
Tests for shunt.pretool — where the far side remembers this session's directory.

The design in one line: the state file is a path for the REMOTE shell, not for os.path.
That is the whole reason this file exists. `$HOME` names the account we LAND IN over there,
which is routinely not the one running the hook (local user → remote root), so the path is
assembled by that shell and never by python. Every way of getting this wrong fails in
SILENCE — a state file written somewhere nobody reads, or not written at all, looks exactly
like a session that simply starts in the login directory.

Coverage:
  - the state lives under $HOME/.cache/shunt, and no longer in world-writable /tmp
  - `$HOME` is left for the far shell, DOUBLE-quoted — no local home baked into a remote
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
    that says the memory is lost — both only on the first command after `@alias`
  - the marker's whole life through the HOOK: `@alias` arms it, the first command spends
    it, `@local` clears it
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
HOST = {"alias": "h", "target": "root@10.0.0.1", "key": None}


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
        os.chmod(self.home, 0o700)          # a read-only-home test must still clean up
        shutil.rmtree(self.root, ignore_errors=True)

    def dir(self):
        return os.path.join(self.home, ".cache", "shunt")

    def state(self, sid):
        return os.path.join(self.dir(), "cwd-" + sid)

    def run(self, cmd, sid, switched=False):
        """Execute the payload the way the far shell would, with our $HOME."""
        return subprocess.run(
            ["bash", "-c", pretool._remote_script(cmd, sid, switched)],
            env={"HOME": self.home, "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            capture_output=True, text=True)


# ── the path itself ────────────────────────────────────────────────────────────

class TestThePath(unittest.TestCase):
    """The three invariants are asserted on the SSH COMMAND, not only on the payload:
    that string is what leaves this machine, and the outer quoting is a second chance to
    get `$HOME` wrong."""

    def _both(self, sid="sess-1"):
        """The payload and the full ssh command around it — both must hold."""
        return (pretool._remote_script("ls", sid),
                pretool.ssh_command(HOST, "ls", sid))

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
        """It would point at a directory that is not on the far machine — and be swallowed
        whole, since the write is silenced: the session would forget every `cd` and say
        nothing about it."""
        for text in self._both():
            self.assertNotIn(os.path.expanduser("~"), text)

    def test_the_script_is_valid_shell(self):
        for switched in (False, True):
            script = pretool._remote_script("echo hi", "sess-1", switched)
            check = subprocess.run(["bash", "-n"], input=script,
                                   capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)

    def test_ssh_command_still_carries_the_script(self):
        """The payload is built in one place; ssh_command only wraps it.

        Asserted so the extraction cannot drift into a second, stale copy — and for both
        shapes, since the switch shape is the one with something to forget.
        """
        import shlex
        for switched in (False, True):
            cmd = pretool.ssh_command(HOST, "ls", "sess-1", switched)
            self.assertIn(shlex.quote(pretool._remote_script("ls", "sess-1", switched)),
                          cmd)


# ── what the far shell actually does with it ───────────────────────────────────

class TestOnTheFarSide(unittest.TestCase):

    def test_the_directory_is_created(self):
        with FakeHome() as h:
            h.run("true", "sess-mk")
            self.assertTrue(os.path.isdir(h.dir()))

    def test_the_directory_is_private(self):
        """The file is a trail of where someone works — world-readable in /tmp was half
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
        nobody looks — and nobody would hear about it."""
        with FakeHome("ho me") as h:
            h.run("cd /usr", "sess-space")
            self.assertTrue(os.path.exists(h.state("sess-space")))

    def test_the_exit_code_survives_the_trap(self):
        with FakeHome() as h:
            self.assertEqual(h.run("exit 7", "sess-rc").returncode, 7)

    def test_the_exit_code_survives_the_switch_housekeeping_too(self):
        """The sweep and the probe run BEFORE the command; neither may become its code."""
        with FakeHome() as h:
            self.assertEqual(h.run("exit 7", "sess-rc2", switched=True).returncode, 7)


# ── the id comes from outside ──────────────────────────────────────────────────

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
            out = h.run("true", "a$(echo OWNED)b", switched=True)
            self.assertNotIn("OWNED", out.stdout + out.stderr)


# ── the new failure the move brings with it ────────────────────────────────────

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
        """Once per `@alias` — the moment the session is orienting anyway."""
        with FakeHome() as h:
            os.chmod(h.home, 0o500)
            out = h.run("true", "sess-ro", switched=True)
            self.assertIn("will not remember", out.stderr)
            self.assertIn("shunt", out.stderr)

    def test_the_probe_is_silent_on_a_healthy_home(self):
        """A line on the way in, every switch, would be the wallpaper we are avoiding."""
        with FakeHome() as h:
            out = h.run("echo ok", "sess-ok", switched=True)
            self.assertEqual(out.stderr, "")
            self.assertEqual(out.stdout.strip(), "ok")

    def test_the_switch_command_still_runs_and_keeps_its_code(self):
        with FakeHome() as h:
            os.chmod(h.home, 0o500)
            out = h.run("echo alive; exit 3", "sess-ro", switched=True)
            self.assertIn("alive", out.stdout)
            self.assertEqual(out.returncode, 3)


# ── the sweep: once per switch, never per command ──────────────────────────────

class TestTheSweep(unittest.TestCase):
    """A session id is born and never dies. Without a sweep the directory grows one file
    per session forever; with one on every command it is a `find` over someone's disk,
    several times a minute, to delete nothing."""

    def _age(self, path, days):
        old = time.time() - days * 86400
        os.utime(path, (old, old))

    def test_the_sweep_rides_only_on_the_switch(self):
        self.assertNotIn("-delete", pretool._remote_script("ls", "sess-1"))
        self.assertIn("-delete", pretool._remote_script("ls", "sess-1", switched=True))

    def test_it_drops_the_dead_and_keeps_the_living(self):
        with FakeHome() as h:
            os.makedirs(h.dir())
            for name in ("cwd-dead", "cwd-alive"):
                open(os.path.join(h.dir(), name), "w").close()
            self._age(os.path.join(h.dir(), "cwd-dead"), 40)
            h.run("true", "sess-sweep", switched=True)
            left = sorted(os.listdir(h.dir()))
            self.assertIn("cwd-alive", left)
            self.assertNotIn("cwd-dead", left)

    def test_it_touches_nothing_that_is_not_ours(self):
        """`-maxdepth 1 -name 'cwd-*'` — a cache directory is a shared place."""
        with FakeHome() as h:
            os.makedirs(h.dir())
            stranger = os.path.join(h.dir(), "notes.txt")
            open(stranger, "w").close()
            self._age(stranger, 400)
            h.run("true", "sess-sweep2", switched=True)
            self.assertTrue(os.path.exists(stranger))

    def test_our_own_cwd_is_read_before_the_sweep(self):
        """Ordering, pinned: the restore reads the file, THEN the sweep runs. A session
        idle for over a month still lands where it left off, and its file comes back
        fresh — the reverse order would send it home without a word."""
        with FakeHome() as h:
            os.makedirs(h.dir())
            with open(h.state("sess-old"), "w") as f:
                f.write("/usr\n")
            self._age(h.state("sess-old"), 40)
            out = h.run("pwd", "sess-old", switched=True)
            self.assertEqual(out.stdout.strip(), "/usr")
            self.assertTrue(os.path.exists(h.state("sess-old")))


# ── the one-shot marker, driven through the hook ───────────────────────────────

class HookConf:
    """A temp SHUNT_CONF with one host, plus the session files the hook reads."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-cwd-conf-")
        with open(os.path.join(self.dir, "shunt.toml"), "w") as f:
            f.write('[hosts]\nh1 = "root@10.0.0.1"\n')
        return self

    def __exit__(self, *_):
        shutil.rmtree(self.dir, ignore_errors=True)

    def exists(self, name):
        return os.path.exists(os.path.join(self.dir, name))


def run_hook(conf, command, sid="s1"):
    """Run pretool.py exactly as the harness does. Returns the parsed hook output.

    PYTHONPATH is stripped for the same reason tests/test_pretool_warnings.py strips it:
    settings.json names pretool.py by absolute path, so the field never has the package
    on sys.path, and the test runner's PYTHONPATH would hide that.
    """
    payload = {"tool_name": "Bash", "session_id": sid,
               "tool_input": {"command": command}}
    env = dict(os.environ, SHUNT_CONF=conf.dir)
    env.pop("PYTHONPATH", None)
    r = subprocess.run([sys.executable, PRETOOL],
                       input=json.dumps(payload).encode(),
                       capture_output=True, env=env)
    out = r.stdout.decode().strip()
    return json.loads(out)["hookSpecificOutput"] if out else {}


class TestTheSwitchMarkerLifecycle(unittest.TestCase):
    """`switched.<sid>` from birth to death, through main() and nothing hand-placed.

    Everything above drives the far-side script directly, with `switched` passed in as an
    argument — which proves what the marker BUYS and never that the hook writes or removes
    one. If `@alias` stopped arming it, the far side's housekeeping would go unpaid forever
    and every other test in this file would still be green.
    """

    def test_a_switch_arms_the_marker(self):
        with HookConf() as c:
            run_hook(c, "@h1")
            self.assertTrue(c.exists("switched.s1"))

    def test_the_first_command_spends_it_and_the_second_carries_nothing(self):
        """One question, two riders: the sweep and the probe ride the same one-shot fact."""
        with HookConf() as c:
            run_hook(c, "@h1")
            first = run_hook(c, "ls")
            self.assertIn("-delete", first["updatedInput"]["command"])
            second = run_hook(c, "ls")
            self.assertNotIn("-delete", second["updatedInput"]["command"])
            self.assertFalse(c.exists("switched.s1"))

    def test_going_local_clears_an_unspent_marker(self):
        """There is no far machine left to keep house on. Left behind, it would be spent
        by the first command of the NEXT switch — housekeeping on a host that never armed
        it, and none on the one that did."""
        with HookConf() as c:
            run_hook(c, "@h1")
            run_hook(c, "@local")
            self.assertFalse(c.exists("switched.s1"))


# ── what deliberately stayed behind ────────────────────────────────────────────

class TestTheSocketDidNotMove(unittest.TestCase):

    def test_controlpath_is_still_in_tmp(self):
        """Pinned so a later sweep of /tmp does not take it along: this path is LOCAL,
        it is meant to die with the machine, and a unix socket has ~104 characters."""
        cmd = pretool.ssh_command(HOST, "ls", "sess-1")
        self.assertIn("ControlPath=/tmp/shunt-cm-", cmd)


if __name__ == "__main__":
    unittest.main()
