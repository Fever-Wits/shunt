# Contributing to shunt

Thanks for helping. shunt is small and deliberately stays that way. Read this
once — it fits on a screen.

## The stdlib-only invariant

**shunt has zero runtime dependencies and must keep it that way.** Everything
runs on the Python standard library (`>=3.11`). This is not an accident: the
hook-rewritten command runs in a strict sandbox, and the file helpers are
deployed inline to remote hosts that may have nothing but `python3`. **Do
not add a new dependency** — `[project].dependencies` in `pyproject.toml` stays
empty. If you think you need one, open an issue first; the answer is almost
always "use stdlib."

## Running tests

```sh
python -m unittest
```

## Linting and formatting

```sh
ruff check .
ruff format .
```

Run both before opening a PR. `ruff format` is the source of truth for style;
don't hand-format around it.

## Commits

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add @host alias autocompletion
fix: preserve CRLF style on edit verify
docs: clarify the transport threat model
```

Common types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`.

## Changelog

**Each PR adds an entry under an `## [Unreleased]` section** in `CHANGELOG.md`,
in the appropriate group (`Added` / `Changed` / `Fixed` / `Security` / etc.).
We follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). At release
time the maintainer renames `[Unreleased]` to the new version.

## Versioning

shunt uses **CalVer** with the scheme `YYYYMMDDHH` — year, month, day, hour of
the release (e.g. `2026062322` = 2026-06-23, 22:00). There is no semantic
major/minor/patch contract; the version simply records when a release was cut.
The maintainer sets it in `pyproject.toml` and tags the release.
