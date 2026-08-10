"""
Tests for pretool.py — what a SPAWNED agent is told about the machine it was born on.

The parent has been warned about spawning into remote mode for a while. The agent itself
heard nothing — and it is the one that acts on it: it runs `ls`, reads a disk it has never
seen, and reports what it found as a fact about the world. (Observed once as "the Bash
tool briefly lost access to the working directory". It had not. It was elsewhere.)

So the hook writes a short frame into the child's prompt. Three things had to be true for
that to be worth doing, and each has tests here:

  it must be a FRAME, not a fact — "you are on @h1" gives an agent nothing to do; what
      routes the commands, what it means for its two hands (bash goes there, files stay
      here) and the way out is a thing it can act on;

  the way out must carry its PRICE. The harness gives a child its parent's session_id —
      documented, and confirmed in this installation's own transcripts — so every file
      this hook keys on is ONE slot shared by the parent and every agent running at that
      moment. `@local` from a child moves everybody. Offering it without saying so would
      hand the child a way to reroute its parent mid-command;

  the rest of the Agent call must survive the trip. `updatedInput` replaces a tool's
      arguments, so the whole input goes back — an Agent call that lost its
      `subagent_type` inside a hook is a much worse failure than an unwritten note.

Coverage:
  - the note lands at the END of the prompt, after the caller's own text, clearly parted
  - it names the host, says bash goes THERE, says the file tools do NOT
  - it names the way home AND that the switch is shared
  - every other field of the Agent input comes back untouched
  - the parent's warning still rides in the SAME reply
  - every spawn gets one — no budget, and no budget shared with Grep
  - a local session is untouched, and so is an Agent call with no prompt to write into
  - the unreadable-routing state deliberately does NOT write a note (it announces itself
    to the child through the very first refused command)
  - nothing here blocks or decides permission: no permissionDecision leaves this hook
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

PRETOOL = pretool.__file__


class HookConf:
    """A temp SHUNT_CONF with two hosts. No ssh is needed: nothing here switches."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-agent-")
        with open(os.path.join(self.dir, "shunt.toml"), "w") as f:
            f.write('[hosts]\nh1 = "root@203.0.113.9"\nh2 = "root@203.0.113.10"\n')
        return self

    def __exit__(self, *_):
        shutil.rmtree(self.dir, ignore_errors=True)

    def route_to(self, alias, sid="s1"):
        with open(os.path.join(self.dir, "target." + sid), "w") as f:
            f.write(alias)

    def break_routing(self, sid="s1"):
        """What a torn write leaves: the file is there and names no host."""
        open(os.path.join(self.dir, "target." + sid), "w").close()


SPAWN = {
    "prompt": "Find every caller of resolve_host and report the list.",
    "subagent_type": "detective",
    "description": "trace callers",
}


def run_tool(conf, tool, tool_input, sid="s1"):
    """Run pretool.py on a tool call, as the harness does. Returns hookSpecificOutput."""
    payload = {"tool_name": tool, "session_id": sid, "tool_input": tool_input}
    env = dict(os.environ, SHUNT_CONF=conf.dir)
    # PYTHONPATH is stripped the way the other hook tests strip it: settings.json names
    # pretool.py by absolute path, so the field never has the package on sys.path.
    env.pop("PYTHONPATH", None)
    r = subprocess.run([sys.executable, PRETOOL], input=json.dumps(payload).encode(), capture_output=True, env=env)
    out = r.stdout.decode().strip()
    return (json.loads(out)["hookSpecificOutput"] if out else {}), r.returncode


def spawn(conf, tool_input=None, sid="s1"):
    reply, _ = run_tool(conf, "Agent", dict(tool_input or SPAWN), sid=sid)
    return reply


def child_prompt(reply):
    return reply.get("updatedInput", {}).get("prompt")


# ── the note itself ────────────────────────────────────────────────────────────


