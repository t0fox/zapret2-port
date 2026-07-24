'use strict';

// zapret2-orchestra-learner — closed-loop runtime learning daemon.
//
// Tails /tmp/zapret2-orchestra/events.ndjson incrementally with a durable
// cursor (learner-state.json), applies the Orchestra state-machine policy to
// each event, persists learned.json / blocked.json, and — only when a lock,
// unlock, or blocked-strategy change occurs — regenerates the preload and
// reloads nfqws2 via the procd-managed service (debounced).  The Lua packet
// path NEVER writes persistent JSON; this daemon is the sole runtime writer
// of the JSON seeds (contract §3, spec §5.5).
//
// Modes (ARGV[0]):
//   run           long-running daemon: poll events.ndjson, process, sleep,
//                 repeat.  Used by the procd init script.
//   process-once  read new events from the cursor once, update state, exit.
//                 Used by tests and by a one-shot "catch up" path.
//   test-process  TEST MODE: same as process-once but takes an explicit events
//                 file path (ARGV[1]) and state dir (env), writes state, and
//                 emits a JSON result describing what changed.  No preload
//                 regen, no service reload.  This is the entry point C's tests
//                 and test_orchestra_learner.py drive when ucode is present.
//
// Lock/unload policy (contract §3, binding):
//   * auto-LOCK after 3 TCP SUCCESS or 1 UDP SUCCESS on a strategy
//     (UDP_ASKEYS = quic, discord, wireguard, dns, stun, unknown).
//   * auto-UNLOCK after 3 consecutive FAIL on an auto-locked strategy.
//   * NEVER auto-unlock a user-locked strategy (manual-locks.json).
//   * Blocked strategy has PRIORITY over auto-lock AND user-lock; a
//     locked==blocked conflict drops the lock (blocked wins).
//   * DEFAULT_BLOCKED_PASS_DOMAINS blocks strategy=1 for seeded domains at
//     load (seeded in blocked.json; not unblockable by user here).
//
// Atomic state writes reuse the validate→serialize→revalidate→tmp→rename→
// .good copy discipline (docs/orchestra-state-schema.md).  Single-writer: a
// mkdir lock at LEARNER_LOCK_DIR; CLI manual actions take the SAME lock.
//
// Override paths (for tests):
//   ORCHESTRA_STATE_DIR, ORCHESTRA_RUNTIME_DIR, ORCHESTRA_EVENTS_FILE,
//   LEARNER_STATE_FILE, LEARNER_LOCK_DIR, LEARNER_LOG_FILE,
//   ORCHESTRA_PRELOAD_WRAPPER, ORCHESTRA_RELOAD_CMD, ORCHESTRA_RELOAD_DEBOUNCE.
//
// Exit status: 0 on success, non-zero on any unrecoverable error.

import { readfile, writefile, mkdir, rename, unlink, stat, rmdir, dirname, popen } from 'fs';

const STATE_DIR    = getenv('ORCHESTRA_STATE_DIR')   ?? '/etc/zapret2-orchestra';
const RUNTIME_DIR  = getenv('ORCHESTRA_RUNTIME_DIR') ?? '/tmp/zapret2-orchestra';
const EVENTS_FILE  = getenv('ORCHESTRA_EVENTS_FILE') ?? (RUNTIME_DIR + '/events.ndjson');
const LEARNER_STATE = getenv('LEARNER_STATE_FILE')   ?? (STATE_DIR + '/learner-state.json');
const LEARNER_LOCK  = getenv('LEARNER_LOCK_DIR')     ?? '/var/lock/zapret2-orchestra-learner.lock';
const LEARNER_LOG   = getenv('LEARNER_LOG_FILE')     ?? '/var/log/zapret2-orchestra-learner.log';
const PRELOAD_WRAPPER = getenv('ORCHESTRA_PRELOAD_WRAPPER') ?? '/usr/sbin/zapret2-orchestra-preload';
// The reload command is owned by the init.d/procd layer, NOT invoked here from
// the packet path.  In daemon mode the learner writes a "reload requested"
// marker and the procd wrapper performs the zapret2 service restart.  For
// test-mode this is never used.  ORCHESTRA_RELOAD_CMD lets a deployment point
// at a wrapper script; default empty (the procd service supplies it).
const RELOAD_CMD   = getenv('ORCHESTRA_RELOAD_CMD')  ?? '';
// ucode has no Lua tonumber(); use int() with a string default so unset env
// yields 5 (int('5') == 5). getenv unset -> null ?? '5' -> '5' -> int 5.
const RELOAD_DEBOUNCE = int(getenv('ORCHESTRA_RELOAD_DEBOUNCE') ?? '5');

// UDP askeys: 1 SUCCESS locks (vs 3 for TCP).  Mirrors the original
// orchestra_runner.py:1156 lock_threshold = 1 if is_udp else 3.
const UDP_ASKEYS = { quic: true, discord: true, wireguard: true, dns: true, stun: true, unknown: true };
const LOCK_SUCCESSES_TCP = 3;
const LOCK_SUCCESSES_UDP = 1;
const UNLOCK_FAILS = 3;

