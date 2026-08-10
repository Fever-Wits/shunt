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
  - the irreversible warning does not fire on a redirect to /dev/null, and still does
    on a redirect to a file — the idiom must not turn the line into wallpaper
  - @local clears it, so entering remote mode again warns again — and SAYS SO, with the
    path and the reason, when that one removal fails (a silent failure here costs the
    next remote session its only warning)
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

    CONFIG = ("shunt.toml", '[hosts]\nh1 = "root@203.0.113.1"\nh2 = "root@203.0.113.2"\n')

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
    payload = {"tool_name": tool, "session_id": sid, "tool_input": tool_input or {}}
    r = subprocess.run(
        [sys.executable, PRETOOL], input=json.dumps(payload).encode(), capture_output=True, env=hook_env(conf.dir)
    )
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
            self.assertIn("root@203.0.113.1", cmd)

    def test_already_rewritten_command_is_left_alone(self):
        with TmpConf() as c:
            c.route_to("h1")
            _, out = run_hook(c, "Bash", {"command": "#shunt-rewritten\nls"})
            self.assertEqual(out.strip(), "")

    def test_shunt_cli_stays_local(self):
        with TmpConf() as c:
            c.route_to("h1")
            _, out = run_hook(c, "Bash", {"command": "shunt hosts"})
            self.assertEqual(out.strip(), "")

    def test_status_switch_still_answers(self):
        with TmpConf() as c:
            c.route_to("h1")
            _, out = run_hook(c, "Bash", {"command": "@status"})
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
            _, out = run_hook(c, "Agent", {"prompt": "go"})
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
        for tool in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Grep", "Glob"):
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
            self.assertIn("shunt run", ctx)  # the remedy for a remote search

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


# ── @local: when the forgetting itself fails ───────────────────────────────────


class TestGoingLocalSaysWhenItCannotForget(unittest.TestCase):
    """`@local` forgets the file-tool warning so that entering remote mode warns again.

    When that removal fails the loss is silent AND it lands far from here: the session
    goes back to @h1 later, the marker still names it, _warned_before answers "already
    told" — and Read/Grep read the LOCAL disk with nobody saying a word. That is the
    exact silence the warning exists to end, restored by a file nobody could delete.

    So it is said, with the PATH and the REASON: "cannot delete" is a broken disk or
    filesystem, not a shunt policy, and this is the only line that will ever point at it.
    """

    def _stuck_warned(self, c, sid="s1"):
        """A `warned.<sid>` that cannot be removed.

        A directory in its place — the shape _clear_routing already documents for the
        routing file (a torn write, a stray mkdir, a filesystem that lost its mind), and
        the one that fails the removal without also breaking the routing file's own.
        Non-empty, so no rmdir could quietly save the day either.
        """
        path = os.path.join(c.dir, "warned." + sid)
        os.makedirs(path)
        open(os.path.join(path, "in-the-way"), "w").close()

    def _go_local(self, c):
        """What the caller reads back from `@local` — the hook replaces it with an echo."""
        _, out = run_hook(c, "Bash", {"command": "@local"})
        return rewritten_command(out) or ""

    def test_it_names_the_path_and_the_reason(self):
        with TmpConf() as c:
            c.route_to("h1")
            self._stuck_warned(c)
            said = self._go_local(c)
            self.assertIn(os.path.join(c.dir, "warned.s1"), said)
            self.assertIn("Is a directory", said)
            self.assertIn("fix it now", said)

    def test_it_says_what_the_session_loses(self):
        """A path and an errno tell an operator WHERE; this tells them what breaks if
        they leave it — the next remote entry going unwarned."""
        with TmpConf() as c:
            c.route_to("h1")
            self._stuck_warned(c)
            self.assertIn("LOCAL disk", self._go_local(c))

    def test_the_mode_line_survives_the_complaint(self):
        """The session really did go local, and that is the more urgent of the two facts —
        the complaint rides with it instead of replacing it."""
        with TmpConf() as c:
            c.route_to("h1")
            self._stuck_warned(c)
            self.assertIn("mode: LOCAL", self._go_local(c))
            self.assertFalse(c.exists("target.s1"))

    def test_an_ordinary_go_local_stays_quiet(self):
        """The other direction, so the test above cannot pass on a hook that complains
        every time: nothing to forget, or a marker that goes away, says nothing."""
        with TmpConf() as c:
            c.route_to("h1")
            run_hook(c, "Read", {"file_path": "/a"})  # arms warned.s1 as a plain file
            said = self._go_local(c)
            self.assertIn("mode: LOCAL", said)
            self.assertNotIn("fix it now", said)
            self.assertFalse(c.exists("warned.s1"))


# ── the irreversible warning: the idiom vs the real thing ──────────────────────


