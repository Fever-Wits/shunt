"""
Tests for shunt.pretool — the PreToolUse hook, end-to-end (JSON in → JSON out).

Coverage:
  - Bash rewriting is UNCHANGED by the warning branch (regression guard)
  - @status / @local switches still work
  - Agent spawned in remote mode → warned, every time (each spawn inherits the mode)
  - file AND search tools in remote mode → warned once per host, then quiet
  - the single warning carries both remedies (remote file · remote search), because
    the budget is shared: Grep is called often enough to turn a per-call line into
    wallpaper
  - switching host re-arms the file-tool warning
  - @local clears it, so entering remote mode again warns again
  - nothing is warned about while local
  - unknown tools are ignored
  - warnings never block: always exit 0, always "additionalContext"
  - addition: the hook still routes over a legacy `hosts` file (the rest runs on the
    canonical shunt.toml)

The hook is exercised as the harness runs it — a subprocess fed JSON on stdin —
because that IS its contract. Two details of that contract are reproduced on purpose:
the hook is started BY PATH, and PYTHONPATH is stripped, because settings.json wires it
in by absolute path and nothing puts the package on sys.path for it. SHUNT_CONF points
at a temp dir, so no real config is read or written.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

# Make the src tree importable when run without installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import shunt.pretool as pretool_mod

# The hook as a file: it runs in its own process, so it is invoked by path.
PRETOOL = pretool_mod.__file__


# ── helpers ────────────────────────────────────────────────────────────────────

class TmpConf:
    """Context manager: a temp SHUNT_CONF with two ssh hosts in shunt.toml.

    shunt.toml is the canon, so that is what the hook is exercised against here. The
    legacy `hosts` file has its own home in tests/test_shunt_config.py; it appears in
    this file exactly once, at the bottom, to prove the hook still routes over it.
    """

    CONFIG = ("shunt.toml", '[hosts]\nh1 = "root@10.0.0.1"\nh2 = "root@10.0.0.2"\n')

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-conf-")
        name, text = self.CONFIG
        with open(os.path.join(self.dir, name), "w") as f:
            f.write(text)
        return self

    def __exit__(self, *_):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def route_to(self, alias, sid="s1"):
        """Pretend the session already switched to @alias."""
        with open(os.path.join(self.dir, "target." + sid), "w") as f:
            f.write(alias)

    def exists(self, name):
        return os.path.exists(os.path.join(self.dir, name))


def hook_env(conf_dir=None):
    """The environment the harness actually gives the hook — WITHOUT PYTHONPATH.

    settings.json names pretool.py by absolute path, so `python3 …/src/shunt/pretool.py`
    puts …/src/shunt on sys.path and never …/src. Leaving the test runner's PYTHONPATH in
    place would hide exactly that: the package would import for a reason the hook cannot
    count on in the field.
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    if conf_dir:
        env["SHUNT_CONF"] = conf_dir
    return env


def run_hook(conf, tool, tool_input=None, sid="s1"):
    """Run pretool.py exactly as the harness does. Returns (exit_code, stdout)."""
    payload = {"tool_name": tool, "session_id": sid,
               "tool_input": tool_input or {}}
    r = subprocess.run([sys.executable, PRETOOL],
                       input=json.dumps(payload).encode(),
                       capture_output=True,
                       env=hook_env(conf.dir))
    return r.returncode, r.stdout.decode()


def context_of(stdout):
    """The additionalContext of a warning, or None if the hook said nothing."""
    if not stdout.strip():
        return None
    try:
        return json.loads(stdout)["hookSpecificOutput"].get("additionalContext")
    except Exception:
        return None


def rewritten_command(stdout):
    """The rewritten command of a Bash redirect, or None."""
    if not stdout.strip():
        return None
    try:
        return json.loads(stdout)["hookSpecificOutput"]["updatedInput"]["command"]
    except Exception:
        return None


# ── regression: the Bash path must be untouched ────────────────────────────────

