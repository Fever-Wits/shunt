#!/usr/bin/env python3
"""
shunt — pretool.py · PreToolUse hook (matcher: Bash|Agent|Read|Write|Edit|MultiEdit|NotebookEdit)

Two jobs, one file — because both need the same answer to "where is this session routed?":

  Bash   → REWRITE: the command runs on the chosen remote machine, transparently.
  others → WARN: these tools do NOT follow the mode (the hook rewrites bash only), so
           running them in remote mode is reported instead of silently doing the wrong
           thing. Never blocked — remote-mode-plus-local-file is legitimate as often as
           it is a mistake; only the silence was the defect.

Switching: @<alias> / @local / @status — PER-SESSION (does not clash across parallel sessions).

The transport is ssh + ControlMaster: zero open ports, zero shared token, encrypted.
Proven: cwd state-file per session, live streaming, speed. It is the only one — the hosts
come from ~/.config/shunt/shunt.toml (config.py, shared with the CLI).

Why hook + updatedInput (rather than replacing the shell): official, documented mechanism;
we stay independent of undocumented env settings.

CRITICAL: the rewritten command runs in a strict sandbox (root, no ~/.config/shunt, BUT network
OK). That is why the client is the `ssh` binary, which is available in the sandbox.
"""
import json, sys, os, shlex, time

try:
    from shunt import config
except ImportError:
    # The hook is wired into settings.json by ABSOLUTE PATH (see README), so it may be
    # started as a plain script: `python3 …/src/shunt/pretool.py` puts …/src/shunt on
    # sys.path and never …/src, leaving its own package unimportable. The parent
    # directory is what makes it resolvable; an installed package never gets here.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    from shunt import config

CONF = os.environ.get("SHUNT_CONF", os.path.expanduser("~/.config/shunt"))

REWRITE_MARKER = "#shunt-rewritten\n"


# Tools that do NOT follow @host mode. The hook rewrites Bash and nothing else, so
# these keep reading the LOCAL disk while the session "feels" remote. Both failures are
# silent — hence a warning instead of letting them be discovered from wrong output.
FILE_TOOLS = ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit")


def emit(command):
    """Return a rewritten command to Claude Code and exit."""
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": {"command": command}}}))
    sys.exit(0)


def echo(msg):
    emit("echo " + shlex.quote(msg))


def warn(msg):
    """Say something into the agent's context and allow the call (never block).

    Blocking would be wrong here: working remotely with local file tools is legitimate
    as often as it is a mistake. Only the SILENCE is the defect.
    """
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": msg}}))
    sys.exit(0)


DAYS_PER_MONTH = 30     # `drop_months` is a human unit, not a calendar one — see _trim_audit


def audit(sid, alias, cmd):
    """Record one redirected command; trim only if the log has grown past its ceiling.

    The log is an ARCHIVE, not a window: the question people bring to it is "where did we
    download that from, two months ago", and a short history answers it with silence.
    So size is the trigger and age is only the unit in which room gets freed.

    Trimming lives HERE rather than in someone's habit — a rule that needs remembering is
    a rule that gets forgotten. Cost per command is a single `getsize`; at any ordinary
    rate (~15 KB per six weeks) the ceiling is never reached at all, which is the point:
    a fuse that blows regularly is a policy in disguise.

    Fire-and-forget by design — an audit line must never break the command it records.
    """
    path = os.path.join(CONF, "audit.log")
    try:
        with open(path, "a") as f:
            f.write("%s sid=%s host=%s :: %s\n"
                    % (time.strftime("%Y-%m-%dT%H:%M:%S"), sid, alias, cmd))
        cfg = config.audit_settings(CONF)
        ceiling = int(cfg["trim_at_mb"] * 1_000_000)
        if os.path.getsize(path) > ceiling:
            _trim_audit(path, cfg["drop_months"], ceiling)
    except Exception:
        pass


def _months_after(iso_date, months):
    """ISO date `months` later, counting a month as 30 days.

    Deliberately not calendar months: adding one to the 31st has no honest answer, and an
    audit log does not need one. Thirty days is predictable and explainable, which matters
    more here than being exact.
    """
    t = time.strptime(iso_date, "%Y-%m-%d")
    return time.strftime("%Y-%m-%d",
                         time.localtime(time.mktime(t) + months * DAYS_PER_MONTH * 86400))


