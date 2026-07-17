"""Static contract checks for the minimal OpenWrt package."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "openwrt" / "zapret2-orchestra"
MAKEFILE = PACKAGE / "Makefile"
BACKEND = PACKAGE / "files/usr/share/rpcd/ucode/zapret2.orchestra"
ACL = PACKAGE / "files/usr/share/rpcd/acl.d/zapret2-orchestra.json"


class PackageContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.makefile = MAKEFILE.read_text(encoding="utf-8")

    def test_package_metadata_is_exact(self) -> None:
        self.assertTrue(MAKEFILE.is_file())
        expected = {
            "PKG_NAME": "zapret2-orchestra",
            "PKG_VERSION": "0.1.0",
            "PKG_RELEASE": "1",
            "PKGARCH": "all",
        }
        for key, value in expected.items():
            self.assertRegex(self.makefile, rf"(?m)^{key}:={re.escape(value)}$")

    def test_dependencies_are_exact(self) -> None:
        package = re.search(
            r"define Package/zapret2-orchestra\n(?P<body>.*?)\nendef",
            self.makefile,
            re.DOTALL,
        )
        self.assertIsNotNone(package)
        dependencies = re.findall(r"\+([a-z0-9-]+)", package.group("body"))
        self.assertEqual(
            dependencies,
            ["zapret2", "rpcd", "rpcd-mod-ucode", "ucode", "ucode-mod-fs", "ucode-mod-uci"],
        )

    def test_only_backend_and_acl_are_installed(self) -> None:
        sources = re.findall(r"\$\(CURDIR\)/files(/usr/share/rpcd/[^\s]+)", self.makefile)
        self.assertEqual(
            sources,
            [
                "/usr/share/rpcd/ucode/zapret2.orchestra",
                "/usr/share/rpcd/acl.d/zapret2-orchestra.json",
            ],
        )
        self.assertIn("$(INSTALL_BIN)", self.makefile)
        self.assertIn("$(INSTALL_DATA)", self.makefile)

    def test_lifecycle_only_restarts_rpcd_on_target(self) -> None:
        self.assertEqual(
            len(re.findall(r"^define Package/zapret2-orchestra/post(?:inst|rm)$", self.makefile, re.MULTILINE)),
            2,
        )
        self.assertEqual(self.makefile.count("/etc/init.d/rpcd restart"), 2)
        self.assertEqual(self.makefile.count("exit 0"), 2)
        self.assertEqual(self.makefile.count('[ -x /etc/init.d/rpcd ]'), 2)
        for forbidden in (
            r"/etc/init\.d/zapret2",
            r"\buci\s+(set|add|delete|rename|commit)\b",
            r"\b(firewall|nft|fw3|fw4)\b",
        ):
            self.assertIsNone(re.search(forbidden, self.makefile, re.IGNORECASE), forbidden)
        self.assertNotIn("python", self.makefile.lower())
        self.assertNotIn("lua", self.makefile.lower())

    def test_backend_and_acl_remain_read_only(self) -> None:
        source = BACKEND.read_text(encoding="utf-8")
        methods = re.findall(r"^\t\t([a-z_]+): \{\n\t\t\tcall: function", source, re.MULTILINE)
        self.assertEqual(methods, ["status", "validate"])
        acl = json.loads(ACL.read_text(encoding="utf-8"))
        self.assertEqual(acl["zapret2-orchestra"]["read"]["ubus"]["zapret2.orchestra"], ["status", "validate"])

    def test_obsolete_paths_and_accidental_file_are_absent(self) -> None:
        self.assertFalse((ROOT / "openwrt" / "orchestra").exists())
        self.assertFalse((ROOT / "zapret-gui").exists())
        self.assertEqual(list(ROOT.glob("tra port*")), [])


if __name__ == "__main__":
    unittest.main()