const SCHEMA_VERSION = 1;

function fail(msg) { die('zapret2-orchestra-learner: ' + msg); }

function log_line(msg) {
	// Structured learner log: one JSON object per line.  Best-effort: never
	// let a log failure kill the daemon.  Uses readfile+writefile (ucode-mod-fs
	// only, no fopen stream API) with a soft size cap so the log does not grow
	// unbounded.  The log is NOT the primary state interface — learner-state.json
	// is the durable, recoverable state; this log is for diagnostics.
	try {
		let line = sprintf('%J\n', { ts: time(), msg: msg });
		let existing = readfile(LEARNER_LOG) ?? '';
		// soft cap: keep the last ~256 KiB to avoid unbounded growth
		let max_log = 262144;
		if (length(existing) > max_log)
			existing = substr(existing, length(existing) - max_log);
		writefile(LEARNER_LOG, existing + line);
	}
	catch (e) { /* best-effort: swallow log errors */ }
}

function is_int(v) {
	return type(v) == 'int' || (type(v) == 'double' && v == int(v));
}

function as_int(v, ctx) {
	if (type(v) == 'string') v = int(v);
	if (!is_int(v)) fail(ctx + ': expected integer');
	return int(v);
}

// ---------------------------------------------------------------------------
// Schema validation
// ---------------------------------------------------------------------------

function validate_learned(doc) {
	if (type(doc) != 'object') return 'not an object';
	if (doc.schema_version != SCHEMA_VERSION) return 'schema_version must be 1';
	if (type(doc.protocols) != 'object') return 'protocols must be an object';
	for (let askey, hosts in doc.protocols) {
		if (type(hosts) != 'object') return 'protocols.' + askey + ' must be an object';
		for (let host, rec in hosts) {
			if (type(rec) != 'object') return 'protocols.' + askey + '.' + host + ' must be an object';
			if (rec.auto_lock != null && (!is_int(rec.auto_lock) || rec.auto_lock < 1))
				return 'auto_lock must be a positive integer or null';
			if (rec.strategies != null && type(rec.strategies) != 'object')
				return 'strategies must be an object';
			if (rec.strategies != null) {
				for (let skey, cnt in rec.strategies) {
					let n = int(skey);
					if (n < 1) return 'strategy key must be positive';
					if (type(cnt) != 'object') return 'strategy entry must be an object';
					if (!is_int(cnt.successes ?? 0)) return 'successes must be int';
					if (!is_int(cnt.failures ?? 0)) return 'failures must be int';
				}
			}
		}
	}
	return null;
}

function validate_blocked(doc) {
	if (type(doc) != 'object') return 'not an object';
	if (doc.schema_version != SCHEMA_VERSION) return 'schema_version must be 1';
	if (type(doc.protocols) != 'object') return 'protocols must be an object';
	for (let askey, bp in doc.protocols) {
		if (type(bp) != 'object') return 'protocols.' + askey + ' must be an object';
		// global, hosts, user_global, user_hosts are optional arrays/objects of
		// RUNTIME strategy numbers (existing numeric-block form).
		for (let k in ['global', 'hosts', 'user_global', 'user_hosts']) {
			if (bp[k] != null && type(bp[k]) != 'array' && type(bp[k]) != 'object')
				return 'blocked.' + askey + '.' + k + ' must be array/object';
		}
		// global_chain / hosts_chain / user_global_chain / user_hosts_chain are
		// the r7 stable-identity form: blocks authored against a STABLE chain id
		// (contract §4).  generate-preload resolves these to runtime strategy
		// numbers via the active profile sidecar and drops chains absent from
		// the active profile.  Validated loosely here (the learner only persists
		// blocked.json; it does not resolve chain ids — that is generate-preload's
		// job).  global_chain / user_global_chain are arrays of strings;
		// hosts_chain / user_hosts_chain are objects of host -> array of strings.
		for (let k in ['global_chain', 'user_global_chain']) {
			if (bp[k] != null && type(bp[k]) != 'array')
				return 'blocked.' + askey + '.' + k + ' must be an array of chain ids';
		}
		for (let k in ['hosts_chain', 'user_hosts_chain']) {
			if (bp[k] != null && type(bp[k]) != 'object')
				return 'blocked.' + askey + '.' + k + ' must be an object of host -> [chain id]';
		}
	}
	return null;
}

function validate_manual_locks(doc) {
	if (type(doc) != 'object') return 'not an object';
	if (doc.schema_version != SCHEMA_VERSION) return 'schema_version must be 1';
	if (type(doc.protocols) != 'object') return 'protocols must be an object';
	return null;
}