class TestTheChildIsTold(unittest.TestCase):
    def test_the_prompt_comes_back_with_something_added(self):
        with HookConf() as c:
            c.route_to("h1")
            self.assertIsNotNone(child_prompt(spawn(c)), "the child was told nothing")

    def test_the_callers_own_prompt_is_untouched_and_comes_first(self):
        """The task is the caller's; the note is an addition to it, never an edit of it."""
        with HookConf() as c:
            c.route_to("h1")
            self.assertTrue(child_prompt(spawn(c)).startswith(SPAWN["prompt"]))

    def test_the_note_is_at_the_end_and_parted_from_the_task(self):
        with HookConf() as c:
            c.route_to("h1")
            prompt = child_prompt(spawn(c))
            head, sep, tail = prompt.partition(pretool.AGENT_FRAME_SEPARATOR)
            self.assertEqual(head, SPAWN["prompt"])
            self.assertTrue(sep)
            self.assertTrue(tail.startswith("[shunt]"))

    def test_it_names_the_host(self):
        with HookConf() as c:
            c.route_to("h2")
            self.assertIn("@h2", child_prompt(spawn(c)))

    def test_it_says_the_childs_bash_runs_there(self):
        with HookConf() as c:
            c.route_to("h1")
            note = child_prompt(spawn(c))
            self.assertIn("bash", note)
            self.assertIn("THERE", note)

    def test_it_says_the_file_tools_do_not_follow(self):
        """The gap this closes is not hypothetical for a child: the once-per-host warning
        the file tools carry is on a budget SHARED with the parent, so a parent that has
        already spent it leaves every child unwarned about Grep for the rest of the
        session."""
        with HookConf() as c:
            c.route_to("h1")
            note = child_prompt(spawn(c))
            self.assertIn("Grep", note)
            self.assertIn("LOCAL disk", note)

    def test_it_names_the_way_home(self):
        with HookConf() as c:
            c.route_to("h1")
            self.assertIn("@local", child_prompt(spawn(c)))

    def test_and_the_price_of_taking_it(self):
        """The measured fact that shaped this text: parent and children share ONE
        session_id, so the routing is one slot. A child that goes `@local` takes its
        parent — and its siblings — with it. Without this sentence the note would be
        handing out a footgun politely."""
        with HookConf() as c:
            c.route_to("h1")
            note = child_prompt(spawn(c))
            self.assertIn("whole session", note)
            self.assertIn("spawned you", note)


# ── the input the note travels in ──────────────────────────────────────────────


class TestNothingElseIsLostOnTheWay(unittest.TestCase):
    """`updatedInput` replaces a tool's arguments. A note written by returning only the
    prompt would strip `subagent_type` from the call — the hook would silently change
    WHICH agent runs, which is far worse than never writing a note at all."""

    def test_every_other_field_survives(self):
        with HookConf() as c:
            c.route_to("h1")
            updated = spawn(c)["updatedInput"]
            for field, value in SPAWN.items():
                if field != "prompt":
                    self.assertEqual(updated.get(field), value, f"{field} was lost")

    def test_unknown_fields_survive_too(self):
        """The input belongs to the harness and may grow; a hook that only knows today's
        fields must still hand tomorrow's back."""
        with HookConf() as c:
            c.route_to("h1")
            call = dict(SPAWN, model="haiku", some_future_field={"a": 1})
            updated = spawn(c, call)["updatedInput"]
            self.assertEqual(updated.get("model"), "haiku")
            self.assertEqual(updated.get("some_future_field"), {"a": 1})

    def test_a_call_with_no_prompt_is_left_alone(self):
        """Nothing to write into. The call passes untouched rather than being reshaped by
        a hook that cannot see the whole of it."""
        with HookConf() as c:
            c.route_to("h1")
            reply = spawn(c, {"subagent_type": "detective"})
            self.assertNotIn("updatedInput", reply)
            self.assertIn("INHERITS", reply.get("additionalContext", ""))

    def test_a_prompt_too_long_to_hand_back_keeps_the_parents_warning(self):
        """This reply carries the child's WHOLE prompt back, so its size is the caller's,
        not ours — and the harness caps what a hook may say. A reply cut off mid-string is
        not JSON: the harness would log a parse error, run the original call, and the
        parent would lose the warning it has always had. So past the budget the note is
        dropped and the warning goes out alone."""
        with HookConf() as c:
            c.route_to("h1")
            reply = spawn(c, dict(SPAWN, prompt="x" * (pretool.AGENT_REPLY_BUDGET + 100)))
            self.assertNotIn("updatedInput", reply)
            self.assertIn("INHERITS", reply["additionalContext"])

    def test_an_ordinary_prompt_is_nowhere_near_the_budget(self):
        """The other direction: the guard must not be firing on everyday briefs."""
        with HookConf() as c:
            c.route_to("h1")
            self.assertIsNotNone(child_prompt(spawn(c, dict(SPAWN, prompt="a task. " * 200))))

    def test_no_permission_is_granted_along_the_way(self):
        """The hook says things; it does not decide whether a spawn may happen. A
        permissionDecision here would quietly overrule whatever rule the caller has."""
        with HookConf() as c:
            c.route_to("h1")
            self.assertNotIn("permissionDecision", spawn(c))


