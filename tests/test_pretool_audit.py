"""
Tests for shunt.pretool — the audit log and its trimming.

The design in one line: the log is an ARCHIVE and trimming is a FUSE. Size is the
trigger; age is only the unit in which room gets freed. A long history is the point —
the question people bring to an audit log is "where did we download that from, two
months ago", and a short window answers it with silence.

Coverage:
  - a redirected command is recorded (time · session · host · command)
  - old history is KEPT while the log is under the ceiling (the fuse is not a policy)
  - over the ceiling, the OLDEST months go — the rest of the history stays
  - over the ceiling with only recent lines, size cuts in as the last resort
    (without it the fuse fails in exactly the case it exists for)
  - trim_at_mb / drop_months come from shunt.toml; bad or missing values fall back
  - a trim leaves no temp file and no fragment line
  - a broken log directory cannot break the command being recorded
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

# Make the src tree importable when run without installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import shunt.config as shunt_config
import shunt.pretool as pretool


def days_ago(n):
    """ISO date n days back — the shape the log lines carry."""
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - n * 86400))


class TmpConf:
    """Context manager: pretool.CONF in a temp dir, optionally with an [audit] section."""

    def __init__(self, audit_toml=None):
        self.audit_toml = audit_toml

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-audit-")
        self._orig = pretool.CONF
        pretool.CONF = self.dir
        self.log = os.path.join(self.dir, "audit.log")
        if self.audit_toml:
            with open(os.path.join(self.dir, "shunt.toml"), "w") as f:
                f.write(self.audit_toml)
        return self

    def __exit__(self, *_):
        pretool.CONF = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_lines(self, specs):
        """specs = [(days_ago, marker, count), …] — oldest first."""
        with open(self.log, "w") as f:
            for age, marker, count in specs:
                for i in range(count):
                    f.write("%sT12:00:00 sid=s host=h :: %s-%d\n"
                            % (days_ago(age), marker, i))

    def body(self):
        with open(self.log) as f:
            return f.read()


# ── recording ──────────────────────────────────────────────────────────────────

class TestRecords(unittest.TestCase):

    def test_command_is_recorded(self):
        with TmpConf() as c:
            pretool.audit("sess-1", "web-01", "ls -la")
            body = c.body()
            self.assertIn("sid=sess-1", body)
            self.assertIn("host=web-01", body)
            self.assertIn(":: ls -la", body)

    def test_unwritable_conf_does_not_raise(self):
        """An audit line must never break the command it is recording."""
        orig = pretool.CONF
        pretool.CONF = "/proc/nonexistent-on-purpose"
        try:
            pretool.audit("s", "h", "ls")      # must not raise
        finally:
            pretool.CONF = orig


# ── the archive: age alone never triggers anything ─────────────────────────────

class TestArchiveIsKept(unittest.TestCase):
    """This is the inversion: old lines survive as long as there is room for them."""

    def test_year_old_history_survives_under_the_ceiling(self):
        with TmpConf() as c:
            c.write_lines([(400, "ancient", 5), (200, "old", 5), (1, "recent", 5)])
            pretool.audit("s", "h", "trigger")
            body = c.body()
            self.assertIn("ancient", body)   # a year old and still here
            self.assertIn("old", body)
            self.assertIn("recent", body)

    def test_nothing_is_rewritten_under_the_ceiling(self):
        with TmpConf() as c:
            c.write_lines([(400, "ancient", 50)])
            before = os.path.getsize(c.log)
            pretool.audit("s", "h", "one-more")
            self.assertGreater(os.path.getsize(c.log), before)   # it only grew
            self.assertEqual(c.body().count("ancient"), 50)


# ── the fuse: size triggers, age frees ─────────────────────────────────────────

class TestFuse(unittest.TestCase):

    TINY = '[audit]\ntrim_at_mb = 0.02\ndrop_months = 2\n'   # 20 KB ceiling

    def test_oldest_months_go_first(self):
        """Five years in the file → the first two months leave, the rest stays."""
        with TmpConf(self.TINY) as c:
            c.write_lines([(400, "ancient", 300),     # oldest — inside the 2-month cut
                           (330, "middle", 300),      # 70 days later — survives
                           (1, "recent", 300)])
            self.assertGreater(os.path.getsize(c.log), 20_000)
            pretool.audit("s", "h", "trigger")
            body = c.body()
            self.assertNotIn("ancient", body)         # the oldest window went
            self.assertIn("middle", body)             # older history still here
            self.assertIn("recent", body)

    def test_size_cuts_in_when_everything_is_recent(self):
        """The case the fuse exists for: a loop writes a month of lines in an hour."""
        with TmpConf(self.TINY) as c:
            c.write_lines([(1, "today", 800)])        # all within the 2-month cut
            self.assertGreater(os.path.getsize(c.log), 20_000)
            pretool.audit("s", "h", "the-newest")
            self.assertLessEqual(os.path.getsize(c.log), 21_000)
            self.assertIn("the-newest", c.body())     # the newest line survives


# ── settings ───────────────────────────────────────────────────────────────────

class TestSettings(unittest.TestCase):

    def test_defaults_when_no_toml(self):
        with TmpConf() as c:
            s = shunt_config.audit_settings(c.dir)
            self.assertEqual(s["trim_at_mb"], 100)
            self.assertEqual(s["drop_months"], 2)

    def test_values_are_read_from_toml(self):
        with TmpConf('[audit]\ntrim_at_mb = 7\ndrop_months = 5\n') as c:
            s = shunt_config.audit_settings(c.dir)
            self.assertEqual(s["trim_at_mb"], 7)
            self.assertEqual(s["drop_months"], 5)

    def test_partial_section_keeps_the_other_default(self):
        with TmpConf('[audit]\ntrim_at_mb = 7\n') as c:
            s = shunt_config.audit_settings(c.dir)
            self.assertEqual(s["trim_at_mb"], 7)
            self.assertEqual(s["drop_months"], 2)

    def test_nonsense_values_fall_back(self):
        """A bad setting must not be the reason a command fails."""
        with TmpConf('[audit]\ntrim_at_mb = "a lot"\ndrop_months = -3\n') as c:
            s = shunt_config.audit_settings(c.dir)
            self.assertEqual(s["trim_at_mb"], 100)
            self.assertEqual(s["drop_months"], 2)

    def test_broken_toml_falls_back(self):
        with TmpConf('[audit\ntrim_at_mb = ') as c:
            self.assertEqual(shunt_config.audit_settings(c.dir), shunt_config.AUDIT_DEFAULTS)


class TestMonthArithmetic(unittest.TestCase):

    def test_two_months_is_sixty_days(self):
        self.assertEqual(pretool._months_after("2026-01-01", 2), "2026-03-02")

    def test_crosses_a_year(self):
        self.assertEqual(pretool._months_after("2025-12-01", 2)[:4], "2026")

    def test_zero_months_is_the_same_day(self):
        self.assertEqual(pretool._months_after("2026-05-05", 0), "2026-05-05")


# ── mechanics of the rewrite ───────────────────────────────────────────────────

class TestRewriteIsClean(unittest.TestCase):

    def test_no_temp_file_left(self):
        with TmpConf(TestFuse.TINY) as c:
            c.write_lines([(400, "old", 400), (1, "new", 400)])
            pretool.audit("s", "h", "trigger")
            self.assertEqual([f for f in os.listdir(c.dir) if f.endswith(".trim")], [])

    def test_every_surviving_line_is_whole(self):
        with TmpConf(TestFuse.TINY) as c:
            c.write_lines([(400, "old", 400), (1, "new", 400)])
            pretool.audit("s", "h", "trigger")
            for line in c.body().splitlines():
                self.assertIn(" :: ", line, "mangled line: %r" % line[:60])
                self.assertTrue(line[:2] == "20", "line lost its date: %r" % line[:40])


if __name__ == "__main__":
    unittest.main()
