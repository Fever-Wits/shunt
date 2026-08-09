"""
Tests for shunt.cli — the CLI writes to the same audit log as the hook.

Why this file exists: the hook recorded every redirected bash command while the CLI
recorded nothing, so `run` · `edit` · `cp` · `bg` · `get` · `commit` left this machine
without a trace — and `run` is the path we recommend to agents, which made the
recommended path the unaudited one.

Coverage:
  - each of the six host-touching subcommands writes one line
  - the line says WHAT it was (sid=cli + the subcommand), so it cannot be read as bash
    that ran on the host
  - the line keeps the shape the trimmer depends on (a date in the first ten characters)
  - `shunt log` shows it, next to the hook's own lines
  - read-only subcommands stay out of the log
  - the record lands in the CLI's config dir, not in the hook's
  - a log that cannot be written does not break the subcommand

ssh/rsync are stubbed everywhere — no connection is attempted.
"""

import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Make the src tree importable when run without installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import shunt.cli as shunt_mod
from shunt import pretool


class TmpConf:
    """Context manager: CONF (and MANIFEST) in a temp dir holding one host."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-audit-cli-")
        with open(os.path.join(self.dir, "shunt.toml"), "w") as f:
            f.write('[hosts]\nh1 = "root@10.0.0.1"\n')
        self._orig_conf = shunt_mod.CONF
        self._orig_manifest = shunt_mod.MANIFEST
        shunt_mod.CONF = self.dir
        shunt_mod.MANIFEST = os.path.join(self.dir, "checkouts", "manifest.json")
        self.log = os.path.join(self.dir, "audit.log")
        return self

    def __exit__(self, *_):
        shunt_mod.CONF = self._orig_conf
        shunt_mod.MANIFEST = self._orig_manifest
        shutil.rmtree(self.dir, ignore_errors=True)

    def lines(self):
        try:
            with open(self.log) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []


def stub_ssh(returncode=0, stdout=b"", stderr=b""):
    """Patch subprocess.run so nothing leaves the machine."""
    return patch.object(
        shunt_mod.subprocess, "run", return_value=MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)
    )


# ── the six subcommands that reach a host ──────────────────────────────────────


class TestEachSubcommandIsRecorded(unittest.TestCase):
    def test_run_is_recorded(self):
        with TmpConf() as c:
            with stub_ssh():
                shunt_mod.cmd_run(["@h1", "ls -la"])
            self.assertEqual(len(c.lines()), 1)
            line = c.lines()[0]
            self.assertIn("host=h1", line)
            self.assertIn("[run] ls -la", line)

    def test_edit_is_recorded_with_the_file(self):
        with TmpConf() as c:
            with stub_ssh():
                shunt_mod.cmd_edit(["@h1", "/etc/nginx.conf", "OLD", "NEW"])
            self.assertIn("[edit] /etc/nginx.conf", c.lines()[0])

    def test_edit_says_when_it_was_only_a_preview(self):
        """A dry run touched nothing — the log must not suggest otherwise."""
        with TmpConf() as c:
            with stub_ssh():
                shunt_mod.cmd_edit(["@h1", "/etc/nginx.conf", "OLD", "NEW", "--dry-run"])
            self.assertIn("[edit] /etc/nginx.conf (dry-run)", c.lines()[0])

    def test_cp_is_recorded_with_both_sides(self):
        with TmpConf() as c:
            with stub_ssh():
                shunt_mod.cmd_cp(["./local.txt", "@h1:/tmp/there.txt"])
            line = c.lines()[0]
            self.assertIn("[cp] ./local.txt -> @h1:/tmp/there.txt", line)

    def test_bg_start_is_recorded(self):
        with TmpConf() as c:
            with stub_ssh():
                shunt_mod.cmd_bg(["@h1", "make -j8 all", "--name", "nightly"])
            self.assertIn("[bg] make -j8 all --name nightly", c.lines()[0])

    def test_bg_stop_is_recorded(self):
        """Stopping a job changes the far machine too — one line for every shape of bg."""
        with TmpConf() as c:
            with stub_ssh():
                shunt_mod.cmd_bg(["@h1", "--stop", "shunt-nightly"])
            self.assertIn("[bg] --stop shunt-nightly", c.lines()[0])

    def test_get_is_recorded_with_url_and_destination(self):
        with TmpConf() as c:
            with stub_ssh():
                shunt_mod.cmd_get(["@h1", "https://example.com/big.iso", "/opt/dl"])
            self.assertIn("[get] https://example.com/big.iso -> /opt/dl", c.lines()[0])

    def test_commit_is_recorded_per_file(self):
        with TmpConf() as c:
            content = b"edited\n"
            sha = hashlib.sha256(content).hexdigest()
            local = os.path.join(c.dir, "checkouts", "h1", "remote", "file.py")
            os.makedirs(os.path.dirname(local), exist_ok=True)
            with open(local, "wb") as f:
                f.write(content)
            shunt_mod._manifest_save({local: {"host": "h1", "remote": "/remote/file.py", "base_sha": sha}})

            calls = {"n": 0}

            def fake_run(cmd, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:  # sha256sum — no conflict
                    return MagicMock(returncode=0, stderr=b"", stdout=(sha + "  /remote/file.py\n").encode())
                return MagicMock(returncode=0, stderr=b"", stdout=json.dumps({"status": "ok", "new_sha": sha}).encode())

            with (
                patch.object(shunt_mod.subprocess, "run", side_effect=fake_run),
                patch.object(sys, "stdout", io.StringIO()),
            ):
                shunt_mod.cmd_commit([])
            self.assertIn("[commit] /remote/file.py", c.lines()[0])


# ── what stays out ─────────────────────────────────────────────────────────────


class TestReadOnlyStaysOut(unittest.TestCase):
    """The log answers "what left this machine" — a read brings something back."""

    def test_read_is_not_recorded(self):
        with TmpConf() as c:
            with stub_ssh():
                shunt_mod.cmd_read(["@h1", "/etc/hosts"])
            self.assertEqual(c.lines(), [])


# ── the shape of the record ────────────────────────────────────────────────────


class TestRecordShape(unittest.TestCase):
    def test_a_cli_record_cannot_be_read_as_remote_bash(self):
        """`shunt run @h1 ls` and a redirected `ls` are different events."""
        with TmpConf() as c:
            with stub_ssh():
                shunt_mod.cmd_run(["@h1", "ls"])
            line = c.lines()[0]
            self.assertIn("sid=cli", line)
            self.assertIn(":: [run] ", line)

    def test_the_line_keeps_the_date_the_trimmer_reads(self):
        """The trimmer dates the cut from the first ten characters of a line."""
        with TmpConf() as c:
            with stub_ssh():
                shunt_mod.cmd_run(["@h1", "ls"])
            self.assertIsNotNone(pretool._cut_date(c.lines()[0], 2))

    def test_shunt_log_shows_it_next_to_the_hooks_lines(self):
        """Both halves in one place, or nobody reads the second."""
        with TmpConf() as c:
            pretool.audit("sess-9", "h1", "hostname", conf=c.dir)
            with stub_ssh():
                shunt_mod.cmd_run(["@h1", "uptime"])
            with patch.object(sys, "stdout", io.StringIO()) as out:
                shunt_mod.cmd_log([])
            shown = out.getvalue()
            self.assertIn("sid=sess-9 host=h1 :: hostname", shown)
            self.assertIn("sid=cli host=h1 :: [run] uptime", shown)


# ── the seam with the hook ─────────────────────────────────────────────────────


class TestLocationStaysWithTheCaller(unittest.TestCase):
    """The hook's audit() is shared; the config dir is passed, never assumed.

    Otherwise the CLI would write wherever the hook module happened to point — which in
    a test is the real log of whoever is running the tests.
    """

    def test_the_record_lands_in_the_clis_conf_not_the_hooks(self):
        hook_conf = tempfile.mkdtemp(prefix="shunt-test-hook-conf-")
        orig = pretool.CONF
        pretool.CONF = hook_conf
        try:
            with TmpConf() as c:
                with stub_ssh():
                    shunt_mod.cmd_run(["@h1", "ls"])
                self.assertEqual(len(c.lines()), 1)
                self.assertFalse(os.path.exists(os.path.join(hook_conf, "audit.log")))
        finally:
            pretool.CONF = orig
            shutil.rmtree(hook_conf, ignore_errors=True)


class TestFailureIsSilent(unittest.TestCase):
    def test_an_unwritable_log_does_not_break_the_subcommand(self):
        """Auditing must never be the reason a command fails."""
        with TmpConf() as c:
            os.mkdir(c.log)  # a directory where the log belongs → the append fails
            with stub_ssh(returncode=7):
                rc = shunt_mod.cmd_run(["@h1", "false"])
            self.assertEqual(rc, 7)  # the command ran, and its exit code came back


if __name__ == "__main__":
    unittest.main()
