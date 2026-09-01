"""
Tests for pretool.py - what the hook does with the INPUT the harness hands it.

Two findings live here, and they are one subject: everything this hook decides is decided
from that JSON, so what it does when the JSON is not whole IS its safety.

**Broken input.** The hook used to answer an unparsable stdin with `sys.exit(0)` - silence,
and the harness then ran the ORIGINAL command. On a session routed to a server that is the
audit's very first finding coming through another door: `rm -rfv /srv/old`, written for the
far machine, deleting the local tree - because the one file that could have said "you are
routed away" is the file that could not be read. A missing `session_id` was worse than it
looked: it fell back to the literal string "default", so two sessions arriving without an
id shared ONE routing slot and a switch made by one sent the other one's commands to that
host. So the answer is now shaped by WHO can still repair the hook:

  bash                  -> STOPS. It is the only tool here that can act on the wrong machine.
  file tools - Agent    -> ALLOWED, and told. Edit on pretool.py repairs the hook with no
                          bash at all; blocking them would lock the door and drop the key.
  nothing readable      -> BLOCKED, all of it - a bash command and a file read are the same
                          shape when the tool name is what went missing.

**The whole input.** A rewrite used to hand back `updatedInput` naming `command` and
nothing else. Measured against harness 2.1.226: a Bash call carrying `run_in_background:
true` and `timeout: 600000` came back with both fields GONE and ran in the foreground - a
ten-minute background job cut at the default timeout for no visible reason. (The reference
says updatedInput is merged, not replaced; this harness replaces. Handing the whole input
back is right under either reading, which is why it does not depend on that question.)

Coverage:
  - garbage - empty - valid-JSON-that-is-not-an-object -> blocked, exit 2, reason on stderr
  - a missing tool_name is blocked too: bash and a file read cannot be told apart
  - bash with no session_id / no command -> refused with a sentence, exit 0, nothing of the
    caller's line surviving into what runs
  - the "default" slot cannot be inherited: a session with NO id is not routed by a
    target.default someone else left behind
  - file tools and Agent keep working, are told every time, and no warning-marker is
    written (there is no id to key one on, which is exactly why it repeats)
  - a rewrite carries the caller's whole input; only `command` differs
  - a REFUSAL carries only the command, deliberately: a sentence that must be read now may
    not inherit `run_in_background` and be filed into a background task

The hook is run as the harness runs it - a subprocess fed JSON on stdin - with SHUNT_CONF
pointed at a temp directory, so no real config or session state is touched.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

# Make the src tree importable when run without installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shunt import pretool

# The hook as a file: it runs in its own process, so it is invoked by path.
PRETOOL = pretool.__file__


class HookConf:
    """A temp SHUNT_CONF with one host, plus whatever session files a test needs."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-input-")
        with open(os.path.join(self.dir, "shunt.toml"), "w") as f:
            f.write('[hosts]\nh1 = "root@203.0.113.11"\n')
        return self

    def __exit__(self, *_):
        shutil.rmtree(self.dir, ignore_errors=True)

    def route_to(self, alias, sid="s1"):
        with open(os.path.join(self.dir, "target." + sid), "w") as f:
            f.write(alias)

    def names(self):
        return sorted(os.listdir(self.dir))


def hook_env(conf_dir, **extra):
    """The environment the harness actually gives the hook - WITHOUT PYTHONPATH.

    settings.json names pretool.py by absolute path, so `python3 .../src/shunt/pretool.py`
    puts .../src/shunt on sys.path and never .../src. Leaving the test runner's PYTHONPATH in
    place would hide exactly that: the package would import for a reason the hook cannot
    count on in the field.
    """
    env = dict(os.environ, SHUNT_CONF=conf_dir, **extra)
    env.pop("PYTHONPATH", None)
    return env


def run_raw(conf, payload):
    """Feed the hook exactly these bytes. Returns (exit code, stdout, stderr)."""
    if not isinstance(payload, bytes):
        payload = json.dumps(payload).encode()
    r = subprocess.run(
        [sys.executable, PRETOOL],
        input=payload,
        capture_output=True,
        env=hook_env(conf.dir),
    )
    return r.returncode, r.stdout.decode(), r.stderr.decode()


def reply(stdout):
    """The hookSpecificOutput object, or {} when the hook said nothing."""
    if not stdout.strip():
        return {}
    return json.loads(stdout)["hookSpecificOutput"]


def what_runs(stdout):
    """The command the harness would actually run - "" when the hook wrote no rewrite."""
    return reply(stdout).get("updatedInput", {}).get("command", "")


# -- nothing readable at all ----------------------------------------------------


