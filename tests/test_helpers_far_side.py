"""
Tests for edit_helper.py / write_helper.py - the part that runs on SOMEBODY ELSE'S machine.

Every other test of these two proves their LOGIC, locally, on the interpreter that happens
to be here. What it cannot prove is the ENVIRONMENT they actually meet: they are shipped as
source over ssh stdin and executed by whatever `python3` the far host has. Measured on real
hosts rather than assumed: 3.7 to 3.13 - five minor versions, none of them chosen by the tool.

Three properties follow from that, and they are one subject: what is true when the code runs
where we cannot look.

Coverage:
  - the python floor is DECLARED (MIN_PYTHON), identical in both helpers, and the guard
    text is byte-for-byte the same - they cannot import each other, so a test is what keeps
    them together (the same shape ssh_opts is held in)
  - the helpers stay PARSEABLE at that floor. This is the half a runtime guard cannot do:
    syntax from the future is a SyntaxError at COMPILE time, so an f-string added anywhere
    in these files would silently turn the guard into dead code
  - below the floor the answer is the helpers' own JSON on stdout and a non-zero exit -
    not a traceback, and the target file is not touched
  - the atomic write's temp file is created in the TARGET's directory. `os.replace` is only
    atomic within one filesystem; a temp file in /tmp turns the rename into a copy across
    devices, or an error, on exactly the hosts where /tmp is its own mount
  - a write the far machine refuses (permissions, and the SELinux shape with it) comes back
    NAMED in the JSON rather than as a traceback on stderr

The version guard is provoked the way the other far-side failures already are: a
`sitecustomize` module on PYTHONPATH, which CPython imports at startup. `sys.version_info`
is writable, so the helper meets an interpreter that says it is old while being this one.
"""

import ast
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

# Make the src tree importable when run without installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shunt import edit_helper, write_helper

PKG = os.path.join(os.path.dirname(__file__), "..", "src", "shunt")
EDIT_HELPER = os.path.join(PKG, "edit_helper.py")
WRITE_HELPER = os.path.join(PKG, "write_helper.py")
PYTHON = sys.executable


# -- helpers --------------------------------------------------------------------


def source(path):
    return io.open(path, encoding="utf-8").read()


def guard_text(path):
    """The version-floor block, cut out by its own markers.

    By marker and not by line number: line numbers move with every edit above them, and a
    test that quietly stops looking at the thing it guards is worse than no test.
    """
    src = source(path)
    start = src.index("# -- the version floor")
    end = src.index("sys.exit(1)\n", start) + len("sys.exit(1)\n")
    return src[start:end]


def run_helper(helper, payload, env=None):
    """Drive a helper the way shunt.py does - base64 argv - and return (result, proc)."""
    arg = base64.b64encode(json.dumps(payload).encode()).decode()
    proc = subprocess.run([PYTHON, helper, arg], capture_output=True, text=True, env=env)
    try:
        return json.loads(proc.stdout.strip()), proc
    except ValueError:
        return None, proc


class Sandbox:
    """A temp directory with one file in it - the thing being edited."""

    def __init__(self, body=b"alpha\nbeta\n"):
        self.body = body

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="shunt-test-farside-")
        self.path = os.path.join(self.dir, "target.txt")
        with open(self.path, "wb") as f:
            f.write(self.body)
        return self

    def __exit__(self, *_):
        os.chmod(self.dir, 0o700)  # a test may have taken write away
        shutil.rmtree(self.dir, ignore_errors=True)


PRETEND_OLD = """
import sys
sys.version_info = (3, 2, 0, 'final', 0)
"""


def with_sitecustomize(sabotage):
    """A PYTHONPATH carrying one module CPython imports before anything else."""
    d = tempfile.mkdtemp(prefix="shunt-test-sabotage-")
    with open(os.path.join(d, "sitecustomize.py"), "w") as f:
        f.write(sabotage)
    return d, dict(os.environ, PYTHONPATH=d + os.pathsep + os.environ.get("PYTHONPATH", ""))


# -- one floor, two files that cannot import each other -------------------------


