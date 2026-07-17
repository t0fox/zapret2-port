# Router ↔ Desktop Orchestra compatibility matrix

**Audit date:** 2026-07-17 (post-import)  
**Router evidence:** `router-baseline/`  
**Desktop evidence:** `reference/desktop-orchestra/`  
**Status values:** IDENTICAL | COMPATIBLE | REQUIRES ADAPTER | UNAVAILABLE | CONTRADICTED | UNKNOWN | NOT CAPTURED

---

## 1. Upstream Lua comparison summary

| File | Router | Desktop reference | Verdict |
|------|--------|-------------------|---------|
| `zapret-auto.lua` | 549 lines | 549 lines | **IDENTICAL** (same structure; `circular` at L312) |
| `zapret-lib.lua` | `NFQWS2_COMPAT_VER_REQUIRED=5` (L1) | `=6` (L1) | **COMPATIBLE** with drift — router matches nfqws2 `lua_compat_ver 5` |
| `zapret-antidpi.lua` | in baseline | not in reference import | Compare N/A — use router baseline |

### zapret-lib.lua drift (CONFIRMED)

| Aspect | Router | Desktop | Impact |
|--------|--------|---------|--------|
| Compat version | 5 | 6 | Desktop reference **must not** replace router copy |
| Extra helpers | — | `dis_ipsrc`, `dis_ipdst`, `dis_l4_name`, `dis_l4_ports`, `dis_timer_name`, `desync_timer_name` (L764–822) | Orchestra `combined-detector.lua` does **not** reference these — **COMPATIBLE** |
| File name helper | `writeable_file_name` (L1379) | `writable_file_name` (L1438) | Orchestra SLM uses `io.open`, not this helper |

Core orchestration API **unchanged** at same line regions:

```230:235:router-baseline/opt/zapret2/lua/zapret-lib.lua
function orchestrate(ctx, desync)
	if not desync.plan then
		execution_plan_cancel(ctx)
		desync.plan = execution_plan(ctx)
	end
end
```

```312:315:router-baseline/opt/zapret2/lua/zapret-auto.lua
function circular(ctx, desync)
	local function count_strategies(hrec)
		if not hrec.ctstrategy then
```

---

## 2. Compatibility matrix

