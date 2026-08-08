"""
Tests for shunt.cli — manifest logic and subcommand registry.

Coverage:
  - _manifest_load / _manifest_save round-trip
  - _checkout_local_path computation
  - _sha256_file
  - cmd_checkout manifest side-effects (subprocess.run stubbed → no real ssh)
  - cmd_checkout --list output
  - cmd_checkout --abandon
  - cmd_commit --abandon
  - cmd_commit manifest update after successful remote write (ssh stubbed)
  - cmd_commit conflict detection
  - fns dict contains checkout+commit and all pre-existing subcommands
  - import smoke: shunt.cli module loads cleanly

SHUNT_CONF is redirected to a temp dir for every test that touches the filesystem.
subprocess.run is monkeypatched to avoid real ssh/scp calls.
"""
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Make the src tree importable when run without installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import shunt.cli as shunt_mod

# ── helpers ────────────────────────────────────────────────────────────────────

def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class TmpConf:
    """Context manager: override SHUNT_CONF + shunt_mod.MANIFEST + shunt_mod.CONF."""

    def __init__(self):
        self._tmpdir = None

    def __enter__(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_conf = shunt_mod.CONF
        self._orig_manifest = shunt_mod.MANIFEST
        shunt_mod.CONF = self._tmpdir
        shunt_mod.MANIFEST = os.path.join(self._tmpdir, "checkouts", "manifest.json")
        return self._tmpdir

    def __exit__(self, *_):
        shunt_mod.CONF = self._orig_conf
        shunt_mod.MANIFEST = self._orig_manifest
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)


# ── AC-3a: import smoke ────────────────────────────────────────────────────────

class TestImportSmoke(unittest.TestCase):
    """Importing shunt.cli does not raise; key callables are present."""

    def test_import_succeeds(self):
        self.assertIsNotNone(shunt_mod)

    def test_main_callable(self):
        self.assertTrue(callable(shunt_mod.main))

    def test_cmd_checkout_callable(self):
        self.assertTrue(callable(shunt_mod.cmd_checkout))

    def test_cmd_commit_callable(self):
        self.assertTrue(callable(shunt_mod.cmd_commit))

    def test_manifest_helpers_callable(self):
        self.assertTrue(callable(shunt_mod._manifest_load))
        self.assertTrue(callable(shunt_mod._manifest_save))
        self.assertTrue(callable(shunt_mod._checkout_local_path))
        self.assertTrue(callable(shunt_mod._sha256_file))


# ── AC-3b: fns dict ────────────────────────────────────────────────────────────