class TestTheFloorIsOneNumber(unittest.TestCase):
    """The helpers are deployed one at a time, alone, as source. Neither can import the
    other and neither can import a shared module - nothing of ours exists over there. So
    the constant is written twice and held together here, exactly as pretool's and the
    CLI's ssh_opts are."""

    def test_both_helpers_declare_the_same_floor(self):
        self.assertEqual(edit_helper.MIN_PYTHON, write_helper.MIN_PYTHON)

    def test_the_guard_is_byte_identical(self):
        """Not just the number: the message, the exit code and the reasoning travel too.
        Two floors that agree while their answers differ is a difference nobody would
        find until a host hit exactly one of them."""
        self.assertEqual(guard_text(EDIT_HELPER), guard_text(WRITE_HELPER))

    def test_the_floor_names_the_api_it_stands_on(self):
        """A bare number ages into folklore. `os.replace` is why 3.3 and not 3.2."""
        self.assertIn("os.replace", guard_text(EDIT_HELPER))

    def test_the_floor_is_below_the_cli_s_own_requirement(self):
        """The CLI needs 3.11 (tomllib). The helpers must NOT inherit that: they run on
        machines the CLI never runs on: measured hosts fall on both sides of 3.11."""
        # 3.11 is written by hand because there is no CLI_MIN constant to read: the CLI's
        # own floor lives in README prose and in `tomllib` being importable, neither of
        # which a test can ask. If one is ever declared, this line should read it instead.
        self.assertLess(edit_helper.MIN_PYTHON, (3, 11))


# -- the half a runtime guard cannot do -----------------------------------------


# Syntax that did not exist at 3.3, by the AST node it produces. Measured, not assumed:
# `ast.parse(feature_version=...)` alone is only HALF a check - it rejects the walrus, `match`,
# `async def`, numeric underscores and positional-only parameters, and, on 3.12+, it accepts
# an f-string at (3, 3) without a word (PEP 701 dropped that gate; older interpreters reject
# it). The f-string is precisely the one a person adds here out of habit, so the node scan
# below carries what the grammar check drops.
NEWER_THAN_3_3 = {
    "JoinedStr": (3, 6),
    "FormattedValue": (3, 6),
    "NamedExpr": (3, 8),
    "Match": (3, 10),
    "AsyncFunctionDef": (3, 5),
    "AsyncFor": (3, 5),
    "AsyncWith": (3, 5),
    "Await": (3, 5),
    "TryStar": (3, 11),
    "TypeAlias": (3, 12),
}


# ...and syntax that is a new SHAPE of an OLD node, which no node-type table can see.
# Each entry is a form the grammar check also accepts at (3, 3) - verified, one by one.
def _decorator_was_legal_before_3_9(dec):
    """Pre-3.9 a decorator had to be `dotted_name ['(' args ')']` - nothing else.

    PEELED, not type-checked at the top: `@a[0].b` and `@a().b` are `Attribute` at their
    root and look legal there, while the subscript and the call sit UNDER it. PEP 614's own
    example, `@buttons[0].clicked`, is exactly that shape. So the optional trailing call
    comes off first, then every dotted step, and what must remain is a bare name.
    """
    if isinstance(dec, ast.Call):
        dec = dec.func
    while isinstance(dec, ast.Attribute):
        dec = dec.value
    return isinstance(dec, ast.Name)


