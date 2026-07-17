# OpenWrt control-plane runtime decision

**Audit date:** 2026-07-17  
**Scope:** local-source architecture audit only; no backend, router access, or deployment.

## 1. Подтверждённые компоненты OpenWrt

| Компонент | Статус | Локальное доказательство |
|---|---|---|
| POSIX/BusyBox-compatible `/bin/sh` service path | CONFIRMED | `router-baseline/etc/init.d/zapret2:1`; wrapper invokes `/bin/sh /etc/rc.common` at lines 42, 57, 74, 80, 86. |
| procd service supervision | CONFIRMED | Baseline wrapper sets `USE_PROCD=1` (`router-baseline/etc/init.d/zapret2:4`). Upstream OpenWrt init opens procd instances and assigns PID files (`zapret2-core/init.d/openwrt/zapret2:47-58`). |
| UCI command-line use | CONFIRMED | Baseline wrapper writes and commits UCI (`router-baseline/etc/init.d/zapret2:38-40`). |
| rpcd/ubus integration pattern in the existing OpenWrt package | CONFIRMED IN SOURCE, NOT BASELINE-INSTALLED | LuCI package reloads rpcd (`zapret-openwrt/luci-app-zapret/Makefile:17-24`), declares rpcd ACLs for UCI, `service.list`, and init actions (`zapret-openwrt/luci-app-zapret/root/usr/share/rpcd/acl.d/luci-app-zapret.json:24-28`), and calls those ubus objects (`zapret-openwrt/luci-app-zapret/htdocs/luci-static/resources/view/zapret/tools.js:66-85`). |
| shell JSON through jshn | CONFIRMED IN SOURCE, NOT BASELINE-INSTALLED | `zapret-openwrt/zapret/update-pkg.sh:39` sources `/usr/share/libubox/jshn.sh`; lines 306-335 parse and traverse JSON. |
| ucode itself | **UNKNOWN** | No installed-package list or ucode binary/module inventory exists in `router-baseline/runtime`. `comfunc.sh` only conditionally checks `/usr/share/ucode/luci/...` (`zapret-openwrt/zapret/comfunc.sh:218-225`); this is not proof that the target contains it. |

### ucode modules requested by the audit

| Capability | Baseline result |
|---|---|
| filesystem (`fs`) | **UNKNOWN installed**. Local package source expects the ucode `fs` import for `stat` (`zapret-openwrt/zapret/comfunc.sh:232-236`), but guards the files as optional at lines 224-225. |
| JSON | **UNKNOWN** for ucode. Only shell/jshn JSON is locally evidenced (`update-pkg.sh:39,306-335`). |
| UCI | **UNKNOWN** for `ucode-mod-uci`. UCI CLI use is confirmed, not the ucode module. |
| ubus | **UNKNOWN** for `ucode-mod-ubus`. rpcd ACLs and LuCI JavaScript ubus calls are confirmed, not the ucode module. |
| process execution | **UNKNOWN** for a ucode API/module. LuCI `fs.exec()` calls are mediated by rpcd ACLs (`tools.js:113,178`; ACL lines 5-22) and do not prove server-side ucode process APIs. |

LuCI presence or its transitive dependency on ucode is therefore **not confirmed**. The local `luci-app-zapret` manifest declares only `LUCI_DEPENDS:=+zapret` (`zapret-openwrt/luci-app-zapret/Makefile:13-15`) and delegates unspecified framework dependencies to `luci.mk` at line 30, whose contents are outside the captured sources.

## 2. Сравнение ucode, shell и Lua

| Runtime | Suitable responsibilities | Limits from this audit |
|---|---|---|
| ucode under rpcd | Typed ubus methods, JSON validation/serialization, UCI access, bounded event reads, deterministic rendering of `preload.lua` and `active.opt`. | Correct primary choice only with explicit package dependencies. All requested target ucode modules remain unverified in the baseline. |
| POSIX shell | One narrow privileged transaction: acquire an atomic `mkdir` lock, install same-directory temporary files with `mv`, call the existing init script, and unwind on failure. | Do not put schema logic or large JSON transformations here. No `flock` evidence exists; do not depend on it. |
| jshn/jsonfilter | Emergency shell-side parsing of small JSON/status values. jshn use is demonstrated locally. | Not the primary state engine; complex validation and generation become fragile. `jsonfilter` presence is **UNKNOWN**. |
| Lua outside nfqws2 packet path | None in the selected control plane. | No standalone Lua interpreter/package is captured. The only confirmed Lua runtime is nfqws2 compatibility level 5 (`docs/current-state.md:12-16`), which must remain packet-runtime-only. |

