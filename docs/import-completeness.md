# Import completeness report

**Audit date:** 2026-07-17 (post-import)  
**Scope:** `router-baseline/`, `reference/desktop-orchestra/`

Legend: **PRESENT** | **MISSING** | **EMPTY** | **UNEXPECTED TYPE** | **NOT IMPORTED** (on router per inventory, absent from baseline)

---

## 1. Expected router-baseline files

| File | Status | Size (bytes) | Notes |
|------|--------|--------------|-------|
| `router-baseline/etc/config/zapret2` | PRESENT | ~1.8K | UCI mirror of live config |
| `router-baseline/etc/init.d/zapret2` | PRESENT | ~2.5K | remittor wrapper; delegates to `$ZAPRET_ORIG_INITD` |
| `router-baseline/opt/zapret2/config` | PRESENT | ~4.5K | Shell config; `POSTNAT=1`, `MODE_FILTER=hostlist` |
| `router-baseline/opt/zapret2/lua/zapret-auto.lua` | PRESENT | 549 lines | Upstream automation |
| `router-baseline/opt/zapret2/lua/zapret-lib.lua` | PRESENT | 2654 lines | `NFQWS2_COMPAT_VER_REQUIRED=5` |
| `router-baseline/opt/zapret2/lua/zapret-antidpi.lua` | PRESENT | ~1241 lines | Desync primitives |
| `router-baseline/opt/zapret2/ipset/zapret-hosts-user.txt` | PRESENT | non-empty | Discord-focused hostlist sample |

---

## 2. Runtime snapshots

| File | Status | Notes |
|------|--------|-------|
| `router-baseline/runtime/openwrt-release.txt` | PRESENT | OpenWrt **25.12.5**, aarch64 |
| `router-baseline/runtime/nfqws2-version.txt` | PRESENT | nfqws2 **0.9.20260307**, `lua_compat_ver 5` |
| `router-baseline/runtime/uci-zapret2.txt` | PRESENT | Full UCI dump |
| `router-baseline/runtime/service-status.txt` | PRESENT | **`inactive`** at capture time |
| `router-baseline/runtime/ubus-zapret2.json` | PRESENT (broken) | `ubus call` parse error — not usable |
| `router-baseline/runtime/zapret2-file-inventory.txt` | PRESENT | 154 paths on router |
| `router-baseline/runtime/nft-ruleset.txt` | PRESENT | `zapret2` sets only; **no NFQUEUE rules** in dump |
| `router-baseline/runtime/nfqws2-process.txt` | **MISSING** | Cannot confirm live cmdline |

---

## 3. Desktop Orchestra reference

| File | Status | Notes |
|------|--------|-------|
| `reference/desktop-orchestra/lua/zapret-auto.lua` | PRESENT | 549 lines |
| `reference/desktop-orchestra/lua/zapret-lib.lua` | PRESENT | 2714 lines; `NFQWS2_COMPAT_VER_REQUIRED=**6**` |
| `reference/desktop-orchestra/lua/combined-detector.lua` | PRESENT | `circular_quality`, combined detectors |
| `reference/desktop-orchestra/lua/strategy-lock-manager.lua` | PRESENT | SLM |
| `reference/desktop-orchestra/lua/strategy-stats.lua` | PRESENT | Preload wrapper |
| `reference/desktop-orchestra/lua/learned-strategies.lua` | PRESENT | Generated preload |
| `reference/desktop-orchestra/lua/circular-config.txt` | PRESENT | Full Orchestra winws2 profile |
| `reference/desktop-orchestra/lua/whitelist.txt` | PRESENT | Orchestra exclude list |
| `reference/desktop-orchestra/settings/settings.json` | PRESENT | Orchestra persistent state |
| `reference/desktop-orchestra/lua/zapret-antidpi.lua` | **NOT IMPORTED** | Not in expected list; use router baseline copy |

---

## 4. On router but NOT in baseline (from inventory)

These exist on the live router (`zapret2-file-inventory.txt`) but were **not** copied into `router-baseline/`:

| Path | Needed for audit |
|------|------------------|
| `/opt/zapret2/nfq2/nfqws2` | Binary smoke / `--help` |
| `/opt/zapret2/common/linux_daemons.sh` | **Critical** — nfqws2 cmdline, `--lua-init`, NFQUEUE |
| `/opt/zapret2/init.d/openwrt/zapret2` | Original init (wrapped by `/etc/init.d/zapret2`) |
| `/opt/zapret2/init.d/openwrt/90-zapret2` | Firewall apply |
| `/opt/zapret2/init.d/openwrt/firewall.zapret2` | nftables NFQUEUE rules |
| `/opt/zapret2/init.d/openwrt/custom.d/*.sh` | Extension hooks (may be disabled) |
| `/opt/zapret2/init.d/openwrt/functions` | Helper functions |
| `/opt/zapret2/lua/zapret-obfs.lua`, `zapret-pcap.lua`, `zapret-tests.lua` | Optional |

**Action:** copy `linux_daemons.sh`, `init.d/openwrt/zapret2`, `firewall.zapret2`, and capture `nfqws2-process.txt` on next snapshot (no SSH in this audit).

---

## 5. Package version note

| Claim | Status |
|-------|--------|
| nfqws2 **0.9.20260307** | CONFIRMED (`runtime/nfqws2-version.txt`) |
| OpenWrt package **0.9.20260307-r1** | **NOT CAPTURED** — no `apk info` / `opkg` output in baseline |
| remittor authorship | CONFIRMED (`etc/init.d/zapret2` L2: Copyright remittor) |

---

## 6. Audit impact

| Gap | Blocks |
|-----|--------|
| Missing `nfqws2-process.txt` | CONFIRMED cmdline, active `--lua-init` order |
| Missing `linux_daemons.sh` | NFQUEUE number, queue flags, lua path resolution |
| Missing `firewall.zapret2` | NFQUEUE nft rules in snapshot |
| Service inactive at capture | Live runtime verification |
| Broken ubus snapshot | Service API contract |

Partial audit of runtime contract and TLS MVP design **continues** using imported Lua + UCI + config.
