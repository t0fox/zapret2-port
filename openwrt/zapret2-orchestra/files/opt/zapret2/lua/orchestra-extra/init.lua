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
if DLOG then DLOG("orchestra-extra TLS runtime loaded") end