## 3. Выбранная архитектура

Choose an **rpcd ucode object as the control-plane authority**, backed by one minimal POSIX shell transaction wrapper. The package must declare every runtime dependency directly and must not rely on LuCI pulling ucode transitively.

Flow: rpcd/ucode reads UCI and JSON state, validates it, reads a bounded amount of `events.ndjson`, and renders candidate files in the destination filesystem. The shell wrapper serializes apply operations, atomically renames candidates, invokes the existing `/etc/init.d/zapret2`, and returns a bounded result to ucode. LuCI, if later added, is only an ubus client; Python is absent from the router.

Feasibility by operation:

| Operation | Decision |
|---|---|
| Atomic JSON replacement | YES, architecturally: write a unique temporary file in the same directory and rename it over the destination. Existing LuCI code demonstrates the temp-plus-`mv -f` pattern (`tools.js:541-564`). Crash-durable `fsync` semantics are **UNKNOWN** from local sources. |
| Parallel-operation lock | YES without extra package: shell `mkdir` lock directory, PID owner file, `trap` cleanup, stale-owner check through `/proc`/`kill -0`. PID inspection through `/proc/<pid>` is already used (`zapret-openwrt/zapret/comfunc.sh:37-59`). `flock` must not be assumed. |
| Read PID | YES in source design: procd is configured to write `/var/run/nfqws2_<instance>.pid` (`zapret2-core/init.d/openwrt/zapret2:40,47-58`). The actual installed original init file and active instance names were not captured, so target PID filenames remain **UNKNOWN** until verified. |
| Call `/etc/init.d/zapret2` | YES. The captured wrapper delegates start/restart to the original init (`router-baseline/etc/init.d/zapret2:77-87`) and must be called rather than replaced. |
| Generate `preload.lua` and `active.opt` | YES as deterministic text generation in ucode plus atomic installation by shell. Consumption of `active.opt` is **BLOCKED/UNKNOWN** because the exact installed daemon argv/config assembly was not captured (`docs/current-state.md:79-84`). |
| Read bounded `events.ndjson` | YES if `ucode-mod-fs` and ucode JSON are installed: enforce byte and record caps before parsing each line. Without those verified modules, only a less desirable shell+jshn implementation is available. |

## 4. Точные зависимости пакета

The future backend package must declare these direct dependencies rather than infer them from LuCI:

| Package dependency | Purpose | Locally proven installed? |
|---|---|---|
| `rpcd` | ubus object host and ACL enforcement | UNKNOWN on baseline; source package expects `/etc/init.d/rpcd` (`luci-app-zapret/Makefile:22-24`). |
| `rpcd-mod-ucode` | load ucode rpcd objects | UNKNOWN. |
| `ucode` | language runtime and core JSON support | UNKNOWN. |
| `ucode-mod-fs` | bounded reads, temporary files, rename/stat operations | UNKNOWN; only source-level `fs` import evidence exists. |
| `ucode-mod-uci` | direct UCI access | UNKNOWN. |
| `ucode-mod-ubus` | service/status ubus calls when needed | UNKNOWN. |
| `libubox` | `jshn.sh` fallback used by the shell environment | UNKNOWN installed; exact path is assumed by `update-pkg.sh:39`. |
| `jsonfilter` | optional bounded extraction in the wrapper only | UNKNOWN; omit unless implementation proves it necessary. |

No Python, standalone Lua, GNU coreutils, or `flock` dependency is permitted. Process execution must initially be isolated in the shell wrapper; no additional ucode process package is required by this architecture. If later implementation insists on direct ucode spawning/captured output, its exact API and providing package must first be verified and added explicitly.

## 5. Точные будущие файлы backend

These names define the minimal future package; none is created in this audit:

- `openwrt/zapret2-orchestra/Makefile` — package manifest with the dependencies above.
- `openwrt/zapret2-orchestra/root/usr/share/rpcd/ucode/zapret2-orchestra` — rpcd ucode object.
- `openwrt/zapret2-orchestra/root/usr/share/rpcd/acl.d/zapret2-orchestra.json` — least-privilege ubus ACL.
- `openwrt/zapret2-orchestra/root/usr/libexec/zapret2-orchestra-apply` — POSIX shell transaction wrapper.
- `/etc/zapret2-orchestra/*.json` — persistent schema-v1 state already represented by local seed files; updates use same-directory temporary files.
- `/tmp/zapret2-orchestra/preload.lua` and `/tmp/zapret2-orchestra/active.opt` — generated runtime artifacts.
- `/tmp/zapret2-orchestra/events.ndjson` — bounded runtime input, read-only to the backend.
- `/tmp/zapret2-orchestra.apply.lock/` — transient transaction lock directory containing an owner PID.

