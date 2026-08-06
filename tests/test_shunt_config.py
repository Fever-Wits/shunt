"""
Tests for shunt.config — the host configuration (shunt.toml), read and written.

Why this file exists: the config is what every command depends on, and its failure mode is
SILENT — an address in one file and its identity in another break access without a word the
day only one side is edited. So the contract is tested from both ends: what a file MEANS
when read, and what `shunt install` leaves behind.

Coverage:
  - a bare string is the target; an inline table adds a per-host key
  - the top-level `key` is the default; a per-host `key` wins over it
  - `~` in a key is expanded, so ssh gets a real path
  - a malformed entry (and a broken file) raises instead of resolving to nothing
  - no shunt.toml but a legacy `hosts` file → still works, notice says where the new
    place is, once; a legacy line that does not name ssh is not a host
  - shunt.toml wins when both files exist
  - neither file → no hosts, and the CLI dies with a reason
  - add_host (what `shunt install` writes): creates the file, replaces the same alias
    instead of duplicating it, keeps comments and the other hosts, round-trips
  - the HOOK resolves through the same module (end-to-end, as the harness runs it)

Everything happens in a temp SHUNT_CONF — no real config is read or written.
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

import shunt.cli as shunt_mod
import shunt.config as shunt_config

# The hook under test, as a file: it runs in its own process, so it is invoked by path.
PRETOOL = os.path.join(os.path.dirname(shunt_mod.__file__), "pretool.py")


# ── helpers ────────────────────────────────────────────────────────────────────

class TmpConf:
    """Context manager: an empty temp conf dir, also bound to the CLI's CONF."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-config-")
        self.said = ""                              # whatever the loader put on stderr
        self._orig = shunt_mod.CONF
        shunt_mod.CONF = self.dir
        shunt_config._legacy_notice_said = False    # the notice is once PER PROCESS
        return self

    def __exit__(self, *_):
        shunt_mod.CONF = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, text):
        with open(os.path.join(self.dir, name), "w") as f:
            f.write(text)

    def read(self, name):
        with open(os.path.join(self.dir, name)) as f:
            return f.read()

    def _quiet(self, call):
        """Load with stderr collected into `said` — so a notice is asserted, not scattered."""
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            result = call()
        self.said += err.getvalue()
        return result

    def hosts(self):
        return self._quiet(lambda: shunt_config.load_hosts(self.dir))

    def resolve(self, alias):
        return self._quiet(lambda: shunt_config.resolve(self.dir, alias))


# ── reading shunt.toml ─────────────────────────────────────────────────────────

class TestTomlReading(unittest.TestCase):

    def test_plain_string_is_the_target(self):
        with TmpConf() as c:
            c.write("shunt.toml", '[hosts]\nweb-01 = "user@203.0.113.10"\n')
            self.assertEqual(c.resolve("web-01")["target"], "user@203.0.113.10")

    def test_inline_table_carries_target_and_key(self):
        with TmpConf() as c:
            c.write("shunt.toml",
                    '[hosts]\n'
                    'special = { target = "root@10.0.0.9", key = "/keys/other" }\n')
            host = c.resolve("special")
            self.assertEqual(host["target"], "root@10.0.0.9")
            self.assertEqual(host["key"], "/keys/other")

    def test_leading_at_sign_is_optional(self):
        """`@h1` is how it is typed; the alias is the same host either way."""
        with TmpConf() as c:
            c.write("shunt.toml", '[hosts]\nh1 = "root@10.0.0.1"\n')
            self.assertEqual(c.resolve("@h1"), c.resolve("h1"))

    def test_unknown_alias_is_none(self):
        with TmpConf() as c:
            c.write("shunt.toml", '[hosts]\nh1 = "root@10.0.0.1"\n')
            self.assertIsNone(c.resolve("nope"))

    def test_several_hosts_are_all_read(self):
        with TmpConf() as c:
            c.write("shunt.toml",
                    '[hosts]\na = "root@10.0.0.1"\nb = "root@10.0.0.2"\n')
            self.assertEqual(sorted(c.hosts()), ["a", "b"])