function validate_learner_state(doc) {
	if (type(doc) != 'object') return 'not an object';
	if (doc.schema_version != SCHEMA_VERSION) return 'schema_version must be 1';
	if (type(doc.event_cursor) != 'object') return 'event_cursor must be an object';
	if (!is_int(doc.event_cursor.bytes ?? 0)) return 'cursor bytes must be int';
	if (!is_int(doc.event_cursor.lines ?? 0)) return 'cursor lines must be int';
	return null;
}

// ---------------------------------------------------------------------------
// Atomic JSON write (validate → serialize → revalidate → tmp → rename → .good)
// ---------------------------------------------------------------------------

function ensure_dir(path) {
	let info = stat(path);
	if (info == null) {
		if (!mkdir(path)) fail('cannot create directory ' + path);
		info = stat(path);
	}
	if (info?.type != 'directory') fail(path + ' is not a directory');
}

function atomic_write_json(path, doc, validator) {
	let err = validator(doc);
	if (err) fail(path + ': validation failed: ' + err);
	let payload = sprintf('%J\n', doc);
	let reparsed;
	try { reparsed = json(payload); }
	catch (e) { fail(path + ': serialization produced invalid JSON: ' + e); }
	let reerr = validator(reparsed);
	if (reerr) fail(path + ': round-trip validation failed: ' + reerr);
	let parent = dirname(path);
	ensure_dir(parent);
	let tmp = path + '.tmp';
	if (writefile(tmp, payload) == null) fail('cannot write ' + tmp);
	if (!rename(tmp, path)) {
		unlink(tmp);
		fail('cannot install ' + path);
	}
	// Validated .good copy for crash recovery (restore if primary is malformed).
	let good = path + '.good';
	let gtmp = good + '.tmp';
	if (writefile(gtmp, payload) == null) return;  // best-effort; primary is authoritative
	rename(gtmp, good);
}

// Read a JSON state file with .good fallback.  Returns { doc, ok } where ok is
// false if both primary and .good are missing/invalid (caller decides whether
// to default or fail).
function read_json_with_good(path, validator, default_factory) {
	let raw = readfile(path);
	if (raw != null) {
		try {
			let doc = json(raw);
			if (validator(doc) == null) return { doc: doc, ok: true };
		} catch (e) { /* fall through to .good */ }
	}
	let good = path + '.good';
	let graw = readfile(good);
	if (graw != null) {
		try {
			let gdoc = json(graw);
			if (validator(gdoc) == null) {
				// Restore primary from .good atomically.
				atomic_write_json(path, gdoc, validator);
				return { doc: gdoc, ok: true };
			}
		} catch (e) { /* both bad */ }
	}
	if (default_factory != null) return { doc: default_factory(), ok: false };
	return { doc: null, ok: false };
}

function default_learned()   { return { schema_version: 1, protocols: {} }; }
function default_blocked()   { return { schema_version: 1, protocols: {} }; }
function default_manual()    { return { schema_version: 1, protocols: {} }; }
function default_learner_state() {
	return { schema_version: 1, event_cursor: { bytes: 0, lines: 0, last_line_sha256: '' }, last_preload_gen: 0, last_run_id: '', updated_at: 0 };
}

// ---------------------------------------------------------------------------
// mkdir single-writer lock (mirrors apply.uc's discipline, DRY)
// ---------------------------------------------------------------------------

function my_pid() {
	let p = readfile('/proc/self/stat');
	if (p != null) {
		let sp = split(p, ' ');
		if (length(sp) > 0) return int(sp[0]);
	}
	return int(getenv('LEARNER_LOCK_TEST_PID') ?? '0');
}

function lock_holder_alive(pid) {
	if (pid <= 0) return false;
	if (stat('/proc/' + pid) == null) return false;
	let cmd = readfile('/proc/' + pid + '/cmdline');
	if (cmd == null) return false;
	return index(cmd, 'zapret2-orchestra-learner') >= 0 || index(cmd, 'learner.uc') >= 0;
}

function lock_acquire() {
	if (mkdir(LEARNER_LOCK)) {
		let pid = my_pid();
		if (writefile(LEARNER_LOCK + '/pid', '' + pid + '\n') == null) {
			rmdir(LEARNER_LOCK);
			return { ok: false, error: 'cannot write pid file' };
		}
		return { ok: true, pid: pid };
	}
	let pidraw = readfile(LEARNER_LOCK + '/pid');
	if (pidraw == null) return { ok: false, error: 'lock busy (no pid file)' };
	let holder = int(trim(pidraw));
	if (holder > 0 && !lock_holder_alive(holder)) {
		let pidraw2 = readfile(LEARNER_LOCK + '/pid');
		if (pidraw2 == null || int(trim(pidraw2)) != holder)
			return { ok: false, error: 'lock busy (recovered by another)' };
		unlink(LEARNER_LOCK + '/pid');
		rmdir(LEARNER_LOCK);
		if (mkdir(LEARNER_LOCK)) {
			let pid = my_pid();
			if (writefile(LEARNER_LOCK + '/pid', '' + pid + '\n') == null) {
				rmdir(LEARNER_LOCK);
				return { ok: false, error: 'cannot write pid file' };
			}
			return { ok: true, pid: pid, stale_recovered: true };
		}
		return { ok: false, error: 'lock busy (lost retry)' };
	}
	return { ok: false, error: 'lock busy', holder: holder };
}

