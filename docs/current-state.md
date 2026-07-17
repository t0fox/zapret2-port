# Current state (confirmed from baseline)

**Updated:** 2026-07-17  
**Source of truth:** dynamically verified OpenWrt observations plus local
untracked `router-baseline/` evidence and `reference/desktop-orchestra/`.

---

## Platform

| Item | Value | Evidence |
|------|-------|----------|
| OpenWrt | 25.12.5 r33051, mediatek/filogic | `runtime/openwrt-release.txt` |
| Architecture | aarch64_cortex-a53 | same |
| nfqws2 version | 0.9.20260307 (git d3b3011) | `runtime/nfqws2-version.txt` |
| Lua compat | **5** (router) | `nfqws2-version.txt`; `zapret-lib.lua` L1 |
| Init wrapper | remittor `/etc/init.d/zapret2` | `etc/init.d/zapret2` |
| Zapret2 package | `zapret2-0.9.20260307-r1` | dynamically verified |
| Service state | **stopped intentionally** | dynamically verified |

---

## Paths (CONFIRMED from inventory + baseline)

| Resource | Path |
|----------|------|
| nfqws2 binary | `/opt/zapret2/nfq2/nfqws2` (inventory; binary not copied) |
| Lua upstream | `/opt/zapret2/lua/` |
| Shell config | `/opt/zapret2/config` |
| UCI | `/etc/config/zapret2` |
| Init (wrapper) | `/etc/init.d/zapret2` |
| nfqws2 PID | `/var/run/nfqws2_1.pid` |
| Init (original) | `/opt/zapret2/init.d/openwrt/zapret2` (inventory only) |
| Hostlist user | `/opt/zapret2/ipset/zapret-hosts-user.txt` |
| Logs | `/tmp/zapret2+…` per UCI `DAEMON_LOG_FILE` |

---

## UCI / config policy (CONFIRMED)

| Option | Value | File |
|--------|-------|------|
| `FWTYPE` | nftables | `etc/config/zapret2` L4 |
| `POSTNAT` | 1 | L5 |
| `FLOWOFFLOAD` | none | L6 |
| `DISABLE_IPV4` | 0 | L8 |
| `DISABLE_IPV6` | **1** | L9 |
| `MODE_FILTER` | **hostlist** | L11 |
| `DISABLE_CUSTOM` | **1** | L12 |
| `NFQWS2_PORTS_TCP` | 80,443 | L30 |
| `NFQWS2_PORTS_UDP` | 443 | L31 |
| `NFQWS2_ENABLE` | 1 | L26 |

---

## Active strategy profile (CONFIRMED — remittor default, not Orchestra)

From `NFQWS2_OPT` (`etc/config/zapret2` L38–58):

- TCP/80: `fake` + `multisplit` (HTTP)
- TCP/443: `fake` + `multidisorder` (TLS)
- UDP/443: QUIC `fake`

**No** `circular`, **no** `circular_quality`, **no** Orchestra `--lua-init` in UCI.

Orchestra is **not** active on router at baseline capture.

---

## Router Lua inventory (CONFIRMED via inventory)

Present on router: `zapret-antidpi.lua`, `zapret-auto.lua`, `zapret-lib.lua`, `zapret-obfs.lua`, `zapret-pcap.lua`, `zapret-tests.lua`.

**Absent on router:** `combined-detector.lua`, `strategy-lock-manager.lua`, `strategy-stats.lua`, `learned-strategies.lua`, `circular-config.txt`, `whitelist.txt` (Orchestra paths).

---

## NOT CAPTURED / UNKNOWN

| Item | Status |
|------|--------|
| NFQUEUE number | `300` (confirmed in runtime scripts) |
| Queue bypass flag | NOT CAPTURED |
| Exact nfqws2 argv | NOT CAPTURED — `nfqws2-process.txt` missing |
| Default `--lua-init` chain | NOT CAPTURED — need `linux_daemons.sh` |
| Package revision `-r1` | NOT CAPTURED |
| `DISABLE_CUSTOM=1` effect on `custom.d/` | UNKNOWN without `init.d/openwrt/functions` |

---

## Orchestra port status

| Layer | Status |
|-------|--------|
| Upstream Lua API for orchestrators | CONFIRMED on router (`circular`, `orchestrate`) |
| Orchestra modules on router | UNAVAILABLE (not installed) |
| orchestra-extra hook | INFERRED via UCI `NFQWS2_OPT` + extra `--lua-init` |
| Read-only control plane | `status` and `validate` implemented and dynamically verified via rpcd/ucode |
| Package layout | `openwrt/zapret2-orchestra`; not built with a real OpenWrt SDK |
| TLS MVP | Lua runtime prototype retained and not deployed |

## Local implementation progress

The first TLS runtime block now exists in `lua/orchestra-extra/` and is not
installed on, or connected to, the router.  It provides a Lua 5.1-compatible
in-memory SLM, protected manual-lock adapter, TLS detector wrappers and the
`circular_quality` selector.  It uses only the upstream symbols documented in
`remittor-runtime-contract.md`.

The read-only control plane is an rpcd/ucode backend in
`openwrt/zapret2-orchestra`. The verified target has `ucode`, `rpcd-mod-ucode`,
`ucode-mod-fs`, and `ucode-mod-uci` installed; its package exposes only
`status` and `validate`.

The read-only backend was manually copied to the router; `status` and
`validate` were dynamically verified, and rpcd was restarted. Zapret2 remained
stopped. No UCI was written, no Zapret2 service action was executed, and the
firewall and NFQUEUE were not changed. TLS Lua was not installed. `QNUM=300`
is a confirmed configuration value from the runtime scripts, not evidence of
an active NFQUEUE.

See `router-desktop-compatibility.md`, `tls-mvp-design.md`.
