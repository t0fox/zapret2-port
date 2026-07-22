"""Reference implementation of the NFQWS2_OPT multiline parser/transformer.

This is the Python *oracle* that mirrors the ucode parser in
``openwrt/zapret2-orchestra/files/usr/share/zapret2-orchestra/apply.uc``.
It is used by the behavioral tests to verify the parser algorithm in pure
Python (independent of a ucode interpreter) and to produce golden fixtures
the ucode runtime tests compare against.

Security model
--------------
The parser NEVER sources, evals, or otherwise executes the config. It scans
the file as plain text, locates the ``NFQWS2_OPT="..."`` assignment, and
extracts or replaces its value with pure string operations. The only shell
execution permitted anywhere in the manager is ``sh -n`` on the *transformed
output* (parse-only, never executes), and that is invoked by the CLI wrapper
-- not by this module.

Assignment rules (errors are raised as ``ParseError``)
------------------------------------------------------
* missing   : no line matches ``^\\s*NFQWS2_OPT=`` (ignoring comments).
* duplicate : more than one line matches ``^\\s*NFQWS2_OPT=``.
* unquoted  : the single assignment is not opened with a double quote on the
              same line, immediately after the ``=``.
* unclosed  : the opening double quote is never closed before EOF.

Byte preservation
-----------------
``transform(text, new_value)`` re-emits the file with the NFQWS2_OPT value
replaced by ``new_value``; every byte outside the quoted value is preserved
exactly because ``head`` and ``tail`` are literal substrings of the input.
``transform(text, extract(text).value)`` (replace with the same value)
reproduces the input byte-for-byte -- the key invariant tested by the golden
fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass


class ParseError(ValueError):
    """Raised for missing/duplicate/unquoted/unclosed NFQWS2_OPT."""


@dataclass(frozen=True)
class Parsed:
    value: str        # logical (escape-resolved) value
    raw: str          # exact bytes between the quotes, escapes intact
    head: str         # text up to and including the opening double quote
    tail: str         # text from the closing double quote to EOF
    open_line: int    # 1-based line of the opening quote
    close_line: int   # 1-based line of the closing quote


_KEY = "NFQWS2_OPT"


def _line_of(text: str, index: int) -> int:
    """1-based line number of ``index`` in ``text``."""
    return text.count("\n", 0, index) + 1


def _assignment_start(line: str) -> int | None:
    """If ``line`` begins (after optional whitespace, not a comment) with
    ``NFQWS2_OPT=``, return the column of the ``N`` in ``line``; else None."""
    i = 0
    while i < len(line) and line[i] in " \t":
        i += 1
    if i < len(line) and line[i] == "#":
        return None
    if line.startswith(_KEY + "=", i):
        return i
    return None


def extract(text: str) -> Parsed:
    """Parse ``text`` and return the NFQWS2_OPT value with surrounding context.

    Raises ParseError on missing/duplicate/unquoted/unclosed.
    """
    # Find assignment lines by scanning line starts in the full text.
    starts: list[int] = []  # absolute indices of the 'N' of NFQWS2_OPT=
    i = 0
    line_start = 0
    while i < len(text):
        if i == line_start:
            s = _assignment_start(text[i: i + len(_KEY) + 1 + 64])
            if s is not None:
                starts.append(line_start + s)
        if text[i] == "\n":
            line_start = i + 1
        i += 1
    # The slice-based check above can false-positive if the line is longer than
    # the window; validate each candidate properly by extracting the line.
    real_starts: list[int] = []
    for st in starts:
        nl = text.find("\n", st)
        line = text[st: nl] if nl != -1 else text[st:]
        if _assignment_start(line) is not None:
            real_starts.append(st)

    if len(real_starts) == 0:
        raise ParseError("missing NFQWS2_OPT assignment")
    if len(real_starts) > 1:
        raise ParseError("duplicate NFQWS2_OPT assignment")

    st = real_starts[0]
    eq_pos = st + len(_KEY)  # index of '='
    open_quote = eq_pos + 1  # expected position of the opening '"'
    if open_quote >= len(text) or text[open_quote] != '"':
        raise ParseError("NFQWS2_OPT value is not double-quoted")
    # Require the quote immediately after '=' (no whitespace gap) for a
    # deterministic, shell-unambiguous parse.
    head = text[: open_quote + 1]

    # Walk the value from open_quote+1, tracking backslash escapes, until the
    # closing double quote. A bare " (not escaped) closes the value.
    raw_chars: list[str] = []
    val_chars: list[str] = []
    escape = False
    j = open_quote + 1
    n = len(text)
    closed = -1
    while j < n:
        ch = text[j]
        if escape:
            raw_chars.append(ch)
            val_chars.append(ch)
            escape = False
            j += 1
            continue
        if ch == "\\":
            raw_chars.append(ch)
            escape = True
            j += 1
            continue
        if ch == '"':
            closed = j
            break
        raw_chars.append(ch)
        val_chars.append(ch)
        j += 1

    if closed == -1:
        raise ParseError("unclosed NFQWS2_OPT value")

    raw = "".join(raw_chars)
    value = "".join(val_chars)
    tail = text[closed:]

    return Parsed(
        value=value,
        raw=raw,
        head=head,
        tail=tail,
        open_line=_line_of(text, open_quote),
        close_line=_line_of(text, closed),
    )


def escape_value(value: str) -> str:
    """Escape a new value for embedding inside double quotes: backslash first,
    then double quote. Newlines are preserved literally (valid inside shell
    double quotes)."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def transform(text: str, new_value: str) -> str:
    """Return ``text`` with the NFQWS2_OPT value replaced by ``new_value``.

    Every byte outside the quoted value is preserved exactly. Raises
    ParseError if the assignment is missing/duplicate/unquoted/unclosed.
    """
    p = extract(text)
    return p.head + escape_value(new_value) + p.tail


def validate_round_trip(text: str) -> bool:
    """True if extracting the value and transforming with the same value
    reproduces ``text`` byte-for-byte. This is the byte-preservation
    invariant."""
    try:
        p = extract(text)
    except ParseError:
        return False
    return transform(text, p.value) == text
