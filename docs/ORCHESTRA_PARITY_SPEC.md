# Orchestra parity spec — Zapret2GUI Orchestra → OpenWrt zapret2-orchestra

**Status:** SPEC (read-only reverse-engineering). No production code, no r7 profile, no CI, no
router tests were run for this document. Source review of the ORIGINAL desktop Orchestra
(`youtubediscord/zapret` @ `9d57e55d6751587d9d52b52147a05a0a8fcc9fd8`, the Zapret2GUI Python app)
cross-checked against the existing OpenWrt port (`openwrt/zapret2-orchestra/`) and the imported
original Lua under `reference/desktop-orchestra/lua/`.

**r7 scope note (supersedes §4's "deferred" list):** §1–§3, §5, §7–§8 (the exact original model,
the Windows-GUI→OpenWrt mapping, compatibility risks, test matrix, unresolved questions) are
stable reference and remain authoritative. §4 and §6 were written in a prior session planning r7
as a *static* discord-v5 profile with the closed-loop runtime learner **deferred**. The current
r7 task **supersedes that deferral**: r7 must include the minimal closed-loop Orchestra (importer
→ catalog → per-domain selection → circular rotation → machine-readable events → async learner →
ratings/history → auto-lock/unlock → persistent state → preload regeneration → safe reload →
reuse after restart). The static discord-v5 profile remains valid only as a manual/fallback
profile, not as the Orchestra implementation. Read §4's "r7 minimum" and "deferred" bullets as
the *prior* plan; the live plan is the 7-step directive that committed this spec.

Companion docs (do not duplicate): `docs/orchestra-state-schema.md` (port JSON schema),
`docs/port-map.md` (feature correspondence), `docs/import-completeness.md`,
`docs/backend-runtime-decision.md`. This doc adds: the EXACT original control-flow model
(source-cited), the runtime-learning-loop gap, the Windows-GUI→OpenWrt-daemon/CLI mapping, and
the r7 vertical slice anchored on the proven golden scenario.

---

## 0. Source provenance and method

- ORIGINAL Python app: `H:/zapret-port/strategy-research/zapret` @ 9d57e55 (cloned read-only).
  Key modules reviewed (file:line citations below):
  `src/orchestra/orchestra_runner.py` (1891 LOC), `src/orchestra/commands.py`,
  `src/orchestra/locked_strategies_manager.py`, `src/orchestra/blocked_strategies_manager.py`,
  `src/orchestra/ignored_targets.py`, `src/orchestra/log_parser.py`,
  `src/orchestra/ratings_workflow.py`, `src/orchestra/managed_lists_workflow.py`,
  `src/utils/circular_strategy_numbering.py`, `src/profile/strategy_catalog.py`,
  `src/profile/user_profiles/service.py`, `src/presets/builtin/winws2/Default (circular).txt`,
  `src/profile/strategy_catalogs/winws2/tcp.txt`.
- ORIGINAL Lua (imported into the port repo): `reference/desktop-orchestra/lua/`:
  `strategy-lock-manager.lua`, `strategy-stats.lua`, `combined-detector.lua`,
  `learned-strategies.lua`, `zapret-auto.lua`, `zapret-lib.lua`. (The GUI repo itself does NOT
  ship the Lua; it lives in the desktop zapret runtime dir `/home/privacy/zapret/lua/` referenced
  from `orchestra_runner.py:211`. The port already imported it under `reference/`.)
- PORT: `openwrt/zapret2-orchestra/files/` — `opt/zapret2/lua/orchestra-extra/{init,slm,slm-adapter,
  events,detectors,orchestrator}.lua`, `usr/share/zapret2-orchestra/{apply,generate-preload}.uc`,
  `usr/sbin/zapret2-orchestra-{profile,apply,preload,blockcheck}`, profiles `*.opt`,
  seeds `etc/zapret2-orchestra/*.json`.
- Method: every claim about the original is cited as `file:line` from the cloned 9d57e55 tree or
  the imported `reference/` Lua. Graphify was attempted for call-graph fan-out but the background
  subagents failed (model unavailable in this environment); all conclusions are confirmed by
  direct source reads.

---

## 1. Exact original model

### 1.1 What Orchestra IS (and is not)

Orchestra is a **circular strategy-rotation engine with passive runtime learning**, layered on
top of nfqws2/winws2. It is NOT the batch `blockcheck test-all` scan (that is a separate
bootstrap/diagnostic in `src/blockcheck/`). Orchestra's job: for each blocked domain, try
candidate desync strategy-chains in rotation, detect success/failure per flow from the nfqws2
debug log, lock the winning strategy after repeated successes, unlock/re-learn after repeated
failures, and persist that learned state so the winner is preloaded on next start.

