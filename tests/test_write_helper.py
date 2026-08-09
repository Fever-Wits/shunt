"""
Tests for shunt.write_helper — server-side full-file writer with optimistic SHA lock.

Drives write_helper.py exactly as shunt.cli will call it:
  - via base64 argv (inline-deploy mode)
  - via stdin JSON (interactive mode)

No remote host is touched. write_helper.py is executed via subprocess on localhost.
"""

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

WRITE_HELPER = os.path.join(os.path.dirname(__file__), "..", "src", "shunt", "write_helper.py")
PYTHON = sys.executable


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def run_via_argv(payload: dict) -> dict:
    """Drive write_helper via base64 argv (the inline-deploy path shunt.cli uses)."""
    b64_arg = base64.b64encode(json.dumps(payload).encode()).decode()
    r = subprocess.run(
        [PYTHON, WRITE_HELPER, b64_arg],
        capture_output=True,
        text=True,
    )
    return json.loads(r.stdout.strip())


def run_via_stdin(payload: dict) -> dict:
    """Drive write_helper via stdin JSON (interactive path)."""
    r = subprocess.run(
        [PYTHON, WRITE_HELPER],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    return json.loads(r.stdout.strip())


class TestWriteHelperNewFile(unittest.TestCase):
    """AC-1: new file (base_sha=null) → created, verified ok."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_new_file_created_argv(self):
        """New file, base_sha=null, via argv → status ok, file on disk, sha matches."""
        content = b"hello shunt\n"
        path = os.path.join(self.tmpdir, "new_file.txt")
        self.assertFalse(os.path.exists(path))

        result = run_via_argv(
            {
                "file": path,
                "content_b64": b64(content),
                "base_sha": None,
            }
        )

        self.assertEqual(result["status"], "ok", result)
        self.assertTrue(result.get("verified"), result)
        self.assertEqual(result["new_sha"], sha256(content))
        self.assertTrue(os.path.exists(path))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), content)

    def test_new_file_created_stdin(self):
        """New file, base_sha=null, via stdin → status ok, file on disk."""
        content = b"stdin path test\n"
        path = os.path.join(self.tmpdir, "new_stdin.txt")
        self.assertFalse(os.path.exists(path))

        result = run_via_stdin(
            {
                "file": path,
                "content_b64": b64(content),
                "base_sha": None,
            }
        )

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["new_sha"], sha256(content))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), content)

    def test_new_file_in_new_subdir(self):
        """File in a directory that does not yet exist → directories created, file written."""
        content = b"nested content\n"
        path = os.path.join(self.tmpdir, "a", "b", "c", "nested.txt")
        self.assertFalse(os.path.exists(path))

        result = run_via_argv(
            {
                "file": path,
                "content_b64": b64(content),
                "base_sha": None,
            }
        )

        self.assertEqual(result["status"], "ok", result)
        self.assertTrue(os.path.exists(path))

    def test_new_file_empty_content(self):
        """New file with empty content (edge case: 0-byte file) → ok."""
        content = b""
        path = os.path.join(self.tmpdir, "empty.txt")

        result = run_via_argv(
            {
                "file": path,
                "content_b64": b64(content),
                "base_sha": None,
            }
        )

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["new_sha"], sha256(content))
        self.assertEqual(os.path.getsize(path), 0)

    def test_new_file_binary_content(self):
        """New file with binary (non-UTF-8) content → ok."""
        content = bytes(range(256))
        path = os.path.join(self.tmpdir, "binary.bin")

        result = run_via_argv(
            {
                "file": path,
                "content_b64": b64(content),
                "base_sha": None,
            }
        )

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["new_sha"], sha256(content))

    def test_verified_flag_is_true(self):
        """AC-1: verified=true in success response."""
        content = b"verified flag test\n"
        path = os.path.join(self.tmpdir, "verify.txt")

        result = run_via_argv(
            {
                "file": path,
                "content_b64": b64(content),
                "base_sha": None,
            }
        )

        self.assertIs(result.get("verified"), True, result)


class TestWriteHelperExistingFileCorrectSha(unittest.TestCase):
    """AC-2: existing file, correct base_sha → atomic overwrite, new_sha correct, verified."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_file(self, content: bytes) -> str:
        path = os.path.join(self.tmpdir, "existing.txt")
        with open(path, "wb") as f:
            f.write(content)
        return path

    def test_overwrite_correct_sha_argv(self):
        """Existing file, correct base_sha via argv → overwrite succeeds."""
        old_content = b"original content\n"
        new_content = b"updated content - version 2\n"
        path = self._make_file(old_content)
        base_sha = sha256(old_content)

        result = run_via_argv(
            {
                "file": path,
                "content_b64": b64(new_content),
                "base_sha": base_sha,
            }
        )

        self.assertEqual(result["status"], "ok", result)
        self.assertTrue(result.get("verified"), result)
        self.assertEqual(result["new_sha"], sha256(new_content))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), new_content)

    def test_overwrite_correct_sha_stdin(self):
        """Existing file, correct base_sha via stdin → overwrite succeeds."""
        old_content = b"original stdin\n"
        new_content = b"updated via stdin\n"
        path = self._make_file(old_content)
        base_sha = sha256(old_content)

        result = run_via_stdin(
            {
                "file": path,
                "content_b64": b64(new_content),
                "base_sha": base_sha,
            }
        )

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["new_sha"], sha256(new_content))

    def test_new_sha_is_sha_of_new_content(self):
        """new_sha in response matches sha256 of written content."""
        old_content = b"old\n"
        new_content = b"new content body\n"
        path = self._make_file(old_content)

        result = run_via_argv(
            {
                "file": path,
                "content_b64": b64(new_content),
                "base_sha": sha256(old_content),
            }
        )

        self.assertEqual(result["new_sha"], sha256(new_content))
        # also verify via independent read
        with open(path, "rb") as f:
            on_disk = f.read()
        self.assertEqual(sha256(on_disk), result["new_sha"])

    def test_overwrite_preserves_permissions(self):
        """Overwrite preserves the file mode of the original (mode 0o644)."""
        old_content = b"perm test\n"
        path = self._make_file(old_content)
        os.chmod(path, 0o644)
        original_mode = os.stat(path).st_mode & 0o777

        result = run_via_argv(
            {
                "file": path,
                "content_b64": b64(b"new\n"),
                "base_sha": sha256(old_content),
            }
        )

        self.assertEqual(result["status"], "ok", result)
        new_mode = os.stat(path).st_mode & 0o777
        self.assertEqual(new_mode, original_mode)

    def test_sequential_overwrites(self):
        """Two sequential correct-sha writes both succeed, sha tracks correctly."""
        content1 = b"version 1\n"
        content2 = b"version 2\n"
        content3 = b"version 3\n"
        path = self._make_file(content1)

        r1 = run_via_argv(
            {
                "file": path,
                "content_b64": b64(content2),
                "base_sha": sha256(content1),
            }
        )
        self.assertEqual(r1["status"], "ok")

        r2 = run_via_argv(
            {
                "file": path,
                "content_b64": b64(content3),
                "base_sha": r1["new_sha"],  # use the returned sha as next base
            }
        )
        self.assertEqual(r2["status"], "ok")
        self.assertEqual(r2["new_sha"], sha256(content3))


