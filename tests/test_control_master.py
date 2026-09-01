"""
Tests for control_master (shunt.config, shunt.pretool) - the per-host switch for ssh's
connection-reuse socket, and the ONE thing it protects against: a ControlPath long enough
that ssh refuses the connection outright (exit 255, no attempt at all) - a failure that
then reads exactly like the host being down (measured, see the report that started this).

Coverage:
  - shunt.toml reads `control_master`: absent -> true, explicit true, explicit false,
    and a false on one host does not leak onto its neighbour
  - ssh_opts drops ControlMaster/ControlPath/ControlPersist when control_master is false,
    and asks for nothing else instead - the only difference is those three
  - -i, BatchMode and StrictHostKeyChecking are asked for either way
  - the ControlPath length is computed correctly - pinned against the exact byte counts
    named in the report (root@192.168.198.84 -> 78, ... -> 102, ... -> 104)
  - the "socket would not fit" warning fires only ABOVE 103 bytes, never AT 103, and only
    while control_master is (still) true
  - end-to-end through the hook: the warning rides on the SWITCH message once, never on
    the commands that follow, and disappears entirely when control_master = false

⚠ No test here opens a real connection - `ssh` is a stub that exits 0 immediately (see
test_pretool_probe.py for why: the address is not ours to knock on).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

# Make the src tree importable when run without installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shunt import config, pretool

PRETOOL = pretool.__file__


# -- shunt.config: reading control_master ----------------------------------------


class TmpConf:
    """Context manager: an empty temp conf dir (mirrors test_shunt_config.TmpConf)."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-cm-config-")
        return self

    def __exit__(self, *_):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, text):
        with open(os.path.join(self.dir, name), "w") as f:
            f.write(text)

    def resolve(self, alias):
        return config.resolve(self.dir, alias)


class TestControlMasterConfig(unittest.TestCase):
    def test_absent_on_a_bare_string_host_defaults_to_true(self):
        with TmpConf() as c:
            c.write("shunt.toml", '[hosts]\nh1 = "root@203.0.113.1"\n')
            self.assertTrue(c.resolve("h1")["control_master"])

    def test_absent_on_the_extended_form_also_defaults_to_true(self):
        with TmpConf() as c:
            c.write("shunt.toml", '[hosts]\nh1 = { target = "root@203.0.113.1", key = "/k" }\n')
            self.assertTrue(c.resolve("h1")["control_master"])

    def test_explicit_true(self):
        with TmpConf() as c:
            c.write("shunt.toml", '[hosts]\nh1 = { target = "root@203.0.113.1", control_master = true }\n')
            self.assertTrue(c.resolve("h1")["control_master"])

    def test_explicit_false(self):
        with TmpConf() as c:
            c.write("shunt.toml", '[hosts]\nh1 = { target = "root@203.0.113.1", control_master = false }\n')
            self.assertFalse(c.resolve("h1")["control_master"])

    def test_false_on_one_host_does_not_affect_another(self):
        with TmpConf() as c:
            c.write(
                "shunt.toml",
                '[hosts]\na = { target = "root@203.0.113.1", control_master = false }\nb = "root@203.0.113.2"\n',
            )
            self.assertFalse(c.resolve("a")["control_master"])
            self.assertTrue(c.resolve("b")["control_master"])


# -- ssh_opts: what control_master actually changes ------------------------------


def _opt_names(opts):
    """Every `-o NAME=...` asked for, plus bare flags - names only, order-independent.

    Same shape as test_ssh_opts.py's own `names()`: filtering on `"=" in o` would read
    only `-o` values and silently skip flags that carry none.
    """
    out, prev = [], ""
    for tok in opts:
        if prev == "-o":
            out.append(tok.split("=")[0])
        elif tok.startswith("-"):
            out.append(tok)
        prev = tok
    return sorted(out)


REUSE_OPTIONS = ("ControlMaster", "ControlPath", "ControlPersist")


