"""
shunt — CLI for operations on remote machines that the hook does not cover.

pretool.py (hook) = transparent EXECUTION of bare bash commands (@host mode).
shunt CLI (this file) = the special operations:

  shunt hosts                              the configured hosts
  shunt run   @host <cmd>                  one command remotely — for scripts, cron and
                                           agents, which have no session and thus no mode
  shunt read  @host <file> [start:end]     content with line numbers (for orientation)
  shunt edit  @host <file> OLD NEW         edit by CONTENT (edit_helper on the other side)
              [--expected N] [--dry-run]
  shunt edit  @host <file> --stdin         JSON {old,new,expected,base_sha} from stdin (multi-line)
              [--dry-run]
  shunt cp    <src> <dst>                  rsync (one side @host:/path)
  shunt bg    @host <cmd> [--name LABEL]   long task (systemd-run); prints JOB=<unit>
  shunt bg    @host --list|--status JOB|--stop JOB
  shunt get   @host <url> [dest]           wget -b (background download on the server itself)
  shunt log   [-n N]                       last N records of ~/.config/shunt/audit.log
                                           (default 50) — redirected bash AND these
                                           subcommands; N counts commands, not lines
  shunt checkout @host <remote_path>       pull remote file locally so agent can Read/Edit/Write it
              [--force]                    …and --force to overwrite local edits (refused without it)
  shunt checkout --list                    show current checkouts (local ↔ remote @host, sha)
  shunt checkout --abandon <local_path>    drop manifest entry (leave local file in place)
  shunt commit  [<local_path>]             push edited local file(s) back to remote (conflict-safe)
  shunt commit  --abandon <local_path>     drop manifest entry without pushing

⚠ Every subcommand starts in the ssh LOGIN directory (usually $HOME). The per-session
`cwd` the hook remembers for @host mode lives in a remote state file that only the hook
reads and writes — the CLI does not share it. So give `run`/`read`/`edit` absolute paths,
and read `get`'s default destination `.` as that login directory, not as wherever the
session last `cd`-ed.

hosts (~/.config/shunt/shunt.toml): see config.py — the legacy `hosts` file is still
read when no shunt.toml exists. Everything goes through ssh, the only transport.
"""

import base64
import hashlib
import json
import os
import shlex
import subprocess
import sys

from shunt import (
    config,
    pretool,  # audit() — see audit_cli(); the log format — see cmd_log()
)

CONF = os.environ.get("SHUNT_CONF", os.path.expanduser("~/.config/shunt"))
SELF_DIR = os.path.dirname(os.path.realpath(__file__))
HELPER = os.path.join(SELF_DIR, "edit_helper.py")
WRITE_HELPER = os.path.join(SELF_DIR, "write_helper.py")
SOCK_NAME = "shunt-cm-cli-%r@%h:%p.sock"  # PER-DESTINATION (%r/%h/%p filled in by ssh) —
# otherwise a shared socket → ControlMaster sends one host's commands to another (silent,
# dangerous bug). Where it LIVES is the other half of the same question — see control_path().
MANIFEST = os.path.join(CONF, "checkouts", "manifest.json")


def die(msg, code=2):
    sys.stderr.write("shunt: " + msg + "\n")
    sys.exit(code)


def load_hosts():
    """All configured hosts; a broken config dies HERE, with the reason."""
    try:
        return config.load_hosts(CONF)
    except Exception as e:
        die(f"cannot read the host config: {e}")


def resolve_host(alias):
    """Alias → {'alias', 'target', 'key'}; dies when the alias is not configured."""
    alias = alias.lstrip("@")
    host = load_hosts().get(alias)
    if not host:
        die(f"unknown host: {alias}")
    return host


def ssh_argv(host):
    """The ssh call every subcommand shares.

    Deliberately NO -tt: `edit` and `commit` hand the helper its source over ssh stdin,
    and through a pty that source never reaches EOF — python drops into its REPL and the
    command hangs. Measured and rejected in ARCHITECTURE.md, together with the rest of
    the cost (pagers, colours, merged stderr).
    """
    return ["ssh"] + ssh_opts(host) + [host["target"]]