class TestFnsDict(unittest.TestCase):
    """fns dict in main() contains checkout+commit and all pre-existing subcommands."""

    def test_checkout_in_fns(self):
        """checkout subcommand is registered."""
        self.assertIn("cmd_checkout", dir(shunt_mod))

    def test_commit_in_fns(self):
        self.assertIn("cmd_commit", dir(shunt_mod))

    def test_all_expected_subcommands_present(self):
        """All expected subcommand functions exist at module level."""
        for name in ("hosts", "read", "edit", "cp", "bg", "get", "log",
                     "install", "checkout", "commit"):
            fn_name = "cmd_" + name
            self.assertIn(fn_name, dir(shunt_mod),
                          "Missing subcommand function: %s" % fn_name)

    def test_main_lists_checkout_commit_in_map(self):
        """main() with no args prints the map, and the map includes checkout and commit.

        No arguments is the tool's self-introduction (the map on stdout — shunt is not
        an MCP server and this is its only way to explain itself in one call) AND a
        missing subcommand (exit 2). Whatever it prints must name every subcommand.
        See tests/test_shunt_help.py.
        """
        with patch.object(sys, "argv", ["shunt"]), patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            with self.assertRaises(SystemExit) as ctx:
                shunt_mod.main()
            out = mock_out.getvalue()
        self.assertEqual(ctx.exception.code, 2)   # no-args = missing subcommand = error
        self.assertIn("checkout", out)
        self.assertIn("commit", out)

    def test_unknown_subcommand_exits_nonzero(self):
        """Unknown subcommand causes SystemExit with non-zero code."""
        with patch.object(sys, "argv", ["shunt", "no-such-cmd"]), self.assertRaises(SystemExit) as ctx:
            shunt_mod.main()
        self.assertNotEqual(ctx.exception.code, 0)

    def test_checkout_subcommand_dispatched(self):
        """'checkout' dispatches to cmd_checkout (not 'unknown subcommand')."""
        with patch.object(shunt_mod, "cmd_checkout", return_value=0) as mock_co:
            with patch.object(sys, "argv", ["shunt", "checkout", "--list"]), self.assertRaises(SystemExit):
                shunt_mod.main()
            mock_co.assert_called_once()

    def test_commit_subcommand_dispatched(self):
        """'commit' dispatches to cmd_commit."""
        with patch.object(shunt_mod, "cmd_commit", return_value=0) as mock_cm:
            with patch.object(sys, "argv", ["shunt", "commit"]), self.assertRaises(SystemExit):
                shunt_mod.main()
            mock_cm.assert_called_once()

    def test_preexisting_subcommands_not_removed(self):
        """hosts/read/edit/cp/bg/get/log/install still dispatch correctly."""
        for sub in ("hosts", "read", "edit", "cp", "bg", "get", "log", "install"):
            fn_name = "cmd_" + sub
            with patch.object(shunt_mod, fn_name, return_value=0) as mock_fn:
                with patch.object(sys, "argv", ["shunt", sub]), self.assertRaises(SystemExit):
                    shunt_mod.main()
                mock_fn.assert_called_once()


# ── Manifest helpers ────────────────────────────────────────────────────────────

class TestManifestHelpers(unittest.TestCase):
    """_manifest_load / _manifest_save / _sha256_file / _checkout_local_path."""

    def test_load_missing_returns_empty_dict(self):
        with TmpConf():
            result = shunt_mod._manifest_load()
        self.assertEqual(result, {})

    def test_save_and_load_round_trip(self):
        with TmpConf():
            data = {"/some/local/path.py": {"host": "myhost", "remote": "/remote/path.py",
                                             "base_sha": "abc123"}}
            shunt_mod._manifest_save(data)
            loaded = shunt_mod._manifest_load()
        self.assertEqual(loaded, data)

    def test_save_creates_parent_dirs(self):
        with TmpConf():
            # manifest dir does not exist yet
            self.assertFalse(os.path.exists(os.path.dirname(shunt_mod.MANIFEST)))
            shunt_mod._manifest_save({"k": "v"})
            self.assertTrue(os.path.exists(shunt_mod.MANIFEST))

    def test_save_is_atomic(self):
        """save writes via .tmp then os.replace — no partial file visible."""
        with TmpConf():
            shunt_mod._manifest_save({"a": "1"})
            # tmp file must not exist after save
            tmp_path = shunt_mod.MANIFEST + ".tmp"
            self.assertFalse(os.path.exists(tmp_path))

    def test_corrupt_manifest_returns_empty(self):
        with TmpConf():
            os.makedirs(os.path.dirname(shunt_mod.MANIFEST), exist_ok=True)
            with open(shunt_mod.MANIFEST, "w") as f:
                f.write("{not valid json")
            result = shunt_mod._manifest_load()
        self.assertEqual(result, {})

    def test_sha256_file_existing(self):
        with TmpConf() as conf:
            p = os.path.join(conf, "test.txt")
            content = b"hello world"
            with open(p, "wb") as f:
                f.write(content)
            result = shunt_mod._sha256_file(p)
        self.assertEqual(result, sha256(content))

    def test_sha256_file_missing_returns_none(self):
        result = shunt_mod._sha256_file("/nonexistent/path/that/cannot/exist.txt")
        self.assertIsNone(result)

    def test_checkout_local_path_absolute_remote(self):
        """Absolute remote path → local path strips leading slash."""
        with TmpConf() as conf:
            result = shunt_mod._checkout_local_path("myhost", "/home/user/file.py")
            expected = os.path.join(conf, "checkouts", "myhost", "home", "user", "file.py")
        self.assertEqual(result, expected)

    def test_checkout_local_path_nested(self):
        with TmpConf() as conf:
            result = shunt_mod._checkout_local_path("srv1", "/opt/app/config.json")
            expected = os.path.join(conf, "checkouts", "srv1", "opt", "app", "config.json")
        self.assertEqual(result, expected)

    def test_checkout_local_path_different_aliases(self):
        """Different host aliases produce different local paths."""
        with TmpConf():
            p1 = shunt_mod._checkout_local_path("host-a", "/etc/hosts")
            p2 = shunt_mod._checkout_local_path("host-b", "/etc/hosts")
        self.assertNotEqual(p1, p2)