class TestSshOptsRespectsControlMaster(unittest.TestCase):
    def test_default_true_keeps_the_reuse_options(self):
        host = {"alias": "h", "target": "root@203.0.113.1", "key": None}
        names = _opt_names(pretool.ssh_opts(host, "sess-1"))
        for name in REUSE_OPTIONS:
            self.assertIn(name, names)

    def test_explicit_true_keeps_the_reuse_options(self):
        host = {"alias": "h", "target": "root@203.0.113.1", "key": None, "control_master": True}
        names = _opt_names(pretool.ssh_opts(host, "sess-1"))
        for name in REUSE_OPTIONS:
            self.assertIn(name, names)

    def test_false_drops_all_three_reuse_options(self):
        host = {"alias": "h", "target": "root@203.0.113.1", "key": None, "control_master": False}
        names = _opt_names(pretool.ssh_opts(host, "sess-1"))
        for name in REUSE_OPTIONS:
            self.assertNotIn(name, names)

    def test_false_still_asks_for_stricthostkeychecking_and_batchmode(self):
        host = {"alias": "h", "target": "root@203.0.113.1", "key": None, "control_master": False}
        names = _opt_names(pretool.ssh_opts(host, "sess-1"))
        self.assertIn("StrictHostKeyChecking", names)
        self.assertIn("BatchMode", names)

    def test_true_also_asks_for_stricthostkeychecking_and_batchmode(self):
        host = {"alias": "h", "target": "root@203.0.113.1", "key": None, "control_master": True}
        names = _opt_names(pretool.ssh_opts(host, "sess-1"))
        self.assertIn("StrictHostKeyChecking", names)
        self.assertIn("BatchMode", names)

    def test_false_keeps_the_key(self):
        host = {"alias": "h", "target": "root@203.0.113.1", "key": "/keys/k", "control_master": False}
        opts = pretool.ssh_opts(host, "sess-1")
        self.assertEqual(opts[:2], ["-i", "/keys/k"])

    def test_no_key_means_no_i_flag_either_way(self):
        for cm in (True, False):
            host = {"alias": "h", "target": "root@203.0.113.1", "key": None, "control_master": cm}
            self.assertNotIn("-i", pretool.ssh_opts(host, "sess-1"))

    def test_the_only_difference_between_true_and_false_is_the_three_reuse_options(self):
        """Nothing else may quietly move when this flag flips."""
        host_true = {"alias": "h", "target": "root@203.0.113.1", "key": None, "control_master": True}
        host_false = {"alias": "h", "target": "root@203.0.113.1", "key": None, "control_master": False}
        names_true = set(_opt_names(pretool.ssh_opts(host_true, "sess-1")))
        names_false = set(_opt_names(pretool.ssh_opts(host_false, "sess-1")))
        self.assertEqual(names_true - names_false, set(REUSE_OPTIONS))
        self.assertEqual(names_false - names_true, set())


# -- the socket-length math -------------------------------------------------------


class TestControlSocketLength(unittest.TestCase):
    """Pinned against the byte counts measured for the report that started this."""

    SID36 = "a" * 36  # a real session id is a 36-character UUID

    def test_matches_the_measured_short_target(self):
        self.assertEqual(pretool._control_socket_length(self.SID36, "root@192.168.198.84"), 78)

    def test_matches_the_measured_target_at_the_edge(self):
        length = pretool._control_socket_length(self.SID36, "root@web-01.eu-central.internal.example.com")
        self.assertEqual(length, 102)

    def test_matches_the_measured_target_just_over(self):
        length = pretool._control_socket_length(self.SID36, "deploy@web-01.eu-central.internal.example.com")
        self.assertEqual(length, 104)


class TestControlSocketNotice(unittest.TestCase):
    """The `>103, not at 103` boundary, and the two ways it can be silenced."""

    SID36 = "a" * 36

    def _target_of_length(self, n):
        """A well-formed user@host of exactly n bytes ("u@" + filler)."""
        return "u@" + "h" * (n - 2)

    def _target_for_total(self, total):
        """The target whose ControlPath, with SID36, comes out to exactly `total` bytes."""
        return self._target_of_length(total - pretool._control_socket_length(self.SID36, ""))

    def test_fires_over_the_limit(self):
        target = self._target_for_total(pretool.CONTROL_SOCKET_MAX + 1)
        host = {"alias": "h", "target": target, "key": None}
        notice = pretool._control_socket_notice(host, self.SID36, "h")
        self.assertIn("connection-reuse socket path would be", notice)
        self.assertIn("@h", notice)

    def test_silent_at_exactly_the_limit(self):
        target = self._target_for_total(pretool.CONTROL_SOCKET_MAX)
        host = {"alias": "h", "target": target, "key": None}
        self.assertEqual(pretool._control_socket_length(self.SID36, target), pretool.CONTROL_SOCKET_MAX)
        self.assertEqual(pretool._control_socket_notice(host, self.SID36, "h"), "")

    def test_silent_well_under_the_limit(self):
        host = {"alias": "h", "target": "root@192.168.198.84", "key": None}
        self.assertEqual(pretool._control_socket_notice(host, self.SID36, "h"), "")

    def test_control_master_false_silences_it_even_far_over_the_limit(self):
        target = self._target_for_total(pretool.CONTROL_SOCKET_MAX + 20)
        host = {"alias": "h", "target": target, "key": None, "control_master": False}
        self.assertEqual(pretool._control_socket_notice(host, self.SID36, "h"), "")

    def test_the_byte_count_named_in_the_message_matches_what_was_computed(self):
        target = self._target_for_total(pretool.CONTROL_SOCKET_MAX + 5)
        host = {"alias": "h", "target": target, "key": None}
        length = pretool._control_socket_length(self.SID36, target)
        notice = pretool._control_socket_notice(host, self.SID36, "h")
        self.assertIn(f"{length} bytes", notice)

    def test_the_alias_named_is_the_one_switched_to(self):
        target = self._target_for_total(pretool.CONTROL_SOCKET_MAX + 1)
        host = {"alias": "build", "target": target, "key": None}
        notice = pretool._control_socket_notice(host, self.SID36, "build")
        self.assertIn("@build -", notice)


