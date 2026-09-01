"""
Tests for pretool.py - the live probe a switch makes before it announces a host.

`@web-01` used to be a note in a local file and nothing more. The hook wrote the alias,
said "mode: REMOTE", and whether anything was listening over there was discovered by the
first command that tried - an ssh error arriving in the middle of other work, minutes
later, read as a failure of that work. The moment the caller is thinking ABOUT the machine
is the moment to tell them it does not answer, so the switch now asks.

Two decisions ride in these tests, both deliberate:

  the switch STANDS whatever the answer - a host may be rebooting, and a session that has
  said where it wants to be is not sent home behind its own back. What changes is that the
  truth is spoken.

  a probe that could not RUN is a third answer, never dressed as one of the other two: the
  hook may not announce a dead host on the strength of a question it never asked.

Coverage:
  - the switch really asks: one ssh, at the switch, and nowhere else
  - it asks over the SAME options the caller's commands will use - including the socket,
    so the ask also warms the tunnel it tested
  - a host that answers -> "connected"; one that does not -> the switch is written, the
    silence is named, and the routing really does stand
  - what ssh said reaches the caller: the LAST line, where the reason lives
  - no ssh binary at all -> neither verdict is claimed
  - the probe cannot hang the hook: a deadline, and - the trap this design is shaped
    around - a ControlPersist master that backgrounds itself holding our descriptors
  - the probe's own output cannot corrupt the hook's, which is a JSON protocol on stdout

⚠ No test here opens a real connection. The address is not ours to knock on, and a suite
that reaches the network is a suite whose result depends on somebody else's router.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

# Make the src tree importable when run without installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shunt import pretool

PRETOOL = pretool.__file__


class HookConf:
    """A temp SHUNT_CONF with one host and a STUB `ssh` the hook finds on PATH.

    The stub records the argv it was called with, which is how "did it ask, and what did
    it ask over" is answered without a machine on the other end.
    """

    HOSTS = '[hosts]\nh1 = "root@203.0.113.9"\n'

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-probe-")
        with open(os.path.join(self.dir, "shunt.toml"), "w") as f:
            f.write(self.HOSTS)
        self.bin = os.path.join(self.dir, "_bin")
        os.makedirs(self.bin)
        self.log = os.path.join(self.dir, "ssh-argv")
        self.stub_ssh()
        return self

    def __exit__(self, *_):
        shutil.rmtree(self.dir, ignore_errors=True)

    def stub_ssh(self, returncode=0, stderr="", stdout="", body=""):
        """Write the `ssh` this test wants: an answer, a refusal, or something stranger."""
        script = ["#!/bin/sh", 'printf "%s\\n" "$*" >> ' + self.log]
        if stdout:
            script.append('printf "%s\\n" ' + repr(stdout).replace("'", '"'))
        if stderr:
            for line in stderr.splitlines():
                script.append('printf "%s\\n" ' + repr(line).replace("'", '"') + " >&2")
        if body:
            script.append(body)
        script.append(f"exit {returncode}")
        path = os.path.join(self.bin, "ssh")
        with open(path, "w") as f:
            f.write("\n".join(script) + "\n")
        os.chmod(path, 0o755)

    def no_ssh_at_all(self):
        """A PATH where no `ssh` exists - the probe cannot be made at all."""
        os.remove(os.path.join(self.bin, "ssh"))
        return self.bin  # used ALONE as PATH, so the real ssh cannot be found either

    def asks(self):
        """Every ssh invocation the hook made, one per line."""
        try:
            with open(self.log) as f:
                return [line.strip() for line in f if line.strip()]
        except FileNotFoundError:
            return []

    def path(self, name):
        return os.path.join(self.dir, name)

    def exists(self, name):
        return os.path.exists(self.path(name))


def run_bash(conf, command, sid="s1", path=None):
    """Run pretool.py on a Bash call, as the harness does. Returns (exit code, stdout)."""
    payload = {"tool_name": "Bash", "session_id": sid, "tool_input": {"command": command}}
    env = dict(os.environ, SHUNT_CONF=conf.dir, PATH=path or (conf.bin + os.pathsep + os.environ["PATH"]))
    # PYTHONPATH is stripped the way the other hook tests strip it: settings.json names
    # pretool.py by absolute path, so the field never has the package on sys.path.
    env.pop("PYTHONPATH", None)
    r = subprocess.run([sys.executable, PRETOOL], input=json.dumps(payload).encode(), capture_output=True, env=env)
    return r.returncode, r.stdout.decode()


def said(stdout):
    """What the hook put in place of the caller's command - "" when it said nothing."""
    if not stdout.strip():
        return ""
    return json.loads(stdout)["hookSpecificOutput"].get("updatedInput", {}).get("command", "")


