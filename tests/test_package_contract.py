"""Static contract checks for the OpenWrt package manifest."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
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
            "PKG_RELEASE": "4",
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
            "PKG_RELEASE": "2",
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

    def test_ipset_scripts_are_installed_executable(self) -> None:
        """create_ipset.sh is invoked directly by the firewall include
        (functions: ``"$IPSET_CR" "$@"``) and the get_*.sh / clear_lists.sh
        scripts are executed directly by cron/systemd. Upstream marks them
        0755; the Makefile must use INSTALL_BIN (0755), not INSTALL_DATA
        (0644), or the post-install firewall reload fails with
        "create_ipset.sh: Permission denied" (router blocker)."""
        install_block = re.search(
            r"define Package/zapret2/install\n(?P<body>.*?)\nendef",
            self.makefile,
            re.DOTALL,
        )
        self.assertIsNotNone(install_block)
        body = install_block.group("body")
        # The executable ipset scripts (*.sh) must be INSTALL_BIN.
        self.assertIn("$(INSTALL_BIN) $(PKG_BUILD_DIR)/ipset/*.sh", body)
        self.assertIn("$(1)/opt/zapret2/ipset/", body)
        # The old broken install that used INSTALL_DATA for everything is gone.
        self.assertNotIn(
            "$(INSTALL_DATA) $(PKG_BUILD_DIR)/ipset/* $(1)/opt/zapret2/ipset/",
            body,
        )
        # def.sh is sourced (no shebang, upstream 0644) — must NOT be executable.
        self.assertIn("$(INSTALL_DATA) $(PKG_BUILD_DIR)/ipset/def.sh", body)
        # antifilter.helper is a data helper — must be INSTALL_DATA.
        self.assertIn(
            "$(INSTALL_DATA) $(PKG_BUILD_DIR)/ipset/antifilter.helper", body
        )


class Blockcheck2PackageContractTest(unittest.TestCase):
    """Contract checks for the blockcheck2 DPI-bypass strategy tester shipped
    in the zapret2 package.

    blockcheck2.sh is the entry point (#!/bin/sh, upstream 0755). The
    blockcheck2.d/ tree holds the ``standard`` and ``custom`` strategy
    modules that blockcheck2.sh SOURCES at runtime via ``. "$script"``. The
    package must ship the whole tree — not just blockcheck2.sh — or strategy
    testing is non-functional on the router (the previous Makefile installed
    only blockcheck2.sh, so every module load failed with "directory
    'blockcheck2.d' is absent or empty").

    The zapret2-core submodule is checked out at the pinned PKG_SOURCE_VERSION
    SHA (CI uses ``submodules: recursive``), so it is a faithful reference for
    what PKG_BUILD_DIR will contain. Source-content checks skip gracefully if
    the submodule is not initialized.
    """

    Z2 = ROOT / "openwrt" / "zapret2"
    Z2_MAKEFILE = Z2 / "Makefile"
    UPSTREAM = ROOT / "zapret2-core"
    PINNED_SHA = "8a0f53f3cf2c92ddeaa66995ee63a35c1210c410"

    REQUIRED_STANDARD_MODULES = (
        "10-http-basic.sh", "15-misc.sh", "17-oob.sh", "20-multi.sh",
        "23-seqovl.sh", "24-syndata.sh", "25-fake.sh", "30-faked.sh",
        "35-hostfake.sh", "50-fake-multi.sh", "55-fake-faked.sh",
        "60-fake-hostfake.sh", "90-quic.sh",
    )
    REQUIRED_CUSTOM_MODULES = ("10-list.sh",)
    REQUIRED_CUSTOM_DATA = (
        "README.txt", "list_http.txt", "list_https_tls12.txt",
        "list_https_tls13.txt", "list_quic.txt",
    )

    def setUp(self) -> None:
        self.makefile = self.Z2_MAKEFILE.read_text(encoding="utf-8")
        m = re.search(
            r"define Package/zapret2/install\n(?P<body>.*?)\nendef",
            self.makefile, re.DOTALL,
        )
        self.assertIsNotNone(m, "Package/zapret2/install block not found")
        self.install_body = m.group("body")

    def _bc_install_lines(self) -> list[str]:
        return [
            ln.strip() for ln in self.install_body.splitlines()
            if "blockcheck2" in ln and ln.strip()
        ]

    # --- Makefile install rules (always run, no submodule needed) ---

    def test_blockcheck2_sh_installed_executable(self) -> None:
        """blockcheck2.sh is the entry point (#!/bin/sh) -> INSTALL_BIN (0755)."""
        self.assertIn(
            "$(INSTALL_BIN) $(PKG_BUILD_DIR)/blockcheck2.sh "
            "$(1)/opt/zapret2/blockcheck2.sh",
            self.install_body,
        )

    def test_blockcheck2_d_standard_tree_installed(self) -> None:
        """standard/ *.sh modules -> INSTALL_BIN (0755); def.inc -> INSTALL_DATA (0644)."""
        self.assertIn(
            "$(INSTALL_DIR) $(1)/opt/zapret2/blockcheck2.d/standard",
            self.install_body,
        )
        self.assertIn(
            "$(INSTALL_BIN) $(PKG_BUILD_DIR)/blockcheck2.d/standard/*.sh "
            "$(1)/opt/zapret2/blockcheck2.d/standard/",
            self.install_body,
        )
        # def.inc is sourced (no shebang, upstream 0644) — must NOT be executable.
        self.assertIn(
            "$(INSTALL_DATA) $(PKG_BUILD_DIR)/blockcheck2.d/standard/def.inc "
            "$(1)/opt/zapret2/blockcheck2.d/standard/def.inc",
            self.install_body,
        )

    def test_blockcheck2_d_custom_tree_installed(self) -> None:
        """custom/ *.sh module -> INSTALL_BIN (0755); lists + README -> INSTALL_DATA (0644)."""
        self.assertIn(
            "$(INSTALL_DIR) $(1)/opt/zapret2/blockcheck2.d/custom",
            self.install_body,
        )
        self.assertIn(
            "$(INSTALL_BIN) $(PKG_BUILD_DIR)/blockcheck2.d/custom/*.sh "
            "$(1)/opt/zapret2/blockcheck2.d/custom/",
            self.install_body,
        )
        self.assertIn(
            "$(INSTALL_DATA) $(PKG_BUILD_DIR)/blockcheck2.d/custom/*.txt "
            "$(1)/opt/zapret2/blockcheck2.d/custom/",
            self.install_body,
        )

    def test_blockcheck2_installed_from_pkg_build_dir_only(self) -> None:
        """blockcheck2 files come from PKG_BUILD_DIR (pinned PKG_SOURCE), never
        from a vendored files/ tree or the ../../zapret2-core submodule path."""
        self.assertTrue(self._bc_install_lines(), "no blockcheck2 install lines")
        for line in self._bc_install_lines():
            # Only real install commands (start with the install macro); comment
            # lines mention "INSTALL_BIN"/"INSTALL_DATA" as prose and must be
            # excluded so they do not trip the $(PKG_BUILD_DIR) assertion.
            if line.startswith("$(INSTALL_BIN)") or line.startswith("$(INSTALL_DATA)"):
                self.assertIn("$(PKG_BUILD_DIR)", line, line)
                self.assertNotIn("$(CURDIR)", line, line)
                self.assertNotIn("../../zapret2-core", line, line)

    def test_no_vendored_blockcheck2_files_directory(self) -> None:
        self.assertFalse(
            (self.Z2 / "files").is_dir(),
            "openwrt/zapret2/files/ must not exist — blockcheck2 installed "
            "from PKG_BUILD_DIR, not vendored",
        )

    def test_blockcheck2_paths_preserve_upstream_structure(self) -> None:
        """Installed under /opt/zapret2/blockcheck2.d/{standard,custom}/."""
        for line in self._bc_install_lines():
            if "$(1)" in line and "blockcheck2.d" in line:
                # INSTALL_DIR targets have no trailing slash (.../standard);
                # INSTALL_BIN/INSTALL_DATA targets do (.../standard/). Accept both.
                self.assertRegex(
                    line,
                    r"\$\(1\)/opt/zapret2/blockcheck2\.d/(?:standard|custom)(?:/|$)",
                )

    def test_blockcheck2_install_set_has_no_binaries(self) -> None:
        """blockcheck2 ships only shell/text. No Windows or ELF binaries."""
        bc = "\n".join(self._bc_install_lines())
        for bad in (r"\.exe\b", r"\.dll\b", r"\.so\b", r"\.elf\b", r"\.bin\b"):
            self.assertNotRegex(bc, bad, f"binary pattern {bad} in blockcheck2 install")
        self.assertNotIn("winws2", bc)
        self.assertNotIn("dvtws2", bc)

    def test_makefile_pins_blockcheck2_upstream_sha(self) -> None:
        """Only the pinned bol-van/zapret2 SHA is used as the source."""
        self.assertRegex(
            self.makefile,
            r"(?m)^PKG_SOURCE_VERSION:=" + re.escape(self.PINNED_SHA) + r"$",
        )

    # --- source-content checks (skip if submodule not initialized) ---

    @unittest.skipUnless((UPSTREAM / "blockcheck2.sh").is_file(),
                         "zapret2-core submodule not checked out")
    def test_upstream_submodule_matches_pinned_sha(self) -> None:
        """The submodule HEAD must equal the Makefile's pinned PKG_SOURCE_VERSION,
        so the inspected source equals what PKG_BUILD_DIR will contain."""
        try:
            r = subprocess.run(
                ["git", "-C", str(self.UPSTREAM), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            self.skipTest("git not available")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), self.PINNED_SHA)

    @unittest.skipUnless((UPSTREAM / "blockcheck2.sh").is_file(),
                         "zapret2-core submodule not checked out")
    def test_required_standard_modules_present_upstream(self) -> None:
        d = self.UPSTREAM / "blockcheck2.d" / "standard"
        self.assertTrue(d.is_dir(), d)
        for name in self.REQUIRED_STANDARD_MODULES:
            self.assertTrue((d / name).is_file(), f"missing standard module: {name}")
        self.assertTrue((d / "def.inc").is_file(), "missing standard/def.inc")

    @unittest.skipUnless((UPSTREAM / "blockcheck2.sh").is_file(),
                         "zapret2-core submodule not checked out")
    def test_required_custom_modules_and_data_present_upstream(self) -> None:
        d = self.UPSTREAM / "blockcheck2.d" / "custom"
        self.assertTrue(d.is_dir(), d)
        for name in self.REQUIRED_CUSTOM_MODULES:
            self.assertTrue((d / name).is_file(), f"missing custom module: {name}")
        for name in self.REQUIRED_CUSTOM_DATA:
            self.assertTrue((d / name).is_file(), f"missing custom data: {name}")

    @unittest.skipUnless((UPSTREAM / "blockcheck2.sh").is_file(),
                         "zapret2-core submodule not checked out")
    def test_upstream_blockcheck2_tree_has_no_binaries(self) -> None:
        """No Windows binaries and no ELF files anywhere in the blockcheck2 tree."""
        for p in (self.UPSTREAM / "blockcheck2.d").rglob("*"):
            if not p.is_file():
                continue
            self.assertFalse(
                p.name.endswith((".exe", ".dll", ".so", ".elf", ".bin")),
                f"binary extension in blockcheck2.d: {p}",
            )
            with p.open("rb") as fh:
                head = fh.read(4)
            self.assertNotEqual(head[:4], b"\x7fELF", f"ELF file in blockcheck2.d: {p}")
            self.assertNotEqual(head[:2], b"MZ", f"PE/Windows binary in blockcheck2.d: {p}")

    @unittest.skipUnless((UPSTREAM / "blockcheck2.sh").is_file(),
                         "zapret2-core submodule not checked out")
    def test_upstream_shell_modules_are_sourced_not_executed(self) -> None:
        """blockcheck2.d/*.sh have no shebang and are sourced by blockcheck2.sh
        (``. "$script"``). They ship executable per the package contract but are
        never invoked directly — confirming the no-shebang/source contract."""
        for sub in ("standard", "custom"):
            for p in sorted((self.UPSTREAM / "blockcheck2.d" / sub).glob("*.sh")):
                first = p.read_text(encoding="utf-8", errors="replace").lstrip()
                self.assertFalse(
                    first.startswith("#!"),
                    f"{p} has a shebang — blockcheck2.d modules must be sourced",
                )
        bc = (self.UPSTREAM / "blockcheck2.sh").read_text(encoding="utf-8")
        self.assertIn('. "$script"', bc)


class Blockcheck2StaticCheckTest(unittest.TestCase):
    """Safe static checks for the shipped blockcheck2 — NO firewall mutation.

    These checks never run the real blockcheck (which would stop services and
    rewrite nftables), never stop zapret/zapret2, and never touch nftables.
    They only:
      * syntax-check (``sh -n``) every shipped .sh; and
      * statically resolve blockcheck2's expected tool/base paths against the
        Makefile install rules.
    See ``docs/blockcheck2-orchestra-integration.md`` for why the real
    blockcheck must run only after legacy zapret + zapret2 are stopped and why
    its results are diagnostic-only (never auto-applied by Orchestra).
    """

    Z2 = ROOT / "openwrt" / "zapret2"
    Z2_MAKEFILE = Z2 / "Makefile"
    UPSTREAM = ROOT / "zapret2-core"

    def setUp(self) -> None:
        self.makefile = self.Z2_MAKEFILE.read_text(encoding="utf-8")
        m = re.search(
            r"define Package/zapret2/install\n(?P<body>.*?)\nendef",
            self.makefile, re.DOTALL,
        )
        self.assertIsNotNone(m, "Package/zapret2/install block not found")
        self.install_body = m.group("body")

    @unittest.skipUnless((UPSTREAM / "blockcheck2.sh").is_file(),
                         "zapret2-core submodule not checked out")
    @unittest.skipUnless(shutil.which("sh"), "sh not available on PATH")
    def test_all_shipped_shell_scripts_pass_syntax_check(self) -> None:
        """``sh -n`` every shipped .sh: blockcheck2.sh, blockcheck2.d/**/*.sh,
        and common/*.sh. Purely syntactic — no execution, so no firewall,
        service, or nftables side effects."""
        shipped = [self.UPSTREAM / "blockcheck2.sh"]
        shipped += sorted((self.UPSTREAM / "blockcheck2.d").rglob("*.sh"))
        shipped += sorted((self.UPSTREAM / "common").glob("*.sh"))
        self.assertGreater(len(shipped), 20, "expected >20 shipped shell scripts")
        failures = []
        for p in shipped:
            r = subprocess.run(
                ["sh", "-n", str(p)],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0:
                failures.append(f"{p}: {r.stderr.strip()}")
        self.assertEqual(failures, [], "sh -n failures:\n" + "\n".join(failures))

    @unittest.skipUnless((UPSTREAM / "blockcheck2.sh").is_file(),
                         "zapret2-core submodule not checked out")
    def test_zapret_base_resolves_to_opt_zapret2(self) -> None:
        """blockcheck2.sh sets ZAPRET_BASE=dirname($0). Since it ships at
        /opt/zapret2/blockcheck2.sh, ZAPRET_BASE resolves to /opt/zapret2."""
        bc = (self.UPSTREAM / "blockcheck2.sh").read_text(encoding="utf-8")
        self.assertRegex(bc, r'EXEDIR="\$\(dirname "\$0"\)"')
        self.assertRegex(bc, r'ZAPRET_BASE=\$\{ZAPRET_BASE:-"\$EXEDIR"\}')
        # And the Makefile ships blockcheck2.sh at /opt/zapret2/blockcheck2.sh.
        self.assertIn("$(1)/opt/zapret2/blockcheck2.sh", self.install_body)

    @unittest.skipUnless((UPSTREAM / "blockcheck2.sh").is_file(),
                         "zapret2-core submodule not checked out")
    def test_blockcheck2_expected_tool_paths_are_shipped(self) -> None:
        """blockcheck2.sh expects nfq2/nfqws2, mdig/mdig, common/*.sh and the
        blockcheck2.d tree under ZAPRET_BASE=/opt/zapret2. Statically verify
        each expected path string is in blockcheck2.sh AND that the Makefile
        installs the corresponding file/dir at that path."""
        bc = (self.UPSTREAM / "blockcheck2.sh").read_text(encoding="utf-8")
        # {expected substring in blockcheck2.sh: expected Makefile install marker}
        expectations = {
            "${ZAPRET_BASE}/nfq2/nfqws2": "$(1)/opt/zapret2/nfq2/nfqws2",
            "${ZAPRET_BASE}/mdig/mdig": "$(1)/opt/zapret2/mdig/mdig",
            "$ZAPRET_BASE/common/base.sh": "common/*.sh $(1)/opt/zapret2/common/",
            "$ZAPRET_BASE/blockcheck2.d": "$(1)/opt/zapret2/blockcheck2.d/standard",
        }
        for script_need, makefile_marker in expectations.items():
            self.assertIn(script_need, bc, f"blockcheck2.sh missing expected path: {script_need}")
            self.assertIn(
                makefile_marker, self.install_body,
                f"Makefile does not ship expected path: {makefile_marker}",
            )

    @unittest.skipUnless((UPSTREAM / "blockcheck2.sh").is_file(),
                         "zapret2-core submodule not checked out")
    def test_blockcheck2_not_wired_into_init_hotplug_or_cron(self) -> None:
        """blockcheck2 must NOT auto-run: it rewrites nftables and must run only
        after legacy zapret + zapret2 are stopped (see docs). Statically verify
        no install line wires blockcheck2 into init.d/hotplug/cron. (The
        pre-existing /etc/hotplug.d/iface/90-zapret2 hook belongs to the
        zapret2 daemon, not blockcheck2, and is intentionally excluded.)"""
        for line in self.install_body.splitlines():
            s = line.strip()
            if "blockcheck2" in s and (
                "init.d" in s or "hotplug" in s or "cron" in s or "service" in s
            ):
                self.fail(f"blockcheck2 is wired into an auto-run path: {s}")

    def test_static_checks_never_invoke_firewall_or_real_blockcheck(self) -> None:
        """Meta-guard: this module must never invoke the real blockcheck2.sh
        (which mutates the firewall) nor any firewall/service tool. Inspect the
        actual ``subprocess.run`` calls via AST — not source text, which would
        self-match the forbidden-command names. The only permitted shell
        subprocess is ``sh -n`` (syntax check); the only git call is
        ``rev-parse`` (read-only SHA verification)."""
        import ast
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        calls: list[list[str]] = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "run"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "subprocess"
                    and node.args
                    and isinstance(node.args[0], (ast.List, ast.Tuple))):
                parts = []
                for elt in node.args[0].elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        parts.append(elt.value)
                    else:
                        parts.append("<dynamic>")
                calls.append(parts)
        self.assertTrue(calls, "no subprocess.run calls found in test module")
        allowed = {"sh", "git"}
        for parts in calls:
            cmd0 = parts[0]
            self.assertIn(cmd0, allowed, f"forbidden subprocess command: {cmd0} in {parts}")
            if cmd0 == "sh":
                self.assertIn("-n", parts, f"sh call is not a syntax check: {parts}")
            if cmd0 == "git":
                self.assertIn("rev-parse", parts, f"git call is not read-only rev-parse: {parts}")


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
        text = wf.read_text(encoding="utf-8")
        self.assertIn(".apk", text)
        self.assertIn("workflow_dispatch", text)
        self.assertIn("sha256sum", text)
        self.assertIn("-j$(nproc)", text)
        self.assertIn("V=sc", text)
        # .ipk may appear only inside the "no .ipk files" contract check
        # block (the find, the FAIL/OK echoes, and the exit). It must never
        # appear as a build target, artifact pattern, or opkg command.
        in_no_ipk_block = False
        for line in text.splitlines():
            stripped = line.strip()
            if "no .ipk files" in stripped:
                in_no_ipk_block = True
                continue
            if in_no_ipk_block and stripped == "":
                in_no_ipk_block = False
            if ".ipk" in stripped:
                self.assertTrue(
                    in_no_ipk_block or "*.ipk" in stripped,
                    f"workflow references .ipk outside the absence check: {stripped}",
                )
        self.assertNotIn("opkg", text)

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

    def test_workflow_uses_sdk_bundled_apk_for_inspection(self) -> None:
        wf = ROOT / ".github" / "workflows" / "build-apk.yml"
        text = wf.read_text(encoding="utf-8")
        # Inspection uses only the apk binary shipped inside the OpenWrt SDK
        # (staging_dir/host/bin/apk), whose presence is confirmed up front by
        # the "Verify SDK layout" step. No Alpine/system apk, no apk-tools
        # package, no host compile of a pinned apk version.
        self.assertIn("staging_dir/host/bin/apk", text)
        self.assertIn("Verify SDK layout", text)
        self.assertIn("test -x staging_dir/host/bin/apk", text)
        self.assertNotIn("command -v apk", text)
        self.assertNotIn("apk-tools", text)
        self.assertNotIn("dl-cdn.alpinelinux.org", text)
        self.assertIn("SDK_ABS", text)
        self.assertIn("APK_BIN", text)
        # File-level inspection uses `apk extract --destination` (which unpacks
        # a local .apk without an installed database), not `apk info`/`apk
        # manifest` (which require an installed DB and fail on a bare file).
        # Locally built APKs carry an untrusted signature, so extraction must
        # also pass --allow-untrusted (the same flag OpenWrt uses to stage its
        # own packages); without it apk rejects the file with exit 99.
        self.assertIn("--allow-untrusted", text)
        self.assertIn("extract --destination", text)
        # The extracted tree feeds every contract check.
        self.assertIn("/tmp/apk-orch", text)
        self.assertIn("/tmp/apk-z2", text)

    def test_workflow_checks_no_ipk_and_arch(self) -> None:
        wf = ROOT / ".github" / "workflows" / "build-apk.yml"
        text = wf.read_text(encoding="utf-8")
        self.assertIn("no .ipk files", text)
        # The workflow enforces orchestra PKGARCH:=all and zapret2 target-arch
        # by grepping the Makefiles for the PKGARCH assignment.
        self.assertIn("PKGARCH", text)
        self.assertIn(r"\s*:=\s*all", text)

    def test_workflow_stage2_audit_verifies_elv_arch_and_manifest(self) -> None:
        wf = ROOT / ".github" / "workflows" / "build-apk.yml"
        text = wf.read_text(encoding="utf-8")
        # A dedicated Stage 2 audit step records SHA256 of both APKs, confirms
        # every ELF in the zapret2 APK is aarch64 (no leaked x86_64 host
        # binary), runs readelf -h on nfqws2/ip2net/mdig, confirms the
        # orchestra APK has no ELF binaries, and dumps the on-artifact
        # package manifests (.list) and conffiles extracted from the APKs.
        self.assertIn("Stage 2 APK audit", text)
        self.assertIn("sha256sum", text)
        self.assertIn("readelf -h", text)
        self.assertIn("x86-64", text)
        self.assertIn("aarch64", text)
        # The audit loops over the three target binaries by name.
        self.assertIn('for bin in nfqws2 ip2net mdig', text)
        self.assertIn('find /tmp/apk-z2 -name "$bin"', text)
        self.assertIn("orchestra APK has no ELF binaries", text)
        self.assertIn("/lib/apk/packages/$p.list", text)
        self.assertIn("/lib/apk/packages/$p.conffiles", text)


if __name__ == "__main__":
    unittest.main()
