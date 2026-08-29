"""
Tests for shunt.cli - the self-introduction (`shunt` with no arguments).

Why this file exists: shunt is not an MCP server, so nothing announces it to whoever
reaches for it. The map printed here is the tool's only way to explain itself in one
call, without loading documentation. If it drifts, the tool goes back to being opaque.

Coverage:
  - `shunt`, `shunt help`, `shunt -h`, `shunt --help` all print the map, exit 0
  - asking is not an error: the map goes to STDOUT, not stderr
  - EVERY registered subcommand appears in the map (guards against the map
    falling behind the code)
  - the map carries the sharpest edge: the mode covers bash only
  - an unknown subcommand still fails (exit 2) and points at the map

The CLI is run as `python -m shunt` - the packaged entry point in a subprocess, which
is what an exit code can be read from.
"""

import os
import re
import subprocess
import sys
import unittest

# Make the src tree importable when run without installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import shunt.cli as shunt_mod

# The subprocess needs the same tree, as an absolute path: it starts elsewhere.
SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))


def run_cli(*args):
    r = subprocess.run(
        [sys.executable, "-m", "shunt", *args], capture_output=True, env=dict(os.environ, PYTHONPATH=SRC)
    )
    return r.returncode, r.stdout.decode(), r.stderr.decode()


class TestMapIsPrinted(unittest.TestCase):
    def test_no_arguments_prints_map_but_fails(self):
        """Asking and forgetting both get the map - only the exit code separates them.

        A script that drops its subcommand must not silently "succeed"; a human staring
        at the terminal still deserves to see what the tool can do.
        """
        code, out, _ = run_cli()
        self.assertEqual(code, 2)
        self.assertIn("I want to...", out)

    def test_help_forms_all_work(self):
        for form in ("help", "-h", "--help"):
            code, out, _ = run_cli(form)
            self.assertEqual(code, 0, f"{form} did not exit 0")
            self.assertIn("I want to...", out, f"{form} printed no map")

    def test_map_goes_to_stdout_not_stderr(self):
        """Asking what a tool does is not an error condition."""
        _, out, err = run_cli()
        self.assertIn("shunt", out)
        self.assertEqual(err.strip(), "")


class TestMapMatchesCode(unittest.TestCase):
    """The map must not fall behind the code - that is the failure being prevented."""

    def test_every_subcommand_appears_in_the_map(self):
        with open(shunt_mod.__file__, encoding="utf-8") as f:
            src = f.read()
        registered = set(re.findall(r'"(\w+)": cmd_', src)) - {"help"}
        missing = sorted(c for c in registered if c not in shunt_mod.MAP)
        self.assertEqual(missing, [], f"subcommands missing from the map: {missing}")

    def test_map_warns_about_the_boundary(self):
        """The mode covering bash only is the trap that cost real work - it must be said."""
        self.assertIn("BASH ONLY", shunt_mod.MAP)
        self.assertIn("INHERITS", shunt_mod.MAP)

    def test_map_points_at_the_full_docs(self):
        self.assertIn("README.md", shunt_mod.MAP)
        self.assertIn("ARCHITECTURE.md", shunt_mod.MAP)


class TestUnknownStillFails(unittest.TestCase):
    def test_unknown_subcommand_exits_nonzero(self):
        code, _, err = run_cli("no-such-cmd")
        self.assertNotEqual(code, 0)
        self.assertIn("unknown subcommand", err)

    def test_unknown_subcommand_points_at_the_map(self):
        _, _, err = run_cli("no-such-cmd")
        self.assertIn("shunt", err)


if __name__ == "__main__":
    unittest.main()