def control_path():
    """The CLI's ControlMaster socket — and the private directory it needs, made on the way.

    It used to be `/tmp/shunt-cm-cli-%r@%h:%p.sock`. The NAME has to stay predictable: the
    whole point of a muxed socket is that the NEXT `shunt` call finds the master this one left
    behind, and a random name would fix the exposure by destroying the feature. The hook's twin
    can afford /tmp because its name carries a session id nobody can guess; this one carries
    none — `%r` is the REMOTE account, and the local uid appears nowhere. So on a shared machine
    two different LOCAL users reaching the same target compute the same path in a world-writable
    directory. The PLACE moves instead — the same fix the far side's cwd state got when it left
    /tmp for ~/.cache/shunt.

    XDG_RUNTIME_DIR first: per-user, 0700 by its own spec, on tmpfs, taken away at logout —
    exactly the lifetime a control socket wants. It is absent in cron, in a container, under
    `ssh host shunt …`; ~/.cache is the fallback, which is where shunt already keeps state.

    `-m 700` applies at CREATION only, so a directory that is already there keeps the
    permissions its owner gave it — the same choice, for the same reason, as _state_write's
    mkdir on the far side. Neither base is world-writable, so the squat this fix is about
    cannot be set up in either.

    Best-effort, and silent, on purpose: MEASURED — ssh whose ControlPath directory does not
    exist still connects (at -v: "Control socket … does not exist", then a plain connection).
    What a failed mkdir costs is connection REUSE, not the command, and the only moment to
    report it would be before every single CLI call.

    ⚠ Length. ssh expands %r/%h/%p and then REFUSES a path that does not fit a unix socket —
    "ControlPath too long … >= 108 bytes", exit 255, no connection attempted: fatal, not a
    fallback. Measured: 107 bytes is the longest path that binds on Linux, macOS allows 103.
    The move spent ~18 bytes against /tmp, and a destination of ordinary size — a six-letter
    account, a 38-character FQDN, port 22 — lands around 87, so ~16 are left even on macOS
    (test_ssh_opts pins that budget). A destination long enough to spend the rest fails
    LOUDLY, in ssh's own words; a fallback to /tmp for that case would quietly restore the
    very exposure this function ends.
    """
    base = os.environ.get("XDG_RUNTIME_DIR") or ""
    if not (base.startswith("/") and os.path.isdir(base)):
        base = os.path.expanduser("~/.cache")
    d = os.path.join(base, "shunt")
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
    except Exception:
        pass  # see the docstring: no directory costs reuse, not the connection
    return os.path.join(d, SOCK_NAME)


def ssh_opts(host):
    """The ssh options every hand shares — ONE place, so they cannot drift apart.

    Most hands want them as argv (see ssh_argv); `cp` wants them as a string for rsync's
    -e. Both start here, because they were once written twice and the copy fell behind:
    it lacked BatchMode (so `cp` could hang on a password prompt in a script) and
    ControlMaster (so it opened a fresh connection every time, for nothing).
    """
    opts = []
    if host["key"]:
        opts += ["-i", host["key"]]
    opts += [
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPath=" + control_path(),
        "-o",
        "ControlPersist=300",
        "-o",
        "BatchMode=yes",
    ]
    return opts


def audit_cli(subcommand, alias, detail):
    """Record one CLI operation in the same log the hook writes.

    The hook logged every redirected bash command while the CLI logged nothing — and the
    CLI is the path we recommend to agents (`shunt run`), so the recommended path was the
    unaudited one. Read/checkout/hosts/log stay out: they only bring things back.

    Shared with the hook rather than copied, because the log is ONE thing: where it
    lives, when it is trimmed, and that a log line may never break the operation it
    records. A second copy of that would drift the day one side changed. The seam is one
    function call with the config dir passed in — the same split config.py already uses
    (format shared, location with the caller), which is also what keeps a test from
    writing into the real log.

    The line says WHAT it was: `sid=cli` (there is no session here) and the subcommand in
    front of the detail, so a CLI record cannot be read as bash that ran on the host.
    """
    pretool.audit("cli", alias, f"[{subcommand}] {detail}", conf=CONF)


# ── subcommands ──────────────────────────────────────────────────────────────
MAP = """shunt — transparent remote hands. A bare bash command runs on the chosen machine.

  I want to…                      → reach for
  ──────────────────────────────────────────────────────────────────────────
  …work over there                  @<alias>          then just write bash
  …see where I am / come back       @status  ·  @local
  …run ONE command, no session      shunt run   @host CMD
  …look at a file over there        shunt read  @host FILE [start:end]
  …change a file over there         shunt edit  @host FILE OLD NEW
  …edit heavily, with full tools    shunt checkout @host FILE → edit → shunt commit
  …move files between machines      shunt cp    SRC DST        (one side @host:/path)
  …start something long             shunt bg    @host CMD      (survives disconnect)
  …download onto the server         shunt get   @host URL
  …see what was sent where          shunt log   [-n N]
  …add a machine                    shunt install user@host [--alias NAME]
  …list the machines                shunt hosts

  ⚠ The mode covers BASH ONLY.
    Read/Write/Edit and Grep/Glob keep touching the LOCAL disk, and a spawned agent
    INHERITS the mode and runs its bash on the far machine — reading absent local
    files as facts. Remote file → `shunt read/edit`.  Remote search → `shunt run`.
    Agent that must work there → `shunt run`.

  Full docs: README.md (usage) · ARCHITECTURE.md (why it is built this way).
"""


def cmd_help(argv=None):
    """Print the map. `shunt` with no arguments lands here — asking is not an error."""
    sys.stdout.write(MAP)
    return 0


def cmd_hosts(argv):
    """The configured hosts, resolved — not the raw file, which may be either format."""
    path = config.config_path(CONF)
    if not path:
        die(f"no host config: {os.path.join(CONF, config.TOML_NAME)}")
    hosts = load_hosts()
    print("# " + path)
    for alias, host in sorted(hosts.items()):
        key = ("  key=" + host["key"]) if host["key"] else ""
        print(f"{alias:<12} {host['target']}{key}")
    if not hosts:
        print("(no hosts configured)")
    return 0


