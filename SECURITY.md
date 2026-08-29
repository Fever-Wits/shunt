# Security Policy

`shunt` gives an AI agent a shell on another machine. That is the whole trust boundary, and it is worth stating plainly: **whoever drives the agent can run anything on every host you add**, with the rights of the user in that host's target. Add machines you are willing to hand over that way, and no others.

shunt is a deliberate **remote-execution** tool. "Security" here means *bounding* that power to the machines you trust - not preventing remote execution, which is the feature. It assumes one operator driving their own trusted machines.

---

## What the transport does and does not protect

Every command travels over `ssh`, with `ControlMaster` reusing one multiplexed connection on a **per-session, per-destination** control socket. So the wire is encrypted and authenticated, **no port is opened, no shared secret exists**, and nothing of shunt's is installed on the server - only `python3` is required there, and only for the file helpers.

None of that limits what the agent may do once it is there. `ssh` protects the channel; it does not protect the machine from its legitimate user. The key you register for a host **is** the reach the agent has.

---

## Threat model

### In scope (shunt tries to get these right)

- **No secret of shunt's own.** There is no token, no service account, and nothing of shunt's listening on any port. Keys stay where ssh keeps them; the config file only names their path - and `shunt.toml`, `hosts`, `target.*`, `active-host.*`, `audit.log` are in `.gitignore`, never committed.
- **Auditability.** Every redirected command is appended to `~/.config/shunt/audit.log` with timestamp, session id, and host (`pretool.py: audit`), and so is every CLI subcommand that reaches a host (`sid=cli`, `cli.py: audit_cli`); `shunt log` shows the last N of them. One command is one **record**, one line - a multi-line command is folded on the way in and unfolded on the way out, so a command cannot forge a record of its own. Two honest limits: the log is **trimmed** once it grows past `trim_at_mb` (default 100 MB), dropping the oldest `drop_months` (default 2) of history - what falls out is gone for good; and parallel sessions append to one file, so a trim racing another session's append can lose a line. It is an audit *trail*, not a tamper-proof ledger.
- **Host-key checking.** ssh uses `StrictHostKeyChecking=accept-new` (trust-on-first-use), not blanket disabling.
- **No accidental double-rewrite / cross-host bleed.** The hook guards against re-rewriting (`#shunt-rewritten` marker) and ControlMaster sockets are per-session **and** per-destination - the socket path carries the user, host and port (`%r`, `%h`, `%p`) - so neither another host nor another **account** on the same host can share a master connection with you (`pretool.py`, `cli.py`). The two sockets are kept out of reach of other **local** users by different means, and both are stated here on purpose: the hook's sits in `/tmp` under a name carrying the session id, which nobody outside the session can work out; the CLI's name has to stay predictable to be reused between calls, so it sits in a private per-user directory instead (`$XDG_RUNTIME_DIR/shunt/`, else `~/.cache/shunt/`, created at mode 700).
- **Loud on a bad config - in both halves.** A malformed host entry raises instead of quietly resolving to nothing, and the CLI reports the reason (`config.py: _load_toml`, `cli.py: load_hosts`). In the legacy `hosts` format, a line that does not name the `ssh` transport is not treated as a host at all - rather than being turned into some other destination silently - and the skipped lines are counted out loud (`config.py: _load_legacy`). The **hook** does not answer a state it understands with a traceback - and cannot: a hook that raises exits *non-blocking*, so the harness would run the original line here. It takes the third way instead: when the session is routed to a host it cannot resolve - unreadable config, renamed alias - it **replaces the command with the reason nothing ran**. (For the exceptions nobody foresaw there is a roof over `main()`, which denies bash outright - exit 2 - rather than letting it fall through; see *A crash in the hook* in the README.) It does not fall back to running it here: `@status` still says REMOTE, so a command that stayed home would execute where nobody aimed it.
- **Integrity of remote writes.** `shunt edit` and `shunt commit` take an optimistic SHA-256 lock on the file's prior content, refuse rather than overwrite when it has changed, write atomically (temp file in the same directory -> `fsync` -> `os.replace` -> `fsync` of the directory), and **verify by checksum after writing** - so a partial or silently-failed write is reported instead of corrupting the target. The edit itself is applied to the **raw bytes**: nothing outside the matched region is rewritten, not a byte that is invalid UTF-8 and not a line ending elsewhere in the file. `shunt edit` exits non-zero whenever the file was not changed, so `shunt edit ... && deploy` cannot deploy an unedited file - sent as a line of its own, since a `shunt ...` line carrying `&&` is refused while the session is routed to a host (`pretool.py: _shunt_cli_here_or_refuse`).
- **Checkout stays in its sandbox, and does not destroy what it refreshes.** A remote path containing `..` that would place the local copy outside `~/.config/shunt/checkouts/` is refused (`cli.py: cmd_checkout`). A pull writes beside the target and is moved into place only on success, so a **failed** re-checkout leaves your local edits untouched. A **successful** one no longer overwrites them either: if the local file's SHA-256 no longer matches the one recorded when it was pulled, `checkout` refuses (exit 2) and names the ways on - push them (`shunt commit`), keep them and stop tracking (`--abandon`), or drop them deliberately (`--force`). This closes the path the tool itself used to point at: `commit`'s conflict message advised a re-checkout, which then ate the edits it was telling you to re-apply.

