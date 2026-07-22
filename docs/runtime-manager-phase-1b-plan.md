# Runtime Manager — Phase 1B plan (design only, no implementation)

**Date:** 2026-07-21
**Status:** plan (not implemented)
**Depends on:** Phase 1A delivered and verified (manager.parse, manager.state, manager.lock, manager.profile, CLI status/validate-config/validate-profile/lock-test).

## Overview

Phase 1A delivered the safe foundation: a config parser that never sources, an atomic JSON state, a mkdir lock, a profile validator, and read-only CLI subcommands. Phase 1B builds the transaction engine on top of that foundation, enabling the four lifecycle operations (`enable`, `disable`, `apply`, `rollback`) and the boot-time integrity check (`boot-check`).

## 1. Persistent `state=applying` and `previous_state`

After a successful `apply`, the manager writes `states: ["idle"]` to `manager-state.json`. If the host loses power or nfqws2 crashes mid-apply, the state file says `idle` but the reality on disk may be inconsistent. Phase 1B introduces an **applying marker**:

- `apply` atomically writes `states: ["applying", "idle"]` BEFORE touching the config or generated files.
- After all files are committed (config written, preload regenerated, whitelist updated, manifest verified), `states` is rolled back to `["idle"]`.
- On next boot (or on `boot-check`), if `states` still contains `"applying"`, the manager knows the last apply was interrupted.

This is a two-phase write: mark intent, do work, clear intent.

## 2. Candidate transaction and internal rollback (no re-lock)

The `apply` operation holds the lock during the entire transaction. If any step fails (config parse error, `sh -n` failure, preload check failure), the manager rolls back without releasing and re-acquiring the lock:

1. **Snapshot state**: read current `manager-state.json`, record generation, hashes, and the filename of a backup copy of the config.
2. **Write candidate config**: `transform_nfqws2_opt(original_config, new_value)` → temp file; run `sh -n`.
3. **Regenerate preload**: `zapret2-orchestra-preload generate`; verify with `check`.
4. **Commit**: `rename` candidate config over `/opt/zapret2/config` (atomic on the same filesystem), then `atomic_write_state` with the new generation and hashes.
5. **On failure at any step before commit**: restore `states` to `["idle"]`, emit error state, and release the lock. No partial writes survive because the candidate is only renamed at the commit point.

The lock is held until commit (or rollback) completes. No re-lock is needed because the lock was never released during the transaction.

## 3. Backup generations

A single backup copy (`/opt/zapret2/config.bak`) is not enough: a failed apply could corrupt the backup too. Phase 1B maintains a limited ring of backup files:

- `/opt/zapret2/config.bak.0` — most recent backup (before the last apply).
- `/opt/zapret2/config.bak.1` — second-most-recent.
- Keep at most `N` generations (e.g., `N=3`). Older generations are rotated out.

`rollback` restores `config.bak.0` by renaming it over `/opt/zapret2/config`, regenerates the preload, checks the manifest, and writes the new state with `previous_state: "rolled_back"`.

On a fresh install, there is no backup. `rollback` in this case returns an error and records it in `last_error`.

## 4. Drift detection

Between `apply` operations, the config or the preload files might be modified outside the manager (by the user, by an upgrade, by a bug). Before every `apply` or `boot-check`, the manager computes the current hashes and compares them to the recorded hashes in `manager-state.json`:

- `nfqws2_opt` hash mismatch → the config has drifted. The manager must NOT silently overwrite user changes. It records a `drift_detected` warning in the state.
- `preload`, `whitelist`, `manifest` mismatch → the runtime files have drifted. The manager regenerates them and warns.

`boot-check` (invoked before the zapret2 service starts) detects drift and in Phase 1B only reports it. A future phase may autor-recover.

## 5. Health gate

Before committing an `apply`, every generated file is verified:

- `sh -n` on the candidate config (parse-only, as in Phase 1A).
- `zapret2-orchestra-preload check` on the generated preload and whitelist (the generator's own check mode verifies the manifest).
- The config file must contain a valid `NFQWS2_OPT` assignment (our parser).
- The four state hashes must match the actual files after commit.

If any gate fails, the apply aborts with `states: ["idle"]` and no config mutation.

## 6. Rollback-conflict

If a user edits the config after an `apply`, the current config no longer matches the backuped version. `rollback` compares the current config hash against the recorded `nfqws2_opt` hash in the state:

- If they match → the config is exactly what the manager last wrote; rollback is safe (restore the backup).
- If they don't match → the config has been modified externally. `rollback` records a warning (`rollback_conflict`) and restores the backup anyway (the user explicitly requested rollback) but marks the state so the user is aware.

A future phase may offer a `force` flag or an interactive confirm.

## 7. Boot-check before zapret2 starts

`/etc/init.d/zapret2-orchestra` already runs at `START=20` (before zapret2 at `START=21`). Phase 1B adds a dispatch entry for `boot-check` that:

1. Reads the current state.
2. If `states` contains `"applying"` → the last apply was interrupted; emit a warning and try to restore from `config.bak.0` if it exists.
3. Computes current hashes; if drift detected → warn.
4. Regenerates the preload if needed (backup guarantee).
5. Writes the updated state.

This ensures that after a power loss or crash during an apply, the system boots into a known state.

## 8. Recovery after power loss

If power is lost during an apply:

- Before `rename` of the candidate config → the real config is untouched; the temp file is lost (/tmp); `states` still says `applying`. On boot, `boot-check` sees `applying`, warns, and recovers.
- After `rename` of the candidate config but before `atomic_write_state` → the config IS updated but the state is stale. `boot-check` detects drift (hash mismatch), warns, and re-writes the state with the observed hashes.
- After both → everything is consistent.

In all cases, the file system guarantees (atomic `rename` on the same filesystem) prevent partial writes.

## Not in Phase 1B

- Live apply without nfqws2 restart (requires SIGHUP or similar; needs investigation into whether nfqws2 supports config reload without restart).
- Auto-rollback on health-check failure (requires a health probe that monitors nfqws2 liveness; out of scope).
- Candidate preview (dry-run) — CLI could accept `apply --dry-run` to show what would change without committing. Useful but not in 1B.
- Atomic multi-file transaction across `/opt/zapret2/config` and `/tmp/zapret2-orchestra/` on different filesystems (rename across filesystems is not atomic; would need a different strategy).
- Migration of state from older schema versions.
