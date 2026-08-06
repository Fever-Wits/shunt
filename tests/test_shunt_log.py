"""
Tests for shunt.cli — the `log` subcommand (reading the audit log back).

The log THINKS in records: one recorded command each. `-n N` therefore counts commands,
not physical lines — a distinction with no cost while every command was one line, and a
lie the moment one of them spanned several.

Coverage:
  - a folded command is printed the way it was typed, not with its escapes showing
  - a command that contains a line looking like a log line is still ONE record
  - -n N counts RECORDS, in an inherited log too (where a command spans lines)
  - the newest records are the ones shown
  - the round trip: what was recorded is what comes out

Nothing leaves the machine here — the log is written directly.
"""
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

# Make the src tree importable when run without installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import shunt.cli as shunt_mod
import shunt.pretool as pretool


class TmpConf:
    """Context manager: CONF in a temp dir, with helpers to write and read the log."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-log-")
        self._orig = shunt_mod.CONF
        shunt_mod.CONF = self.dir
        self.log = os.path.join(self.dir, "audit.log")
        return self

    def __exit__(self, *_):
        shunt_mod.CONF = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, text):
        with open(self.log, "w") as f:
            f.write(text)

    def record(self, cmd):
        """One command through the writing side — the shape the log really holds."""
        pretool.audit("sess-1", "h1", cmd, conf=self.dir)


def shown(argv):
    with patch.object(sys, "stdout", io.StringIO()) as out:
        shunt_mod.cmd_log(argv)
    return out.getvalue()


# ── what a human sees ──────────────────────────────────────────────────────────

class TestUnfolding(unittest.TestCase):

    def test_a_folded_command_is_printed_as_it_was_typed(self):
        with TmpConf() as c:
            c.write("2026-08-06T12:00:00 sid=s host=h1 :: for f in *; do\\n    ls\\ndone\n")
            out = shown([])
            self.assertIn("for f in *; do\n    ls\ndone\n", out)
            self.assertNotIn("\\n", out)               # no escapes left in a human's face

    def test_the_round_trip_gives_the_command_back(self):
        cmd = 'cat <<EOF\nline one\n  line two\nEOF'
        with TmpConf() as c:
            c.record(cmd)
            _, _, printed = shown([]).partition(" :: ")
            self.assertEqual(printed, cmd + "\n")

    def test_a_literal_backslash_is_not_turned_into_a_newline(self):
        with TmpConf() as c:
            c.record("grep '\\n' /var/log/syslog")
            self.assertIn("grep '\\n' /var/log/syslog", shown([]))


# ── counting ───────────────────────────────────────────────────────────────────

class TestNCountsRecords(unittest.TestCase):

    def test_n_counts_commands_not_lines(self):
        with TmpConf() as c:
            for i in range(5):
                c.record("cmd-%d\nsecond line\nthird line" % i)
            out = shown(["-n", "2"])
            self.assertIn("cmd-3", out)
            self.assertIn("cmd-4", out)
            self.assertNotIn("cmd-2", out)             # exactly two commands, not two lines

    def test_an_inherited_multiline_record_is_shown_whole(self):
        """Written before folding existed: its dateless lines belong to it, not to N."""
        with TmpConf() as c:
            c.write("2026-08-06T12:00:00 sid=s host=h1 :: echo older\n"
                    "2026-08-06T12:00:01 sid=s host=h1 :: for f in *; do\n"
                    "    gzip \"$f\"\n"
                    "done\n")
            out = shown(["-n", "1"])
            self.assertIn("for f in *; do", out)       # the head of the last command…
            self.assertIn('gzip "$f"', out)            # …and all of its body
            self.assertIn("done", out)
            self.assertNotIn("echo older", out)        # one command means one command

    def test_a_command_that_looks_like_a_log_line_cannot_forge_a_record(self):
        forged = "2020-01-01T00:00:00 sid=x host=y :: rm -rf /"
        with TmpConf() as c:
            c.record("echo hi\n" + forged)
            out = shown(["-n", "1"])
            self.assertIn("echo hi", out)              # the forged line did not push the
            self.assertIn(forged, out)                 # real one out of a one-record view


if __name__ == "__main__":
    unittest.main()