| Component | Desktop source | Router source | Desktop contract | Router contract | Status | Evidence | Required adapter | Risk | MVP decision |
|-----------|----------------|---------------|------------------|-----------------|--------|----------|------------------|------|--------------|
| **orchestrate** | `reference/.../zapret-lib.lua` L230 | `router-baseline/.../zapret-lib.lua` L230 | Lazy `desync.plan = execution_plan(ctx)` | Same | **IDENTICAL** | Line-aligned | None | — | Use as-is |
| **execution_plan** | C binding via `execution_plan(ctx)` | Same symbol | C populates plan array | Same | **COMPATIBLE** | `orchestrate()` | None | C API assumed same build | Use as-is |
| **circular** | `reference/.../zapret-auto.lua` L312 | `router-baseline/.../zapret-auto.lua` L312 | Rotate `hrec.nstrategy`, execute matching instances | Same | **IDENTICAL** | 549-line parity | None | — | Baseline fallback |
| **circular_quality** | `reference/.../combined-detector.lua` L942 | — | Orchestra orchestrator + SLM | Not loaded | **UNAVAILABLE** | Not in router inventory | Ship in `orchestra-extra/`; register via `--lua-init` + `--lua-desync=circular_quality` | Profile must define `strategy=N` instances | **Required for MVP** |
| **autostate** | `zapret-auto.lua` `automate_host_record` L29 | Same L29 | `autostate[askey][hostkey]` | Same | **IDENTICAL** | Same function | None | In-memory only | Use as-is |
| **hostkey normalization** | `standard_hostkey` + `slm_normalize_hostkey` | `standard_hostkey` on router; SLM absent | NLD + lowercase | `standard_hostkey` available | **COMPATIBLE** | `zapret-auto.lua` L9 | Port `slm_normalize_hostkey` in orchestra-extra | NLD mismatch if args differ | Use `nld=3` per desktop TLS profile |
| **host record** | `automate_host_record` | Same | Per-host table in autostate | Same | **IDENTICAL** | L29–57 | None | — | Use as-is |
| **connection record** | `automate_conn_record` | Same | `desync.track.lua_state.automate` | Same | **IDENTICAL** | L60–65 | None | — | Use as-is |
| **combined detector** | `combined-detector.lua` | — | `combined_failure_detector`, `combined_success_detector` | Not present | **UNAVAILABLE** | Reference only | Copy to orchestra-extra | TLS heuristics tuned on desktop | Port for MVP |
| **silent-drop detector** | Desktop `circular-config.txt` L16 | — | Optional init | Not on router | **UNAVAILABLE** | Not in reference import | Optional MVP omit | — | Defer |
| **SLM** | `strategy-lock-manager.lua` | — | Global `SLM_QUALITY`, `BLOCKED_STRATEGIES`, `slm_*` | Not present | **UNAVAILABLE** | Reference only | Full port + **manual lock adapter** | `lists/` paths in SKIP_PASS loader | Port for MVP |
| **strategy statistics** | `strategy-stats.lua` | — | Wraps `circular_quality` preload | Not present | **UNAVAILABLE** | Reference L159–199 | Replace Python registry with JSON preload in orchestra-extra | Desktop depends on Python for persistence | Adapter: init-only preload |
| **learned state** | `learned-strategies.lua` (generated) | — | `slm_preload_*` at init | Not present | **UNAVAILABLE** | Reference | JSON → Lua preload generator (off-router) | Large file on desktop | Batch persist via `/tmp` events |
| **blocked strategies** | `slm_preload_blocked`, `BLOCKED_STRATEGIES` | — | `[askey][host]={strats}` | Not present | **UNAVAILABLE** | SLM L789+ | Merge default/user at preload | Single table on desktop | Adapter in orchestra-extra |
| **whitelist** | `whitelist.txt` + `--hostlist-exclude` | Router uses `zapret-hosts-user.txt` + `<HOSTLIST>` | Pre-Lua exclusion | hostlist mode active | **REQUIRES ADAPTER** | UCI `MODE_FILTER=hostlist`; desktop `circular-config.txt` L84 | Generate `/tmp/zapret2-orchestra/whitelist.txt`; inject `--hostlist-exclude` when Orchestra enabled | Router hostlist ≠ Orchestra whitelist | MVP implemented locally |
| **manual lock** | `slm_set_locked`, `slm_preload_locked(..., true)` | — | `is_user_lock=true` blocks auto-unlock | Not present | **REQUIRES ADAPTER** | SLM L646–671, L744–766; `circular_quality` L1103 | **`slm_set_user_locked()` wrapper** sets `is_user_lock` | `slm_set_locked` omits flag — CONFIRMED gap | Fix in orchestra-extra only |
| **auto lock** | `slm_should_lock` | — | Threshold lock on best strat | Not present | **UNAVAILABLE** | SLM L530+ | Port SLM | False positive lock | MVP include |
| **unlock** | `circular_quality` + `slm_reset` | — | Auto-unlock after `unlock_fails` unless user lock | Not present | **UNAVAILABLE** | `combined-detector.lua` L1101–1118 | Port with user-lock guard | Flapping | MVP include |
| **Lua registration** | `circular-config.txt` L12–20 `--lua-init=…` | UCI `NFQWS2_OPT` only static desync | Explicit init chain | Init chain **NOT CAPTURED** | **NOT CAPTURED** | Missing `linux_daemons.sh` | Append `--lua-init=@/opt/zapret2/lua/orchestra-extra/init.lua` | Order: lib → antidpi → auto → orchestra-extra | See hook analysis |
| **persistent state** | `settings.json` orchestra section | Separate versioned JSON | JSON on disk | Local backend implemented | **REQUIRES TARGET VALIDATION** | `docs/orchestra-state-schema.md` | `/etc/zapret2-orchestra/*.json` | Flash wear | Backend only |
| **runtime state** | `autostate`, SLM globals | Same autostate potential | In-memory | Same | **COMPATIBLE** | `automate_host_record` | `/tmp/zapret2-orchestra/` | No flash write in nfq path | Implemented locally |

---

## 3. Desktop dependencies on Windows / Python

| Dependency | Location | Router impact | Adapter |
|------------|----------|---------------|---------|
| Python Orchestra GUI | Not in repo | None at runtime | Replace with rpcd/ucode (post-MVP) |
| Python-generated `learned-strategies.lua` | `reference/.../learned-strategies.lua` | Cannot use file as-is | JSON + init preload |
| WinDivert filters | `circular-config.txt` L1–9 | **Not applicable** | OpenWrt uses nftables + NFQUEUE |
| `custom_funcs.lua`, blobs `@bin/` | Desktop profile | Strategy instances differ | MVP uses **minimal** TLS strategy set — not full desktop profile |
| `lists/ipset-*.txt` for SKIP_PASS | SLM L294+ | Paths relative to CWD | Map to `/opt/zapret2/ipset/` or embed in JSON |
| `strategy-stats.lua` comment | References Python registry | Persistence off hot path | Event queue |

Orchestra **orchestrator logic** is portable; **profile and persistence plumbing** need OpenWrt adapters.

---

## 4. Previous audit re-verification

