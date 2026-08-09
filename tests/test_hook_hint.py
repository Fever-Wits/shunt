"""
Tests for shunt.cli — the hook line that `shunt install` prints.

What install TELLS you to register must match what the hook actually handles. This is
the only file guarding that seam: the CLI prints a matcher, the hook acts on a set of
tools, and nothing else connects the two.

Coverage:
  - the hint names every tool the hook warns about (not just Bash)
  - the hint is a valid JSON fragment (it is copy-pasted into settings.json)
  - the hint points at THIS installation's pretool.py
"""

import io
import json
import os
import sys
import unittest
from unittest.mock import patch

# Make the src tree importable when run without installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import shunt.cli as shunt_mod
from shunt import pretool


class TestHookHintMatchesReality(unittest.TestCase):
    """What install TELLS you to register must match what the hook actually handles.

    Found 2026-08-06: install printed `"matcher": "Bash"`, so every fresh install lost the
    mode-boundary warnings entirely. They worked locally only because a human had widened
    the matcher by hand — the tool never asked for it.
    """

    def _hint(self):
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            shunt_mod._print_hook_hint()
            return out.getvalue()

    def test_hint_names_every_tool_the_hook_warns_about(self):
        hint = self._hint()
        for tool in ("Bash", "Agent") + pretool.LOCAL_DISK_TOOLS:
            self.assertIn(tool, hint, f"install would not register {tool}")

    def test_hint_is_valid_json_fragment(self):
        """It is copy-pasted into settings.json — a broken quote breaks every hook there."""
        hint = self._hint()
        start = hint.index("{")
        json.loads(hint[start : hint.rindex("}") + 1])  # raises if malformed

    def test_hint_points_at_this_installation(self):
        self.assertIn(shunt_mod.SELF_DIR, self._hint())


if __name__ == "__main__":
    unittest.main()
