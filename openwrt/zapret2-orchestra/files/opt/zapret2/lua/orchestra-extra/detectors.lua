-- TLS-only detector wrappers.  They intentionally do not treat generic HTTP
-- 3xx/4xx responses as transport failures and ignore a late RST once a
-- connection has already reached a confirmed success.

local function is_tls_alert(payload)
    if type(payload) ~= "string" or #payload < 7 or payload:byte(1) ~= 0x15 then return false end
    local major, minor = payload:byte(2), payload:byte(3)
    return major == 0x03 and minor >= 0x00 and minor <= 0x04
end

function combined_failure_detector(desync, crec)
    if not crec or crec.nocheck or crec.orchestra_success_confirmed then return false end
    if not desync.outgoing and desync.dis and is_tls_alert(desync.dis.payload) then return true end
    -- The standard detector is a confirmed upstream API.  It only regards a
    -- DPI redirect as a failure, not arbitrary HTTP 3xx/4xx status codes.
    return standard_failure_detector(desync, crec)
end

function combined_success_detector(desync, crec)
    if not crec or crec.nocheck then return false end
    if not desync.outgoing and desync.dis and is_tls_alert(desync.dis.payload) then return false end
    if standard_success_detector(desync, crec) then
        crec.orchestra_success_confirmed = true
        return true
    end
    return false
end
