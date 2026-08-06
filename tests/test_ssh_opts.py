"""
Tests for shunt.cli — the shared ssh options (ssh_opts) and the hands that use them.

Coverage:
  - ssh_argv is literally built from ssh_opts (not a parallel copy)
  - cp hands rsync the SAME options, as a string
  - BatchMode reaches cp  — it did not, and cp could hang on a password prompt
  - ControlMaster reaches cp — it did not, so cp opened a fresh connection each time
  - the key is included when the host has one, absent when it does not
  - a per-host key wins over the default

Why this file exists: the options were written TWICE — once in ssh_argv, once inside
cmd_cp — and the copy fell behind the original. This is the test that makes them one.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Make the src tree importable when run without installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import shunt.cli as shunt_mod


class TmpHosts:
    """Context manager: temp CONF with a keyed host and a keyless one."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-opts-")
        with open(os.path.join(self.dir, "shunt.toml"), "w") as f:
            f.write('key = "/keys/default"\n'
                    '[hosts]\n'
                    'keyed = "root@10.0.0.1"\n'
                    'own = { target = "root@10.0.0.2", key = "/keys/own" }\n')
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
        host = {"alias": "h", "target": "root@10.0.0.1", "key": None}
        opts = shunt_mod.ssh_opts(host)
        argv = shunt_mod.ssh_argv(host)
        self.assertEqual(argv, ["ssh"] + opts + [host["target"]])

    def test_cp_and_ssh_share_every_option(self):
        """Whatever ssh gets, rsync's ssh gets — that is the point of the shared source."""
        with TmpHosts():
            e = rsync_ssh_string(["@keyed:/remote/f", "/local/f"])
            host = shunt_mod.resolve_host("keyed")
            for opt in shunt_mod.ssh_opts(host):
                self.assertIn(opt, e, "cp is missing %r" % opt)


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
        host = {"alias": "h", "target": "root@10.0.0.1", "key": None}
        self.assertNotIn("-i", shunt_mod.ssh_opts(host))

    def test_key_present_means_i_flag(self):
        host = {"alias": "h", "target": "root@10.0.0.1", "key": "/keys/k"}
        opts = shunt_mod.ssh_opts(host)
        self.assertEqual(opts[:2], ["-i", "/keys/k"])


if __name__ == "__main__":
    unittest.main()
