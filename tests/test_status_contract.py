"""Static contract checks for the read-only rpcd/ucode status and validate methods."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "openwrt/orchestra/files/usr/share/rpcd/ucode/zapret2.orchestra"
ACL = ROOT / "openwrt/orchestra/files/usr/share/rpcd/acl.d/zapret2-orchestra.json"
class StatusContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.source = BACKEND.read_text(encoding="utf-8")
        self.acl = json.loads(ACL.read_text(encoding="utf-8"))

    def test_rpcd_object_and_status_method(self) -> None:
        self.assertRegex(self.source, r"['\"]zapret2\.orchestra['\"]:\s*\{\s*status:\s*\{")
        self.assertRegex(self.source, r"status:\s*\{\s*call:\s*function\(\)")

    def test_status_method_remains_unchanged(self) -> None:
        self.assertIn(
            "return { installed, service, nfqws2_binary, nfqws2_running, pid };",
            self.source,
        )
        self.assertIn("readlink(`/proc/${pid}/exe`) == NFQWS2 ? pid : null;", self.source)

    def test_acl_is_read_only_status_and_validate_only(self) -> None:
        self.assertEqual(set(self.acl), {"zapret2-orchestra"})
        grant = self.acl["zapret2-orchestra"]
        self.assertEqual(set(grant), {"description", "read"})
        self.assertEqual(grant["read"], {"ubus": {"zapret2.orchestra": ["status", "validate"]}})
        self.assertNotIn("write", grant)
        self.assertNotIn("exec", grant)
        self.assertNotIn("file", grant["read"])
        self.assertNotIn("uci", grant["read"])

    def test_status_uses_only_real_read_only_evidence(self) -> None:
        for path in (
            "/etc/init.d/zapret2",
            "/etc/config/zapret2",
            "/opt/zapret2/nfq2/nfqws2",
            "/var/run/nfqws2_1.pid",
        ):
            self.assertIn(path, self.source)
        self.assertIn("stat(path)", self.source)
        self.assertIn("readfile(PID_FILE)", self.source)
        self.assertIn("readlink(`/proc/${pid}/exe`)", self.source)
        self.assertIn("exists(INIT_SCRIPT) && exists(UCI_CONFIG) && nfqws2_binary", self.source)
        self.assertIn("nfqws2_running ? 'running' : 'stopped'", self.source)

    def test_contract_is_exact_and_forbidden_operations_are_absent(self) -> None:
        self.assertIn(
            "return { installed, service, nfqws2_binary, nfqws2_running, pid };",
            self.source,
        )
        forbidden = (
            r"\b(system|popen)\s*\(",
            r"\bexec\s*\(",
            r"fs\.writefile\b",
            r"fs\.open\s*\(",
            r"\buci\s+(set|commit)\b",
            r"\bnft\b",
            r"\bapk\b",
            r"active\.opt",
            r"/etc/init\.d/zapret2\s+(start|stop|restart|reload|enable|disable)",
        )
        for pattern in forbidden:
            self.assertIsNone(re.search(pattern, self.source), pattern)

    def test_validate_contract_and_all_checks_are_exact(self) -> None:
        self.assertRegex(self.source, r"validate:\s*\{\s*call:\s*function\(\)")
        self.assertIn("return { ok, checks, errors };", self.source)
        check_names = (
            "init_script",
            "uci_config",
            "nfqws2_binary",
            "lua_lib",
            "lua_antidpi",
            "lua_auto",
        )
        positions = [self.source.index(f"{name}:") for name in check_names]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("let ok = checks.init_script && checks.uci_config && checks.nfqws2_binary &&", self.source)
        self.assertIn("checks.lua_lib && checks.lua_antidpi && checks.lua_auto;", self.source)

    def test_validate_error_order_is_fixed(self) -> None:
        error_codes = (
            "missing_init_script",
            "missing_uci_config",
            "missing_nfqws2_binary",
            "missing_lua_lib",
            "missing_lua_antidpi",
            "missing_lua_auto",
        )
        positions = [self.source.index(f"push(errors, '{code}')") for code in error_codes]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("let errors = [];", self.source)
        self.assertNotIn("errors.push(", self.source)

    def test_validate_uses_read_only_filesystem_and_uci_reads(self) -> None:
        for path in (
            "/opt/zapret2/lua/zapret-lib.lua",
            "/opt/zapret2/lua/zapret-antidpi.lua",
            "/opt/zapret2/lua/zapret-auto.lua",
        ):
            self.assertIn(path, self.source)
        self.assertIn("return stat(path)?.type == 'file';", self.source)
        self.assertIn("cursor().get_all('zapret2', 'config')", self.source)
        self.assertNotIn("cursor().get('zapret2', 'config')", self.source)
        self.assertIn("push(errors,", self.source)
        self.assertNotIn("errors.push(", self.source)
        for option in ("FWTYPE", "MODE_FILTER", "NFQWS2_ENABLE", "NFQWS2_OPT"):
            self.assertIn(f"config.{option} != null", self.source)

    def test_validate_ok_is_false_when_any_check_is_missing(self) -> None:
        checks = (
            "init_script",
            "uci_config",
            "nfqws2_binary",
            "lua_lib",
            "lua_antidpi",
            "lua_auto",
        )
        for check in checks:
            self.assertIn(f"if (!checks.{check})", self.source)
        self.assertIn(
            "let ok = checks.init_script && checks.uci_config && checks.nfqws2_binary &&",
            self.source,
        )

    def test_saved_stopped_fixture_contract(self) -> None:
        expected = {
            "installed": True,
            "service": "stopped",
            "nfqws2_binary": True,
            "nfqws2_running": False,
            "pid": None,
        }
        self.assertEqual(set(expected), {"installed", "service", "nfqws2_binary", "nfqws2_running", "pid"})
        self.assertTrue(expected["installed"])
        self.assertEqual(expected["service"], "stopped")
        self.assertTrue(expected["nfqws2_binary"])
        self.assertFalse(expected["nfqws2_running"])
        self.assertIsNone(expected["pid"])

    @unittest.skip("NOT RUN: target ucode executable and saved postinstall fixture are unavailable locally")
    def test_dynamic_ucode_fixture(self) -> None:
        self.fail("Dynamic target-fixture execution requires a target OpenWrt environment.")

    @unittest.skip("NOT RUN: local target ucode is unavailable")
    def test_dynamic_validate_ucode(self) -> None:
        self.fail("Dynamic validate execution requires a target OpenWrt environment.")


if __name__ == "__main__":
    unittest.main()