# -- the switch asks ------------------------------------------------------------


class TestTheSwitchAsksTheMachine(unittest.TestCase):
    def test_it_asks(self):
        with HookConf() as c:
            run_bash(c, "@h1")
            self.assertEqual(len(c.asks()), 1, "the switch did not probe at all")

    def test_it_asks_the_host_it_is_switching_to(self):
        with HookConf() as c:
            run_bash(c, "@h1")
            self.assertIn("root@203.0.113.9", c.asks()[0])

    def test_it_asks_for_the_cheapest_thing_there_is(self):
        """`true` proves a shell was reached and changes nothing on the far side. Anything
        heavier turns a question into an action on somebody else's machine."""
        with HookConf() as c:
            run_bash(c, "@h1")
            self.assertTrue(c.asks()[0].endswith(" true"), c.asks()[0])

    def test_a_host_that_answers_is_reported_connected(self):
        with HookConf() as c:
            _, out = run_bash(c, "@h1")
            self.assertIn("connected", said(out))

    def test_the_mode_line_survives_the_verdict(self):
        """The switch announcement is what every other test in the suite reads."""
        with HookConf() as c:
            _, out = run_bash(c, "@h1")
            self.assertIn("mode: REMOTE -> h1", said(out))


# -- and when nothing answers ---------------------------------------------------


class TestAHostThatDoesNotAnswer(unittest.TestCase):
    """The hard half. The switch is still written - on purpose - so the message has to
    carry the whole truth: where the session now is, and that nothing will come of it
    until the machine is back."""

    def _switch_onto_silence(self, c, stderr=""):
        c.stub_ssh(returncode=255, stderr=stderr)
        _, out = run_bash(c, "@h1")
        return said(out)

    def test_the_silence_is_named(self):
        with HookConf() as c:
            message = self._switch_onto_silence(c)
            self.assertIn("did not answer the check", message)
            self.assertIn("@h1", message)

    def test_it_claims_no_cause_it_did_not_check(self):
        """A failed `ssh ... true` proves that THIS CHECK did not get through - never that
        the machine is down. A refused key, a changed host key and a broken login shell all
        answer from a machine that is wide awake, and a cause guessed in the loudest line
        sends the reader to look in the wrong place."""
        with HookConf() as c:
            message = self._switch_onto_silence(c, stderr="Permission denied (publickey).")
            self.assertNotIn("host or ssh down", message)
            self.assertIn("Permission denied (publickey).", message)

    def test_a_silent_failure_is_named_as_one(self):
        """ssh that fails without a word: a reader given a bare refusal with no reason
        will go looking for the reason, and there is none to find."""
        with HookConf() as c:
            message = self._switch_onto_silence(c)
            self.assertIn("said nothing", message)

    def test_it_does_not_claim_a_connection(self):
        with HookConf() as c:
            self.assertNotIn("connected", self._switch_onto_silence(c))

    def test_it_says_the_switch_was_written_anyway(self):
        """Or the caller reads a failure and retypes it, and nothing tells them their
        session already moved."""
        with HookConf() as c:
            self.assertIn("switch written", self._switch_onto_silence(c))

    def test_the_routing_really_stands(self):
        """A message is worth what the state behind it is: the next command must still
        leave for the host, because a machine that is rebooting comes back."""
        with HookConf() as c:
            self._switch_onto_silence(c)
            with open(c.path("target.s1")) as f:
                self.assertEqual(f.read().strip(), "h1")
            c.stub_ssh()  # the host comes back
            _, out = run_bash(c, "ls")
            self.assertIn("root@203.0.113.9", said(out))

    def test_the_reminder_is_still_owed(self):
        """The ticket is armed by the switch, not by the answer - the first command still
        runs THERE, and still says so."""
        with HookConf() as c:
            self._switch_onto_silence(c)
            self.assertTrue(c.exists("switched.s1"))

    def test_what_ssh_said_reaches_the_caller(self):
        """The message names the two ordinary causes; only ssh can name the one that
        actually happened, and "Permission denied (publickey)" is not a down host."""
        with HookConf() as c:
            message = self._switch_onto_silence(c, stderr="Permission denied (publickey).")
            self.assertIn("Permission denied (publickey).", message)

    def test_the_reason_is_the_last_line_not_the_first(self):
        """ssh opens with pleasantries and ends with the reason."""
        with HookConf() as c:
            message = self._switch_onto_silence(
                c,
                stderr="Warning: Permanently added the host to the list of known hosts.\n"
                "ssh: connect to host 203.0.113.9 port 22: No route to host",
            )
            self.assertIn("No route to host", message)
            self.assertNotIn("Permanently added", message)


# -- whose words are these ------------------------------------------------------