### Out of scope (explicitly **unsupported**)

- **Sandboxing what the agent runs.** shunt faithfully relays commands; it does not filter, confine, or sanitize them. The remote shell runs with your ssh user's full privileges.
- **Fencing the mode boundary.** The mode covers **`bash` only**. File tools (`Read`, `Write`, `Edit`, `MultiEdit`, `NotebookEdit`) and search tools (`Grep`, `Glob`) keep touching the **local** disk while the session feels remote, and a spawned agent **inherits** the routing and runs its bash on the far machine. The hook **warns** about both instead of failing silently - but a warning is not a fence, and it never blocks. See "Where the mode stops" in the README.
- **The hook edits one tool input besides `Bash`.** When an `Agent` is spawned while the session is routed, shunt hands the call back with a short note appended to that agent's prompt: which host its bash goes to, that its file tools do not follow, and that `@local` is one setting for the whole session rather than a private choice. Nothing is removed and no permission decision is made - the rest of the call, `subagent_type` included, travels untouched. It is stated here because it is the one place shunt changes what another tool was asked to do, and because the text lands in a context you did not write.
- **Protecting the command text once it is in the agent transcript.** The rewritten command - the full `ssh` invocation and the command itself - is recorded in the agent transcript and in the audit log. Do not type secrets into a redirected command. (There is no token in either; the transport has none.)
- **Your ssh setup.** shunt uses the keys and accounts you give it; it does not manage them, and it does not harden the server's `sshd`.
- **Multi-tenant isolation.** shunt assumes one operator and machines that operator trusts.

---

## Trust & data flow

1. The **PreToolUse hook** (`pretool.py`) runs locally with your privileges. It reads routing from `~/.config/shunt` (the `shunt.toml` config - or the legacy `hosts` file - and the per-session `target.<sid>`) and rewrites the agent's bash command.
2. The **rewritten command runs in the agent's sandbox** - which has network access but **no access to `~/.config/shunt`** (documented in `pretool.py`). That constraint is why the client is the `ssh` binary already present in the sandbox, with the whole invocation baked into the rewritten string.
3. `ssh` with `ControlMaster` carries the command to the remote host over a multiplexed, encrypted, key-authenticated channel; cwd is preserved per session via a remote state file. No open port, no shared token.
4. The remote host executes and streams stdout/stderr back. An **interrupted** foreground command is not killed on the far side - no pty is allocated, so nothing sends it SIGHUP, and it runs to its own end.

---

## Hardening

- **Use a dedicated key and a dedicated account.** `root@` everywhere is the simplest setup and the most expensive mistake. A key that is used only by shunt can be revoked from the server's `authorized_keys` on any suspicion of exposure, without taking anything else down with it - the same discipline a rotated credential buys, at the place where the credential now lives. (`shunt bg` is the one feature that wants system-level rights on the remote host; if you do not use it, do not grant them.)
- **Read the audit log.** `shunt log` is the cheapest way to see what actually left this machine, and for which host.
- **Keep the transcript as sensitive as the commands in it.** See the out-of-scope note above.

---

## Reporting a vulnerability

Please report security issues **privately** via GitHub Security Advisories:

> **https://github.com/Fever-Wits/shunt** -> **Security** tab -> **Report a vulnerability**

Do not open a public issue for a vulnerability. Include: affected version, reproduction steps, and impact. We will acknowledge, investigate, and coordinate a fix and disclosure.
