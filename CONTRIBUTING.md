# Contributing to shunt

Thanks for helping. shunt is small and deliberately stays that way. Read this
once - it fits on a screen.

## The stdlib-only invariant

**shunt has zero runtime dependencies and must keep it that way.** Everything
runs on the Python standard library (`>=3.11`). This is not an accident: the
hook is invoked as a bare `python3 <path>`, and the file helpers are
deployed inline to remote hosts that may have nothing but `python3`. **Do
not add a new dependency** - `[project].dependencies` in `pyproject.toml` stays
empty. If you think you need one, open an issue first; the answer is almost
always "use stdlib."

## ASCII-only source

**The source is ASCII**, with one deliberate exception: `U+26A0` (WARNING SIGN),
`U+2713` (CHECK MARK) and `U+2139` (INFORMATION SOURCE) inside messages the tool
*emits* to the reading model -- these are signal glyphs: attention cues the model
acts on, not decoration. The rule is by role, not by character: a new glyph that
appears inside an emitted message is kept and added to this list; the same glyph
in a comment, docstring or piece of documentation is decoration and is spelled out
instead. A test input that genuinely needs a non-ASCII character is written as a
`\uXXXX` escape, with a word saying which Unicode property it exercises.

| glyph | code point | Unicode name | what it looks like | what it tells the reading model |
| --- | --- | --- | --- | --- |
| ⚠ | U+26A0 | WARNING SIGN | a triangle with an exclamation mark | "be careful here" |
| ✓ | U+2713 | CHECK MARK | a tick | "this is fine / done" |
| ℹ | U+2139 | INFORMATION SOURCE | a circled lowercase i | "informational, no action needed" |

This table is the one place a document shows the glyphs themselves -- here they are the
subject, not decoration; the anchor below skips exactly these three code points.

```sh
grep -rnP '[^\x00-\x7F]' . --exclude-dir=.git | grep -vP '[\x{26A0}\x{2713}\x{2139}]'   # -> empty
```

## English only

**Code, comments, docstrings, documentation and test fixtures are in English.** If a
test needs a non-ASCII value, use one that explains itself and says why in a word:
`cafU+00E9` for "bytes that are not ASCII", `U+00B2` or `U+0663` when the point is the Unicode
category itself. Do not reach for another script as a stand-in - a fixture in a
language nobody else in the file speaks is noise at best, and at worst it is the only
thing a reader remembers about the test.

## The helpers avoid f-strings - on purpose

`src/shunt/edit_helper.py` and `src/shunt/write_helper.py` use `%` formatting while the
rest of the package uses f-strings. This is not an oversight and should not be tidied
up. Both files carry `MIN_PYTHON = (3, 3)` and a guard that answers in JSON when the
remote interpreter is older; an f-string is 3.6 syntax, so adding one makes the file
fail to *compile* below 3.6 and the guard can never run - a promise of protection that
cannot fire. `tests/test_helpers_far_side.py` enforces this.

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
fix: keep the socket keyed on the ssh user
docs: clarify the transport threat model
```

Common types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`.

## Changelog

**Each PR adds an entry under an `## [Unreleased]` section** in `CHANGELOG.md`,
in the appropriate group (`Added` / `Changed` / `Fixed` / `Security` / etc.).
We follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). At release
time the maintainer renames `[Unreleased]` to the new version.

## Versioning

shunt uses **CalVer** with the scheme `YYYYMMDDHH` - year, month, day, hour of
the release (e.g. `2026062322` = 2026-06-23, 22:00). There is no semantic
major/minor/patch contract; the version simply records when a release was cut.
The maintainer sets it in `pyproject.toml` and tags the release.