def _trim_audit(path, drop_months, ceiling):
    """Free room by dropping the OLDEST `drop_months` of history — not by keeping only
    the newest ones. The distinction is the whole design: a log holding five years loses
    its first two months and keeps the rest.

    If that is not enough — everything in the file is recent, because something wrote a
    month's worth of lines in an hour — the oldest lines go until it fits. Without this
    the fuse fails in exactly the case it exists for.

    Written via a temp file and os.replace, so a crash mid-trim cannot leave half a log.
    ⚠ What falls out is gone for good.
    ⚠ Parallel sessions append to one file. Appends are safe; a trim coinciding with
    another session's append can lose a line or two. Accepted for an audit trail — said
    plainly so it is not read as a guarantee.
    """
    with open(path) as f:
        lines = f.readlines()
    if not lines:
        return

    cutoff = _months_after(lines[0][:10], drop_months)   # oldest line dates the cut
    kept = [line for line in lines if line[:10] >= cutoff]
    if not kept:
        # Every line falls inside the span we meant to drop: the log is not OLD, it is
        # FAST — a month's worth written in an hour. Age can free nothing here, and
        # cutting by it would empty the file, losing the newest lines too. Leave it to
        # size below, which drops from the front and keeps the tail.
        kept = lines

    total = sum(len(line) for line in kept)
    if total > ceiling:                     # last resort: the history itself is the flood
        start, acc = len(kept), 0
        while start > 0 and acc + len(kept[start - 1]) <= ceiling:
            start -= 1
            acc += len(kept[start])
        kept = kept[start:]

    tmp = path + ".trim"
    with open(tmp, "w") as f:
        f.writelines(kept)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_target(sid):
    """Alias of the host this session is routed to, or "" for local."""
    try:
        with open(os.path.join(CONF, "target." + sid)) as f:
            return f.read().strip()
    except Exception:
        return ""


def _warned_before(sid, alias):
    """True if this session was already warned about THIS host.

    Keyed by host, not just session: switching @web-01 → @web-02 warns again, because the
    file tools are pointed somewhere new and the old warning no longer describes it.
    """
    p = os.path.join(CONF, "warned." + sid)
    try:
        with open(p) as f:
            if f.read().strip() == alias:
                return True
    except Exception:
        pass
    try:
        os.makedirs(CONF, exist_ok=True)
        with open(p, "w") as f:
            f.write(alias)
    except Exception:
        pass  # best-effort: a repeated warning beats a crash
    return False


def warn_if_off_mode(tool, sid):
    """Warn when a tool that ignores @host mode runs while the session is remote."""
    alias = read_target(sid)
    if not alias:
        return                                  # local — nothing to warn about
    if tool == "Agent":
        # Every spawn matters: the agent inherits the mode and reads missing local
        # files as facts about the world. Observed once as "the Bash tool briefly
        # lost access to the working directory" — it had not; it was elsewhere.
        warn("⚠ shunt: you are on @%s — a spawned agent INHERITS this and will run "
             "its bash there, reading absent local files as facts. Switch with "
             "`@local` first, or make sure the agent is meant to work on %s."
             % (alias, alias))
    if tool in FILE_TOOLS and not _warned_before(sid, alias):
        warn("⚠ shunt: you are on @%s, but %s reads and writes the LOCAL disk — the "
             "mode covers bash only. For a remote file use `shunt read/edit @%s …`. "
             "(said once per host)" % (alias, tool, alias))


def resolve_host(alias):
    """Alias → {'alias', 'target', 'key'} or None.

    None also covers a broken config: a hook that raises would put a traceback in front
    of every bash command. Falling back to LOCAL is the safe direction; `shunt hosts`
    says what is wrong with the file.
    """
    try:
        return config.resolve(CONF, alias)
    except Exception:
        return None