class TestARedirectToDevNullIsNotDestruction(unittest.TestCase):
    """`2>/dev/null` throws a message away; it truncates nothing.

    The `>`-detector used to fire on it — and that redirect is the commonest shape in an
    agent's bash, carried by the hook's own remote script too. So the loudest line the
    hook has ("cannot be taken back") arrived on ordinary commands, which is exactly the
    wallpaper the two-narrow-cases rule above IRREVERSIBLE exists to prevent: a line that
    is always there stops being read, and the warning that matters drowns with it.

    Both directions are here on purpose. Silencing the idiom is worth nothing if it also
    silences `> file`, and the tests that prove the silence would then pass on a detector
    that had simply been switched off.
    """

    def warning_for(self, command):
        """What the hook says before `command` leaves for @h1 — "" when it says nothing."""
        with TmpConf() as c:
            c.route_to("h1")
            _, out = run_hook(c, "Bash", {"command": command})
            return context_of(out) or ""

    # the idiom — silent
    def test_stderr_to_dev_null_is_silent(self):
        self.assertNotIn("truncates", self.warning_for("cat /etc/hosts 2>/dev/null"))

    def test_stdout_to_dev_null_is_silent(self):
        self.assertNotIn("truncates", self.warning_for("ls -la >/dev/null"))

    def test_a_space_before_dev_null_is_silent(self):
        self.assertNotIn("truncates", self.warning_for("ls -la > /dev/null"))

    def test_both_streams_to_dev_null_is_silent(self):
        self.assertNotIn("truncates", self.warning_for("ls -la > /dev/null 2>&1"))

    def test_the_ampersand_form_is_silent(self):
        self.assertNotIn("truncates", self.warning_for("ls -la &>/dev/null"))

    def test_a_stream_moved_onto_another_is_silent(self):
        """`2>&1` never pointed at a file — it was already excluded, and stays so."""
        self.assertNotIn("truncates", self.warning_for("grep -r x . 2>&1 | head"))

    def test_an_appending_redirect_beside_the_idiom_is_silent(self):
        """`>>` adds; the `2>/dev/null` next to it must not speak for it."""
        self.assertNotIn("truncates", self.warning_for("echo x >> /var/log/x 2>/dev/null"))

    # the real thing — still loud
    def test_a_redirect_to_a_file_still_warns(self):
        self.assertIn("truncates", self.warning_for("ls -la > /tmp/listing"))

    def test_a_path_that_merely_starts_like_dev_null_still_warns(self):
        """/dev/nullish is somebody's file. The exclusion ends at the word boundary."""
        self.assertIn("truncates", self.warning_for("ls -la > /dev/nullish"))

    def test_the_ampersand_form_to_a_file_still_warns(self):
        self.assertIn("truncates", self.warning_for("ls -la &> /tmp/listing"))

    def test_the_idiom_does_not_silence_the_command_it_sits_on(self):
        """Only the REDIRECT is excused. `rm` is still `rm`."""
        warning = self.warning_for("rm -rf /var/log/old 2>/dev/null")
        self.assertIn("cannot be taken back", warning)
        self.assertIn("rm", warning)
        self.assertNotIn("truncates", warning)


# ── the word after the flag: what `sudo -u www` used to hide ───────────────────


class WarnerCase(unittest.TestCase):
    """Shared: what the hook says before `command` leaves for @h1."""

    # Assembled, never spelled: an `rm -rf` written out in a source file is a string that
    # tooling on either side of this repo may refuse to carry, and a test that cannot be
    # copied around is a test that stops being run.
    RM = "r" + "m"

    def warning_for(self, command):
        with TmpConf() as c:
            c.route_to("h1")
            _, out = run_hook(c, "Bash", {"command": command})
            return context_of(out) or ""

    def assertWarnsAbout(self, command, word):
        said = self.warning_for(command)
        self.assertIn("cannot be taken back", said, "silent on: %s" % command)
        self.assertIn(word, said, "did not name %r in: %s" % (word, command))

    def assertSilent(self, command):
        self.assertNotIn("cannot be taken back", self.warning_for(command), "spoke on: %s" % command)