def forms_newer_than_3_3(tree):
    """Every use of a post-3.3 syntactic FORM in this tree, named for a reader."""
    found = []
    for node in ast.walk(tree):
        # `if`, not `elif`: these are INDEPENDENT questions about one node, and a Call can
        # answer two of them. Chained, `f(*a, *b, **c, **d)` reported only the first and the
        # second went unseen - no difference today, and exactly the sort of quiet coupling
        # that stops being harmless the day a third Call check joins them.
        if isinstance(node, ast.Dict) and any(k is None for k in node.keys):
            found.append("PEP 448 dict unpacking `{**a}` (3.5)")
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)) and any(isinstance(e, ast.Starred) for e in node.elts):
            found.append("PEP 448 iterable unpacking `[*a]` (3.5)")
        if isinstance(node, ast.Call) and sum(isinstance(a, ast.Starred) for a in node.args) > 1:
            found.append("PEP 448 multiple `*args` in one call (3.5)")
        if isinstance(node, ast.Call) and sum(k.arg is None for k in node.keywords) > 1:
            # `f(**a, **b)` lives in `keywords`, not in `args` - a hole found by perturbing
            # the scan rather than by reading the PEP.
            found.append("PEP 448 multiple `**kwargs` in one call (3.5)")
        for dec in getattr(node, "decorator_list", None) or []:
            if not _decorator_was_legal_before_3_9(dec):
                found.append("PEP 614 arbitrary decorator expression (3.9)")
    return sorted(set(found))


class TestTheHelpersStayParseableAtTheFloor(unittest.TestCase):
    """Python compiles the WHOLE file before running line one. Syntax from the future is a
    SyntaxError at compile time, and then the guard below it never speaks - the caller gets
    a traceback about a syntax error instead of a sentence about a version. These are the
    only tests that notice, and it takes THREE of them: no one mechanism is whole, and the
    third exists because the first two shared a hole (see below).

    WARNING: And the three TOGETHER still have one, named here rather than left to be discovered:
    parentheses around several context managers - `with (open(a) as x, open(b) as y):` -
    are 3.9, and invisible to all of it. `feature_version` does not gate them at any level,
    the node is an ordinary `With`, and the parentheses do not survive into the AST at all.
    These two files hold four `with open(...)` between them, so merging a pair is exactly
    the kind of tidying someone does without thinking. If `with (` followed by `as` ever
    appears in either helper, that is above the floor and nothing here will say so."""

    def test_the_grammar_accepts_them_at_the_floor(self):
        for path in (EDIT_HELPER, WRITE_HELPER):
            with self.subTest(helper=os.path.basename(path)):
                ast.parse(source(path), feature_version=edit_helper.MIN_PYTHON)

    def test_no_node_newer_than_the_floor_is_used(self):
        for path in (EDIT_HELPER, WRITE_HELPER):
            with self.subTest(helper=os.path.basename(path)):
                used = {type(n).__name__ for n in ast.walk(ast.parse(source(path)))}
                too_new = sorted(
                    "%s (%d.%d)" % (name, ver[0], ver[1])
                    for name, ver in NEWER_THAN_3_3.items()
                    if name in used and ver > edit_helper.MIN_PYTHON
                )
                self.assertEqual(too_new, [], "syntax above the declared floor: %s" % too_new)

    def test_no_form_newer_than_the_floor_is_used(self):
        """The third mechanism, and it exists because the first two BOTH have a hole.

        PEP 448 (`{**a, **b}`, `[*a, *b]`, `f(*a, *b)` - all 3.5) and PEP 614 (a decorator
        that is any expression - 3.9) parse cleanly at feature_version=(3, 3) AND produce
        only nodes that existed in 3.3. They are new SHAPES of old nodes, so nothing that
        asks "which node types" can see them. Measured, not feared: `_ = {**{}, "x": 1}`
        dropped into a helper left all eighteen tests green.
        """
        for path in (EDIT_HELPER, WRITE_HELPER):
            with self.subTest(helper=os.path.basename(path)):
                self.assertEqual(forms_newer_than_3_3(ast.parse(source(path))), [])

    def test_each_mechanism_bites_where_the_others_do_not(self):
        """A check that cannot fail is not a check. One example each, and none of the three
        catches the other two's: the grammar takes the walrus, the node scan takes the
        f-string, and only the form scan takes PEP 448.

        `feature_version` stopped gating f-strings in 3.12: PEP 701 rewrote the parser and
        dropped that gate, so this parse raises SyntaxError on 3.11 and older and succeeds
        on 3.12 and newer. Either outcome serves the leg, because the leg is about the NODE
        scan - and that one bites in both cases."""
        with self.assertRaises(SyntaxError):
            ast.parse("if (n := 1): pass", feature_version=edit_helper.MIN_PYTHON)

        try:
            fstring = ast.parse('x = f"{1}"', feature_version=edit_helper.MIN_PYTHON)
        except SyntaxError:  # <= 3.11 refuses it at the gate; the node scan is the subject here
            fstring = ast.parse('x = f"{1}"')
        self.assertIn("JoinedStr", {type(n).__name__ for n in ast.walk(fstring)})

        unpack = ast.parse('_ = {**{}, "x": 1}', feature_version=edit_helper.MIN_PYTHON)
        self.assertEqual({type(n).__name__ for n in ast.walk(unpack)} & set(NEWER_THAN_3_3), set())
        self.assertNotEqual(forms_newer_than_3_3(unpack), [])


