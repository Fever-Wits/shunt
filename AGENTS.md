# AGENTS.md - shunt

Transparent remote hands for an AI coding agent: a Claude Code PreToolUse hook that
redirects bash to a chosen remote host. Pure stdlib, Python >=3.11.

## Build / test / lint
- Test:  `PYTHONPATH=src python -m unittest discover -s tests` (the suite is committed; add to it)
- Lint:  `ruff check src/`
- Format: `ruff format src/`
- Install (editable): `pip install -e .`

## Hard invariant - stdlib only
`dependencies = []` in `pyproject.toml`. NEVER add a third-party runtime dependency - ever.
shunt asks to be wired in as a bare `python3 <path>` - no venv, no activation - so the hook runs on
whatever `python3` is on PATH - not necessarily the environment shunt was installed into, and the helpers are deployed inline
to servers that may have nothing but `python3`; an import of anything non-stdlib breaks them.
Keep it stdlib. (`ruff`/`hatchling` are dev/build tools, not runtime deps.)

## Critical safety gotcha
shunt executes **arbitrary remote commands** with the full rights of the ssh user in the host's
target - it does not filter or confine them. The transport is ssh and only ssh: no listening port,
no shared secret, nothing of shunt's installed on the server. Do not weaken that - a transport that
opens a port on the server is a shell for whoever reaches it.

## @host switching convention (per-session)
The hook rewrites bash based on a per-session target file. The agent toggles routing by issuing a
bare command:
- `@<alias>` - route subsequent bash to that host (from `~/.config/shunt/shunt.toml`)
- `@local`  - stop redirecting; run locally
- `@status` - report current routing
Commands starting with `shunt ` always run locally (they do their own transport).

## Entry points
- `src/shunt/cli.py` - the `shunt` CLI
  (`hosts|run|read|edit|cp|bg|get|log|checkout|commit|install|help`); `main()` is
  the console-script entry. These are the operations the hook does NOT cover (one-shot remote
  command, file read/edit, rsync, background jobs, install). With no arguments it prints its own
  map (and exits 2 - a script that dropped its subcommand must not "succeed").
- `src/shunt/pretool.py` - the PreToolUse hook (matcher:
  `Bash|Agent|Read|Write|Edit|MultiEdit|NotebookEdit|Grep|Glob`); does the transparent `@host`
  redirection via `updatedInput`. Only **Bash** has its command rewritten; an `Agent` call
  spawned while routed gets a note appended to the child prompt (see SECURITY.md). The remaining
  tools are matched so the hook can warn that the mode does not cover them, and it never blocks.
  Keep the matcher printed by `cli.py:HOOK_MATCHER`, the README snippet and
  `pretool.LOCAL_DISK_TOOLS` in step. It is wired into `settings.json` by absolute path, so it
  may be launched as a plain script - hence the sys.path fallback guarding its `shunt.config`
  import.
- `src/shunt/config.py` - the host configuration (`shunt.toml`, legacy `hosts` as fallback).
  The ONLY module that knows either format; CLI and hook both resolve through it. Do not
  re-parse hosts anywhere else.
- `src/shunt/edit_helper.py` - content-based remote edit helper, shipped inline over ssh.
