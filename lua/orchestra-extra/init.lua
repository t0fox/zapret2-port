-- Load after zapret-lib.lua, zapret-antidpi.lua and zapret-auto.lua.
-- The package installs this directory at /opt/zapret2/lua/orchestra-extra.

local dir = ORCHESTRA_EXTRA_DIR or "/opt/zapret2/lua/orchestra-extra"
dofile(dir .. "/slm.lua")
dofile(dir .. "/slm-adapter.lua")
dofile(dir .. "/events.lua")
dofile(dir .. "/detectors.lua")
dofile(dir .. "/orchestrator.lua")
local preload = ORCHESTRA_PRELOAD_FILE or "/tmp/zapret2-orchestra/preload.lua"
local preload_ok, preload_error = pcall(dofile, preload)
if not preload_ok then
    if DLOG_ERR then DLOG_ERR("orchestra-extra preload failed: " .. tostring(preload_error)) end
    error("orchestra-extra preload failed: " .. tostring(preload_error))
end

-- Establish a stable per-nfqws2-start run id and record the preload generation
-- so every event emitted this run carries them (contract §2: run_id and
-- generation are required fields).  run_id is derived from the wall clock at
-- load time and is stable for the lifetime of this nfqws2 process; it lets the
-- learner dedup events across restarts and attribute a burst to one start.
if not ORCHESTRA_RUN_ID then
    local ts = os.time() or 0
    local fmt = string.format
    ORCHESTRA_RUN_ID = fmt("nfqws2-%d", ts)
end
-- ORCHESTRA_PRELOAD_GENERATION is normally baked into preload.lua by
-- generate-preload.uc; if the preload did not set it, default to 0 so the
-- field is always present (the learner treats 0 as "pre-generation tracking").
if not ORCHESTRA_PRELOAD_GENERATION then ORCHESTRA_PRELOAD_GENERATION = 0 end

-- Emit a start lifecycle event so the learner can attribute a new run and
-- reset its in-memory consecutive-fail counter.  This is the only place a
-- start event is emitted.
if orchestra_emit_event then
    orchestra_emit_event("start", {message = ORCHESTRA_RUN_ID, generation = ORCHESTRA_PRELOAD_GENERATION})
end

if DLOG then DLOG("orchestra-extra TLS runtime loaded (run_id=" .. tostring(ORCHESTRA_RUN_ID) .. ")") end