class TestKeys(unittest.TestCase):
    """The whole point of the format: the identity lives WITH the address."""

    def test_top_level_key_is_the_default(self):
        with TmpConf() as c:
            c.write("shunt.toml", 'key = "/keys/default"\n[hosts]\nh1 = "root@10.0.0.1"\n')
            self.assertEqual(c.resolve("h1")["key"], "/keys/default")

    def test_per_host_key_wins_over_the_default(self):
        with TmpConf() as c:
            c.write("shunt.toml",
                    'key = "/keys/default"\n[hosts]\n'
                    'h1 = "root@10.0.0.1"\n'
                    'h2 = { target = "root@10.0.0.2", key = "/keys/special" }\n')
            self.assertEqual(c.resolve("h1")["key"], "/keys/default")
            self.assertEqual(c.resolve("h2")["key"], "/keys/special")

    def test_no_key_anywhere_is_none(self):
        """No key configured → ssh picks its own identity, as before."""
        with TmpConf() as c:
            c.write("shunt.toml", '[hosts]\nh1 = "root@10.0.0.1"\n')
            self.assertIsNone(c.resolve("h1")["key"])

    def test_tilde_is_expanded(self):
        """ssh -i gets a path, not a shell shorthand — nothing expands it later."""
        with TmpConf() as c:
            c.write("shunt.toml", 'key = "~/.ssh/id_test"\n[hosts]\nh1 = "root@10.0.0.1"\n')
            self.assertEqual(c.resolve("h1")["key"],
                             os.path.expanduser("~/.ssh/id_test"))


class TestBrokenConfigIsLoud(unittest.TestCase):
    """Silence is the failure mode here — a broken config must say so."""

    def test_entry_without_target_raises(self):
        with TmpConf() as c:
            c.write("shunt.toml", '[hosts]\nh1 = { key = "/keys/only" }\n')
            with self.assertRaises(ValueError):
                c.hosts()

    def test_entry_of_a_wrong_type_raises(self):
        with TmpConf() as c:
            c.write("shunt.toml", "[hosts]\nh1 = 8766\n")
            with self.assertRaises(ValueError):
                c.hosts()

    def test_invalid_toml_raises(self):
        with TmpConf() as c:
            c.write("shunt.toml", "[hosts\nh1 = ")
            with self.assertRaises(Exception):
                c.hosts()

    def test_cli_dies_with_the_reason(self):
        """The CLI turns it into one message instead of a traceback."""
        with TmpConf() as c:
            c.write("shunt.toml", "[hosts\nh1 = ")
            with patch("sys.stderr", new_callable=io.StringIO) as err:
                with self.assertRaises(SystemExit):
                    shunt_mod.resolve_host("h1")
            self.assertIn("cannot read the host config", err.getvalue())


# ── the legacy `hosts` file ────────────────────────────────────────────────────

class TestLegacyFallback(unittest.TestCase):
    """An existing setup keeps working — migration is the owner's move, not the tool's."""

    def test_legacy_file_is_read_when_no_toml(self):
        with TmpConf() as c:
            c.write("hosts", "h1 ssh root@10.0.0.1\n")
            self.assertEqual(c.resolve("h1")["target"], "root@10.0.0.1")

    def test_legacy_key_option_is_read(self):
        with TmpConf() as c:
            c.write("hosts", "h1 ssh root@10.0.0.1 key=~/.ssh/id_legacy\n")
            self.assertEqual(c.resolve("h1")["key"],
                             os.path.expanduser("~/.ssh/id_legacy"))

    def test_comments_and_blank_lines_are_ignored(self):
        with TmpConf() as c:
            c.write("hosts", "# a comment\n\nh1 ssh root@10.0.0.1\n")
            self.assertEqual(sorted(c.hosts()), ["h1"])

    def test_line_not_naming_ssh_is_not_a_host(self):
        """ssh is the only transport: a line in any other shape carries something that is
        not an ssh target, so it must not silently become one."""
        with TmpConf() as c:
            c.write("hosts", "h1 ssh root@10.0.0.1\nlan not-ssh 10.0.0.9:8766\n")
            self.assertEqual(sorted(c.hosts()), ["h1"])
            self.assertIsNone(c.resolve("lan"))

    def test_skipped_line_is_reported(self):
        """A host that vanishes without a word is exactly the silent failure to avoid."""
        with TmpConf() as c:
            c.write("hosts", "h1 ssh root@10.0.0.1\nlan not-ssh 10.0.0.9:8766\n")
            c.hosts()
            self.assertIn("1 line(s) skipped", c.said)

    def test_nothing_is_reported_when_nothing_is_skipped(self):
        with TmpConf() as c:
            c.write("hosts", "h1 ssh root@10.0.0.1\n")
            c.hosts()
            self.assertNotIn("skipped", c.said)

    def test_toml_wins_when_both_exist(self):
        with TmpConf() as c:
            c.write("hosts", "h1 ssh root@10.0.0.1\n")
            c.write("shunt.toml", '[hosts]\nh1 = "root@10.0.0.99"\n')
            self.assertEqual(c.resolve("h1")["target"], "root@10.0.0.99")

    def test_notice_names_the_new_place(self):
        with TmpConf() as c:
            c.write("hosts", "h1 ssh root@10.0.0.1\n")
            c.hosts()
            self.assertIn(os.path.join(c.dir, "hosts"), c.said)
            self.assertIn(os.path.join(c.dir, "shunt.toml"), c.said)

    def test_notice_is_said_once(self):
        """Once per process — a line before every command would become wallpaper."""
        with TmpConf() as c:
            c.write("hosts", "h1 ssh root@10.0.0.1\n")
            c.hosts()
            c.hosts()
            c.hosts()
            self.assertEqual(c.said.count("legacy host list"), 1)

    def test_nothing_is_migrated_automatically(self):
        """Reading the old file must not write the new one behind the owner's back."""
        with TmpConf() as c:
            c.write("hosts", "h1 ssh root@10.0.0.1\n")
            c.hosts()
            self.assertFalse(os.path.exists(os.path.join(c.dir, "shunt.toml")))