# -- end-to-end through the hook: once, at the switch -----------------------------


class HookConf:
    """A temp SHUNT_CONF with a too-long host (on and off) and a short one, plus a stub
    `ssh` that answers instantly - the probe the switch makes must not cost this suite a
    real network round-trip (see test_pretool_probe.py, same reasoning).
    """

    # Same target as the "104 bytes, FAILS" line of the report - kept literal on purpose,
    # so a reader can check this suite against that report by eye.
    LONG_TARGET = "deploy@web-01.eu-central.internal.example.com"
    SHORT_TARGET = "root@203.0.113.9"

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-cm-switch-")
        with open(os.path.join(self.dir, "shunt.toml"), "w") as f:
            f.write(
                "[hosts]\n"
                f'long = "{self.LONG_TARGET}"\n'
                f'longoff = {{ target = "{self.LONG_TARGET}", control_master = false }}\n'
                f'short = "{self.SHORT_TARGET}"\n'
            )
        self.bin = os.path.join(self.dir, "_bin")
        os.makedirs(self.bin)
        path = os.path.join(self.bin, "ssh")
        with open(path, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o755)
        return self

    def __exit__(self, *_):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run(self, command, sid):
        payload = {"tool_name": "Bash", "session_id": sid, "tool_input": {"command": command}}
        env = dict(os.environ, SHUNT_CONF=self.dir, PATH=self.bin + os.pathsep + os.environ["PATH"])
        # PYTHONPATH is stripped the way the other hook tests strip it: settings.json
        # names pretool.py by absolute path, so the field never has the package on
        # sys.path (see run_bash in test_pretool_probe.py).
        env.pop("PYTHONPATH", None)
        r = subprocess.run([sys.executable, PRETOOL], input=json.dumps(payload).encode(), capture_output=True, env=env)
        out = r.stdout.decode().strip()
        if not out:
            return ""
        return json.loads(out)["hookSpecificOutput"].get("updatedInput", {}).get("command", "")


class TestNoticeFiresOnceAtSwitch(unittest.TestCase):
    # A real session id is a 36-character UUID - the report's math (and the message's own
    # wording, "the 36-byte session id") assumes exactly that length.
    SID = "1" * 36

    def test_the_switch_carries_the_warning(self):
        with HookConf() as c:
            out = c.run("@long", self.SID)
            self.assertIn("connection-reuse socket path would be", out)
            self.assertIn("@long", out)

    def test_the_byte_count_matches_the_formula(self):
        with HookConf() as c:
            out = c.run("@long", self.SID)
            expected = pretool._control_socket_length(self.SID, HookConf.LONG_TARGET)
            self.assertIn(f"{expected} bytes", out)

    def test_a_short_host_gets_no_warning(self):
        with HookConf() as c:
            out = c.run("@short", self.SID)
            self.assertNotIn("connection-reuse socket path", out)

    def test_control_master_false_silences_the_same_long_target(self):
        with HookConf() as c:
            out = c.run("@longoff", self.SID)
            self.assertNotIn("connection-reuse socket path", out)

    def test_the_warning_does_not_repeat_on_the_next_command(self):
        with HookConf() as c:
            c.run("@long", self.SID)
            out = c.run("ls -la", self.SID)
            self.assertNotIn("connection-reuse socket path", out)

    def test_control_master_false_actually_drops_the_options_from_the_real_command(self):
        """The notice is one half; the other is that the command sent to ssh changed too."""
        with HookConf() as c:
            c.run("@longoff", self.SID)
            out = c.run("ls -la", self.SID)
            self.assertNotIn("ControlMaster", out)
            self.assertNotIn("ControlPersist", out)
            self.assertIn("BatchMode=yes", out)


if __name__ == "__main__":
    unittest.main()
