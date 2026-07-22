"""Lexer helper that distinguishes ucode source comments from `--` inside strings.

ucode (the OpenWrt script interpreter) uses C-style ``//`` line comments. The
zapret2-orchestra runtime manager sources (``apply.uc``, ``generate-preload.uc``)
were originally written with Lua-style ``--`` line comments, which the target
ucode treats as the decrement operator and refuses to parse.

This module provides a small line-based lexer that walks a ucode source line
while tracking single- and double-quoted string state (with ``\\`` escapes), so
that only ``--`` sequences *outside* string literals are reported as Lua-style
comment starters. ``--`` inside string literals (e.g. ``'--force'``,
``'-- Auto-generated ...'``) and NFQWS2 flag strings is preserved.

The same lexer powers:

* the one-shot converter that rewrites ``--`` comments to ``//`` comments;
* the regression test that asserts no Lua-style comments remain and that the
  preserved string literals are still present.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommentMark:
    """A ``--`` that begins a Lua-style line comment (outside any string)."""

    line: int          # 1-indexed line number
    column: int        # 0-indexed byte column of the first '-'
    full_line: str     # the raw source line


@dataclass(frozen=True)
class StringDash:
    """A ``--`` that lives inside a string literal and must be preserved."""

    line: int
    column: int
    full_line: str


def _scan_line(line: str):
    """Yield (kind, column) tuples for one source line.

    kind is 'comment' for a ``--`` outside strings, 'string-dash' for a ``--``
    inside a string literal. Only the first comment starter on a line is
    reported (the rest of the line is comment text). String dashes are reported
    for each ``--`` found while inside a string.
    """
    i = 0
    n = len(line)
    in_single = False
    in_double = False
    while i < n:
        ch = line[i]
        if in_single:
            if ch == '\\' and i + 1 < n:
                i += 2
                continue
            if ch == "'":
                in_single = False
            # track '--' inside single-quoted string
            if ch == '-' and i + 1 < n and line[i + 1] == '-':
                yield ('string-dash', i)
                i += 2
                continue
            i += 1
            continue
        if in_double:
            if ch == '\\' and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                in_double = False
            if ch == '-' and i + 1 < n and line[i + 1] == '-':
                yield ('string-dash', i)
                i += 2
                continue
            i += 1
            continue
        # outside any string
        if ch == "'":
            in_single = True
            i += 1
            continue
        if ch == '"':
            in_double = True
            i += 1
            continue
        # C-style line comment: the rest of the line is comment body. Any `--`
        # after this point is comment text, not a Lua-style comment starter, so
        # stop scanning without reporting it.
        if ch == '/' and i + 1 < n and line[i + 1] == '/':
            return
        # Lua-style line comment: report it (this is what the converter targets).
        if ch == '-' and i + 1 < n and line[i + 1] == '-':
            yield ('comment', i)
            # rest of line is comment text; stop scanning
            return
        i += 1
    return


def scan_source(text: str):
    """Scan multi-line source text. Returns (comments, string_dashes)."""
    comments = []
    string_dashes = []
    for idx, line in enumerate(text.splitlines(), start=1):
        for kind, col in _scan_line(line):
            if kind == 'comment':
                comments.append(CommentMark(line=idx, column=col, full_line=line))
            else:
                string_dashes.append(StringDash(line=idx, column=col, full_line=line))
    return comments, string_dashes


def convert_line(line: str) -> str:
    """Rewrite the first Lua-style ``--`` comment on a line to ``//``.

    Returns the line unchanged if it has no ``--`` comment starter outside a
    string. Only the first comment starter is rewritten; any text after it
    (including stray ``--`` inside the comment text) is left as-is because it
    is comment body, not code.
    """
    for kind, col in _scan_line(line):
        if kind == 'comment':
            return line[:col] + '//' + line[col + 2:]
    return line


def convert_source(text: str) -> str:
    """Convert a whole source document, preserving line endings as splitlines/join."""
    lines = text.splitlines()
    out = [convert_line(ln) for ln in lines]
    # Re-join with \n; caller can normalize trailing newline.
    return '\n'.join(out) + '\n' if text.endswith('\n') else '\n'.join(out)
