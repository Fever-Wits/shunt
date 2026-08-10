"""
Tests for shunt.cli — the shared ssh options (ssh_opts) and the hands that use them.

Coverage:
  - ssh_argv is literally built from ssh_opts (not a parallel copy)
  - cp hands rsync the SAME options, as a string
  - BatchMode reaches cp  — it did not, and cp could hang on a password prompt
  - ControlMaster reaches cp — it did not, so cp opened a fresh connection each time
  - the key is included when the host has one, absent when it does not
  - a per-host key wins over the default
  - the hook's own socket is keyed on the same things as the CLI's (user, host, port)

Why this file exists: the options were written TWICE — once in ssh_argv, once inside
cmd_cp — and the copy fell behind the original. This is the test that makes them one.
"""

import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Make the src tree importable when run without installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import shunt.cli as shunt_mod
from shunt import pretool


class TmpHosts:
    """Context manager: temp CONF with a keyed host and a keyless one."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-opts-")
        with open(os.path.join(self.dir, "shunt.toml"), "w") as f:
            f.write(
                'key = "/keys/default"\n'
                "[hosts]\n"
                'keyed = "root@203.0.113.1"\n'
                'own = { target = "root@203.0.113.2", key = "/keys/own" }\n'
            )
        self._orig = shunt_mod.CONF
        shunt_mod.CONF = self.dir
        return self

    def __exit__(self, *_):
        shunt_mod.CONF = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)


def rsync_ssh_string(argv):
    """Run cmd_cp with rsync stubbed; return the string handed to rsync's -e."""
    seen = {}

    def fake_run(a, *args, **kwargs):
        seen["argv"] = a
        return MagicMock(returncode=0)

    with patch.object(shunt_mod.subprocess, "run", fake_run):
        shunt_mod.cmd_cp(argv)
    a = seen["argv"]
    return a[a.index("-e") + 1]


# ── one source, not two ────────────────────────────────────────────────────────


class TestSingleSource(unittest.TestCase):
    def test_ssh_argv_is_built_from_ssh_opts(self):
        host = {"alias": "h", "target": "root@203.0.113.1", "key": None}
        opts = shunt_mod.ssh_opts(host)
        argv = shunt_mod.ssh_argv(host)
        self.assertEqual(argv, ["ssh"] + opts + [host["target"]])

    def test_cp_and_ssh_share_every_option(self):
        """Whatever ssh gets, rsync's ssh gets — that is the point of the shared source."""
        with TmpHosts():
            e = rsync_ssh_string(["@keyed:/remote/f", "/local/f"])
            host = shunt_mod.resolve_host("keyed")
            for opt in shunt_mod.ssh_opts(host):
                self.assertIn(opt, e, f"cp is missing {opt!r}")


# ── the two options cp used to lack ────────────────────────────────────────────


class TestCpRegressions(unittest.TestCase):
    def test_cp_gets_batchmode(self):
        """Without it, cp can sit forever on a password prompt inside a script."""
        with TmpHosts():
            self.assertIn("BatchMode=yes", rsync_ssh_string(["@keyed:/r", "/l"]))

    def test_cp_gets_controlmaster(self):
        """Without it, cp opens a fresh connection instead of reusing the muxed one."""
        with TmpHosts():
            e = rsync_ssh_string(["@keyed:/r", "/l"])
            self.assertIn("ControlMaster=auto", e)
            self.assertIn("ControlPersist=300", e)

    def test_cp_still_gets_controlpath(self):
        """The one option it always had must not be lost in the merge."""
        with TmpHosts():
            self.assertIn("ControlPath=", rsync_ssh_string(["@keyed:/r", "/l"]))


# ── the key ────────────────────────────────────────────────────────────────────


