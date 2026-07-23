"""Regression coverage for the shipped Orchestra CLI argv sentinel.

The runtime manager ``apply.uc`` is invoked two ways in production:

  * directly by tests / the CI sandbox:  ``ucode apply.uc <sub> [args]``
  * by the SHIPPED wrapper ``zapret2-orchestra-apply``:
        exec ucode "$UC" -- "$SUB" "$@"      (general case)
        ucode "$UC" -- validate-config        (validate-config case)

The ``--`` is a getopt sentinel that lets subcommand arguments such as
``rollback --force --gen N`` reach apply.uc as ARGV instead of being parsed
by the ucode CLI. The router's ucode build (ucode-2026.01.16~85922056) does
NOT consume ``--`` — it passes it through to the script as ARGV[0], where the
old dispatcher read ``ARGV[0]`` as the subcommand and rejected it with
``{"ok": false, "error": "unknown subcommand", "command": "--"}``. The host
ucode build used in CI DOES consume ``--``, which is why this regression
escaped CI: every wrapper invocation was green on the CI ucode while the
identical shipped wrapper failed on the router.

apply.uc now normalizes an OPTIONAL leading ``--`` (offset 0 or 1) so the
shipped wrapper works under both ucode behaviors, and direct (no-sentinel)
invocation is unchanged.

This module has two layers:

  1. Static contract checks on apply.uc and the wrappers (always run). These
     are the deterministic pre-fix-fail / post-fix-pass guards: before the
     fix the sentinel normalization is absent, so these fail; after the fix
     they pass — on every platform, with or without ucode.
  2. Runtime checks that EXECUTE the real shipped wrappers (apply-wrapper and
     profile-frontend) against the repo apply.uc in an isolated sandbox, with
     and without the sentinel, plus rollback argument preservation. Skipped
     when ``ucode`` is not on PATH (CI runs them; the validate job forbids
     the skip).
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

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "openwrt" / "zapret2-orchestra"
APPLY_UC = PACKAGE / "files/usr/share/zapret2-orchestra/apply.uc"
APPLY_WRAPPER = PACKAGE / "files/usr/sbin/zapret2-orchestra-apply"
PROFILE_WRAPPER = PACKAGE / "files/usr/sbin/zapret2-orchestra-profile"
MAKEFILE = PACKAGE / "Makefile"
REPO_PROFILES = PACKAGE / "files/usr/share/zapret2-orchestra/profiles"
REPO_LUA = PACKAGE / "files/opt/zapret2/lua/orchestra-extra"


# ---------------------------------------------------------------------------
# 1. Static contract checks (always run — the deterministic sentinel guard)
# ---------------------------------------------------------------------------

class ApplyUcSentinelStaticTest(unittest.TestCase):
    """apply.uc must normalize an optional leading ``--`` argv sentinel."""

    def setUp(self) -> None:
        self.uc = APPLY_UC.read_text(encoding="utf-8")
        self.apply_wrapper = APPLY_WRAPPER.read_text(encoding="utf-8")
        self.profile_wrapper = PROFILE_WRAPPER.read_text(encoding="utf-8")

    def test_apply_uc_accepts_optional_leading_sentinel(self) -> None:
        # The dispatcher must skip a single leading "--" before reading the
        # subcommand. Before the fix none of these exist -> this test fails;
        # after the fix they are present -> passes. This is the deterministic
        # regression guard that fails pre-fix on EVERY platform (incl. CI's
        # host ucode, which consumes "--" and thus never exercises the bug at
        # runtime — the static check is what closes that CI gap).
        self.assertRegex(self.uc, r"let\s+offset\s*=\s*0\s*;",
                         "apply.uc must declare an offset starting at 0")
        self.assertRegex(self.uc, r"ARGV\[0\]\s*==\s*'--'",
                         "apply.uc must detect a leading '--' sentinel")
        self.assertRegex(self.uc, r"ARGV\[offset\]",
                         "apply.uc must read the subcommand at ARGV[offset]")
        self.assertRegex(self.uc, r"i\s*=\s*offset\s*\+\s*1",
                         "apply.uc must build rest from ARGV[offset+1:]")

    def test_apply_uc_dispatch_still_reads_subcommand(self) -> None:
        # The subcommand variable is still derived from ARGV (now at offset).
        self.assertRegex(self.uc, r"let\s+sub\s*=\s*length\(ARGV\)\s*>\s*offset")

    def test_apply_uc_unknown_subcommand_branch_unchanged(self) -> None:
        # The error shape the router hit must still exist for genuinely
        # unknown subcommands (just not for the sentinel anymore).
        self.assertIn("'unknown subcommand'", self.uc)

    def test_wrapper_retains_sentinel_in_general_case(self) -> None:
        # The fix is in apply.uc, NOT the wrapper: the wrapper MUST keep the
        # "--" so rollback's --force/--gen are not parsed by the ucode CLI on
        # builds that DO consume "--" (e.g. the CI host ucode).
        self.assertRegex(self.apply_wrapper,
                         r'exec\s+ucode\s+"\$UC"\s+--\s+"\$SUB"\s+"\$@"')

    def test_wrapper_retains_sentinel_in_validate_config_case(self) -> None:
        self.assertRegex(self.apply_wrapper,
                         r'ucode\s+"\$UC"\s+--\s+validate-config')

    def test_wrapper_unknown_subcommand_message(self) -> None:
        # The wrapper's own unknown-subcommand branch (distinct from
        # apply.uc's) must still reject genuinely unknown subcommands.
        self.assertIn("unknown subcommand", self.apply_wrapper)

    def test_profile_frontend_delegates_to_apply(self) -> None:
        # The profile frontend must dispatch validate/enable/disable/status to
        # the apply wrapper (which inserts the sentinel), not call apply.uc
        # directly. Validate uses validate-profile; enable pre-validates then
        # enables; disable/status delegate directly.
        for token in ('validate-profile "$1"', 'enable "$1"', 'disable',
                      'status'):
            self.assertIn(token, self.profile_wrapper, token)
        self.assertRegex(self.profile_wrapper, r'APPLY=.*ZAPRET2_APPLY')

    def test_apply_uc_does_not_double_consume_sentinel(self) -> None:
        # Only ONE leading "--" is skipped; a second "--" must reach the
        # subcommand (so e.g. `ucode apply.uc -- -- status` is NOT silently
        # turned into status — it is an unknown subcommand "--"). This keeps
        # the normalization minimal and matches the router's pass-through of
        # a single sentinel.
        # The guard is a single `if` (not a while loop) consuming at most one.
        self.assertRegex(self.uc, r"if\s*\(\s*length\(ARGV\)\s*>\s*0\s*&&\s*ARGV\[0\]\s*==\s*'--'\s*\)")
        self.assertNotRegex(self.uc, r"while\s*\([^)]*ARGV\[0\]\s*==\s*'--'")


# ---------------------------------------------------------------------------
# 2. Runtime checks: execute the real shipped wrappers (skip if no ucode)
# ---------------------------------------------------------------------------

class ShippedCliSentinelRuntimeTest(unittest.TestCase):
    """Execute the shipped apply-wrapper and profile-frontend end to end.

    Skipped when ucode is not on PATH (the validate CI job forbids the skip).
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.ucode = shutil.which("ucode")

    def setUp(self) -> None:
        if self.ucode is None:
            self.skipTest("ucode executable not found on PATH")
        if shutil.which("sh") is None:
            self.skipTest("sh not on PATH")
        self.tmp = tempfile.mkdtemp(prefix="orch-cli-sentinel-")
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
        # Copy ALL shipped builtin profiles so every ready profile validates.
        for o in REPO_PROFILES.glob("*.opt"):
            (self.builtin_profiles / o.name).write_text(
                o.read_text(encoding="utf-8"), encoding="utf-8")
        # Orchestra Lua dir must exist; prefer the repo tree, fall back to a
        # stub so validate-profile's source-path checks resolve.
        if REPO_LUA.is_dir():
            self.orch_lua = REPO_LUA
        else:
            self.orch_lua = Path(self.tmp) / "orch-lua"
            self.orch_lua.mkdir()
            (self.orch_lua / "init.lua").write_text("-- test\n", encoding="utf-8")
        (self.orch / "whitelist.json").write_text(
            json.dumps({"schema_version": 1, "hosts": []}), encoding="utf-8")
        # A fake preload wrapper so any preload check passes without touching
        # the real generate-preload.uc.
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
            '    [ -f \"$ORCHESTRA_RUNTIME_DIR/preload.lua\" ] && exit 0 || exit 1 ;;\n'
            "  *) exit 1 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_preload.chmod(0o755)
        self._write_default_config()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_default_config(self, nfqws2_opt: str = "--lua-desync=fake:blob=default") -> None:
        (self.opt / "config").write_text(
            f'NFQWS2_ENABLE=0\nNFQWS2_OPT="{nfqws2_opt}"\nMODE_FILTER=none\n',
            encoding="utf-8",
        )

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
        env["ZAPRET2_APPLY_UC"] = str(APPLY_UC)
        env["ZAPRET2_APPLY"] = str(APPLY_WRAPPER)
        env["ORCHESTRA_STATE_DIR"] = str(self.orch)
        env["ORCHESTRA_RUNTIME_DIR"] = str(self.runtime)
        env["ORCHESTRA_PRELOAD_FILE"] = str(self.runtime / "preload.lua")
        env["ORCHESTRA_WHITELIST_FILE"] = str(self.runtime / "whitelist.txt")
        env["ORCHESTRA_MANIFEST_FILE"] = str(self.runtime / "manifest.json")
        return env

    def _run_uc(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.ucode, str(APPLY_UC), *args],
            env=self._env(), capture_output=True, text=True, timeout=10)

    def _run_uc_sentinel(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.ucode, str(APPLY_UC), "--", *args],
            env=self._env(), capture_output=True, text=True, timeout=10)

    def _run_wrapper(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", str(APPLY_WRAPPER), *args],
            env=self._env(), capture_output=True, text=True, timeout=10)

    def _run_profile(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", str(PROFILE_WRAPPER), *args],
            env=self._env(), capture_output=True, text=True, timeout=10)

    def _assert_no_sentinel_error(self, r: subprocess.CompletedProcess, ctx: str) -> None:
        self.assertNotIn(
            'unknown subcommand', r.stdout + r.stderr,
            f"{ctx}: shipped CLI must not emit 'unknown subcommand' (sentinel "
            f"leak). rc={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}")
        self.assertNotIn('"command": "--"', r.stdout + r.stderr, ctx)

    # --- direct apply.uc (no sentinel): unchanged behaviour -----------------

    def test_direct_status_no_sentinel(self) -> None:
        r = self._run_uc("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(json.loads(r.stdout)["ok"])

    def test_direct_validate_profile_no_sentinel(self) -> None:
        r = self._run_uc("validate-profile", "orchestra-tls-mvp")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(json.loads(r.stdout)["ok"])

    # --- direct apply.uc WITH sentinel: the router's ARGV shape -------------

    def test_direct_status_with_sentinel(self) -> None:
        # `ucode apply.uc -- status`: on a ucode that passes "--" through
        # (router) apply.uc receives ARGV=["--","status"]; on a ucode that
        # consumes "--" (CI host) apply.uc receives ARGV=["status"]. Both
        # must yield ok:true now.
        r = self._run_uc_sentinel("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self._assert_no_sentinel_error(r, "direct -- status")
        self.assertTrue(json.loads(r.stdout)["ok"])

    def test_direct_validate_profile_with_sentinel(self) -> None:
        r = self._run_uc_sentinel("validate-profile", "orchestra-tls-mvp")
        self.assertEqual(r.returncode, 0, r.stderr)
        self._assert_no_sentinel_error(r, "direct -- validate-profile")
        self.assertTrue(json.loads(r.stdout)["ok"])

    # --- shipped apply-wrapper (exec ucode "$UC" -- "$SUB" "$@") -----------

    def test_wrapper_status(self) -> None:
        r = self._run_wrapper("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self._assert_no_sentinel_error(r, "wrapper status")
        self.assertTrue(json.loads(r.stdout)["ok"])

    def test_wrapper_validate_config(self) -> None:
        r = self._run_wrapper("validate-config")
        self.assertEqual(r.returncode, 0, r.stderr)
        self._assert_no_sentinel_error(r, "wrapper validate-config")
        self.assertIn('"sh_n_ok":true', r.stdout)

    def test_wrapper_validate_profile_mvp(self) -> None:
        r = self._run_wrapper("validate-profile", "orchestra-tls-mvp")
        self.assertEqual(r.returncode, 0, r.stderr)
        self._assert_no_sentinel_error(r, "wrapper validate-profile mvp")
        self.assertTrue(json.loads(r.stdout)["ok"])

    def test_wrapper_lock_test(self) -> None:
        r = self._run_wrapper("lock-test")
        self.assertEqual(r.returncode, 0, r.stderr)
        self._assert_no_sentinel_error(r, "wrapper lock-test")

    def test_wrapper_unknown_subcommand_is_not_sentinel(self) -> None:
        # A genuinely unknown subcommand must be rejected, but with the real
        # name — never the sentinel "--".
        r = self._run_wrapper("bogus-subcommand")
        self.assertNotEqual(r.returncode, 0)
        combined = r.stdout + r.stderr
        self.assertIn("unknown subcommand", combined)
        self.assertIn("bogus-subcommand", combined)
        self.assertNotIn('"command": "--"', combined)
        self.assertNotIn("'--'", combined)

    # --- shipped profile-frontend (zapret2-orchestra-profile) --------------

    def test_profile_frontend_status(self) -> None:
        r = self._run_profile("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self._assert_no_sentinel_error(r, "profile status")
        self.assertTrue(json.loads(r.stdout)["ok"])

    def test_profile_frontend_validate_mvp(self) -> None:
        r = self._run_profile("validate", "orchestra-tls-mvp")
        self.assertEqual(r.returncode, 0, r.stderr)
        self._assert_no_sentinel_error(r, "profile validate mvp")
        self.assertTrue(json.loads(r.stdout)["ok"])

    def test_profile_frontend_validate_all_six(self) -> None:
        for pid in ("orchestra-tls-mvp", "gui-tls-multisplit",
                    "gui-tls-multidisorder", "gui-tls-hostfakesplit",
                    "gui-tls-syndata", "gui-circular"):
            r = self._run_profile("validate", pid)
            self.assertEqual(r.returncode, 0,
                             f"profile validate {pid}: rc={r.returncode} "
                             f"stdout={r.stdout!r} stderr={r.stderr!r}")
            self._assert_no_sentinel_error(r, f"profile validate {pid}")
            self.assertTrue(json.loads(r.stdout)["ok"], pid)

    # --- rollback argument preservation (no dangerous rollback executed) ---

    def test_rollback_force_gen_args_preserved(self) -> None:
        # `rollback --force --gen 1` must reach cmd_rollback with
        # ARGV=["--force","--gen","1"]. In a clean sandbox there is no backup
        # generation 1, so cmd_rollback fails with "backup generation 1 not
        # found" — which PROVES --gen 1 was parsed (target_gen=1), without
        # ever performing a real rollback. The failure must NOT be the
        # sentinel "unknown subcommand '--'".
        r = self._run_wrapper("rollback", "--force", "--gen", "1")
        combined = r.stdout + r.stderr
        self._assert_no_sentinel_error(r, "rollback --force --gen 1")
        # --gen 1 was parsed -> it looked for backup generation 1.
        self.assertIn("generation 1", combined)
        # Non-zero is expected (no such backup); the point is the args were
        # preserved and parsed, not that rollback succeeded.
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
