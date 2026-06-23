# Security Policy

**Intended use / scope:** shunt is for an operator driving their **own trusted machines**. The nonsecure daemon (`--mode nonsecure`, TCP + token) **MUST NOT be exposed to an untrusted network** — it grants a shell to anyone who can reach the port and present the token.

shunt is a deliberate **remote-execution** tool: it rewrites an AI coding agent's `bash` commands and runs them on a host you choose. That is its job. "Security" here means *bounding* that power to the machines and networks you trust — not preventing remote execution, which is the feature.

---

## Two transport modes (pick the right one)

shunt supports two transports, configured per-host in `~/.config/shunt/hosts`:

| | `ssh` (secure — **default**) | `daemon` (nonsecure) |
|---|---|---|
| Open port on the remote | none | TCP `:8766` (configurable) |
| Authentication | your ssh key | shared/per-alias token |
| Encryption | yes (ssh) | **none** (plaintext TCP) |
| Token in agent transcript | no | **yes** |
| Safe over the internet | yes (it's ssh) | **no — trusted LAN only** |

`shunt install user@host` defaults to `--mode secure` (ssh). You only get the daemon if you explicitly pass `--mode nonsecure`, and the installer prints a threat-model warning when you do (`cli.py: _install_nonsecure`).

**Recommendation: use ssh mode.** It has no open port, no shared secret, and is encrypted end-to-end. Use the daemon only on a LAN you fully control, and prefer to tunnel even then (see Hardening below).

---

## Threat model

### In scope (we try to get these right)

- **Token confidentiality at rest.** The daemon token is generated with `secrets.token_hex(16)` and written to `~/.config/shunt/token` and `/etc/shunt/daemon.env` with `chmod 600` / `umask 177` (`cli.py: _install_nonsecure`). `token`, `token.*`, `hosts`, `*.env` are all in `.gitignore` — never committed.
- **Token not leaked to other local users via `ps`.** In daemon mode the token is passed to the inline client through the **environment** (`SHUNT_TOK=...`), not as a command-line argument, so it does not appear in another user's `ps`/`/proc/*/cmdline` (`pretool.py: daemon_command`, `INLINE_DAEMON`).
- **Constant-time token check.** The daemon compares tokens with `hmac.compare_digest` (`daemon.py: handle`), avoiding timing oracles.
- **Refuse to run unconfigured.** The daemon exits with code 2 if `SHUNT_TOKEN` is unset (`daemon.py: __main__`).
- **Least privilege by intent.** The daemon warns when run as root, and the shipped systemd unit documents a non-root `User=`/`Group=` to uncomment (`daemon.py: __main__`, `systemd/shunt-daemon.service`).
- **Default to loopback.** The daemon binds `127.0.0.1` unless `SHUNT_HOST` is explicitly set. (Note: `shunt install --mode nonsecure` deliberately sets `SHUNT_HOST=0.0.0.0` so the LAN can reach it — that is the exposing step; restrict it with a firewall, see below.)
- **Auditability.** Every redirected command is appended to `~/.config/shunt/audit.log` with timestamp, session id, and host (`pretool.py: main`); `shunt log` tails it.
- **Host-key checking.** ssh transports use `StrictHostKeyChecking=accept-new` (trust-on-first-use), not blanket disabling.
- **No accidental double-rewrite / cross-host bleed.** The hook guards against re-rewriting (`#shunt-rewritten` marker) and ControlMaster sockets are per-session **and** per-destination (`%h`/`%p` in the socket path) so one host's commands cannot silently go to another (`pretool.py`, `cli.py`).

### Out of scope (explicitly **unsupported**)

- **Defending the nonsecure daemon on an untrusted network.** There is **no TLS by design** — the daemon speaks plaintext TCP. Anyone who can both reach the port and obtain the token gets a shell. If you put the daemon on a hostile network, you are outside shunt's supported envelope.
- **Protecting the token once it is in the agent transcript.** In daemon mode the rewritten command line (including the `SHUNT_TOK=` env assignment and the command itself) is recorded in the Claude Code transcript. Treat that transcript as secret-bearing. **ssh mode has no token at all, so no token-in-transcript problem** — another reason to prefer it.
- **Sandboxing what the agent runs.** shunt faithfully relays commands; it does not filter, confine, or sanitize them. The remote shell runs with the daemon user's (or your ssh user's) full privileges.
- **Network-level DoS / resource exhaustion** of an exposed daemon.
- **Multi-tenant isolation.** shunt assumes one operator and machines that operator trusts.

---

## Trust & data flow

