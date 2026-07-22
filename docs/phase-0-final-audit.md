# Phase 0 — final audit

**Date:** 2026-07-21
**Branch:** `nightly/20260721-224907Z`
**Audited commits:** `92efc8f` (test alignment), `d061651` (CI `--allow-untrusted` fix), `dd6e9c3` (Stage 2 audit step)
**Successful workflow runs:** [`29875775778`](https://github.com/t0fox/zapret2-port/actions/runs/29875775778) (`d061651`), [`29877837114`](https://github.com/t0fox/zapret2-port/actions/runs/29877837114) (`dd6e9c3`)
**Tests:** `python3 -m unittest discover -s tests` — 75 tests, 0 failures, 9 skipped (ucode runtime tests, skipped when no `ucode` interpreter is on PATH).
**Artifact evidence:** `docs/phase-0-apk-verification.md`

This audit consolidates the Phase 0 requirements against the current nightly branch. Each item is verified either by a static source check, a passing test, or a successful CI run with downloaded and inspected artifacts.

## Checklist

### Source provenance

| Item | Value | Evidence |
|---|---|---|
| Source only from bol-van/zapret2 | `PKG_SOURCE_URL:=https://github.com/bol-van/zapret2.git` | `openwrt/zapret2/Makefile:10`; `tests/test_package_contract.py::Zapret2PackageContractTest::test_uses_standard_source_contract` |
| Pinned SHA | `8a0f53f3cf2c92ddeaa66995ee63a35c1210c410` | `openwrt/zapret2/Makefile:11`; matches the `zapret2-core` submodule HEAD (`git ls-tree HEAD zapret2-core`); `PKG_SOURCE_DATE:=2026-03-07` |
| No third-party Zapret forks | none referenced | workflow contains no `cp -r .../zapret2-core` and no non-official zapret git URL; `tests/...::test_workflow_does_not_copy_zapret2_core` |

### Toolchain and target

| Item | Value | Evidence |
|---|---|---|
| OpenWrt version | 25.12.5 | `build-apk.yml` SDK URL `downloads.openwrt.org/releases/25.12.5/...` |
| Target | mediatek/filogic | SDK path `targets/mediatek/filogic/` |
| Architecture | `aarch64_cortex-a53` | both APKs produced under `bin/packages/aarch64_cortex-a53/base/`; `zapret2-core` init and config target this arch |
| SDK SHA256 pinned | `ff4a38a397caa2cfe1c39e18f84ddede14878221b3593c3f2c4cfe24e3ec4c25` | `build-apk.yml` `SDK_SHA256`; verified at runtime by `sha256sum` before extraction |

### Package format

| Item | Value | Evidence |
|---|---|---|
| APK-only (ADB v3) | yes | both artifacts are ADB v3 (magic `ADBd`); 0 legacy ipkg-format archives in `bin/packages`; `tests/...::test_workflow_targets_apk_not_ipk`, `::test_no_ipk_or_opkg_in_makefile` |
| No legacy ipkg/opkg as a build/artifact target | confirmed | workflow has no `opkg` and no ipkg-format build target; the only ipkg-format mention is the "no ... files" absence check |

### Package architecture split

| Item | Value | Evidence |
|---|---|---|
| zapret2 target architecture | target (no `PKGARCH`) | `openwrt/zapret2/Makefile` has no `PKGARCH`; built under `aarch64_cortex-a53/base/`; `tests/...::test_is_target_arch_not_all` |
| orchestra `PKGARCH:=all` | yes | `openwrt/zapret2-orchestra/Makefile:6`; `packageinfo` `Provides: @zapret2-orchestra-any`; workflow gates on `PKGARCH:=all`; `tests/...::test_package_metadata_is_exact` |

### Package relationship

| Item | Value | Evidence |
|---|---|---|
| orchestra depends on zapret2 | `DEPENDS: +zapret2 ...` | `openwrt/zapret2-orchestra/Makefile:25`; built `packageinfo.txt` `Depends: ...+zapret2...`; `tests/...::test_dependencies_are_exact_and_no_lua_package_dependency` |
| No nfqws2 inside orchestra | confirmed | orchestra `.list` manifest has no `nfqws2`/`ip2net`/`mdig`; orchestra ELF inventory is empty; CI contract step gates on this |
| nfqws2 is AArch64 ELF | `Machine: AArch64`, `Type: EXEC` | `readelf -h` on extracted nfqws2 (artifact `zapret2-elf-checks.txt`); NEEDED libs are musl-aarch64; `docs/phase-0-apk-verification.md` |
| No x86_64 target binaries | confirmed | zapret2 ELF inventory lists exactly three binaries, all `ARM aarch64`; Stage 2 audit gates on `x86-64` absence |

### conffiles

| Package | Conffiles | Evidence |
|---|---|---|
| zapret2 | `/opt/zapret2/config` | Makefile `conffiles` block; APK `zapret2.conffiles`; `tests/...::test_conffiles_lists_config` |
| zapret2-orchestra | `blocked.json`, `learned.json`, `manual-locks.json`, `whitelist.json` (all under `/etc/zapret2-orchestra/`) | Makefile `conffiles` block; APK `zapret2-orchestra.conffiles`; `tests/...::test_json_seeds_are_listed_as_conffiles` |

### Self-contained packages

| Item | Evidence |
|---|---|
| No `../../zapret2-core` in any Makefile | `tests/...::test_makefile_has_no_relative_escape_paths_or_absolute_paths`, `::test_no_submodule_path_dependency`; CI contract step greps `package/` |
| No runtime dependency on submodules | orchestra Makefile installs only from `$(CURDIR)/files/` (8 references, 0 to `zapret2-core`/`zapret-openwrt`/`zapret2gui`); zapret2 builds from the `PKG_SOURCE` download, not a submodule path; `tests/...::test_all_runtime_files_are_installed_from_package_files_tree` |
| Package is not location-dependent | orchestra install uses `$(CURDIR)/files/...` only; zapret2 uses `$(PKG_BUILD_DIR)/...`; `tests/...::test_install_uses_pkg_build_dir_not_curdir_files` |

### Init ordering

| Service | START | Evidence |
|---|---|---|
| zapret2-orchestra (boot hook) | `START=20` | `openwrt/zapret2-orchestra/files/etc/init.d/zapret2-orchestra:21`; `tests/...::test_boot_hook_runs_before_zapret2_and_is_backup_only` |
| zapret2 | `START=21` | `zapret2-core/init.d/openwrt/zapret2:5` (upstream source, pinned SHA) |

Orchestra runs first so the preload already exists if Orchestra is later enabled on the zapret2 service. The boot hook is backup-only (it regenerates `/tmp/zapret2-orchestra/*` from the persistent JSON seeds and does not start/stop/reconfigure zapret2).

### Build, tests, artifact verification

| Item | Result | Evidence |
|---|---|---|
| Unit tests | 75 pass, 9 skip, 0 fail | `python3 -m unittest discover -s tests` |
| APK workflow | success | run `29877837114`, all 20 steps green |
| Artifact SHA256 (local + CI match) | confirmed | `docs/phase-0-apk-verification.md` |
| Manifest read from artifacts | confirmed | `package-manifests.txt` |
| ELF audit (aarch64, no x86_64, orchestra no ELF) | confirmed | `zapret2-elf-checks.txt`, `zapret2-elf-inventory.txt`, `orchestra-elf-inventory.txt` |

## Verdict

Every Phase 0 requirement is satisfied with direct evidence on the current nightly branch:

- provenance is bol-van/zapret2 at the pinned SHA, built with the official OpenWrt 25.12.5 mediatek/filogic SDK for `aarch64_cortex-a53`;
- the output is APK-only (ADB v3), with no legacy ipkg/opkg artifacts;
- zapret2 is target-arch and ships aarch64 nfqws2/ip2net/mdig (verified by `readelf -h`/`readelf -d`), with no x86_64 binaries;
- orchestra is `PKGARCH:=all`, depends on zapret2, carries no compiled binaries, and ships the rpcd backend, ACL, preload generator, wrapper, boot hook (`START=20`, before zapret2 `START=21`), Lua runtime, and JSON seeds;
- conffiles are correct; packages are self-contained with no submodule runtime dependency and no `../../zapret2-core`;
- tests, the CI build, and the artifact verification all pass.

**PHASE 0 READY.**

Proceeding to Phase 1A (Runtime Manager safe foundation) on the same nightly branch.