# -- what an old interpreter is told --------------------------------------------


class TestBelowTheFloor(unittest.TestCase):
    def _refusal(self, helper, payload):
        d, env = with_sitecustomize(PRETEND_OLD)
        try:
            return run_helper(helper, payload, env=env)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_edit_answers_in_json_not_a_traceback(self):
        with Sandbox() as s:
            result, proc = self._refusal(EDIT_HELPER, {"file": s.path, "old": "beta", "new": "gamma", "expected": 1})
            self.assertIsNotNone(result, "stdout was not the helper's JSON: %r" % proc.stdout)
            self.assertEqual(result["status"], "error")
            self.assertEqual(proc.stderr.strip(), "", "a traceback reached stderr")

    def test_the_message_names_both_versions(self):
        """What is here and what is needed - a reader must not have to look either up."""
        with Sandbox() as s:
            result, _ = self._refusal(EDIT_HELPER, {"file": s.path, "old": "beta", "new": "gamma", "expected": 1})
            self.assertIn("3.2", result["message"])
            self.assertIn("%d.%d" % edit_helper.MIN_PYTHON, result["message"])

    def test_the_exit_code_is_non_zero(self):
        """`shunt edit` promises non-zero unless the edit was applied. This is the
        strongest possible 'not applied'."""
        with Sandbox() as s:
            _, proc = self._refusal(EDIT_HELPER, {"file": s.path, "old": "beta", "new": "gamma", "expected": 1})
            self.assertNotEqual(proc.returncode, 0)

    def test_the_target_file_is_not_touched(self):
        with Sandbox() as s:
            self._refusal(EDIT_HELPER, {"file": s.path, "old": "beta", "new": "gamma", "expected": 1})
            with open(s.path, "rb") as f:
                self.assertEqual(f.read(), b"alpha\nbeta\n")

    def test_write_refuses_the_same_way(self):
        with Sandbox() as s:
            result, proc = self._refusal(
                WRITE_HELPER,
                {"file": s.path, "content_b64": base64.b64encode(b"new\n").decode(), "base_sha": None},
            )
            self.assertEqual(result["status"], "error")
            self.assertNotEqual(proc.returncode, 0)
            with open(s.path, "rb") as f:
                self.assertEqual(f.read(), b"alpha\nbeta\n")

    def test_a_current_interpreter_is_left_alone(self):
        """The parachute: the guard must be invisible on every host that clears it."""
        with Sandbox() as s:
            result, proc = run_helper(EDIT_HELPER, {"file": s.path, "old": "beta", "new": "gamma", "expected": 1})
            self.assertEqual(result["status"], "ok")
            self.assertEqual(proc.returncode, 0)


# -- the temp file belongs to the target's filesystem ---------------------------

WATCH_MKSTEMP = """
import os, tempfile
_real = tempfile.mkstemp
def _watched(*a, **k):
    with open(os.environ["SHUNT_TEST_MKSTEMP_LOG"], "a") as f:
        f.write("%s\\n" % k.get("dir"))
    return _real(*a, **k)
tempfile.mkstemp = _watched
"""


