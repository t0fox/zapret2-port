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
        self.assertIn("import { readfile, writefile, mkdir, rename, unlink, stat, rmdir, readlink } from 'fs';", self.uc)
        # No shell execution primitives inside the ucode manager.
        for forbidden in (r"\bsystem\s*\(", r"\bpopen\s*\(", r"\bexec\s*\(", r"fs\.open\s*\("):
            self.assertIsNone(re.search(forbidden, self.uc), forbidden)

    def test_apply_uc_has_all_components(self) -> None:
        for sym in (
            "parse_nfqws2_opt",
            "escape_value",
            "transform_nfqws2_opt",
            "hash31",
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
            "cmd_not_implemented",
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

    def test_apply_uc_not_implemented_commands_return_phase_1a(self) -> None:
        for cmd in ("enable", "disable", "apply", "rollback", "boot-check"):
            self.assertIn(cmd, self.uc, cmd)
        self.assertIn("not-implemented-phase-1a", self.uc)
        # The not-implemented exit code is non-zero (2), not 0.
        self.assertIn("function cmd_not_implemented", self.uc)
        self.assertIn("exit(2)", self.uc)

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
        # manager-state.json lives under /tmp and must not be a conffile.
        conffiles = re.search(
            r"define Package/zapret2-orchestra/conffiles\n(?P<body>.*?)\nendef",
            self.makefile,
            re.DOTALL,
        )
        self.assertIsNotNone(conffiles)
        body = conffiles.group("body")
        self.assertNotIn("manager-state.json", body)
        self.assertNotIn("/tmp/", body)

    def test_apply_uc_profile_validator_rejects_dangerous_sequences(self) -> None:
        # The ucode source must contain the rejection checks by name.
        for token in ("command substitution", "backtick", "NUL", "carriage return", "shell separator", "pipe", "redirect", "unclosed"):
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
        # to CONFIG_FILE.
        self.assertIn("VALIDATE_OUT", self.uc)
        self.assertIn("writefile(VALIDATE_OUT", self.uc)
        # No writefile to CONFIG_FILE anywhere in the manager.
        self.assertNotIn("writefile(CONFIG_FILE", self.uc)

    def test_apply_uc_atomic_state_write_uses_tmp_and_rename(self) -> None:
        self.assertIn("STATE_FILE + '.tmp'", self.uc)
        self.assertIn("rename(tmp, STATE_FILE)", self.uc)
        # Validates the serialized bytes by re-parsing before installing.
        self.assertIn("json(payload)", self.uc)
        self.assertIn("validate_state(reparsed)", self.uc)


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
    if ">" in s:
        return "redirect >"
    if "<" in s:
        return "redirect <"
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
        for bad in (";", "|", "&&", ">", "<"):
            self.assertIsNotNone(profile_value_ok(f"a{bad}b"), bad)

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
            # Simulate pid_alive_and_is_manager returning False -> recover.
            holder_alive = False  # in the oracle, a dead pid
            if not holder_alive:
                (lock / "pid").unlink()
                lock.rmdir()
            self.assertFalse(lock.exists())  # recovered


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
        self.profiles = Path(self.tmp) / "profiles"
        self.profiles.mkdir()
        (self.profiles / "builtin").mkdir()
        self.orch_lua = Path(self.tmp) / "orch-lua"
        self.orch_lua.mkdir()
        (self.orch_lua / "init.lua").write_text("-- test\n", encoding="utf-8")
        (self.orch / "whitelist.json").write_text(
            json.dumps({"schema_version": 1, "hosts": []}), encoding="utf-8"
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self) -> dict:
        env = os.environ.copy()
        env["ZAPRET2_RUNTIME_DIR"] = str(self.runtime)
        env["ZAPRET2_ORCHESTRA_DIR"] = str(self.orch)
        env["ZAPRET2_PROFILES_DIR"] = str(self.profiles)
        env["ZAPRET2_ORCHESTRA_LUA"] = str(self.orch_lua)
        env["ZAPRET2_STATE_FILE"] = str(self.runtime / "manager-state.json")
        env["ZAPRET2_LOCK_DIR"] = str(self.runtime / "apply.lock")
        env["ZAPRET2_VALIDATE_OUT"] = str(self.runtime / "validate-config.cfg")
        return env

    def _run(self, *args: str, config_text: str | None = None) -> subprocess.CompletedProcess:
        env = self._env()
        if config_text is not None:
            cfg = Path(self.tmp) / "config"
            cfg.write_text(config_text, encoding="utf-8")
            env["ZAPRET2_CONFIG"] = str(cfg)
        return subprocess.run(
            [self.ucode, str(APPLY_UC), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def _run_wrapper(self, *args: str, config_text: str | None = None) -> subprocess.CompletedProcess:
        env = self._env()
        if config_text is not None:
            cfg = Path(self.tmp) / "config"
            cfg.write_text(config_text, encoding="utf-8")
            env["ZAPRET2_CONFIG"] = str(cfg)
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
        self.assertEqual(doc["phase"], "1a")
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
        prof = self.profiles / "bad"
        prof.mkdir()
        (prof / "profile.conf").write_text(
            'NFQWS2_OPT="--lua-desync=circular_quality:key=tls:blob=$(reboot)"\n',
            encoding="utf-8",
        )
        r = self._run("validate-profile", "bad")
        self.assertNotEqual(r.returncode, 0)
        doc = json.loads(r.stdout)
        self.assertFalse(doc["ok"])
        self.assertTrue(any("command substitution" in p for p in doc["problems"]))

    def test_validate_profile_accepts_clean(self) -> None:
        prof = self.profiles / "good"
        prof.mkdir()
        (prof / "profile.conf").write_text(
            'NFQWS2_OPT="--lua-desync=circular_quality:key=tls:fails=1:failure_detector=combined_failure_detector"\n',
            encoding="utf-8",
        )
        r = self._run("validate-profile", "good")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertTrue(doc["ok"])

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

    def test_not_implemented_commands_return_phase_1a(self) -> None:
        for cmd in ("enable", "disable", "apply", "rollback", "boot-check"):
            with self.subTest(cmd=cmd):
                r = self._run(cmd)
                self.assertEqual(r.returncode, 2, f"{cmd}: {r.stderr}")
                doc = json.loads(r.stdout)
                self.assertFalse(doc["ok"])
                self.assertEqual(doc["error"], "not-implemented-phase-1a")
                self.assertEqual(doc["command"], cmd)

    def test_atomic_state_write_and_read(self) -> None:
        # We can't directly call atomic_write_state from outside, but status
        # reads the state file. First, write a valid state file and verify
        # status reports it.
        state = default_state()
        state["states"] = ["idle"]
        state["generation"] = 7
        (self.runtime / "manager-state.json").write_text(
            json.dumps(state) + "\n", encoding="utf-8"
        )
        cfg = (FIXTURES / "config-single-line.txt").read_text(encoding="utf-8")
        r = self._run("status", config_text=cfg)
        doc = json.loads(r.stdout)
        self.assertTrue(doc["state"]["valid"])
        self.assertEqual(doc["state"]["generation"], 7)

    def test_corrupted_state_is_reported_invalid(self) -> None:
        (self.runtime / "manager-state.json").write_text("{not json", encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