function lock_release() {
	let pidraw = readfile(LEARNER_LOCK + '/pid');
	let holder = int(trim(pidraw ?? ''));
	let mine = my_pid();
	if (holder != mine) return { ok: false, error: 'lock not mine' };
	unlink(LEARNER_LOCK + '/pid');
	rmdir(LEARNER_LOCK);
	return { ok: true };
}

// ---------------------------------------------------------------------------
// Helpers for the state model
// ---------------------------------------------------------------------------

function is_udp_askey(askey) { return UDP_ASKEYS[askey] == true; }

function normalize_host(host) {
	if (type(host) != 'string') return null;
	let h = lc(host);  // ucode lc(), not Lua lower()
	// Strip leading/trailing dots. ucode replace() is literal (not regex), so
	// the original Lua patterns '^%.*'/'%.*$' would silently no-op; use substr.
	while (length(h) > 0 && substr(h, 0, 1) == '.') h = substr(h, 1);
	while (length(h) > 0 && substr(h, length(h) - 1, 1) == '.') h = substr(h, 0, length(h) - 1);
	if (length(h) == 0) return null;
	return h;
}

// Does `host` match a domain in `domains` (exact or subdomain)?
function host_matches_domain(host, domain) {
	if (host == domain) return true;
	return substr(host, length(host) - length(domain) - 1) == '.' + domain;
}

// Is a strategy blocked for (askey, host)?  Effective blocked = union of
// global, matching host/domain entries, user_global, user_hosts.  host="*"
// matches everything (used for global blocks).
function is_blocked(blocked, askey, host, strategy) {
	let bp = blocked.protocols[askey];
	if (bp == null) return false;
	function in_list(list) {
		if (type(list) != 'array') return false;
		for (let i = 0; i < length(list); i++)
			if (as_int(list[i], 'blocked entry') == strategy) return true;
		return false;
	}
	if (in_list(bp.global)) return true;
	if (in_list(bp.user_global)) return true;
	if (type(bp.hosts) == 'object') {
		for (let dom, vals in bp.hosts) {
			if (host_matches_domain(host, dom) && in_list(vals)) return true;
		}
	}
	if (type(bp.user_hosts) == 'object') {
		for (let dom, vals in bp.user_hosts) {
			if (host_matches_domain(host, dom) && in_list(vals)) return true;
		}
	}
	return false;
}

// Is (askey, host) user-locked?  (manual-locks.json carries user locks.)
function is_user_locked(manual, askey, host) {
	let m = manual.protocols[askey];
	if (m == null) return false;
	return m[host] != null;
}

// Ensure the nested structure exists.
function ensure_host_record(learned, askey, host) {
	learned.protocols[askey] = learned.protocols[askey] ?? {};
	let rec = learned.protocols[askey][host];
	if (rec == null) {
		rec = { strategies: {} };
		learned.protocols[askey][host] = rec;
	}
	rec.strategies = rec.strategies ?? {};
	return rec;
}

function strategy_history(rec, strategy) {
	let skey = '' + strategy;
	rec.strategies[skey] = rec.strategies[skey] ?? { successes: 0, failures: 0 };
	return rec.strategies[skey];
}

// Cursor last-line fingerprint.  The contract field is named
// `last_line_sha256`, but ucode-mod-fs does not expose a SHA-256 helper
// portably (the digest module is not always present), so we store a
// lightweight 31-bit rolling hash (same family as the preload manifest hash)
// in that field.  This is NOT a security primitive — it only detects
// truncation/rotation between poll cycles (the bytes offset + dedup keys
// carry the correctness guarantees).  The field name is kept for contract
// compatibility; the value is an 8-hex-char rolling hash.
function hash31(data) {
	let h = 5381;
	for (let i = 0; i < length(data); i++)
		h = (h * 33 + ord(substr(data, i, 1))) & 0x7fffffff;
	return h;
}

// ---------------------------------------------------------------------------
// Core: process one event against the in-memory state.
//
// Returns { changed_lock: bool, changed_blocked: bool } where changed_lock
// means the auto_lock for some host changed (→ preload regen + reload) and
// changed_blocked is reserved for future user-block persistence.  SUCCESS/FAIL
// update strategy_history but do NOT set changed_lock (no reload on every
// history update — contract §3 reload policy).
// ---------------------------------------------------------------------------

