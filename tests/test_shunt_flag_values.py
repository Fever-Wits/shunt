"""
Tests for shunt.py - `_flag_value`, the one place a flag's missing or unreadable value
is answered.

The REASON lives in the docstring of `shunt._flag_value`; this file is its proof. Retold
here it would be two copies of one argument, and the copy is the one that goes stale.

`read`'s `start:end` is the same shape without a flag, so it is tested here too.

Coverage:
  - every hand refuses instead of raising: --expected, --alias, --key, -n, --name, a:b
  - a line number is an ASCII digit and nothing else: `\u00b2`, `\u0663`, `abc` and empty all get
    the same usage refusal, and a plain `3:5` still builds its awk
  - the refusal carries a usage line and names the flag, so it can be acted on
  - a bad VALUE is refused as loudly as a missing one, and the value is quoted back
  - the two hands that already had a refusal kept their own wording - the shared helper
    did not flatten four messages into one
  - the flag and its value are removed from the list, so neither reaches a host
  - the ordinary shapes still work: a flag with its value, and no flag at all

Nothing here reaches a machine: ssh is stubbed, or the refusal fires before it.
"""

import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Make the src tree importable when run without installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import shunt.cli as shunt_mod


# -- helpers --------------------------------------------------------------------


class TmpHosts:
    """Context manager: temp CONF holding one host in shunt.toml."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-flag-")
        with open(os.path.join(self.dir, "shunt.toml"), "w") as f:
            f.write('[hosts]\nh1 = "root@203.0.113.1"\n')
        self._orig = shunt_mod.CONF
        shunt_mod.CONF = self.dir
        return self

    def __exit__(self, *_):
        shunt_mod.CONF = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)


class RefusalCase(unittest.TestCase):
    """One way to ask "was this refused, and what did it say?", shared by the six hands.

    A base class rather than a module-level function, following the shape the neighbouring
    `bg` tests already use (`TestNameNeedsALabel._refused`): these hands answer through
    die(), which exits, so `self.assertRaises(SystemExit)` is what asks the question.
    Letting the call simply return would assert the opposite of what is wanted - that the
    command carried on.
    """

    def refused(self, fn, argv):
        """Call a subcommand expecting a refusal; return (exit code, stderr)."""
        err = io.StringIO()
        with TmpHosts():
            with patch.object(sys, "stderr", err):
                with patch.object(shunt_mod.subprocess, "run", MagicMock(return_value=MagicMock(returncode=0))):
                    with self.assertRaises(SystemExit) as ctx:
                        fn(argv)
        return ctx.exception.code, err.getvalue()


# -- the two hands that used to raise -------------------------------------------


class TestEditExpected(RefusalCase):
    """`--expected` is the count-and-refuse guard. A fallback to 1 would be worse than the
    traceback it replaced: it is the wrong-place edit the flag exists to prevent."""

    def test_a_trailing_flag_is_refused(self):
        code, err = self.refused(shunt_mod.cmd_edit, ["@h1", "/tmp/f", "old", "new", "--expected"])
        self.assertNotEqual(code, 0)
        self.assertIn("--expected", err)
        self.assertIn("usage:", err)

    def test_a_value_that_is_not_a_number_is_refused(self):
        code, err = self.refused(shunt_mod.cmd_edit, ["@h1", "/tmp/f", "old", "new", "--expected", "abc"])
        self.assertNotEqual(code, 0)
        self.assertIn("'abc'", err)  # quoted back, so the reader sees what was read
        self.assertIn("number", err)

    def test_neither_leaves_a_traceback(self):
        """The failure this file is about: `IndexError` / `ValueError` on stdout."""
        for argv in (
            ["@h1", "/tmp/f", "old", "new", "--expected"],
            ["@h1", "/tmp/f", "old", "new", "--expected", "abc"],
        ):
            with self.subTest(argv=argv):
                _, err = self.refused(shunt_mod.cmd_edit, argv)
                self.assertNotIn("Traceback", err)
                self.assertNotIn("Error", err)


class TestInstallAliasAndKey(RefusalCase):
    def test_a_trailing_alias_is_refused(self):
        code, err = self.refused(shunt_mod.cmd_install, ["root@203.0.113.1", "--alias"])
        self.assertNotEqual(code, 0)
        self.assertIn("--alias", err)

    def test_a_trailing_key_is_refused(self):
        code, err = self.refused(shunt_mod.cmd_install, ["root@203.0.113.1", "--key"])
        self.assertNotEqual(code, 0)
        self.assertIn("--key", err)

    def test_the_refusal_carries_the_usage_line(self):
        _, err = self.refused(shunt_mod.cmd_install, ["root@203.0.113.1", "--alias"])
        self.assertIn("usage: shunt install", err)


class TestReadRange(RefusalCase):
    """No flag, same shape: the two halves of `start:end` went straight into int()."""

    def test_a_non_numeric_range_is_refused(self):
        code, err = self.refused(shunt_mod.cmd_read, ["@h1", "/tmp/f", "a:b"])
        self.assertNotEqual(code, 0)
        self.assertIn("line numbers", err)

    def test_a_three_part_range_is_refused(self):
        """`1:2:3` splits once, so `b` was "2:3" - a ValueError, not a range."""
        code, _ = self.refused(shunt_mod.cmd_read, ["@h1", "/tmp/f", "1:2:3"])
        self.assertNotEqual(code, 0)

    def test_line_numbers_are_ascii_digits_only(self):
        """The contract, written in the guard rather than inherited from `int()`.

        `int()` accepts every Unicode decimal - `\u0663` reads back as 3 - so leaving it to
        decide would make Python's character table shunt's contract, and the next reader
        would go on maintaining a feature nobody chose. `\u00b2` is the mirror: a digit to
        `str.isdigit()` that `int()` refuses. Both are outside `0-9`, so both are refused,
        by the SAME sentence as `abc`.
        """
        for bad in ("\u00b2:3", "1:\u00b2", "\u0663:\u0664", "\u0661\u0662:3"):
            with self.subTest(range=bad):
                code, err = self.refused(shunt_mod.cmd_read, ["@h1", "/tmp/f", bad])
                self.assertNotEqual(code, 0)
                self.assertIn("line numbers", err)
                self.assertNotIn("Traceback", err)

    def test_a_range_of_ascii_digits_is_accepted(self):
        """The other half of the contract: what IS `0-9` still works, unchanged."""
        seen = {}

        def fake_run(a, *args, **kwargs):
            seen["cmd"] = a[-1]
            return MagicMock(returncode=0)

        with TmpHosts():
            with patch.object(shunt_mod.subprocess, "run", fake_run):
                shunt_mod.cmd_read(["@h1", "/tmp/f", "3:5"])
        self.assertIn("NR>=3 && NR<=5", seen["cmd"])


# -- the two hands that already refused: their wording is not collateral damage --


class TestTheOlderRefusalsKeptTheirWords(RefusalCase):
    """A shared helper is only worth it if it costs nothing. These two messages were
    written for a failure someone met; flattening them into one wording would have been
    this refactor quietly taking something away."""

    def test_name_still_asks_for_a_label(self):
        _, err = self.refused(shunt_mod.cmd_bg, ["@h1", "deploy.sh", "--name"])
        self.assertIn("--name", err)
        self.assertIn("label", err)

    def test_n_still_asks_for_a_number(self):
        _, err = self.refused(shunt_mod.cmd_log, ["-n"])
        self.assertIn("-n", err)
        self.assertIn("number", err)

    def test_a_bad_n_is_still_refused_rather_than_narrowed(self):
        """`-n 5OO` (letter O) must not quietly print fifty records."""
        code, err = self.refused(shunt_mod.cmd_log, ["-n", "5OO"])
        self.assertNotEqual(code, 0)
        self.assertIn("number", err)


# -- the pair is removed, so it cannot travel -----------------------------------


class TestTheFlagAndItsValueLeaveTheList(unittest.TestCase):
    """The original `--name` defect was not the missing refusal - it was the flag being
    left in the list and joined into the command sent to the far machine."""

    def test_the_helper_hands_back_the_rest_without_the_pair(self):
        value, rest = shunt_mod._flag_value(["a", "--name", "nightly", "b"], "--name", "usage: ...")
        self.assertEqual(value, "nightly")
        self.assertEqual(rest, ["a", "b"])

    def test_the_cast_is_applied(self):
        value, rest = shunt_mod._flag_value(["-n", "7", "x"], "-n", "usage: ...", int)
        self.assertEqual(value, 7)
        self.assertEqual(rest, ["x"])

    def test_a_label_that_is_present_still_reaches_the_unit_name(self):
        seen = {}

        def fake_run(a, *args, **kwargs):
            seen["cmd"] = a[-1]
            return MagicMock(returncode=0)

        with TmpHosts():
            with patch.object(shunt_mod.subprocess, "run", fake_run):
                shunt_mod.cmd_bg(["@h1", "sleep 60", "--name", "nightly"])
        self.assertIn("--unit=shunt-nightly", seen["cmd"])
        self.assertNotIn("--name", seen["cmd"])


if __name__ == "__main__":
    unittest.main()