def cmd_run(argv):
    """shunt run @host <cmd> — one command on the host; output and exit code pass through.

    The hook covers INTERACTIVE bash: it needs a session to know where that session is
    routed. A script, a cron job or a spawned agent has no mode of its own, so without
    this they reach for raw ssh and go around the tool entirely.

    It is also the EXPLICIT path for an agent. Until now the only way to make an agent
    work on another machine was to leave the session in remote mode and let it inherit
    that silently — the very trap the hook now warns about. This gives somewhere to
    stand instead of only something to avoid.
    """
    if len(argv) < 2:
        die("usage: shunt run @host <cmd>   (quote the command to keep pipes/redirects)")
    host = resolve_host(argv[0])
    # one argument → passed through verbatim, so `shunt run @h "ls | wc -l"` keeps its pipe
    # several → re-quoted, so `shunt run @h echo "a b"` stays two words, not three
    cmd = argv[1] if len(argv) == 2 else shlex.join(argv[1:])
    audit_cli("run", host["alias"], cmd)
    return subprocess.run(ssh_argv(host) + [cmd]).returncode


def cmd_read(argv):
    if len(argv) < 2:
        die("usage: shunt read @host <file> [start:end]")
    host = resolve_host(argv[0])
    f = argv[1]
    if len(argv) > 2 and ":" in argv[2]:
        a, b = argv[2].split(":", 1)
        remote = f"awk 'NR>={int(a)} && NR<={int(b)}{{printf \"%6d\\t%s\\n\", NR, $0}}' {shlex.quote(f)}"
    else:
        remote = f"cat -n -- {shlex.quote(f)}"
    return subprocess.run(ssh_argv(host) + [remote]).returncode


def cmd_edit(argv):
    if len(argv) < 2:
        die("usage: shunt edit @host <file> OLD NEW [--expected N] [--dry-run] | --stdin")
    host = resolve_host(argv[0])
    f = argv[1]
    rest = argv[2:]
    if "--stdin" in rest:
        payload = json.load(sys.stdin)
        payload["file"] = f
        if "--dry-run" in rest:
            # The flag reaches the payload here too, or it would mean two different
            # things depending on how the edit was handed over: a preview on one path
            # and a WRITE on someone else's file on the other. It may only ADD safety —
            # a payload that already asks for a dry run is never turned into a write.
            payload["dry_run"] = True
    else:
        dry = "--dry-run" in rest
        rest = [x for x in rest if x != "--dry-run"]
        expected = 1
        if "--expected" in rest:
            i = rest.index("--expected")
            expected = int(rest[i + 1])
            rest = rest[:i] + rest[i + 2 :]
        if len(rest) < 2:
            die("usage: shunt edit @host <file> OLD NEW [--expected N] [--dry-run]")
        payload = {"file": f, "old": rest[0], "new": rest[1], "expected": expected, "dry_run": dry}
    audit_cli("edit", host["alias"], f + (" (dry-run)" if payload.get("dry_run") else ""))
    b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    with open(HELPER, "rb") as _hf:
        helper_src = _hf.read()
    # inline deployment: helper source via stdin (python3 -), JSON as base64 argv
    r = subprocess.run(ssh_argv(host) + ["python3", "-", b64], input=helper_src, capture_output=True)
    raw_out = (r.stdout or b"").decode("utf-8", "replace")
    sys.stdout.write(raw_out)  # the helper's JSON is the answer — verbatim
    sys.stderr.write((r.stderr or b"").decode("utf-8", "replace"))
    if r.returncode != 0:
        return r.returncode  # the transport itself failed
    # The exit code must mean what a caller reads it to mean. The helper answers in JSON
    # and always exits 0 — `not_found`, `ambiguous`, `conflict` included — so passing ssh's
    # code straight back made `shunt edit … && deploy` deploy an unchanged file. Read the
    # status, the way cmd_commit already does.
    try:
        result = json.loads(raw_out.strip())
    except Exception:
        return 1  # an answer that is not the helper's
    return 0 if result.get("status") == "ok" else 1


def cmd_cp(argv):
    if len(argv) < 2:
        die("usage: shunt cp <src> <dst> (one side @host:/path)")
    host = {"ref": None}

    def conv(p):
        if p.startswith("@") and ":" in p:
            alias, _, path = p[1:].partition(":")
            h = resolve_host(alias)
            host["ref"] = h
            return h["target"] + ":" + path
        return p

    rsrc, rdst = conv(argv[0]), conv(argv[1])
    h = host["ref"]
    if not h:
        die("at least one side must be @host:/path")
    audit_cli("cp", h["alias"], f"{argv[0]} -> {argv[1]}")
    # rsync takes its ssh command as ONE string — same options as every other hand
    e = "ssh " + " ".join(shlex.quote(o) for o in ssh_opts(h))
    return subprocess.run(["rsync", "-az", "--info=progress2", "-e", e, rsrc, rdst]).returncode


