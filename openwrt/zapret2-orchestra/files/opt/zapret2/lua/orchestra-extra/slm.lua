-- TLS Orchestra strategy lock manager.
--
-- This file is an independent extension: it deliberately does not modify the
-- upstream Zapret2 Lua files.  It is Lua 5.1 compatible and keeps all state in
-- memory; the control-plane is responsible for batching persistence.

SLM_QUALITY = SLM_QUALITY or {}
BLOCKED_STRATEGIES = BLOCKED_STRATEGIES or {}

local function log(message)
    if DLOG then DLOG(message) end
end

function slm_normalize_hostkey(hostname)
    if type(hostname) ~= "string" then return nil end
    local key = hostname:lower():gsub("^%.*", ""):gsub("%.*$", "")
    if key == "" then return nil end
    return key
end

local function quality_record(askey, hostname)
    askey = askey or "default"
    local host = slm_normalize_hostkey(hostname)
    if not host then return nil, nil end
    SLM_QUALITY[askey] = SLM_QUALITY[askey] or {}
    local record = SLM_QUALITY[askey][host]
    if not record then
        record = {
            strategy_successes = {}, strategy_tests = {}, total_tests = 0,
            locked_strategy = nil, lock_reason = nil, is_user_lock = false
        }
        SLM_QUALITY[askey][host] = record
    end
    return record, host
end

local function contains(values, needle)
    for _, value in ipairs(values or {}) do
        if value == needle then return true end
    end
    return false
end

function slm_is_blocked(askey, hostname, strategy)
    local host = slm_normalize_hostkey(hostname)
    if not host or type(strategy) ~= "number" then return false end
    local blocked = BLOCKED_STRATEGIES[askey or "default"]
    if type(blocked) ~= "table" then return false end
    if contains(blocked["*"], strategy) then return true end
    for domain, values in pairs(blocked) do
        if domain ~= "*" and (host == domain or host:sub(-#domain - 1) == "." .. domain) then
            if contains(values, strategy) then return true end
        end
    end
    return false
end

function slm_preload_blocked(askey, hostname, strategies)
    askey = askey or "default"
    local host = slm_normalize_hostkey(hostname)
    if not host or type(strategies) ~= "table" then return false end
    BLOCKED_STRATEGIES[askey] = BLOCKED_STRATEGIES[askey] or {}
    local values = BLOCKED_STRATEGIES[askey][host] or {}
    for _, strategy in ipairs(strategies) do
        if type(strategy) == "number" and strategy > 0 and not contains(values, strategy) then
            values[#values + 1] = strategy
        end
    end
    table.sort(values)
    BLOCKED_STRATEGIES[askey][host] = values
    return true
end

function slm_record_result(askey, hostname, strategy, success)
    if type(strategy) ~= "number" or strategy < 1 then return false end
    local record, host = quality_record(askey, hostname)
    if not record then return false end
    record.strategy_successes[strategy] = record.strategy_successes[strategy] or 0
    record.strategy_tests[strategy] = (record.strategy_tests[strategy] or 0) + 1
    record.total_tests = record.total_tests + 1
    if success then record.strategy_successes[strategy] = record.strategy_successes[strategy] + 1 end
    log("orchestra: result [" .. (askey or "default") .. "] " .. host .. " #" .. strategy .. "=" .. (success and "success" or "fail"))
    return true
end

function slm_get_best(askey, hostname, skip_strategy)
    askey = askey or "default"
    local host = slm_normalize_hostkey(hostname)
    local record = host and SLM_QUALITY[askey] and SLM_QUALITY[askey][host]
    if not record then return nil, 0, 0 end
    local best, best_successes, best_tests = nil, -1, 0
    for strategy, successes in pairs(record.strategy_successes) do
        local tests = record.strategy_tests[strategy] or 0
        if strategy ~= skip_strategy and not slm_is_blocked(askey, host, strategy) then
            if successes > best_successes or (successes == best_successes and tests > best_tests) then
                best, best_successes, best_tests = strategy, successes, tests
            end
        end
    end
    return best, math.max(0, best_successes), best_tests
end

function slm_should_lock(askey, hostname, arg)
    askey = askey or "default"
    local record, host = quality_record(askey, hostname)
    if not record then return false, nil end
    if record.locked_strategy and not slm_is_blocked(askey, host, record.locked_strategy) then
        return true, record.locked_strategy
    end
    if record.locked_strategy then
        record.locked_strategy, record.lock_reason, record.is_user_lock = nil, nil, false
    end
    local min_successes = tonumber(arg and arg.lock_successes) or 3
    local min_tests = tonumber(arg and arg.lock_tests) or 5
    local min_rate = tonumber(arg and arg.lock_rate) or 0.6
    if record.total_tests < min_tests then return false, nil end
    local best, successes, tests = slm_get_best(askey, host, tonumber(arg and arg.skip_strategy) or 1)
    if best and successes >= min_successes and tests > 0 and successes / tests >= min_rate then
        record.locked_strategy = best
        record.lock_reason = "quality"
        record.is_user_lock = false
        log("orchestra: auto-lock [" .. askey .. "] " .. host .. " #" .. best)
        return true, best
    end
    return false, nil
end

function slm_get_locked(askey, hostname)
    local host = slm_normalize_hostkey(hostname)
    local record = host and SLM_QUALITY[askey or "default"] and SLM_QUALITY[askey or "default"][host]
    return record and record.locked_strategy or nil
end

function slm_is_user_lock(askey, hostname)
    local host = slm_normalize_hostkey(hostname)
    local record = host and SLM_QUALITY[askey or "default"] and SLM_QUALITY[askey or "default"][host]
    return record and record.is_user_lock == true or false
end

function slm_preload_locked(askey, hostname, strategy, is_user_lock)
    if type(strategy) ~= "number" or strategy < 1 or slm_is_blocked(askey, hostname, strategy) then return false end
    local record, host = quality_record(askey, hostname)
    if not record then return false end
    record.locked_strategy = strategy
    record.is_user_lock = is_user_lock == true
    record.lock_reason = record.is_user_lock and "user" or "preload"
    log("orchestra: preload lock [" .. (askey or "default") .. "] " .. host .. " #" .. strategy)
    return true
end

function slm_reset(askey, hostname)
    local host = slm_normalize_hostkey(hostname)
    local records = SLM_QUALITY[askey or "default"]
    if host and records then records[host] = nil end
end

function slm_preload_history(askey, hostname, strategy, successes, failures)
    if type(strategy) ~= "number" or strategy < 1 then return false end
    local record = quality_record(askey, hostname)
    if not record then return false end
    successes, failures = tonumber(successes) or 0, tonumber(failures) or 0
    record.strategy_successes[strategy] = successes
    record.strategy_tests[strategy] = successes + failures
    record.total_tests = record.total_tests + successes + failures
    return true
end