Class docstring (`orchestra_runner.py:186-196`): "Runner для circular оркестратора с
автоматическим обучением … Детекция: RST injection + silent drop + SUCCESS по байтам (2KB) …
LOCK после 3 успехов на одной стратегии … UNLOCK после 2 failures … Группировка субдоменов".

### 1.2 The 9 ASKEY protocol profiles

`locked_strategies_manager.py:47` (and mirrored at `blocked_strategies_manager.py:41`):

```python
ASKEY_ALL = ["tls","http","quic","discord","wireguard","mtproto","dns","stun","unknown"]
TCP_ASKEYS = ["tls","http","mtproto"]          # keyed by hostname
UDP_ASKEYS = ["quic","discord","wireguard","dns","stun","unknown"]  # keyed by IP or hostname
PROTO_TO_ASKEY = {"tls":"tls","http":"http","udp":"quic","unknown":"unknown","quic":"quic",
                  "discord":"discord","wireguard":"wireguard","mtproto":"mtproto","dns":"dns","stun":"stun"}
```

`askey` = protocol-profile key. l7proto from the packet → askey via `PROTO_TO_ASKEY`
(`orchestra_runner.py:992`, `locked_strategies_manager.py:56`). For UDP, an IP→ipset-label
lookup (`_load_ipset_networks`/`_resolve_ipset_label`, `orchestra_runner.py:1817-1866`) maps a
destination IP to a service label (discord/youtube/…) using the ipset-*.txt subnets — this is the
"ASKEY grouping" for UDP. TCP is keyed purely by hostname (subdomain-grouped).

### 1.3 Strategy chain, strategy=N, and the circular selector

**Strategy chain** = one or more `--lua-desync=<fn>:...` invocations that run together as a unit.
In a circular preset, the first desync line is the SELECTOR and every subsequent desync line is a
numbered strategy in the rotation pool. Concretely, `src/presets/builtin/winws2/Default (circular).txt:107-126`:

```
--lua-desync=circular:fails=3:retrans=3:maxseq=8192:inseq=2048:nld=3
--lua-desync=send:repeats=2:strategy=1 --lua-desync=syndata:blob=stun_pat:strategy=1 --lua-desync=hostfakesplit_multi:...:strategy=1
--lua-desync=hostfakesplit_multi:...:strategy=2
--lua-desync=tls_multisplit_sni:seqovl=652:seqovl_pattern=tls_google:strategy=3
--lua-desync=send:repeats=2:strategy=4 --lua-desync=syndata:blob=stun_pat:strategy=4 --lua-desync=tls_multisplit_sni:...:strategy=4
... strategy=5 .. strategy=19
```

- The `circular` (or `circular_quality`) line is the **selector**; it is NOT numbered
  (`circular_strategy_numbering.py:5,43-48`).
- Each subsequent `--lua-desync=` gets `:strategy=N`; N increments per chain, resets on `--new`
  and on a new `--payload=` group (`circular_strategy_numbering.py:34-56`).
- **Multiple `--lua-desync=` on the same logical line share the same `:strategy=N`**
  (`circular_strategy_numbering.py:58-68`) — that is exactly "one strategy chain = several
  desync functions executed together". E.g. `strategy=1` above is the chain
  `send → syndata → hostfakesplit_multi`.
- At runtime the Lua selector (`circular`/`circular_quality` in
  `reference/desktop-orchestra/lua/strategy-stats.lua` + the port's
  `orchestra-extra/orchestrator.lua:28 circular_quality`) picks one `strategy=N` per
  connection-record, records success/failure via detectors, and locks/unlocks accordingly.

Selector args (`Default (circular).txt:107`): `fails=3` (failures → unlock), `retrans=3`
(retransmission threshold), `maxseq/inseq/nld` (detector window params). The port's
`gui-circular.opt` uses the richer `circular_quality:key=tls:fails=1:failure_detector=
combined_failure_detector:success_detector=combined_success_detector:lock_successes=3:
unlock_fails=3:lock_tests=5:lock_rate=0.6:inseq=0x1000:nld=3`.

**Strategy-chain catalog** (`src/profile/strategy_catalogs/winws2/{tcp,udp,http80,voice}.txt`,
parsed by `src/profile/strategy_catalog.py:_parse_catalog_file:46`): INI-style `[strategy_id]`
sections, each with `name` (+ optional `author`/`description`) and one or more `--lua-desync=`
lines. Each section = one chain. Sample (`tcp.txt`):

```
[hostfakesplit_multi_syndata]
name = hostfakesplit_multi + syndata
--lua-desync=send:repeats=2
--lua-desync=syndata:blob=stun_pat
--lua-desync=hostfakesplit_multi:hosts=google.com,vimeo.com:tcp_ts=-1000:tcp_md5:repeats=2
```