class TestKey(unittest.TestCase):
    def test_default_key_is_passed(self):
        with TmpHosts():
            self.assertIn("/keys/default", rsync_ssh_string(["@keyed:/r", "/l"]))

    def test_per_host_key_wins(self):
        with TmpHosts():
            e = rsync_ssh_string(["@own:/r", "/l"])
            self.assertIn("/keys/own", e)
            self.assertNotIn("/keys/default", e)

    def test_no_key_means_no_i_flag(self):
        """ssh should choose the identity itself, not be handed an empty -i."""
        host = {"alias": "h", "target": "root@203.0.113.1", "key": None}
        self.assertNotIn("-i", shunt_mod.ssh_opts(host))

    def test_key_present_means_i_flag(self):
        host = {"alias": "h", "target": "root@203.0.113.1", "key": "/keys/k"}
        opts = shunt_mod.ssh_opts(host)
        self.assertEqual(opts[:2], ["-i", "/keys/k"])


# ── the third copy: the hook's own socket ──────────────────────────────────────


class TestHookControlPath(unittest.TestCase):
    """A muxed connection is shared by whoever names the same socket.

    So the socket name must carry every part of the destination. It carried the host and
    the port but not the USER — and the config allows two aliases onto one machine with
    different accounts (`deploy@web-01`, `root@web-01`). The second one would ride the
    first one's master and run as the wrong account, silently, with the right output.
    """

    def _controlpath(self, target):
        host = {"alias": "h", "target": target, "key": None}
        cmd = pretool.ssh_command(host, "ls", "sess-1")
        opt = [part for part in cmd.split() if part.startswith("ControlPath=")]
        return opt[0]

    def test_the_socket_is_keyed_on_the_user(self):
        self.assertIn("%r", self._controlpath("root@203.0.113.1"))

    def test_the_socket_is_keyed_on_host_and_port_too(self):
        path = self._controlpath("root@203.0.113.1")
        self.assertIn("%h", path)
        self.assertIn("%p", path)

    def test_the_socket_is_keyed_on_the_session(self):
        """Parallel sessions must not share a master either — that was already true."""
        self.assertIn("sess-1", self._controlpath("root@203.0.113.1"))

    def test_the_hook_and_the_cli_key_on_the_same_things(self):
        """One fact, two homes: whatever one keys on, the other must key on as well.

        What they no longer share is the PLACE — see TestTheSocketLeftTmp. The hook's name
        carries a session id and can afford /tmp; the CLI's cannot and moved.
        """
        hook = self._controlpath("root@203.0.113.1")
        for token in ("%r", "%h", "%p"):
            self.assertIn(token, shunt_mod.SOCK_NAME, f"the CLI dropped {token}")
            self.assertIn(token, hook, f"the hook dropped {token}")


# ── where the CLI's socket lives ───────────────────────────────────────────────


