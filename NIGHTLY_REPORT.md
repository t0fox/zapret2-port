# NIGHTLY REPORT — 2026-07-21

**Branch:** `nightly/20260721-224907Z`
**Base:** `origin/main` (`8cc6681`)
**Working directory:** `H:\zapret-port\target`

## Commits

| Hash | Description |
|---|---|
| `8cc6681` | Base (origin/main) — "Fix CI: use apk extract --destination, drop broken apk info/manifest" |
| `92efc8f` | tests: align APK contract tests with SDK-bundled apk inspection |
| `d061651` | fix(ci): pass --allow-untrusted to apk extract for locally-built APKs |
| `dd6e9c3` | ci: add Stage 2 APK audit step (SHA256, ELF arch, readelf -h, manifest) |
| `a60b2bb` | docs: Phase 0 final audit → PHASE 0 READY |
| `c875de2` | feat: Phase 1A runtime manager safe foundation |
| `8e5fcc3` | docs: Phase 1B runtime manager plan (design only) |

## Workflow Runs

| Run ID | Commit | Branch | Result | URL |
|---|---|---|---|---|
| `29874001782` | `8cc6681` | main | FAILED (step 14: UNTRUSTED signature) | [link](https://github.com/t0fox/zapret2-port/actions/runs/29874001782) |
| `29875775778` | `d061651` | nightly | **SUCCESS** (first) | [link](https://github.com/t0fox/zapret2-port/actions/runs/29875775778) |
| `29877837114` | `dd6e9c3` | nightly | **SUCCESS** (Stage 2 audit) | [link](https://github.com/t0fox/zapret2-port/actions/runs/29877837114) |
| `29905721035` | `c875de2` | nightly | **SUCCESS** (Phase 1A, manager files in orchestra APK) | [link](https://github.com/t0fox/zapret2-port/actions/runs/29905721035) |

## Errors Found and Fixed

### 1. UNTRUSTED signature (exit 99)
**Run:** 29874001782, step 14
**Cause:** `apk extract --destination` refused untrusted locally-built APKs.
**Fix:** `d061651` — added `--allow-untrusted` global flag (the same flag OpenWrt uses to stage packages). Extraction failure also made fatal (exit 1) instead of WARN, so future issues surface immediately.

### 2. Stale contract tests
**Cause:** Three tests asserted the OLD workflow approach (`package/system/apk/host/compile`, `3.0.5`, `actions/cache@v`, `arch.*all`), but the workflow correctly uses the SDK-bundled `staging_dir/host/bin/apk` per the instruction requirement.
**Fix:** `92efc8f` — aligned tests with the current (correct) workflow.

## APK Artifact Verification (Stage 2)

### SHA256

| File | SHA256 | Size |
|---|---|---|
| `zapret2-0.9.20260307-r1.apk` | `fe9cc92c8367be39b4b97bd4ae2fb8d914b0cf73bd39c4953aace3a7ff875b78` | 253303 B |
| `zapret2-orchestra-0.1.0-r3.apk` (Phase 0) | `073e5d74f9c19ff700b65cf0156096979561fcc22205fc7a702a0106da3299a1` | 11147 B |
| `zapret2-orchestra-0.1.0-r3.apk` (Phase 1A) | `6113ad6c5917f2e0a7e1b731d0799a6a37253fbd2d07314c2e6485b9bf7520a5` | 17511 B |

**zapret2 APK SHA256 is byte-identical** between the Phase 0 and Phase 1A runs — confirms the zapret2 package was not unexpectedly modified.

### ELF verification (nfqws2, ip2net, mdig)
- nfqws2: `ELF64, Machine: AArch64, Type: EXEC`, interpreter `/lib/ld-musl-aarch64.so.1`. NEEDED: libluajit-5.1, libz, libnetfilter_queue, libnfnetlink, libmnl, libgcc_s, libc.
- ip2net: `ELF64, Machine: AArch64`. NEEDED: libgcc_s, libc.
- mdig: `ELF64, Machine: AArch64`. NEEDED: libgcc_s, libc.
- **No x86_64 ELF** in zapret2 APK.
- Orchestra APK contains **zero ELF binaries**.

### Architecture
- zapret2: target arch `aarch64_cortex-a53` (no PKGARCH in Makefile)
- orchestra: `PKGARCH:=all` (confirmed by Makefile, packageinfo `Provides: @zapret2-orchestra-any`, and on-artifact conffiles)

### Dependencies
- orchestra `Depends: +zapret2` — confirmed in both Makefile (tests) and built packageinfo.txt.
- zapret2 `Depends: +libc +nftables +curl +gzip +coreutils*(3) +kmod-nft-*(3) +libnetfilter-queue +libmnl +libcap +zlib +luajit`

### Package contents (orchestra, Phase 1A run)
- rpcd backend (`/usr/share/rpcd/ucode/zapret2.orchestra`)
- ACL (`/usr/share/rpcd/acl.d/zapret2-orchestra.json`)
- preload generator (`/usr/share/zapret2-orchestra/generate-preload.uc`)
- preload wrapper (`/usr/sbin/zapret2-orchestra-preload`)
- **runtime manager ucode** (`/usr/share/zapret2-orchestra/apply.uc`) ← NEW
- **runtime manager CLI** (`/usr/sbin/zapret2-orchestra-apply`) ← NEW
- boot hook (`/etc/init.d/zapret2-orchestra`, START=20)
- 6 Lua modules (`/opt/zapret2/lua/orchestra-extra/*.lua`)
- 4 JSON seeds (`/etc/zapret2-orchestra/*.json`, conffiles)
- **manager-state.json is NOT a conffile** (lives under /tmp)

## Test Results

```
python3 -m unittest discover -s tests -v
Ran 147 tests in 0.046s
OK (skipped=21)
```

- 126 tests always run (static contract checks, Python parser oracle, fixture rounds-trip, shell injection guards, byte preservation, profile validation oracle, state schema oracle, mkdir lock oracle, no-config-mutation invariants, no-UCI/remittor/config-fallback invariants)
- 9 skipped: ucode preload generator runtime tests (ucode not on Windows PATH)
- 12 skipped: ucode runtime manager runtime tests (ucode not on Windows PATH)
- **0 failures**

## Phase 0 Verdict

**PHASE 0 READY.** (See `docs/phase-0-final-audit.md` for full evidence.)

## Phase 1A Status

**PHASE 1A COMPLETE.** Delivered:

- `openwrt/zapret2-orchestra/files/usr/share/zapret2-orchestra/apply.uc` — safe NFQWS2_OPT parser/transformer, atomic JSON state, mkdir lock, profile validator, CLI dispatch
- `openwrt/zapret2-orchestra/files/usr/sbin/zapret2-orchestra-apply` — shell CLI wrapper with `sh -n` on validate-config
- `tests/_nfqws2_parser.py` — Python parser oracle (the spec)
- `tests/fixtures/nfqws2/` — 4 fixture configs
- `tests/test_runtime_manager.py` — 72 behavioral tests (static + oracle + runtime)
- Updated `openwrt/zapret2-orchestra/Makefile` to install the two new files

All new files are confirmed present in the built orchestra APK (run `29905721035`).

## Remaining Issues / What to Check Manually

1. **Runtime testing on a real router**: The ucode runtime tests (12 skipped) require a `ucode` interpreter. These tests verify the actual ucode manager against the Python oracle. Deploy the nightly orchestra APK to an OpenWrt 25.12.5 aarch64_cortex-a53 device and run:
   ```
   ucode /usr/share/zapret2-orchestra/apply.uc status
   ucode /usr/share/zapret2-orchestra/apply.uc validate-config
   sh /usr/sbin/zapret2-orchestra-apply validate-config
   ```

2. **`ucode-mod-fs` API surface**: `apply.uc` uses `rmdir` and `readlink` from the `fs` module. These are standard in `ucode-mod-fs` but were not confirmed from the existing `generate-preload.uc` (which only uses `readfile, writefile, mkdir, rename, unlink, stat`). Verify these symbols are available in the target's ucode-mod-fs.

3. **Phase 1A is read-only**: `enable`, `disable`, `apply`, `rollback`, `boot-check` return `not-implemented-phase-1a` (exit 2, no side effects). The live config (`/opt/zapret2/config`) is never written.

4. **Phase 1B (planned, not implemented)**: See `docs/runtime-manager-phase-1b-plan.md` for the transaction engine design.

## Documents Produced

- `docs/phase-0-apk-verification.md` — Stage 2 evidence
- `docs/phase-0-final-audit.md` — Phase 0 verdict
- `docs/runtime-manager-phase-1b-plan.md` — Phase 1B design (no implementation)
