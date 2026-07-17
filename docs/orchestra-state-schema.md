# Orchestra state schema — TLS MVP

**Version:** 1  
**Implemented:** 2026-07-17  
**Scope:** persistent control-plane state and generated Lua preload.

## Paths and ownership

| File | Owner | Packet path access |
|---|---|---|
| `/etc/zapret2-orchestra/learned.json` | backend | none |
| `/etc/zapret2-orchestra/blocked.json` | backend | none |
| `/etc/zapret2-orchestra/whitelist.json` | backend | none |
| `/etc/zapret2-orchestra/manual-locks.json` | backend | none |
| `/tmp/zapret2-orchestra/preload.lua` | backend generator | loaded once at init |
| `/tmp/zapret2-orchestra/whitelist.txt` | backend generator | nfqws2 profile filter |
| `/tmp/zapret2-orchestra/events.ndjson` | backend + transition-only Lua sink | append on state transition |

Every persistent document has `schema_version: 1`. Host keys are lowercase,
have no leading/trailing dot, and are stored after the protocol's normal host
normalization. Strategy IDs are positive integers.

## `learned.json`

Ratings and auto-locks are grouped by protocol and host:

```json
{
  "schema_version": 1,
  "protocols": {
    "tls": {
      "video.example": {
        "auto_lock": 2,
        "strategies": {
          "2": {"successes": 3, "failures": 1}
        }
      }
    }
  }
}
```

`clear --host video.example --protocol tls` removes only that host under the
selected protocol. Other hosts and future protocol maps remain unchanged.

## `blocked.json`

TLS MVP exposes global strategy blocking through the CLI; the schema also
supports preloaded per-host blocks:

```json
{
  "schema_version": 1,
  "protocols": {
    "tls": {
      "global": [3],
      "hosts": {"video.example": [2]}
    }
  }
}
```

Lists must be sorted and unique. Effective blocked strategies are the union of
global and matching host/domain entries.

## `whitelist.json`

```json
{"schema_version":1,"hosts":["safe.example"]}
```

Hosts are sorted and unique. The generator mirrors them to runtime
`whitelist.txt`; nfqws2 `--hostlist-exclude` is authoritative and a preloaded
Lua table provides a second in-memory guard.

## `manual-locks.json`

```json
{
  "schema_version": 1,
  "protocols": {"tls": {"video.example": 2}}
}
```

Generation emits `slm_preload_locked("tls", "video.example", 2, true)` so a
manual lock is protected from automatic unlock.

## Atomic update and recovery

For every persistent write the backend:

1. validates the in-memory document;
2. serializes deterministic compact JSON;
3. parses and validates those exact bytes again;
4. writes a same-directory temporary file;
5. flushes and calls `fsync` where the platform supports it;
6. atomically renames it over the target;
7. updates a validated `<name>.good` copy the same way;
8. `fsync`s the containing directory where supported.

If the primary file is malformed or violates schema, the backend validates
the `.good` copy and atomically restores the primary. If both are invalid, the
command fails without manufacturing state. All CLI commands are serialized by
the runtime backend lock.

## Generated preload

`preload.lua` is deterministic and contains only data calls already supplied
by the independent Orchestra extension:

- `slm_preload_history(...)`;
- `slm_preload_locked(...)`;
- `slm_preload_blocked(...)`;
- assignments to `ORCHESTRA_WHITELIST`.

`init.lua` loads this file once. Failure to load it is fatal to Orchestra
initialization so later health/rollback handling can restore the standard
remittor mode. No packet callback opens persistent JSON.

## Event schema

Each NDJSON record contains `event`, Unix `ts`, and a small subset of:
`host`, `protocol`, `strategy`, `state`, `message`, `dry_run`.

Allowed event types are `rotate`, `lock`, `unlock`, `error`, `start`, and
`stop`. Packet payloads, dissect structures, and dumps are forbidden. The
default maximum is 2 MiB and can be reduced for tests through
`ORCHESTRA_EVENTS_MAX_BYTES`.