1. The **PreToolUse hook** (`pretool.py`) runs locally with your privileges. It reads routing/secrets from `~/.config/shunt` (the `hosts` file, the per-session `target.<sid>`, and the token), and rewrites the agent's bash command.
2. The **rewritten command runs in the agent's sandbox** — which has network access **but no access to `~/.config/shunt`** (documented in `pretool.py`). That constraint is why the daemon client is a self-contained inline Python snippet (zero files needed on disk) and why the ssh client uses the `ssh` binary already present in the sandbox.
3. The command then reaches the remote host by one of two paths:
   - **ssh mode:** `ssh` with `ControlMaster` (a multiplexed, encrypted, key-authenticated channel; cwd is preserved per session via a remote state-file). No open port, no shared token.
   - **daemon mode:** the inline TCP client connects to the daemon and sends `{token, cmd, cwd, sid, mark}` in the clear; the daemon authenticates the token and runs the command under `bash -c`.
4. The remote host executes and streams stdout/stderr back.

**Key consequence:** in **daemon** mode the token *and* the command land in the transcript (step 2/3) → **trusted LAN only**. In **ssh** mode no token ever transits the transcript or the wire in cleartext.

---

## Hardening the nonsecure daemon (copy-paste)

If you must run the daemon, lock down who can reach the port. Pick **one** (tunnel/Tailscale is strongest):

### 1. Restrict the listening port to a single CIDR

**ufw:**
```sh
# allow only your LAN subnet to reach the daemon port; deny everyone else
sudo ufw allow from 192.0.2.0/24 to any port 8766 proto tcp
sudo ufw deny 8766/tcp
sudo ufw reload
```

**nftables:**
```sh
sudo nft add table inet shunt
sudo nft add chain inet shunt input '{ type filter hook input priority 0; policy accept; }'
# allow your CIDR, drop the rest, for tcp/8766
sudo nft add rule inet shunt input tcp dport 8766 ip saddr 192.0.2.0/24 accept
sudo nft add rule inet shunt input tcp dport 8766 drop
```

### 2. Better: don't expose the port at all — tunnel over ssh

Bind the daemon to loopback on the server (set `SHUNT_HOST=127.0.0.1` in `/etc/shunt/daemon.env`, then `systemctl restart shunt-daemon`), and forward the port over ssh from your client:

```sh
# client → forward local 8766 to the server's loopback daemon
ssh -N -L 8766:localhost:8766 user@server
```

Then point the host at the local tunnel end — the inline client honors `SHUNT_HOST`:

```sh
export SHUNT_HOST=127.0.0.1
# hosts line: lan daemon 127.0.0.1:8766
```

This gives you the daemon's speed with ssh's encryption and authentication, and **no port is exposed** on the server's network interface.

### 3. Best for roaming / multi-host: Tailscale (or other WireGuard mesh)

Put both machines on a Tailscale tailnet and bind the daemon to the tailnet address (or keep it on loopback and use a Tailscale Funnel/serve only inside the tailnet). The daemon then lives on an authenticated, encrypted overlay rather than the open LAN:

```sh
# in /etc/shunt/daemon.env, set the tailnet IP:
# SHUNT_HOST=100.x.y.z
sudo systemctl restart shunt-daemon
# hosts line uses the tailnet IP: lan daemon 100.x.y.z:8766
```

Combine with the firewall rule from (1) scoped to the Tailscale CIDR (`100.64.0.0/10`) for defense in depth.

### 4. Always: run the daemon as a non-root user

Edit `/etc/systemd/system/shunt-daemon.service`, uncomment and set:

```ini
User=shunt
Group=shunt
```

then `sudo systemctl daemon-reload && sudo systemctl restart shunt-daemon`. A breach then yields only that user's privileges. The daemon also warns if started as root.

### Token rotation

```sh
# 1) new token
NEW=$(python3 -c 'import secrets; print(secrets.token_hex(16))')

# 2) update the SERVER env file (chmod 600) and restart the daemon
ssh user@server "umask 177; \
  sed -i 's/^SHUNT_TOKEN=.*/SHUNT_TOKEN=$NEW/' /etc/shunt/daemon.env; \
  systemctl restart shunt-daemon"

# 3) update the CLIENT token file(s) (chmod 600)
printf '%s\n' "$NEW" > ~/.config/shunt/token
chmod 600 ~/.config/shunt/token
# per-alias token, if you use one:
printf '%s\n' "$NEW" > ~/.config/shunt/token.<alias>
chmod 600 ~/.config/shunt/token.<alias>
```

Rotate the token whenever a transcript containing it may have been shared, after any suspected exposure, and on a regular cadence. The simplest rotation of all is to re-run `shunt install user@host --mode nonsecure`, which mints a fresh token end-to-end — or migrate the host to `--mode secure` and retire the token entirely.

---

## Reporting a vulnerability

Please report security issues **privately** via GitHub Security Advisories:

> **https://github.com/Fever-Wits/shunt** → **Security** tab → **Report a vulnerability**

Do not open a public issue for a vulnerability. Include: affected mode (ssh/daemon) and version, reproduction steps, and impact. We will acknowledge, investigate, and coordinate a fix and disclosure.
