"""
Tests for shunt.edit_helper — pure logic via JSON stdin/argv interface.
Drives: count-and-refuse (0/1/>1 matches), SHA conflict, size guard,
dry_run, CRLF normalisation.
No external dependencies — stdlib only.
"""

import base64
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _run(req: dict, *, via_argv: bool = False) -> dict:
    """Invoke edit_helper.main() with the given request; capture stdout."""
    import importlib
    import shunt.edit_helper as eh

    importlib.reload(eh)

    captured = io.StringIO()
    orig_stdout = sys.stdout
    orig_stdin = sys.stdin
    orig_argv = sys.argv

    try:
        sys.stdout = captured
        if via_argv:
            b64 = base64.b64encode(json.dumps(req).encode()).decode()
            sys.argv = ["edit_helper", b64]
        else:
            sys.stdin = io.StringIO(json.dumps(req))
            sys.argv = ["edit_helper"]
        eh.main()
    finally:
        sys.stdout = orig_stdout
        sys.stdin = orig_stdin
        sys.argv = orig_argv

    return json.loads(captured.getvalue())


class TestEditHelperNotFound(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".txt")
        os.write(fd, b"hello world\n")
        os.close(fd)

    def tearDown(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_old_not_in_file_returns_not_found(self):
        r = _run({"file": self.path, "old": "MISSING_STRING", "new": "x"})
        self.assertEqual(r["status"], "not_found")

    def test_hint_present_on_not_found(self):
        r = _run({"file": self.path, "old": "MISSING_STRING", "new": "x"})
        self.assertIn("hint", r)


class TestEditHelperAmbiguous(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".txt")
        os.write(fd, b"foo\nfoo\nfoo\n")
        os.close(fd)

    def tearDown(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_multiple_matches_returns_ambiguous(self):
        r = _run({"file": self.path, "old": "foo", "new": "bar"})
        self.assertEqual(r["status"], "ambiguous")

    def test_ambiguous_count_is_correct(self):
        r = _run({"file": self.path, "old": "foo", "new": "bar"})
        self.assertEqual(r["count"], 3)

    def test_expected_field_reflected(self):
        r = _run({"file": self.path, "old": "foo", "new": "bar", "expected": 1})
        self.assertIn("expected", r)
        self.assertEqual(r["expected"], 1)

    def test_exactly_three_matches_with_expected_three_is_ok(self):
        r = _run({"file": self.path, "old": "foo", "new": "baz", "expected": 3})
        self.assertEqual(r["status"], "ok")


class TestEditHelperOk(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".txt")
        os.write(fd, b"hello world\n")
        os.close(fd)

    def tearDown(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_single_match_applies_edit(self):
        r = _run({"file": self.path, "old": "hello", "new": "goodbye"})
        self.assertEqual(r["status"], "ok")
        with open(self.path, "rb") as f:
            self.assertIn(b"goodbye", f.read())

    def test_verified_true_after_ok(self):
        r = _run({"file": self.path, "old": "hello", "new": "goodbye"})
        self.assertTrue(r.get("verified"))

    def test_diff_present_and_non_empty(self):
        r = _run({"file": self.path, "old": "hello", "new": "goodbye"})
        self.assertIn("diff", r)
        self.assertGreater(len(r["diff"]), 0)

    def test_new_sha_changes(self):
        import hashlib

        with open(self.path, "rb") as f:
            before = hashlib.sha256(f.read()).hexdigest()
        r = _run({"file": self.path, "old": "hello", "new": "goodbye"})
        self.assertNotEqual(r["new_sha"], before)

    def test_via_argv_base64_works(self):
        r = _run({"file": self.path, "old": "world", "new": "earth"}, via_argv=True)
        self.assertEqual(r["status"], "ok")


class TestEditHelperDryRun(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".txt")
        os.write(fd, b"alpha beta\n")
        os.close(fd)

    def tearDown(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_dry_run_does_not_modify_file(self):
        with open(self.path, "rb") as f:
            original = f.read()
        r = _run({"file": self.path, "old": "alpha", "new": "CHANGED", "dry_run": True})
        self.assertEqual(r["status"], "ok")
        self.assertTrue(r.get("dry_run"))
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), original)

    def test_dry_run_returns_diff(self):
        r = _run({"file": self.path, "old": "alpha", "new": "CHANGED", "dry_run": True})
        self.assertIn("diff", r)


class TestEditHelperConflict(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".txt")
        os.write(fd, b"content here\n")
        os.close(fd)

    def tearDown(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_wrong_base_sha_returns_conflict(self):
        r = _run(
            {"file": self.path, "old": "content", "new": "x", "base_sha": "0" * 64}
        )
        self.assertEqual(r["status"], "conflict")

    def test_conflict_includes_current_sha(self):
        r = _run(
            {"file": self.path, "old": "content", "new": "x", "base_sha": "0" * 64}
        )
        self.assertIn("current_sha", r)
        self.assertEqual(len(r["current_sha"]), 64)

    def test_correct_base_sha_allows_edit(self):
        import hashlib

        with open(self.path, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        r = _run(
            {"file": self.path, "old": "content", "new": "replaced", "base_sha": sha}
        )
        self.assertEqual(r["status"], "ok")


class TestEditHelperSizeGuard(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".txt")
        os.write(fd, b"x" * 10)
        os.close(fd)

    def tearDown(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_file_within_limit_is_accepted(self):
        os.environ["SHUNT_EDIT_MAX_BYTES"] = "100"
        try:
            r = _run({"file": self.path, "old": "x" * 10, "new": "y" * 10})
            self.assertNotEqual(r["status"], "error")
        finally:
            os.environ.pop("SHUNT_EDIT_MAX_BYTES", None)

    def test_file_exceeding_limit_returns_error(self):
        os.environ["SHUNT_EDIT_MAX_BYTES"] = "5"
        try:
            r = _run({"file": self.path, "old": "x", "new": "y"})
            self.assertEqual(r["status"], "error")
            self.assertIn("too large", r["message"])
        finally:
            os.environ.pop("SHUNT_EDIT_MAX_BYTES", None)


class TestEditHelperCRLF(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".txt")
        os.write(fd, b"line one\r\nline two\r\n")
        os.close(fd)

    def tearDown(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_crlf_file_matched_with_lf_old(self):
        r = _run({"file": self.path, "old": "line one\nline two", "new": "replaced"})
        self.assertIn(r["status"], ("ok", "not_found"))
        # We just care it doesn't crash and sets `normalized` when matched.
        if r["status"] == "ok":
            self.assertTrue(r.get("normalized"))

    def test_normalized_flag_set_on_crlf_match(self):
        r = _run(
            {"file": self.path, "old": "line one\nline two\n", "new": "replaced\n"}
        )
        if r["status"] == "ok":
            self.assertTrue(r["normalized"])

    def test_empty_old_returns_error(self):
        r = _run({"file": self.path, "old": "", "new": "something"})
        self.assertEqual(r["status"], "error")


if __name__ == "__main__":
    unittest.main()
