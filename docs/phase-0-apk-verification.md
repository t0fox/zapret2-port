# Phase 0 — APK artifact verification

**Date:** 2026-07-21
**Workflow run (final):** [`29877837114`](https://github.com/t0fox/zapret2-port/actions/runs/29877837114) on `nightly/20260721-224907Z`, commit `dd6e9c3` — **success**.
**Inspection-only run (first success):** [`29875775778`](https://github.com/t0fox/zapret2-port/actions/runs/29875775778) on commit `d061651` — **success**.
**SDK:** OpenWrt 25.12.5, `mediatek/filogic`, `aarch64_cortex-a53`, apk-tools 3.0.5 (SDK-bundled `staging_dir/host/bin/apk`).
**APK format:** ADB v3 (magic `41 44 42 64` = "ADBd"), confirmed by inspecting the first bytes of both packages.

The workflow builds `zapret2` (target arch) and `zapret2-orchestra` (`PKGARCH:=all`) with the official OpenWrt SDK, extracts both packages with `apk --allow-untrusted extract --destination` (locally built packages carry an untrusted signature), and runs two gating verification steps: a contract inspection and a Stage 2 ELF/manifest audit. All checks below passed in run `29877837114`.

## Artifacts and SHA256

SHA256 computed locally with `Get-FileHash` and cross-checked against the CI `sha256sum` output (`artifacts/apk-sha256.txt`); the two match byte-for-byte.

| Package | Filename | Size | SHA256 |
|---|---|---|---|
| zapret2 | `zapret2-0.9.20260307-r1.apk` | 253303 B | `fe9cc92c8367be39b4b97bd4ae2fb8d914b0cf73bd39c4953aace3a7ff875b78` |
| zapret2-orchestra | `zapret2-orchestra-0.1.0-r3.apk` | 11147 B | `073e5d74f9c19ff700b65cf0156096979561fcc22205fc7a702a0106da3299a1` |

Both packages are produced under `bin/packages/aarch64_cortex-a53/base/`. No legacy ipkg-format archives appear anywhere in `bin/packages` (`bin-packages-listing.txt` contains zero such entries).

## Package name / version / architecture / dependencies

From the OpenWrt build system's `tmp/.packageinfo` (artifact `packageinfo.txt`) and the `.apk` filenames:

| Field | zapret2 | zapret2-orchestra |
|---|---|---|
| Package | `zapret2` | `zapret2-orchestra` |
| Version | `0.9.20260307-r1` | `0.1.0-r3` |
| Architecture | target (`aarch64_cortex-a53`; no `PKGARCH` in Makefile) | `all` (`PKGARCH:=all`; packageinfo `Provides: @zapret2-orchestra-any`) |
| Depends | `+libc +nftables +curl +gzip +coreutils +coreutils-sort +coreutils-sleep +kmod-nft-nat +kmod-nft-offload +kmod-nft-queue +libnetfilter-queue +libmnl +libcap +zlib +luajit` | `+libc +zapret2 +rpcd +rpcd-mod-ucode +ucode +ucode-mod-fs +ucode-mod-uci` |
| Source | `zapret2-0.9.20260307.tar.zst` (bol-van/zapret2, pinned SHA) | self-contained (no `PKG_SOURCE`) |

The packageinfo `Type: ipkg` label is a known OpenWrt build-system metadata field and does not indicate the output format; the actual artifacts are `.apk` (ADB v3), confirmed by the `.apk` filenames, the `file` output, and the absence of legacy ipkg-format archives.

**orchestra depends on zapret2** — confirmed both in the Makefile `DEPENDS` (`+zapret2`, verified by `tests/test_package_contract.py`) and in the built package metadata (`packageinfo.txt` `Depends: ...+zapret2...`).

## Manifest (on-artifact file lists and conffiles)

The package file manifests and conffiles were read directly from the extracted APK trees at `/lib/apk/packages/<pkg>.list` and `/lib/apk/packages/<pkg>.conffiles` (artifact `package-manifests.txt`).

### zapret2 (94 files)

Notable entries: `/opt/zapret2/nfq2/nfqws2`, `/opt/zapret2/ip2net/ip2net`, `/opt/zapret2/mdig/mdig`, six upstream Lua modules under `/opt/zapret2/lua/` (`zapret-lib`, `zapret-antidpi`, `zapret-auto`, `zapret-obfs`, `zapret-pcap`, `zapret-tests`), `/etc/init.d/zapret2`, `/etc/hotplug.d/iface/90-zapret2`, `/etc/firewall.zapret2`, shell helpers under `/opt/zapret2/common/`, ipset scripts under `/opt/zapret2/ipset/`, fake-packet data under `/opt/zapret2/files/fake/`, and `/opt/zapret2/config` + `/opt/zapret2/config.default`.

Conffiles (from the APK): `/opt/zapret2/config`. The APK also records a `conffiles_static` entry with the SHA256 of the default config.

### zapret2-orchestra (15 files)

`/etc/init.d/zapret2-orchestra`, the four JSON seeds under `/etc/zapret2-orchestra/` (`blocked`, `learned`, `manual-locks`, `whitelist`), six Orchestra Lua modules under `/opt/zapret2/lua/orchestra-extra/` (`init`, `slm`, `slm-adapter`, `events`, `detectors`, `orchestrator`), `/usr/sbin/zapret2-orchestra-preload` (wrapper), `/usr/share/rpcd/acl.d/zapret2-orchestra.json` (ACL), `/usr/share/rpcd/ucode/zapret2.orchestra` (rpcd backend), and `/usr/share/zapret2-orchestra/generate-preload.uc` (preload generator).

Conffiles (from the APK): `blocked.json`, `learned.json`, `manual-locks.json`, `whitelist.json` — matching the Makefile `conffiles` declaration. `conffiles_static` records the SHA256 of each seed.

**orchestra contains no `nfqws2`, `ip2net`, or `mdig`** — the orchestra `.list` has none of those entries, and the orchestra ELF inventory is empty (see below).

## zapret2 ELF verification

The Stage 2 audit inventories every ELF file in the extracted zapret2 tree. Exactly three ELF files exist, all `ARM aarch64`, dynamically linked against the musl-aarch64 dynamic loader (`interpreter /lib/ld-musl-aarch64.so.1`). No `x86-64` ELF was found (the audit gates on this and would fail the workflow otherwise).

`file` output (artifact `zapret2-elf-inventory.txt`):

```
/opt/zapret2/ip2net/ip2net: ELF 64-bit LSB executable, ARM aarch64, version 1 (SYSV), dynamically linked, interpreter /lib/ld-musl-aarch64.so.1, no section header
/opt/zapret2/mdig/mdig:     ELF 64-bit LSB executable, ARM aarch64, version 1 (SYSV), dynamically linked, interpreter /lib/ld-musl-aarch64.so.1, no section header
/opt/zapret2/nfq2/nfqws2:   ELF 64-bit LSB executable, ARM aarch64, version 1 (SYSV), dynamically linked, interpreter /lib/ld-musl-aarch64.so.1, no section header
```

`readelf -h` (artifact `zapret2-elf-checks.txt`) for each binary reports `Class: ELF64`, `Type: EXEC (Executable file)`, `Machine: AArch64`. Entry points: nfqws2 `0x406a9c`, ip2net `0x4015f8`, mdig `0x40174c`.

`readelf -d` NEEDED shared libraries:

| Binary | NEEDED |
|---|---|
| nfqws2 | `libluajit-5.1.so.2`, `libz.so.1`, `libnetfilter_queue.so.1`, `libnfnetlink.so.0`, `libmnl.so.0`, `libgcc_s.so.1`, `libc.so` |
| ip2net | `libgcc_s.so.1`, `libc.so` |
| mdig | `libgcc_s.so.1`, `libc.so` |

Every NEEDED library is a musl-aarch64 target runtime library; none reference `ld-linux-x86-64` or `libc.so.6`, which independently confirms the aarch64 target. The NEEDED set matches the package `DEPENDS` (libnetfilter-queue, libmnl, zlib, luajit, and the implicit libc/libgcc).

**No x86_64 target binaries** are present in the zapret2 APK — the only ELF files are the three aarch64 binaries above.

## orchestra ELF verification

The orchestra APK contains **no ELF binaries at all** (`orchestra-elf-inventory.txt` is empty; the audit gates on this). The package is architecture-independent (`PKGARCH:=all`) and ships only scripts, Lua modules, ucode, JSON, and a shell wrapper — consistent with having no compiled binaries.

## Contract inspection (workflow step 14)

The preceding "Inspect and verify APKs" step additionally confirmed, against the extracted trees and the in-tree Makefiles:

- zapret2 APK contains `nfqws2`, and it is an AArch64 ELF.
- orchestra APK contains no `nfqws2`.
- orchestra Makefile references `zapret2` in `DEPENDS`.
- no `../../zapret2-core` reference in any package Makefile.
- orchestra conffiles (`blocked.json`, `learned.json`, `manual-locks.json`, `whitelist.json`) present.
- orchestra required files present (rpcd backend, ACL, preload generator, wrapper, boot hook, `orchestra-extra/init.lua`).
- zapret2 required files present (`nfqws2`, `ip2net`, `mdig`, `zapret-lib.lua`, `zapret-antidpi.lua`, `zapret-auto.lua`, `/etc/init.d/zapret2`).
- no legacy ipkg-format archives in `bin/packages`.
- zapret2 Makefile does not declare `PKGARCH:=all` (target arch).
- orchestra Makefile declares `PKGARCH:=all`.

## Verdict

All mandatory Phase 0 artifact checks pass:

- Both APKs built, downloaded, and SHA256-verified (local + CI match).
- zapret2 is target arch `aarch64_cortex-a53`; orchestra is `PKGARCH:=all`.
- orchestra depends on zapret2; orchestra contains no `nfqws2`/`ip2net`/`mdig`.
- `nfqws2`, `ip2net`, `mdig` are present and are AArch64 ELF executables (`readelf -h` Machine: AArch64, `readelf -d` NEEDED = musl-aarch64 libs).
- No x86_64 target binaries in the zapret2 APK.
- orchestra contains no ELF binaries.
- Manifests and conffiles read from the artifacts match the Makefile declarations.
- No legacy ipkg-format archives; APK-only (ADB v3).

**Phase 0 artifact verification: PASS.**
