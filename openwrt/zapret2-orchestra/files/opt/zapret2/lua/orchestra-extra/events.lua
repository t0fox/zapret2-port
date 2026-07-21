-- Compact runtime-only event sink.  It is called only on state transitions,
-- never for every packet, and never writes under /etc.

local EVENT_TYPES = {
    rotate=true, lock=true, unlock=true, error=true, start=true, stop=true
}

local function json_escape(value)
    return tostring(value):gsub("\\", "\\\\"):gsub('"', '\\"'):gsub("\n", "\\n"):gsub("\r", "\\r")
end

local function json_value(value)
    if type(value) == "number" then return tostring(value) end
    if type(value) == "boolean" then return value and "true" or "false" end
    return '"' .. json_escape(value) .. '"'
end

function orchestra_emit_event(event, fields)
    if not EVENT_TYPES[event] then return false end
    fields = fields or {}
    local allowed = {host=true, protocol=true, strategy=true, state=true, message=true, dry_run=true}
    local keys = {}
    for key, _ in pairs(fields) do
        if allowed[key] then keys[#keys + 1] = key end
    end
    table.sort(keys)
    local parts = {'{"event":' .. json_value(event), '"ts":' .. tostring(os.time())}
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