class TestTheTempFileLandsBesideTheTarget(unittest.TestCase):
    """`os.replace` is atomic only WITHIN a filesystem. A temp file in /tmp - which is its
    own mount on plenty of servers - makes the rename either a cross-device error or a
    copy that is no longer atomic, and the failure appears only on those hosts.

    Both helpers already do this right; nothing pinned it, so a later `mkstemp()` written
    without `dir=` would have looked like a simplification."""

    def _dir_used(self, helper, payload):
        d, env = with_sitecustomize(WATCH_MKSTEMP)
        log = os.path.join(d, "dirs.txt")
        env["SHUNT_TEST_MKSTEMP_LOG"] = log
        try:
            run_helper(helper, payload, env=env)
            with open(log) as f:
                return [line.strip() for line in f if line.strip()]
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_edit_uses_the_targets_directory(self):
        with Sandbox() as s:
            used = self._dir_used(EDIT_HELPER, {"file": s.path, "old": "beta", "new": "gamma", "expected": 1})
            self.assertEqual(used, [os.path.dirname(s.path)])

    def test_write_uses_the_targets_directory(self):
        with Sandbox() as s:
            payload = {
                "file": s.path,
                "content_b64": base64.b64encode(b"new\n").decode(),
                "base_sha": None,
            }
            used = self._dir_used(WRITE_HELPER, payload)
            self.assertEqual(used, [os.path.dirname(s.path)])


# -- a write the far machine refuses --------------------------------------------


class TestAWriteTheMachineRefusesIsNamed(unittest.TestCase):
    """Permissions, and the SELinux denial with them, arrive as an OSError somewhere inside
    the atomic write. The helper's contract is that every outcome is JSON on stdout - so
    this must be a message, never a traceback the caller has to read as 'unexpected
    response'."""

    def _refused_write(self, helper, payload):
        with Sandbox() as s:
            payload = dict(payload, file=s.path)
            os.chmod(s.dir, 0o500)  # readable, not writable: no temp file can be created
            try:
                return run_helper(helper, payload)
            finally:
                os.chmod(s.dir, 0o700)

    def test_edit_says_what_failed(self):
        result, proc = self._refused_write(EDIT_HELPER, {"old": "beta", "new": "gamma", "expected": 1})
        self.assertIsNotNone(result, "stdout was not JSON: %r" % proc.stdout)
        self.assertEqual(result["status"], "error")
        self.assertIn("write failed", result["message"])
        self.assertEqual(proc.stderr.strip(), "", "a traceback reached stderr")

    def test_edit_names_the_reason_not_only_the_step(self):
        """'write failed' alone sends the reader to the wrong place; the errno is the
        difference between a full disk, a read-only mount and a denial."""
        result, _ = self._refused_write(EDIT_HELPER, {"old": "beta", "new": "gamma", "expected": 1})
        self.assertIn("Permission denied", result["message"])

    def test_write_says_what_failed(self):
        payload = {"content_b64": base64.b64encode(b"new\n").decode(), "base_sha": None}
        result, proc = self._refused_write(WRITE_HELPER, payload)
        self.assertIsNotNone(result, "stdout was not JSON: %r" % proc.stdout)
        self.assertEqual(result["status"], "error")
        self.assertEqual(proc.stderr.strip(), "", "a traceback reached stderr")


# -- the far machine has no python3 at all --------------------------------------