# ── neither file ───────────────────────────────────────────────────────────────

class TestNothingConfigured(unittest.TestCase):

    def test_no_files_means_no_hosts(self):
        with TmpConf() as c:
            self.assertEqual(c.hosts(), {})
            self.assertIsNone(c.resolve("h1"))
            self.assertIsNone(shunt_config.config_path(c.dir))

    def test_cli_dies_on_a_missing_config(self):
        with TmpConf():
            with patch("sys.stderr", new_callable=io.StringIO) as err:
                with self.assertRaises(SystemExit):
                    shunt_mod.resolve_host("@h1")
            self.assertIn("unknown host", err.getvalue())

    def test_hosts_subcommand_dies_and_says_where(self):
        with TmpConf() as c:
            with patch("sys.stderr", new_callable=io.StringIO) as err:
                with self.assertRaises(SystemExit):
                    shunt_mod.cmd_hosts([])
            self.assertIn(os.path.join(c.dir, "shunt.toml"), err.getvalue())


# ── writing: what `shunt install` leaves behind ────────────────────────────────

class TestAddHost(unittest.TestCase):

    def test_creates_the_file(self):
        with TmpConf() as c:
            shunt_config.add_host(c.dir, "h1", "root@10.0.0.1")
            self.assertEqual(c.resolve("h1")["target"], "root@10.0.0.1")

    def test_key_becomes_the_default_of_a_fresh_file(self):
        """The documented idiom: one identity at the top, hosts as bare strings."""
        with TmpConf() as c:
            shunt_config.add_host(c.dir, "h1", "root@10.0.0.1", "~/.ssh/id_test")
            body = c.read("shunt.toml")
            self.assertIn('key = "~/.ssh/id_test"', body)
            self.assertIn('h1 = "root@10.0.0.1"', body)
            self.assertEqual(c.resolve("h1")["key"], os.path.expanduser("~/.ssh/id_test"))

    def test_second_host_is_added_next_to_the_first(self):
        with TmpConf() as c:
            shunt_config.add_host(c.dir, "h1", "root@10.0.0.1")
            shunt_config.add_host(c.dir, "h2", "root@10.0.0.2")
            self.assertEqual(sorted(c.hosts()), ["h1", "h2"])

    def test_same_alias_is_replaced_not_duplicated(self):
        """Idempotent: installing the same machine twice must not fork the truth."""
        with TmpConf() as c:
            shunt_config.add_host(c.dir, "h1", "root@10.0.0.1")
            shunt_config.add_host(c.dir, "h1", "root@10.0.0.7")
            self.assertEqual(c.read("shunt.toml").count("h1 ="), 1)
            self.assertEqual(c.resolve("h1")["target"], "root@10.0.0.7")

    def test_replacement_stays_in_place(self):
        """The line keeps its position, so the comment above it still describes it."""
        with TmpConf() as c:
            c.write("shunt.toml",
                    '[hosts]\n# the fast one\nh1 = "root@10.0.0.1"\nh2 = "root@10.0.0.2"\n')
            shunt_config.add_host(c.dir, "h1", "root@10.0.0.7")
            body = c.read("shunt.toml").splitlines()
            self.assertEqual(body.index('h1 = "root@10.0.0.7"'),
                             body.index("# the fast one") + 1)

    def test_comments_and_other_hosts_survive(self):
        """It is the owner's file — an install may not eat what is written in it."""
        with TmpConf() as c:
            c.write("shunt.toml",
                    '# my machines\n'
                    'key = "/keys/default"\n\n'
                    '[hosts]\n'
                    '# the fast one\n'
                    'h1 = "root@10.0.0.1"\n')
            shunt_config.add_host(c.dir, "h2", "root@10.0.0.2")
            body = c.read("shunt.toml")
            self.assertIn("# my machines", body)
            self.assertIn("# the fast one", body)
            self.assertEqual(c.resolve("h1")["target"], "root@10.0.0.1")
            self.assertEqual(c.resolve("h2")["key"], "/keys/default")

    def test_key_equal_to_the_default_is_not_repeated(self):
        with TmpConf() as c:
            c.write("shunt.toml", 'key = "/keys/default"\n[hosts]\n')
            line = shunt_config.add_host(c.dir, "h1", "root@10.0.0.1", "/keys/default")
            self.assertEqual(line, 'h1 = "root@10.0.0.1"')

    def test_differing_key_is_written_per_host(self):
        with TmpConf() as c:
            c.write("shunt.toml", 'key = "/keys/default"\n[hosts]\n')
            shunt_config.add_host(c.dir, "h1", "root@10.0.0.1", "/keys/other")
            self.assertEqual(c.resolve("h1")["key"], "/keys/other")

    def test_hosts_section_is_created_when_missing(self):
        with TmpConf() as c:
            c.write("shunt.toml", 'key = "/keys/default"\n')
            shunt_config.add_host(c.dir, "h1", "root@10.0.0.1")
            self.assertEqual(c.resolve("h1")["target"], "root@10.0.0.1")

    def test_alias_needing_quotes_stays_one_key(self):
        """A dot in a bare TOML key would nest a table — quote it instead."""
        with TmpConf() as c:
            shunt_config.add_host(c.dir, "srv.one", "root@10.0.0.1")
            self.assertEqual(c.resolve("srv.one")["target"], "root@10.0.0.1")

    def test_install_writes_the_host(self):
        """The wiring: `shunt install` must actually reach add_host."""
        with TmpConf() as c:
            with patch.object(shunt_mod.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, b"Python 3.11.0", b"")
                with patch("sys.stdout", new_callable=io.StringIO):
                    shunt_mod.cmd_install(["root@10.0.0.1", "--alias", "h1",
                                           "--key", "~/.ssh/id_test"])
            self.assertEqual(c.resolve("h1")["target"], "root@10.0.0.1")
            self.assertEqual(c.resolve("h1")["key"], os.path.expanduser("~/.ssh/id_test"))
            # written down as typed, so the file travels between machines
            self.assertIn('"~/.ssh/id_test"', c.read("shunt.toml"))


