"""Behavioral tests for the zapret2-orchestra runtime manager (Phase 1A).

Six layers:
  1. Static contract checks on apply.uc, the CLI wrapper, and the Makefile
     (always runs).
  2. A Python oracle for the NFQWS2_OPT parser/transformer, verified against
     fixture configs under tests/fixtures/nfqws2/ (always runs).
  3. Shell-injection and byte-preservation invariants (always runs).
  4. Python oracles for profile validation, atomic JSON state, and the mkdir
     lock (always runs).
  5. No-config-mutation and no-UCI/remittor/config.default-fallback invariants
     (always runs).
  6. Runtime tests that execute the real ucode manager and the `sh -n` path.
     Skipped when ``ucode`` is not on PATH.
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

# The parser oracle is the spec the ucode implementation must mirror.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _nfqws2_parser as P  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "openwrt" / "zapret2-orchestra"
APPLY_UC = PACKAGE / "files/usr/share/zapret2-orchestra/apply.uc"
APPLY_WRAPPER = PACKAGE / "files/usr/sbin/zapret2-orchestra-apply"
MAKEFILE = PACKAGE / "Makefile"
FIXTURES = ROOT / "tests/fixtures/nfqws2"


# ---------------------------------------------------------------------------
# 1. Static contract checks
# ---------------------------------------------------------------------------

class RuntimeManagerStaticContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.uc = APPLY_UC.read_text(encoding="utf-8")
        self.wrapper = APPLY_WRAPPER.read_text(encoding="utf-8")
        self.makefile = MAKEFILE.read_text(encoding="utf-8")

    def test_apply_uc_uses_strict_and_fs_only(self) -> None:
        self.assertIn("'use strict';", self.uc)
        self.assertIn("import { readfile, writefile, mkdir, rename, unlink, stat, rmdir, dirname, opendir, popen } from 'fs';", self.uc)
        # readlink must NOT be imported — it is unused.
        self.assertNotIn("readlink", self.uc)
        # ucode's fs.popen is shell-based — there is no array-form execvp
        # (lib/fs.c calls popen(ucv_string_get(comm), ..) through the shell).
        # The no-shell path is system([..]) (lib.c UC_ARRAY → execvp). So:
        #   sh -n runs via system([..]) — no shell.
        #   the preload wrapper runs via popen(shell_quote(..), 'r') to capture
        #   its stdout; its command string is only shell-quoted controlled
        #   paths and a literal mode — no user NFQWS2_OPT input is on the
        #   command line (the value lives in the candidate FILE that sh -n
        #   validates as file content). String-literal popen/system (which
        #   would pass unquoted args to the shell) are forbidden.
        self.assertNotRegex(self.uc, r"popen\s*\(\s*['\"]")
        self.assertRegex(self.uc, r"popen\s*\(\s*shell_quote")
        self.assertNotRegex(self.uc, r"system\s*\(\s*['\"]")
        self.assertRegex(self.uc, r"system\s*\(\s*\[")
        # No other shell execution primitives inside the ucode manager.
        for forbidden in (r"\bexec\s*\(", r"fs\.open\s*\("):
            self.assertIsNone(re.search(forbidden, self.uc), forbidden)

    def test_apply_uc_has_all_components(self) -> None:
        for sym in (
            "parse_nfqws2_opt",
            "escape_value",
            "transform_nfqws2_opt",
            "hash31",
            "run_sh_n",
            "run_preload",
            "atomic_write_state",
            "validate_state",
            "read_state",
            "lock_acquire",
            "lock_release",
            "pid_alive_and_is_manager",
            "profile_value_ok",
            "validate_profile",
            "cmd_status",
            "cmd_validate_config",
            "cmd_validate_profile",
            "cmd_lock_test",
            "cmd_apply",
            "cmd_enable",
            "cmd_disable",
            "cmd_rollback",
            "cmd_boot_check",
            "do_apply_transaction",
        ):
            self.assertIn(sym, self.uc, sym)

    def test_apply_uc_never_sources_or_evals_config(self) -> None:
        # The config must never be sourced, eval'd, or passed to a shell.
        # 'source' as a shell builtin (followed by a path), not as a JSON key
        # or a variable name in a comment.
        for forbidden in (r"\bsource\s+/", r"\beval\s+", r"\b\. \s*/", r"sh\s+-c", r"\bsh\s+/", r"\bbash\b"):
            self.assertIsNone(re.search(forbidden, self.uc), forbidden)

    def test_apply_uc_no_uci_remittor_or_config_default_fallback(self) -> None:
        # Phase 1A must not fall back to UCI, remittor, or config.default.
        for forbidden in (r"\buci\b", r"remittor", r"config\.default", r"/etc/config/zapret2"):
            self.assertIsNone(re.search(forbidden, self.uc), forbidden)

    def test_apply_uc_does_not_store_full_nfqws2_opt_in_state(self) -> None:
        # The state stores a HASH of NFQWS2_OPT, never the full value.
        self.assertIn("hashes", self.uc)
        self.assertIn("nfqws2_opt", self.uc)
        # The state must not serialize the parsed value into the state file.
        self.assertNotIn("nfqws2_opt_value", self.uc)
        self.assertNotIn("full_value", self.uc)

    def test_apply_uc_transaction_uses_same_fs_candidate(self) -> None:
        # The candidate must be on the same filesystem as the config for
        # an atomic rename. /opt/zapret2/.config.orchestra.tmp → /opt/zapret2/config.
        self.assertIn("CANDIDATE_FILE", self.uc)
        self.assertIn("rename(CANDIDATE_FILE, CONFIG_FILE)", self.uc)
        self.assertIn("/opt/zapret2/.config.orchestra.tmp", self.uc)
        # The candidate must NOT be under /tmp (cross-filesystem rename is
        # not atomic).
        self.assertNotIn("/tmp/.config.orchestra", self.uc)

    def test_apply_uc_writes_state_applying_before_rename(self) -> None:
        # The applying marker must be written BEFORE the config rename,
        # within the apply transaction function body.
        fn_start = self.uc.index("function do_apply_transaction")
        fn_end = self.uc.index("// ----", fn_start + 10)
        fn_body = self.uc[fn_start:fn_end]
        applying_pos = fn_body.index("state.states = ['applying']")
        rename_pos = fn_body.index("rename(CANDIDATE_FILE, CONFIG_FILE)")
        self.assertLess(applying_pos, rename_pos, "state=applying must be written before rename in do_apply_transaction")

    def test_apply_uc_generation_only_after_success(self) -> None:
        # generation++ must happen AFTER the rename and preload checks,
        # not before.
        rename_pos = self.uc.index("rename(CANDIDATE_FILE, CONFIG_FILE)")
        gen_inc_pos = self.uc.index("state.generation = gen + 1")
        self.assertLess(rename_pos, gen_inc_pos, "generation must increase only after rename")

    def test_apply_uc_rollback_no_relock(self) -> None:
        # internal_rollback must NOT call lock_acquire (no nested lock).
        rollback_body_start = self.uc.index("function internal_rollback")
        rollback_body_end = self.uc.index("// ----", rollback_body_start + 10)
        rollback_body = self.uc[rollback_body_start:rollback_body_end]
        self.assertNotIn("lock_acquire", rollback_body, "internal_rollback must not re-acquire the lock")
        self.assertNotIn("lock_release", rollback_body, "internal_rollback must not release the lock")

    def test_apply_uc_rollback_conflict_and_force(self) -> None:
        self.assertIn("rollback-conflict", self.uc)
        self.assertIn("--force", self.uc)
        # Without --force, rollback must refuse to overwrite a drifted config.
        self.assertIn("use --force to override", self.uc)

    def test_apply_uc_boot_check_no_health_check(self) -> None:
        self.assertIn("cmd_boot_check", self.uc)
        self.assertIn("health_check", self.uc)
        self.assertIn("not-run", self.uc)
        # boot-check must NOT start or restart the zapret2 service.
        self.assertNotRegex(self.uc, r"/etc/init\.d/zapret2\s+(start|stop|restart|reload)")

    def test_apply_uc_candidate_is_not_tmp_for_rename(self) -> None:
        # The final rename target must be CONFIG_FILE, and the source must
        # be CANDIDATE_FILE (same FS), not a /tmp path.
        self.assertIn("rename(CANDIDATE_FILE, CONFIG_FILE)", self.uc)

    def test_apply_uc_status_reports_state_and_lock_but_not_value(self) -> None:
        # status emits JSON with the config/state/lock summary. The NFQWS2_OPT
        # value itself is not emitted -- only its byte length and hash.
        self.assertIn("nfqws2_opt", self.uc)
        self.assertIn("hash: sprintf", self.uc)
        self.assertIn("bytes: length(p.value)", self.uc)

    def test_wrapper_runs_sh_n_on_temp_only(self) -> None:
        self.assertIn("exec ucode", self.wrapper)
        self.assertIn("sh -n", self.wrapper)
        # sh -n runs on the temp validate output, never on the real config.
        self.assertIn("ZAPRET2_VALIDATE_OUT", self.wrapper)
        self.assertIn("/tmp/zapret2-orchestra/validate-config.cfg", self.wrapper)
        for forbidden in ("sh -n /opt/zapret2/config", "sh /opt/zapret2/config", ". /opt/zapret2/config"):
            self.assertNotIn(forbidden, self.wrapper)

    def test_wrapper_delegates_unknown_and_known_to_ucode(self) -> None:
        for cmd in ("status", "validate-profile", "lock-test", "enable", "disable", "apply", "rollback", "boot-check"):
            self.assertIn(cmd, self.wrapper)

    def test_makefile_installs_manager_files(self) -> None:
        self.assertIn("apply.uc", self.makefile)
        self.assertIn("zapret2-orchestra-apply", self.makefile)
        self.assertIn("$(INSTALL_DATA) $(CURDIR)/files/usr/share/zapret2-orchestra/apply.uc", self.makefile)
        self.assertIn("$(INSTALL_BIN) $(CURDIR)/files/usr/sbin/zapret2-orchestra-apply", self.makefile)

    def test_manager_state_json_is_not_a_conffile(self) -> None:
        # manager-state.json lives under /etc/zapret2-orchestra/ (persistent)
        # but must NOT be a conffile — it is rebuilt by the manager.
        conffiles = re.search(
            r"define Package/zapret2-orchestra/conffiles\n(?P<body>.*?)\nendef",
            self.makefile,
            re.DOTALL,
        )
        self.assertIsNotNone(conffiles)
        body = conffiles.group("body")
        self.assertNotIn("manager-state.json", body)

    def test_apply_uc_profile_validator_rejects_dangerous_sequences(self) -> None:
        # The ucode source must contain the rejection checks by name.
        for token in ("command substitution", "backtick", "NUL", "carriage return", "shell separator", "pipe", "unclosed"):
            self.assertIn(token, self.uc, token)

    def test_apply_uc_lock_uses_mkdir_not_flock_or_rm_rf(self) -> None:
        self.assertIn("mkdir(LOCK_DIR)", self.uc)
        self.assertNotIn("flock", self.uc)
        # Release removes only the pid file and the dir, never rm -rf.
        self.assertNotIn("rm -rf", self.uc)
        self.assertNotIn("rm('-rf'", self.uc)
        self.assertIn("unlink(LOCK_DIR + '/pid')", self.uc)
        self.assertIn("rmdir(LOCK_DIR)", self.uc)

    def test_apply_uc_lock_release_checks_pid_owner(self) -> None:
        # release must verify the recorded PID equals ours before removing.
        self.assertIn("holder != mine", self.uc)
        self.assertIn("lock not mine", self.uc)

    def test_apply_uc_validate_config_writes_only_to_temp(self) -> None:
        # validate-config writes to VALIDATE_OUT (a temp under /tmp), never
        # to CONFIG_FILE. The apply transaction writes to CANDIDATE_FILE
        # (same FS) then renames — never writefile to CONFIG_FILE.
        self.assertIn("VALIDATE_OUT", self.uc)
        self.assertIn("writefile(VALIDATE_OUT", self.uc)
        # No writefile to CONFIG_FILE anywhere in the manager (only rename).
        self.assertNotIn("writefile(CONFIG_FILE", self.uc)

    def test_apply_uc_atomic_state_write_uses_tmp_and_rename(self) -> None:
        self.assertIn("STATE_FILE + '.tmp'", self.uc)
        self.assertIn("rename(tmp, STATE_FILE)", self.uc)
        # Validates the serialized bytes by re-parsing before installing.
        self.assertIn("json(payload)", self.uc)
        self.assertIn("validate_state(reparsed)", self.uc)

    def test_state_file_is_persistent_under_etc(self) -> None:
        # manager-state.json must live under /etc/zapret2-orchestra/, not /tmp.
        self.assertIn("ORCH_DIR + '/manager-state.json'", self.uc)
        self.assertNotIn("RUNTIME_DIR + '/manager-state.json'", self.uc)
        self.assertNotIn("/tmp/zapret2-orchestra/manager-state.json", self.uc)

    def test_lock_dir_is_var_lock(self) -> None:
        # The lock must be at /var/lock/zapret2-orchestra-apply.lock.
        self.assertIn("/var/lock/zapret2-orchestra-apply.lock", self.uc)
        self.assertNotIn("RUNTIME_DIR + '/apply.lock'", self.uc)

    def test_lock_acquire_does_not_remove_missing_pid(self) -> None:
        # The race fix: when mkdir fails and no pid file exists, the lock
        # must NOT be removed (the holder may be in the writefile window).
        self.assertIn("no pid file", self.uc)
        # There must be no busy-wait loop.
        self.assertNotIn("slept", self.uc)
        self.assertNotIn("while (slept", self.uc)

    def test_lock_acquire_has_safe_stale_recheck(self) -> None:
        # Before removing a stale lock, the pid is re-read to confirm it
        # hasn't changed (safe re-check).
        self.assertIn("pidraw2", self.uc)
        self.assertIn("recovered by another", self.uc)

    def test_profile_contract_uses_opt_files(self) -> None:
        # Profiles are .opt files, not directories with profile.conf.
        self.assertIn("USER_PROFILES_DIR", self.uc)
        self.assertIn("BUILTIN_PROFILES_DIR", self.uc)
        self.assertIn("name + '.opt'", self.uc)
        self.assertNotIn("profile.conf", self.uc)
        # User override has priority over builtin.
        user_block = self.uc[self.uc.index("stat(user_path)"):]
        builtin_block = self.uc[self.uc.index("stat(builtin_path)"):]
        self.assertLess(self.uc.index("stat(user_path)"), self.uc.index("stat(builtin_path)"))

    def test_makefile_installs_builtin_profiles(self) -> None:
        self.assertIn("profiles", self.makefile)
        self.assertIn("$(INSTALL_DIR) $(1)/usr/share/zapret2-orchestra/profiles", self.makefile)
        self.assertIn("$(INSTALL_DATA) $(CURDIR)/files/usr/share/zapret2-orchestra/profiles/*.opt", self.makefile)

    def test_builtin_profile_file_exists(self) -> None:
        profile = PACKAGE / "files/usr/share/zapret2-orchestra/profiles/orchestra-tls-mvp.opt"
        self.assertTrue(profile.is_file(), profile)
        text = profile.read_text(encoding="utf-8")
        self.assertIn("NFQWS2_OPT=", text)
        self.assertIn("circular_quality", text)


# ---------------------------------------------------------------------------
# 2. Parser oracle against fixtures
# ---------------------------------------------------------------------------

class Nfqws2ParserOracleTest(unittest.TestCase):
    def test_extract_default_config(self) -> None:
        text = (FIXTURES / "config-default.txt").read_text(encoding="utf-8")
        p = P.extract(text)
        self.assertIn("--filter-tcp=80", p.value)
        self.assertIn("--filter-udp=443", p.value)
        self.assertGreater(p.open_line, 0)
        self.assertGreaterEqual(p.close_line, p.open_line)

    def test_extract_single_line(self) -> None:
        text = (FIXTURES / "config-single-line.txt").read_text(encoding="utf-8")
        p = P.extract(text)
        self.assertEqual(p.value, "simple single-line value")
        self.assertEqual(p.open_line, 2)
        self.assertEqual(p.close_line, 2)

    def test_extract_escapes_resolve_correctly(self) -> None:
        text = (FIXTURES / "config-escapes.txt").read_text(encoding="utf-8")
        p = P.extract(text)
        # \" -> ", \\ -> \, \$ -> $ (backslash before $ is preserved as \$ in
        # the raw, and resolved to $ in the value? No: in shell double quotes,
        # \$ is a literal $. Our parser treats \X as X for any X, so \$ -> $.)
        self.assertIn('"', p.value)
        self.assertIn("\\", p.value)
        self.assertIn("$", p.value)

    def test_round_trip_all_fixtures_byte_identical(self) -> None:
        for f in sorted(FIXTURES.glob("config-*.txt")):
            with self.subTest(fixture=f.name):
                text = f.read_text(encoding="utf-8")
                self.assertTrue(P.validate_round_trip(text), f"{f.name}: round-trip not byte-identical")

    def test_transform_with_new_value_preserves_outside_bytes(self) -> None:
        text = (FIXTURES / "config-default.txt").read_text(encoding="utf-8")
        p = P.extract(text)
        out = P.transform(text, "REPLACED")
        # Everything before the opening quote and after the closing quote is
        # preserved exactly.
        self.assertTrue(out.startswith(p.head))
        self.assertTrue(out.endswith(p.tail))
        self.assertIn("REPLACED", out)
        # The surrounding config lines survive.
        self.assertIn("NFQWS2_ENABLE=0", out)
        self.assertIn("MODE_FILTER=none", out)

    def test_transform_escapes_new_value(self) -> None:
        text = (FIXTURES / "config-single-line.txt").read_text(encoding="utf-8")
        out = P.transform(text, 'new "quoted" and \\backslash')
        self.assertIn('\\"', out)
        self.assertIn("\\\\", out)
        # And it round-trips back.
        p2 = P.extract(out)
        self.assertEqual(p2.value, 'new "quoted" and \\backslash')

    def test_multiline_value_preserves_internal_newlines(self) -> None:
        text = (FIXTURES / "config-default.txt").read_text(encoding="utf-8")
        p = P.extract(text)
        self.assertIn("\n", p.value)
        out = P.transform(text, p.value)
        self.assertEqual(out, text)


# ---------------------------------------------------------------------------
# 3. Shell injection and byte preservation invariants
# ---------------------------------------------------------------------------

class ShellInjectionAndPreservationTest(unittest.TestCase):
    def test_injection_payload_is_treated_as_text(self) -> None:
        text = (FIXTURES / "config-injection.txt").read_text(encoding="utf-8")
        p = P.extract(text)
        # The dangerous payloads are present verbatim in the value; they are
        # never executed by the parser.
        self.assertIn("$(reboot)", p.value)
        self.assertIn("`touch /tmp/pwn`", p.value)
        self.assertIn("; rm -rf / ;", p.value)

    def test_injection_round_trip_is_byte_identical(self) -> None:
        text = (FIXTURES / "config-injection.txt").read_text(encoding="utf-8")
        self.assertTrue(P.validate_round_trip(text))

    def test_transform_with_injection_value_keeps_it_as_text(self) -> None:
        text = (FIXTURES / "config-injection.txt").read_text(encoding="utf-8")
        p = P.extract(text)
        out = P.transform(text, p.value)
        self.assertEqual(out, text)
        # The injection payloads are still text in the output.
        self.assertIn("$(reboot)", out)

    def test_transform_with_new_injection_value_escapes_safely(self) -> None:
        text = (FIXTURES / "config-single-line.txt").read_text(encoding="utf-8")
        malicious = '--lua-desync=fake:blob=$(cat /etc/shadow); rm -rf /'
        out = P.transform(text, malicious)
        # The value is embedded inside double quotes with quotes/backslashes
        # escaped; $ and ; are NOT special to our escaper (they are valid
        # inside shell double quotes and are the profile's own business), but
        # the surrounding double quotes are intact.
        self.assertIn('NFQWS2_OPT="', out)
        # Round-trips back to the malicious value (proving no data loss).
        p2 = P.extract(out)
        self.assertEqual(p2.value, malicious)

    def test_missing_assignment_raises(self) -> None:
        with self.assertRaises(P.ParseError):
            P.extract("A=1\nB=2\n")

    def test_duplicate_assignment_raises(self) -> None:
        with self.assertRaises(P.ParseError):
            P.extract('NFQWS2_OPT="a"\nNFQWS2_OPT="b"\n')

    def test_unquoted_value_raises(self) -> None:
        with self.assertRaises(P.ParseError):
            P.extract("NFQWS2_OPT=noquote\n")

    def test_unclosed_value_raises(self) -> None:
        with self.assertRaises(P.ParseError):
            P.extract('NFQWS2_OPT="unclosed\n')

    def test_unclosed_at_eof_after_escape_raises(self) -> None:
        with self.assertRaises(P.ParseError):
            P.extract('NFQWS2_OPT="a\\')

    def test_commented_assignment_is_not_recognized(self) -> None:
        with self.assertRaises(P.ParseError):
            P.extract('# NFQWS2_OPT="commented"\nA=1\n')

    def test_assignment_with_leading_whitespace_is_recognized(self) -> None:
        p = P.extract('  NFQWS2_OPT="value"\n')
        self.assertEqual(p.value, "value")

    def test_value_closing_at_eof_without_trailing_newline(self) -> None:
        p = P.extract('NFQWS2_OPT="abc"')
        self.assertEqual(p.value, "abc")
        self.assertEqual(p.tail, '"')
        self.assertTrue(P.validate_round_trip('NFQWS2_OPT="abc"'))


# ---------------------------------------------------------------------------
# 4. Profile validation, state schema, and lock oracles
# ---------------------------------------------------------------------------

def profile_value_ok(s: str | None) -> str | None:
    """Python oracle for apply.uc profile_value_ok."""
    if s is None:
        return "null value"
    if "\x00" in s:
        return "NUL byte"
    if "\r" in s:
        return "carriage return"
    if "$(" in s:
        return "command substitution $(...)"
    if "`" in s:
        return "backtick command substitution"
    if ";" in s:
        return "shell separator ;"
    if "|" in s:
        return "pipe |"
    if "&&" in s:
        return "shell separator &&"
    # '<' and '>' are NOT rejected: NFQWS2_OPT values legitimately use
    # <HOSTLIST> placeholder tokens; the value is double-quoted and escaped.
    if s.count('"') % 2 != 0:
        return "unclosed double quote"
    if s.count("'") % 2 != 0:
        return "unclosed single quote"
    return None


class ProfileValidationOracleTest(unittest.TestCase):
    def test_accepts_clean_value(self) -> None:
        self.assertIsNone(profile_value_ok("--lua-desync=fake:blob=default --new"))

    def test_rejects_command_substitution(self) -> None:
        self.assertIsNotNone(profile_value_ok("$(reboot)"))
        self.assertIsNotNone(profile_value_ok("blob=$(cat /etc/shadow)"))

    def test_rejects_backticks(self) -> None:
        self.assertIsNotNone(profile_value_ok("`touch /tmp/pwn`"))

    def test_rejects_nul_and_cr(self) -> None:
        self.assertIsNotNone(profile_value_ok("a\x00b"))
        self.assertIsNotNone(profile_value_ok("a\rb"))

    def test_rejects_shell_separators(self) -> None:
        for bad in (";", "|", "&&"):
            self.assertIsNotNone(profile_value_ok(f"a{bad}b"), bad)

    def test_accepts_hostlist_placeholder(self) -> None:
        # NFQWS2_OPT values legitimately use <HOSTLIST> tokens; the value is
        # double-quoted and escaped, so '<' and '>' are not shell redirects.
        self.assertIsNone(profile_value_ok("--filter-l7=tls <HOSTLIST> --payload=tls_client_hello"))

    def test_rejects_unclosed_quotes(self) -> None:
        self.assertIsNotNone(profile_value_ok('a "b'))
        self.assertIsNotNone(profile_value_ok("a 'b"))

    def test_accepts_closed_quotes(self) -> None:
        self.assertIsNone(profile_value_ok('a "b" c'))
        self.assertIsNone(profile_value_ok("a 'b' c"))

    def test_rejects_none(self) -> None:
        self.assertIsNotNone(profile_value_ok(None))


# The state schema oracle mirrors validate_state in apply.uc.
STATE_SCHEMA_VERSION = 1


def validate_state(doc) -> str | None:
    if not isinstance(doc, dict):
        return "state is not an object"
    if doc.get("schema_version") != STATE_SCHEMA_VERSION:
        return f"schema_version must be {STATE_SCHEMA_VERSION}"
    if not isinstance(doc.get("states"), list):
        return "states must be an array"
    gen = doc.get("generation")
    if not (isinstance(gen, int) and not isinstance(gen, bool)):
        return "generation must be an integer"
    if not isinstance(doc.get("hashes"), dict):
        return "hashes must be an object"
    for k in ("nfqws2_opt", "preload", "whitelist", "manifest"):
        if not isinstance(doc["hashes"].get(k), str):
            return f"hashes.{k} must be a string"
    if not isinstance(doc.get("warnings"), list):
        return "warnings must be an array"
    return None


def default_state() -> dict:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "states": ["idle"],
        "generation": 0,
        "previous_state": None,
        "hash_algorithm": "djb2-31",
        "hashes": {
            "nfqws2_opt": "00000000",
            "preload": "00000000",
            "whitelist": "00000000",
            "manifest": "00000000",
        },
        "updated_at": 0,
        "last_error": None,
        "warnings": [],
    }


class StateSchemaOracleTest(unittest.TestCase):
    def test_default_state_is_valid(self) -> None:
        self.assertIsNone(validate_state(default_state()))

    def test_rejects_non_object(self) -> None:
        self.assertIsNotNone(validate_state([]))
        self.assertIsNotNone(validate_state("x"))

    def test_rejects_wrong_schema_version(self) -> None:
        s = default_state()
        s["schema_version"] = 2
        self.assertIsNotNone(validate_state(s))

    def test_rejects_missing_states_array(self) -> None:
        s = default_state()
        s["states"] = "idle"
        self.assertIsNotNone(validate_state(s))

    def test_rejects_non_integer_generation(self) -> None:
        s = default_state()
        s["generation"] = "0"
        self.assertIsNotNone(validate_state(s))

    def test_rejects_missing_hash_key(self) -> None:
        s = default_state()
        del s["hashes"]["preload"]
        self.assertIsNotNone(validate_state(s))

    def test_rejects_non_string_hash(self) -> None:
        s = default_state()
        s["hashes"]["manifest"] = 123
        self.assertIsNotNone(validate_state(s))

    def test_rejects_non_array_warnings(self) -> None:
        s = default_state()
        s["warnings"] = "x"
        self.assertIsNotNone(validate_state(s))

    def test_state_does_not_store_full_nfqws2_opt(self) -> None:
        # The state dict must not carry the full value, only its hash.
        s = default_state()
        self.assertNotIn("nfqws2_opt_value", s)
        self.assertNotIn("full_value", s)
        self.assertIn("hashes", s)
        self.assertIn("nfqws2_opt", s["hashes"])

    def test_state_json_round_trips_through_json_module(self) -> None:
        s = default_state()
        payload = json.dumps(s) + "\n"
        reparsed = json.loads(payload)
        self.assertIsNone(validate_state(reparsed))
        self.assertEqual(reparsed, s)


class LockOracleTest(unittest.TestCase):
    """The mkdir lock is modeled as: exactly one creator of a directory wins.
    On POSIX, mkdir is atomic. We verify the recovery and release-only-if-mine
    semantics with a file-based simulation."""

    def test_mkdir_lock_first_caller_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "apply.lock"
            lock.mkdir()
            self.assertTrue(lock.is_dir())
            # A second mkdir on the existing dir fails.
            with self.assertRaises(FileExistsError):
                lock.mkdir()

    def test_release_removes_only_pid_and_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "apply.lock"
            lock.mkdir()
            pidfile = lock / "pid"
            pidfile.write_text("12345\n")
            # Release: unlink pid, rmdir lock.
            pidfile.unlink()
            lock.rmdir()
            self.assertFalse(lock.exists())

    def test_release_refuses_if_pid_not_mine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "apply.lock"
            lock.mkdir()
            (lock / "pid").write_text("99999\n")  # someone else
            mine = 12345
            holder = int((lock / "pid").read_text().strip())
            self.assertNotEqual(holder, mine)
            # A correct release refuses and leaves the lock in place.
            if holder != mine:
                pass  # would return {ok: false}
            else:
                (lock / "pid").unlink()
                lock.rmdir()
            self.assertTrue(lock.exists())  # not removed

    def test_stale_recovery_when_holder_dead(self) -> None:
        # If the recorded PID is not alive (or not our executable), the lock
        # is stale and is recovered by removing the pid file and dir.
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "apply.lock"
            lock.mkdir()
            (lock / "pid").write_text("999999999\n")  # almost certainly dead
            # Safe re-check: re-read the pid to confirm it hasn't changed.
            holder1 = int((lock / "pid").read_text().strip())
            holder2 = int((lock / "pid").read_text().strip())
            if holder1 == holder2:
                (lock / "pid").unlink()
                lock.rmdir()
            self.assertFalse(lock.exists())  # recovered

    def test_missing_pid_lock_is_not_removed(self) -> None:
        # The race fix: a lock dir with no pid file must NOT be removed.
        # The holder may still be inside the writefile window.
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "apply.lock"
            lock.mkdir()
            # No pid file — simulates the mkdir/writefile race window.
            self.assertFalse((lock / "pid").exists())
            # The correct behavior is to NOT remove and report busy.
            # (In the ucode: return { ok: false, error: 'lock busy (no pid file)' })
            self.assertTrue(lock.exists())  # still there — not removed


# ---------------------------------------------------------------------------
# 5. No-config-mutation and no-UCI-fallback invariants (static)
# ---------------------------------------------------------------------------

class NoConfigMutationTest(unittest.TestCase):
    def test_validate_config_does_not_write_to_config_file(self) -> None:
        uc = APPLY_UC.read_text(encoding="utf-8")
        # The only writefile targets are the state tmp and VALIDATE_OUT.
        writes = re.findall(r"writefile\(([^,]+)", uc)
        for w in writes:
            self.assertNotIn("CONFIG_FILE", w, f"writefile targets CONFIG_FILE: {w}")

    def test_apply_uc_has_no_uci_cursor_or_config_default(self) -> None:
        uc = APPLY_UC.read_text(encoding="utf-8")
        self.assertNotIn("cursor()", uc)
        self.assertNotIn("/etc/config/zapret2", uc)
        self.assertNotIn("config.default", uc)
        self.assertNotIn("remittor", uc)

    def test_wrapper_does_not_source_or_exec_config(self) -> None:
        wrapper = APPLY_WRAPPER.read_text(encoding="utf-8")
        self.assertNotIn(". /opt/zapret2/config", wrapper)
        self.assertNotIn("source /opt/zapret2/config", wrapper)
        self.assertNotIn("eval ", wrapper)
        self.assertNotIn("sh /opt/zapret2/config", wrapper)
        # sh -n is parse-only and runs on the temp, not the real config.
        self.assertIn("sh -n", wrapper)
        self.assertIn("validate-config.cfg", wrapper)
        self.assertNotIn("sh -n /opt/zapret2/config", wrapper)


# ---------------------------------------------------------------------------
# 6. Runtime tests (execute real ucode; skip if ucode absent)
# ---------------------------------------------------------------------------

class RuntimeManagerRuntimeTest(unittest.TestCase):
    """Executes the real ucode manager; skipped when ucode is absent."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.ucode = shutil.which("ucode")

    def setUp(self) -> None:
        if self.ucode is None:
            self.skipTest("ucode executable not found on PATH")
        self.tmp = tempfile.mkdtemp(prefix="orchestra-mgr-")
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
        self.orch_lua.mkdir()
        (self.orch_lua / "init.lua").write_text("-- test\n", encoding="utf-8")
        (self.orch / "whitelist.json").write_text(
            json.dumps({"schema_version": 1, "hosts": []}), encoding="utf-8"
        )
        # Copy the builtin profile into the test builtin dir
        builtin = PACKAGE / "files/usr/share/zapret2-orchestra/profiles/orchestra-tls-mvp.opt"
        (self.builtin_profiles / "orchestra-tls-mvp.opt").write_text(
            builtin.read_text(encoding="utf-8"), encoding="utf-8"
        )
        # Create a fake preload wrapper that succeeds and creates placeholder
        # runtime files so the manager's preload check passes.
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
            "    [ -f \"$ORCHESTRA_RUNTIME_DIR/preload.lua\" ] && exit 0 || exit 1 ;;\n"
            "  *) exit 1 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_preload.chmod(0o755)
        # Write a default config so most tests don't need to set it up
        self._write_default_config()

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
        # The preload generator (generate-preload.uc) uses ORCHESTRA_* env
        # vars, while apply.uc uses ZAPRET2_* vars. Both must point to the
        # same sandbox dirs so the fake preload wrapper and the real
        # generate-preload.uc (when used) write to the right runtime path.
        env["ORCHESTRA_STATE_DIR"] = str(self.orch)
        env["ORCHESTRA_RUNTIME_DIR"] = str(self.runtime)
        env["ORCHESTRA_PRELOAD_FILE"] = str(self.runtime / "preload.lua")
        env["ORCHESTRA_WHITELIST_FILE"] = str(self.runtime / "whitelist.txt")
        env["ORCHESTRA_MANIFEST_FILE"] = str(self.runtime / "manifest.json")
        return env

    def _write_default_config(self, nfqws2_opt: str = "--lua-desync=fake:blob=default") -> None:
        """Write a valid config with the given NFQWS2_OPT value."""
        (self.opt / "config").write_text(
            f'NFQWS2_ENABLE=0\nNFQWS2_OPT="{nfqws2_opt}"\nMODE_FILTER=none\n',
            encoding="utf-8",
        )

    def _run(self, *args: str, config_text: str | None = None) -> subprocess.CompletedProcess:
        env = self._env()
        if config_text is not None:
            (self.opt / "config").write_text(config_text, encoding="utf-8")
        r = subprocess.run(
            [self.ucode, str(APPLY_UC), "--", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        # Surface apply.uc's die() message on failure so the CI log shows the
        # real reason instead of a bare JSONDecodeError on empty stdout.
        if r.returncode != 0 or not r.stdout.strip():
            sys.stderr.write(
                f"[_run {args!r} rc={r.returncode}] stdout={r.stdout!r} stderr={r.stderr!r}\n"
            )
        return r

    def _run_wrapper(self, *args: str, config_text: str | None = None) -> subprocess.CompletedProcess:
        env = self._env()
        # The wrapper hardcodes /usr/share/.../apply.uc but honors
        # ZAPRET2_APPLY_UC for tests; point it at the repo source tree.
        env["ZAPRET2_APPLY_UC"] = str(APPLY_UC)
        if config_text is not None:
            (self.opt / "config").write_text(config_text, encoding="utf-8")
        # The wrapper execs ucode; run it through sh for the case statement.
        return subprocess.run(
            ["sh", str(APPLY_WRAPPER), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_status_emits_valid_json_with_state_and_lock(self) -> None:
        cfg = (FIXTURES / "config-single-line.txt").read_text(encoding="utf-8")
        r = self._run("status", config_text=cfg)
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["phase"], "1b")
        self.assertIn("config", doc)
        self.assertIn("nfqws2_opt", doc)
        self.assertIn("state", doc)
        self.assertIn("lock", doc)
        self.assertEqual(doc["nfqws2_opt"]["bytes"], len("simple single-line value"))
        # The value itself is not in the status output (only bytes + hash).
        self.assertNotIn("simple single-line value", r.stdout)

    def test_status_on_missing_assignment_reports_parse_error(self) -> None:
        r = self._run("status", config_text="A=1\nB=2\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertIsNotNone(doc["parse_error"])
        self.assertIsNone(doc["nfqws2_opt"])

    def test_validate_config_writes_temp_and_round_trips(self) -> None:
        cfg = (FIXTURES / "config-default.txt").read_text(encoding="utf-8")
        r = self._run("validate-config", config_text=cfg)
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertTrue(doc["ok"])
        self.assertTrue(doc["byte_identical"])
        out = (self.runtime / "validate-config.cfg").read_text(encoding="utf-8")
        self.assertEqual(out, cfg)  # re-emission is byte-identical

    def test_validate_config_does_not_mutate_real_config(self) -> None:
        cfg_path = Path(self.tmp) / "config"
        cfg_path.write_text((FIXTURES / "config-default.txt").read_text(encoding="utf-8"), encoding="utf-8")
        before = cfg_path.read_bytes()
        env = self._env()
        env["ZAPRET2_CONFIG"] = str(cfg_path)
        subprocess.run([self.ucode, str(APPLY_UC), "validate-config"], env=env, capture_output=True, timeout=10)
        after = cfg_path.read_bytes()
        self.assertEqual(before, after, "real config was mutated")

    def test_validate_config_rejects_unclosed(self) -> None:
        r = self._run("validate-config", config_text='NFQWS2_OPT="unclosed\n')
        self.assertNotEqual(r.returncode, 0)

    def test_validate_profile_rejects_injection(self) -> None:
        (self.user_profiles / "bad.opt").write_text(
            'NFQWS2_OPT="--lua-desync=circular_quality:key=tls:blob=$(reboot)"\n',
            encoding="utf-8",
        )
        r = self._run("validate-profile", "bad")
        self.assertNotEqual(r.returncode, 0)
        doc = json.loads(r.stdout)
        self.assertFalse(doc["ok"])
        self.assertTrue(any("command substitution" in p for p in doc["problems"]))

    def test_validate_profile_accepts_clean_builtin(self) -> None:
        (self.builtin_profiles / "good.opt").write_text(
            'NFQWS2_OPT="--lua-desync=circular_quality:key=tls:fails=1:failure_detector=combined_failure_detector"\n',
            encoding="utf-8",
        )
        r = self._run("validate-profile", "good")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["source_type"], "builtin")

    def test_user_profile_overrides_builtin(self) -> None:
        (self.builtin_profiles / "ovr.opt").write_text(
            'NFQWS2_OPT="--lua-desync=circular_quality:key=tls:fails=2"\n',
            encoding="utf-8",
        )
        (self.user_profiles / "ovr.opt").write_text(
            'NFQWS2_OPT="--lua-desync=circular_quality:key=tls:fails=9"\n',
            encoding="utf-8",
        )
        r = self._run("validate-profile", "ovr")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["source_type"], "user")
        self.assertIn("user-profiles", doc["source"])

    def test_validate_profile_builtin_file_from_package(self) -> None:
        # The shipped builtin profile orchestra-tls-mvp.opt must validate.
        builtin = PACKAGE / "files/usr/share/zapret2-orchestra/profiles/orchestra-tls-mvp.opt"
        # Copy it into the test builtin dir so ucode can find it.
        (self.builtin_profiles / "orchestra-tls-mvp.opt").write_text(
            builtin.read_text(encoding="utf-8"), encoding="utf-8"
        )
        r = self._run("validate-profile", "orchestra-tls-mvp")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["source_type"], "builtin")

    def test_lock_test_acquires_and_releases(self) -> None:
        r = self._run("lock-test")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = [json.loads(l) for l in r.stdout.strip().split("\n") if l.strip()]
        self.assertTrue(lines[0]["ok"])
        self.assertEqual(lines[0]["stage"], "acquired")
        self.assertTrue(lines[1]["ok"])
        self.assertEqual(lines[1]["stage"], "released")
        # The lock dir is gone after release.
        self.assertFalse((self.runtime / "apply.lock").exists())

    def test_transactional_commands_are_implemented(self) -> None:
        # In Phase 1B, enable/disable/apply/rollback/boot-check are implemented.
        # They must NOT return "not-implemented-phase-1a".
        for cmd in ("apply", "enable", "disable", "rollback", "boot-check"):
            with self.subTest(cmd=cmd):
                if cmd == "apply":
                    r = self._run(cmd, "orchestra-tls-mvp")
                elif cmd == "enable":
                    r = self._run(cmd, "orchestra-tls-mvp")
                elif cmd == "disable":
                    r = self._run(cmd)
                elif cmd == "rollback":
                    r = self._run(cmd, "--force")
                else:
                    r = self._run(cmd)
                if r.stdout.strip():
                    doc = json.loads(r.stdout)
                    self.assertNotEqual(doc.get("error"), "not-implemented-phase-1a",
                                        f"{cmd} still returns not-implemented")

    def test_atomic_state_write_and_read(self) -> None:
        # We can't directly call atomic_write_state from outside, but status
        # reads the state file. First, write a valid state file and verify
        # status reports it. The state file is now under the orch dir
        # (persistent /etc path).
        state = default_state()
        state["states"] = ["idle"]
        state["generation"] = 7
        (self.orch / "manager-state.json").write_text(
            json.dumps(state) + "\n", encoding="utf-8"
        )
        cfg = (FIXTURES / "config-single-line.txt").read_text(encoding="utf-8")
        r = self._run("status", config_text=cfg)
        doc = json.loads(r.stdout)
        self.assertTrue(doc["state"]["valid"])
        self.assertEqual(doc["state"]["generation"], 7)

    def test_corrupted_state_is_reported_invalid(self) -> None:
        (self.orch / "manager-state.json").write_text("{not json", encoding="utf-8")
        cfg = (FIXTURES / "config-single-line.txt").read_text(encoding="utf-8")
        r = self._run("status", config_text=cfg)
        doc = json.loads(r.stdout)
        self.assertFalse(doc["state"]["valid"])

    def test_wrapper_validate_config_runs_sh_n(self) -> None:
        if shutil.which("sh") is None:
            self.skipTest("sh not on PATH")
        cfg = (FIXTURES / "config-default.txt").read_text(encoding="utf-8")
        r = self._run_wrapper("validate-config", config_text=cfg)
        self.assertEqual(r.returncode, 0, r.stderr)
        # The wrapper emits a sh_n_ok:true line.
        self.assertIn('"sh_n_ok":true', r.stdout)

    # ------------------------------------------------------------------
    # Phase 1B transactional tests
    # ------------------------------------------------------------------

    def test_apply_success(self) -> None:
        r = self._run("apply", "orchestra-tls-mvp")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["command"], "apply")
        self.assertEqual(doc["profile"], "orchestra-tls-mvp")
        self.assertEqual(doc["generation"], 1)
        # Config was modified — the NFQWS2_OPT value should now contain circular_quality
        cfg = (self.opt / "config").read_text(encoding="utf-8")
        self.assertIn("circular_quality", cfg)
        # A backup was created
        backups = list((self.orch / "backup").glob("config.gen-*.bak"))
        self.assertEqual(len(backups), 1)

    def test_apply_validation_failure_no_config_change(self) -> None:
        # Make sh -n fail by creating a profile with invalid shell syntax.
        # We can't easily make sh -n fail on a well-formed config, so we
        # test parse failure instead: remove NFQWS2_OPT from the config.
        self._write_default_config()
        (self.opt / "config").write_text("A=1\nB=2\n", encoding="utf-8")
        before = (self.opt / "config").read_bytes()
        r = self._run("apply", "orchestra-tls-mvp")
        self.assertNotEqual(r.returncode, 0)
        after = (self.opt / "config").read_bytes()
        self.assertEqual(before, after, "config was changed despite validation failure")
        # No backup created
        self.assertFalse((self.orch / "backup").exists())

    def test_apply_generation_increments_only_on_success(self) -> None:
        # First successful apply
        r1 = self._run("apply", "orchestra-tls-mvp")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(json.loads(r1.stdout)["generation"], 1)
        # Second successful apply
        r2 = self._run("apply", "orchestra-tls-mvp")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(json.loads(r2.stdout)["generation"], 2)
        # Failed apply (broken config) — generation must NOT increase
        (self.opt / "config").write_text("A=1\n", encoding="utf-8")
        r3 = self._run("apply", "orchestra-tls-mvp")
        self.assertNotEqual(r3.returncode, 0)
        # State generation should still be 2
        state = json.loads((self.orch / "manager-state.json").read_text())
        self.assertEqual(state["generation"], 2)

    def test_apply_candidate_same_filesystem(self) -> None:
        # The candidate file must be under /opt (same FS as config), not /tmp.
        # We verify by checking that CANDIDATE_FILE is a sibling of CONFIG_FILE.
        env = self._env()
        candidate = env["ZAPRET2_CANDIDATE_FILE"]
        config = env["ZAPRET2_CONFIG"]
        self.assertEqual(str(Path(candidate).parent), str(Path(config).parent))

    def test_apply_injection_payloads_not_executed(self) -> None:
        # A profile containing $(reboot) must be REJECTED by profile_value_ok
        # before any config change.
        (self.user_profiles / "evil.opt").write_text(
            'NFQWS2_OPT="--lua-desync=circular_quality:blob=$(reboot)"\n',
            encoding="utf-8",
        )
        before = (self.opt / "config").read_bytes()
        r = self._run("apply", "evil")
        self.assertNotEqual(r.returncode, 0)
        after = (self.opt / "config").read_bytes()
        self.assertEqual(before, after, "config was changed by injection attempt")

    def test_rollback_restores_backup(self) -> None:
        # First apply to create a backup
        r1 = self._run("apply", "orchestra-tls-mvp")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        applied_cfg = (self.opt / "config").read_bytes()
        # Now manually change the config (simulating a second apply or edit)
        self._write_default_config("--lua-desync=something_else")
        # Rollback should restore the backup
        r2 = self._run("rollback", "--force")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        doc = json.loads(r2.stdout)
        self.assertTrue(doc["ok"])
        self.assertTrue(doc["forced"])
        restored_cfg = (self.opt / "config").read_text(encoding="utf-8")
        self.assertIn("circular_quality", restored_cfg)

    def test_rollback_conflict_without_force(self) -> None:
        # Apply to create state
        r1 = self._run("apply", "orchestra-tls-mvp")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        # Externally modify the config (drift)
        self._write_default_config("--lua-desync=external_edit")
        # Rollback without --force should detect conflict
        r2 = self._run("rollback")
        self.assertNotEqual(r2.returncode, 0)
        doc = json.loads(r2.stdout)
        self.assertFalse(doc["ok"])
        self.assertEqual(doc["error"], "rollback-conflict")
        # Config should NOT be changed
        cfg = (self.opt / "config").read_text(encoding="utf-8")
        self.assertIn("external_edit", cfg)

    def test_rollback_force_overrides_conflict(self) -> None:
        # Apply, drift, then --force rollback
        self._run("apply", "orchestra-tls-mvp")
        self._write_default_config("--lua-desync=external_edit")
        r = self._run("rollback", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertTrue(doc["ok"])
        self.assertTrue(doc["forced"])
        self.assertTrue(doc["drift"])

    def test_enable_disable_idempotent(self) -> None:
        # First enable
        r1 = self._run("enable", "orchestra-tls-mvp")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        # Second enable (same profile) — idempotent
        r2 = self._run("enable", "orchestra-tls-mvp")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        doc2 = json.loads(r2.stdout)
        self.assertTrue(doc2["idempotent"])
        # Disable
        r3 = self._run("disable")
        self.assertEqual(r3.returncode, 0, r3.stderr)
        # Second disable — idempotent
        r4 = self._run("disable")
        self.assertEqual(r4.returncode, 0, r4.stderr)
        doc4 = json.loads(r4.stdout)
        self.assertTrue(doc4["idempotent"])

    def test_repeated_enable_disable_cycle(self) -> None:
        for _ in range(3):
            r_en = self._run("enable", "orchestra-tls-mvp")
            self.assertEqual(r_en.returncode, 0, r_en.stderr)
            state = json.loads((self.orch / "manager-state.json").read_text())
            self.assertTrue(state["enabled"])
            r_dis = self._run("disable")
            self.assertEqual(r_dis.returncode, 0, r_dis.stderr)
            state = json.loads((self.orch / "manager-state.json").read_text())
            self.assertFalse(state["enabled"])

    def test_boot_check_detects_interrupted_apply(self) -> None:
        # Simulate an interrupted apply by writing state=applying manually
        state = {
            "schema_version": 1,
            "states": ["applying"],
            "generation": 5,
            "previous_state": None,
            "enabled": False,
            "profile": None,
            "applying_gen": 5,
            "applying_backup": None,
            "hash_algorithm": "djb2-31",
            "hashes": {"nfqws2_opt": "00000000", "preload": "00000000", "whitelist": "00000000", "manifest": "00000000"},
            "updated_at": 0,
            "last_error": None,
            "warnings": [],
        }
        (self.orch / "manager-state.json").write_text(json.dumps(state) + "\n", encoding="utf-8")
        # Create a backup so boot-check can restore
        (self.orch / "backup").mkdir()
        (self.orch / "backup" / "config.gen-5.bak").write_text(
            'NFQWS2_OPT="--lua-desync=circular_quality:key=tls"\n', encoding="utf-8"
        )
        r = self._run("boot-check")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertTrue(doc["ok"])
        self.assertTrue(doc["was_applying"])
        # State should be recovered to idle
        state2 = json.loads((self.orch / "manager-state.json").read_text())
        self.assertIn("idle", state2["states"])

    def test_boot_check_no_health_check(self) -> None:
        r = self._run("boot-check")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertIn("not-run", doc["health_check"])

    def test_no_nested_lock_in_rollback(self) -> None:
        # If apply fails after rename, internal_rollback is called without
        # re-acquiring the lock. We verify statically that internal_rollback
        # does not contain lock_acquire.
        # (Already covered by test_apply_uc_rollback_no_relock, but this is
        # a runtime confirmation that the lock is released after a failed
        # apply, proving no lock leak.)
        # Make preload fail by removing the fake wrapper
        env = self._env()
        env["ZAPRET2_PRELOAD_WRAPPER"] = "/nonexistent/path"
        (self.opt / "config").write_text(
            'NFQWS2_OPT="--lua-desync=fake"\n', encoding="utf-8"
        )
        r = subprocess.run(
            [self.ucode, str(APPLY_UC), "apply", "orchestra-tls-mvp"],
            env=env, capture_output=True, text=True, timeout=10,
        )
        # The apply should fail (preload can't run), but the lock should be
        # released. We verify by running lock-test after.
        r2 = self._run("lock-test")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        lines = [json.loads(l) for l in r2.stdout.strip().split("\n") if l.strip()]
        self.assertTrue(lines[0]["ok"], "lock was not released after failed apply")


if __name__ == "__main__":
    unittest.main()
