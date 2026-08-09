"""
Tests for shunt.edit_helper — the server-side editor by CONTENT, which writes on OTHER
people's machines.

Why the first half exists: the helper decoded the file with `errors="replace"`, edited the
TEXT and wrote the text back — so every byte that was not UTF-8 came back as U+FFFD, and
a mixed-line-ending file was converted whole. Both were reported as `status: ok`,
`verified: true`, with a diff that showed only the line that had been asked for. The
answer was true about the match and false about the file.

So the first thing tested here is the thing the helper is FOR: everything outside the
match must come back byte for byte. Then the matching itself, because that is what the
fix rewrote — an exact match, the two line-ending variants, and the refusals.

The second half drives the SHAPE of the answers — count-and-refuse (0/1/>1 matches), SHA
conflict, size guard, dry_run, the hint and the diff. A caller reads that shape to know
what happened; it must not drift even when the matching underneath is rewritten.

Driven exactly as the CLI calls it: base64 argv (inline-deploy) and JSON on stdin.
No remote host is touched; edit_helper.py runs via subprocess on localhost.
"""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

EDIT_HELPER = os.path.join(os.path.dirname(__file__), "..", "src", "shunt", "edit_helper.py")
PYTHON = sys.executable


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run_via_argv(payload: dict) -> dict:
    """Drive edit_helper via base64 argv (the inline-deploy path the CLI uses)."""
    b64_arg = base64.b64encode(json.dumps(payload).encode()).decode()
    r = subprocess.run([PYTHON, EDIT_HELPER, b64_arg], capture_output=True)
    return json.loads(r.stdout.decode())


def run_via_stdin(payload: dict) -> dict:
    """Drive edit_helper via stdin JSON (interactive path)."""
    r = subprocess.run([PYTHON, EDIT_HELPER], input=json.dumps(payload).encode(), capture_output=True)
    return json.loads(r.stdout.decode())


