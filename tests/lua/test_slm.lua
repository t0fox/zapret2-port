-- Run on any Lua 5.1+ host: lua tests/lua/test_slm.lua <repo-root>
local root = assert(arg[1], "repository root is required")
dofile(root .. "/lua/orchestra-extra/slm.lua")
dofile(root .. "/lua/orchestra-extra/slm-adapter.lua")

local function equal(actual, expected, message)
    assert(actual == expected, (message or "values differ") .. ": expected " .. tostring(expected) .. ", got " .. tostring(actual))
end

equal(slm_normalize_hostkey(".YouTube.COM..."), "youtube.com", "normalization")
assert(slm_preload_blocked("tls", "youtube.com", { 1, 3 }))
assert(slm_is_blocked("tls", "i.youtube.com", 3), "subdomain block")
assert(not slm_is_blocked("tls", "youtube.com", 2), "unblocked strategy")
assert(slm_preload_blocked("tls", "*", { 4 }))
assert(slm_is_blocked("tls", "any.example", 4), "global block")

for i = 1, 3 do assert(slm_record_result("tls", "video.example", 2, true)) end
for i = 1, 2 do assert(slm_record_result("tls", "video.example", 2, false)) end
local should_lock, strategy = slm_should_lock("tls", "video.example", { lock_successes = 3, lock_tests = 5, lock_rate = 0.6 })
assert(should_lock, "quality lock")
equal(strategy, 2, "quality lock strategy")

assert(orchestra_set_manual_lock("tls", "manual.example", 2))
assert(slm_is_user_lock("tls", "manual.example"), "manual lock must be protected")
assert(orchestra_clear_manual_lock("tls", "manual.example"))
assert(not slm_get_locked("tls", "manual.example"), "manual unlock")
print("ok: test_slm")
