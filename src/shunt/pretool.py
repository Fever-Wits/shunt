#!/usr/bin/env python3
"""
shunt — pretool.py · PreToolUse hook (matcher: Bash)

Transparently redirects the agent's bash commands to a chosen remote machine.
Switching: @<alias> / @local / @status — PER-SESSION (does not clash across parallel sessions).

The transport is ssh + ControlMaster (configured per-host in ~/.config/shunt/hosts):
zero open ports, zero shared token, encrypted. Proven: cwd state-file per session,
live streaming, speed.

Why hook + updatedInput (rather than replacing the shell): official, documented mechanism;
we stay independent of undocumented env settings.

CRITICAL: the rewritten command runs in a strict sandbox (root, no ~/.config/shunt, BUT network
OK). That is why the client is the `ssh` binary, which is available in the sandbox.
"""
import json, sys, os, shlex, time

CONF = os.environ.get("SHUNT_CONF", os.path.expanduser("~/.config/shunt"))

REWRITE_MARKER = "#shunt-rewritten\n"


def conf_read(name):
    try:
        with open(os.path.join(CONF, name)) as f:
            return f.read()
    except Exception:
        return ""


def emit(command):
    """Return a rewritten command to Claude Code and exit."""
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": {"command": command}}}))
    sys.exit(0)


def echo(msg):
    emit("echo " + shlex.quote(msg))


def resolve_host(alias):
    """hosts line: `<alias> ssh <target> [key=...]` → dict or None."""
    for line in conf_read("hosts").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = line.split()
        # ssh is the only transport: a line naming anything else holds something that is
        # not an ssh destination, and must not silently become a host
        if len(p) >= 3 and p[0] == alias and p[1] == "ssh":
            return {"alias": p[0], "target": p[2], "opts": p[3:]}
    return None


def ssh_command(host, cmd, sid):
    """ssh + ControlMaster; cwd is kept per session via a remote state-file."""
    key = None
    for o in host["opts"]:
        if o.startswith("key="):
            key = os.path.expanduser(o[4:])
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
    if data.get("tool_name") != "Bash":
        sys.exit(0)

    sid = data.get("session_id") or "default"
    target_file = os.path.join(CONF, "target." + sid)
    cmd = (data.get("tool_input") or {}).get("command", "")

    # guard against double rewriting — marker is a bash comment prepended by ssh_command
    if cmd.lstrip().startswith("#shunt-rewritten"):
        sys.exit(0)

    s = cmd.strip()

    # shunt CLI commands run LOCALLY (they do the transport themselves) → do not redirect
    if s == "shunt" or s.startswith("shunt "):
        sys.exit(0)

    def read_target():
        try:
            with open(target_file) as f:
                return f.read().strip()
        except Exception:
            return ""

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
        echo("[shunt] mode: LOCAL")
        sys.exit(0)
    elif s == "@status":
        t = read_target()
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
    alias = read_target()
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
        try:
            with open(os.path.join(CONF, "audit.log"), "a") as f:
                f.write(time.strftime("%Y-%m-%dT%H:%M:%S")
                        + " sid=" + sid + " host=" + alias + " :: " + cmd + "\n")
        except Exception:
            pass
        emit(ssh_command(host, cmd, sid))

    sys.exit(0)


if __name__ == "__main__":
    main()