# ── cmd_checkout --list ────────────────────────────────────────────────────────

class TestCheckoutList(unittest.TestCase):
    """--list reads manifest and prints it."""

    def test_list_empty_manifest(self):
        with TmpConf():
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                rc = shunt_mod.cmd_checkout(["--list"])
            self.assertEqual(rc, 0)
            self.assertIn("no checkouts", mock_out.getvalue())

    def test_list_shows_entries(self):
        with TmpConf():
            data = {
                "/local/file.py": {"host": "srv1", "remote": "/remote/file.py",
                                   "base_sha": "abcdef123456"},
            }
            shunt_mod._manifest_save(data)
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                rc = shunt_mod.cmd_checkout(["--list"])
            output = mock_out.getvalue()
            self.assertEqual(rc, 0)
            self.assertIn("srv1", output)
            self.assertIn("/remote/file.py", output)
            self.assertIn("abcdef123456", output)

    def test_list_shows_sha_prefix(self):
        """Only first 12 chars of sha are shown."""
        with TmpConf():
            full_sha = "a" * 64
            data = {"/lp": {"host": "h", "remote": "/rp", "base_sha": full_sha}}
            shunt_mod._manifest_save(data)
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                shunt_mod.cmd_checkout(["--list"])
            output = mock_out.getvalue()
            # 12-char prefix must appear; full 64-char sha must NOT appear verbatim
            self.assertIn("a" * 12, output)
            self.assertNotIn("a" * 64, output)

    def test_list_multiple_entries_sorted(self):
        with TmpConf():
            data = {
                "/z/last.py": {"host": "h", "remote": "/r1", "base_sha": "sha1"},
                "/a/first.py": {"host": "h", "remote": "/r2", "base_sha": "sha2"},
            }
            shunt_mod._manifest_save(data)
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                shunt_mod.cmd_checkout(["--list"])
            lines = [l for l in mock_out.getvalue().splitlines() if l.strip()]
            # sorted: /a comes before /z
            self.assertLess(lines[0].find("/a/first.py"), len(lines[0]))
            self.assertGreater(lines.index(
                next(l for l in lines if "/z/last.py" in l)),
                lines.index(next(l for l in lines if "/a/first.py" in l)),
            )


# ── cmd_checkout --abandon ─────────────────────────────────────────────────────

class TestCheckoutAbandon(unittest.TestCase):
    """--abandon removes manifest entry, leaves local file in place."""

    def test_abandon_removes_entry(self):
        with TmpConf() as conf:
            local = os.path.join(conf, "file.py")
            with open(local, "w") as f:
                f.write("content")
            real_local = os.path.realpath(local)
            shunt_mod._manifest_save({real_local: {"host": "h", "remote": "/r", "base_sha": "s"}})

            with patch("sys.stdout", new_callable=io.StringIO):
                rc = shunt_mod.cmd_checkout(["--abandon", local])

            self.assertEqual(rc, 0)
            m = shunt_mod._manifest_load()
            self.assertNotIn(real_local, m)

    def test_abandon_leaves_local_file(self):
        """Local file is NOT deleted by --abandon."""
        with TmpConf() as conf:
            local = os.path.join(conf, "keep.py")
            with open(local, "w") as f:
                f.write("keep me")
            real_local = os.path.realpath(local)
            shunt_mod._manifest_save({real_local: {"host": "h", "remote": "/r", "base_sha": "s"}})

            with patch("sys.stdout", new_callable=io.StringIO):
                shunt_mod.cmd_checkout(["--abandon", local])

            self.assertTrue(os.path.exists(local))
            with open(local) as f:
                self.assertEqual(f.read(), "keep me")

    def test_abandon_not_in_manifest_returns_nonzero(self):
        """--abandon for a path not in manifest returns 1."""
        with TmpConf():
            with patch("sys.stdout", new_callable=io.StringIO):
                rc = shunt_mod.cmd_checkout(["--abandon", "/nonexistent/path.py"])
            self.assertEqual(rc, 1)

    def test_abandon_missing_arg_dies(self):
        """--abandon without a path calls die() → SystemExit."""
        with TmpConf(), self.assertRaises(SystemExit):
            shunt_mod.cmd_checkout(["--abandon"])