**Preset selection → chains**: `src/profile/user_profiles/service.py:_first_strategy_lines:164`
loads the catalog for the chosen engine+protocol and emits the catalog entries' `--lua-desync`
lines into the profile. A "preset" is a curated set of these chains plus the selector, rendered
into one NFQWS2_OPT-style config (`circular-config.txt` on desktop). So: **preset → (selector +
catalog chains) → renumber → `:strategy=N` → circular rotation pool**.

### 1.4 Core state machine (per askey, per host)

States for a single (askey, hostname) target, driven by Lua-emitted events parsed in
`_read_output` (`orchestra_runner.py:917-1288`):

```
                ┌─────────────┐  SUCCESS x lock_threshold   ┌─────────┐
   start ──────▶│  LEARNING   │ ───────────────────────────▶│ LOCKED  │
                │ (rotating)  │◀─────────────────────────── │ (winner)│
                └──────┬──────┘   UNLOCK (Lua, fails=3)      └────┬────┘
                       │ FAIL                                         │ FAIL x unlock_fails
                       ▼                                              ▼ (re-learn)
                 record history                                 UNLOCK → LEARNING
```

- `lock_threshold = 1 if is_udp else 3` (`orchestra_runner.py:1156`) — auto-LOCK after 3 successes
  (1 for UDP). Matches the class docstring "LOCK после 3 успехов".
- UNLOCK is emitted by Lua after `fails=3` consecutive failures on the locked strategy; Python
  removes the host from ALL askey profiles (`orchestra_runner.py:1022-1057`) → re-learning.
- User locks (`user_locked_by_askey`) are NEVER overwritten by auto-lock/auto-unlock
  (`orchestra_runner.py:995-997,1038-1041`, `locked_strategies_manager.py:343-358`).
- Blocked strategies are skipped during rotation (Lua `install_blocked_filter` wrapper in
  `_generate_learned_lua:878-904`; port equivalent `orchestra-extra/orchestrator.lua:47-50`).

Event types (`log_parser.py:165-182` `EventType`, patterns at `log_parser.py:70-160`):
`LOCK`, `UNLOCK`, `RESET`, `APPLIED`, `SUCCESS` (two sources: `slm_quality … SUCCESS s/t` at
`log_parser.py:98` and `standard_success_detector` at `log_parser.py:132`), `FAIL`
(`log_parser.py:108` + `udp_aggressive_failure_detector:128`), `ROTATE`
(`circular: rotate strategy to N` `log_parser.py:115`, `circular_quality: rotate to strategy N`
`log_parser.py:160`), `RST` (`standard_failure_detector: incoming RST` `log_parser.py:122`),
`HISTORY` (`HISTORY host sN successes=… failures=… rate=%` `log_parser.py:111`),
`PRELOADED` (`log_parser.py:75`), `HOSTKEY`, `CACHED_PROFILE`, `TCP/UDP_PROFILE_SEARCH`.

### 1.5 Data model (original, persisted in settings.json)

All Orchestra state lives in the desktop app's `settings.json` via `settings.store` helpers
(`locked_strategies_manager.py:27-42`, `blocked_strategies_manager.py:30-36`). Fields:

| Field | Shape | Owner | Where read/written |
|---|---|---|---|
| `locked_by_askey` | `{askey: {hostname: strategy:int}}` | LockedStrategiesManager | `locked_strategies_manager.py:85` |
| `user_locked_by_askey` | `{askey: set(hostname)}` (manual locks, protected) | LockedStrategiesManager | `:88` |
| `strategy_history` | `{hostname: {str(strategy): {successes:int, failures:int}}}` | LockedStrategiesManager | `:91` |
| `blocked_by_askey` | `{askey: {hostname: [strategy:int]}}` | BlockedStrategiesManager | `blocked_strategies_manager.py:144` |
| `user_blocked_by_askey` | `{askey: {hostname: set(strategy)}}` | BlockedStrategiesManager | `:148` |
| `DEFAULT_BLOCKED_PASS_DOMAINS` | static set of ~60 domains (discord.com, youtube.com, google.com, …) | BlockedStrategiesManager | `blocked_strategies_manager.py:65-102` |
| whitelist | `DEFAULT_WHITELIST_DOMAINS` (system) + user domains | OrchestraRunner | `orchestra_runner.py:1707-1815` |
| ignored_targets | Telegram proxy relay domains (never train/lock/block) | ignored_targets.py | `ignored_targets.py:77-91` |

Rating is **derived, not stored**: `rate = successes/(successes+failures)*100`
(`locked_strategies_manager.py:433,530,573`). `get_best_strategy_from_history(host, exclude)`
(`:538-579`) returns the highest-rate non-blocked strategy.

`DEFAULT_BLOCKED_PASS_DOMAINS` is applied at load: `strategy=1` (pass) is blacklisted for those
domains on TLS (`blocked_strategies_manager.py:193-197,316`). **discord.com is in this set**
(`:67`) — so on the original, `strategy=1` is never tried for discord.com.