def ssh_command(host, cmd, sid):
    """ssh + ControlMaster; cwd is kept per session via a remote state-file.

    Deliberately NO -tt, even though it would fix the one thing this transport does not
    do — killing a command on the far side when the local ssh dies. Measured and
    rejected: a pty makes `python3 -` (how `edit`/`commit` deliver their helper) never
    see EOF, so those commands hang; every pager and stdin reader hangs with them; and
    a controlling terminal EXISTS, so any program can open /dev/tty and block. That last
    one is why a list of workarounds cannot close it. An interrupted command keeps
    running there until it ends on its own — use `shunt bg` for long work.
    """
    key = host["key"]
    sock = "/tmp/shunt-cm-%s-%%h-%%p.sock" % sid  # per-session AND PER-DESTINATION (%h/%p from ssh) —
    # otherwise two different hosts in the same session → shared socket → commands go to the wrong host
    state = "/tmp/shunt-cwd-%s" % sid
    # trap EXIT captures the code + updates cwd on EVERY exit (incl. `exit N` in the command)
    trap_action = "rc=$?; pwd > %s 2>/dev/null; exit $rc" % shlex.quote(state)
    remote = (
        'cd "$(cat %s 2>/dev/null || echo "$HOME")" 2>/dev/null || cd ~\n' % shlex.quote(state)
        + "trap " + shlex.quote(trap_action) + " EXIT\n"
        + cmd
    )
    opts = ["-o", "StrictHostKeyChecking=accept-new",
            "-o", "ControlMaster=auto", "-o", "ControlPath=" + sock,
            "-o", "ControlPersist=300", "-o", "BatchMode=yes"]
    if key:
        opts = ["-i", key] + opts
    return (REWRITE_MARKER
            + "ssh " + " ".join(shlex.quote(o) for o in opts)
            + " " + shlex.quote(host["target"])
            + " " + shlex.quote(remote))


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    sid = data.get("session_id") or "default"
    tool = data.get("tool_name") or ""

    # Tools other than Bash are never rewritten — but some of them silently ignore
    # @host mode, so they get a warning instead of nothing.
    if tool != "Bash":
        try:
            warn_if_off_mode(tool, sid)   # exits via warn() when it has something to say
        except Exception:
            pass                          # fail-open: never break someone else's tool
        sys.exit(0)

    target_file = os.path.join(CONF, "target." + sid)
    cmd = (data.get("tool_input") or {}).get("command", "")

    # guard against double rewriting — marker is a bash comment prepended by ssh_command
    if cmd.lstrip().startswith("#shunt-rewritten"):
        sys.exit(0)

    s = cmd.strip()

    # shunt CLI commands run LOCALLY (they do the transport themselves) → do not redirect
    if s == "shunt" or s.startswith("shunt "):
        sys.exit(0)

    # --- switches ---
    if s == "@local":
        try:
            os.remove(target_file)
        except OSError:
            pass
        # best-effort: remove active-host sidecar for this session
        try:
            os.remove(os.path.join(CONF, "active-host." + sid))
        except OSError:
            pass
        # forget the file-tool warning too → entering remote mode again warns again
        try:
            os.remove(os.path.join(CONF, "warned." + sid))
        except OSError:
            pass
        echo("[shunt] mode: LOCAL")
        sys.exit(0)
    elif s == "@status":
        t = read_target(sid)
        echo("[shunt] " + (("REMOTE → " + t) if t else "LOCAL"))
        sys.exit(0)
    elif s.startswith("@") and len(s) > 1 and " " not in s:
        alias = s[1:]
        host = resolve_host(alias)
        if not host:
            echo("[shunt] unknown host: " + alias)
            sys.exit(0)
        os.makedirs(CONF, exist_ok=True)
        try:
            with open(target_file, "w") as f:
                f.write(alias)
        except Exception:
            sys.exit(0)
        echo("[shunt] mode: REMOTE → %s (%s)" % (alias, host["target"]))
        sys.exit(0)

    # --- remote execution ---
    alias = read_target(sid)
    if alias:
        host = resolve_host(alias)
        if not host:               # host disappeared from the config → fall back to local
            sys.exit(0)
        # sidecar: record active routing target + append to audit log (fire-and-forget)
        try:
            with open(os.path.join(CONF, "active-host." + sid), "w") as f:
                f.write(alias)
        except Exception:
            pass
        audit(sid, alias, cmd)
        emit(ssh_command(host, cmd, sid))

    sys.exit(0)


if __name__ == "__main__":
    main()