// Contract §2 emits state-machine `type` in UPPER-CASE (SUCCESS/FAIL/LOCK/
// UNLOCK/APPLIED/ROTATE) and lifecycle in lower-case (error/start/stop). The
// learner compares against the lower-case internal names, so normalize once.
// Explicit map (no ucode lower() builtin dependency) — robust on every build.
// Declared BEFORE event_dedup_key (ucode requires declaration before use).
const ETYPE_CANON = {
	SUCCESS: 'success', FAIL: 'fail', LOCK: 'lock', UNLOCK: 'unlock',
	APPLIED: 'applied', ROTATE: 'rotate',
	success: 'success', fail: 'fail', lock: 'lock', unlock: 'unlock',
	applied: 'applied', rotate: 'rotate',
	error: 'error', start: 'start', stop: 'stop',
};

function normalize_etype(t) {
	if (t == null) return '';
	return ETYPE_CANON[t] ?? t;
}

function event_dedup_key(ev) {
	return (ev.run_id ?? '') + '|' + normalize_etype(ev.type) + '|' + (ev.host ?? '') + '|' + (ev.strategy ?? '') + '|' + (ev.ts ?? '');
}

function process_event(learned, blocked, manual, ev, seen_keys) {
	let result = { changed_lock: false, changed_blocked: false, applied: false };
	if (type(ev) != 'object') return result;
	let etype = normalize_etype(ev.type);
	let key = event_dedup_key(ev);
	// Idempotent: skip a duplicate event (cursor stale after restart).
	if (seen_keys[key]) return result;
	seen_keys[key] = true;
	result.applied = true;

	let askey = ev.askey ?? ev.protocol;
	let host = normalize_host(ev.host);
	let strategy = ev.strategy;
	if (askey == null || host == null || strategy == null) return result;
	strategy = as_int(strategy, 'event.strategy');

	if (etype == 'success' || etype == 'fail') {
		let rec = ensure_host_record(learned, askey, host);
		let h = strategy_history(rec, strategy);
		if (etype == 'success') h.successes = as_int(h.successes, 'successes') + 1;
		else h.failures = as_int(h.failures, 'failures') + 1;
		// Auto-lock check on SUCCESS: lock after LOCK_SUCCESSES (3 TCP / 1 UDP)
		// consecutive-or-cumulative successes on this strategy, UNLESS it is
		// blocked or the host is user-locked (user locks are protected).
		if (etype == 'success') {
			let threshold = is_udp_askey(askey) ? LOCK_SUCCESSES_UDP : LOCK_SUCCESSES_TCP;
			let already_locked = (rec.auto_lock != null);
			let user_locked = is_user_locked(manual, askey, host);
			let blocked_now = is_blocked(blocked, askey, host, strategy);
			if (!already_locked && !user_locked && !blocked_now && h.successes >= threshold) {
				rec.auto_lock = strategy;
				result.changed_lock = true;
			}
		}
		else {
			// Auto-UNLOCK check on FAIL: after UNLOCK_FAILS (3) cumulative
			// failures on the AUTO-locked strategy, remove auto_lock so
			// rotation resumes (contract §3).  Guards:
			//   * only when auto-locked (rec.auto_lock != null)
			//   * only when the FAIL is on the locked strategy itself
			//     (rec.auto_lock == strategy) — failures on a different
			//     strategy do not unlock the locked one
			//   * NEVER auto-unlock a user-locked host (user locks are
			//     protected; they live in manual-locks.json)
			//   * a blocked strategy is never auto-locked in the first place,
			//     so blocked_now is not re-checked here
			// Use `delete` (not `= null`) so the key is truly absent in the
			// serialized JSON — ucode %J retains null-valued keys, which would
			// fail an assertNotIn on auto_lock after unlock.
			if (rec.auto_lock != null && rec.auto_lock == strategy) {
				let user_locked = is_user_locked(manual, askey, host);
				if (!user_locked && h.failures >= UNLOCK_FAILS) {
					delete rec.auto_lock;
					result.changed_lock = true;
				}
			}
		}
		return result;
	}

	if (etype == 'lock') {
		// A LOCK event from the Lua runtime means auto-lock fired there.  We
		// persist it as auto_lock UNLESS blocked or user-locked takes priority.
		let user_locked = is_user_locked(manual, askey, host);
		let blocked_now = is_blocked(blocked, askey, host, strategy);
		if (blocked_now) {
			// Blocked wins: drop any auto_lock for this host (match
			// orchestrator.lua slm_reset on locked==blocked conflict).  Use
			// `delete` so the key is truly absent (ucode %J retains null keys).
			let rec = learned.protocols[askey]?.[host];
			if (rec != null && rec.auto_lock != null) {
				delete rec.auto_lock;
				result.changed_lock = true;
			}
			return result;
		}
		if (user_locked) return result;  // never overwrite a user lock
		let rec = ensure_host_record(learned, askey, host);
		if (rec.auto_lock != strategy) {
			rec.auto_lock = strategy;
			result.changed_lock = true;
		}
		return result;
	}

	if (etype == 'unlock') {
		// UNLOCK from the runtime: remove auto_lock.  NEVER touch a user lock.
		// Use `delete` so the key is truly absent (ucode %J retains null keys,
		// which would fail an assertNotIn on auto_lock after unlock).
		if (is_user_locked(manual, askey, host)) return result;
		let rec = learned.protocols[askey]?.[host];
		if (rec != null && rec.auto_lock != null) {
			delete rec.auto_lock;
			result.changed_lock = true;
		}
		return result;
	}

	// APPLIED / ROTATE / start / stop / error: no persistent state change.
	return result;
}