class TestUnreadableInput(unittest.TestCase):
    """No tool name, no command, no routing. The one state where even a file read is
    stopped: telling it apart from a bash command is precisely what is missing."""

    def test_garbage_is_blocked_and_not_allowed_through(self):
        with HookConf() as c:
            code, _, err = run_raw(c, b"not json")
            self.assertEqual(code, 2, "exit 0 lets the ORIGINAL command run")
            self.assertIn("UNREADABLE", err)
            self.assertIn("BLOCKED", err)

    def test_empty_stdin_is_blocked(self):
        with HookConf() as c:
            code, _, _ = run_raw(c, b"")
            self.assertEqual(code, 2)

    def test_valid_json_that_is_not_an_object_is_blocked(self):
        """`["ls"]` parses. It still says nothing about tool, command or routing."""
        with HookConf() as c:
            for payload in (b'["ls"]', b'"ls"', b"null", b"7"):
                code, _, _ = run_raw(c, payload)
                self.assertEqual(code, 2, "%s was let through" % payload)

    def test_it_says_where_the_way_out_is(self):
        """A blocked session cannot run bash to fix itself - the message has to point
        outside it, or the reader is left in a room with no door."""
        with HookConf() as c:
            _, _, err = run_raw(c, b"not json")
            self.assertIn("outside this session", err)

    def test_stdout_stays_empty(self):
        """Half a JSON reply beside exit 2 would give the harness two answers."""
        with HookConf() as c:
            _, out, _ = run_raw(c, b"not json")
            self.assertEqual(out.strip(), "")


class TestNoToolName(unittest.TestCase):
    """Readable JSON, but the field that says WHICH tool is gone."""

    def test_a_missing_tool_name_is_blocked(self):
        with HookConf() as c:
            code, _, err = run_raw(c, {"session_id": "s1", "tool_input": {"command": "ls"}})
            self.assertEqual(code, 2)
            self.assertIn("tool_name", err)

    def test_an_empty_tool_name_is_blocked(self):
        with HookConf() as c:
            code, _, _ = run_raw(c, {"session_id": "s1", "tool_name": "", "tool_input": {}})
            self.assertEqual(code, 2)


# -- readable, but a field is missing: bash stops -------------------------------


class TestBashStopsOnIncompleteInput(unittest.TestCase):
    DANGEROUS = "rm -rfv /srv/old-release"

    def test_no_session_id_refuses_and_names_the_field(self):
        with HookConf() as c:
            code, out, _ = run_raw(c, {"tool_name": "Bash", "tool_input": {"command": "ls"}})
            self.assertEqual(code, 0, "a refusal here is a rewrite, not a block")
            self.assertIn("incomplete", what_runs(out))
            self.assertIn("session_id", what_runs(out))
            self.assertIn("NOT run", what_runs(out))

    def test_the_callers_command_does_not_survive(self):
        """The whole point: what runs is the sentence, never the line.

        The refusal is asserted alongside the absence on purpose - "the command is not in
        what runs" is also true when NOTHING was written, which is exactly the old
        behaviour this replaces. Absence alone would pass against the defect.
        """
        with HookConf() as c:
            _, out, _ = run_raw(c, {"tool_name": "Bash", "tool_input": {"command": self.DANGEROUS}})
            self.assertIn("NOT run", what_runs(out))
            self.assertNotIn("rm -rfv", what_runs(out))
            self.assertNotIn("/srv/old-release", what_runs(out))

    def test_a_missing_command_refuses(self):
        with HookConf() as c:
            _, out, _ = run_raw(c, {"tool_name": "Bash", "session_id": "s1", "tool_input": {}})
            self.assertIn("tool_input.command", what_runs(out))

    def test_a_tool_input_of_the_wrong_shape_refuses(self):
        with HookConf() as c:
            _, out, _ = run_raw(c, {"tool_name": "Bash", "session_id": "s1", "tool_input": "ls"})
            self.assertIn("tool_input", what_runs(out))

    def test_a_blank_session_id_is_not_an_id(self):
        with HookConf() as c:
            _, out, _ = run_raw(c, {"tool_name": "Bash", "session_id": "   ", "tool_input": {"command": "ls"}})
            self.assertIn("session_id", what_runs(out))

    def test_the_default_slot_cannot_be_inherited(self):
        """The sharpest edge of the old fallback: `session_id or "default"` meant a command
        with NO id read target.default - a routing slot any other id-less session may have
        written. It must not be sent anywhere on the strength of that."""
        with HookConf() as c:
            c.route_to("h1", sid="default")
            _, out, _ = run_raw(c, {"tool_name": "Bash", "tool_input": {"command": self.DANGEROUS}})
            ran = what_runs(out)
            self.assertNotIn("ssh", ran)
            self.assertNotIn("203.0.113.11", ran)
            self.assertIn("NOT run", ran)


# -- readable, a field is missing: the repairing hands stay open ----------------