class EditCase(unittest.TestCase):
    """A temp file to edit, written and read as BYTES — the unit that matters here."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="shunt-test-edit-helper-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def make(self, content: bytes, name="target.conf") -> str:
        path = os.path.join(self.tmpdir, name)
        with open(path, "wb") as f:
            f.write(content)
        return path

    def bytes_of(self, path: str) -> bytes:
        with open(path, "rb") as f:
            return f.read()


# ── the defect: bytes outside the match must survive ───────────────────────────


class TestNonUtf8BytesSurvive(EditCase):
    """A config with one latin-1 byte in a comment is an ordinary file, not an exotic one."""

    ORIGINAL = b"# caf\xe9 comment\nlisten 80;\n"

    def test_the_edit_changes_only_what_was_asked_for(self):
        path = self.make(self.ORIGINAL)

        result = run_via_argv({"file": path, "old": "listen 80;", "new": "listen 8080;", "expected": 1})

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(self.bytes_of(path), b"# caf\xe9 comment\nlisten 8080;\n")

    def test_the_invalid_byte_is_not_replaced_by_the_unicode_marker(self):
        """Named on its own: U+FFFD (\\xef\\xbf\\xbd) in the file is the corruption."""
        path = self.make(self.ORIGINAL)

        run_via_argv({"file": path, "old": "listen 80;", "new": "listen 8080;", "expected": 1})

        self.assertNotIn(b"\xef\xbf\xbd", self.bytes_of(path))
        self.assertIn(b"caf\xe9", self.bytes_of(path))

    def test_ok_means_ok_the_answer_matches_the_disk(self):
        """`verified` and `new_sha` describe the file, or they are decoration."""
        path = self.make(self.ORIGINAL)

        result = run_via_argv({"file": path, "old": "listen 80;", "new": "listen 8080;", "expected": 1})

        self.assertTrue(result["verified"], result)
        self.assertEqual(result["new_sha"], sha256(self.bytes_of(path)))

    def test_a_needle_that_is_not_utf8_is_refused_not_guessed(self):
        """The match is expressed in JSON, so it can only be UTF-8. Say no, do not damage.

        This is the honest edge of the byte path: a caller who must edit latin-1 TEXT has
        `shunt checkout` / `shunt commit`, which never decode at all.
        """
        path = self.make(self.ORIGINAL)

        result = run_via_argv({"file": path, "old": "café comment", "new": "cafe comment", "expected": 1})

        self.assertEqual(result["status"], "not_found", result)
        self.assertEqual(self.bytes_of(path), self.ORIGINAL)


class TestMixedLineEndingsSurvive(EditCase):
    """A file with both styles in it — the case the whole-file conversion silently ate."""

    ORIGINAL = b"alpha\r\nlisten 80;\r\nbeta\ngamma\n"

    def test_lines_nobody_asked_about_keep_their_endings(self):
        """The old is written with LF, the match lives in a CRLF region, the tail is LF."""
        path = self.make(self.ORIGINAL)

        result = run_via_argv({"file": path, "old": "alpha\nlisten 80;", "new": "alpha\nlisten 8080;", "expected": 1})

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(self.bytes_of(path), b"alpha\r\nlisten 8080;\r\nbeta\ngamma\n")

    def test_the_matched_region_keeps_the_style_it_was_found_in(self):
        """Matched as CRLF → written back as CRLF; the replacement is not smuggled in as LF."""
        path = self.make(self.ORIGINAL)

        run_via_argv({"file": path, "old": "alpha\nlisten 80;", "new": "alpha\nlisten 8080;", "expected": 1})

        self.assertIn(b"listen 8080;\r\n", self.bytes_of(path))

    def test_the_diff_describes_the_write_that_actually_happens(self):
        """The diff used to be computed BEFORE the conversion, which is how it hid it.

        Applied to the file, the diff's '-' and '+' lines must be the whole story: what
        is not in the diff is not changed on disk.
        """
        path = self.make(self.ORIGINAL)

        result = run_via_argv({"file": path, "old": "alpha\nlisten 80;", "new": "alpha\nlisten 8080;", "expected": 1})

        removed = [
            ln[1:] for ln in result["diff"].splitlines(keepends=True) if ln.startswith("-") and not ln.startswith("---")
        ]
        added = [
            ln[1:] for ln in result["diff"].splitlines(keepends=True) if ln.startswith("+") and not ln.startswith("+++")
        ]
        after = self.ORIGINAL.decode("utf-8")
        for line in removed:
            after = after.replace(line, "", 1)
        self.assertEqual(added, ["listen 8080;\r\n"])
        self.assertEqual(removed, ["listen 80;\r\n"])
        self.assertEqual(self.bytes_of(path).decode("utf-8"), after.replace("alpha\r\n", "alpha\r\n" + added[0], 1))


# ── the matching itself (what the fix rewrote) ─────────────────────────────────


class TestMatching(EditCase):
    def test_exact_match_needs_no_normalisation(self):
        path = self.make(b"listen 80;\n")

        result = run_via_argv({"file": path, "old": "listen 80;", "new": "listen 8080;", "expected": 1})

        self.assertEqual(result["status"], "ok", result)
        self.assertIs(result["normalized"], False)
        self.assertEqual(self.bytes_of(path), b"listen 8080;\n")

    def test_crlf_file_is_matched_by_an_lf_old(self):
        path = self.make(b"line one\r\nline two\r\n")

        result = run_via_argv({"file": path, "old": "line one\nline two", "new": "replaced"})

        self.assertEqual(result["status"], "ok", result)
        self.assertIs(result["normalized"], True)
        self.assertEqual(self.bytes_of(path), b"replaced\r\n")

    def test_lf_file_is_matched_by_a_crlf_old(self):
        """The other direction: the caller's clipboard had CRLF, the file does not."""
        path = self.make(b"line one\nline two\n")

        result = run_via_argv({"file": path, "old": "line one\r\nline two", "new": "replaced"})

        self.assertEqual(result["status"], "ok", result)
        self.assertIs(result["normalized"], True)
        self.assertEqual(self.bytes_of(path), b"replaced\n")

    def test_no_match_leaves_the_file_alone(self):
        original = b"listen 80;\n"
        path = self.make(original)

        result = run_via_argv({"file": path, "old": "listen 443;", "new": "listen 8443;"})

        self.assertEqual(result["status"], "not_found", result)
        self.assertEqual(self.bytes_of(path), original)

    def test_two_matches_where_one_was_expected_are_refused(self):
        original = b"listen 80;\nlisten 80;\n"
        path = self.make(original)

        result = run_via_argv({"file": path, "old": "listen 80;", "new": "listen 8080;", "expected": 1})

        self.assertEqual(result["status"], "ambiguous", result)
        self.assertEqual(result["count"], 2)
        self.assertEqual(self.bytes_of(path), original)

    def test_two_matches_expected_are_both_replaced(self):
        path = self.make(b"listen 80;\nlisten 80;\n")

        result = run_via_argv({"file": path, "old": "listen 80;", "new": "listen 8080;", "expected": 2})

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(self.bytes_of(path), b"listen 8080;\nlisten 8080;\n")

    def test_dry_run_answers_without_writing(self):
        original = b"listen 80;\n"
        path = self.make(original)

        result = run_via_argv({"file": path, "old": "listen 80;", "new": "listen 8080;", "dry_run": True})

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["new_sha"], sha256(b"listen 8080;\n"))
        self.assertEqual(self.bytes_of(path), original)

    def test_a_stale_base_sha_is_a_conflict_not_a_write(self):
        original = b"listen 80;\n"
        path = self.make(original)

        result = run_via_argv({"file": path, "old": "listen 80;", "new": "listen 8080;", "base_sha": "a" * 64})

        self.assertEqual(result["status"], "conflict", result)
        self.assertEqual(result["current_sha"], sha256(original))
        self.assertEqual(self.bytes_of(path), original)

    def test_the_stdin_path_edits_the_same_way(self):
        """The CLI uses argv; a person at a terminal uses stdin. One behaviour."""
        path = self.make(b"# caf\xe9\nlisten 80;\n")

        result = run_via_stdin({"file": path, "old": "listen 80;", "new": "listen 8080;", "expected": 1})

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(self.bytes_of(path), b"# caf\xe9\nlisten 8080;\n")