### 1.6 Runtime signals (Lua→Python contract)

Lua (`reference/desktop-orchestra/lua/strategy-stats.lua` + `strategy-lock-manager.lua` +
`combined-detector.lua`) emits `--debug=1` stdout lines that Python parses
(`orchestra_runner.py:919 parser = LogParser()`):

- success: `LUA: standard_success_detector: …successful` (`log_parser.py:132`) and
  `slm_quality: [askey] host strat=N SUCCESS s/t` (`:98`).
- failure: `standard_failure_detector: incoming RST` (`:122`),
  `standard_failure_detector: retransmission n/m` (`:125`),
  `udp_aggressive_failure_detector: FAIL` (`:128`),
  `slm_quality: [askey] host strat=N FAIL s/t` (`:108`).
- lifecycle: `slm_quality: [askey] LOCK: host -> strat=N` (`:80`),
  `slm_quality: [askey] UNLOCK: host` (`:85`), `slm_quality: RESET host` (`:88`),
  `circular[_quality]: rotate to strategy N` (`:115,160`), `strategy-stats: APPLIED host = strategy N` (`:70`),
  `strategy-stats: PRELOADED host = strategy N` (`:75`),
  `HISTORY host sN successes=… failures=… rate=%` (`:111`),
  `strategy-stats: UNSTICKY host` (`:155`).

Python reaction (`_read_output`):
- SUCCESS → `increment_history(host, strat, is_success=True)` (`:1099,1132`) → auto-lock at
  threshold (`:1156-1175`); history saved every 5 (`:1120-1122`).
- FAIL → `increment_history(…, is_success=False)` (`:1193`); Discord consecutive-fail counter →
  app restart callback (`:1211-1221`).
- LOCK → set `locked_by_askey[askey][host]=strat` + save (`:1010-1019`), skip if blocked/user-locked.
- UNLOCK → remove host from all askey profiles + save (`:1034-1056`), skip user-locks.
- HISTORY → `update_history` full replace + save (`:1274,1279`).
- ROTATE/APPLIED/RST/PRELOADED → UI messages only.

### 1.7 Persistence + crash recovery

- Persistence: `save()`/`save_history()` write to `settings.json` (locked/user-locked/history)
  (`locked_strategies_manager.py:241,475`); `BlockedStrategiesManager.save()` writes only
  user-blocked (`blocked_strategies_manager.py:239`).
- Generated runtime artifact: `_generate_learned_lua` (`orchestra_runner.py:742-912`) writes
  `learned-strategies.lua` containing `slm_preload_blocked/locked/history(askey,host,…)` calls
  + `install_circular_wrapper()` + `install_blocked_filter()` (wraps the Lua `circular` fn to
  apply preloaded locks and skip blocked strategies). This file is passed to winws2 via
  `--lua-init=@learned-strategies.lua` at start (`orchestra_runner.py:1497-1500`).
- Crash recovery: `_read_output` `finally` block (`:1294-1343`) saves history on thread exit and,
  if the process died while not stop-requested, records `last_exit_info` {exit_code, uptime_sec,
  reason, timestamp, config_path, command, recent_output} via `_guess_start_failure_reason`
  (`:498`) + `_build_startup_diagnostics` (`:347`). Startup diagnostics forwarded to UI
  (`_emit_startup_diagnostics:388`).
- Log history: rotated debug logs with id/path/timestamp/size; `get_log_history/delete_log/
  clear_all_logs` (`:623-722`); max size truncation (`:958-963`).

### 1.8 Runner lifecycle (`orchestra_runner.py`)

`start()` (`:1425`): is_running check → `prepare()` (`:1344`, verifies lua/exe/config) →
`load_existing_strategies()` (`:730`, loads locked/blocked/history/whitelist from settings.json,
runs `_clean_blocked_conflicts` `:187` to drop locked==blocked conflicts, blocked wins) →
init `_success_counts` from history (resume counters) → gen `learned-strategies.lua` → spawn
`winws2 @circular-config.txt --lua-init=@learned-strategies.lua --debug=1` → start `_read_output`
thread. `stop()` (`:1580`), `restart()` (`:1628`), `is_running()` (`:1658`), `get_pid()` (`:1664`).
`clear_learned_data()` (`:1674`) → `locked_manager.clear()` (`:360`, clears locked/user-locked/
history).

### 1.9 Manual actions (commands.py + managed_lists_workflow.py)

