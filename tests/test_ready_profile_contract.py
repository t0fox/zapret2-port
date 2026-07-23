"""Ready TLS profile set + per-profile contract + release versions (TDD).

Encodes the runtime contract that the runtime branch (A) implements:
  * Exactly 6 ready TLS profiles with fixed IDs; the manifest (profiles.tsv)
    and the installed .opt set must match; gui-quic-fake is NOT ready/install.
  * Each ready profile contains EXACTLY ONE ``--lua-init=@.../init.lua`` line,
    located BEFORE the first ``--lua-desync=circular_quality`` (the selector).
  * Each ready profile has at least one ``strategy=N``; numbering starts at 1,
    is unique and contiguous (no gaps); the selector appears before any
    strategy instance; the NFQWS2_OPT assignment parses as valid shell.
  * orchestra-tls-mvp contains ``strategy=1``.
  * Releases: zapret2 PKG_RELEASE=3, zapret2-orchestra PKG_RELEASE=5.

These tests are RED on the pre-merge base (0b046fa) and GREEN after the
runtime branch lands the contract. The ucode runtime check uses the REAL
pinned host ucode (85922056) via ``ucode apply.uc validate-profile <id>``;
it skips locally when ucode is absent (Windows) and MUST NOT skip in GitHub
Actions (the workflow's zero-skip gate greps for the skip reason string).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _nfqws2_parser as P  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "openwrt" / "zapret2-orchestra"
SHARE_FILES = PACKAGE / "files/usr/share/zapret2-orchestra"
PROFILES_DIR = SHARE_FILES / "profiles"
MANIFEST = SHARE_FILES / "profiles.tsv"
APPLY_UC = SHARE_FILES / "apply.uc"
ORCH_LUA_DIR = PACKAGE / "files/opt/zapret2/lua/orchestra-extra"
INIT_LUA = ORCH_LUA_DIR / "init.lua"

Z2_MAKEFILE = ROOT / "openwrt" / "zapret2" / "Makefile"
ORCH_MAKEFILE = PACKAGE / "Makefile"

# The exact ready TLS profile set. gui-quic-fake is deliberately excluded: it
# is a UDP/QUIC profile, not a ready TLS install profile.
READY_IDS = (
    "orchestra-tls-mvp",
    "gui-tls-multisplit",
    "gui-tls-multidisorder",
    "gui-tls-hostfakesplit",
    "gui-tls-syndata",
    "gui-circular",
)

LUA_INIT_LINE = "--lua-init=@/opt/zapret2/lua/orchestra-extra/init.lua"


def _manifest_ids() -> list[str]:
    ids: list[str] = []
    for line in MANIFEST.read_text("utf-8").splitlines():
        if not line.strip() or line.startswith("id\t"):
            continue
        ids.append(line.split("\t")[0])
    return ids


def _opt_files() -> list[Path]:
    return sorted(PROFILES_DIR.glob("*.opt"))


# ---------------------------------------------------------------------------
# B1 — exact ready set
# ---------------------------------------------------------------------------


class ReadyProfileSetTest(unittest.TestCase):
    """The ready TLS profile set is exactly the 6 fixed IDs, and the manifest
    matches the shipped .opt files. gui-quic-fake is not a ready profile."""

    def test_exactly_six_opt_files(self) -> None:
        opts = _opt_files()
        self.assertEqual(
            len(opts), 6,
            f"expected exactly 6 ready .opt profiles, got {len(opts)}: "
            f"{[p.name for p in opts]}",
        )

    def test_opt_ids_are_the_exact_ready_set(self) -> None:
        ids = {p.stem for p in _opt_files()}
        self.assertEqual(ids, set(READY_IDS), f"opt ids mismatch: {ids}")

    def test_manifest_lists_exactly_the_ready_set(self) -> None:
        ids = _manifest_ids()
        self.assertEqual(
            ids, list(READY_IDS),
            f"manifest ids must be the ready set in order, got {ids}",
        )

    def test_manifest_and_opt_set_match(self) -> None:
        manifest_ids = set(_manifest_ids())
        opt_ids = {p.stem for p in _opt_files()}
        self.assertEqual(manifest_ids, opt_ids,
                         f"manifest {manifest_ids} != opt set {opt_ids}")

    def test_gui_quic_fake_is_not_ready(self) -> None:
        # gui-quic-fake must NOT ship as a ready/install profile.
        self.assertFalse(
            (PROFILES_DIR / "gui-quic-fake.opt").is_file(),
            "gui-quic-fake.opt must not be a ready/install profile",
        )
        self.assertNotIn("gui-quic-fake", _manifest_ids(),
                         "gui-quic-fake must not be in the manifest")

    def test_every_ready_id_has_an_opt_file(self) -> None:
        for pid in READY_IDS:
            self.assertTrue(
                (PROFILES_DIR / f"{pid}.opt").is_file(),
                f"ready id '{pid}' has no .opt file",
            )


# ---------------------------------------------------------------------------
# B2 — per-profile content contract
# ---------------------------------------------------------------------------


class ReadyProfileContentTest(unittest.TestCase):
    """Each ready profile has exactly one --lua-init line before the first
    circular_quality selector, at least one strategy=N, contiguous numbering
    from 1, selector before strategies, and a valid shell assignment."""

    def _profile_text(self, pid: str) -> str:
        return (PROFILES_DIR / f"{pid}.opt").read_text("utf-8")

    def _value(self, pid: str) -> str:
        return P.extract(self._profile_text(pid)).value

    def test_each_ready_profile_has_exactly_one_lua_init(self) -> None:
        for pid in READY_IDS:
            text = self._profile_text(pid)
            n = text.count(LUA_INIT_LINE)
            self.assertEqual(
                n, 1,
                f"{pid}: expected exactly one --lua-init line, got {n}",
            )

    def test_lua_init_before_first_circular_quality(self) -> None:
        for pid in READY_IDS:
            text = self._profile_text(pid)
            init_idx = text.find(LUA_INIT_LINE)
            cq_idx = text.find("--lua-desync=circular_quality")
            self.assertGreater(init_idx, -1, f"{pid}: no --lua-init line")
            self.assertGreater(cq_idx, -1, f"{pid}: no circular_quality selector")
            self.assertLess(
                init_idx, cq_idx,
                f"{pid}: --lua-init must precede the first circular_quality",
            )

    def test_each_ready_profile_has_at_least_one_strategy(self) -> None:
        for pid in READY_IDS:
            val = self._value(pid)
            strategies = re.findall(r"strategy=(\d+)", val)
            self.assertGreaterEqual(
                len(strategies), 1,
                f"{pid}: must have at least one strategy=N",
            )

    def test_strategy_numbering_starts_at_one_contiguous_unique(self) -> None:
        for pid in READY_IDS:
            val = self._value(pid)
            nums = [int(n) for n in re.findall(r"strategy=(\d+)", val)]
            self.assertEqual(
                nums, list(range(1, len(nums) + 1)),
                f"{pid}: strategy numbers must start at 1 and be contiguous "
                f"and unique, got {nums}",
            )

    def test_selector_appears_before_strategy_instances(self) -> None:
        for pid in READY_IDS:
            text = self._profile_text(pid)
            cq_idx = text.find("--lua-desync=circular_quality")
            first_strategy = text.find("strategy=")
            self.assertGreater(cq_idx, -1, f"{pid}: no circular_quality selector")
            self.assertGreater(first_strategy, -1, f"{pid}: no strategy=N")
            self.assertLess(
                cq_idx, first_strategy,
                f"{pid}: circular_quality selector must precede strategy instances",
            )

    def test_profiles_parse_as_valid_shell_assignment(self) -> None:
        if not shutil.which("sh"):
            self.skipTest("sh not on PATH")
        for pid in READY_IDS:
            p = PROFILES_DIR / f"{pid}.opt"
            r = subprocess.run(["sh", "-n", str(p)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{pid}: sh -n failed: {r.stderr}")

    def test_profiles_pass_profile_value_ok_contract(self) -> None:
        # Mirror apply.uc profile_value_ok: reject NUL/CR/$()/`/;/|/&& and
        # unclosed quotes. < and > are allowed (<HOSTLIST> placeholders).
        for pid in READY_IDS:
            val = self._value(pid)
            self.assertNotIn("\x00", val, f"{pid}: NUL")
            self.assertNotIn("\r", val, f"{pid}: CR")
            self.assertNotIn("$(", val, f"{pid}: command substitution")
            self.assertNotIn("`", val, f"{pid}: backtick")
            self.assertNotIn(";", val, f"{pid}: ;")
            self.assertNotIn("|", val, f"{pid}: |")
            self.assertNotIn("&&", val, f"{pid}: &&")
            self.assertEqual(val.count('"') % 2, 0, f"{pid}: unclosed double quote")
            self.assertEqual(val.count("'") % 2, 0, f"{pid}: unclosed single quote")

    def test_each_ready_profile_references_circular_quality(self) -> None:
        for pid in READY_IDS:
            val = self._value(pid)
            self.assertIn("circular_quality", val,
                          f"{pid}: must reference circular_quality")

    def test_mvp_contains_strategy_one(self) -> None:
        val = self._value("orchestra-tls-mvp")
        self.assertIn("strategy=1", val,
                      "orchestra-tls-mvp must contain strategy=1")

    def test_lua_init_points_at_packaged_init_lua(self) -> None:
        # The --lua-init target must exist in the package source tree so the
        # installed profile resolves the Orchestra extension at runtime.
        self.assertTrue(INIT_LUA.is_file(),
                        "orchestra-extra/init.lua missing from package source")


# ---------------------------------------------------------------------------
# B2 (runtime) — real pinned ucode validate-profile for each ready profile
# ---------------------------------------------------------------------------


class ReadyProfileUcodeRuntimeTest(unittest.TestCase):
    """Runs the REAL pinned host ucode ``apply.uc validate-profile <id>`` for
    each ready profile. Skips locally when ucode is absent; MUST NOT skip in
    GitHub Actions (the workflow zero-skip gate greps for this reason)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.ucode = shutil.which("ucode")

    def setUp(self) -> None:
        if self.ucode is None:
            self.skipTest("ucode executable not found on PATH")
        self.tmp = tempfile.mkdtemp(prefix="orch-ready-uc-")
        self.runtime = Path(self.tmp) / "runtime"
        self.runtime.mkdir()
        self.orch = Path(self.tmp) / "orch"
        self.orch.mkdir()
        self.opt = Path(self.tmp) / "opt" / "zapret2"
        self.opt.mkdir(parents=True)
        self.user_profiles = Path(self.tmp) / "user-profiles"
        self.user_profiles.mkdir()
        self.builtin_profiles = Path(self.tmp) / "builtin-profiles"
        self.builtin_profiles.mkdir()
        self.orch_lua = Path(self.tmp) / "orch-lua"
        self.orch_lua.mkdir(parents=True)
        # Seed the orchestra-extra lua the profile --lua-init resolves against.
        for f in ORCH_LUA_DIR.glob("*.lua"):
            (self.orch_lua / f.name).write_text(f.read_text("utf-8"), "utf-8")
        # Persistent JSON seeds so validate-profile / preload checks find them.
        for jf in ("blocked.json", "learned.json", "manual-locks.json", "whitelist.json"):
            src = PACKAGE / "files/etc/zapret2-orchestra" / jf
            if src.is_file():
                (self.orch / jf).write_text(src.read_text("utf-8"), "utf-8")
        # Fake preload wrapper so the manager's preload check passes.
        fake_preload = Path(self.tmp) / "fake-preload.sh"
        fake_preload.write_text(
            "#!/bin/sh\n"
            "mkdir -p \"$ORCHESTRA_RUNTIME_DIR\"\n"
            'case "$1" in\n'
            "  generate)\n"
            '    echo "preload" > "$ORCHESTRA_RUNTIME_DIR/preload.lua"\n'
            '    echo "whitelist" > "$ORCHESTRA_RUNTIME_DIR/whitelist.txt"\n'
            '    echo \'{"schema_version":1}\' > "$ORCHESTRA_RUNTIME_DIR/manifest.json"\n'
            "    exit 0 ;;\n"
            "  check)\n"
            '    [ -f "$ORCHESTRA_RUNTIME_DIR/preload.lua" ] && exit 0 || exit 1 ;;\n'
            "  *) exit 1 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_preload.chmod(0o755)
        (self.opt / "config").write_text(
            'NFQWS2_ENABLE=0\nNFQWS2_OPT="--lua-desync=circular_quality:key=tls"\n'
            'MODE_FILTER=none\n',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self) -> dict:
        env = os.environ.copy()
        env["ZAPRET2_RUNTIME_DIR"] = str(self.runtime)
        env["ZAPRET2_ORCHESTRA_DIR"] = str(self.orch)
        env["ZAPRET2_SHARE_DIR"] = str(Path(self.tmp) / "share")
        env["ZAPRET2_USER_PROFILES_DIR"] = str(self.user_profiles)
        env["ZAPRET2_BUILTIN_PROFILES_DIR"] = str(self.builtin_profiles)
        env["ZAPRET2_ORCHESTRA_LUA"] = str(self.orch_lua)
        env["ZAPRET2_STATE_FILE"] = str(self.orch / "manager-state.json")
        env["ZAPRET2_LOCK_DIR"] = str(self.runtime / "apply.lock")
        env["ZAPRET2_VALIDATE_OUT"] = str(self.runtime / "validate-config.cfg")
        env["ZAPRET2_CANDIDATE_FILE"] = str(self.opt / ".config.orchestra.tmp")
        env["ZAPRET2_BACKUP_DIR"] = str(self.orch / "backup")
        env["ZAPRET2_CONFIG"] = str(self.opt / "config")
        env["ZAPRET2_PRELOAD_WRAPPER"] = str(Path(self.tmp) / "fake-preload.sh")
        env["ORCHESTRA_STATE_DIR"] = str(self.orch)
        env["ORCHESTRA_RUNTIME_DIR"] = str(self.runtime)
        env["ORCHESTRA_PRELOAD_FILE"] = str(self.runtime / "preload.lua")
        env["ORCHESTRA_WHITELIST_FILE"] = str(self.runtime / "whitelist.txt")
        env["ORCHESTRA_MANIFEST_FILE"] = str(self.runtime / "manifest.json")
        return env

    def _run_validate(self, pid: str) -> subprocess.CompletedProcess:
        # Copy the shipped builtin profile into the test builtin dir so ucode
        # can find it, exactly as the package installs it.
        src = PROFILES_DIR / f"{pid}.opt"
        (self.builtin_profiles / f"{pid}.opt").write_text(
            src.read_text("utf-8"), "utf-8")
        r = subprocess.run(
            [self.ucode, str(APPLY_UC), "--", "validate-profile", pid],
            env=self._env(), capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0 or not r.stdout.strip():
            sys.stderr.write(
                f"[validate-profile {pid} rc={r.returncode}] "
                f"stdout={r.stdout!r} stderr={r.stderr!r}\n")
        return r

    def test_every_ready_profile_validates_under_real_ucode(self) -> None:
        for pid in READY_IDS:
            with self.subTest(profile=pid):
                r = self._run_validate(pid)
                self.assertEqual(r.returncode, 0,
                                 f"{pid}: validate-profile failed: {r.stderr}")
                doc = json.loads(r.stdout)
                self.assertTrue(doc["ok"],
                                f"{pid}: validate-profile ok=false: {doc}")
                self.assertEqual(doc["source_type"], "builtin",
                                 f"{pid}: expected builtin source_type")

    def test_mvp_validates_under_real_ucode(self) -> None:
        r = self._run_validate("orchestra-tls-mvp")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertTrue(doc["ok"], f"MVP validate failed: {doc}")


# ---------------------------------------------------------------------------
# B3 — release versions
# ---------------------------------------------------------------------------


class ReleaseVersionTest(unittest.TestCase):
    """zapret2 PKG_RELEASE=3, zapret2-orchestra PKG_RELEASE=7 (r7)."""

    def test_zapret2_release_is_three(self) -> None:
        text = Z2_MAKEFILE.read_text("utf-8")
        self.assertRegex(text, r"(?m)^PKG_RELEASE:=3$",
                         "zapret2 PKG_RELEASE must be 3")

    def test_orchestra_release_is_seven(self) -> None:
        text = ORCH_MAKEFILE.read_text("utf-8")
        self.assertRegex(text, r"(?m)^PKG_RELEASE:=7$",
                         "zapret2-orchestra PKG_RELEASE must be 7 (r7)")


if __name__ == "__main__":
    unittest.main()