# ── cmd_checkout (main pull path, ssh stubbed) ─────────────────────────────────

class TestCheckoutPull(unittest.TestCase):
    """cmd_checkout @host <remote_path> writes manifest entry."""

    def _make_hosts(self, conf):
        with open(os.path.join(conf, "hosts"), "w") as f:
            f.write("myhost ssh user@203.0.113.1\n")

    def test_checkout_writes_manifest_entry(self):
        """After a successful checkout, manifest records host/remote/base_sha."""
        file_content = b"remote file content\n"

        def fake_run(cmd, **kwargs):
            # cmd_checkout opens the local file for writing and passes the fd to
            # subprocess.run via stdout=open(local,"wb").  The real subprocess writes
            # into it; our stub must do the same — write through the fd then return.
            stdout = kwargs.get("stdout")
            if stdout is not None and hasattr(stdout, "write"):
                stdout.write(file_content)
                stdout.flush()
            m = MagicMock()
            m.returncode = 0
            m.stderr = b""
            return m

        with TmpConf() as conf:
            self._make_hosts(conf)
            with patch("subprocess.run", side_effect=fake_run):
                with patch("sys.stdout", new_callable=io.StringIO):
                    rc = shunt_mod.cmd_checkout(["@myhost", "/remote/file.py"])

            self.assertEqual(rc, 0)
            m = shunt_mod._manifest_load()
            # find the entry whose remote is /remote/file.py
            entries = [(k, v) for k, v in m.items() if v.get("remote") == "/remote/file.py"]
            self.assertEqual(len(entries), 1, "manifest must have exactly one entry")
            local_key, entry = entries[0]
            self.assertEqual(entry["host"], "myhost")
            # base_sha must be sha256 of the content written into the local file
            with open(local_key, "rb") as f:
                actual_content = f.read()
            self.assertEqual(entry["base_sha"], sha256(actual_content))
            # and the content on disk must be what we wrote
            self.assertEqual(actual_content, file_content)

    def test_checkout_prints_local_path(self):
        """cmd_checkout prints the local path to stdout."""
        file_content = b"data"

        def fake_run(cmd, **kwargs):
            stdout = kwargs.get("stdout")
            if stdout and hasattr(stdout, "write"):
                stdout.write(file_content)
            m = MagicMock()
            m.returncode = 0
            m.stderr = b""
            return m

        with TmpConf() as conf:
            self._make_hosts(conf)
            with patch("subprocess.run", side_effect=fake_run):
                with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                    shunt_mod.cmd_checkout(["@myhost", "/remote/file.py"])
            output = mock_out.getvalue().strip()
            # printed path must be an absolute path within CONF/checkouts/
            self.assertTrue(os.path.isabs(output))
            self.assertIn("myhost", output)

    def test_checkout_ssh_failure_cleans_up(self):
        """If ssh cat fails, local file is removed and rc is non-zero."""
        def fake_run(cmd, **kwargs):
            stdout = kwargs.get("stdout")
            if stdout and hasattr(stdout, "write"):
                pass  # write nothing
            m = MagicMock()
            m.returncode = 1
            m.stderr = b"ssh: connection refused"
            return m

        with TmpConf() as conf:
            self._make_hosts(conf)
            with patch("subprocess.run", side_effect=fake_run):
                with patch("sys.stderr", new_callable=io.StringIO):
                    with self.assertRaises(SystemExit) as ctx:
                        shunt_mod.cmd_checkout(["@myhost", "/remote/file.py"])
            self.assertNotEqual(ctx.exception.code, 0)
            # manifest must NOT have a new entry
            self.assertEqual(shunt_mod._manifest_load(), {})
            # and nothing half-pulled is left behind either
            local = shunt_mod._checkout_local_path("myhost", "/remote/file.py")
            self.assertFalse(os.path.exists(local))
            self.assertFalse(os.path.exists(local + ".part"))

    def test_failed_recheckout_keeps_the_edited_local_file(self):
        """The expensive one: checkout → edit for forty minutes → checkout again → ssh dies.

        The pull used to open the local file for writing, which truncates it the moment
        the process starts — before ssh has said a word — and then unlinked it when ssh
        failed. The work was gone, and the command that destroyed it was the one asked to
        REFRESH it. The remedy is the atomic pattern the rest of the tree already uses:
        write beside the target, move into place only on success.
        """
        edited = b"my forty minutes of work\n"

        def fake_run(cmd, **kwargs):
            stdout = kwargs.get("stdout")
            if stdout and hasattr(stdout, "write"):
                stdout.write(b"partial garbage")     # a half-written pull, then failure
            m = MagicMock()
            m.returncode = 255
            m.stderr = b"ssh: connect to host 203.0.113.1 port 22: No route to host"
            return m

        with TmpConf() as conf:
            self._make_hosts(conf)
            local = os.path.realpath(shunt_mod._checkout_local_path("myhost", "/remote/file.py"))
            os.makedirs(os.path.dirname(local), exist_ok=True)
            with open(local, "wb") as f:
                f.write(edited)
            shunt_mod._manifest_save({local: {"host": "myhost", "remote": "/remote/file.py",
                                              "base_sha": "sha_from_the_first_checkout"}})

            with patch("subprocess.run", side_effect=fake_run):
                with patch("sys.stderr", new_callable=io.StringIO):
                    with self.assertRaises(SystemExit) as ctx:
                        shunt_mod.cmd_checkout(["@myhost", "/remote/file.py"])

            self.assertNotEqual(ctx.exception.code, 0)
            with open(local, "rb") as f:
                self.assertEqual(f.read(), edited)   # the edits are still there
            self.assertFalse(os.path.exists(local + ".part"))
            # the manifest still describes the checkout that DID happen
            self.assertEqual(shunt_mod._manifest_load()[local]["base_sha"],
                             "sha_from_the_first_checkout")

    def test_checkout_rejects_path_traversal(self):
        """A remote_path with '..' that escapes the sandbox is refused before any
        ssh call, and writes no manifest entry."""
        called = {"ssh": False}

        def fake_run(cmd, **kwargs):
            called["ssh"] = True
            m = MagicMock(); m.returncode = 0; m.stderr = b""
            return m

        with TmpConf() as conf:
            self._make_hosts(conf)
            with patch("subprocess.run", side_effect=fake_run):
                with patch("sys.stderr", new_callable=io.StringIO):
                    with self.assertRaises(SystemExit) as ctx:
                        shunt_mod.cmd_checkout(["@myhost", "/../../../../etc/passwd"])
            self.assertNotEqual(ctx.exception.code, 0)
            self.assertFalse(called["ssh"], "guard must fire before any ssh call")
            self.assertEqual(shunt_mod._manifest_load(), {})

    def test_checkout_updates_existing_manifest_entry(self):
        """Second checkout of same remote replaces old manifest entry."""
        file_content_v2 = b"version 2\n"

        def fake_run(cmd, **kwargs):
            stdout = kwargs.get("stdout")
            if stdout is not None and hasattr(stdout, "write"):
                stdout.write(file_content_v2)
                stdout.flush()
            m = MagicMock()
            m.returncode = 0
            m.stderr = b""
            return m

        with TmpConf() as conf:
            self._make_hosts(conf)
            # pre-populate manifest with stale sha
            expected_local = os.path.realpath(shunt_mod._checkout_local_path("myhost", "/remote/file.py"))
            shunt_mod._manifest_save({
                expected_local: {"host": "myhost", "remote": "/remote/file.py",
                                 "base_sha": "stale_sha"}
            })
            with patch("subprocess.run", side_effect=fake_run):
                with patch("sys.stdout", new_callable=io.StringIO):
                    shunt_mod.cmd_checkout(["@myhost", "/remote/file.py"])

            m = shunt_mod._manifest_load()
            entry = m[expected_local]
            # sha must reflect the content actually on disk after the checkout
            with open(expected_local, "rb") as f:
                disk_content = f.read()
            self.assertEqual(entry["base_sha"], sha256(disk_content))
            self.assertEqual(disk_content, file_content_v2)


