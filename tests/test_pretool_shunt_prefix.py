"""
Tests for shunt.pretool — the `shunt ` prefix speaks for ONE command, not for a whole line.

`shunt …` is the one thing the hook must NOT redirect: the CLI does its own transport, so
it belongs on this machine in any mode. The prefix was read off the START of the line,
though, and a line has room for more than one command. In remote mode
`shunt hosts; rm -rf /var/log/*` therefore ran WHOLE, here, and said nothing — while the
mirror of that mistake (a shunt command sent to a host) comes back loudly as "command not
found". The guarded direction was the cheap one.

The fix lets through only a line that can hold no second command, and refuses the rest the
way this file already refuses an unresolvable alias: the command is REPLACED by a message,
so what runs is an echo and never the line. Refusing rather than warning, because a
warning does not stop anything — and this is the case that must be stopped.

Coverage:
  - the two lines the audit proved: `;` and `&&` — neither reaches the shell any more, and
    the destructive half is nowhere in what does run
  - refusing means neither machine runs it: no ssh either, and no `rm` anywhere in it
  - it is a rewrite, not a block — the exit code stays 0, as everywhere in this hook
  - every separator that can begin a command is seen: ; & | ` $ ( and a newline
  - a single `shunt …` line, and a bare `shunt`, still run here untouched — including
    `shunt run @h "…"`, the shape the hook's own warning recommends
  - a LOCAL session is left alone: nothing is routed anywhere, so `shunt hosts | grep web`
    is ordinary work and a refusal there would be pure noise
  - the message names the host, the separator it saw, and the way out
  - a routing state that cannot be read refuses too, without inventing a host
  - THE MIRROR SHAPE: `shunt …` that is not the first word of the line
    (`cat f | shunt edit @h1 /x --stdin`) — no prefix, so nothing above ever saw it, and
    the whole line was shipped to a host where `shunt` does not exist while the half in
    front of the pipe ran over there on the wrong machine's files

The hook is run as the harness runs it — a subprocess fed JSON on stdin, started BY PATH
with PYTHONPATH stripped — and SHUNT_CONF points at a temp directory, so no real config or
session state is touched.
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

# The two lines the audit ran against a live remote session. Both exited silently, which
# meant both had just run — here, in full.
AUDIT_CASES = ("shunt hosts; rm -rf /var/log/*", "shunt log -n 3 && rm -rf ./build")


class HookConf:
    """A temp SHUNT_CONF with one host, plus the session files the hook reads."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-prefix-")
        with open(os.path.join(self.dir, "shunt.toml"), "w") as f:
            f.write('[hosts]\nh1 = "root@203.0.113.1"\n')
        return self

    def __exit__(self, *_):
        shutil.rmtree(self.dir, ignore_errors=True)

    def route_to(self, alias, sid="s1"):
        """Pretend the session already switched to @alias."""
        with open(os.path.join(self.dir, "target." + sid), "w") as f:
            f.write(alias)

    def break_routing(self, sid="s1"):
        """The routing file exists and names no host — an emptied or half-written file."""
        open(os.path.join(self.dir, "target." + sid), "w").close()


def run_hook(conf, command, sid="s1"):
    """Run pretool.py exactly as the harness does. Returns (exit code, stdout).

    PYTHONPATH is stripped for the same reason tests/test_pretool_warnings.py strips it:
    settings.json names pretool.py by absolute path, so the field never has the package on
    sys.path, and the test runner's PYTHONPATH would hide that.
    """
    payload = {"tool_name": "Bash", "session_id": sid, "tool_input": {"command": command}}
    env = dict(os.environ, SHUNT_CONF=conf.dir)
    env.pop("PYTHONPATH", None)
    r = subprocess.run([sys.executable, PRETOOL], input=json.dumps(payload).encode(), capture_output=True, env=env)
    return r.returncode, r.stdout.decode()


def ran_instead(stdout):
    """The command the hook put in place of the caller's — None when it said nothing.

    None is the answer that matters here: silence from this hook means the caller's own
    line went to the shell untouched.
    """
    if not stdout.strip():
        return None
    return json.loads(stdout)["hookSpecificOutput"]["updatedInput"]["command"]


# ── the two lines the audit proved ─────────────────────────────────────────────