| Prior claim | New status | Notes |
|-------------|------------|-------|
| Repo missing baseline | **CONTRADICTED** | Baseline now imported |
| `circular` in upstream | **CONFIRMED** | Router `zapret-auto.lua` L312 |
| `circular_quality` on router | **CONTRADICTED** | Not in router Lua inventory |
| NFQUEUE 300 | **NOT CAPTURED** | Not in `nft-ruleset.txt`; need `firewall.zapret2` |
| POSTNAT=1 | **CONFIRMED** | UCI + `opt/zapret2/config` L14–15 |
| hostlist mode | **CONFIRMED** | UCI L11, `MODE_FILTER=hostlist` |
| IPv6 disabled | **CONFIRMED** | UCI `DISABLE_IPV6=1` |
| Zapret2 0.9.20260307-r1 | **PARTIAL** | nfqws2 0.9.20260307 CONFIRMED; `-r1` NOT CAPTURED |
| `/opt/zapret2/nfq2/nfqws2` | **CONFIRMED** | File inventory L147 |
| Orchestra modules on router | **CONTRADICTED** | Absent — must ship orchestra-extra |
| `slm_set_locked` missing `is_user_lock` | **CONFIRMED** | `strategy-lock-manager.lua` L646–671 |
| Whitelist via hostlist-exclude | **CONFIRMED** (desktop) | `circular-config.txt` L84; router uses `<HOSTLIST>` placeholder |
| Safe extension without upstream patch | **CONFIRMED** | remittor wrapper pattern + separate Lua files |
| lua_compat v5 on router | **CONFIRMED** | **New** — desktop reference is v6, must not overwrite router lib |

---

## 5. orchestra-extra hook classification

| Question | Answer | Status |
|----------|--------|--------|
| Extra Lua via existing config? | **Yes** — append `--lua-init` to `NFQWS2_OPT` or remittor hook (when `DISABLE_CUSTOM=0` or dedicated sync) | **SAFE WITH ADAPTER** |
| Change `/etc/init.d/zapret2`? | **No** — forbidden by AGENTS.md | — |
| Change upstream Lua? | **No** | — |
| Additional `--lua-init`? | **Yes** — after lib/antidpi/auto (desktop order: L12–19 `circular-config.txt`) | **NOT CAPTURED** default router order |
| Order matters? | **Yes** — orchestra-extra requires `circular`, detectors, `orchestrate` | CONFIRMED |
| Symbols required before adapter | `orchestrate`, `automate_*`, `standard_*_detector`, `plan_instance_*`, `VERDICT_PASS`, `DLOG` | CONFIRMED from upstream |
| Adapter exports | `circular_quality`, `combined_*_detector`, `slm_*`, optional `orchestra_extra_init()` | INFERRED |
| Global conflicts | Orchestra defines new globals; must not redefine `circular` unless intentional wrapper | REQUIRES ADAPTER discipline |
| Disable adapter | Restore UCI `NFQWS2_OPT` to remittor default (backup in `/etc/zapret2-orchestra/backup/`) | INFERRED rollback |
| Rollback without uninstall | UCI revert + `zapret2 restart` | INFERRED |

**Classification: SAFE WITH ADAPTER**

---

## 6. Manual lock — adapter design (no code)

### Findings (CONFIRMED from reference)

1. **`slm_set_locked` does NOT set `is_user_lock`** — only `locked_strategy` and `lock_reason` (`strategy-lock-manager.lua` L664–666).
2. **`slm_preload_locked(..., true)` sets `is_user_lock=true`** (L766).
3. **Auto-unlock checks `slm_is_user_lock` only** (`combined-detector.lua` L1103–1106), not `lock_reason`.
4. **Risk:** manual lock via `slm_set_locked` **can be auto-unlocked** after `unlock_fails` — **CONFIRMED** design gap.

### Adapter contract (orchestra-extra)

| Function | Behavior |
|----------|----------|
| `orchestra_set_manual_lock(askey, host, strategy)` | Call SLM internals; set `is_user_lock=true`, `lock_reason="user"` |
| `orchestra_clear_manual_lock(askey, host)` | Clear lock; reset `is_user_lock` |
| Backend/LuCI | Never call raw `slm_set_locked` for user actions |

**Location:** `lua/orchestra-extra/slm-adapter.lua` (new file; do not patch reference SLM in place — wrap or fork with fix).

### LuCI/backend minimum contract

```json
{"askey":"tls","host":"example.com","strategy":5,"action":"manual_lock"}
```

→ preload at init + optional immediate `slm_preload_locked(askey, host, strategy, **true**)**.

---

## 7. MVP verdict

| Verdict | **CONDITIONAL GO** |
|---------|-------------------|
| Allowed now | Port orchestra-extra against **router** `zapret-lib.lua` v5; JSON schema; local Lua tests; UCI overlay design |
| Blocked | Live deploy; NFQUEUE confirmation; exact cmdline; full profile parity with desktop `circular-config.txt` |
| Critical conflict | None — upstream API sufficient; Orchestra modules absent by design |

**First code commit after blockers (recommended):**

1. `lua/orchestra-extra/{init,slm,slm-adapter,detectors,orchestrator}.lua`
2. `etc/zapret2-orchestra/{learned,blocked,whitelist,manual-locks}.json` (empty v1)
3. `tests/lua/test_manual_lock_adapter.lua`
4. `docs/orchestra-uci-overlay.md` (NFQWS2_OPT template — not deploy)

**Remaining captures before router staging:**

- `router-baseline/opt/zapret2/common/linux_daemons.sh`
- `router-baseline/opt/zapret2/init.d/openwrt/firewall.zapret2`
- `router-baseline/runtime/nfqws2-process.txt` (service running)