# ── cmd_commit --abandon ───────────────────────────────────────────────────────

class TestCommitAbandon(unittest.TestCase):
    """cmd_commit --abandon removes manifest entry, leaves local file."""

    def test_commit_abandon_removes_entry(self):
        with TmpConf() as conf:
            local = os.path.join(conf, "edit.py")
            with open(local, "w") as f:
                f.write("edited content")
            real_local = os.path.realpath(local)
            shunt_mod._manifest_save({real_local: {"host": "h", "remote": "/r.py",
                                                    "base_sha": "s"}})
            with patch("sys.stdout", new_callable=io.StringIO):
                rc = shunt_mod.cmd_commit(["--abandon", local])
            self.assertEqual(rc, 0)
            self.assertNotIn(real_local, shunt_mod._manifest_load())

    def test_commit_abandon_leaves_local_file(self):
        with TmpConf() as conf:
            local = os.path.join(conf, "keep.py")
            with open(local, "w") as f:
                f.write("do not delete")
            real_local = os.path.realpath(local)
            shunt_mod._manifest_save({real_local: {"host": "h", "remote": "/r.py",
                                                    "base_sha": "s"}})
            with patch("sys.stdout", new_callable=io.StringIO):
                shunt_mod.cmd_commit(["--abandon", local])
            self.assertTrue(os.path.exists(local))

    def test_commit_abandon_not_in_manifest(self):
        with TmpConf():
            with patch("sys.stdout", new_callable=io.StringIO):
                rc = shunt_mod.cmd_commit(["--abandon", "/nonexistent/path.py"])
            self.assertEqual(rc, 1)

    def test_commit_abandon_missing_arg_dies(self):
        with TmpConf(), self.assertRaises(SystemExit):
            shunt_mod.cmd_commit(["--abandon"])