class TestTheTwoProvenCases(unittest.TestCase):
    def _refusal(self, line):
        with HookConf() as c:
            c.route_to("h1")
            code, out = run_hook(c, line)
            said = ran_instead(out)
            self.assertIsNotNone(said, "the hook said nothing — which means this line ran, here, whole")
            return code, said

    def test_neither_is_let_through(self):
        for line in AUDIT_CASES:
            with self.subTest(line=line):
                self._refusal(line)

    def test_the_destructive_half_is_not_what_runs(self):
        """The assertion in its strongest form: whatever went to the shell in place of the
        line, the `rm` is not in it."""
        for line in AUDIT_CASES:
            with self.subTest(line=line):
                _, said = self._refusal(line)
                self.assertNotIn("rm -rf", said)

    def test_what_runs_instead_says_so(self):
        for line in AUDIT_CASES:
            with self.subTest(line=line):
                _, said = self._refusal(line)
                self.assertTrue(said.startswith("echo "), said)
                self.assertIn("NOT run", said)

    def test_the_tail_is_not_sent_to_the_far_machine_either(self):
        """The other wrong answer: rewriting the whole line over ssh would delete the
        SERVER's /var/log instead of ours. Refused means neither machine runs it."""
        for line in AUDIT_CASES:
            with self.subTest(line=line):
                _, said = self._refusal(line)
                self.assertNotIn("ssh ", said)

    def test_the_refusal_is_still_exit_zero(self):
        """A rewrite, not a block. The exit code is the harness' own channel and this hook
        has never spoken through it — every path here ends in `sys.exit(0)`. What stops the
        command is the JSON, exactly as it is for an unresolvable alias."""
        for line in AUDIT_CASES:
            with self.subTest(line=line):
                code, _ = self._refusal(line)
                self.assertEqual(code, 0)


# ── the whole class, not the two instances ─────────────────────────────────────


class TestEverySeparatorThatCanBeginACommand(unittest.TestCase):
    """`;` and `&&` were the two that were tried. The check is written against the class:
    anything that can put a second command on the line."""

    SEPARATORS = {
        "semicolon": "shunt hosts; rm -rf /var/log/*",
        "and": "shunt log -n 3 && rm -rf ./build",
        "or": "shunt hosts || rm -rf ./build",
        "background": "shunt hosts & rm -rf ./build",
        "pipe": "shunt hosts | xargs rm -rf",
        "backtick": "shunt run @h1 `rm -rf ./build`",
        "substitution": "shunt run @h1 $(rm -rf ./build)",
        # Not a shell anyone would write — the `(` is what is under test, and it arrives
        # on its own in a subshell as surely as it does inside `$(`.
        "paren": "shunt run @h1 (rm -rf ./build)",
        "newline": "shunt hosts\nrm -rf /var/log/*",
    }

    def test_none_of_them_is_let_through(self):
        for name, line in self.SEPARATORS.items():
            with self.subTest(separator=name), HookConf() as c:
                c.route_to("h1")
                _, out = run_hook(c, line)
                self.assertIsNotNone(ran_instead(out), f"{name} got through: the line ran here, whole")


# ── what must keep working ─────────────────────────────────────────────────────


class TestASingleShuntCommandStillRunsHere(unittest.TestCase):
    """The reason the branch exists at all — it must not become collateral damage."""

    PLAIN = (
        "shunt",
        "shunt hosts",
        "shunt log -n 3",
        "shunt read @h1 /etc/hosts",
        'shunt run @h1 "grep -rn PATTERN /path"',
    )

    def test_they_pass_through_untouched_in_remote_mode(self):
        for line in self.PLAIN:
            with self.subTest(line=line), HookConf() as c:
                c.route_to("h1")
                code, out = run_hook(c, line)
                self.assertEqual(code, 0)
                self.assertEqual(out.strip(), "", "a plain shunt line was interfered with")


class TestALocalSessionIsLeftAlone(unittest.TestCase):
    """Nothing is routed anywhere, so the line runs here either way and there is no
    surprise to protect anyone from. Refusing here would only take working shapes away."""

    def test_a_compound_shunt_line_runs_as_it_always_did(self):
        with HookConf() as c:
            code, out = run_hook(c, "shunt hosts | grep web")
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "")

    def test_even_the_audit_line_is_local_business_when_local(self):
        with HookConf() as c:
            _, out = run_hook(c, "shunt hosts; rm -rf ./build")
            self.assertEqual(out.strip(), "")


# ── what the refusal says ──────────────────────────────────────────────────────


class TestWhatTheRefusalSays(unittest.TestCase):
    """A refusal that does not carry its way out is a wall. The false positives of a check
    this blunt — `shunt hosts | grep web` in remote mode — are paid for here, in one line
    the reader can act on."""

    def test_it_names_the_host_and_the_way_out(self):
        with HookConf() as c:
            c.route_to("h1")
            _, out = run_hook(c, AUDIT_CASES[0])
            said = ran_instead(out)
            self.assertIn("@h1", said)
            self.assertIn("@local", said)

    def test_it_names_the_separator_it_saw(self):
        with HookConf() as c:
            c.route_to("h1")
            _, out = run_hook(c, "shunt log -n 3 && rm -rf ./build")
            self.assertIn("&", ran_instead(out))

    def test_a_newline_is_named_in_words(self):
        """The character itself would break the echo in two, the second half a fragment of
        our own message arriving as a command."""
        with HookConf() as c:
            c.route_to("h1")
            _, out = run_hook(c, "shunt hosts\nrm -rf /var/log/*")
            said = ran_instead(out)
            self.assertIn("newline", said)
            self.assertNotIn("\n", said)