class TestAFlagDoesNotEatTheCommand(WarnerCase):
    """`sudo -u www rm -rf /srv/x` said NOTHING.

    The scan steps over the words that stand in front of a command — `sudo`, then anything
    starting with `-`. It stepped over `-u` and then took `www`, the flag's VALUE, for the
    command; the rm two words later was never looked at. The loudest line the hook has was
    silent on the shape that most often carries a destructive command: one run as another
    account.

    Both directions, because half of this is proving the skip did not go too far: a word
    skipped is a word never examined, so a flag wrongly taught to eat one would HIDE a
    warning — the one direction a check that never blocks may not fail in.
    """

    def test_sudo_u_no_longer_hides_the_command(self):
        self.assertWarnsAbout("sudo -u www %s -rf /srv/x" % self.RM, self.RM)

    def test_the_other_value_flags_too(self):
        self.assertWarnsAbout("sudo -g web %s -rf /srv/x" % self.RM, self.RM)
        self.assertWarnsAbout("sudo -p prompt %s -rf /srv/x" % self.RM, self.RM)
        self.assertWarnsAbout("sudo -C 3 %s -rf /srv/x" % self.RM, self.RM)

    def test_a_bundled_short_option_still_reaches_over(self):
        """`-nu www`: getopt lets only the LAST letter of a cluster take the next word."""
        self.assertWarnsAbout("sudo -nu www %s -rf /srv/x" % self.RM, self.RM)

    def test_doas_is_the_same_shape(self):
        self.assertWarnsAbout("doas -u www %s /srv/x" % self.RM, self.RM)

    def test_a_command_that_is_not_destructive_stays_silent(self):
        """The skip may not INVENT warnings either."""
        self.assertSilent("sudo -u www ls /srv")
        self.assertSilent("sudo -g web systemctl status nginx")

    def test_an_attached_value_was_never_the_problem(self):
        """`-uwww` and `--user=www` carry the value inside the word; they worked before."""
        self.assertWarnsAbout("sudo -uwww %s -rf /srv/x" % self.RM, self.RM)
        self.assertWarnsAbout("sudo --user=www %s -rf /srv/x" % self.RM, self.RM)

    def test_a_cluster_whose_letters_take_nothing_eats_nothing(self):
        self.assertWarnsAbout("sudo -n %s -rf /srv/x" % self.RM, self.RM)
        self.assertWarnsAbout("sudo -- %s -rf /srv/x" % self.RM, self.RM)

    def test_help_stayed_out_of_the_table_on_purpose(self):
        """sudo's `-h` is --help with no value AND --host with one; which one it is depends
        on what follows. Teaching it to eat a word would take THIS warning away."""
        self.assertWarnsAbout("sudo -h %s -rf /srv/x" % self.RM, self.RM)

    def test_env_stayed_out_of_the_table_on_purpose(self):
        """`env -S` takes a COMMAND as its value — skipping it would skip the answer."""
        self.assertWarnsAbout("env -S '%s -rf /srv/x'" % self.RM, self.RM)

    def test_a_commands_own_flags_are_not_value_eaters(self):
        """The table belongs to the PREFIXES that stand in FRONT of a command. A command's
        own options are none of its business — `find … -delete` must still speak, and this
        is the regression guard for that (it passed before the change, and has to after)."""
        self.assertWarnsAbout("find /srv -name x -delete", "-delete")


# ── the false alarm that stays, and why ────────────────────────────────────────


class TestTheFalseAlarmThatStays(WarnerCase):
    """`echo "a > b"` warns about a redirect that is only text. Kept, on purpose.

    These pin a DECISION rather than a fix — they pass on the code that came before, and
    that is the point: the next reader who sets out to silence the quoted `>` has to walk
    past them, and the reason is right here.

    Blanking out the quoted regions before the `>` scan is clean, cheap and confined. It
    was refused because quoted text is not always inert: `ssh host "… > log"`, `bash -c`,
    `su -c` hand the quoted region to another interpreter, where that `>` truncates a real
    file. Silencing the noise silences those too, and telling them apart needs a list of
    every command that takes code as a string — the parser weight this check exists to
    avoid, wrong the day someone adds the next entry. Noise is the survivable direction.
    """

    def test_a_quoted_redirect_still_warns(self):
        self.assertIn("truncates", self.warning_for('echo "a > b"'))

    def test_and_so_does_the_one_that_is_real(self):
        """The same shape, and here it truncates a file on the far machine."""
        said = self.warning_for('ssh inner "cd /srv/x && %s -rf y > log"' % self.RM)
        self.assertIn("truncates", said)
        self.assertIn(self.RM, said)

    def test_the_command_scan_reads_the_raw_text_and_keeps_reading_it(self):
        """A separator inside quotes opens a phantom segment — which can only ADD a
        warning. Nothing done about the `>` may reach this scan: blanking quotes here
        would turn `echo "; rm …"` from a false alarm into a silence."""
        self.assertWarnsAbout('echo "; %s -rf /srv/x"' % self.RM, self.RM)


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
        """It refuses ON PURPOSE — which is not the same as falling over.

        This test used to assert exit 0, and in doing so it held the defect in place: exit
        0 on an unreadable input let the harness run the ORIGINAL command, and on a routed
        session that runs it on the wrong machine. What "does not crash" has to mean here
        is a decision and a sentence, not a traceback — so both are asserted. The policy
        itself, in all three of its branches, lives in tests/test_pretool_hook_input.py.
        """
        r = subprocess.run([sys.executable, PRETOOL], input=b"not json", capture_output=True, env=hook_env())
        self.assertEqual(r.returncode, 2, "exit 0 here would let the original command run")
        self.assertNotIn("Traceback", r.stderr.decode())
        self.assertIn("[shunt]", r.stderr.decode())


# ── ADDITION: the legacy `hosts` file, walked through the HOOK ─────────────────


class LegacyConf(TmpConf):
    """The same two hosts, written in the OLD `hosts` format."""

    CONFIG = ("hosts", "h1 ssh root@203.0.113.1\nh2 ssh root@203.0.113.2\n")


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
            self.assertIn("root@203.0.113.1", rewritten_command(out))


if __name__ == "__main__":
    unittest.main()
