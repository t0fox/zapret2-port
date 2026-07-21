"""Static contract checks for the OpenWrt package manifest."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "openwrt" / "zapret2-orchestra"
MAKEFILE = PACKAGE / "Makefile"
FILES = PACKAGE / "files"
BACKEND = FILES / "usr/share/rpcd/ucode/zapret2.orchestra"
ACL = FILES / "usr/share/rpcd/acl.d/zapret2-orchestra.json"
GENERATOR = FILES / "usr/share/zapret2-orchestra/generate-preload.uc"
WRAPPER = FILES / "usr/sbin/zapret2-orchestra-preload"
INIT = FILES / "etc/init.d/zapret2-orchestra"
PKG_LUA = FILES / "opt/zapret2/lua/orchestra-extra"
PKG_STATE = FILES / "etc/zapret2-orchestra"

# Canonical development copies used by the runtime-behavior tests.
DEV_LUA = ROOT / "lua" / "orchestra-extra"
DEV_STATE = ROOT / "etc" / "zapret2-orchestra"

EXPECTED_LUA = ("init.lua", "slm.lua", "slm-adapter.lua", "events.lua", "detectors.lua", "orchestrator.lua")
EXPECTED_JSON = ("blocked.json", "learned.json", "manual-locks.json", "whitelist.json")


class PackageContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.makefile = MAKEFILE.read_text(encoding="utf-8")

    def test_package_metadata_is_exact(self) -> None:
        self.assertTrue(MAKEFILE.is_file())
        expected = {
            "PKG_NAME": "zapret2-orchestra",
            "PKG_VERSION": "0.1.0",
            "PKG_RELEASE": "3",
            "PKGARCH": "all",
        }
        for key, value in expected.items():
            self.assertRegex(self.makefile, rf"(?m)^{key}:={re.escape(value)}$")

    def test_dependencies_are_exact_and_no_lua_package_dependency(self) -> None:
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
        self.assertNotIn("+lua", self.makefile)

    def test_makefile_has_no_relative_escape_paths_or_absolute_paths(self) -> None:
        self.assertNotIn("../../", self.makefile)
        self.assertNotIn("LUA_SRC", self.makefile)
        self.assertNotIn("STATE_SRC", self.makefile)
        install_block = re.search(
            r"define Package/zapret2-orchestra/install\n(?P<body>.*?)\nendef",
            self.makefile,
            re.DOTALL,
        )
        self.assertIsNotNone(install_block)
        for line in install_block.group("body").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "$(" in stripped:
                continue
            self.assertFalse(
                re.match(r"^/(etc|opt|usr|var)/", stripped),
                f"install references absolute path: {line}",
            )

    def test_all_runtime_files_are_installed_from_package_files_tree(self) -> None:
        install_block = re.search(
            r"define Package/zapret2-orchestra/install\n(?P<body>.*?)\nendef",
            self.makefile,
            re.DOTALL,
        )
        self.assertIsNotNone(install_block)
        body = install_block.group("body")

        expected_install_lines = [
            ("$(INSTALL_BIN)", "files/usr/share/rpcd/ucode/zapret2.orchestra", "$(1)/usr/share/rpcd/ucode/zapret2.orchestra"),
            ("$(INSTALL_DATA)", "files/usr/share/rpcd/acl.d/zapret2-orchestra.json", "$(1)/usr/share/rpcd/acl.d/zapret2-orchestra.json"),
            ("$(INSTALL_DATA)", "files/usr/share/zapret2-orchestra/generate-preload.uc", "$(1)/usr/share/zapret2-orchestra/generate-preload.uc"),
            ("$(INSTALL_BIN)", "files/usr/sbin/zapret2-orchestra-preload", "$(1)/usr/sbin/zapret2-orchestra-preload"),
            ("$(INSTALL_BIN)", "files/etc/init.d/zapret2-orchestra", "$(1)/etc/init.d/zapret2-orchestra"),
        ]
        for cmd, src, dst in expected_install_lines:
            self.assertIn(f"{cmd} $(CURDIR)/{src} {dst}", body)

        self.assertIn("$(INSTALL_DIR) $(1)/opt/zapret2/lua/orchestra-extra", body)
        self.assertIn("$(INSTALL_DATA) $(CURDIR)/files/opt/zapret2/lua/orchestra-extra/*.lua $(1)/opt/zapret2/lua/orchestra-extra/", body)
        self.assertIn("$(INSTALL_DIR) $(1)/etc/zapret2-orchestra", body)
        self.assertIn("$(INSTALL_DATA) $(CURDIR)/files/etc/zapret2-orchestra/*.json $(1)/etc/zapret2-orchestra/", body)

    def test_json_seeds_are_listed_as_conffiles(self) -> None:
        conffiles = re.search(
            r"define Package/zapret2-orchestra/conffiles\n(?P<body>.*?)\nendef",
            self.makefile,
            re.DOTALL,
        )
        self.assertIsNotNone(conffiles)
        body = conffiles.group("body")
        for name in EXPECTED_JSON:
            self.assertIn(f"/etc/zapret2-orchestra/{name}", body)

    def test_installed_source_files_exist_inside_package_tree(self) -> None:
        self.assertTrue(BACKEND.is_file(), BACKEND)
        self.assertTrue(ACL.is_file(), ACL)
        self.assertTrue(GENERATOR.is_file(), GENERATOR)
        self.assertTrue(WRAPPER.is_file(), WRAPPER)
        self.assertTrue(INIT.is_file(), INIT)
        for name in EXPECTED_LUA:
            self.assertTrue((PKG_LUA / name).is_file(), PKG_LUA / name)
        for name in EXPECTED_JSON:
            self.assertTrue((PKG_STATE / name).is_file(), PKG_STATE / name)

    def test_packaged_lua_and_json_match_repository_dev_copies(self) -> None:
        for name in EXPECTED_LUA:
            self.assertEqual(
                (PKG_LUA / name).read_bytes(),
                (DEV_LUA / name).read_bytes(),
                f"divergence: files/opt/zapret2/lua/orchestra-extra/{name} != lua/orchestra-extra/{name}",
            )
        for name in EXPECTED_JSON:
            self.assertEqual(
                (PKG_STATE / name).read_bytes(),
                (DEV_STATE / name).read_bytes(),
                f"divergence: files/etc/zapret2-orchestra/{name} != etc/zapret2-orchestra/{name}",
            )

    def test_lifecycle_guards_ipkg_instroot_and_only_manages_orchestra_init_and_rpcd(self) -> None:
        self.assertEqual(
            len(re.findall(r"^define Package/zapret2-orchestra/post(?:inst|rm)$", self.makefile, re.MULTILINE)),
            2,
        )
        for hook in ("postinst", "postrm"):
            block = re.search(
                rf"define Package/zapret2-orchestra/{hook}\n#!/bin/sh\n(?P<body>.*?)\nendef",
                self.makefile,
                re.DOTALL,
            )
            self.assertIsNotNone(block, hook)
            body = block.group("body")
            self.assertIn('${IPKG_INSTROOT}', body)
            self.assertTrue(body.strip().startswith('if [ -z "$${IPKG_INSTROOT}" ]'))
        self.assertEqual(self.makefile.count("/etc/init.d/rpcd restart"), 2)
        self.assertEqual(self.makefile.count("exit 0"), 2)
        self.assertEqual(self.makefile.count("[ -x /etc/init.d/rpcd ]"), 2)
        self.assertIn("/etc/init.d/zapret2-orchestra enable", self.makefile)
        self.assertIn("/etc/init.d/zapret2-orchestra disable", self.makefile)
        self.assertIn("/usr/sbin/zapret2-orchestra-preload", self.makefile)
        for forbidden in (
            r"/etc/init\.d/zapret2\s+(start|stop|restart|reload|enable|disable)",
            r"\buci\s+(set|add|delete|rename|commit)\b",
            r"\b(firewall|nft|fw3|fw4)\b",
            r"\bapk\b",
        ):
            self.assertIsNone(re.search(forbidden, self.makefile, re.IGNORECASE), forbidden)
        self.assertNotIn("python", self.makefile.lower())

    def test_backend_and_acl_remain_read_only(self) -> None:
        source = BACKEND.read_text(encoding="utf-8")
        methods = re.findall(r"^\t\t([a-z_]+): \{\n\t\t\tcall: function", source, re.MULTILINE)
        self.assertEqual(methods, ["status", "validate"])
        acl = json.loads(ACL.read_text(encoding="utf-8"))
        self.assertEqual(acl["zapret2-orchestra"]["read"]["ubus"]["zapret2.orchestra"], ["status", "validate"])

    def test_generator_supports_generate_and_check_modes_and_manifest(self) -> None:
        gen = GENERATOR.read_text(encoding="utf-8")
        self.assertIn("'use strict';", gen)
        self.assertIn("import { readfile, writefile, mkdir, rename, unlink, stat } from 'fs';", gen)
        for seed in ("blocked.json", "learned.json", "manual-locks.json", "whitelist.json"):
            self.assertIn(f"read_seed('{seed}')", gen)
        self.assertIn("schema_version", gen)
        self.assertIn("atomic_write", gen)
        self.assertIn("slm_preload_blocked", gen)
        self.assertIn("slm_preload_locked", gen)
        self.assertIn("slm_preload_history", gen)
        self.assertIn("ORCHESTRA_WHITELIST", gen)
        self.assertIn("MANIFEST_FILE", gen)
        self.assertIn("write_manifest", gen)
        self.assertIn("hash31", gen)
        self.assertIn("ARGV[0]", gen)
        self.assertIn("mode == 'generate'", gen)
        self.assertIn("mode == 'check'", gen)

    def test_wrapper_passes_arguments_through(self) -> None:
        wrapper = WRAPPER.read_text(encoding="utf-8")
        self.assertIn("exec ucode /usr/share/zapret2-orchestra/generate-preload.uc", wrapper)
        self.assertIn('"$@"', wrapper)

    def test_boot_hook_runs_before_zapret2_and_is_backup_only(self) -> None:
        init = INIT.read_text(encoding="utf-8")
        self.assertIn("#!/bin/sh /etc/rc.common", init)
        self.assertRegex(init, r"(?m)^START=20\s*$")
        self.assertIn("/usr/sbin/zapret2-orchestra-preload", init)
        body_lines = [
            line.strip()
            for line in init.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        body = "\n".join(body_lines)
        self.assertNotIn("/etc/init.d/zapret2", body)
        self.assertNotIn("nfqws2", body)
        self.assertNotIn("uci ", body)

    def test_obsolete_paths_and_accidental_file_are_absent(self) -> None:
        self.assertFalse((ROOT / "openwrt" / "orchestra").exists())
        self.assertFalse((ROOT / "zapret-gui").exists())
        self.assertEqual(list(ROOT.glob("tra port*")), [])


class Zapret2PackageContractTest(unittest.TestCase):
    """Contract checks for the zapret2 target-arch package.

    Builds from official bol-van/zapret2 source at pinned SHA via PKG_SOURCE,
    not from a local submodule path. Cross-compiles nfqws2, ip2net, mdig.
    """

    Z2 = ROOT / "openwrt" / "zapret2"
    Z2_MAKEFILE = Z2 / "Makefile"

    def setUp(self) -> None:
        self.makefile = self.Z2_MAKEFILE.read_text(encoding="utf-8")

    def test_package_metadata(self) -> None:
        self.assertTrue(self.Z2_MAKEFILE.is_file())
        for key, value in {
            "PKG_NAME": "zapret2",
            "PKG_VERSION": "0.9.20260307",
            "PKG_RELEASE": "1",
        }.items():
            self.assertRegex(self.makefile, rf"(?m)^{key}:={re.escape(value)}$")

    def test_uses_standard_source_contract(self) -> None:
        self.assertRegex(self.makefile, r"(?m)^PKG_SOURCE_PROTO:=git$")
        self.assertRegex(self.makefile, r"(?m)^PKG_SOURCE_URL:=https://github\.com/bol-van/zapret2\.git$")
        self.assertRegex(
            self.makefile,
            r"(?m)^PKG_SOURCE_VERSION:=8a0f53f3cf2c92ddeaa66995ee63a35c1210c410$",
        )

    def test_is_target_arch_not_all(self) -> None:
        self.assertNotIn("PKGARCH:=all", self.makefile)
        self.assertNotIn("PKGARCH", self.makefile)

    def test_no_submodule_path_dependency(self) -> None:
        self.assertNotIn("../../zapret2-core", self.makefile)
        self.assertNotIn("ZAPRET2_CORE_DIR", self.makefile)
        self.assertNotIn("$(CURDIR)/../../", self.makefile)

    def test_build_compile_is_not_empty(self) -> None:
        compile_block = re.search(
            r"define Build/Compile\n(?P<body>.*?)\n?endef",
            self.makefile,
            re.DOTALL,
        )
        self.assertIsNotNone(compile_block)
        body = compile_block.group("body").strip()
        self.assertNotEqual(body, "", "Build/Compile must compile binaries")
        self.assertIn("$(PKG_BUILD_DIR)/nfq2", body)
        self.assertIn("$(PKG_BUILD_DIR)/ip2net", body)
        self.assertIn("$(PKG_BUILD_DIR)/mdig", body)

    def test_build_compiles_nfqws2_with_lua(self) -> None:
        compile_block = re.search(
            r"define Build/Compile\n(?P<body>.*?)\n?endef",
            self.makefile,
            re.DOTALL,
        )
        body = compile_block.group("body")
        self.assertIn("LUA_CFLAGS", body)
        self.assertIn("LUA_LIB", body)
        self.assertIn("TARGET_CONFIGURE_OPTS", self.makefile)

    def test_install_contains_nfqws2_binary(self) -> None:
        install_block = re.search(
            r"define Package/zapret2/install\n(?P<body>.*?)\nendef",
            self.makefile,
            re.DOTALL,
        )
        self.assertIsNotNone(install_block)
        body = install_block.group("body")
        self.assertIn("$(PKG_BUILD_DIR)/nfq2/nfqws2", body)
        self.assertIn("$(1)/opt/zapret2/nfq2/nfqws2", body)
        self.assertIn("$(PKG_BUILD_DIR)/ip2net/ip2net", body)
        self.assertIn("$(PKG_BUILD_DIR)/mdig/mdig", body)

    def test_install_uses_pkg_build_dir_not_curdir_files(self) -> None:
        install_block = re.search(
            r"define Package/zapret2/install\n(?P<body>.*?)\nendef",
            self.makefile,
            re.DOTALL,
        )
        body = install_block.group("body")
        self.assertIn("$(PKG_BUILD_DIR)", body)
        self.assertNotIn("$(CURDIR)/files", body)

    def test_no_manually_copied_files_directory(self) -> None:
        self.assertFalse(
            (self.Z2 / "files").is_dir(),
            "openwrt/zapret2/files/ must not exist — source downloaded via PKG_SOURCE",
        )

    def test_no_ipk_or_opkg_in_makefile(self) -> None:
        self.assertNotIn(".ipk", self.makefile)
        self.assertNotRegex(self.makefile, r"\bopkg\b")

    def test_conffiles_lists_config(self) -> None:
        conffiles = re.search(
            r"define Package/zapret2/conffiles\n(?P<body>.*?)\nendef",
            self.makefile,
            re.DOTALL,
        )
        self.assertIsNotNone(conffiles)
        self.assertIn("/opt/zapret2/config", conffiles.group("body"))

    def test_install_path_is_opt_zapret2(self) -> None:
        install_block = re.search(
            r"define Package/zapret2/install\n(?P<body>.*?)\nendef",
            self.makefile,
            re.DOTALL,
        )
        body = install_block.group("body")
        self.assertIn("/opt/zapret2/", body)
        self.assertNotIn("/opt/zapret/", body)

    def test_dependencies_include_build_libs_and_lua(self) -> None:
        for dep in ("+libnetfilter-queue", "+libmnl", "+libcap", "+zlib"):
            self.assertIn(dep, self.makefile)
        self.assertIn("LUA_DEP", self.makefile)


class ApkFormatContractTest(unittest.TestCase):
    """Verify docs and tests do not reference the obsolete IPK/opkg format."""

    def test_docs_do_not_reference_ipk_or_opkg_as_current_format(self) -> None:
        for doc in (ROOT / "docs").glob("*.md"):
            text = doc.read_text(encoding="utf-8")
            for forbidden in (r"\bIPK\b", r"\.ipk\b", r"`opkg files`", r"`opkg info`"):
                self.assertIsNone(
                    re.search(forbidden, text),
                    f"{doc.name}: obsolete IPK/opkg reference: {forbidden}",
                )

    def test_workflow_targets_apk_not_ipk(self) -> None:
        wf = ROOT / ".github" / "workflows" / "build-apk.yml"
        self.assertTrue(wf.is_file(), "build-apk.yml workflow not found")
        text = wf.read_text(encoding="utf-8")
        self.assertIn(".apk", text)
        self.assertNotIn(".ipk", text)
        self.assertNotIn("opkg", text)
        self.assertIn("workflow_dispatch", text)
        self.assertIn("sha256sum", text)
        self.assertIn("actions/cache@v", text)
        self.assertIn("-j$(nproc)", text)
        self.assertIn("V=sc", text)

    def test_workflow_does_not_copy_zapret2_core(self) -> None:
        wf = ROOT / ".github" / "workflows" / "build-apk.yml"
        text = wf.read_text(encoding="utf-8")
        self.assertNotIn("$SDK_DIR/zapret2-core", text)
        self.assertNotIn("cp -r $GITHUB_WORKSPACE/zapret2-core", text)

    def test_workflow_builds_both_packages(self) -> None:
        wf = ROOT / ".github" / "workflows" / "build-apk.yml"
        text = wf.read_text(encoding="utf-8")
        self.assertIn("package/zapret2/compile", text)
        self.assertIn("package/zapret2-orchestra/compile", text)


if __name__ == "__main__":
    unittest.main()
