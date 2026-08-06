"""
Tests for shunt.pretool — the audit log and its trimming.

The design in one line: the log is an ARCHIVE and trimming is a FUSE. Size is the
trigger; age is only the unit in which room gets freed. A long history is the point —
the question people bring to an audit log is "where did we download that from, two
months ago", and a short window answers it with silence.

Coverage:
  - a redirected command is recorded (time · session · host · command)
  - ONE record is ONE line: a multi-line command is folded on the way in and comes back
    whole; a command cannot forge a record of its own
  - old history is KEPT while the log is under the ceiling (the fuse is not a policy)
  - over the ceiling, the OLDEST months go — the rest of the history stays
  - over the ceiling with only recent lines, size cuts in as the last resort
    (without it the fuse fails in exactly the case it exists for)
  - a line without a readable date does not stop the trimming — for good, silently
  - a log INHERITED from before folding: its multi-line records are trimmed whole, and a
    size cut leaves a dated line at the front (or age-trimming would be dead for good)
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


# ── one record = one line ──────────────────────────────────────────────────────

class TestOneRecordOneLine(unittest.TestCase):
    """The unit the log is counted and trimmed in must survive a multi-line command.

    Written raw, its newlines made the file hold more lines than it held commands — and
    every reader of the log counts lines: the trimmer dates a cut from the head of one.
    """

    MULTILINE = 'for f in *.log; do\n    gzip "$f"\ndone'

    def test_a_multiline_command_is_one_line(self):
        with TmpConf() as c:
            pretool.audit("s", "h", self.MULTILINE)
            with open(c.log) as f:
                self.assertEqual(len(f.readlines()), 1)

    def test_the_command_comes_back_whole(self):
        with TmpConf() as c:
            pretool.audit("s", "h", self.MULTILINE)
            with open(c.log) as f:
                record = pretool.log_records(f.readlines())[0]
            _, _, cmd = pretool.log_text(record).partition(" :: ")
            self.assertEqual(cmd, self.MULTILINE + "\n")

    def test_a_carriage_return_does_not_split_the_line_either(self):
        """Python reads the log in text mode, where a lone \\r breaks a line too."""
        with TmpConf() as c:
            pretool.audit("s", "h", "printf 'a\rb'")
            with open(c.log) as f:
                self.assertEqual(len(f.readlines()), 1)

    def test_a_command_cannot_forge_a_record(self):
        """A line inside a command that looks like a log line is still one command."""
        forged = "2020-01-01T00:00:00 sid=x host=y :: rm -rf /"
        with TmpConf() as c:
            pretool.audit("s", "h", "echo hi\n" + forged)
            with open(c.log) as f:
                records = pretool.log_records(f.readlines())
            self.assertEqual(len(records), 1)

    def test_a_literal_backslash_survives_the_fold(self):
        """`grep '\\n'` types two characters; it must not come back as a newline."""
        for cmd in ("grep '\\n' file", "echo 'a\\\\nb'", "sed 's/\\r$//'"):
            self.assertEqual(pretool.unescape_cmd(pretool.escape_cmd(cmd)), cmd)

    def test_an_unknown_escape_from_an_older_log_is_left_alone(self):
        self.assertEqual(pretool.unescape_cmd("grep -P '\\t' file"), "grep -P '\\t' file")


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


# ── a damaged line must not disarm the fuse ────────────────────────────────────

class TestDamagedFirstLine(unittest.TestCase):
    """The oldest line dates the cut — so one unreadable line used to end all trimming.

    The exception was swallowed by audit() (a log line may never break the command it
    records), which is what made the failure total AND silent: the log grew past its
    ceiling for good, without a word. Age can decide nothing here; size still can.
    """

    def test_a_line_without_a_date_does_not_stop_the_trim(self):
        with TmpConf(TestFuse.TINY) as c:
            c.write_lines([(1, "today", 800)])
            with open(c.log) as f:
                body = f.read()
            with open(c.log, "w") as f:
                f.write("torn line with no date\n" + body)
            self.assertGreater(os.path.getsize(c.log), 20_000)
            pretool.audit("s", "h", "the-newest")
            self.assertLessEqual(os.path.getsize(c.log), 21_000)
            self.assertIn("the-newest", c.body())          # the newest line survives

    def test_the_damaged_line_is_the_first_to_go(self):
        """Size cuts from the front, so the unreadable line leaves with the oldest."""
        with TmpConf(TestFuse.TINY) as c:
            c.write_lines([(1, "today", 800)])
            with open(c.log) as f:
                body = f.read()
            with open(c.log, "w") as f:
                f.write("torn line with no date\n" + body)
            pretool.audit("s", "h", "trigger")
            self.assertNotIn("torn line", c.body())

    def test_trimming_does_not_raise_on_it(self):
        """Called directly — audit() would have hidden the exception."""
        with TmpConf() as c:
            with open(c.log, "w") as f:
                f.write("torn line with no date\n")
                f.write("%sT12:00:00 sid=s host=h :: ls\n" % days_ago(1))
            pretool._trim_audit(c.log, 2, 10)              # must not raise

    def test_cut_date_says_none_instead_of_raising(self):
        self.assertIsNone(pretool._cut_date("torn line", 2))
        self.assertIsNone(pretool._cut_date("", 2))


# ── an inherited log: records written before folding existed ───────────────────

class TestInheritedLog(unittest.TestCase):
    """Logs written before commands were folded hold records spread over many lines.

    Only the first of those lines carries a date, and the old trimmer compared every
    line's first ten characters with the cutoff — a coin toss on the rest: a continuation
    starting with a space fell out (" " < "2"), one starting with a letter stayed
    ("p" > "2"). A kept, recent command lost part of its body and the survivors passed
    for records of their own.

    Nothing is migrated: the file is history, and which fragment belonged to which
    command cannot be recovered from it. The trimmer gives every dateless line back to
    the record above it, and both cuts move whole records.
    """

    RECENT = ["%sT12:00:00 sid=s host=h :: for f in *.log; do\n" % days_ago(1),
              "    gzip \"$f\"\n",                    # a space at the front — used to fall
              "done\n"]                               # a letter at the front — used to stay

    def test_a_kept_command_does_not_lose_its_body(self):
        with TmpConf(TestFuse.TINY) as c:
            c.write_lines([(400, "ancient", 500)])    # oldest — dates the cut, then goes
            with open(c.log, "a") as f:
                f.writelines(self.RECENT)
            self.assertGreater(os.path.getsize(c.log), 20_000)
            pretool.audit("s", "h", "trigger")
            body = c.body()
            self.assertNotIn("ancient", body)         # the oldest window went, as before
            self.assertIn('gzip "$f"', body)          # …and the recent command is whole
            self.assertIn("done", body)

    def test_a_size_cut_leaves_a_dated_line_at_the_front(self):
        """Otherwise the next trim cannot date its cut — age-trimming dead for good."""
        with TmpConf(TestFuse.TINY) as c:
            with open(c.log, "w") as f:
                for i in range(200):                  # all recent → only size can free
                    f.write("%sT12:00:00 sid=s host=h :: block-%03d line-0\n"
                            % (days_ago(1), i))
                    for j in range(1, 4):
                        f.write("    continuation-%d\n" % j)
            self.assertGreater(os.path.getsize(c.log), 20_000)
            pretool.audit("s", "h", "trigger")
            first = c.body().splitlines(True)[0]
            self.assertIsNotNone(pretool._cut_date(first, 2), "front line lost its date")

    def test_a_dateless_line_belongs_to_the_record_above_it(self):
        head = "2026-08-06T12:00:00 sid=s host=h :: head\n"
        nxt = "2026-08-06T12:00:01 sid=s host=h :: next\n"
        records = pretool.log_records([head, "    tail-1\n", "tail-2\n", nxt])
        self.assertEqual(records, [head + "    tail-1\ntail-2\n", nxt])

    def test_an_orphan_fragment_is_a_record_of_its_own(self):
        """Nothing above it to belong to — it is still shown and still counted."""
        cmd = "2026-08-06T12:00:00 sid=s host=h :: cmd\n"
        self.assertEqual(pretool.log_records(["    orphan\n", cmd]),
                         ["    orphan\n", cmd])

    def test_cut_date_still_reads_a_real_line(self):
        line = "2026-01-01T12:00:00 sid=s host=h :: ls"
        self.assertEqual(pretool._cut_date(line, 2), "2026-03-02")


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