# ── the parent still hears it, in the same breath ──────────────────────────────


class TestTheParentIsStillWarned(unittest.TestCase):
    """Two readers, one event: the parent decides whether the spawn belongs over there,
    the child needs to know where it is standing. The note must not have cost the
    warning."""

    def test_the_warning_rides_in_the_same_reply(self):
        with HookConf() as c:
            c.route_to("h1")
            reply = spawn(c)
            self.assertIn("INHERITS", reply["additionalContext"])
            self.assertIsNotNone(child_prompt(reply))

    def test_the_warning_still_names_the_host(self):
        with HookConf() as c:
            c.route_to("h1")
            self.assertIn("@h1", spawn(c)["additionalContext"])

    def test_every_spawn_is_told(self):
        """No budget: each spawn is a new agent that has heard nothing."""
        with HookConf() as c:
            c.route_to("h1")
            spawn(c)
            second = spawn(c)
            self.assertIsNotNone(child_prompt(second))
            self.assertIn("INHERITS", second["additionalContext"])

    def test_a_grep_cannot_spend_the_spawns_word(self):
        """The file tools share a once-per-host budget. Agent must not draw on it — one
        Grep would otherwise leave every later child born into silence."""
        with HookConf() as c:
            c.route_to("h1")
            run_tool(c, "Grep", {"pattern": "x"})
            self.assertIsNotNone(child_prompt(spawn(c)))

    def test_it_never_blocks(self):
        with HookConf() as c:
            c.route_to("h1")
            _, code = run_tool(c, "Agent", dict(SPAWN))
            self.assertEqual(code, 0)


# ── and where it stays quiet ───────────────────────────────────────────────────


class TestWhereNoNoteIsWritten(unittest.TestCase):
    def test_a_local_session_spawns_untouched(self):
        """By far the common case. A frame about a remote host in every agent prompt of
        every local session would be noise inside somebody else's task."""
        with HookConf() as c:
            self.assertEqual(spawn(c), {})

    def test_the_unreadable_state_warns_the_parent_but_writes_no_note(self):
        """Deliberate, and the asymmetry is the reasoning: while the routing cannot be
        read, main() REFUSES every bash command with both ways out named — so the child
        learns from its own first command. The remote state is the silent one, where the
        commands succeed on a machine nobody mentioned."""
        with HookConf() as c:
            c.break_routing()
            reply = spawn(c)
            self.assertIn("unreadable", reply["additionalContext"])
            self.assertNotIn("updatedInput", reply)

    def test_other_tools_are_not_reshaped(self):
        """Only the Agent call carries a prompt for a reader; Read and Grep get the
        warning they always got, and their input is none of the hook's business."""
        with HookConf() as c:
            c.route_to("h1")
            reply, _ = run_tool(c, "Read", {"file_path": "/etc/hosts"})
            self.assertIn("LOCAL disk", reply["additionalContext"])
            self.assertNotIn("updatedInput", reply)


if __name__ == "__main__":
    unittest.main()