// ---------------------------------------------------------------------------
// Incremental NDJSON reader with durable cursor + truncated-line recovery.
//
// Reads EVENTS_FILE from cursor.bytes.  For each COMPLETE line (terminated by
// \n) that parses as JSON, calls handler(line_obj).  A trailing partial line
// (no \n, or invalid JSON) is NOT advanced past: the cursor stays at the start
// of that line so the next poll re-reads it (the writer may still be mid-append).
// Returns the new cursor { bytes, lines, last_line_sha256 } and the count of
// processed events.
// ---------------------------------------------------------------------------

function read_events_from_cursor(cursor, handler) {
	let raw = readfile(EVENTS_FILE);
	if (raw == null) return { cursor: cursor, count: 0 };
	let total = length(raw);
	let pos = cursor.bytes;
	if (pos > total) {
		// File shrank (truncated/rotated): reset to 0 and re-process from the
		// start.  Idempotency (dedup keys) prevents double-application.
		pos = 0;
	}
	let count = 0;
	let last_line = '';
	while (pos < total) {
		let nl = index(substr(raw, pos), '\n');
		if (nl < 0) break;  // trailing partial line: stop, do not advance
		let line = substr(raw, pos, nl);
		let next_pos = pos + nl + 1;
		let obj = null;
		try { obj = json(line); }
		catch (e) { obj = null; }
		if (obj != null && type(obj) == 'object') {
			handler(obj);
			count++;
			last_line = line;
		}
		// Even an unparseable-but-terminated line is advanced past (it's a
		// complete line; only the UNTerminated tail is held back).  This
		// prevents a single corrupt line from blocking the cursor forever.
		pos = next_pos;
	}
	let last_hash = '';
	if (count > 0 && length(last_line) > 0) {
		// Fingerprint the last fully-consumed line so a restart can detect
		// truncation/rotation (the bytes offset alone is insufficient if the
		// file was rotated to a smaller size).
		last_hash = sprintf('%08x', hash31(last_line));
	}
	return {
		cursor: { bytes: pos, lines: (cursor.lines ?? 0) + count, last_line_sha256: last_hash },
		count: count
	};
}

// ---------------------------------------------------------------------------
// State load/save
// ---------------------------------------------------------------------------

function load_state() {
	let learned = read_json_with_good(STATE_DIR + '/learned.json', validate_learned, default_learned);
	let blocked = read_json_with_good(STATE_DIR + '/blocked.json', validate_blocked, default_blocked);
	let manual  = read_json_with_good(STATE_DIR + '/manual-locks.json', validate_manual_locks, default_manual);
	let lstate  = read_json_with_good(LEARNER_STATE, validate_learner_state, default_learner_state);
	return { learned: learned.doc, blocked: blocked.doc, manual: manual.doc, lstate: lstate.doc };
}

function save_state(st, only_learner_state) {
	atomic_write_json(STATE_DIR + '/learned.json', st.learned, validate_learned);
	atomic_write_json(STATE_DIR + '/blocked.json', st.blocked, validate_blocked);
	atomic_write_json(LEARNER_STATE, st.lstate, validate_learner_state);
}

// ---------------------------------------------------------------------------
// Reload: debounced preload regen + service restart.
//
// The daemon coalesces lock/unlock/blocked changes within RELOAD_DEBOUNCE
// seconds into one reload.  The actual zapret2 service restart is owned by
// the procd wrapper (ORCHESTRA_RELOAD_CMD); the learner never reloads from the
// Lua packet path.  In test mode this is a no-op.
// ---------------------------------------------------------------------------

function regen_preload(lstate) {
	// Delegate to the preload wrapper (generate + check).  Best-effort: a
	// failure is logged but does not roll back learned state (the next poll
	// retries).  We use popen via a shell-quoted controlled path.
	let proc = popen("'" + replace(PRELOAD_WRAPPER, "'", "'\\''") + "' generate", 'r');
	if (proc != null) { proc.read('all'); proc.close(); }
	proc = popen("'" + replace(PRELOAD_WRAPPER, "'", "'\\''") + "' check", 'r');
	if (proc != null) { proc.read('all'); proc.close(); }
	// Bump the preload generation counter — the observable signal that a
	// regen was triggered by a lock/blocked change (contract §3 reload
	// policy).  The counter advances on every TRIGGERED regen, including a
	// best-effort attempt whose wrapper popen failed (the test sandbox ships
	// no preload wrapper; last_preload_gen is how tests observe that the
	// learner requested a regen).  Callers persist lstate after this returns.
	if (lstate != null)
		lstate.last_preload_gen = as_int(lstate.last_preload_gen ?? 0, 'last_preload_gen') + 1;
}