# ── the hook reads the same config (end-to-end) ────────────────────────────────

class TestHookUsesTheSameConfig(unittest.TestCase):
    """The resolver is shared; the hook runs in its own process, so prove it there."""

    def _run_hook(self, conf_dir, command, sid="s1"):
        payload = {"tool_name": "Bash", "session_id": sid,
                   "tool_input": {"command": command}}
        r = subprocess.run([sys.executable, PRETOOL],
                           input=json.dumps(payload).encode(), capture_output=True,
                           env=dict(os.environ, SHUNT_CONF=conf_dir))
        out = r.stdout.decode().strip()
        if not out:
            return None
        return json.loads(out)["hookSpecificOutput"]["updatedInput"]["command"]

    def test_toml_host_is_rewritten_with_target_and_key(self):
        with TmpConf() as c:
            c.write("shunt.toml",
                    'key = "/keys/default"\n[hosts]\nh1 = "root@10.0.0.1"\n')
            with open(os.path.join(c.dir, "target.s1"), "w") as f:
                f.write("h1")
            cmd = self._run_hook(c.dir, "ls -la")
            self.assertIn("root@10.0.0.1", cmd)
            self.assertIn("-i /keys/default", cmd)

    def test_broken_config_keeps_bash_local(self):
        """A traceback in front of every command would be worse than staying home."""
        with TmpConf() as c:
            c.write("shunt.toml", "[hosts\nh1 = ")
            with open(os.path.join(c.dir, "target.s1"), "w") as f:
                f.write("h1")
            self.assertIsNone(self._run_hook(c.dir, "ls -la"))


if __name__ == "__main__":
    unittest.main()