`commands.py` is a thin facade (`create_loaded_locked_manager`, `create_loaded_blocked_manager`,
`is_default_blocked_pass_domain`, whitelist snapshot/add/remove/clear, `set_setting`).
Manual actions live in `managed_lists_workflow.py`:
`change_blocked_strategy` (`:158`), `add_blocked_strategy` (`:169`), `remove_blocked_strategy`
(`:182`), `clear_user_blocked_strategies` (`:211`), `current_locked_strategy` (`:224`),
`change_locked_strategy` (`:230`). Underlying mutators: `LockedStrategiesManager.lock/unlock`
(`:263,306`, `user_lock=True` for manual), `BlockedStrategiesManager.block/unblock`
(`:384,433`). **blocked has PRIORITY over locked including user-lock**
(`blocked_strategies_manager.py:356-382,429-431`); default blocks (s1 for DEFAULT_BLOCKED_PASS_DOMAINS)
cannot be unblocked (`:450`). Clear-learned = `LockedStrategiesManager.clear()` + blocked user-clear.

---

## 2. Sequence diagrams (text)

### 2.1 Start + passive runtime learning
```
GUI/CLI ──start()──▶ OrchestraRunner
  │  load_existing_strategies() ─▶ settings.json ─▶ locked/blocked/history/whitelist
  │  _clean_blocked_conflicts()  (blocked wins, drop locked==blocked; strip s1 for default-blocked)
  │  _generate_learned_lua()     ─▶ learned-strategies.lua (slm_preload_* + install_circular_wrapper + install_blocked_filter)
  │  spawn winws2 @circular-config.txt --lua-init=@learned-strategies.lua --debug=1
  ▼
_read_output thread:
  winws2 stdout ──parse_line──▶ ParsedEvent ──▶ dispatch by EventType
    SUCCESS ─▶ increment_history ─▶ (count>=3) auto-lock + save
    FAIL    ─▶ increment_history ─▶ (discord N fails) restart callback
    LOCK    ─▶ locked_by_askey[askey][host]=strat + save   (skip blocked/user-locked)
    UNLOCK  ─▶ remove host from all askey + save           (skip user-locked)
    HISTORY ─▶ update_history(full) + save
```

### 2.2 Per-packet runtime selection (Lua side)
```
nfqws2 packet (tls_client_hello for host H) ──▶ circular_quality(ctx,desync)
  ctstrategy = count of :strategy=N in plan        (orchestrator.lua:12 strategy_count)
  if slm_is_blocked(askey,H,nstrategy): nstrategy = selected_next(...)
  if locked = slm_get_locked(askey,H): use locked; on repeated fails → unlock event, selected_next
  else rotate nstrategy; record result via slm_record_result(askey,H,nstrategy,success)
  success→ slm_should_lock(askey,H,arg)? lock_successes/lock_rate met → lock + emit LOCK
  emit APPLIED/ROTATE/SUCCESS/FAIL/UNLOCK (orchestra_emit_event → DLOG → stdout → Python)
```

---

## 3. Windows-GUI → OpenWrt daemon/CLI mapping

| Original (Windows GUI, Python) | OpenWrt port target | Port status now | Gap / action |
|---|---|---|---|
| `OrchestraRunner.start()/stop()/restart()` spawn winws2 | `/etc/init.d/zapret2 restart` + `apply.uc enable` | `apply.uc enable` writes NFQWS2_OPT + manager state but does NOT start nfqws2; `/etc/init.d/zapret2` is the service | **Gap:** enable should set NFQWS2_ENABLE=1 + start service (or document the two-step). Original = one start(). |
| `_read_output` runtime event consumer (stdout→state→regen) | (none) | `events.ndjson` is a transition-only Lua sink; no closed-loop consumer | **KEY GAP:** no runtime learning loop. Port = static preload. |
| `_generate_learned_lua` → learned-strategies.lua | `generate-preload.uc generate` → `/tmp/.../preload.lua` + whitelist.txt + manifest.json | PRESENT | preload generation parity OK; but only re-run on explicit CLI, not on runtime events. |
| settings.json (locked/blocked/history/whitelist/user) | `/etc/zapret2-orchestra/{learned,blocked,whitelist,manual-locks}.json` (schema in `docs/orchestra-state-schema.md`) | PRESENT (seeds empty) | schema differs (per-protocol nested) vs original flat; acceptable. |
| `BlockedStrategiesManager` + `DEFAULT_BLOCKED_PASS_DOMAINS` | `blocked.json` + `slm.lua slm_is_blocked` | `slm_is_blocked` PRESENT; **DEFAULT_BLOCKED_PASS_DOMAINS NOT seeded** (blocked.json empty) | **Gap:** port does not blacklist strategy=1 for discord.com/youtube.com/… by default. |
| `LockedStrategiesManager` (auto + user lock, history, get_best) | `slm.lua slm_preload_locked/slm_record_result/slm_get_best/slm_should_lock` + learned.json | Lua PRESENT; Python/ucode mutator for manual lock PRESENT via CLI | parity mostly OK on Lua; manual user-lock `is_user_lock` flag (port-map.md notes the gap). |
| `circular`/`circular_quality` selector + strategy=N | `orchestra-extra/orchestrator.lua circular_quality` + `circular_strategy_numbering.py` logic (port renumber? check) | `circular_quality` PRESENT in `orchestrator.lua`; profiles use `:strategy=N` | renumber helper is Python on desktop; port must renumber in ucode at profile-build time (or ship pre-numbered .opt, which it does). |
| ratings UI (`ratings_workflow.py`) | CLI `zapret2-orchestra-profile status` + rpcd | partial | UI-only rendering; CLI needs a ratings/history dump command. |
| managed-lists UI (block/lock/unblock/change) | CLI commands | partial | needs CLI subcommands: lock/unlock/block/unblock/change/clear. |
| ignored_targets (Telegram proxy) | n/a on router (no telegram_proxy feature) | n/a | **Deferred / drop** — no proxy-relay feature on OpenWrt. |
| log history rotation + viewer | logread / daemon log | partial | router uses logread/syslog; no rotated per-session viewer. |
| strategy catalog winws2/{tcp,udp,http80,voice}.txt | port ships ready `.opt` profiles (gui-*) | PRESENT (6 ready profiles) | catalog→chain→renumber is pre-baked into .opt files; dynamic catalog build not ported. |
| Discord auto-restart on N fails (`:1211`) | n/a (router restarts nfqws2 via service) | n/a | **Deferred** — router has no Discord-client restart concept; map to nfqws2 restart. |

