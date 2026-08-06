# Security Policy

**Intended use / scope:** shunt is for an operator driving their **own trusted machines**. Adding a host means handing the agent a shell on it.

shunt is a deliberate **remote-execution** tool: it rewrites an AI coding agent's `bash` commands and runs them on a host you choose. That is its job. "Security" here means *bounding* that power to the machines you trust — not preventing remote execution, which is the feature.

---

## What the transport does and does not protect

Everything travels over `ssh` — with `ControlMaster` reusing one multiplexed connection on a per-session, per-destination control socket. So the wire is encrypted and authenticated, **no port is opened, no shared secret exists**, and nothing of shunt's is installed on the server; only `python3` is required there, and only for the file helpers.

None of that limits what the agent may do once it is there. `ssh` protects the channel; it does not protect the machine from its legitimate user. The key you register for a host **is** the reach the agent has.

---

## Threat model

### In scope (we try to get these right)

- **No secret of shunt's own.** There is no token, no service account, and nothing of shunt's listening on any port. Keys stay where ssh keeps them; the config file only names their path — and `shunt.toml`, `hosts`, `target.*`, `active-host.*`, `audit.log` are in `.gitignore`, never committed.
- **Auditability.** Every redirected command is appended to `~/.config/shunt/audit.log` with timestamp, session id, and host (`pretool.py: main`); `shunt log` tails it.
- **Host-key checking.** ssh uses `StrictHostKeyChecking=accept-new` (trust-on-first-use), not blanket disabling.
- **No accidental double-rewrite / cross-host bleed.** The hook guards against re-rewriting (`#shunt-rewritten` marker) and ControlMaster sockets are per-session **and** per-destination (`%h`/`%p` in the socket path) so one host's commands cannot silently go to another (`pretool.py`, `cli.py`).
- **Loud on a bad config.** A malformed host entry raises instead of quietly resolving to nothing, and the CLI reports the reason (`config.py: _load_toml`, `cli.py: load_hosts`). In the legacy `hosts` format, a line that does not name the `ssh` transport is not treated as a host at all — rather than being turned into some other destination silently — and the skipped lines are counted out loud (`config.py: _load_legacy`).

### Out of scope (explicitly **unsupported**)

- **Sandboxing what the agent runs.** shunt faithfully relays commands; it does not filter, confine, or sanitize them. The remote shell runs with your ssh user's full privileges.
- **Protecting the command text once it is in the agent transcript.** The rewritten command — the full `ssh` invocation and the command itself — is recorded in the Claude Code transcript and in the audit log. Do not type secrets into a redirected command. (There is no token in either, since the transport has none.)
- **Your ssh setup.** shunt uses the keys and accounts you give it; it does not manage them, and it does not harden the server's `sshd`.
- **Multi-tenant isolation.** shunt assumes one operator and machines that operator trusts.

---

## Trust & data flow

1. The **PreToolUse hook** (`pretool.py`) runs locally with your privileges. It reads routing from `~/.config/shunt` (the `shunt.toml` config — or the legacy `hosts` file — and the per-session `target.<sid>`) and rewrites the agent's bash command.
2. The **rewritten command runs in the agent's sandbox** — which has network access **but no access to `~/.config/shunt`** (documented in `pretool.py`). That constraint is why the client is the `ssh` binary already present in the sandbox.
3. `ssh` with `ControlMaster` carries the command to the remote host over a multiplexed, encrypted, key-authenticated channel; cwd is preserved per session via a remote state-file. No open port, no shared token.
4. The remote host executes and streams stdout/stderr back.

---

## Hardening

- **Use a dedicated key and a dedicated account.** `root@` everywhere is the simplest setup and the most expensive mistake. A key that is used only by shunt can be revoked from the server's `authorized_keys` on any suspicion of exposure, without taking anything else down with it — the same discipline a rotated credential buys, at the place where the credential now lives.
- **Read the audit log.** `shunt log` is the cheapest way to see what actually left this machine, and for which host.
- **Keep the transcript as sensitive as the commands in it.** See the out-of-scope note above.

---

## Reporting a vulnerability

Please report security issues **privately** via GitHub Security Advisories:

> **https://github.com/Fever-Wits/shunt** → **Security** tab → **Report a vulnerability**

Do not open a public issue for a vulnerability. Include: affected version, reproduction steps, and impact. We will acknowledge, investigate, and coordinate a fix and disclosure.