class TestTheReasonIsAttributedExactlyOnce(unittest.TestCase):
    """The detail lands INSIDE our own sentence, so it has to say who is speaking - a
    reader must not take ssh's words for the tool's verdict. But ssh's own transport
    failures already open with that attribution, and adding it blindly stutters:

        ... did not answer the check ... ssh: ssh: connect to host ... Connection timed out

    Cosmetic, which is why it stood for a while: it never lied and never went quiet. The
    fix is a test, not a strip - the other shape of failure has no prefix to keep.
    """

    def _detail(self, stderr):
        with HookConf() as c:
            c.stub_ssh(returncode=255, stderr=stderr)
            _, out = run_bash(c, "@h1")
            return said(out)

    def test_ssh_s_own_prefix_is_not_doubled(self):
        message = self._detail("ssh: connect to host 203.0.113.9 port 22: Connection timed out")
        self.assertNotIn("ssh: ssh:", message)
        self.assertIn("ssh: connect to host", message)

    def test_a_line_without_it_is_still_attributed(self):
        """Auth failures are the other shape - `user@host: Permission denied` names the
        ACCOUNT, not the program - and they still have to say who is speaking."""
        message = self._detail("nosuchuser@203.0.113.9: Permission denied (publickey,password).")
        self.assertIn("ssh: nosuchuser@203.0.113.9: Permission denied", message)


# -- the third answer: the probe that could not be made -------------------------


class TestAProbeThatCouldNotRun(unittest.TestCase):
    """Could-not-ask is not does-not-answer. Announcing a dead host on a question that
    never left this machine is the hook stating what it has not verified - and the caller
    would go looking at a machine that is perfectly fine."""

    def test_no_ssh_binary_claims_neither_verdict(self):
        with HookConf() as c:
            _, out = run_bash(c, "@h1", path=c.no_ssh_at_all())
            message = said(out)
            self.assertNotIn("connected", message)
            self.assertNotIn("does not answer", message)

    def test_it_says_that_it_could_not_check(self):
        with HookConf() as c:
            _, out = run_bash(c, "@h1", path=c.no_ssh_at_all())
            self.assertIn("could not check", said(out))

    def test_the_switch_still_happens(self):
        """The probe is a courtesy on top of the switch, never a gate in front of it."""
        with HookConf() as c:
            _, out = run_bash(c, "@h1", path=c.no_ssh_at_all())
            self.assertIn("mode: REMOTE -> h1", said(out))
            self.assertTrue(c.exists("target.s1"))


# -- what it costs --------------------------------------------------------------


class TestTheProbeIsPaidForOnceperSwitch(unittest.TestCase):
    """A round trip on every command would be a tax on the whole session for a fact that
    changes rarely - and the commands themselves already report a connection that has
    died. The switch is the one moment that is about the machine."""

    def test_ordinary_commands_ask_nothing(self):
        with HookConf() as c:
            run_bash(c, "@h1")
            run_bash(c, "ls -la")
            run_bash(c, "uptime")
            self.assertEqual(len(c.asks()), 1)

    def test_going_local_asks_nothing(self):
        with HookConf() as c:
            run_bash(c, "@local")
            self.assertEqual(c.asks(), [])

    def test_status_asks_nothing(self):
        """It reports what is written down; it is not a health check."""
        with HookConf() as c:
            run_bash(c, "@h1")
            run_bash(c, "@status")
            self.assertEqual(len(c.asks()), 1)

    def test_an_unknown_alias_asks_nothing(self):
        with HookConf() as c:
            run_bash(c, "@nope")
            self.assertEqual(c.asks(), [])


# -- it must test the path the command will take --------------------------------


class TestTheProbeGoesTheWayTheCommandWill(unittest.TestCase):
    """A probe over different options tests a different journey: a green answer over the
    default identity says nothing about a command that will use a per-host key. So both
    are built from ONE ssh_opts, and this is what keeps them from drifting apart."""

    def _argv_of_the_probe(self, c):
        run_bash(c, "@h1")
        return c.asks()[0]

    def test_every_option_of_the_rewrite_is_in_the_probe(self):
        with HookConf() as c:
            argv = self._argv_of_the_probe(c)
            host = {"alias": "h1", "target": "root@203.0.113.9", "key": None}
            for opt in pretool.ssh_opts(host, "s1"):
                self.assertIn(opt, argv, f"the probe is missing {opt!r}")

    def test_it_names_the_same_socket_the_command_will_use(self):
        """Which is what makes the ask also WARM the tunnel: the master it opens is the
        one the first command wants. Best-effort by nature, and free when it works."""
        with HookConf() as c:
            self.assertIn("ControlPath=/tmp/shunt-cm-s1-", self._argv_of_the_probe(c))

    def test_it_cannot_sit_on_a_password_prompt(self):
        """BatchMode, spelled out: without it a host with keys misconfigured would hang
        the probe until the deadline instead of failing in a millisecond."""
        with HookConf() as c:
            self.assertIn("BatchMode=yes", self._argv_of_the_probe(c))

    def test_it_bounds_the_handshake(self):
        with HookConf() as c:
            self.assertIn(f"ConnectTimeout={pretool.PROBE_TIMEOUT}", self._argv_of_the_probe(c))

    def test_the_hosts_own_key_is_carried(self):
        with HookConf() as c:
            with open(c.path("shunt.toml"), "w") as f:
                f.write('[hosts]\nh1 = { target = "root@203.0.113.9", key = "/keys/own" }\n')
            self.assertIn("/keys/own", self._argv_of_the_probe(c))