class TestAMissingPython3GetsShuntsOwnLine(unittest.TestCase):
    """The one failure the helper cannot report: it was never born. The remote shell says
    `bash: line 1: python3: command not found` and exits 127 - clear, but a stranger's
    voice, and it says nothing about what it costs or where it was already reported. So
    shunt adds the half it owns, the way `_transport_epilogue` does for ssh's 255."""

    def _run(self, returncode, stderr):
        import shunt.cli as shunt_mod

        conf = tempfile.mkdtemp(prefix="shunt-test-nopy-")
        with open(os.path.join(conf, "shunt.toml"), "w") as f:
            f.write('[hosts]\nh1 = "root@203.0.113.1"\n')
        orig, shunt_mod.CONF = shunt_mod.CONF, conf
        out, err = io.StringIO(), io.StringIO()

        def fake_run(argv_, *a, **kw):
            return subprocess.CompletedProcess(argv_, returncode, b"", stderr.encode())

        try:
            with patch.object(shunt_mod.subprocess, "run", fake_run):
                with patch("sys.stdout", out), patch("sys.stderr", err):
                    try:
                        rc = shunt_mod.cmd_edit(["@h1", "/tmp/f", "a", "b"])
                    except SystemExit as e:
                        rc = e.code
        finally:
            shunt_mod.CONF = orig
            shutil.rmtree(conf, ignore_errors=True)
        return rc, err.getvalue()

    NOT_FOUND = "bash: line 1: python3: command not found\n"

    def test_shunt_says_which_host_and_what_is_lost(self):
        _, err = self._run(127, self.NOT_FOUND)
        self.assertIn("[shunt] @h1 has no python3", err)
        self.assertIn("edit/commit need it", err)

    def test_the_shell_s_own_words_are_kept(self):
        """Ours is an ADDITION. The line that names the missing command is the diagnosis."""
        _, err = self._run(127, self.NOT_FOUND)
        self.assertIn("command not found", err)

    def test_the_callers_exit_code_is_untouched(self):
        rc, _ = self._run(127, self.NOT_FOUND)
        self.assertEqual(rc, 127)

    def test_another_127_is_not_claimed_to_be_python(self):
        """127 alone is any missing command. Saying "no python3" about those would be the
        tool stating what it has not verified."""
        _, err = self._run(127, "bash: line 1: mysqldump: command not found\n")
        self.assertNotIn("[shunt]", err)

    def test_another_exit_code_says_nothing(self):
        _, err = self._run(1, "some other failure\n")
        self.assertNotIn("[shunt]", err)

    def test_commit_says_it_on_stdout_where_its_neighbours_are(self):
        """`edit` reports on stderr and `commit` on stdout, so the line has to follow the
        hand that speaks it. Written from inside the helper it went to stderr in both, and
        under `shunt commit > log` the one line explaining the failure detached from the
        failure it explains."""
        import shunt.cli as shunt_mod

        conf = tempfile.mkdtemp(prefix="shunt-test-nopy-commit-")
        with open(os.path.join(conf, "shunt.toml"), "w") as f:
            f.write('[hosts]\nh1 = "root@203.0.113.1"\n')
        local = os.path.join(conf, "edited.txt")
        with open(local, "w") as f:
            f.write("body\n")
        manifest = os.path.join(conf, "manifest.json")
        with open(manifest, "w") as f:
            json.dump({local: {"host": "h1", "remote": "/tmp/f", "base_sha": None}}, f)

        orig_conf, orig_manifest = shunt_mod.CONF, shunt_mod.MANIFEST
        shunt_mod.CONF, shunt_mod.MANIFEST = conf, manifest
        out, err = io.StringIO(), io.StringIO()

        def fake_run(argv_, *a, **kw):
            if "sha256sum" in argv_[-1]:  # the remote sha matches base_sha=None -> no conflict
                return subprocess.CompletedProcess(argv_, 0, b"", b"")
            return subprocess.CompletedProcess(argv_, 127, b"", b"bash: python3: command not found\n")

        try:
            with patch.object(shunt_mod.subprocess, "run", fake_run):
                with patch("sys.stdout", out), patch("sys.stderr", err):
                    shunt_mod.cmd_commit([local])
        finally:
            shunt_mod.CONF, shunt_mod.MANIFEST = orig_conf, orig_manifest
            shutil.rmtree(conf, ignore_errors=True)

        self.assertIn("[shunt] @h1 has no python3", out.getvalue())
        self.assertNotIn("[shunt]", err.getvalue())

    def test_the_knowledge_lives_in_one_place(self):
        """Two callers, two streams, one sentence: the helper RETURNS it, and each hand
        decides how to say it. A second copy of the condition is how they drift."""
        import shunt.cli as shunt_mod

        self.assertIsNone(shunt_mod._no_python3_line("h1", 0, ""))
        self.assertIsNone(shunt_mod._no_python3_line("h1", 127, "mysqldump: not found"))
        self.assertIn("@h1", shunt_mod._no_python3_line("h1", 127, "python3: command not found"))


if __name__ == "__main__":
    unittest.main()