class TestBashUnchanged(unittest.TestCase):
    """The warning branch sits in FRONT of the Bash path — prove it stays out of it."""

    def test_local_bash_is_not_rewritten(self):
        with TmpConf() as c:
            code, out = run_hook(c, "Bash", {"command": "ls -la"})
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "")

    def test_remote_bash_is_rewritten_over_ssh(self):
        with TmpConf() as c:
            c.route_to("h1")
            code, out = run_hook(c, "Bash", {"command": "ls -la"})
            self.assertEqual(code, 0)
            cmd = rewritten_command(out)
            self.assertIsNotNone(cmd)
            self.assertIn("ssh", cmd)
            self.assertIn("root@10.0.0.1", cmd)

    def test_already_rewritten_command_is_left_alone(self):
        with TmpConf() as c:
            c.route_to("h1")
            code, out = run_hook(c, "Bash", {"command": "#shunt-rewritten\nls"})
            self.assertEqual(out.strip(), "")

    def test_shunt_cli_stays_local(self):
        with TmpConf() as c:
            c.route_to("h1")
            code, out = run_hook(c, "Bash", {"command": "shunt hosts"})
            self.assertEqual(out.strip(), "")

    def test_status_switch_still_answers(self):
        with TmpConf() as c:
            c.route_to("h1")
            code, out = run_hook(c, "Bash", {"command": "@status"})
            self.assertIn("REMOTE", rewritten_command(out))


# ── the agent warning (problem 1: silent inheritance) ──────────────────────────

class TestAgentWarning(unittest.TestCase):

    def test_agent_warned_in_remote_mode(self):
        with TmpConf() as c:
            c.route_to("h1")
            code, out = run_hook(c, "Agent", {"prompt": "go"})
            self.assertEqual(code, 0)
            ctx = context_of(out)
            self.assertIsNotNone(ctx)
            self.assertIn("h1", ctx)
            self.assertIn("INHERITS", ctx)

    def test_agent_silent_when_local(self):
        with TmpConf() as c:
            code, out = run_hook(c, "Agent", {"prompt": "go"})
            self.assertIsNone(context_of(out))

    def test_agent_warned_every_time(self):
        """Each spawn inherits the mode — a once-per-session warning would miss spawns."""
        with TmpConf() as c:
            c.route_to("h1")
            run_hook(c, "Agent", {"prompt": "one"})
            _, out = run_hook(c, "Agent", {"prompt": "two"})
            self.assertIsNotNone(context_of(out))


# ── the file-tool warning (problem 3: mode covers bash only) ───────────────────

