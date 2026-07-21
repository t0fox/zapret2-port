-- Control-plane adapter.  Backends must use these functions rather than the
-- desktop slm_set_locked API, which does not mark manual locks as protected.

function orchestra_set_manual_lock(askey, hostname, strategy)
    return slm_preload_locked(askey, hostname, strategy, true)
end

function orchestra_clear_manual_lock(askey, hostname)
    if not slm_is_user_lock(askey, hostname) then return false end
    slm_reset(askey, hostname)
    return true
end