def cmd_bg(argv):
    if len(argv) < 2:
        die("usage: shunt bg @host <cmd> [--name LABEL] | --list | --status JOB | --stop JOB")
    host = resolve_host(argv[0])
    sa = ssh_argv(host)
    rest = argv[1:]
    # one line for every shape of bg — starting a job, stopping one, asking about them:
    # what the CLI asked that host to do is exactly what the log is read for.
    # Quoted with shlex.join, the same hand that assembles the command actually sent (below,
    # and in cmd_run): ` `.join wrote `rm -rf /var/lib/My App` for an argument that was ONE
    # path, so the log said two. The audit line is the standing answer to "what did I run on
    # somebody else's machine" — a witness that quotes differently from the executor is worse
    # than no witness. The line still records the invocation AS TYPED (that is what keeps
    # --list/--status/--stop in the log at all), so a `--name` is in it and a single quoted
    # argument comes back quoted; both now read back unambiguously with shlex.split.
    audit_cli("bg", host["alias"], shlex.join(rest))
    if rest[0] == "--list":
        # No `|| true` here. `list-units` already exits 0 when the glob matches nothing,
        # so the guard never bought the empty list anything — it only paid out when the
        # question could NOT be answered (no systemd on the far side, no permission, a
        # bad invocation): systemctl failed, `true` answered 0, and an empty listing at
        # exit 0 reads exactly like "this host has no jobs". The same silence the `--stop`
        # branch below was fixed for — nothing may be stated that has not been verified.
        # No motive for the guard was ever recorded: it arrived with the first commit that
        # tracked this file, nothing anywhere explains it, and later silent-failure passes
        # walked past it. It was measured instead of guessed at, and removed.
        return subprocess.run(sa + ["systemctl list-units 'shunt-*' --type=service --no-legend"]).returncode
    if rest[0] == "--status":
        if len(rest) < 2:
            die("usage: shunt bg @host --status JOB")
        # `systemctl show` INVENTS an answer for a unit it has never heard of: every
        # property comes back at its default — Result=success, SubState=dead,
        # ExecMainStatus=0 — and it exits 0 while doing it. So a mistyped job name read
        # exactly like a job that had finished cleanly, and this is the hand that works
        # with nobody watching the screen: there was no second reader to notice. The twin
        # of the `--stop` fix below (never state what has not been verified) — the word AND
        # the exit code have to come from what systemd said. LoadState is asked as a
        # QUESTION, not merely printed among the properties: a line in a listing is read by
        # a human, an exit code is read by a script, and this hand is used by both. The
        # name goes through a variable so the two questions and the two messages cannot
        # drift apart, the way _remote_script does it.
        remote = "\n".join(
            [
                f"__shunt_job={shlex.quote(rest[1])}",
                'journalctl -u "$__shunt_job" --no-pager -n 60 2>/dev/null',
                "echo '----'",
                'systemctl show "$__shunt_job" -p LoadState -p ExecMainStatus -p ExecMainCode'
                " -p Result -p SubState ||"
                ' { echo "shunt: could not ask systemd about $__shunt_job on this host" >&2; exit 1; }',
                'if systemctl show "$__shunt_job" -p LoadState 2>/dev/null | grep -qx LoadState=not-found; then',
                '  echo "shunt: no such job $__shunt_job on this host — the status above is systemd'
                " answering about NOTHING, not about a job that ran."
                ' \\`shunt bg @<host> --list\\` shows the jobs it knows." >&2',
                "  exit 1",
                "fi",
            ]
        )
        return subprocess.run(sa + [remote]).returncode
    if rest[0] == "--stop":
        if len(rest) < 2:
            die("usage: shunt bg @host --stop JOB")
        job = shlex.quote(rest[1])
        # `&&`, not `;`: the word "stopped" and the exit code are the only things the
        # caller has, and both used to be produced by the `echo` no matter what systemctl
        # did — a mistyped unit answered "stopped", exit 0, while the job kept running.
        # Nothing may be stated that has not been verified. systemctl's own stderr already
        # says why it failed; what was missing was not saying so.
        # `reset-failed` stays fire-and-forget INSIDE the success branch — it is cleanup
        # after a stop that happened, and nothing reads its result.
        remote = f"systemctl stop {job} && {{ systemctl reset-failed {job} 2>/dev/null; echo stopped {job}; }}"
        return subprocess.run(sa + [remote]).returncode
    # start: system-level — survives disconnect, preserves exit code
    # optional --name LABEL for a human-readable unit name
    import re as _re

    label = None
    if "--name" in rest:
        ni = rest.index("--name")
        if ni + 1 >= len(rest):
            # A flag with no value used to be left where it stood and joined the command
            # by ` `.join below — so `shunt bg @h "deploy.sh" --name` shipped
            # `deploy.sh --name` to the far machine. The two hands above (--status,
            # --stop) already refuse a flag without its argument; this one fell to a
            # default instead, and the default was a command nobody typed.
            die("usage: shunt bg @host <cmd> [--name LABEL]   (--name needs a label)")
        label = rest[ni + 1]
        rest = rest[:ni] + rest[ni + 2 :]
    if not rest:
        # `--name` used to be able to eat the whole line: `shunt bg @h --name deploy` left
        # nothing behind, ` `.join made "" out of it, and systemd-run started a unit around
        # an empty command — a job that reports JOB=shunt-deploy and does nothing. The
        # sibling refusals above (a flag without its argument) already answer this shape.
        die("usage: shunt bg @host <cmd> [--name LABEL]   (no command left to run)")
    # Assembled the way cmd_run does it, and for the same reason. ` `.join re-splits every
    # argument that carried a space, so `shunt bg @h rm -rf "/var/lib/My App"` arrived on
    # the far side as `rm -rf /var/lib/My App` — two paths, neither of them the one that
    # was typed, on the hand that runs with nobody watching the screen and does not come
    # back. One argument still passes through verbatim, so a quoted line keeps its pipes
    # and redirects (the same split cmd_run makes, and it must stay the same split).
    cmd = rest[0] if len(rest) == 1 else shlex.join(rest)
    if label:
        label = _re.sub(r"[^a-z0-9-]", "-", label.lower()).strip("-")
    if label:  # empty/all-illegal label → random
        unit = "shunt-" + label
    else:
        unit = "shunt-" + base64.b16encode(os.urandom(4)).decode().lower()
    remote = (
        f"systemd-run --collect --remain-after-exit --unit={unit} bash -lc {shlex.quote(cmd)} "
        f">/dev/null && echo 'JOB={unit}'"
    )
    return subprocess.run(sa + [remote]).returncode