# ── cmd_commit — push path (ssh stubbed) ──────────────────────────────────────

class TestCommitPush(unittest.TestCase):
    """cmd_commit pushes local edits to remote via write_helper (ssh stubbed)."""

    def _setup_conf(self, conf, local_content: bytes):
        """Create hosts, a local file, and a matching manifest entry."""
        with open(os.path.join(conf, "hosts"), "w") as f:
            f.write("myhost ssh user@203.0.113.1\n")
        local = os.path.join(conf, "checkouts", "myhost", "remote", "file.py")
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "wb") as f:
            f.write(local_content)
        base_sha = sha256(local_content)
        shunt_mod._manifest_save({local: {"host": "myhost", "remote": "/remote/file.py",
                                           "base_sha": base_sha}})
        return local, base_sha

    def test_commit_success_updates_manifest_sha(self):
        """On ok from write_helper, manifest base_sha is updated to new_sha."""
        local_content = b"edited content\n"
        new_sha = sha256(local_content)
        write_helper_response = json.dumps({"status": "ok", "new_sha": new_sha,
                                            "verified": True}).encode()

        call_count = {"n": 0}

        def fake_run(cmd, **kwargs):
            m = MagicMock()
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First call: sha256sum to get remote sha
                m.returncode = 0
                # Return the SAME sha as manifest (no conflict)
                m.stdout = (sha256(local_content) + "  /remote/file.py\n").encode()
                m.stderr = b""
            else:
                # Second call: write_helper deployment
                m.returncode = 0
                m.stdout = write_helper_response
                m.stderr = b""
            return m

        with TmpConf() as conf:
            local, base_sha = self._setup_conf(conf, local_content)
            with patch("subprocess.run", side_effect=fake_run):
                with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                    rc = shunt_mod.cmd_commit([])

            self.assertEqual(rc, 0)
            m = shunt_mod._manifest_load()
            self.assertEqual(m[local]["base_sha"], new_sha)
            self.assertIn("ok", mock_out.getvalue())

    def test_commit_skips_unknown_host_and_keeps_going(self):
        """A manifest entry can outlive its host. One stale entry must not abandon the
        files after it — commit reports it, sets a non-zero code, and carries on."""
        with TmpConf() as conf:
            local, base_sha = self._setup_conf(conf, b"content\n")

            # a second entry whose host is no longer configured, ordered FIRST
            stale = os.path.join(conf, "checkouts", "goneaway", "remote", "old.py")
            os.makedirs(os.path.dirname(stale), exist_ok=True)
            with open(stale, "wb") as f:
                f.write(b"old\n")
            m = shunt_mod._manifest_load()
            m[stale] = {"host": "goneaway", "remote": "/remote/old.py",
                        "base_sha": sha256(b"old\n")}
            shunt_mod._manifest_save(m)

            reached = {"good": False}

            def fake_run(cmd, **kwargs):
                reached["good"] = True          # only the surviving host gets here
                r = MagicMock()
                r.returncode = 0
                r.stdout = (base_sha + "  /remote/file.py\n").encode()
                r.stderr = b""
                return r

            with patch("subprocess.run", side_effect=fake_run):
                with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                    rc = shunt_mod.cmd_commit([])
            output = mock_out.getvalue()

        self.assertIn("SKIP", output)
        self.assertIn("goneaway", output)
        self.assertNotEqual(rc, 0)                    # the failure is reported
        self.assertTrue(reached["good"],
                        "the entry after the stale one was never processed")

    def test_commit_conflict_detected_before_push(self):
        """If remote sha differs from manifest base_sha, commit reports CONFLICT and does not push."""
        local_content = b"local edits\n"

        call_count = {"n": 0}

        def fake_run(cmd, **kwargs):
            m = MagicMock()
            call_count["n"] += 1
            if call_count["n"] == 1:
                # sha256sum returns a DIFFERENT sha → conflict
                m.returncode = 0
                m.stdout = ("f" * 64 + "  /remote/file.py\n").encode()
                m.stderr = b""
            else:
                # write_helper should NOT be called
                raise AssertionError("write_helper called despite conflict")
            return m

        with TmpConf() as conf:
            local, base_sha = self._setup_conf(conf, local_content)
            with patch("subprocess.run", side_effect=fake_run):
                with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                    rc = shunt_mod.cmd_commit([])

            self.assertNotEqual(rc, 0)
            output = mock_out.getvalue()
            self.assertIn("CONFLICT", output)
            # manifest base_sha must be unchanged
            m = shunt_mod._manifest_load()
            self.assertEqual(m[local]["base_sha"], base_sha)

    def test_commit_specific_file(self):
        """cmd_commit [local_path] commits only that file."""
        local_content = b"specific file\n"
        new_sha = sha256(local_content)

        call_count = {"n": 0}

        def fake_run(cmd, **kwargs):
            m = MagicMock()
            call_count["n"] += 1
            if call_count["n"] == 1:
                m.returncode = 0
                m.stdout = (sha256(local_content) + "  /remote/file.py\n").encode()
                m.stderr = b""
            else:
                m.returncode = 0
                m.stdout = json.dumps({"status": "ok", "new_sha": new_sha,
                                       "verified": True}).encode()
                m.stderr = b""
            return m

        with TmpConf() as conf:
            local, _ = self._setup_conf(conf, local_content)
            with patch("subprocess.run", side_effect=fake_run):
                with patch("sys.stdout", new_callable=io.StringIO):
                    rc = shunt_mod.cmd_commit([local])
            self.assertEqual(rc, 0)

    def test_commit_file_not_in_manifest_dies(self):
        """Specifying a local_path not in manifest → die() → SystemExit.

        The manifest must be non-empty, otherwise cmd_commit's early-return
        ('no checkouts in manifest') fires before reaching the argv lookup.
        """
        with TmpConf() as conf:
            with open(os.path.join(conf, "hosts"), "w") as f:
                f.write("myhost ssh user@203.0.113.1\n")
            # populate manifest with a real (different) entry so the early-return is skipped
            shunt_mod._manifest_save({
                "/some/other/file.py": {"host": "myhost", "remote": "/r.py", "base_sha": "x"}
            })
            with self.assertRaises(SystemExit) as ctx:
                shunt_mod.cmd_commit(["/path/not/in/manifest.py"])
            self.assertNotEqual(ctx.exception.code, 0)

    def test_commit_empty_manifest_returns_zero(self):
        """No checkouts in manifest → prints message, returns 0."""
        with TmpConf():
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                rc = shunt_mod.cmd_commit([])
            self.assertEqual(rc, 0)
            self.assertIn("no checkouts", mock_out.getvalue())

    def test_commit_write_helper_conflict_response(self):
        """write_helper returning conflict → CONFLICT printed, manifest unchanged."""
        local_content = b"my edits\n"

        call_count = {"n": 0}

        def fake_run(cmd, **kwargs):
            m = MagicMock()
            call_count["n"] += 1
            if call_count["n"] == 1:
                # sha256sum matches → no pre-push conflict
                m.returncode = 0
                m.stdout = (sha256(local_content) + "  /remote/file.py\n").encode()
                m.stderr = b""
            else:
                # write_helper returns conflict (race: file changed mid-flight)
                m.returncode = 0
                m.stdout = json.dumps({
                    "status": "conflict",
                    "current_sha": "e" * 64,
                    "base_sha": sha256(local_content),
                }).encode()
                m.stderr = b""
            return m

        with TmpConf() as conf:
            local, base_sha = self._setup_conf(conf, local_content)
            with patch("subprocess.run", side_effect=fake_run):
                with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                    rc = shunt_mod.cmd_commit([])

            self.assertNotEqual(rc, 0)
            self.assertIn("CONFLICT", mock_out.getvalue())
            # manifest base_sha must NOT be updated
            m = shunt_mod._manifest_load()
            self.assertEqual(m[local]["base_sha"], base_sha)

    def test_commit_write_helper_error_response(self):
        """write_helper returning error → ERROR printed, rc non-zero."""
        local_content = b"edits\n"

        call_count = {"n": 0}

        def fake_run(cmd, **kwargs):
            m = MagicMock()
            call_count["n"] += 1
            if call_count["n"] == 1:
                m.returncode = 0
                m.stdout = (sha256(local_content) + "  /f\n").encode()
                m.stderr = b""
            else:
                m.returncode = 0
                m.stdout = json.dumps({
                    "status": "error",
                    "message": "write failed: permission denied",
                }).encode()
                m.stderr = b""
            return m

        with TmpConf() as conf:
            local, _ = self._setup_conf(conf, local_content)
            with patch("subprocess.run", side_effect=fake_run):
                with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                    rc = shunt_mod.cmd_commit([])

            self.assertNotEqual(rc, 0)
            self.assertIn("ERROR", mock_out.getvalue())

    def test_commit_sha256sum_failure_skips_file(self):
        """If sha256sum call fails (non-zero rc), commit skips the file."""
        local_content = b"data\n"

        def fake_run(cmd, **kwargs):
            m = MagicMock()
            m.returncode = 1
            m.stdout = b""
            m.stderr = b"sha256sum: not found"
            return m

        with TmpConf() as conf:
            local, _ = self._setup_conf(conf, local_content)
            with patch("subprocess.run", side_effect=fake_run):
                with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                    rc = shunt_mod.cmd_commit([])

            self.assertNotEqual(rc, 0)
            self.assertIn("SKIP", mock_out.getvalue())
