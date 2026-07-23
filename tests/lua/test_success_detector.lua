-- Run on any Lua 5.1+ host: lua tests/lua/test_success_detector.lua <repo-root>
-- Exercises the REAL combined_success_detector (lua/orchestra-extra/detectors.lua)
-- with mock desync/crec tables and a faithful mock of standard_success_detector
-- (mirroring zapret2-core/lua/zapret-auto.lua:226) so the SUCCESS verdicts are
-- verified against the actual wrapper logic, not a model.
local root = assert(arg[1], "repository root is required")

-- ---- mock the nfqws2 Lua globals detectors.lua depends on ---------------
-- standard_success_detector: faithful TCP reimplementation (zapret-auto.lua).
function standard_success_detector(desync, crec)
    local arg = desync.arg or {}
    local inseq = tonumber(arg.inseq) or 0x1000
    local maxseq = tonumber(arg.maxseq) or 32768
    if desync.dis and desync.dis.tcp then
        local seq = pos_get(desync, 's')
        if desync.outgoing then
            return maxseq > 0 and seq > maxseq
        else
            return inseq > 0 and seq > inseq
        end
    end
    return false
end
-- pos_get: return the current packet's relative seq for the active direction.
function pos_get(desync, mode)
    if mode == 's' then return desync._seq or 0 end
    return 0
end
function DLOG() end  -- silent; the wrapper calls DLOG at each gate

-- ---- load the real wrapper ----------------------------------------------
dofile(root .. "/lua/orchestra-extra/detectors.lua")

local function equal(actual, expected, message)
    assert(actual == expected, (message or "values differ") ..
        ": expected " .. tostring(expected) .. ", got " .. tostring(actual))
end

-- Build a mock desync for a TCP packet in a given direction with a relative
-- sequence and optional payload bytes (a Lua string).
local function mock_desync(outgoing, seq, payload, inseq)
    return {
        outgoing = outgoing,
        _seq = seq,
        arg = { inseq = inseq or 0x1000 },
        dis = { tcp = {}, payload = payload },
        track = {},  -- present so current_seq/pos_get guards pass
    }
end
local function fresh_crec()
    return { nocheck = false }
end

-- reply TLS ServerHello with seq beyond inseq => SUCCESS
local crec = fresh_crec()
equal(combined_success_detector(mock_desync(false, 4381, "\16\3\3" .. string.rep("\0", 50)), crec),
    true, "reply seq>inseq is SUCCESS")
assert(crec.orchestra_success_confirmed, "success must set orchestra_success_confirmed")

-- reply with seq below inseq => not SUCCESS
equal(combined_success_detector(mock_desync(false, 1, "\16\3\3" .. string.rep("\0", 50)), fresh_crec()),
    false, "reply seq<inseq is not SUCCESS")
equal(combined_success_detector(mock_desync(false, 4096, "\16\3\3" .. string.rep("\0", 50)), fresh_crec()),
    false, "reply seq==inseq is not SUCCESS (strict >)")

-- outgoing ClientHello (seq 500 << maxseq 32768) => no false SUCCESS
equal(combined_success_detector(mock_desync(true, 500, "\16\3\1" .. string.rep("\0", 50)), fresh_crec()),
    false, "outgoing ClientHello is not a false SUCCESS")

-- reply TLS alert (ContentType 0x15) => not SUCCESS even with seq > inseq
local alert = "\21\3\3\0\2\2\40"  -- fatal handshake_failure
equal(combined_success_detector(mock_desync(false, 5000, alert), fresh_crec()),
    false, "reply TLS alert is not SUCCESS")

-- nocheck crec => skip, not SUCCESS
local nc = { nocheck = true }
equal(combined_success_detector(mock_desync(false, 9999, "\16\3\3"), nc), false, "nocheck skips")

print("ok: test_success_detector")
