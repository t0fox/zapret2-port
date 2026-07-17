# OpenWrt control-plane runtime decision

**Audit date:** 2026-07-17  
**Scope:** the read-only backend was manually deployed and dynamically
verified; package `0.1.0` has not been built with an SDK or installed as a
package. The Zapret2 runtime was not changed.

## 1. Подтверждённые компоненты OpenWrt

| Компонент | Статус | Локальное доказательство |
|---|---|---|
| POSIX/BusyBox-compatible `/bin/sh` service path | CONFIRMED | `router-baseline/etc/init.d/zapret2:1`; wrapper invokes `/bin/sh /etc/rc.common` at lines 42, 57, 74, 80, 86. |
| procd service supervision | CONFIRMED | Baseline wrapper sets `USE_PROCD=1` (`router-baseline/etc/init.d/zapret2:4`). Upstream OpenWrt init opens procd instances and assigns PID files (`zapret2-core/init.d/openwrt/zapret2:47-58`). |
| UCI command-line use | CONFIRMED | Baseline wrapper writes and commits UCI (`router-baseline/etc/init.d/zapret2:38-40`). |
| rpcd/ubus integration pattern in the existing OpenWrt package | CONFIRMED IN SOURCE, NOT BASELINE-INSTALLED | LuCI package reloads rpcd (`zapret-openwrt/luci-app-zapret/Makefile:17-24`), declares rpcd ACLs for UCI, `service.list`, and init actions (`zapret-openwrt/luci-app-zapret/root/usr/share/rpcd/acl.d/luci-app-zapret.json:24-28`), and calls those ubus objects (`zapret-openwrt/luci-app-zapret/htdocs/luci-static/resources/view/zapret/tools.js:66-85`). |
| shell JSON through jshn | CONFIRMED IN SOURCE, NOT BASELINE-INSTALLED | `zapret-openwrt/zapret/update-pkg.sh:39` sources `/usr/share/libubox/jshn.sh`; lines 306-335 parse and traverse JSON. |
| ucode itself | CONFIRMED INSTALLED | Dynamically verified on the target. |

### ucode modules requested by the audit

| Capability | Baseline result |
|---|---|
| filesystem (`fs`) | `ucode-mod-fs` is installed and used for read-only `stat`. |
| JSON | ucode is installed; no separate JSON module is required by this backend. |
| UCI | `ucode-mod-uci` is installed and used only with `cursor().get_all('zapret2', 'config')`. |
| ubus | `ucode-mod-ubus` is installed, but is not imported or depended on by this backend. |
| process execution | **UNKNOWN** for a ucode API/module. LuCI `fs.exec()` calls are mediated by rpcd ACLs (`tools.js:113,178`; ACL lines 5-22) and do not prove server-side ucode process APIs. |

LuCI is not part of this package and no LuCI dependency is inferred.

## 2. Сравнение ucode, shell и Lua

| Runtime | Suitable responsibilities | Limits from this audit |
|---|---|---|
| ucode under rpcd | Read-only `status` and `validate`, filesystem checks, and UCI reads. | Package has explicit dependencies; it does not import ubus. |
| POSIX shell | Package lifecycle restarts rpcd after install or removal. | It does not call Zapret2 or modify UCI. |
| jshn/jsonfilter | Not used. | Not a package dependency. |
| Lua outside nfqws2 packet path | None in the selected control plane. | No standalone Lua interpreter/package is captured. The only confirmed Lua runtime is nfqws2 compatibility level 5 (`docs/current-state.md:12-16`), which must remain packet-runtime-only. |

## 3. Выбранная архитектура

The implemented package is a narrow rpcd ucode object with only `status` and
`validate`. It performs filesystem checks and a read-only UCI read; it has no
shell apply wrapper, state storage, or service-control API.

Feasibility by operation:

| Operation | Decision |
|---|---|
| Read PID | `status` verifies `/var/run/nfqws2_1.pid` and its `/proc/<pid>/exe` link. |
| Validate installation | `validate` checks the init script, required UCI options, nfqws2, and upstream Lua files. |
| Restart rpcd | Package lifecycle scripts restart only rpcd on a real target. |

## 4. Точные зависимости пакета

The current package declares these direct runtime dependencies:

| Package dependency | Purpose | Locally proven installed? |
|---|---|---|
| `zapret2` | Provides the checked runtime files | confirmed installed |
| `rpcd` | ubus object host and ACL enforcement | confirmed installed |
| `rpcd-mod-ucode` | Loads ucode rpcd objects | confirmed installed |
| `ucode` | ucode runtime | confirmed installed |
| `ucode-mod-fs` | Read-only filesystem checks | confirmed installed |
| `ucode-mod-uci` | Read-only UCI access | confirmed installed |

No Python, Lua package, `ucode-mod-ubus`, `jsonfilter`, `flock`, `curl`, or nftables dependency is used.

## 5. Точные файлы backend

The minimal package contains only these installed files:

- `openwrt/zapret2-orchestra/Makefile` — package manifest with the dependencies above.
- `openwrt/zapret2-orchestra/files/usr/share/rpcd/ucode/zapret2.orchestra` — rpcd ucode object.
- `openwrt/zapret2-orchestra/files/usr/share/rpcd/acl.d/zapret2-orchestra.json` — least-privilege ubus ACL.

The installation paths are `/usr/share/rpcd/ucode/zapret2.orchestra` and
`/usr/share/rpcd/acl.d/zapret2-orchestra.json`.

## 6. Что остаётся в nfqws2 Lua

Only packet-path behavior remains in nfqws2 Lua: in-memory strategy state, detectors, strategy selection, preload consumption during initialization, and bounded transition-event append under `/tmp`. It must not write persistent JSON or invoke UCI, ubus, rpcd, shell, or service actions. This separation is already required by `docs/remittor-runtime-contract.md:223-235` and the confirmed safety state in `docs/checkpoints/latest.md:26-32`.

## 7. Что выполняет rpcd/ucode

- Exposes only `status` and `validate`.
- Reads UCI through `ucode-mod-uci` and filesystem metadata through `ucode-mod-fs`.
- Never writes UCI, edits `/etc/init.d/zapret2`, or modifies upstream Lua.

## 8. Lifecycle scripts

- `postinst` and `postrm` restart `/etc/init.d/rpcd` only when `IPKG_INSTROOT` is empty.
- They do not start, stop, enable, or otherwise change Zapret2.

## 9. Неизвестные данные и блокеры

- `router-baseline/` remains local untracked evidence rather than a package input.
- The package has not been built with a real OpenWrt SDK or installed from such a build.
- The public package license remains to be selected.
- The TLS Lua prototype is not connected to nfqws2 or deployed.

## 10. Один минимальный следующий этап реализации

Do not expand the backend beyond `status` and `validate` until a separately scoped change is approved.