# ── the routing state this branch cannot read ──────────────────────────────────


class TestAnUnreadableRoutingState(unittest.TestCase):
    """The third state of `target.<sid>` reaches this branch as well. It is not "local",
    so the line is refused — and the message may not name a host nobody read."""

    def test_a_compound_line_is_refused(self):
        with HookConf() as c:
            c.break_routing()
            _, out = run_hook(c, AUDIT_CASES[0])
            self.assertIn("NOT run", ran_instead(out))

    def test_no_sentinel_leaks_into_the_message(self):
        """The unreadable state is an object; formatted into a string it would arrive as
        `<object object at 0x…>` and teach the reader nothing."""
        with HookConf() as c:
            c.break_routing()
            _, out = run_hook(c, AUDIT_CASES[0])
            self.assertNotIn("object at", ran_instead(out))

    def test_a_single_shunt_command_still_runs_here(self):
        """`shunt hosts` is how a broken state gets diagnosed — taking it away exactly
        then would be the worst possible moment."""
        with HookConf() as c:
            c.break_routing()
            code, out = run_hook(c, "shunt hosts")
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "")


class TestShuntPastTheStartOfTheLine(unittest.TestCase):
    """The mirror of everything above: `shunt …` that is NOT the first word.

    `cat payload.json | shunt edit @h1 /etc/nginx.conf --stdin` carries no shunt PREFIX,
    so the guard above never looks at it — and in remote mode the WHOLE line was rewritten
    and shipped to a machine where `shunt` is not installed. An older comment called this
    the loud direction, and it is: "command not found", for a tool the caller has, blamed
    on a machine they were not thinking about. Loud is not clear, and it is not harmless —
    the half in FRONT of the pipe had already run over there, silently, on the far
    machine's files.
    """

    PIPED = "cat payload.json | shunt edit @h1 /etc/nginx.conf --stdin"

    def refusal(self, line, conf=None):
        with HookConf() as c:
            c.route_to("h1")
            code, out = run_hook(c, line)
            said = ran_instead(out)
            self.assertIsNotNone(said, "the hook said nothing — the line went to the host whole")
            self.assertEqual(code, 0, "a refusal here is a rewrite, not a block")
            return said

    def test_the_piped_shunt_line_is_refused(self):
        said = self.refusal(self.PIPED)
        self.assertIn("NOT run", said)

    def test_nothing_of_the_line_leaves_for_the_host(self):
        """Neither half: not the `shunt` call that would fail there, and not the `cat`
        that would read the WRONG machine's copy of the file."""
        said = self.refusal(self.PIPED)
        self.assertNotIn("ssh", said)
        self.assertNotIn("cat payload.json", said)

    def test_the_message_names_the_host_the_separator_and_the_way_out(self):
        said = self.refusal(self.PIPED)
        self.assertIn("@h1", said)
        self.assertIn("`|`", said)
        self.assertIn("@local", said)

    def test_every_separator_that_can_begin_a_command_is_seen(self):
        for line in (
            "echo hi; shunt hosts",
            "make build && shunt cp @h1 ./out /srv",
            "cat f | shunt edit @h1 /x --stdin",
            "echo `shunt hosts`",
            "echo $(shunt hosts)",
            "echo hi\nshunt hosts",
        ):
            with self.subTest(line=line):
                self.assertIn("NOT run", self.refusal(line))

    def test_a_newline_is_named_in_words(self):
        """A raw newline printed into the message would break the sentence in half."""
        self.assertIn("newline", self.refusal("echo hi\nshunt hosts"))

    def test_a_local_session_is_left_alone(self):
        """Nothing is routed anywhere, so `shunt` runs here and so does the rest of the
        line — which is what the caller meant. A refusal here would be pure noise."""
        with HookConf() as c:
            code, out = run_hook(c, self.PIPED)
            self.assertEqual(code, 0)
            self.assertIsNone(ran_instead(out))

    def test_a_word_that_merely_contains_shunt_is_not_shunt(self):
        """`shunt` has to be a WORD in command position, or every line mentioning the tool
        would be refused."""
        with HookConf() as c:
            c.route_to("h1")
            for line in ("ls | myshunt x", "ls | shunt2 x", "ls | grep shunt", "ls /opt/shuntx | wc -l"):
                with self.subTest(line=line):
                    _, out = run_hook(c, line)
                    self.assertIn("ssh", ran_instead(out) or "", "%s should have gone to the host" % line)

    def test_the_price_is_paid_in_the_cheap_direction(self):
        """A separator inside quotes costs a refusal nobody needed — the same trade the
        guard above makes, and the same reason: the other direction costs a command that
        leaves unnoticed."""
        self.assertIn("NOT run", self.refusal('echo "pipe it: cmd | shunt read @h1 /x"'))


if __name__ == "__main__":
    unittest.main()
