"""
Tests for pretool.py — housekeeping on THIS side of the wire.

The far side has been swept since the cwd files were born: `find … -name 'cwd-*' -mtime
+30 -delete`, paid for once per switch. The near side never was. A session id is born and
never dies, so `~/.config/shunt/` kept `active-host.<sid>` · `warned.<sid>` ·
`switched.<sid>` for every session that ever went remote, for as long as the machine lives
— the same class of leak, already solved on one side and left standing on the other.

⚠ The interesting part is what is NOT swept. `target.<sid>` is written ONCE, at the switch,
and never touched again, so an old mtime there does not mean a dead session — it means a
session that switched a while ago and may well still be working. Sweeping it would answer
that session's next command with "never switched" and run it HERE: the silent fall to the
wrong machine this whole file exists to prevent, performed by the housekeeping itself. The
three that ARE swept cost a repeated warning, a stale status line and a missed one-shot
notice; none of them can move a command to another machine.

Coverage:
  - old markers go, on a switch, in both directions (`@local` and `@alias`)
  - fresh markers of live sessions stay
  - `target.<sid>` is never swept, however old — the deliberate exception
  - an ordinary command sweeps NOTHING: this rides on the rare moment, not the hot path
  - a config dir that cannot be listed costs the switch nothing

SHUNT_CONF points at a temp directory, and the `@alias` case runs against a stub `ssh` on
PATH so the switch's probe answers at once instead of waiting on a real handshake.
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

OLD = 40 * 86400  # comfortably past the 30-day window
YOUNG = 2 * 86400  # comfortably inside it


class HookConf:
    """A temp SHUNT_CONF with one host and a stub `ssh`, so a switch does not wait."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-sweep-")
        with open(os.path.join(self.dir, "shunt.toml"), "w") as f:
            f.write('[hosts]\nh1 = "root@203.0.113.11"\n')
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        path = os.path.join(self.bin, "ssh")
        with open(path, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o755)
        return self

    def __exit__(self, *_):
        shutil.rmtree(self.dir, ignore_errors=True)

    def marker(self, name, age):
        """Put a file in the config dir and age it."""
        path = os.path.join(self.dir, name)
        with open(path, "w") as f:
            f.write("h1")
        when = time.time() - age
        os.utime(path, (when, when))
        return path

    def names(self, sid="s1"):
        """The session files left behind — never the LIVE session's own.

        A switch arms its own ticket (`switched.<sid>`) and may write its own sidecar on
        the way through, so those appear in the directory by design. They are what the
        switch just DID; the sweep is about what other sessions left.
        """
        return sorted(n for n in os.listdir(self.dir) if n not in ("shunt.toml", "bin") and not n.endswith("." + sid))


def hook_env(conf_dir, **extra):
    """The environment the harness actually gives the hook — WITHOUT PYTHONPATH.

    settings.json names pretool.py by absolute path, so `python3 …/src/shunt/pretool.py`
    puts …/src/shunt on sys.path and never …/src. Leaving the test runner's PYTHONPATH in
    place would hide exactly that: the package would import for a reason the hook cannot
    count on in the field.
    """
    env = dict(os.environ, SHUNT_CONF=conf_dir, **extra)
    env.pop("PYTHONPATH", None)
    return env


def run_hook(conf, command, sid="s1"):
    payload = {"tool_name": "Bash", "session_id": sid, "tool_input": {"command": command}}
    r = subprocess.run(
        [sys.executable, PRETOOL],
        input=json.dumps(payload).encode(),
        capture_output=True,
        env=hook_env(conf.dir, PATH=conf.bin + os.pathsep + os.environ["PATH"]),
    )
    return r.returncode, r.stdout.decode()


class TestTheSweepOnASwitch(unittest.TestCase):
    STALE = ("active-host.dead", "warned.dead", "switched.dead")

    def test_old_markers_go_when_coming_home(self):
        with HookConf() as c:
            for name in self.STALE:
                c.marker(name, OLD)
            run_hook(c, "@local")
            self.assertEqual(c.names(), [])

    def test_old_markers_go_when_switching_to_a_host(self):
        with HookConf() as c:
            for name in self.STALE:
                c.marker(name, OLD)
            run_hook(c, "@h1")
            self.assertEqual(c.names(), [])

    def test_fresh_markers_of_live_sessions_stay(self):
        with HookConf() as c:
            for name in self.STALE:
                c.marker(name, YOUNG)
            run_hook(c, "@local")
            self.assertEqual(c.names(), sorted(self.STALE))

    def test_the_routing_file_is_never_swept(self):
        """The deliberate exception. `target.<sid>` is written once and never touched, so
        age says nothing about whether that session is alive — and taking it away would
        send its next command to the local machine without a word."""
        with HookConf() as c:
            c.marker("target.dead", OLD)
            c.marker("warned.dead", OLD)
            run_hook(c, "@local")
            self.assertIn("target.dead", c.names())
            self.assertNotIn("warned.dead", c.names())

    def test_an_ordinary_command_sweeps_nothing(self):
        """A directory listing before every bash command would be work done several times
        a minute to delete nothing. The switch is the rare moment; this is the hot path."""
        with HookConf() as c:
            c.route = c.marker("target.s1", YOUNG)
            for name in self.STALE:
                c.marker(name, OLD)
            run_hook(c, "ls")
            for name in self.STALE:
                self.assertIn(name, c.names(), "%s was swept from the hot path" % name)

    def test_a_switch_survives_a_config_dir_it_cannot_list(self):
        """Housekeeping may never cost the switch: the routing is the point of the call."""
        with HookConf() as c:
            os.chmod(c.dir, 0o300)  # write+execute, no read → listdir raises
            try:
                code, out = run_hook(c, "@local")
                self.assertEqual(code, 0)
                self.assertIn("LOCAL", out)
            finally:
                os.chmod(c.dir, 0o700)


if __name__ == "__main__":
    unittest.main()