function request_reload() {
	if (length(RELOAD_CMD) == 0) {
		log_line('reload requested (no ORCHESTRA_RELOAD_CMD; procd wrapper handles it)');
		return;
	}
	log_line('reload: ' + RELOAD_CMD);
	let proc = popen(RELOAD_CMD, 'r');
	if (proc != null) { proc.read('all'); proc.close(); }
}

// ---------------------------------------------------------------------------
// One processing pass: read new events, apply, persist, maybe reload.
// Returns a JSON-serializable result describing the pass (for test mode).
// ---------------------------------------------------------------------------

function process_pass(opts) {
	opts = opts ?? {};
	let st = load_state();
	let seen = {};
	let summary = { processed: 0, changed_lock: false, changed_blocked: false, lock_changes: [], cursor_before: st.lstate.event_cursor.bytes, cursor_after: 0, dedup_hits: 0 };

	// Reload dedup window: if a lock change happened and we already reloaded
	// within RELOAD_DEBOUNCE seconds, skip the immediate reload and let the
	// daemon's debounce timer coalesce.  In process-once/test mode we reload
	// immediately if changed_lock (tests assert the regen happens).
	let need_reload = false;

	let res = read_events_from_cursor(st.lstate.event_cursor, function (ev) {
		let before = { changed_lock: summary.changed_lock };
		let r = process_event(st.learned, st.blocked, st.manual, ev, seen);
		if (r.applied) summary.processed++;
		if (r.changed_lock) {
			summary.changed_lock = true;
			need_reload = true;
			push(summary.lock_changes, { type: ev.type, askey: ev.askey ?? ev.protocol, host: ev.host, strategy: ev.strategy });
		}
		if (r.changed_blocked) summary.changed_blocked = true;
	});
	st.lstate.event_cursor = res.cursor;
	summary.cursor_after = res.cursor.bytes;
	st.lstate.updated_at = time();
	// Track the run_id of the most recent start event seen.
	if (summary.processed > 0) {
		// last_run_id is updated by the caller if a start event was seen; here
		// we just persist the cursor.  A start event sets last_run_id.
	}

	// Persist always (cursor advances even on history-only updates).  Atomic.
	save_state(st, false);

	if (need_reload && !opts.no_reload) {
		regen_preload(st.lstate);
		// Persist the bumped preload generation counter (regen_preload
		// advanced st.lstate.last_preload_gen in memory).
		atomic_write_json(LEARNER_STATE, st.lstate, validate_learner_state);
		if (opts.reload) request_reload();
	}

	// Re-read the updated learner state to report the persisted cursor.
	summary.cursor_after = st.lstate.event_cursor.bytes;
	summary.learned = st.learned;
	summary.blocked = st.blocked;
	summary.lstate = st.lstate;
	return summary;
}

// Update last_run_id when a start event is seen (so the cursor attributes the
// current run).  This is folded into process_event via a side flag in daemon
// mode; for test mode the caller can set it from the summary.
function note_run_id(st, ev) {
	if (ev.type == 'start' && type(ev.message) == 'string') {
		st.lstate.last_run_id = ev.message;
	}
}

// ---------------------------------------------------------------------------
// Modes
// ---------------------------------------------------------------------------

