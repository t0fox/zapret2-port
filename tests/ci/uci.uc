// CI-only stub for the OpenWrt "uci" ucode module.
//
// The production rpcd backend (usr/share/rpcd/ucode/zapret2.orchestra) opens
// with `import { cursor } from 'uci'`. The real uci module is an OpenWrt-native
// shared library (lib/uci.c -> uci.so) that pulls in libubox and the uci config
// store, neither of which exists on the GitHub runner. Building it host-side is
// disproportionate to the goal here: we only need the import to RESOLVE so
// `ucode -L tests/ci -c` can syntax-check the rpcd backend the same way it
// already checks apply.uc and generate-preload.uc.
//
// This stub exports only cursor() -- the single binding the backend imports --
// as a callable no-op. It is never executed during `-c` (compile only, no VM
// run) and is never shipped in the APK; it lives only under tests/ci and is
// added to the ucode module search path via the `-L` flag in CI.
//
// Module resolution: ucode's import looks for `uci.so` then `uci.uc` along the
// search path (CMakeLists LIB_SEARCH_PATH + `-L` entries). `-L <dir>` with no
// `*` adds both `<dir>/*.so` and `<dir>/*.uc`, so this file is found as uci.uc.
//
// Do NOT import this in production code. Do NOT ship it. Do NOT edit the
// production rpcd backend to accommodate it -- its syntax is already correct.

function cursor() {
	return {};
}

export { cursor };