class TestTheSocketLeftTmp(unittest.TestCase):
    """It was `/tmp/shunt-cm-cli-%r@%h:%p.sock` — a predictable name in a world-writable
    directory.

    `%r` is the REMOTE account; the local uid appears nowhere in it. So on a shared machine
    two different LOCAL users reaching the same target computed the same path, in a place
    anybody can write to. The name cannot be randomised — the next `shunt` call has to find
    the master this one left, which IS the feature — so the place moved instead, exactly the
    way the far side's cwd state left /tmp for ~/.cache/shunt.

    Both halves are pinned here: private enough that the squat is impossible, and stable
    enough that the reuse survives. Either one alone can be had by giving up the other.
    """

    def path_with(self, xdg=None, home=None):
        env = {k: v for k, v in os.environ.items() if k != "XDG_RUNTIME_DIR"}
        if xdg is not None:
            env["XDG_RUNTIME_DIR"] = xdg
        if home is not None:
            env["HOME"] = home
        with patch.dict(os.environ, env, clear=True):
            return shunt_mod.control_path()

    # ── private ────────────────────────────────────────────────────────────────

    def test_the_directory_is_not_world_writable(self):
        """The property the move is FOR. /tmp has this bit set; that is the whole defect."""
        with tempfile.TemporaryDirectory() as run:
            d = os.path.dirname(self.path_with(xdg=run))
            self.assertFalse(os.stat(d).st_mode & stat.S_IWOTH, "%s is world-writable" % d)

    def test_the_directory_is_made_at_0700(self):
        with tempfile.TemporaryDirectory() as run:
            d = os.path.dirname(self.path_with(xdg=run))
            self.assertTrue(os.path.isdir(d))
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)

    def test_the_old_path_is_gone(self):
        with tempfile.TemporaryDirectory() as run:
            self.assertNotIn("/tmp/shunt-cm-cli-", self.path_with(xdg=run))

    # ── which private place ────────────────────────────────────────────────────

    def test_xdg_runtime_dir_is_preferred(self):
        """Per-user, 0700 by its spec, on tmpfs, taken away at logout — the lifetime a
        control socket wants, and the shortest path, which is a budget here."""
        with tempfile.TemporaryDirectory() as run:
            self.assertTrue(self.path_with(xdg=run).startswith(os.path.join(run, "shunt") + os.sep))

    def test_without_xdg_it_falls_back_to_cache(self):
        """cron, containers, `ssh host shunt …` — no session, no runtime dir."""
        with tempfile.TemporaryDirectory() as home:
            p = self.path_with(xdg=None, home=home)
            self.assertEqual(os.path.dirname(p), os.path.join(home, ".cache", "shunt"))

    def test_an_xdg_that_is_not_there_is_not_believed(self):
        """A stale value in the environment must not send the socket into thin air."""
        with tempfile.TemporaryDirectory() as home:
            p = self.path_with(xdg=os.path.join(home, "no-such-runtime-dir"), home=home)
            self.assertEqual(os.path.dirname(p), os.path.join(home, ".cache", "shunt"))

    def test_a_relative_xdg_is_not_believed(self):
        """The spec says absolute. A relative one would put the socket wherever the caller
        happened to be standing — a different socket per working directory."""
        with tempfile.TemporaryDirectory() as home:
            p = self.path_with(xdg="runtime", home=home)
            self.assertEqual(os.path.dirname(p), os.path.join(home, ".cache", "shunt"))

    # ── still reusable ─────────────────────────────────────────────────────────

    def test_two_calls_name_the_same_socket(self):
        """The reuse IS the feature. Anything per-call in the name — a pid, a timestamp,
        randomness — would close the exposure by removing the reason the socket exists."""
        with tempfile.TemporaryDirectory() as run:
            self.assertEqual(self.path_with(xdg=run), self.path_with(xdg=run))

    def test_the_whole_ssh_call_is_stable_too(self):
        host = {"alias": "h", "target": "root@203.0.113.1", "key": None}
        self.assertEqual(shunt_mod.ssh_argv(host), shunt_mod.ssh_argv(host))

    def test_the_name_leaves_room_for_a_real_destination(self):
        """ssh expands %r/%h/%p and then REFUSES a path that does not fit a unix socket:
        "ControlPath too long … >= 108 bytes", exit 255, nothing attempted — fatal, not a
        fallback. Measured: 107 bytes bind on Linux, macOS allows 103. So the name is a
        budget, and this is what it still has to buy after the move."""
        base = "/run/user/1000/shunt"  # the preferred base, as a Linux system hands it out
        expanded = (
            shunt_mod.SOCK_NAME.replace("%r", "deploy")
            .replace("%h", "web-01.eu-central.internal.example.com")
            .replace("%p", "22")
        )
        whole = os.path.join(base, expanded)
        self.assertLessEqual(len(whole), 103, "%d bytes — too long for a unix socket on macOS" % len(whole))

    # ── best-effort ────────────────────────────────────────────────────────────

    def test_a_directory_that_cannot_be_made_still_gives_a_path(self):
        """MEASURED: ssh whose ControlPath directory is missing still connects — it says so
        at -v and opens a plain connection. What is lost is REUSE, not the command, so the
        mkdir may not be allowed to take the command down with it."""
        with tempfile.TemporaryDirectory() as run:

            def refuse(*a, **k):
                raise PermissionError(13, "Permission denied")

            with patch.object(shunt_mod.os, "makedirs", refuse):
                self.assertTrue(self.path_with(xdg=run).startswith(run))


if __name__ == "__main__":
    unittest.main()