# -- the probe may not become the thing that hangs ------------------------------


class TestTheProbeCannotHangTheHook(unittest.TestCase):
    """The hook runs in FRONT of every command. Anything here that can wait forever waits
    in front of the caller's work - so the probe is asked in-process, where the deadline
    can be made short enough to test."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-probe-hang-")
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        self._path, self._deadline = os.environ["PATH"], pretool.PROBE_DEADLINE
        os.environ["PATH"] = self.bin + os.pathsep + self._path
        pretool.PROBE_DEADLINE = 2

    def tearDown(self):
        os.environ["PATH"] = self._path
        pretool.PROBE_DEADLINE = self._deadline
        shutil.rmtree(self.dir, ignore_errors=True)

    def _ssh(self, script):
        path = os.path.join(self.bin, "ssh")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + script + "\n")
        os.chmod(path, 0o755)

    def _probe(self):
        host = {"alias": "h1", "target": "root@203.0.113.9", "key": None}
        start = time.monotonic()
        verdict, detail = pretool.probe_host(host, "s1")
        return verdict, detail, time.monotonic() - start

    def test_an_ssh_that_never_returns_is_given_up_on(self):
        self._ssh("sleep 30")
        verdict, detail, spent = self._probe()
        self.assertIs(verdict, False)
        self.assertIn("no answer", detail)
        self.assertLess(spent, pretool.PROBE_DEADLINE + 2)

    def test_a_master_that_backgrounds_itself_does_not_hold_us(self):
        """THE trap this design is shaped around. With ControlPersist, ssh leaves a master
        running in the background - and it inherits whatever descriptors it was given. Had
        the probe read stdout/stderr through a PIPE, it would wait for that master to exit
        (five minutes) and the deadline would fire on the healthiest case there is: a host
        that answered on the very first connection.

        ⚠ The background job must NOT redirect its own streams. A stub written as
        `sleep 30 >/dev/null 2>&1 &` inherits nothing and the test passes on a pipe-based
        probe as readily as on this one - measured: 0.00s either way. Bare `&` is what
        makes it hold our descriptors, and then a pipe-based probe times out (measured:
        3.00s against a 3s deadline) while this one returns at once.
        """
        self._ssh("sleep 30 &\nexit 0")
        verdict, _, spent = self._probe()
        self.assertIs(verdict, True, "a backgrounded master swallowed the probe")
        self.assertLess(spent, pretool.PROBE_DEADLINE, "the probe waited for the master")

    def test_a_host_that_answers_at_once_costs_nothing(self):
        """The other direction, so the two above cannot pass on a probe that gave up
        immediately every time."""
        self._ssh("exit 0")
        verdict, _, spent = self._probe()
        self.assertIs(verdict, True)
        self.assertLess(spent, 1)


# -- the probe's output may not touch the hook's --------------------------------


class TestTheProbeCannotCorruptTheProtocol(unittest.TestCase):
    """The hook's stdout IS the protocol - one JSON object, read by the harness. A child
    process that inherits it and prints one line makes that object unparseable, and an
    unparseable hook reply means the ORIGINAL command runs: `@h1` goes to bash as a
    command while the caller reads nothing at all."""

    def test_a_talkative_ssh_leaves_the_reply_readable(self):
        with HookConf() as c:
            c.stub_ssh(stdout="chatter from ssh")
            _, out = run_bash(c, "@h1")
            parsed = json.loads(out)  # fails loudly if the stub's line got in
            self.assertIn("mode: REMOTE", parsed["hookSpecificOutput"]["updatedInput"]["command"])

    def test_a_complaining_ssh_leaves_the_reply_readable(self):
        with HookConf() as c:
            c.stub_ssh(returncode=255, stderr="ssh: something went wrong")
            _, out = run_bash(c, "@h1")
            json.loads(out)


if __name__ == "__main__":
    unittest.main()
