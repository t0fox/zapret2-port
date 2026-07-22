#!/usr/bin/env python3
"""One-shot converter: rewrite Lua-style `--` comments to `//` in ucode sources.

Uses the lexer in tests/_ucode_comment_lint.py so the conversion and the
regression test share the exact same notion of "comment vs string".
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
from _ucode_comment_lint import scan_source, convert_source  # noqa: E402

TARGETS = [
    "openwrt/zapret2-orchestra/files/usr/share/zapret2-orchestra/apply.uc",
    "openwrt/zapret2-orchestra/files/usr/share/zapret2-orchestra/generate-preload.uc",
    "openwrt/zapret2-orchestra/files/usr/share/rpcd/ucode/zapret2.orchestra",
]


def main(root: Path) -> int:
    rc = 0
    for rel in TARGETS:
        path = root / rel
        if not path.exists():
            print(f"MISSING: {path}")
            rc = 1
            continue
        text = path.read_text(encoding="utf-8")
        comments, string_dashes = scan_source(text)
        if not comments:
            print(f"CLEAN (no Lua comments): {rel}  [preserved -- in strings: {len(string_dashes)}]")
            continue
        converted = convert_source(text)
        # sanity: conversion must remove every comment mark
        new_comments, new_strings = scan_source(converted)
        if new_comments:
            print(f"ERROR: conversion left {len(new_comments)} Lua comments in {rel}")
            rc = 1
            continue
        # preserve the string dashes exactly (same count, same lines)
        if len(new_strings) != len(string_dashes):
            print(f"ERROR: string-dash count changed {len(string_dashes)} -> {len(new_strings)} in {rel}")
            rc = 1
            continue
        path.write_text(converted, encoding="utf-8")
        print(f"CONVERTED: {rel}  comments={len(comments)}  preserved_string_dashes={len(string_dashes)}")
    return rc


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    sys.exit(main(root))