The exact rpcd ucode installation filename convention is **UNKNOWN from the captured local sources**; `/usr/share/rpcd/ucode/zapret2-orchestra` is the selected package contract and must be checked against the target OpenWrt feed before implementation.

## 6. Что остаётся в nfqws2 Lua

Only packet-path behavior remains in nfqws2 Lua: in-memory strategy state, detectors, strategy selection, preload consumption during initialization, and bounded transition-event append under `/tmp`. It must not write persistent JSON or invoke UCI, ubus, rpcd, shell, or service actions. This separation is already required by `docs/remittor-runtime-contract.md:223-235` and the confirmed safety state in `docs/checkpoints/latest.md:26-32`.

## 7. Что выполняет rpcd/ucode

- Expose narrow ubus methods for status, reading state/events, validating candidate state, rendering artifacts, and requesting an apply transaction.
- Enforce input schemas, path allowlists, byte/record limits, and structured errors.
- Read UCI through `ucode-mod-uci`, service state through ubus when available, and JSON/files through ucode core plus `ucode-mod-fs`.
- Render candidate JSON, `preload.lua`, and `active.opt` without executing user-provided text.
- Never edit `/etc/init.d/zapret2` or upstream Lua.

## 8. Что выполняет shell wrapper

- Acquire `/tmp/zapret2-orchestra.apply.lock` with atomic `mkdir`; record `$$`; reject a live owner and clean only a demonstrably stale lock.
- Validate that every source and destination is a fixed allowlisted path.
- Replace files using temporary files on the same filesystem and `mv`; keep rollback copies for the transaction.
- Call only fixed actions of the existing `/etc/init.d/zapret2` (initially `restart` after validation), then report exit status and bounded diagnostics.
- Restore previous artifacts if restart/health validation fails. Exact health checks cannot yet be specified because active argv, instance names, and a successful `ubus service list` capture are missing.

## 9. Неизвестные данные и блокеры

- Installed package manager inventory and exact package versions: **UNKNOWN**; baseline contains no `opkg status/list-installed` or `apk info` capture.
- Whether ucode, `rpcd-mod-ucode`, `ucode-mod-fs`, `ucode-mod-uci`, and `ucode-mod-ubus` are installed: **UNKNOWN**.
- Whether LuCI is installed on the target and which LuCI package supplies ucode runtime dependencies: **UNKNOWN**. Conditional source paths are not installation proof.
- Exact ucode JSON, filesystem, rename, and process APIs on OpenWrt 25.12.5: **UNKNOWN from local sources**.
- `jsonfilter` and `flock` availability: **UNKNOWN**; architecture does not rely on `flock` and treats `jsonfilter` as optional.
- Exact installed `/opt/zapret2/init.d/openwrt/zapret2`, procd instance names, PID files, daemon argv, `active.opt` integration point, NFQUEUE number, and bypass flag: **UNKNOWN/not captured** (`docs/current-state.md:75-84`).
- The captured `router-baseline/runtime/ubus-zapret2.json:1` contains only a failed malformed call, so it does not validate the service object or response schema.
- Robust post-restart health criteria and rollback trigger cannot be finalized until service/argv artifacts are captured.

Functions that cannot be implemented as selected without additional packages are: rpcd ucode methods without `rpcd-mod-ucode`; direct filesystem/bounded-event operations without `ucode-mod-fs`; direct UCI without `ucode-mod-uci`; direct ubus calls without `ucode-mod-ubus`. Direct process capture from ucode is deliberately excluded until its provider/API is verified. The shell wrapper, jshn fallback, and existing init invocation do not justify omitting these declared dependencies from the primary design.

## 10. Один минимальный следующий этап реализации

After a separate read-only capture confirms the package inventory and ucode module APIs, implement only the package skeleton plus one read-only rpcd `capabilities` method. It should report dependency/API availability and bounded service metadata, perform no state writes, generate no runtime files, and never call `/etc/init.d/zapret2`. Do not begin apply, rollback, CLI, or LuCI work in that stage.
