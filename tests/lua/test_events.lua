-- Run with a disposable directory:
-- lua tests/lua/test_events.lua <repo-root> <temporary-directory>
local root = assert(arg[1], "repository root is required")
local temporary = assert(arg[2], "temporary directory is required")
ORCHESTRA_EVENTS_FILE = temporary .. "/events.ndjson"
ORCHESTRA_EVENTS_MAX_BYTES = 512
dofile(root .. "/lua/orchestra-extra/events.lua")

for strategy = 1, 40 do
    assert(orchestra_emit_event("rotate", {host="video.example", protocol="tls", strategy=strategy}))
end
local file = assert(io.open(ORCHESTRA_EVENTS_FILE, "rb"))
local payload = file:read("*a")
file:close()
assert(#payload <= ORCHESTRA_EVENTS_MAX_BYTES, "event log exceeded bound")
assert(payload:find('"event":"rotate"', 1, true), "event missing")
assert(not payload:find("packet", 1, true), "packet data must not be logged")
os.remove(ORCHESTRA_EVENTS_FILE)
print("ok: test_events")
