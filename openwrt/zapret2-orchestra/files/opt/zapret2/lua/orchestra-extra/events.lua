-- Compact runtime-only event sink.  It is called only on state transitions,
-- never for every packet, and never writes under /etc.
--
-- Contract: docs/ORCHESTRA_R7_CONTRACTS.md §2.  Each record is one JSON object
-- per line appended to ORCHESTRA_EVENTS_FILE (default
-- /tmp/zapret2-orchestra/events.ndjson).  The learner daemon tails this file
-- incrementally and is the sole writer of the persistent JSON seeds; the Lua
-- packet path only ever appends here.
--
-- Field set (r7, extends the v1 sink):
--   schema_version : 1 (int, required)
--   ts             : unix seconds (int, required)
--   type           : SUCCESS|FAIL|LOCK|UNLOCK|APPLIED|ROTATE|error|start|stop
--   askey          : one of the 9 protocol profiles (required for the state-
--                    transition types; the legacy "protocol" alias is also
--                    emitted so older readers keep working)
--   host           : normalized hostname
--   strategy       : the :strategy=N number (int >= 1)
--   chain_id       : stable chain id matching `strategy` via the profile's
--                    chain_id_for_strategy map (baked into preload.lua as
--                    ORCHESTRA_CHAIN_ID_FOR_STRATEGY)
--   reason         : short machine string (combined_success_detector,
--                    lock_successes_met, unlock_fails_met, rotate, applied, ...)
--   generation     : preload generation active when the event was emitted
--   run_id         : stable per-nfqws2-start id (set by init.lua at load)
--   state, message, dry_run : legacy fields retained for lifecycle/error rows
--
-- Forbidden in events: packet payloads, dissect structures, dumps (same rule
-- as the existing schema).  Max file size and rotation policy unchanged
-- (2 MiB default, ORCHESTRA_EVENTS_MAX_BYTES).

local EVENT_TYPES = {
    -- state-machine events the learner consumes
    success=true, fail=true, lock=true, unlock=true, applied=true, rotate=true,
    -- lifecycle (retained from the v1 sink)
    error=true, start=true, stop=true
}

-- Canonical `type` spelling per contract §2: state-machine types are UPPER-
-- CASE (SUCCESS/FAIL/LOCK/UNLOCK/APPLIED/ROTATE); lifecycle types stay
-- lower-case (error/start/stop). Callers pass the lower-case internal name
-- (e.g. orchestra_emit_event("fail", ...)); this map emits the contract-
-- canonical spelling so the NDJSON `type` field matches the contract exactly.
-- The learner normalizes `type` back to lower-case for its own comparisons, so
-- both the UPPER-case wire form and the lower-case internal form agree.
local CANONICAL_TYPE = {
    success="SUCCESS", fail="FAIL", lock="LOCK", unlock="UNLOCK",
    applied="APPLIED", rotate="ROTATE",
    error="error", start="start", stop="stop",
}

-- Fields allowed in an event record.  The v1 set (host, protocol, strategy,
-- state, message, dry_run) is retained; r7 adds askey, chain_id, reason,
-- generation, run_id.  `protocol` is kept as a legacy alias of `askey`.
local ALLOWED_FIELDS = {
    host=true, protocol=true, strategy=true, state=true, message=true, dry_run=true,
    askey=true, chain_id=true, reason=true, generation=true, run_id=true
}

-- Field emission order: a stable, sorted order makes records deterministic
-- (the learner's dedup key is stable across restarts).  We sort keys
-- alphabetically at emit time, so this table only whitelists membership.
local function json_escape(value)
    return tostring(value):gsub("\\", "\\\\"):gsub('"', '\\"'):gsub("\n", "\\n"):gsub("\r", "\\r")
end

local function json_value(value)
    if type(value) == "number" then return tostring(value) end
    if type(value) == "boolean" then return value and "true" or "false" end
    return '"' .. json_escape(value) .. '"'
end

-- Resolve the chain_id for a (askey, strategy) pair from the table baked into
-- preload.lua by generate-preload.uc (ORCHESTRA_CHAIN_ID_FOR_STRATEGY).  The
-- table is keyed by askey then by strategy number (as a Lua number).  Returns
-- nil if no map is present (e.g. a non-circular native profile) so the emitter
-- can omit the field rather than write a bogus value.
local function chain_id_for(askey, strategy)
    local map = ORCHESTRA_CHAIN_ID_FOR_STRATEGY
    if type(map) ~= "table" then return nil end
    local by_askey = map[askey]
    if type(by_askey) ~= "table" then return nil end
    local s = tonumber(strategy)
    if s == nil then return nil end
    -- strategy keys are baked as numeric Lua table keys by the preload
    -- generator; fall back to string lookup for robustness.
    local cid = by_askey[s] or by_askey[tostring(s)]
    if type(cid) == "string" and cid ~= "" then return cid end
    return nil
end

-- Public so orchestrator.lua can fill chain_id without duplicating the lookup.
function orchestra_chain_id_for_strategy(askey, strategy)
    return chain_id_for(askey, strategy)
end

function orchestra_emit_event(event, fields)
    if not EVENT_TYPES[event] then return false end
    fields = fields or {}
    -- Normalize askey: prefer an explicit askey field, fall back to the legacy
    -- `protocol` field, and mirror back into `askey` so the learner reads one
    -- canonical key.  Keep `protocol` in the output for backward compatibility.
    local askey = fields.askey or fields.protocol
    if askey and not fields.askey then fields.askey = askey end
    -- If chain_id was not supplied by the caller but a strategy+askey are
    -- present, resolve it from the baked map so every state-transition record
    -- carries a chain_id when one is knowable.
    if not fields.chain_id and fields.strategy and askey then
        local cid = chain_id_for(askey, fields.strategy)
        if cid then fields.chain_id = cid end
    end
    -- run_id and generation default from the globals init.lua sets at load.
    if not fields.run_id and ORCHESTRA_RUN_ID then fields.run_id = ORCHESTRA_RUN_ID end
    if not fields.generation and ORCHESTRA_PRELOAD_GENERATION then
        fields.generation = ORCHESTRA_PRELOAD_GENERATION
    end

    -- Whitelist + sort keys for deterministic output.
    local keys = {}
    for key, _ in pairs(fields) do
        if ALLOWED_FIELDS[key] then keys[#keys + 1] = key end
    end
    table.sort(keys)
    -- Emit the contract-canonical `type` spelling (UPPER-CASE for state-machine
    -- types, lower-case for lifecycle) so the NDJSON wire form matches §2.
    local canon = CANONICAL_TYPE[event] or event
    local parts = {
        '{"schema_version":1',
        '"ts":' .. tostring(os.time()),
        '"type":' .. json_value(canon)
    }
    for _, key in ipairs(keys) do
        parts[#parts + 1] = '"' .. key .. '":' .. json_value(fields[key])
    end
    local line = table.concat(parts, ",") .. "}\n"
    local path = ORCHESTRA_EVENTS_FILE or "/tmp/zapret2-orchestra/events.ndjson"
    local max_bytes = tonumber(ORCHESTRA_EVENTS_MAX_BYTES) or 2097152
    if max_bytes < 256 then max_bytes = 256 end
    local file = io.open(path, "a+")
    if not file then return false end
    local size = file:seek("end") or 0
    if size + #line > max_bytes then
        file:close()
        file = io.open(path, "wb")
        if not file then return false end
    end
    file:write(line)
    file:flush()
    file:close()
    return true
end
