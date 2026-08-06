# AGENTS.md — shunt

Transparent remote hands for an AI coding agent: a Claude Code PreToolUse hook that
redirects bash to a chosen remote host. Pure stdlib, Python >=3.11.

## Build / test / lint
- Test:  `python -m unittest discover -s tests` (no tests committed yet — add under `tests/`)
- Lint:  `ruff check src/`
- Format: `ruff format src/`
- Install (editable): `pip install -e .`

## Hard invariant — stdlib only
`dependencies = []` in `pyproject.toml`. NEVER add a third-party runtime dependency — ever.
The hook runs inside a restricted sandbox (root, no `~/.config/shunt`, network OK) with only the
system `python3` and `ssh`, and the helpers are deployed inline to servers that may have nothing
but `python3`; an import of anything non-stdlib breaks them.
Keep it stdlib. (`ruff`/`hatchling` are dev/build tools, not runtime deps.)

## Critical safety gotcha
shunt executes **arbitrary remote commands** with the full rights of the ssh user in the host's
target — it does not filter or confine them. The transport is ssh and only ssh: no listening port,
no shared secret, nothing of shunt's installed on the server. Do not weaken that — a transport that
opens a port on the server is a shell for whoever reaches it.

## @host switching convention (per-session)
The hook rewrites bash based on a per-session target file. The agent toggles routing by issuing a
bare command:
- `@<alias>` — route subsequent bash to that host (from `~/.config/shunt/hosts`)
- `@local`  — stop redirecting; run locally
- `@status` — report current routing
Commands starting with `shunt ` always run locally (they do their own transport).

## Entry points
- `src/shunt/cli.py` — the `shunt` CLI (`hosts|read|edit|cp|bg|get|log|install`); `main()` is the
  console-script entry. These are the operations the hook does NOT cover (file read/edit, rsync,
  background jobs, install).
- `src/shunt/pretool.py` — the PreToolUse hook (matcher: Bash); does the transparent `@host`
  redirection via `updatedInput`.
- `src/shunt/edit_helper.py` — content-based remote edit helper, shipped inline over ssh.