class TestTheRepairingHandsStayOpen(unittest.TestCase):
    """Edit on pretool.py is how this state gets fixed from inside a session that has no
    bash. Blocking these would leave no way back in short of another terminal."""

    def test_file_tools_are_allowed_and_told(self):
        with HookConf() as c:
            for tool in ("Read", "Edit", "Write", "Grep", "Glob"):
                code, out, _ = run_raw(c, {"tool_name": tool, "tool_input": {"file_path": "/x"}})
                self.assertEqual(code, 0, "%s was stopped" % tool)
                self.assertIn("incomplete", reply(out).get("additionalContext", ""))
                self.assertNotIn("updatedInput", reply(out), "%s must run as it was asked" % tool)

    def test_the_word_names_the_tool_and_the_local_disk(self):
        with HookConf() as c:
            _, out, _ = run_raw(c, {"tool_name": "Edit", "tool_input": {}})
            said = reply(out)["additionalContext"]
            self.assertIn("Edit", said)
            self.assertIn("LOCAL disk", said)

    def test_agent_is_warned_never_stopped(self):
        with HookConf() as c:
            code, out, _ = run_raw(c, {"tool_name": "Agent", "tool_input": {"prompt": "go"}})
            self.assertEqual(code, 0)
            self.assertIn("incomplete", reply(out)["additionalContext"])

    def test_the_warning_repeats(self):
        """No session id means no budget file to key "already said" on - and for a fault
        that is happening right now, repeating is the right shape, not a defect."""
        with HookConf() as c:
            first = reply(run_raw(c, {"tool_name": "Read", "tool_input": {}})[1])
            second = reply(run_raw(c, {"tool_name": "Read", "tool_input": {}})[1])
            self.assertIn("incomplete", first["additionalContext"])
            self.assertIn("incomplete", second["additionalContext"])

    def test_no_marker_is_written_for_a_session_that_has_no_id(self):
        with HookConf() as c:
            run_raw(c, {"tool_name": "Read", "tool_input": {}})
            self.assertEqual(c.names(), ["shunt.toml"])


# -- the whole input survives a rewrite -----------------------------------------


class TestTheWholeInputSurvivesARewrite(unittest.TestCase):
    FULL = {
        "command": "sleep 600",
        "run_in_background": True,
        "timeout": 600000,
        "description": "a long job on the server",
    }

    def rewrite(self, c):
        _, out, _ = run_raw(c, {"tool_name": "Bash", "session_id": "s1", "tool_input": dict(self.FULL)})
        return reply(out)["updatedInput"]

    def test_background_and_timeout_reach_the_harness(self):
        with HookConf() as c:
            c.route_to("h1")
            updated = self.rewrite(c)
            self.assertIs(updated.get("run_in_background"), True)
            self.assertEqual(updated.get("timeout"), 600000)

    def test_only_the_command_is_changed(self):
        with HookConf() as c:
            c.route_to("h1")
            updated = self.rewrite(c)
            self.assertEqual(sorted(updated), sorted(self.FULL))
            self.assertEqual(updated["description"], self.FULL["description"])
            self.assertNotEqual(updated["command"], self.FULL["command"])
            self.assertIn("ssh", updated["command"])

    def test_the_callers_command_is_inside_the_rewrite(self):
        with HookConf() as c:
            c.route_to("h1")
            self.assertIn("sleep 600", self.rewrite(c)["command"])


class TestARefusalCarriesOnlyTheCommand(unittest.TestCase):
    """The other half of the same decision. A refusal REPLACES the command instead of
    wrapping it, and it has to be read NOW - inheriting `run_in_background` would file the
    sentence into a background task while the caller reads "Command running in background"
    and believes their line is on its way.

    ⚠ This class is a PARACHUTE, not a proof: it passed before the fix too, because the old
    code named only `command` everywhere. Its role is to hold the line that the fix must
    NOT cross - the day someone hands `tool_input` to echo() as well, this is what says no.
    """

    def test_a_switch_reply_names_only_the_command(self):
        with HookConf() as c:
            _, out, _ = run_raw(
                c,
                {
                    "tool_name": "Bash",
                    "session_id": "s1",
                    "tool_input": {"command": "@status", "run_in_background": True, "timeout": 600000},
                },
            )
            self.assertEqual(list(reply(out)["updatedInput"]), ["command"])

    def test_an_unresolvable_alias_names_only_the_command(self):
        with HookConf() as c:
            c.route_to("gone")
            _, out, _ = run_raw(
                c,
                {
                    "tool_name": "Bash",
                    "session_id": "s1",
                    "tool_input": {"command": "ls", "run_in_background": True},
                },
            )
            self.assertEqual(list(reply(out)["updatedInput"]), ["command"])
            self.assertIn("NOT run", what_runs(out))


if __name__ == "__main__":
    unittest.main()