---

## 4. Minimal r7 vertical slice (anchored on the proven golden scenario)

**Proven golden scenario (this project, router-only):**
- Default old (send+syndata:stun_pat+tls_multisplit_sni seqovl=652, circular-style) → **router FAIL**:
  `seqovl cancelled, too large` (652 ≥ SNI pos ~121) + the original would also blacklist strategy=1
  for discord.com. discord.com stayed TIMEOUT 3/3.
- **Default v5 (send:repeats=3 + syndata:blob=tls_google + syndata, ipset-discord.txt,
  init_vars.lua for tls_google) → router PASS**: discord.com HTTP 200 ×3/3, discordapp.com 301
  ×3/3, example.com 200. nfqws2 log: `syndata_1_2/1_3 desync`, `LUA: syndata: 16 03 01 02 A0…`.
  NO circular, NO strategy=N, NO custom Lua function (send+syndata are core).

**Implication for parity:** Default v5 is a **static native nfqws2 strategy** (no circular
rotation, no learning). The Orchestra circular approach (the r5/r6 MVP profile with
`multisplit:strategy=1`) failed for discord.com because (a) `strategy=1` is default-blacklisted
on the original, and (b) a single-strategy pool has nothing to rotate to. Therefore the r7
**minimum** is to ship the *proven working static strategy* as a ready profile, and treat the
closed-loop Orchestra learning as a deferred parity feature.

**r7 minimum (must):**
1. Ship `discord-v5` as a ready native nfqws2 profile (`NFQWS2_OPT` with send+syndata:tls_google+
   syndata, `--ipset=ipset-discord.txt`, `--lua-init=@/opt/zapret2/lua/init_vars.lua`). Add
   `init_vars.lua` (verbatim from the youtubediscord/youtube-discord pin, provides tls_google via
   the nfqws2 builtin `tls_mod`) + `ipset-discord.txt` to the orchestra package; Makefile
   INSTALL_DATA rules. (Artifacts already prepared at
   `H:/zapret-port/strategy-research/port/r7/`.)
2. Relax `apply.uc validate_profile()` (`apply.uc:731`) so a **native nfqws2 profile that does
   not load orchestra-extra/init.lua is NOT required to reference `circular_quality`** (the
   discord-v5 profile uses none). Either (a) gate the circular_quality requirement on
   "profile loads orchestra runtime", or (b) ship a separate native-enable path.
3. `apply.uc enable` (or a new subcommand) must actually activate the datapath: set
   `NFQWS2_ENABLE=1` in `/opt/zapret2/config` + `/etc/init.d/zapret2 restart` (today enable only
   edits NFQWS2_OPT + state; the service start is a separate manual step — that is the parity gap
   from `OrchestraRunner.start()`).
4. Make `discord.com` a **learned/locked winner** for the discord-v5 profile in the port state:
   seed `learned.json` with `protocols.tls.discord.com = {auto_lock: <v5-chain-id>,
   strategies: {…}}` — but since v5 is a static (non-circular) chain there is no `strategy=N` to
   lock. So for r7, "discord.com → Default v5 learned/locked winner" is expressed as: the
   discord-v5 profile is the enabled profile for the discord ipset, persisted in manager-state
   (profile=discord-v5, enabled=true). The auto-LOCK semantics (circular) are deferred.