def cmd_log(argv):
    """shunt log [-n N] — the last N RECORDS of ~/.config/shunt/audit.log (default 50).

    Both halves of what left this machine: bash the hook redirected (`sid=<session>`)
    and CLI subcommands that touched a host (`sid=cli`, subcommand in brackets).

    N counts COMMANDS, not lines. A multi-line command is stored folded onto one line and
    printed back the way it was typed; one inherited from a log written before folding
    spans several lines and is shown with the record it belongs to (pretool.log_records —
    the same grouping the trimmer uses, so both halves count the same thing).
    """
    n = 50
    rest = list(argv)
    if "-n" in rest:
        i = rest.index("-n")
        try:
            n = int(rest[i + 1])
        except (IndexError, ValueError):
            # Falling back to 50 answered a question nobody asked: `-n 5OO` (letter O)
            # printed fifty records with no word, and fifty records that LOOK like the
            # whole answer are how someone concludes a command was never run on a server —
            # the reader acts confidently in the wrong direction. The log is the thing
            # people bring hard questions to; it may not quietly narrow them.
            die("usage: shunt log [-n N]   (-n needs a number)")
        rest = rest[:i] + rest[i + 2 :]
    log_path = os.path.join(CONF, "audit.log")
    try:
        with open(log_path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        sys.stderr.write(f"shunt: no audit log yet ({log_path})\n")
        return 0
    except Exception as e:
        sys.stderr.write(f"shunt: cannot read audit log: {e}\n")
        return 1
    records = pretool.log_records(lines)
    n = abs(n)  # negative -n → last |n| (not a bad slice)
    shown = records[-n:] if n else []
    sys.stdout.writelines(pretool.log_text(rec) for rec in shown)
    return 0


def cmd_get(argv):
    if len(argv) < 2:
        die("usage: shunt get @host <url> [dest_dir]")
    host = resolve_host(argv[0])
    url = argv[1]
    dest = argv[2] if len(argv) > 2 else "."
    audit_cli("get", host["alias"], f"{url} -> {dest}")
    log = f"/tmp/shunt-wget-{base64.b16encode(os.urandom(3)).decode().lower()}.log"
    remote = (
        f"cd {shlex.quote(dest)} && wget -b -o {shlex.quote(log)} {shlex.quote(url)} && "
        f"echo 'downloading in background; progress: shunt read @{host['alias']} "
        f"{shlex.quote(log)} or tail -f {shlex.quote(log)}'"
    )
    return subprocess.run(ssh_argv(host) + [remote]).returncode


HOOK_MATCHER = "Bash|Agent|Read|Write|Edit|MultiEdit|NotebookEdit|Grep|Glob"


def _print_hook_hint():
    """hook instruction (we do NOT touch someone else's settings.json automatically).

    The matcher is wider than Bash on purpose. Bash is the tool that is REDIRECTED; the
    rest are matched so the hook can WARN that the mode does not cover them (see
    pretool.py). Print the narrow one and a fresh install silently loses those warnings —
    which is how this was found: one setup only had them because a human had widened it by
    hand.
    ⚠ Not "only Bash is ever rewritten", which this line said until the note into a spawned
    agent's prompt made it false: an Agent call is handed back rewritten too, with the note
    appended to the child's prompt. A comment is read far more often than a manual, so the
    stale one was teaching the wrong thing to everyone who opened the file.
    """
    print("\nTo activate, add to ~/.claude/settings.json → hooks.PreToolUse (if not already there):")
    print(f'  {{ "matcher": "{HOOK_MATCHER}",')
    print('    "hooks": [ { "type": "command",')
    print(f'      "command": "python3 {SELF_DIR}/pretool.py" }} ] }}')
    print("  (requires restarting the Claude Code session)")


def cmd_install(argv):
    """shunt install <user>@<host> [--alias A] [--key PATH]

    ssh + ControlMaster: zero open ports, zero shared token.
    """
    if not argv or "@" not in argv[0]:
        die("usage: shunt install <user>@<host> [--alias A] [--key PATH]")
    dest = argv[0]
    rest = argv[1:]
    alias = rest[rest.index("--alias") + 1] if "--alias" in rest else None
    # written down as given (`~/…` travels between machines), expanded only for ssh here
    key = rest[rest.index("--key") + 1] if "--key" in rest else None
    host_ip = dest.split("@", 1)[1]
    if not alias:
        alias = host_ip.replace(".", "-")
    sb = ["ssh", "-o", "StrictHostKeyChecking=accept-new"]
    if key:
        sb[1:1] = ["-i", os.path.expanduser(key)]
    sb.append(dest)
    # 1) python3 on the server (needed for edit_helper / write_helper)
    r = subprocess.run(sb + ["python3 --version"], capture_output=True)
    if r.returncode != 0:
        die(f"python3 missing on {host_ip}: {(r.stderr or b'').decode()[:200]}")
    print(f"✓ python3 on {host_ip}: {(r.stdout or r.stderr).decode().strip()}")

    # 2) the host entry (idempotent — an entry with the same alias is replaced)
    try:
        line = config.add_host(CONF, alias, dest, key)
    except Exception as e:
        die(f"cannot write the host config: {e}")
    print(f"✓ {os.path.join(CONF, config.TOML_NAME)}: {line}")
    # 3) hook instruction (we do NOT touch someone else's settings.json automatically)
    _print_hook_hint()
    # 4) connection test — the LAST word install says, and the one a reader keeps: a tick
    # and a hostname. Its code used to be thrown away and install returned 0 regardless,
    # so `shunt install … && <next step>` carried on against a machine nothing had
    # reached, and a failing probe printed no tick but left no trace in the exit code
    # either. The code IS the answer here; the tick is only its human half.
    print("\nTest:")
    r = subprocess.run(sb + ["echo '  ✓ connected to' $(hostname)"])
    if r.returncode != 0:
        # ssh's own code travels out (255 = the transport), the way `run`/`checkout`
        # already pass a remote code through instead of flattening it to one number.
        die(
            f"connection test to {host_ip} FAILED (ssh exit {r.returncode}) — the host "
            "entry and the hook line above stand, but nothing was reached. Fix the "
            "connection, then run `shunt install` again; it is idempotent.",
            r.returncode,
        )
    return 0


# ── checkout / commit helpers ─────────────────────────────────────────────────


def _sha256_file(path):
    """sha256 of local file bytes, or None if the file does not exist."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return None


def _manifest_load():
    """The checkouts; {} when there are none — and a REFUSAL when they cannot be read.

    Missing and unreadable used to come back as the same {}, and everything downstream
    takes {} to MEAN "nothing is checked out" and acts on it:
      · `checkout --list` prints "(no checkouts)" — the one answer it has not verified;
      · `commit` prints "no checkouts in manifest" and exits 0, while edited files sit
        unpushed and a script reading that code carries on as if they had landed;
      · the next `checkout` writes the file back with ONE entry, taking every other
        entry's base_sha with it — and base_sha is the whole of what makes a commit
        conflict-safe. The damage is done by the recovery attempt.
    So the failure falls to refusal, never to the default, the way load_hosts() already
    refuses a config it cannot read: a state file we cannot read is not a state we may
    guess at.

    Parsing is not reading, which is how `null` walked past all of that: it parses
    cleanly, lands on None, and None is FALSY — so it reached the very "no checkouts,
    exit 0" answer above without the guard ever being asked. `[]` takes that same path;
    a non-empty list or a string takes the other one and reaches `m.keys()` as a bare
    traceback. Both are one fact — this file is not a mapping of checkouts — so both
    fall to the same refusal rather than to a second, half-shaped one.
    """
    try:
        with open(MANIFEST) as f:
            m = json.load(f)
        if not isinstance(m, dict):
            found = "null" if m is None else type(m).__name__  # the word the FILE uses
            raise ValueError(f"expected an object of checkouts, found {found}")
        return m
    except FileNotFoundError:
        return {}  # nothing checked out yet — the ordinary empty state
    except Exception as e:
        die(
            f"cannot read the checkout manifest {MANIFEST}: {e}\n"
            "  it holds every checkout's base_sha, so nothing is listed, committed or "
            "checked out until it can be read (move it aside to start over — the local "
            "files stay where they are)."
        )


def _manifest_save(m):
    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    tmp = MANIFEST + ".tmp"
    with open(tmp, "w") as f:
        json.dump(m, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, MANIFEST)


def _checkout_local_path(alias, remote_path):
    """Canonical local path for a checked-out remote file."""
    stripped = remote_path.lstrip("/")
    return os.path.join(CONF, "checkouts", alias, stripped)


def cmd_checkout(argv):
    """shunt checkout @host <remote_path> [--force] | --list | --abandon <local_path>"""
    if not argv:
        die("usage: shunt checkout @host <remote_path> [--force] | --list | --abandon <local_path>")

    if argv[0] == "--list":
        m = _manifest_load()
        if not m:
            print("(no checkouts)")
            return 0
        for local, info in sorted(m.items()):
            sha_short = (info.get("base_sha") or "")[:12]
            print(f"{local:<60}  @{info['host']}:{info['remote']}  sha={sha_short}")
        return 0

    if argv[0] == "--abandon":
        if len(argv) < 2:
            die("usage: shunt checkout --abandon <local_path>")
        local = os.path.realpath(argv[1])
        m = _manifest_load()
        if local not in m:
            print(f"shunt: not in manifest: {local}")
            return 1
        del m[local]
        _manifest_save(m)
        print(f"abandoned (manifest entry removed; local file left in place): {local}")
        return 0

    # default: pull remote file
    # `--force` is stripped before the positional arguments are counted, the way cmd_edit
    # strips `--dry-run`, so it may be written anywhere on the line.
    force = "--force" in argv
    argv = [a for a in argv if a != "--force"]
    if len(argv) < 2:
        die("usage: shunt checkout @host <remote_path> [--force]")
    host = resolve_host(argv[0])  # dies if the alias is unknown
    remote_path = argv[1]

    local = os.path.realpath(_checkout_local_path(host["alias"], remote_path))
    # path-traversal guard: a remote_path with '..' must not escape the sandbox;
    # realpath also makes the manifest key match the realpath() used by abandon/commit.
    safe_root = os.path.realpath(os.path.join(CONF, "checkouts"))
    if local != safe_root and not local.startswith(safe_root + os.sep):
        die(f"unsafe remote path (escapes checkout sandbox): {remote_path}")

    # The manifest is read BEFORE anything is touched on disk — it is the last gate that
    # can refuse, and a gate placed after the write is not a gate. Read where it used to
    # be (below, beside the save), a manifest that refuses did so with the pull already
    # moved into place: a RE-checkout over an edited file destroyed exactly the work the
    # refusal then claimed it had left alone. Same lesson as the `.part` file below, one
    # step earlier in the same command — the pull is not the only thing that can be too
    # late to take back.
    m = _manifest_load()

    # …and the second gate, in the same place and for the same reason as the first: the
    # local file may be WORK. A re-checkout is the ordinary way to pick up remote changes,
    # and it silently replaced whatever was sitting here — the tool telling you to do it
    # was `commit`'s own conflict message. Everything else about a checkout is recoverable;
    # this is not, so it is the one place that asks rather than acts (`--force` says drop
    # them, `--abandon` says keep them and stop tracking).
    #
    # Three states, and only the middle one refuses:
    #   the file is GONE          → nothing to lose, pull again (this is how a deleted
    #                               checkout is repaired, and it must stay possible)
    #   it differs from base_sha  → local edits, or a base_sha we cannot vouch for → REFUSE
    #   it matches base_sha       → an untouched copy, refreshing it changes nothing
    entry = m.get(local)
    if entry and not force:
        local_sha = _sha256_file(local)
        base_sha = entry.get("base_sha")
        if local_sha is not None and local_sha != base_sha:
            die(
                f"refusing to overwrite local edits: {local}\n"
                "  this file no longer matches what was checked out, so it holds changes "
                "that exist nowhere else — and a checkout replaces it whole.\n"
                f"    checked-out sha: {base_sha or '(none recorded)'}\n"
                f"    local file sha : {local_sha}\n"
                f"  · keep them and push  → shunt commit {local}\n"
                f"  · keep them, stop tracking → shunt checkout --abandon {local}  (the file stays as it is)\n"
                f"  · DROP them, take the remote copy → shunt checkout {argv[0]} {remote_path} --force"
            )

    os.makedirs(os.path.dirname(local), exist_ok=True)

    # pull via `cat` over ssh — raw-faithful (no scp binary quoting issues).
    # Into a temp file NEXT TO the target, moved into place only on success (same pattern
    # as _manifest_save): opening `local` for writing truncates it before ssh has even
    # started, so a failed RE-checkout used to destroy the local edits it was called to
    # refresh — forty minutes of work for one unreachable host.
    sa = ssh_argv(host)
    part = local + ".part"
    with open(part, "wb") as f:
        r = subprocess.run(sa + ["cat -- " + shlex.quote(remote_path)], stdout=f, stderr=subprocess.PIPE)
    if r.returncode != 0:
        try:
            os.unlink(part)
        except Exception:
            pass
        sys.stderr.write((r.stderr or b"").decode("utf-8", "replace"))
        die(f"checkout failed (exit {r.returncode}) — the local file is untouched", r.returncode)
    os.replace(part, local)

    base_sha = _sha256_file(local)
    m[local] = {"host": host["alias"], "remote": remote_path, "base_sha": base_sha}
    _manifest_save(m)
    print(local)
    return 0


def cmd_commit(argv):
    """shunt commit [<local_path>] | --abandon <local_path>"""
    if argv and argv[0] == "--abandon":
        if len(argv) < 2:
            die("usage: shunt commit --abandon <local_path>")
        local = os.path.realpath(argv[1])
        m = _manifest_load()
        if local not in m:
            print(f"shunt: not in manifest: {local}")
            return 1
        del m[local]
        _manifest_save(m)
        print(f"abandoned (manifest entry removed; local file left in place): {local}")
        return 0

    m = _manifest_load()
    if not m:
        print("shunt: no checkouts in manifest")
        return 0

    # determine targets
    if argv:
        local = os.path.realpath(argv[0])
        if local not in m:
            die(f"not in manifest: {local}")
        targets = [local]
    else:
        targets = sorted(m.keys())

    with open(WRITE_HELPER, "rb") as _wf:
        write_helper_src = _wf.read()
    overall_rc = 0

    for local in targets:
        info = m[local]
        alias = info["host"]
        remote_path = info["remote"]
        manifest_base_sha = info.get("base_sha")

        # a manifest entry may outlive its host — report and keep going, one bad entry
        # must not abandon the files after it
        host = load_hosts().get(alias)
        if not host:
            print(f"SKIP {local} — unknown host '{alias}' (not in the config)")
            overall_rc = 1
            continue
        sa = ssh_argv(host)

        # get remote current sha via sha256sum
        r = subprocess.run(sa + ["sha256sum -- " + shlex.quote(remote_path)], capture_output=True)
        if r.returncode != 0:
            print(f"SKIP {local} — cannot sha256sum remote: {(r.stderr or b'').decode('utf-8', 'replace').strip()}")
            overall_rc = 1
            continue
        remote_sha_line = (r.stdout or b"").decode("utf-8", "replace").strip()
        remote_sha = remote_sha_line.split()[0] if remote_sha_line else None

        if remote_sha != manifest_base_sha:
            print(f"CONFLICT {local} — remote has changed since checkout")
            print(f"  manifest base_sha : {manifest_base_sha or '(none)'}")
            print(f"  remote current_sha: {remote_sha or '(unknown)'}")
            # Named exactly, because a bare "re-checkout" now walks into the guard that
            # protects the very edits this message is about: the local file differs from
            # base_sha (you edited it), so the plain checkout refuses. Save first, then
            # force — in that order, and the order is the whole advice.
            print("  copy your version aside, then:")
            print(f"    shunt checkout @{alias} {remote_path} --force   # takes the remote copy, DROPS this one")
            print("  and re-apply your edits to the fresh file.")
            overall_rc = 1
            continue

        # read local edited bytes
        try:
            with open(local, "rb") as f:
                local_bytes = f.read()
        except Exception as e:
            print(f"SKIP {local} — cannot read local file: {e}")
            overall_rc = 1
            continue

        local_sha = hashlib.sha256(local_bytes).hexdigest()

        # deploy write_helper over ssh (inline, like cmd_edit)
        payload = {
            "file": remote_path,
            "content_b64": base64.b64encode(local_bytes).decode(),
            "base_sha": manifest_base_sha,
        }
        b64 = base64.b64encode(json.dumps(payload).encode()).decode()
        # logged per file, here: everything before this point only READ the remote side
        audit_cli("commit", alias, remote_path)
        r2 = subprocess.run(sa + ["python3", "-", b64], input=write_helper_src, capture_output=True)
        raw_out = (r2.stdout or b"").decode("utf-8", "replace").strip()
        try:
            result = json.loads(raw_out)
        except Exception:
            # The helper answers in JSON and in nothing else, so an unparseable answer means
            # it never got to answer: no python3 over there, a process killed, a permission
            # that stopped it at the first import. All of that arrives as an exit code and a
            # traceback on STDERR — and both were dropped on the floor here, leaving the
            # caller "unexpected response: " with an empty stdout and nowhere to look.
            # Shown only on THIS branch: on a write that worked, the far side's stderr is
            # login banners and warnings, and a commit that succeeded must not read like
            # trouble. The last lines and not the first — a traceback keeps its message at
            # the end, and that message is the diagnosis.
            print(
                f"ERROR {local} — unexpected response (ssh exit {r2.returncode}): "
                f"{raw_out[:200] or '(nothing on stdout)'}"
            )
            said = (r2.stderr or b"").decode("utf-8", "replace").strip()
            for line in said.splitlines()[-20:]:
                print(f"    {line}")
            overall_rc = 1
            continue

        status = result.get("status")
        if status == "ok":
            new_sha = result.get("new_sha", local_sha)
            m[local]["base_sha"] = new_sha
            _manifest_save(m)
            old_short = (manifest_base_sha or "")[:12]
            new_short = new_sha[:12]
            print(f"ok  {local}  ({old_short} → {new_short})")
            # The helper says when the content landed but something around it did not —
            # ownership that could not follow, a rename not yet flushed. `shunt edit` shows
            # them for free (it prints the helper's JSON verbatim); this path parses the
            # JSON and would have dropped them on the floor, which is the same silence one
            # layer up. Not an error: the file IS written, and calling a successful write a
            # failure is the very bug the fsync half of this fixes.
            for w in result.get("warnings") or []:
                print(f"    ⚠ {w}")
        elif status == "conflict":
            print(f"CONFLICT {local} — write_helper detected conflict (remote changed mid-flight)")
            print(f"  current_sha: {result.get('current_sha')}")
            overall_rc = 1
        else:
            print(f"ERROR {local} — {result.get('message', raw_out[:200])}")
            overall_rc = 1

    return overall_rc


def main():
    # Asking (`-h`) and forgetting (no arguments) both deserve the map — but they are
    # not the same event. Asking succeeds; a missing subcommand is an error, or a script
    # that dropped an argument would silently "succeed". Map to stdout either way, so a
    # human reading the terminal sees it; only the exit code tells them apart.
    if sys.argv[1:2] and sys.argv[1] in ("help", "-h", "--help"):
        sys.exit(cmd_help())
    if len(sys.argv) < 2:
        cmd_help()
        sys.exit(2)
    sub, argv = sys.argv[1], sys.argv[2:]
    fns = {
        "hosts": cmd_hosts,
        "run": cmd_run,
        "read": cmd_read,
        "edit": cmd_edit,
        "cp": cmd_cp,
        "bg": cmd_bg,
        "get": cmd_get,
        "log": cmd_log,
        "install": cmd_install,
        "checkout": cmd_checkout,
        "commit": cmd_commit,
        "help": cmd_help,
    }
    fn = fns.get(sub)
    if not fn:
        die(f"unknown subcommand: {sub} (run `shunt` for the map)")
    sys.exit(fn(argv) or 0)


if __name__ == "__main__":
    main()
