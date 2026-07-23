# Orchestra r7 contracts — catalog, machine events, persistent state

**Status:** FROZEN contract for the r7 subagents (importer / runtime-learner / contracts-tests).
Companion to `docs/ORCHESTRA_PARITY_SPEC.md` (the reverse-engineered model) and
`docs/orchestra-state-schema.md` (the existing v1 state schema). This document EXTENDS the
existing v1 schema — it does not replace it. Every field the task requires is mapped to a
concrete file below. Subagents A/B/C implement against this contract; they must not diverge.

Pinned sources (provenance is mandatory in every catalog entry):
- Native presets: `youtubediscord/zapret2-youtube-discord` @ `4d75c70b430562e970bcf64cbe24072ce104b36a` (presets/*.txt, nfqws2 `--lua-desync` syntax). Cloned at `H:/zapret-port/strategy-research/zapret2-youtube-discord`.
- Original GUI (for DEFAULT_BLOCKED_PASS_DOMAINS + Orchestra model): `youtubediscord/zapret` @ `9d57e55d6751587d9d52b52147a05a0a8fcc9fd8` (submodule `zapret2gui`; Python at `src/orchestra/blocked_strategies_manager.py:65-102`).
- zapret2 core (lua desync fns send/syndata/multisplit/...): `zapret2-core` @ `8a0f53f3cf2c92ddeaa66995ee63a35c1210c410` (submodule).

---

## 1. Catalog entry contract (Subagent A owns)

The importer (`tools/import_strategy_catalog.py`) reads pinned presets and emits a catalog
fixture committed to the repo at `strategy-sources/catalog.json` (deterministic, sorted keys,
no timestamps, no randomness — reproducible byte-for-byte from the pinned inputs).

### Catalog file shape

```json
{
  "schema_version": 1,
  "catalog_version": 1,
  "source": {
    "repo": "youtubediscord/zapret2-youtube-discord",
    "commit": "4d75c70b430562e970bcf64cbe24072ce104b36a",
    "presets_path": "presets/"
  },
  "default_blocked_pass_domains": {
    "source_repo": "youtubediscord/zapret",
    "source_commit": "9d57e55d6751587d9d52b52147a05a0a8fcc9fd8",
    "source_path": "src/orchestra/blocked_strategies_manager.py:65-102",
    "domains": ["discord.com", "youtube.com", "google.com", "..."]
  },
  "entries": [ <catalog entry>, ... ]
}
```

### Catalog entry shape (every field required unless marked optional)

```json
{
  "stable_id": "discord-default-v5",
  "source_id": "Default v5",
  "source_commit": "4d75c70b430562e970bcf64cbe24072ce104b36a",
  "source_path": "presets/Default v5.txt",
  "source_block_index": 1,
  "source_sha256": "<sha256 of the exact preset block bytes (from --new to next --new/EOF)>",
  "chain_id": "<sha256 of the canonical, normalized lua_steps serialization — stable across catalog reordering>",
  "strategy_number": 2,
  "askey": "tls",
  "services": ["discord"],
  "domains": ["discord.com", "discordapp.com"],
  "hostlists": [],
  "ipsets": ["lists/ipset-discord.txt"],
  "lua_steps": [
    {"func": "send",     "args": {"repeats": "3"}},
    {"func": "syndata",  "args": {"blob": "tls_google"}},
    {"func": "syndata",  "args": {}}
  ],
  "required_assets": [
    {"path": "lua/init_vars.lua",            "sha256": "...", "source": "youtubediscord/zapret2-youtube-discord@4d75c70b"},
    {"path": "lists/ipset-discord.txt",      "sha256": "...", "source": "youtubediscord/zapret2-youtube-discord@4d75c70b"}
  ],
  "compatibility": {
    "status": "compatible",
    "dropped_options": [],
    "notes": ""
  },
  "warnings": []
}
```

Rules (binding):

1. **stable_id** is deterministic from the chain content (e.g. a slug derived from
   `chain_id`), NOT from the preset's position in the catalog or its filename. Re-running the
   importer on the same pinned inputs yields identical `stable_id`s. Renaming or reordering
   presets does NOT change a `stable_id`.
2. **chain_id** = `sha256` of the normalized `lua_steps` serialization (sorted args keys,
   canonical JSON). Stable across runs and reorderings.
3. **strategy_number** is assigned by the importer, deterministically, WITHIN a generated
   adaptive profile (see §4). The catalog entry records the strategy number the importer
   assigned to this chain in the adaptive profile it generated. Runtime MUST NOT renumber
   strategies after learned state exists — the persisted `locked_by_askey` strategy number is
   resolved to a `stable_id`/`chain_id` via the profile's `chain_id_for_strategy` map (§4), so
   a renumber would silently misapply a learned lock. The importer is the ONLY numberer.
4. **source_sha256** is the sha256 of the exact preset block bytes the entry was derived from
   (from the line after a `--new` separator to the next `--new` or EOF), so the entry is
   auditable back to pinned upstream.
5. **compatibility.status** ∈ `compatible` | `incompatible`. `dropped_options` lists the
   Windows-specific transport options removed (ONLY these, verbatim from the task):
   `--wf-tcp-out`, `--wf-udp-out`, `--wf-raw-part`, and WinDivert-specific filters. The
   importer MUST NOT alter the meaning of `--filter`, `--payload`, `--out-range`,
   `--lua-desync`, `--blob`, `--hostlist`, `--ipset`, or `--new`, and MUST NOT replace an
   unknown function with a simplified analog — unknown functions go to `incompatible`.
6. **warnings**: any chain using `seqovl=<N>` that may cancel on a short SNI gets a warning
   (Default old's `tls_multisplit_sni:seqovl=652` is the canonical example; spec §5.2).
7. **default_blocked_pass_domains.domains** is the EXACT set imported from the pinned GUI
   (`blocked_strategies_manager.py:65-102`), with provenance. It is NOT derived from
   autohostlist. `discord.com` MUST be present.
8. All three Default-v5 lua_steps (send, syndata:tls_google, syndata) MUST share one
   `strategy_number` and one `chain_id` — they are ONE strategy chain. A test enforces this.
9. Default old AND Default v5 MUST both be present in the same Discord candidate pool (same
   `services` contains "discord" OR `domains`/`ipsets` target Discord), so circular rotation
   can consider both.

### Adaptive profile generation (importer output, also Subagent A)

The importer additionally generates an adaptive profile for Discord with ≥2 chains:
- Strategy 1 = Default old Discord chain.
- Strategy 2 = Default v5 chain (send:repeats=3 + syndata:blob=tls_google + syndata), all
  three steps sharing strategy_number=2.
The profile is a `circular_quality`-based `.opt` (selector + numbered chains) plus a
sidecar `chain_id_for_strategy` map (see §4). The importer returns the strategy numbers it
assigned.

---

## 2. Machine event contract (Subagent B owns the emitter; A/C depend)

Runtime events are NDJSON appended to `/tmp/zapret2-orchestra/events.ndjson` by the Lua
runtime (extended `events.lua` + `orchestrator.lua`). The learner tails this file. The
existing sink (`events.lua`) already emits `rotate`/`lock`/`unlock`/`error`/`start`/`stop`;
r7 EXTENDS the type set and fields.

### Event record shape (one JSON object per line)

```json
{"schema_version":1,"ts":1753290000,"type":"SUCCESS","askey":"tls","host":"discord.com","strategy":2,"chain_id":"discord-default-v5","reason":"combined_success_detector","generation":3,"run_id":"nfqws2-20260723T203000Z"}
```

Fields:
- `schema_version`: 1 (int). Required.
- `ts`: unix seconds (int). Required.
- `type`: one of `SUCCESS`, `FAIL`, `LOCK`, `UNLOCK`, `APPLIED`, `ROTATE` (plus the existing
  `error`/`start`/`stop` retained for lifecycle). Required.
- `askey`: one of the 9 profiles (`tls`,`http`,`quic`,`discord`,`wireguard`,`mtproto`,`dns`,
  `stun`,`unknown`). Required for SUCCESS/FAIL/LOCK/UNLOCK/APPLIED/ROTATE.
- `host`: normalized hostname (lowercase, no leading/trailing dot). Required for the same set.
- `strategy`: the `:strategy=N` number the event concerns (int ≥1). Required for SUCCESS/FAIL/
  LOCK/UNLOCK/APPLIED/ROTATE.
- `chain_id`: the stable chain ID matching `strategy` via the profile's
  `chain_id_for_strategy` map (§4). Required (lets the learner persist a lock that survives
  renumbering and lets tests assert runtime↔catalog linkage).
- `reason`: short machine string, e.g. `combined_success_detector`, `combined_failure_detector`,
  `lock_successes_met`, `unlock_fails_met`, `rotate`, `applied`. Required.
- `generation`: the preload generation active when the event was emitted (int). Required.
- `run_id`: a stable per-nfqws2-start identifier (set by init.lua at load). Required.

Forbidden in events: packet payloads, dissect structures, dumps (same rule as existing schema).
Max file size and rotation policy unchanged (2 MiB default, `ORCHESTRA_EVENTS_MAX_BYTES`).

### Emission points (binding on orchestrator.lua)

- `APPLIED` — once per connection when a strategy is chosen and its chain executed.
- `SUCCESS` — when `combined_success_detector` succeeds for the applied strategy.
- `FAIL` — when `combined_failure_detector` fails for the applied strategy.
- `LOCK` — on auto-lock (already emitted today; add `chain_id`/`reason`/`generation`/`run_id`).
- `UNLOCK` — on auto-unlock (already emitted today; add fields; NEVER emitted for a user-lock).
- `ROTATE` — on circular rotation to a new strategy (already emitted today; add fields).

The learner MUST NOT parse human logs (logread/nfqws2 stdout). The NDJSON stream is the only
runtime signal source.

---

## 3. Persistent state contract (Subagent B owns the writer; C validates)

The task requires these state fields. They map onto the EXISTING v1 schema files plus
targeted extensions — the existing atomic-write/recovery machinery is reused (DRY):

| Task field | File (existing unless NEW) | Shape |
|---|---|---|
| `schema_version` | every state file | `1` (existing) |
| `locked_by_askey` | `learned.json` | `protocols.<askey>.<host>.auto_lock = <strategy>` (EXISTING) |
| `strategy_history` | `learned.json` | `protocols.<askey>.<host>.strategies."<N>" = {successes, failures}` (EXISTING) |
| `user_locked_by_askey` | `manual-locks.json` | `protocols.<askey>.<host> = <strategy>` (EXISTING; emits `slm_preload_locked(...,true)`) |
| `blocked_by_askey` (default) | `blocked.json` | `protocols.<askey>.global = [N,...]` and `protocols.<askey>.hosts.<host> = [N,...]` (EXISTING, RUNTIME strategy numbers). r7 stable identity: `protocols.<askey>.global_chain = ["<stable_id>",...]` and `protocols.<askey>.hosts_chain.<host> = ["<stable_id>",...]` hold STABLE CHAIN IDs (contract §4); `generate-preload.uc` resolves each to the runtime strategy number the ACTIVE profile's sidecar assigns and DROPS chains absent from the active profile (a block never transfers to a different chain sharing the same runtime number). DEFAULT_BLOCKED_PASS_DOMAINS seeds `hosts_chain.<host> = ["discord-send-syndata-tls_multisplit_sni-44860d17"]` (the pass-like chain) for the discord/youtube/… askey=TLS hosts. |
| `user_blocked_by_askey` | `blocked.json` (EXTEND) | NEW keys `user_global`/`user_hosts` alongside `global`/`hosts`. User-blocks are unblockable-by-user but NOT protected from… (see rules). generate-preload.uc emits user-blocks with a user flag so `slm.lua` can distinguish default-blocked (never unblockable) from user-blocked (unblockable by user action only). |
| `whitelist` | `whitelist.json` | `hosts: [...]` (EXISTING) |
| processed event cursor | `learner-state.json` (NEW) | `{schema_version:1, event_cursor:{bytes:<offset>, lines:<n>, sha256:<last-line-hash>}, last_preload_gen:<int>, run_id:"...", updated_at:<ts>}` |
| preload generation | `manager-state.json` (EXISTING) + `learner-state.json.last_preload_gen` | `generation` (EXISTING) |
| selected adaptive profile | `manager-state.json` (EXISTING) | `profile` (EXISTING) |

### `learner-state.json` (NEW — Subagent B)

```json
{
  "schema_version": 1,
  "event_cursor": {"bytes": 4096, "lines": 87, "last_line_sha256": "..."},
  "last_preload_gen": 3,
  "last_run_id": "nfqws2-20260723T203000Z",
  "updated_at": 1753290120
}
```

The cursor makes event processing resumable and idempotent: on restart the learner seeks to
`bytes` and re-validates `lines`/`last_line_sha256`; if the tail changed (truncation/rotation)
it recovers per §"Write guarantees".

### State write guarantees (binding — reuse existing pattern)

Every persistent write (learned/blocked/whitelist/manual-locks/learner-state) MUST:
1. hold the single-writer lock (reuse apply.uc's mkdir-lock pattern at a sibling lock dir
   `/var/lock/zapret2-orchestra-learner.lock`; CLI manual actions take the SAME lock so the
   learner and CLI never write concurrently — DRY, one mutual-exclusion discipline);
2. validate the in-memory document against schema;
3. serialize deterministic compact JSON;
4. re-parse and validate those exact bytes;
5. write a same-directory temp file;
6. `fsync` where supported;
7. atomic rename over the target;
8. update a validated `<name>.good` copy the same way;
9. `fsync` the containing directory where supported.

Recovery: if a primary file is malformed/violates schema, validate the `.good` copy and
atomically restore. If both invalid, fail without manufacturing state.

### Event processing guarantees (binding on the learner)

- **Incremental**: tail events.ndjson from the durable cursor; never re-read the whole file.
- **Idempotent**: each event is processed at most once. Dedup key = `(run_id, type, host,
  strategy, ts)`; a repeated event after restart (cursor stale) is a no-op.
- **Truncated-line recovery**: if the last line is partial (no trailing `\n` or invalid JSON),
  skip it, do NOT advance the cursor past it, and retry on the next poll (the writer may still
  be mid-append). Only advance the cursor over complete, validated lines.
- **No packet-path writes**: the Lua packet path NEVER writes persistent JSON; it only appends
  to events.ndjson (the existing transition-only sink). The learner is the sole JSON writer at
  runtime. (Spec §5.5 / AGENTS.md constraint.)

### Lock/unlock policy (binding)

- Auto-LOCK after 3 TCP SUCCESS (`lock_successes=3`) or 1 UDP SUCCESS on a strategy.
- Auto-UNLOCK after 3 consecutive FAIL (`unlock_fails=3`) on an auto-locked strategy.
- NEVER auto-unlock a user-locked strategy (`slm_is_user_lock` true → skip).
- Blocked strategy has PRIORITY over auto-lock AND user-lock (blocked is skipped in rotation;
  a locked==blocked conflict is dropped, blocked wins — `slm_reset` in orchestrator.lua:56-59).
- DEFAULT_BLOCKED_PASS_DOMAINS blocks strategy=1 for the seeded domains at load (cannot be
  unblocked by user).

### Reload policy (binding)

- The learner regenerates preload (`generate-preload.uc generate` + `check`) and reloads
  nfqws2 ONLY when `locked_by_askey` or `blocked_by_askey` changes — NOT on every SUCCESS/FAIL
  history update. Reload is debounced (coalesce changes within a short window, e.g. 5 s) so a
  burst of events produces one reload, not many. nfqws2 reload = `/etc/init.d/zapret2 restart`
  via the procd-managed service, never from the Lua packet path.

---

## 4. Adaptive profile + chain_id_for_strategy map (A generates, B consumes, C tests)

The adaptive Discord profile ships as two files:
- `profiles/discord-adaptive.opt` — a `circular_quality` `.opt` with strategy=1 (Default old)
  and strategy=2 (Default v5) chains.
- `profiles/discord-adaptive.json` (sidecar) — the strategy↔chain map and metadata:

```json
{
  "schema_version": 1,
  "profile_id": "discord-adaptive",
  "askey": "tls",
  "chain_id_for_strategy": {
    "1": "<default-old chain_id>",
    "2": "discord-default-v5"
  },
  "strategy_for_chain_id": { "<default-old chain_id>": 1, "discord-default-v5": 2 },
  "default_blocked_pass_domains_applied": true
}
```

`generate-preload.uc` (extended by B) bakes `chain_id_for_strategy` into `preload.lua` so that
on restart a persisted `locked_by_askey.tls["discord.com"] = 2` resolves to
`chain_id = discord-default-v5` and the runtime applies the v5 chain. The learner writes
`learned.json` with the strategy NUMBER (per the task's required learned-result form
`locked_by_askey.tls["discord.com"] = <strategy number Default v5>`); the number↔chain mapping
is stable because the importer is the only numberer (§1 rule 3). The sidecar's
`profile_id` MUST match the active profile in `manager-state.json`; a stale or mismatched
sidecar (e.g. an old adaptive sidecar left at the original-pool path) is rejected with an
explicit diagnostic rather than silently baked.

DEFAULT_BLOCKED_PASS_DOMAINS blocks the pass-like chain by STABLE CHAIN ID
(`hosts_chain.<host> = ["discord-send-syndata-tls_multisplit_sni-44860d17"]`), not by runtime
strategy number. `generate-preload.uc` resolves the stable id to the runtime strategy number
the ACTIVE profile's sidecar assigns via `strategy_for_chain_id` and DROPS it when the chain
is absent from the active profile. In the `discord-adaptive` (2-strategy) profile the chain
resolves to runtime strategy=1 (Default old) and circular rotation skips it
(`slm_is_blocked` → `selected_next`), moving to strategy=2 (Default v5). In
`discord-adaptive-original-pool` (24-strategy) the pass-like chain is EXCLUDED, so the block is
dropped and the winner at runtime strategy=1 (`chain-tls_multisplit_sni-70576793`) is NOT
blocked — the block never transfers to a different chain sharing the same runtime number.

---

## 5. enable/disable lifecycle contract (Subagent B)

- **apply** (existing): validate + apply NFQWS2_OPT + manager state; not required to start
  the service.
- **enable**: validate → snapshot/backup → apply NFQWS2_OPT → set `NFQWS2_ENABLE=1` in
  `/opt/zapret2/config` (byte-edit via the existing parser) → start/restart `zapret2` (via the
  procd layer / init.d wrapper, NOT a direct `/etc/init.d` reference inside apply.uc —
  reconcile `test_working_prototype`'s prohibition) → start the learner → verify both
  processes + nft/NFQUEUE → rollback on any error.
- **disable**: stop learner → stop zapret2 → remove datapath (`NFQWS2_ENABLE=0` + table
  teardown via the service) → restore backup → `enabled=false` → verify no nfqws2 + no
  `table inet zapret2`.

"enable" MUST be one-step (parity with `OrchestraRunner.start()`): a single
`zapret2-orchestra-profile enable discord-adaptive` results in a running, bypassing datapath.

---

## 6. Test contract hooks (Subagent C)

Tests MUST cover (non-exhaustive — see spec §7 + the task's Step 3/C list):
- deterministic importer; stable chain IDs (re-run → identical); deterministic strategy
  numbering; `--new` block preservation; dependency closure; service/domain/ASKEY detection;
  Default v5 chain grouping (3 ops, one strategy number, one chain_id); circular_quality NOT
  required for the static native `discord-v5.opt`; adaptive profile has selector + numbered
  chains + `chain_id_for_strategy`.
- blocked > locked (incl. user-lock); user-lock protected from auto-unlock; TCP auto-lock@3;
  UDP auto-lock@1; auto-unlock@3; duplicate-event idempotency; truncated-NDJSON recovery;
  state atomicity; cursor recovery; preload debounce; restart persistence.
- enable starts learner + datapath; disable restores config.
- `discord.com` ∈ DEFAULT_BLOCKED_PASS_DOMAINS.
- `tls_mod` confirmed by target runtime contract, not by string matching.
- Default old/v5 network results are ROUTER acceptance evidence, NOT hardcoded as universal
  unit-test assertions.

r6→r7 reconciliation: existing `test_ready_profile_contract.py`, `test_working_prototype.py`,
`test_package_contract.py`, `test_cli_sentinel.py` stay green; release assertions + Stage 3
regex updated to r7.