5. Tests: `tests/test_discord_v5_profile.py` (static: profile content, init_vars/ipset presence,
   Makefile install contract; runtime ucode-guarded: parse + profile_value_ok + the validator
   relaxation). Reconcile ready-set / circular_quality assertions for the native profile.

**r7 deferred (explicitly NOT in r7):**
- Closed-loop runtime learning (the `_read_output` equivalent: parse nfqws2/slm events → update
  learned.json/blocked.json → regenerate preload.lua → restart nfqws2). This is the big parity
  gap and needs a daemon/watcher (a ucode/rpcd-driven loop or a small long-running process).
- `DEFAULT_BLOCKED_PASS_DOMAINS` seeding (blacklist strategy=1 for discord/youtube/…). Needed
  only once circular rotation is live; for the static v5 profile it is irrelevant.
- Full ASKEY UDP profiling (ipset-label grouping), UDP/Discord-voice/stun chains, wireguard,
  mtproto, dns profiles.
- Strategy catalog dynamic build + renumber (port ships pre-baked .opt instead).
- Manual lock/block/unblock/change/clear CLI subcommands (beyond profile enable/disable).
- Ratings/history viewer, log-history rotation, Discord-client auto-restart.

---

## 5. Compatibility risks

1. **strategy=1 default-blacklist divergence:** original blacklists strategy=1 for
   discord.com/youtube.com/… (`blocked_strategies_manager.py:65`); port does not
   (`blocked.json` empty). Any future circular profile for discord MUST replicate this or
   strategy=1 (pass) will be tried and waste rotation slots / fail.
2. **seqovl=652 cancels on small SNI:** `tls_multisplit_sni:seqovl=652` cancels when
   `seqovl >= pos[1]-1` (observed on router: "seqovl cancelled, too large" for discord SNI at
   ~122). Strategies importing this from desktop presets may silently degrade to seqovl=0 on
   router. Validate seqovl vs SNI position per domain.
3. **`tls_mod` is a nfqws2-C builtin** (verified in the nfqws2 binary strings), not a Lua file.
   `init_vars.lua` relies on it. The port's nfqws2 is the same upstream, so OK — but it must be
   confirmed on the exact pinned nfqws2 (0.9.20260307) the port ships.
4. **circular_quality requires contiguous strategy=N from 1** (`orchestrator.lua:23`:
   "circular_quality: strategies must be contiguous from 1"). Profile builders must number
   strategies 1..N with no gaps.
5. **Packet-path writes forbidden** (AGENTS.md / `docs/orchestra-state-schema.md`): the original
   updates state from the `_read_output` thread (Python, not packet path). The port MUST NOT
   write JSON from the Lua packet path; the runtime-learning loop must be an async
   daemon/watcher that reads `events.ndjson`/logread and writes seeds, then regenerates preload
   and restarts nfqws2. This is an architectural constraint, not just a style choice.
6. **apply.uc enable does not start nfqws2** — a caller who expects `OrchestraRunner.start()`
   semantics (one call → running) will see "enabled but not bypassing" until they separately
   restart the service.
7. **Whitelist ordering:** the original excludes whitelist hosts BEFORE the Lua orchestrator
   runs (`--hostlist-exclude=whitelist.txt`). The port must keep that ordering or Lua may
   train/lock on hosts that should be excluded (`docs/port-map.md` already flags this).
8. **hostlist vs ipset filter:** Default v5 uses `--ipset=ipset-discord.txt` (IP-based). The
   port's default `MODE_FILTER=hostlist` + `<HOSTLIST>` placeholder does not fit an ipset-based
   native profile; the discord-v5 profile must use a literal `--ipset=` (no `<HOSTLIST>`), which
   also conflicts with the circular_quality validator expectation. Hence r7-minimum item 2.

---

## 6. Files to change (r7 minimum — for the later importer/runtime subagents, NOT now)

| File | Change |
|---|---|
| `openwrt/zapret2-orchestra/files/opt/zapret2/lua/init_vars.lua` | NEW — verbatim from youtubediscord pin (tls_google etc. via builtin tls_mod) |
| `openwrt/zapret2-orchestra/files/etc/zapret2-orchestra/lists/ipset-discord.txt` | NEW — verbatim ipset-discord.txt |
| `openwrt/zapret2-orchestra/files/usr/share/zapret2-orchestra/profiles/discord-v5.opt` | NEW — native NFQWS2_OPT profile (send+syndata:tls_google+syndata, --ipset, --lua-init=init_vars) |
| `openwrt/zapret2-orchestra/files/usr/share/zapret2-orchestra/profiles.tsv` | add discord-v5 row (mark native/non-circular) |
| `openwrt/zapret2-orchestra/Makefile` | INSTALL_DATA rules for init_vars.lua + lists/ipset-discord.txt; bump PKG_RELEASE 6→7 |
| `openwrt/zapret2-orchestra/files/usr/share/zapret2-orchestra/apply.uc` | relax `validate_profile()` circular_quality requirement for native (non-orchestra-runtime) profiles (`:731`); make `enable` activate datapath (NFQWS2_ENABLE=1 + service start) |
| `tests/test_discord_v5_profile.py` | NEW — static + runtime tests |
| `tests/test_ready_profile_contract.py`, `tests/test_working_prototype.py`, `tests/test_package_contract.py` | reconcile ready-set / circular_quality / r6→r7 assertions |
| `.github/workflows/build-apk.yml` | Stage 3 r6→r7 release regex; audit discord-v5.opt + init_vars.lua + ipset-discord.txt in extracted APK |

