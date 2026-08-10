"""
Tests for pretool.py — the ticket that speaks for the way HOME, and the state-writes
around it that used to fail in silence.

The hook has always said a word before the first command after `@alias`: "this runs THERE,
not here". The way back said nothing at all — `@local` cleared the marker and the next
command ran here without comment. That asymmetry has a cost measured in a session working
across two machines: *sometimes I forget where I am*. Forgetting is symmetric; the reminder
was not. So the marker is no longer CLEARED by `@local`, it CHANGES SIDES: one ticket, one
file, whichever direction the switch went, and the dance @a → @b → local → @c carries a
word at every step instead of at every second one.

That is one behaviour. The rest of this file is the class of defect it uncovered on the way:
every state-write around the ticket that answered a broken config dir with `pass`. Each of
them is quiet in a way that lands far from its cause —

  arming on `@alias` — no reminder, and no once-per-switch housekeeping on the far side at
      all: the sweep never runs, and the probe that says "this session cannot remember its
      working directory" never speaks. The session then works for hours with a cwd that is
      silently not being written.
  READING the marker — a directory in its place used to read as "not armed", so the ticket
      could never be spent: no reminder, ever, and no housekeeping, ever, with nothing
      anywhere saying why. Unreadable is now a state of its OWN — not absent (which is
      silence) and not armed (which would have the hook claim a switch it never read, and
      buy the far side's housekeeping with it).
  the active-host sidecar on `@local` — nothing in the hook reads that file, which is
      exactly why its failure must be said: it is written for the OUTSIDE, and a stale one
      tells a reader this session is on @h1 while it is home.

Coverage:
  - the first command after `@local` says it runs HERE; the second says nothing
  - a session that never switched hears nothing, ever (the common case, and the point)
  - the note rides ALONG with the command — nothing local is rewritten
  - `@local` arms the ticket instead of removing it, and no host can spend it
  - the last switch wins in both directions
  - each of the three silent writes above, said with the path and the reason
  - and the other direction for each, so none of it can pass on a hook that just complains

⚠ SHUNT_CONF is a temp directory in every test, and the stub `ssh` means no switch here
opens a connection.
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
    """A temp SHUNT_CONF with one host and a stub `ssh` (the switch probes)."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-ticket-")
        with open(os.path.join(self.dir, "shunt.toml"), "w") as f:
            f.write('[hosts]\nh1 = "root@203.0.113.9"\nh2 = "root@203.0.113.10"\n')
        self.bin = os.path.join(self.dir, "_bin")
        os.makedirs(self.bin)
        with open(os.path.join(self.bin, "ssh"), "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(os.path.join(self.bin, "ssh"), 0o755)
        return self

    def __exit__(self, *_):
        os.chmod(self.dir, 0o700)  # a frozen-dir test must still be able to clean up
        shutil.rmtree(self.dir, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.dir, name)

    def exists(self, name):
        return os.path.exists(self.path(name))

    def read(self, name):
        with open(self.path(name)) as f:
            return f.read().strip()

    def put_a_directory_at(self, name):
        """A DIRECTORY where a state file belongs — a torn write, a stray mkdir, a
        filesystem that lost its mind.

        This shape and not a frozen config dir, deliberately: freezing the whole directory
        breaks the FIRST write of every branch (the routing file), so the hook refuses
        before it ever reaches the state-write under test. A directory in one file's place
        fails that one file and leaves its neighbours working, which is what isolates it.
        Non-empty, so no rmdir can quietly save the day either.
        """
        if os.path.isfile(self.path(name)):
            os.remove(self.path(name))
        os.mkdir(self.path(name))
        open(os.path.join(self.path(name), "in-the-way"), "w").close()


def run_hook(conf, command, sid="s1"):
    """Run pretool.py exactly as the harness does. Returns the parsed hookSpecificOutput."""
    payload = {"tool_name": "Bash", "session_id": sid, "tool_input": {"command": command}}
    env = dict(os.environ, SHUNT_CONF=conf.dir, PATH=conf.bin + os.pathsep + os.environ["PATH"])
    # PYTHONPATH is stripped the way the other hook tests strip it: settings.json names
    # pretool.py by absolute path, so the field never has the package on sys.path.
    env.pop("PYTHONPATH", None)
    r = subprocess.run([sys.executable, PRETOOL], input=json.dumps(payload).encode(), capture_output=True, env=env)
    out = r.stdout.decode().strip()
    return json.loads(out)["hookSpecificOutput"] if out else {}


def context(reply):
    return reply.get("additionalContext", "")


def said(reply):
    return reply.get("updatedInput", {}).get("command", "")


# ── the way home speaks too ────────────────────────────────────────────────────


class TestTheFirstCommandAfterGoingLocal(unittest.TestCase):
    def _go_out_and_back(self, c):
        run_hook(c, "@h1")
        run_hook(c, "@local")

    def test_it_says_this_one_runs_here(self):
        with HookConf() as c:
            self._go_out_and_back(c)
            note = context(run_hook(c, "ls -la"))
            self.assertIn("first command since", note)
            self.assertIn("HERE", note)

    def test_it_names_the_switch_that_earned_the_note(self):
        with HookConf() as c:
            self._go_out_and_back(c)
            self.assertIn("@local", context(run_hook(c, "ls -la")))

    def test_the_second_command_is_quiet(self):
        """One ticket, spent once. A line on every local command is wallpaper, and
        wallpaper is silent exactly when it needs to speak."""
        with HookConf() as c:
            self._go_out_and_back(c)
            run_hook(c, "ls")
            self.assertEqual(context(run_hook(c, "ls")), "")

    def test_the_command_itself_is_not_touched(self):
        """Local means local: the note rides in the same reply, and nothing is rewritten.
        A rewrite here would be the hook putting its own hands on a command it has no
        business changing."""
        with HookConf() as c:
            self._go_out_and_back(c)
            reply = run_hook(c, "ls -la")
            self.assertEqual(said(reply), "")
            self.assertNotEqual(context(reply), "")

    def test_a_session_that_never_switched_hears_nothing(self):
        """By far the common case — every ordinary local session — and the silence there
        is what makes the one line above worth reading."""
        with HookConf() as c:
            self.assertEqual(run_hook(c, "ls -la"), {})

    def test_the_marker_is_gone_after_it_is_spent(self):
        with HookConf() as c:
            self._go_out_and_back(c)
            run_hook(c, "ls")
            self.assertFalse(c.exists("switched.s1"))


# ── one ticket, two directions ─────────────────────────────────────────────────


class TestOneTicketForBothDirections(unittest.TestCase):
    """The same file carries either side's ticket, which is what makes "the last switch
    wins" true without any bookkeeping: a switch overwrites whatever the previous one
    left. The sentinel carries an `@`, and no alias can — `@local` is caught before the
    alias branch, so the string can never arrive as a host name."""

    def test_going_local_arms_instead_of_clearing(self):
        with HookConf() as c:
            run_hook(c, "@h1")
            run_hook(c, "@local")
            self.assertEqual(c.read("switched.s1"), pretool.LOCAL_MARK)

    def test_no_host_can_spend_the_local_ticket(self):
        """Asked of the hook's own function: a ticket that says HOME is not @h1's to
        punch. If it were, the far side's housekeeping would ride on a switch that never
        armed one — a `find` over somebody's disk, bought with the wrong ticket."""
        with HookConf() as c:
            run_hook(c, "@h1")
            run_hook(c, "@local")
            orig, pretool.CONF = pretool.CONF, c.dir
            try:
                self.assertEqual(pretool._spend_switch_marker("s1", "h1"), (False, "", ""))
            finally:
                pretool.CONF = orig
            self.assertTrue(c.exists("switched.s1"), "the host spent a ticket that was not its own")

    def test_the_last_switch_wins_going_out(self):
        with HookConf() as c:
            run_hook(c, "@local")
            run_hook(c, "@h1")
            self.assertEqual(c.read("switched.s1"), "h1")
            self.assertIn("THERE", context(run_hook(c, "ls")))

    def test_the_last_switch_wins_coming_back(self):
        with HookConf() as c:
            run_hook(c, "@h1")
            run_hook(c, "@local")
            self.assertIn("HERE", context(run_hook(c, "ls")))

    def test_the_dance_across_hosts_and_home_says_something_every_step(self):
        """@h1 → @h2 → local → @h1, one command after each. The step that used to be
        silent is the third."""
        with HookConf() as c:
            steps = ["@h1", "@h2", "@local", "@h1"]
            for step in steps:
                run_hook(c, step)
                self.assertIn("first command since", context(run_hook(c, "pwd")), f"silent after {step}")


# ── the writes that used to fail without a word ────────────────────────────────


class TestArmingTheTicketMayNotFailInSilence(unittest.TestCase):
    """`@alias` arming was `except Exception: pass`. The switch does stand without the
    marker — that much was true — but the loss is real and lands far away: no reminder,
    and on the far side no once-per-switch housekeeping AT ALL. The sweep of dead
    sessions' files never runs, and the one probe that reports "this session cannot
    remember its working directory" never speaks, so a session can work for hours with a
    cwd that is silently not being written."""

    def test_the_failure_names_the_path_and_the_reason(self):
        with HookConf() as c:
            c.put_a_directory_at("switched.s1")
            message = said(run_hook(c, "@h1"))
            self.assertIn(c.path("switched.s1"), message)
            self.assertIn("Is a directory", message)
            self.assertIn("fix it now", message)

    def test_it_says_what_the_switch_loses(self):
        """A path and an errno say WHERE; this says what breaks if it is left."""
        with HookConf() as c:
            c.put_a_directory_at("switched.s1")
            message = said(run_hook(c, "@h1"))
            self.assertIn("reminder", message)
            self.assertIn("housekeeping", message)

    def test_the_switch_still_stands_and_says_so(self):
        """The complaint rides WITH the mode line, never instead of it: where the session
        now is remains the more urgent of the two facts."""
        with HookConf() as c:
            c.put_a_directory_at("switched.s1")
            message = said(run_hook(c, "@h1"))
            self.assertIn("mode: REMOTE → h1", message)
            self.assertEqual(c.read("target.s1"), "h1")

    def test_a_healthy_switch_does_not_complain(self):
        with HookConf() as c:
            self.assertNotIn("fix it now", said(run_hook(c, "@h1")))

    def test_going_local_says_it_too(self):
        """The same write, the other direction — and there the loss is the home reminder."""
        with HookConf() as c:
            run_hook(c, "@h1")
            c.put_a_directory_at("switched.s1")
            message = said(run_hook(c, "@local"))
            self.assertIn(c.path("switched.s1"), message)
            self.assertIn("fix it now", message)
            self.assertIn("mode: LOCAL", message)

    def test_an_ordinary_return_home_does_not_complain(self):
        with HookConf() as c:
            run_hook(c, "@h1")
            self.assertNotIn("fix it now", said(run_hook(c, "@local")))


class TestAMarkerThatCannotBeREAD(unittest.TestCase):
    """The reading side of the same ticket, and a THIRD state.

    A directory in its place used to fall into the one `except` that also covers "no
    marker at all", and answer NOT ARMED — so the ticket could never be spent, the
    reminder never came, the housekeeping never ran, and nothing anywhere pointed at the
    cause. Silence, with the damage landing far from it.

    The fix is a state of its own, and the two things it must NOT be are what these tests
    pin. It is not ABSENT (that is silence, and this is a broken config dir that has to be
    named). And it is not ARMED: calling it armed would make the hook say "first command
    since `@local`" to a session that never switched, and — the expensive half — would buy
    the far side's once-per-switch housekeeping with a ticket nobody has read.
    """

    def test_the_remote_side_complains_instead_of_going_quiet(self):
        with HookConf() as c:
            run_hook(c, "@h1")
            c.put_a_directory_at("switched.s1")
            note = context(run_hook(c, "ls"))
            self.assertIn(c.path("switched.s1"), note)
            self.assertIn("Is a directory", note)

    def test_the_local_side_complains_too(self):
        with HookConf() as c:
            c.put_a_directory_at("switched.s1")
            note = context(run_hook(c, "ls"))
            self.assertIn(c.path("switched.s1"), note)
            self.assertIn("fix it now", note)

    def test_the_command_still_leaves_for_the_host(self):
        """First and loudest: a broken config dir may not cost the rewrite. An unrewritten
        command runs HERE, on the machine the caller believes they left."""
        with HookConf() as c:
            run_hook(c, "@h1")
            c.put_a_directory_at("switched.s1")
            self.assertIn("root@203.0.113.9", said(run_hook(c, "ls")))

    def test_the_housekeeping_does_not_ride_on_a_ticket_that_cannot_be_punched(self):
        with HookConf() as c:
            run_hook(c, "@h1")
            c.put_a_directory_at("switched.s1")
            self.assertNotIn("-delete", said(run_hook(c, "ls")))

    def test_it_claims_no_switch_that_it_did_not_read(self):
        """The line it must NOT say. An unreadable marker is not evidence of a switch —
        this session never made one — and a hook that answers "first command since
        `@local`" here is inventing the very fact it could not read. Unreadable is its own
        answer, never folded into ARMED."""
        with HookConf() as c:
            c.put_a_directory_at("switched.s1")
            note = context(run_hook(c, "ls"))
            self.assertIn("cannot read", note)
            self.assertNotIn("first command since", note)

    def test_it_buys_no_housekeeping_with_a_ticket_nobody_read(self):
        """The expensive half. An unreadable marker that CAN be removed used to come back
        as "armed and spent" — which pays for the far side's once-per-switch housekeeping:
        a `find … -delete` over someone else's disk, bought with a ticket whose contents
        nobody has seen. A failure falls to no-action, never to action."""
        with HookConf() as c:
            run_hook(c, "@h1")
            os.chmod(c.path("switched.s1"), 0o000)  # readable to no one, removable by its owner
            reply = run_hook(c, "ls")
            self.assertNotIn("-delete", said(reply))
            self.assertIn("cannot read", context(reply))

    def test_the_unreadable_marker_is_taken_away_so_it_is_said_once(self):
        """A complaint that cannot be acted on becomes wallpaper. When the file CAN go, it
        goes — and the session is back to ordinary silence."""
        with HookConf() as c:
            run_hook(c, "@h1")
            os.chmod(c.path("switched.s1"), 0o000)
            run_hook(c, "ls")
            self.assertFalse(c.exists("switched.s1"))
            self.assertEqual(context(run_hook(c, "ls")), "")

    def test_an_absent_marker_is_still_silence(self):
        """The other half of the split, and the one that must not be lost: no marker at
        all is the ordinary command, and it says nothing."""
        with HookConf() as c:
            self.assertEqual(run_hook(c, "ls"), {})


class TestTheActiveHostSidecar(unittest.TestCase):
    """`active-host.<sid>` is written for the OUTSIDE — nothing in the hook reads it — and
    that is precisely why a failure to remove it has to be said: whoever does read it (a
    status line, a person in the config dir) would be told this session is on @h1 while it
    is home, and nothing else in the session would ever contradict them."""

    def test_the_sidecar_is_written_by_a_routed_command(self):
        """The premise the rest of this class rests on, pinned rather than assumed."""
        with HookConf() as c:
            run_hook(c, "@h1")
            run_hook(c, "ls")
            self.assertEqual(c.read("active-host.s1"), "h1")

    def test_the_failure_is_said_with_path_and_reason(self):
        with HookConf() as c:
            run_hook(c, "@h1")
            c.put_a_directory_at("active-host.s1")
            message = said(run_hook(c, "@local"))
            self.assertIn(c.path("active-host.s1"), message)
            self.assertIn("Is a directory", message)

    def test_it_says_what_a_reader_would_be_told(self):
        with HookConf() as c:
            run_hook(c, "@h1")
            c.put_a_directory_at("active-host.s1")
            self.assertIn("@h1", said(run_hook(c, "@local")))

    def test_a_second_attempt_still_names_something(self):
        """The message exists to name a host, so it may never print a bare `@`. By the
        second `@local` the routing is already cleared, so the name cannot come from
        there; when the sidecar itself is the unreadable thing, it cannot come from the
        file either. Then it says so in words rather than pointing at nothing."""
        with HookConf() as c:
            run_hook(c, "@h1")
            c.put_a_directory_at("active-host.s1")
            run_hook(c, "@local")
            message = said(run_hook(c, "@local"))
            self.assertIn("still names", message)
            self.assertNotIn("names @,", message)
            self.assertNotIn("names @ ", message)

    def test_the_session_really_is_local_afterwards(self):
        """The complaint is about a sidecar nobody in here reads — it may not hold the
        session back from going home."""
        with HookConf() as c:
            run_hook(c, "@h1")
            c.put_a_directory_at("active-host.s1")
            self.assertIn("mode: LOCAL", said(run_hook(c, "@local")))
            self.assertFalse(c.exists("target.s1"))

    def test_an_ordinary_return_home_is_quiet_about_it(self):
        with HookConf() as c:
            run_hook(c, "@h1")
            run_hook(c, "ls")
            message = said(run_hook(c, "@local"))
            self.assertIn("mode: LOCAL", message)
            self.assertNotIn("active-host", message)
            self.assertFalse(c.exists("active-host.s1"))

    def test_a_session_that_was_never_remote_is_quiet_too(self):
        """Absent is the ordinary state — the sidecar is only written by a routed command."""
        with HookConf() as c:
            message = said(run_hook(c, "@local"))
            self.assertIn("mode: LOCAL", message)
            self.assertNotIn("active-host", message)


if __name__ == "__main__":
    unittest.main()
