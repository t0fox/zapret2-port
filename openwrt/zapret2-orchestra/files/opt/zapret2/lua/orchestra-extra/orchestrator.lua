-- TLS quality-aware strategy selector built solely on the upstream v5 Lua API
-- (`orchestrate`, `automate_*`, and `plan_instance_*`).

local function selected_next(askey, host, current, count)
    for _ = 1, count do
        current = (current % count) + 1
        if not slm_is_blocked(askey, host, current) then return current end
    end
    return nil
end

local function strategy_count(plan)
    local seen, count = {}, 0
    for _, instance in pairs(plan) do
        local strategy = instance.arg and tonumber(instance.arg.strategy)
        if strategy then
            if strategy < 1 or strategy % 1 ~= 0 then error("circular_quality: invalid strategy") end
            seen[strategy] = true
        end
    end
    while seen[count + 1] do count = count + 1 end
    for strategy, _ in pairs(seen) do
        if strategy > count then error("circular_quality: strategies must be contiguous from 1") end
    end
    return count
end

function circular_quality(ctx, desync)
    if desync.replay_seq then return VERDICT_PASS end
    orchestrate(ctx, desync)
    if not desync.plan or #desync.plan == 0 then return VERDICT_PASS end
    if not desync.track then
        if DLOG_ERR then DLOG_ERR("circular_quality: conntrack is required") end
        return VERDICT_PASS
    end
    local hrec = automate_host_record(desync)
    if not hrec then return VERDICT_PASS end
    hrec.ctstrategy = hrec.ctstrategy or strategy_count(desync.plan)
    if hrec.ctstrategy == 0 then error("circular_quality: add contiguous strategy=N arguments") end
    local askey = desync.arg.key or desync.func_instance or "tls"
    local host = slm_normalize_hostkey(standard_hostkey(desync))
    if not host then return VERDICT_PASS end
    if ORCHESTRA_WHITELIST and ORCHESTRA_WHITELIST[host] then
        return VERDICT_PASS
    end
    hrec.nstrategy = hrec.nstrategy or 1
    if slm_is_blocked(askey, host, hrec.nstrategy) then
        hrec.nstrategy = selected_next(askey, host, hrec.nstrategy, hrec.ctstrategy)
    end
    if not hrec.nstrategy then return VERDICT_PASS end

    local crec = automate_conn_record(desync)
    local failure = combined_failure_detector(desync, crec)
    local success = not failure and combined_success_detector(desync, crec)
    local locked = slm_get_locked(askey, host)
    if locked and slm_is_blocked(askey, host, locked) then
        slm_reset(askey, host)
        locked = nil
    end
    if locked then
        hrec.nstrategy = locked
        if failure and not crec.locked_failure_recorded then
            crec.locked_failure_recorded = true
            hrec.locked_fail_count = (hrec.locked_fail_count or 0) + 1
            slm_record_result(askey, host, locked, false)
            if hrec.locked_fail_count >= (tonumber(desync.arg.unlock_fails) or 3) and not slm_is_user_lock(askey, host) then
                if orchestra_emit_event then orchestra_emit_event("unlock", {host=host, protocol=askey, strategy=locked, state="auto"}) end
                slm_reset(askey, host)
                hrec.nstrategy = selected_next(askey, host, locked, hrec.ctstrategy)
                hrec.locked_fail_count = 0
            end
        elseif success and not crec.locked_success_recorded then
            crec.locked_success_recorded = true
            hrec.locked_fail_count = 0
            slm_record_result(askey, host, locked, true)
        end
    elseif failure and not crec.quality_failure_recorded then
        crec.quality_failure_recorded = true
        slm_record_result(askey, host, hrec.nstrategy, false)
        if automate_failure_counter(hrec, crec, tonumber(desync.arg.fails) or 1, tonumber(desync.arg.time) or 60) then
            hrec.nstrategy = selected_next(askey, host, hrec.nstrategy, hrec.ctstrategy) or hrec.nstrategy
            if orchestra_emit_event then orchestra_emit_event("rotate", {host=host, protocol=askey, strategy=hrec.nstrategy}) end
        end
    elseif success and not crec.quality_success_recorded then
        crec.quality_success_recorded = true
        slm_record_result(askey, host, hrec.nstrategy, true)
        automate_failure_counter_reset(hrec)
        local should_lock, strategy = slm_should_lock(askey, host, desync.arg)
        if should_lock then
            hrec.nstrategy = strategy
            if orchestra_emit_event then orchestra_emit_event("lock", {host=host, protocol=askey, strategy=strategy, state="auto"}) end
        end
    end

    local verdict = VERDICT_PASS
    while true do
        local instance = plan_instance_pop(desync)
        if not instance then break end
        if instance.arg and tonumber(instance.arg.strategy) == hrec.nstrategy then
            verdict = plan_instance_execute(desync, verdict, instance)
        end
    end
    return verdict
end