# ── the shape of the answer (what a caller reads to know what happened) ────────


class TestEditHelperNotFound(EditCase):
    def test_hint_present_on_not_found(self):
        path = self.make(b"hello world\n")
        r = run_via_stdin({"file": path, "old": "MISSING_STRING", "new": "x"})
        self.assertIn("hint", r)


class TestEditHelperAmbiguous(EditCase):
    def setUp(self):
        super().setUp()
        self.path = self.make(b"foo\nfoo\nfoo\n")

    def test_ambiguous_count_is_correct(self):
        r = run_via_stdin({"file": self.path, "old": "foo", "new": "bar"})
        self.assertEqual(r["count"], 3)

    def test_expected_field_reflected(self):
        r = run_via_stdin({"file": self.path, "old": "foo", "new": "bar", "expected": 1})
        self.assertIn("expected", r)
        self.assertEqual(r["expected"], 1)

    def test_exactly_three_matches_with_expected_three_is_ok(self):
        r = run_via_stdin({"file": self.path, "old": "foo", "new": "baz", "expected": 3})
        self.assertEqual(r["status"], "ok")


class TestEditHelperOk(EditCase):
    def setUp(self):
        super().setUp()
        self.path = self.make(b"hello world\n")

    def test_verified_true_after_ok(self):
        r = run_via_stdin({"file": self.path, "old": "hello", "new": "goodbye"})
        self.assertTrue(r.get("verified"))

    def test_diff_present_and_non_empty(self):
        r = run_via_stdin({"file": self.path, "old": "hello", "new": "goodbye"})
        self.assertIn("diff", r)
        self.assertGreater(len(r["diff"]), 0)

    def test_new_sha_changes(self):
        before = sha256(self.bytes_of(self.path))
        r = run_via_stdin({"file": self.path, "old": "hello", "new": "goodbye"})
        self.assertNotEqual(r["new_sha"], before)

    def test_via_argv_base64_works(self):
        r = run_via_argv({"file": self.path, "old": "world", "new": "earth"})
        self.assertEqual(r["status"], "ok")

    def test_empty_old_returns_error(self):
        r = run_via_stdin({"file": self.path, "old": "", "new": "something"})
        self.assertEqual(r["status"], "error")


class TestEditHelperDryRun(EditCase):
    def test_dry_run_returns_diff(self):
        path = self.make(b"alpha beta\n")
        r = run_via_stdin({"file": path, "old": "alpha", "new": "CHANGED", "dry_run": True})
        self.assertIn("diff", r)


class TestEditHelperConflict(EditCase):
    def test_conflict_includes_current_sha(self):
        path = self.make(b"content here\n")
        r = run_via_stdin({"file": path, "old": "content", "new": "x", "base_sha": "0" * 64})
        self.assertIn("current_sha", r)
        self.assertEqual(len(r["current_sha"]), 64)

    def test_correct_base_sha_allows_edit(self):
        path = self.make(b"content here\n")
        r = run_via_stdin({"file": path, "old": "content", "new": "replaced", "base_sha": sha256(b"content here\n")})
        self.assertEqual(r["status"], "ok")


class TestEditHelperSizeGuard(EditCase):
    """The cap is on the PUSH direction — a file too big to hand over gets a reason."""

    def setUp(self):
        super().setUp()
        self.path = self.make(b"x" * 10)

    def test_file_within_limit_is_accepted(self):
        os.environ["SHUNT_EDIT_MAX_BYTES"] = "100"
        try:
            r = run_via_stdin({"file": self.path, "old": "x" * 10, "new": "y" * 10})
            self.assertNotEqual(r["status"], "error")
        finally:
            os.environ.pop("SHUNT_EDIT_MAX_BYTES", None)

    def test_file_exceeding_limit_returns_error(self):
        os.environ["SHUNT_EDIT_MAX_BYTES"] = "5"
        try:
            r = run_via_stdin({"file": self.path, "old": "x", "new": "y"})
            self.assertEqual(r["status"], "error")
            self.assertIn("too large", r["message"])
        finally:
            os.environ.pop("SHUNT_EDIT_MAX_BYTES", None)


if __name__ == "__main__":
    unittest.main()