Deferred (post-r7, for the runtime subagent): a new `zapret2-orchestra-learner` daemon/watcher
(ucode or small long-run process) that tails nfqws2 `slm_quality:`/event output → updates
learned.json/blocked.json → runs `generate-preload.uc generate` → restarts nfqws2; plus
`DEFAULT_BLOCKED_PASS_DOMAINS` seeding and manual lock/block/unblock/change/clear CLI
subcommands.

---

## 7. Test matrix

| Area | Test | Layer | r7? |
|---|---|---|---|
| Profile static | discord-v5.opt contains send+syndata:tls_google+syndata, --ipset=ipset-discord.txt, --lua-init=init_vars; NO circular_quality | unit | r7 |
| Validator relaxation | native profile (no circular_quality, no orchestra runtime) passes `validate_profile` | unit (ucode) | r7 |
| Makefile contract | init_vars.lua + ipset-discord.txt + discord-v5.opt installed to correct dest with right modes | unit | r7 |
| Release | orchestra PKG_RELEASE=7; APK named zapret2-orchestra-…-r7.apk; Stage 3 regex | unit/CI | r7 |
| Enable activates datapath | `enable discord-v5` → NFQWS2_ENABLE=1 + nfqws2 running with the v5 argv | runtime (router) | r7 |
| Golden functional | router-only: baseline discord TIMEOUT ≥2/3 → after enable discord.com HTTP ≥2/3 (2/3), control intact, nfqws2 log shows syndata desync | runtime (router) | r7 |
| strategy=N numbering | renumber: after circular, each --lua-desync gets :strategy=N; reset on --new and --payload=; same-line share N (port `circular_strategy_numbering` equivalent) | unit | deferred |
| Runtime learning loop | SUCCESS→history→auto-lock@3; FAIL→history; UNLOCK→re-learn; preload regen; blocked skip | unit+runtime | deferred |
| DEFAULT_BLOCKED | strategy=1 blocked for discord.com/youtube.com on TLS; not unblockable | unit | deferred |
| Manual actions | lock/unlock (user_lock protected), block/unblock (blocked>locked), change, clear | unit | deferred |
| Persistence/crash-recovery | state survives restart; crashed-process diagnostics recorded | runtime | deferred |
| Whitelist ordering | whitelist hosts excluded before Lua runs | runtime | deferred |

---

## 8. Unresolved questions (for importer/runtime subagents)

1. Does the port ship a `circular_strategy_numbering` equivalent in ucode, or are all profiles
   pre-numbered `.opt` files? (Desktop renumbers at profile-build time; the port's ready `.opt`
   files are pre-numbered — confirm no dynamic build path exists.)
2. Where is the runtime-learning loop meant to live — a new init.d daemon, an rpcd-driven
   periodic job, or a ucode long-run? AGENTS.md forbids packet-path writes, so it must be async.
   Does `events.ndjson` already carry enough signal (LOCK/UNLOCK/SUCCESS/FAIL) to reconstruct the
   `_read_output` logic, or must the learner tail `logread`/nfqws2 stdout instead?
3. For the static discord-v5 profile, what is the persistent "learned/locked winner" representation
   in `learned.json` when there is no `strategy=N`? (Proposal: manager-state `profile=discord-v5`,
   `enabled=true` IS the lock; learned.json stays empty for non-circular profiles.)
4. Should `apply.uc enable` start the service, or is the two-step (enable + `/etc/init.d/zapret2
   restart`) intentional? The original is one-step (`start()`).
5. Does the pinned nfqws2 (0.9.20260307) expose the `tls_mod` builtin identically to the desktop
   build? (Verified by binary strings on router, but pin the contract in a test.)
6. `DEFAULT_BLOCKED_PASS_DOMAINS` — port the exact ~60-domain set, or derive from
   `russia-blacklist`/autohostlist? (Original hardcodes it.)
7. seqovl=652 cancellation: should the port rewrite/avoid seqovl=652 chains for small-SNI
   domains, or leave them (they silently no-op)? The golden scenario shows Default old's
   tls_multisplit_sni:seqovl=652 is useless for discord — should the strategy catalog prune it?