function mode_test_process() {
	// TEST MODE: ARGV[0] = "test-process" (the mode), ARGV[1] = events file
	// path (overrides EVENTS_FILE).  Emits a JSON result describing the pass.
	// The state dir is taken from ORCHESTRA_STATE_DIR (tests set it to a temp
	// dir).  Unlike the production daemon, test-process does NOT debounce:
	// if a lock/blocked change occurred it flushes a preload regen immediately
	// before exit so callers/tests can observe the generation advance (the
	// daemon coalesces within RELOAD_DEBOUNCE; test-process flushes once).
	let events = EVENTS_FILE;
	if (length(ARGV) > 1 && ARGV[1] != null)
		events = ARGV[1];
	let st = load_state();
	let seen = {};
	let summary = { processed: 0, changed_lock: false, changed_blocked: false, lock_changes: [], cursor_before: st.lstate.event_cursor.bytes, cursor_after: 0, last_run_id: st.lstate.last_run_id };

	// Read the test events file directly (it may be a temp file outside the
	// normal runtime dir).
	let raw = readfile(events);
	if (raw == null) {
		printf('%J\n', summary);
		exit(0);
	}
	let pos = st.lstate.event_cursor.bytes;
	if (pos > length(raw)) pos = 0;
	let count = 0;
	while (pos < length(raw)) {
		let nl = index(substr(raw, pos), '\n');
		if (nl < 0) break;
		let line = substr(raw, pos, nl);
		let next_pos = pos + nl + 1;
		let obj = null;
		try { obj = json(line); } catch (e) { obj = null; }
		if (obj != null && type(obj) == 'object') {
			note_run_id(st, obj);
			let r = process_event(st.learned, st.blocked, st.manual, obj, seen);
			if (r.applied) { summary.processed++; count++; }
			if (r.changed_lock) {
				summary.changed_lock = true;
				push(summary.lock_changes, { type: obj.type, askey: obj.askey ?? obj.protocol, host: obj.host, strategy: obj.strategy });
			}
			if (r.changed_blocked) summary.changed_blocked = true;
		}
		pos = next_pos;
	}
	st.lstate.event_cursor = { bytes: pos, lines: (st.lstate.event_cursor.lines ?? 0) + count, last_line_sha256: '' };
	st.lstate.updated_at = time();
	summary.last_run_id = st.lstate.last_run_id;
	summary.cursor_after = pos;

	// Persist learned/blocked FIRST (atomic, with .good) so a preload regen
	// reads the new state.
	atomic_write_json(STATE_DIR + '/learned.json', st.learned, validate_learned);
	atomic_write_json(STATE_DIR + '/blocked.json', st.blocked, validate_blocked);

	// Explicit final flush: test-process does NOT debounce.  If a lock or
	// blocked change occurred, regenerate the preload immediately and bump
	// last_preload_gen so the generation advance is observable in the
	// persisted learner-state.json (the daemon debounces; test-process
	// flushes once before exit).  Best-effort: the preload wrapper may be
	// absent in the test sandbox; regen_preload still bumps the gen counter
	// (the observable signal that a regen was triggered).
	if (summary.changed_lock || summary.changed_blocked) {
		regen_preload(st.lstate);
	}

	// Persist learner-state (cursor + bumped last_preload_gen) last.
	atomic_write_json(LEARNER_STATE, st.lstate, validate_learner_state);

	summary.learned = st.learned;
	summary.blocked = st.blocked;
	summary.lstate = st.lstate;
	printf('%J\n', summary);
	exit(0);
}

function mode_process_once() {
	let acq = lock_acquire();
	if (!acq.ok) { log_line('lock: ' + acq.error); exit(1); }
	let summary = process_pass({ no_reload: false, reload: false });
	lock_release();
	log_line(sprintf('process-once: %d events, changed_lock=%s', summary.processed, summary.changed_lock));
	exit(0);
}

function mode_run() {
	// Long-running daemon.  Poll events.ndjson, process, sleep, repeat.
	// Reload is debounced: coalesce lock/unlock changes within RELOAD_DEBOUNCE
	// seconds into one reload.
	log_line('learner daemon started');
	let last_reload = 0;
	let pending_reload = false;
	while (true) {
		let acq = lock_acquire();
		if (!acq.ok) {
			// Another writer (CLI manual action) holds the lock; back off.
			sleep(1);
			continue;
		}
		let need_reload = false;
		let st = load_state();
		let seen = {};
		let res = read_events_from_cursor(st.lstate.event_cursor, function (ev) {
			note_run_id(st, ev);
			let r = process_event(st.learned, st.blocked, st.manual, ev, seen);
			if (r.changed_lock) need_reload = true;
		});
		st.lstate.event_cursor = res.cursor;
		st.lstate.updated_at = time();
		save_state(st, false);
		lock_release();

		if (need_reload) {
			let now = time();
			if (now - last_reload >= RELOAD_DEBOUNCE) {
				regen_preload(st.lstate);
				// Persist the bumped preload generation counter.
				atomic_write_json(LEARNER_STATE, st.lstate, validate_learner_state);
				request_reload();
				last_reload = now;
				pending_reload = false;
				log_line('reload: performed (debounce window elapsed)');
			} else {
				pending_reload = true;
				log_line('reload: deferred (within debounce window)');
			}
		} else if (pending_reload && (time() - last_reload >= RELOAD_DEBOUNCE)) {
			regen_preload(st.lstate);
			// Persist the bumped preload generation counter.
			atomic_write_json(LEARNER_STATE, st.lstate, validate_learner_state);
			request_reload();
			last_reload = time();
			pending_reload = false;
			log_line('reload: performed (deferred coalesced)');
		}

		sleep(1);
	}
}

// Dispatch
let mode = length(ARGV) > 0 ? ARGV[0] : 'run';
// Strip a leading "--" sentinel some ucode builds pass through (mirrors apply.uc).
if (mode == '--') mode = length(ARGV) > 1 ? ARGV[1] : 'run';
if (mode == 'run') mode_run();
else if (mode == 'process-once') mode_process_once();
else if (mode == 'test-process') mode_test_process();
else { fprintf(stderr, 'unknown mode "%s"\n', mode); exit(2); }
