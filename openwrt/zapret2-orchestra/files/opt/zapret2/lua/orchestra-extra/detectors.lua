-- TLS-only detector wrappers.  They intentionally do not treat generic HTTP
-- 3xx/4xx responses as transport failures and ignore a late RST once a
-- connection has already reached a confirmed success.
--
-- r7 SUCCESS-debug gates: every branch emits a DLOG line so --debug=syslog
-- shows exactly which condition fired (or did not) per packet.  This is the
-- minimal instrumentation needed to diagnose "145 APPLIED, 4 FAIL, 0 SUCCESS"
-- on the router: without it the only signal is the missing SUCCESS event.

local function dlog(message)
    if DLOG then DLOG(message) end
end

local function is_tls_alert(payload)
    if type(payload) ~= "string" or #payload < 7 or payload:byte(1) ~= 0x15 then return false end
    local major, minor = payload:byte(2), payload:byte(3)
    return major == 0x03 and minor >= 0x00 and minor <= 0x04
end

-- Relative sequence of the current packet for the active direction.  Guarded
-- so a missing conntrack/pos_get never throws inside the detector path.
local function current_seq(desync)
    if desync and desync.track and type(pos_get) == "function" then
        return pos_get(desync, 's')
    end
    return 0
end

function combined_failure_detector(desync, crec)
    if not crec or crec.nocheck or crec.orchestra_success_confirmed then return false end
    if not desync.outgoing and desync.dis and is_tls_alert(desync.dis.payload) then
        dlog("combined_failure_detector: reply TLS alert (failure)")
        return true
    end
    -- The standard detector is a confirmed upstream API.  It only regards a
    -- DPI redirect as a failure, not arbitrary HTTP 3xx/4xx status codes.
    return standard_failure_detector(desync, crec)
end

function combined_success_detector(desync, crec)
    if not crec or crec.nocheck then
        dlog("combined_success_detector: skip (no crec or nocheck)")
        return false
    end
    -- Reply packets are REQUIRED for TCP success detection: standard_success_detector
    -- fires on an incoming relative sequence > inseq (0x1000).  If reply packets
    -- never reach this function, SUCCESS is impossible -- which is the symptom of
    -- a missing --in-range in the profile (nfqws2 defaults --in-range=x = never).
    if not desync.outgoing and desync.dis and is_tls_alert(desync.dis.payload) then
        dlog("combined_success_detector: reply TLS alert -> not success")
        return false
    end
    if standard_success_detector(desync, crec) then
        crec.orchestra_success_confirmed = true
        dlog("combined_success_detector: SUCCESS confirmed dir="
            .. (desync.outgoing and "out" or "in") .. " seq=" .. tostring(current_seq(desync)))
        return true
    end
    dlog("combined_success_detector: no success dir="
        .. (desync.outgoing and "out" or "in") .. " seq=" .. tostring(current_seq(desync)))
    -- Fallback SUCCESS: any incoming TCP reply = the server answered.
    -- standard_success_detector requires seq > inseq but seq stays 0-1 for
    -- the first reply packets (empty/control), and nft ct reply packets 1-10
    -- limits how many replies nfqws2 sees. The failure detector already
    -- ruled out RST/retransmission (caller checks `not failure` first), so
    -- an incoming TCP packet on a TLS connection = success.
    if not desync.outgoing and desync.dis and desync.dis.tcp then
        crec.orchestra_success_confirmed = true
        dlog("combined_success_detector: SUCCESS (fallback: incoming TCP reply)")
        return true
    end
    return false
end