class TestWriteHelperWrongSha(unittest.TestCase):
    """AC-3: existing file, WRONG base_sha → status 'conflict', file UNCHANGED."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_file(self, content: bytes) -> str:
        path = os.path.join(self.tmpdir, "conflict.txt")
        with open(path, "wb") as f:
            f.write(content)
        return path

    def test_wrong_sha_returns_conflict(self):
        """Wrong base_sha → status=conflict."""
        content = b"real content\n"
        path = self._make_file(content)
        wrong_sha = "a" * 64  # obviously wrong

        result = run_via_argv(
            {
                "file": path,
                "content_b64": b64(b"attacker content\n"),
                "base_sha": wrong_sha,
            }
        )

        self.assertEqual(result["status"], "conflict", result)

    def test_wrong_sha_file_unchanged(self):
        """Wrong base_sha → file on disk is unchanged."""
        content = b"must not be overwritten\n"
        path = self._make_file(content)
        wrong_sha = "b" * 64

        run_via_argv(
            {
                "file": path,
                "content_b64": b64(b"should not appear\n"),
                "base_sha": wrong_sha,
            }
        )

        with open(path, "rb") as f:
            self.assertEqual(f.read(), content)

    def test_conflict_contains_current_sha(self):
        """Conflict response includes current_sha of the actual file."""
        content = b"current content\n"
        path = self._make_file(content)
        expected_current_sha = sha256(content)

        result = run_via_argv(
            {
                "file": path,
                "content_b64": b64(b"new\n"),
                "base_sha": "0" * 64,
            }
        )

        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result.get("current_sha"), expected_current_sha)

    def test_conflict_contains_provided_base_sha(self):
        """Conflict response echoes back the base_sha we provided."""
        content = b"stable content\n"
        path = self._make_file(content)
        provided = "c" * 64

        result = run_via_argv(
            {
                "file": path,
                "content_b64": b64(b"new\n"),
                "base_sha": provided,
            }
        )

        self.assertEqual(result.get("base_sha"), provided)

    def test_stale_sha_one_version_behind(self):
        """base_sha that was valid one write ago → conflict (realistic stale-checkout scenario)."""
        content_v1 = b"version 1\n"
        content_v2 = b"version 2\n"
        path = self._make_file(content_v1)
        sha_v1 = sha256(content_v1)

        # advance the file to v2 with a correct write
        run_via_argv(
            {
                "file": path,
                "content_b64": b64(content_v2),
                "base_sha": sha_v1,
            }
        )

        # now attempt to write using the old sha_v1 → conflict
        result = run_via_argv(
            {
                "file": path,
                "content_b64": b64(b"attacker\n"),
                "base_sha": sha_v1,
            }
        )

        self.assertEqual(result["status"], "conflict")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), content_v2)  # file still at v2

    def test_base_sha_supplied_but_file_missing(self):
        """base_sha provided but file does not exist → conflict (current_sha=None)."""
        path = os.path.join(self.tmpdir, "does_not_exist.txt")
        self.assertFalse(os.path.exists(path))

        result = run_via_argv(
            {
                "file": path,
                "content_b64": b64(b"new\n"),
                "base_sha": "d" * 64,  # wrong sha for a non-existent file
            }
        )

        # current_sha of a missing file is None; base_sha is non-null → mismatch → conflict
        self.assertEqual(result["status"], "conflict")
        # file must NOT be created
        self.assertFalse(os.path.exists(path))


class TestWriteHelperSizeGuard(unittest.TestCase):
    """AC-4: oversized content → error, no file written."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _env_with_limit(self, max_bytes: int) -> dict:
        env = os.environ.copy()
        env["SHUNT_EDIT_MAX_BYTES"] = str(max_bytes)
        return env

    def test_oversized_b64_payload_rejected(self):
        """b64 payload that exceeds 2× MAX_BYTES limit → error before decoding.

        The guard is: len(content_b64) > MAX * 2.
        b64 expands by 4/3, so we need content_len > 1.5 * MAX (= 151 bytes when MAX=100).
        """
        max_bytes = 100
        # 151 bytes → b64 is 204 chars > 2*100=200: triggers the first guard
        content = b"X" * 151
        b64_str = b64(content)
        # verify precondition: this really does exceed the guard threshold
        self.assertGreater(len(b64_str), max_bytes * 2)

        path = os.path.join(self.tmpdir, "oversized.txt")
        payload_b64 = base64.b64encode(
            json.dumps(
                {
                    "file": path,
                    "content_b64": b64_str,
                    "base_sha": None,
                }
            ).encode()
        ).decode()

        env = self._env_with_limit(max_bytes)
        r = subprocess.run(
            [PYTHON, WRITE_HELPER, payload_b64],
            capture_output=True,
            text=True,
            env=env,
        )
        result = json.loads(r.stdout.strip())

        self.assertEqual(result["status"], "error")
        self.assertIn("too large", result["message"].lower(), result)
        self.assertFalse(os.path.exists(path))

    def test_oversized_decoded_bytes_rejected(self):
        """Content whose decoded size exceeds MAX_BYTES but b64 passes first check → error."""
        # The first guard triggers when len(b64) > MAX*2.
        # The second guard is len(decoded) > MAX.
        # To trigger only the second guard we need len(b64) <= MAX*2 but len(decoded) > MAX.
        # That is geometrically impossible (b64 is always ≥ decoded), so we instead
        # verify that for a payload right at the boundary, the size is correctly enforced.
        # We set MAX=10 and content of 11 bytes → b64 ~16 chars, 2*MAX=20 → first guard passes,
        # second guard (11 > 10) catches it.
        max_bytes = 10
        content = b"X" * (max_bytes + 1)  # 11 bytes
        b64_str = b64(content)
        # confirm first guard does NOT trigger
        self.assertLessEqual(len(b64_str), max_bytes * 2 + 4)  # ~16 <= 24, passes first check

        path = os.path.join(self.tmpdir, "boundary.txt")
        payload_b64 = base64.b64encode(
            json.dumps(
                {
                    "file": path,
                    "content_b64": b64_str,
                    "base_sha": None,
                }
            ).encode()
        ).decode()

        env = self._env_with_limit(max_bytes)
        r = subprocess.run(
            [PYTHON, WRITE_HELPER, payload_b64],
            capture_output=True,
            text=True,
            env=env,
        )
        result = json.loads(r.stdout.strip())

        self.assertEqual(result["status"], "error")
        self.assertIn("too large", result["message"].lower(), result)

    def test_content_at_exact_limit_succeeds(self):
        """Content at exactly MAX_BYTES passes (boundary: size == MAX, not >)."""
        max_bytes = 20
        content = b"Y" * max_bytes  # exactly 20 bytes
        path = os.path.join(self.tmpdir, "exact_limit.txt")
        payload_b64 = base64.b64encode(
            json.dumps(
                {
                    "file": path,
                    "content_b64": b64(content),
                    "base_sha": None,
                }
            ).encode()
        ).decode()

        env = self._env_with_limit(max_bytes)
        r = subprocess.run(
            [PYTHON, WRITE_HELPER, payload_b64],
            capture_output=True,
            text=True,
            env=env,
        )
        result = json.loads(r.stdout.strip())

        self.assertEqual(result["status"], "ok", result)


class TestWriteHelperBadInput(unittest.TestCase):
    """Error handling: malformed input → status=error, no crash."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_bad_base64_argv(self):
        """Corrupted base64 argv → error (bad request)."""
        r = subprocess.run(
            [PYTHON, WRITE_HELPER, "not-valid-base64!!!"],
            capture_output=True,
            text=True,
        )
        result = json.loads(r.stdout.strip())
        self.assertEqual(result["status"], "error")
        self.assertIn("bad request", result["message"].lower())

    def test_invalid_json_stdin(self):
        """Non-JSON stdin → error (bad request)."""
        r = subprocess.run(
            [PYTHON, WRITE_HELPER],
            input="{not valid json",
            capture_output=True,
            text=True,
        )
        result = json.loads(r.stdout.strip())
        self.assertEqual(result["status"], "error")

    def test_bad_b64_content_field(self):
        """content_b64 field contains invalid base64 → error."""
        path = os.path.join(self.tmpdir, "bad_b64.txt")
        result = run_via_argv(
            {
                "file": path,
                "content_b64": "this is not base64!!!",
                "base_sha": None,
            }
        )
        self.assertEqual(result["status"], "error")
        self.assertFalse(os.path.exists(path))