class TestFileToolWarning(unittest.TestCase):

    def test_read_warned_in_remote_mode(self):
        with TmpConf() as c:
            c.route_to("h1")
            _, out = run_hook(c, "Read", {"file_path": "/etc/hosts"})
            ctx = context_of(out)
            self.assertIsNotNone(ctx)
            self.assertIn("LOCAL disk", ctx)
            self.assertIn("shunt read", ctx)

    def test_all_local_disk_tools_covered(self):
        """Spelled out, not read from the tuple: a name dropped from it must fail here."""
        for tool in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit",
                     "Grep", "Glob"):
            with TmpConf() as c:
                c.route_to("h1")
                _, out = run_hook(c, tool, {"file_path": "/x"})
                self.assertIsNotNone(context_of(out), f"{tool} not warned")

    def test_grep_warned_in_remote_mode(self):
        """The agent's tool: it searches the local disk and reads the hits as remote."""
        with TmpConf() as c:
            c.route_to("h1")
            _, out = run_hook(c, "Grep", {"pattern": "TODO", "path": "/etc"})
            ctx = context_of(out)
            self.assertIsNotNone(ctx)
            self.assertIn("LOCAL disk", ctx)
            self.assertIn("shunt run", ctx)          # the remedy for a remote search

    def test_search_and_file_tools_share_one_warning_per_host(self):
        """A line per Grep call would become wallpaper — and wallpaper is never read."""
        with TmpConf() as c:
            c.route_to("h1")
            _, first = run_hook(c, "Grep", {"pattern": "a"})
            self.assertIsNotNone(context_of(first))
            _, second = run_hook(c, "Grep", {"pattern": "b"})
            _, third = run_hook(c, "Read", {"file_path": "/a"})
            self.assertIsNone(context_of(second))
            self.assertIsNone(context_of(third))

    def test_the_one_warning_carries_both_remedies(self):
        """Whichever tool spends the budget, the other's way out must be in the line."""
        with TmpConf() as c:
            c.route_to("h1")
            _, out = run_hook(c, "Read", {"file_path": "/a"})
            ctx = context_of(out)
            self.assertIn("shunt read/edit", ctx)
            self.assertIn("shunt run", ctx)

    def test_second_read_is_quiet(self):
        """Once per host — a warning on every read would become wallpaper."""
        with TmpConf() as c:
            c.route_to("h1")
            run_hook(c, "Read", {"file_path": "/a"})
            _, out = run_hook(c, "Read", {"file_path": "/b"})
            self.assertIsNone(context_of(out))

    def test_switching_host_warns_again(self):
        """The old warning described a different machine — it no longer applies."""
        with TmpConf() as c:
            c.route_to("h1")
            run_hook(c, "Read", {"file_path": "/a"})
            c.route_to("h2")
            _, out = run_hook(c, "Read", {"file_path": "/a"})
            ctx = context_of(out)
            self.assertIsNotNone(ctx)
            self.assertIn("h2", ctx)

    def test_going_local_rearms_the_warning(self):
        with TmpConf() as c:
            c.route_to("h1")
            run_hook(c, "Read", {"file_path": "/a"})
            run_hook(c, "Bash", {"command": "@local"})
            self.assertFalse(c.exists("warned.s1"))
            c.route_to("h1")
            _, out = run_hook(c, "Read", {"file_path": "/a"})
            self.assertIsNotNone(context_of(out))

    def test_file_tool_silent_when_local(self):
        with TmpConf() as c:
            _, out = run_hook(c, "Read", {"file_path": "/a"})
            self.assertIsNone(context_of(out))

    def test_parallel_sessions_warn_independently(self):
        with TmpConf() as c:
            c.route_to("h1", sid="s1")
            c.route_to("h1", sid="s2")
            run_hook(c, "Read", {"file_path": "/a"}, sid="s1")
            _, out = run_hook(c, "Read", {"file_path": "/a"}, sid="s2")
            self.assertIsNotNone(context_of(out))


# ── boundaries ─────────────────────────────────────────────────────────────────

class TestBoundaries(unittest.TestCase):

    def test_unknown_tool_is_ignored(self):
        with TmpConf() as c:
            c.route_to("h1")
            code, out = run_hook(c, "WebSearch", {"query": "x"})
            self.assertEqual(code, 0)
            self.assertIsNone(context_of(out))

    def test_warning_never_blocks(self):
        """exit 2 would deny the call — these are notes, not gates."""
        with TmpConf() as c:
            c.route_to("h1")
            for tool in ("Agent", "Read", "Write"):
                code, _ = run_hook(c, tool, {})
                self.assertEqual(code, 0, f"{tool} did not exit 0")

    def test_garbage_stdin_does_not_crash(self):
        r = subprocess.run([sys.executable, PRETOOL], input=b"not json",
                           capture_output=True, env=hook_env())
        self.assertEqual(r.returncode, 0)


# ── ADDITION: the legacy `hosts` file, walked through the HOOK ─────────────────

class LegacyConf(TmpConf):
    """The same two hosts, written in the OLD `hosts` format."""

    CONFIG = ("hosts", "h1 ssh root@10.0.0.1\nh2 ssh root@10.0.0.2\n")


class TestLegacyConfigStillRoutes(unittest.TestCase):
    """An addition to everything above, which now runs on shunt.toml.

    test_shunt_config.py proves the legacy format is READ; nothing proved the hook still
    ROUTES over it. An installation that never migrated must keep working, so that one
    step is covered here rather than left to the fixture.
    """

    def test_remote_bash_is_rewritten_from_the_legacy_file(self):
        with LegacyConf() as c:
            c.route_to("h1")
            code, out = run_hook(c, "Bash", {"command": "ls -la"})
            self.assertEqual(code, 0)
            self.assertIn("root@10.0.0.1", rewritten_command(out))


if __name__ == "__main__":
    unittest.main()
