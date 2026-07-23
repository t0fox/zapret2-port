'use strict';

// zapret2-orchestra preload generator and manifest checker.
//
// Modes (selected by ARGV[1]):
//   generate  (default)  read the persistent JSON seeds under STATE_DIR, render
//                        preload.lua + whitelist.txt, and write a manifest.json
//                        that records the byte length and a 31-bit rolling
//                        hash of each generated file. The manifest is written
//                        LAST and atomically, so its presence with matching
//                        hashes is proof of a complete generation.
//   check                read manifest.json and verify that preload.lua and
//                        whitelist.txt exist and match the recorded length and
//                        hash. Exit 0 if consistent, non-zero otherwise.
//
// The generated preload contains only data calls supplied by the Orchestra
// extension (slm_preload_history, slm_preload_locked, slm_preload_blocked) and
// an assignment to ORCHESTRA_WHITELIST, as specified in
// docs/orchestra-state-schema.md. The generator never writes under /etc and
// never invokes shell commands; it uses only ucode-mod-fs and the built-in
// json() function.
//
// Override paths with ORCHESTRA_STATE_DIR / ORCHESTRA_RUNTIME_DIR /
// ORCHESTRA_PRELOAD_FILE / ORCHESTRA_WHITELIST_FILE / ORCHESTRA_MANIFEST_FILE
// (used by tests).
//
// Exit status: 0 on success, non-zero (with a diagnostic on stderr) on any
// read, schema, or write error.

import { readfile, writefile, mkdir, rename, unlink, stat } from 'fs';

const STATE_DIR      = getenv('ORCHESTRA_STATE_DIR')        ?? '/etc/zapret2-orchestra';
const RUNTIME_DIR    = getenv('ORCHESTRA_RUNTIME_DIR')      ?? '/tmp/zapret2-orchestra';
const PRELOAD_FILE   = getenv('ORCHESTRA_PRELOAD_FILE')     ?? (RUNTIME_DIR + '/preload.lua');
const WHITELIST_FILE = getenv('ORCHESTRA_WHITELIST_FILE')   ?? (RUNTIME_DIR + '/whitelist.txt');
const MANIFEST_FILE  = getenv('ORCHESTRA_MANIFEST_FILE')    ?? (RUNTIME_DIR + '/manifest.json');
// manager-state.json lives under STATE_DIR; the profile sidecar
// profiles/<profile>.json (contract §4 chain_id_for_strategy map) lives under
// STATE_DIR/profiles (user override) or SHARE_DIR/profiles (builtin). Tests
// override SHARE_DIR / the sidecar search root via ORCHESTRA_SHARE_DIR /
// ORCHESTRA_PROFILES_DIR.
const MANAGER_STATE  = getenv('ORCHESTRA_MANAGER_STATE_FILE') ?? (STATE_DIR + '/manager-state.json');
const SHARE_DIR      = getenv('ORCHESTRA_SHARE_DIR')          ?? '/usr/share/zapret2-orchestra';
const PROFILES_DIR   = getenv('ORCHESTRA_PROFILES_DIR')       ?? (STATE_DIR + '/profiles');
const BUILTIN_PROFILES_DIR = getenv('ORCHESTRA_BUILTIN_PROFILES_DIR') ?? (SHARE_DIR + '/profiles');

function fail(msg) {
	die('zapret2-orchestra preload: ' + msg);
}

function is_int(v) {
	return type(v) == 'int' || (type(v) == 'double' && v == int(v));
}

function as_int(v, ctx) {
	// JSON object keys are always strings, but strategy keys and similar
	// fields are conceptually integers (e.g. {"10": {"successes": 1}}).
	// Coerce string representations of integers before the type check so
	// that parsed JSON seeds work correctly. int("abc") returns NaN which
	// is_int() rejects, so non-numeric strings still fail.
	if (type(v) == 'string')
		v = int(v);
	if (!is_int(v))
		fail(ctx + ': expected integer');
	return int(v);
}

function read_seed(name) {
	let path = STATE_DIR + '/' + name;
	let raw = readfile(path);
	if (raw == null)
		fail('cannot read ' + path);
	let doc;
	try {
		doc = json(raw);
	}
	catch (e) {
		fail('invalid JSON in ' + path + ': ' + e);
	}
	if (type(doc) != 'object')
		fail(path + ' is not a JSON object');
	if (doc.schema_version != 1)
		fail(path + ': schema_version must be 1');
	return doc;
}

function sorted_unique_strategies(values, ctx) {
	let seen = {}, out = [];
	for (let i = 0; i < length(values); i++) {
		let s = as_int(values[i], ctx);
		if (s < 1)
			fail(ctx + ': strategy must be a positive integer');
		if (!seen[s]) {
			seen[s] = true;
			push(out, s);
		}
	}
	return sort(out);
}

function sorted_unique_hosts(values) {
	let deduped = uniq(values ?? []);
	let out = [];
	for (let i = 0; i < length(deduped); i++) {
		let h = deduped[i];
		if (type(h) != 'string' || length(h) == 0)
			fail('whitelist: host must be a non-empty string');
		push(out, h);
	}
	return sort(out);
}

function lua_quote(s) {
	let esc = replace(replace(s, '\\', '\\\\'), '"', '\\"');
	return '"' + esc + '"';
}

function lua_int_list(values) {
	return '{' + join(', ', values) + '}';
}

function sorted_keys(obj) {
	return sort(keys(obj ?? {}));
}

function ensure_dir(path) {
	let info = stat(path);
	if (info == null) {
		if (!mkdir(path))
			fail('cannot create directory ' + path);
		info = stat(path);
	}
	if (info?.type != 'directory')
		fail(path + ' is not a directory');
}

// Atomic write: write a sibling temp file in the same directory (therefore on
// the same filesystem) and rename it over the target. rename is atomic on the
// same filesystem, so a reader never observes a partially written file.
function atomic_write(path, data) {
	let tmp = path + '.tmp';
	if (writefile(tmp, data) == null)
		fail('cannot write ' + tmp);
	if (!rename(tmp, path)) {
		unlink(tmp);
		fail('cannot install ' + path);
	}
}

// 31-bit rolling hash (djb2 variant). Kept under 2^31 so that the intermediate
// product (hash * 33 + byte) stays below 2^36, well within the exact integer
// range of double precision. Used only to detect mismatched/truncated
// generated files, not for any security purpose.
function hash31(data) {
	let h = 5381;
	for (let i = 0; i < length(data); i++) {
		let b = ord(substr(data, i, 1));
		h = (h * 33 + b) & 0x7fffffff;
	}
	return h;
}

function file_entry(data) {
	return { bytes: length(data), hash: sprintf('%08x', hash31(data)) };
}

function render_whitelist_table(seeds) {
	let hosts = sorted_unique_hosts(seeds.whitelist.hosts ?? []);
	if (length(hosts) == 0)
		return 'ORCHESTRA_WHITELIST = {}';
	let entries = [];
	for (let i = 0; i < length(hosts); i++)
		push(entries, '[' + lua_quote(hosts[i]) + ']=true');
	return 'ORCHESTRA_WHITELIST = { ' + join(', ', entries) + ' }';
}

function render_blocked(lines, seeds) {
	let protocols = sorted_keys(seeds.blocked.protocols);
	for (let pi = 0; pi < length(protocols); pi++) {
		let askey = protocols[pi];
		let bp = seeds.blocked.protocols[askey] ?? {};
		let global = sorted_unique_strategies(bp.global ?? [], 'blocked.' + askey + '.global');
		if (length(global) > 0)
			push(lines, 'slm_preload_blocked(' + lua_quote(askey) + ', "*", ' + lua_int_list(global) + ')');
		let bhosts = sorted_keys(bp.hosts);
		for (let hi = 0; hi < length(bhosts); hi++) {
			let host = bhosts[hi];
			let vals = sorted_unique_strategies(bp.hosts[host], 'blocked.' + askey + '.' + host);
			if (length(vals) > 0)
				push(lines, 'slm_preload_blocked(' + lua_quote(askey) + ', ' + lua_quote(host) + ', ' + lua_int_list(vals) + ')');
		}
	}
}

function render_learned(lines, seeds) {
	let protocols = sorted_keys(seeds.learned.protocols);
	for (let pi = 0; pi < length(protocols); pi++) {
		let askey = protocols[pi];
		let lhosts_map = seeds.learned.protocols[askey] ?? {};
		let lhosts = sorted_keys(lhosts_map);
		for (let hi = 0; hi < length(lhosts); hi++) {
			let host = lhosts[hi];
			let rec = lhosts_map[host] ?? {};
			let strats = sorted_keys(rec.strategies ?? {});
			for (let si = 0; si < length(strats); si++) {
				let skey = strats[si];
				let s = as_int(skey, 'learned.' + askey + '.' + host + ' strategy key');
				if (s < 1)
					fail('learned.' + askey + '.' + host + ': strategy must be a positive integer');
				let cnt = rec.strategies[skey] ?? {};
				let succ = as_int(cnt.successes ?? 0, 'learned.' + askey + '.' + host + '.' + skey + '.successes');
				let fcount = as_int(cnt.failures ?? 0, 'learned.' + askey + '.' + host + '.' + skey + '.failures');
				push(lines, 'slm_preload_history(' + lua_quote(askey) + ', ' + lua_quote(host) + ', ' + s + ', ' + succ + ', ' + fcount + ')');
			}
			if (rec.auto_lock != null) {
				let al = as_int(rec.auto_lock, 'learned.' + askey + '.' + host + '.auto_lock');
				if (al >= 1)
					push(lines, 'slm_preload_locked(' + lua_quote(askey) + ', ' + lua_quote(host) + ', ' + al + ', false)');
			}
		}
	}
}

function render_manual_locks(lines, seeds) {
	let protocols = sorted_keys(seeds.manual_locks.protocols);
	for (let pi = 0; pi < length(protocols); pi++) {
		let askey = protocols[pi];
		let mhosts_map = seeds.manual_locks.protocols[askey] ?? {};
		let mhosts = sorted_keys(mhosts_map);
		for (let hi = 0; hi < length(mhosts); hi++) {
			let host = mhosts[hi];
			let strat = as_int(mhosts_map[host], 'manual-locks.' + askey + '.' + host);
			if (strat < 1)
				fail('manual-locks.' + askey + '.' + host + ': strategy must be a positive integer');
			push(lines, 'slm_preload_locked(' + lua_quote(askey) + ', ' + lua_quote(host) + ', ' + strat + ', true)');
		}
	}
}

// ---------------------------------------------------------------------------
// Adaptive profile sidecar: chain_id_for_strategy (contract §4).
//
// The selected adaptive profile is read from manager-state.json (`profile`).
// Its sidecar profiles/<profile>.json carries the strategy<->chain map the
// importer (Subagent A) generated.  We bake it into preload.lua as
// ORCHESTRA_CHAIN_ID_FOR_STRATEGY so the runtime can:
//   (a) emit chain_id on every state-transition event, and
//   (b) resolve a persisted locked strategy NUMBER to a stable chain id on
//       restart (so a renumber would never silently misapply a learned lock).
// The sidecar is OPTIONAL: a native (non-circular) profile has no sidecar and
// no chain map, in which case we emit an empty table and the runtime omits
// chain_id from events.  manager-state.json is also optional (absent on a
// fresh install before any enable); we default generation to 0 and profile to
// null.
// ---------------------------------------------------------------------------

// Read manager-state.json; return { profile, generation } or defaults.  Never
// fails the generation if manager-state is absent/corrupt — the preload must
// still be producible from the four seeds alone (the preload is the PRIMARY
// nfqws2 init artifact, and manager-state is rebuilt by apply.uc).
function read_manager_state() {
	let raw = readfile(MANAGER_STATE);
	if (raw == null)
		return { profile: null, generation: 0 };
	let doc;
	try { doc = json(raw); }
	catch (e) { return { profile: null, generation: 0 }; }
	let gen = 0;
	if (type(doc.generation) == 'int' || (type(doc.generation) == 'double' && doc.generation == int(doc.generation)))
		gen = int(doc.generation);
	let prof = (type(doc.profile) == 'string') ? doc.profile : null;
	return { profile: prof, generation: gen };
}

// Resolve the sidecar JSON path for a profile name.  User override under
// STATE_DIR/profiles takes priority over the builtin under SHARE_DIR/profiles,
// mirroring apply.uc's validate_profile priority.  Returns null if no sidecar
// exists (native profile, or adaptive profile without a sidecar yet).
function sidecar_path(profile) {
	if (profile == null || length(profile) == 0)
		return null;
	if (index(profile, '/') >= 0 || index(profile, '..') >= 0 || index(profile, '\x00') >= 0)
		return null;
	let user = PROFILES_DIR + '/' + profile + '.json';
	if (stat(user)?.type == 'file')
		return user;
	let builtin = BUILTIN_PROFILES_DIR + '/' + profile + '.json';
	if (stat(builtin)?.type == 'file')
		return builtin;
	return null;
}

// Read and validate the chain_id_for_strategy map from the sidecar.  Returns
// { askey, chain_id_for_strategy } or null if no sidecar / no map.  The map is
// keyed by strategy NUMBER (string in JSON) -> chain_id (string).  We group by
// askey so the runtime lookup is ORCHESTRA_CHAIN_ID_FOR_STRATEGY[askey][n].
function read_chain_map(profile) {
	let path = sidecar_path(profile);
	if (path == null)
		return null;
	let raw = readfile(path);
	if (raw == null)
		return null;
	let doc;
	try { doc = json(raw); }
	catch (e) { fail('invalid JSON in sidecar ' + path + ': ' + e); }
	if (type(doc) != 'object')
		fail(path + ' is not a JSON object');
	if (doc.schema_version != 1)
		fail(path + ': schema_version must be 1');
	let askey = (type(doc.askey) == 'string') ? doc.askey : null;
	let map = doc.chain_id_for_strategy;
	if (type(map) != 'object')
		return null;
	// Validate: keys are strategy numbers (as strings in JSON), values are
	// non-empty strings.  Return a normalized { askey, by_strategy } where
	// by_strategy is { "<n>": "<chain_id>" }.
	let by_strategy = {};
	for (let k, v in map) {
		let n = int(k);
		if (n < 1 || n != int(n))
			fail(path + ': chain_id_for_strategy key ' + k + ' must be a positive integer');
		if (type(v) != 'string' || length(v) == 0)
			fail(path + ': chain_id_for_strategy[' + k + '] must be a non-empty string');
		by_strategy[n] = v;
	}
	return { askey: askey, by_strategy: by_strategy };
}

// Render the ORCHESTRA_CHAIN_ID_FOR_STRATEGY Lua table and the
// ORCHESTRA_PRELOAD_GENERATION assignment.  The chain map is grouped by askey;
// when the sidecar carries an askey we key under it, otherwise under the
// profile's implicit askey (default "tls").  An empty map is emitted as {} so
// the runtime always sees a table (events.lua checks type == 'table').
function render_chain_map_and_generation(seeds, mgr) {
	let lines = [];
	let cm = read_chain_map(mgr.profile);
	let askey = cm?.askey ?? 'tls';
	let by_strategy = cm?.by_strategy ?? {};
	// Build ORCHESTRA_CHAIN_ID_FOR_STRATEGY[askey] = { [n] = "chain_id", ... }
	// with numeric keys sorted ascending for deterministic output.
	let nums = sort(keys(by_strategy));
	if (length(nums) == 0) {
		push(lines, 'ORCHESTRA_CHAIN_ID_FOR_STRATEGY = {}');
	} else {
		let entries = [];
		for (let i = 0; i < length(nums); i++) {
			let n = nums[i];
			push(entries, '[' + n + ']=' + lua_quote(by_strategy[n]));
		}
		push(lines, 'ORCHESTRA_CHAIN_ID_FOR_STRATEGY = { [' + lua_quote(askey) + '] = { ' + join(', ', entries) + ' } }');
	}
	// ORCHESTRA_PRELOAD_GENERATION lets the runtime stamp every event with the
	// generation active when it was emitted (contract §2 `generation`).
	push(lines, 'ORCHESTRA_PRELOAD_GENERATION = ' + mgr.generation);
	return join('\n', lines);
}

function render_preload(seeds, mgr) {
	let lines = [
		'-- Auto-generated by zapret2-orchestra preload generator. Do not edit.',
		'-- Source: ' + STATE_DIR + '/*.json',
		render_whitelist_table(seeds)
	];
	render_blocked(lines, seeds);
	render_learned(lines, seeds);
	render_manual_locks(lines, seeds);
	// chain_id_for_strategy map + preload generation (contract §4 / §2).
	push(lines, render_chain_map_and_generation(seeds, mgr));
	return join('\n', lines) + '\n';
}

function render_whitelist_txt(seeds) {
	let hosts = sorted_unique_hosts(seeds.whitelist.hosts ?? []);
	if (length(hosts) == 0)
		return '';
	return join('\n', hosts) + '\n';
}

function load_seeds() {
	let seeds = {
		blocked:      read_seed('blocked.json'),
		learned:      read_seed('learned.json'),
		manual_locks: read_seed('manual-locks.json'),
		whitelist:    read_seed('whitelist.json')
	};
	if (type(seeds.whitelist.hosts) != 'array')
		fail('whitelist.json: hosts must be an array');
	if (type(seeds.blocked.protocols) != 'object')
		fail('blocked.json: protocols must be an object');
	if (type(seeds.learned.protocols) != 'object')
		fail('learned.json: protocols must be an object');
	if (type(seeds.manual_locks.protocols) != 'object')
		fail('manual-locks.json: protocols must be an object');
	return seeds;
}

function write_manifest(preload, whitelist) {
	let manifest = {
		schema_version: 1,
		generated_at: time(),
		state_dir: STATE_DIR,
		preload:   file_entry(preload),
		whitelist: file_entry(whitelist)
	};
	atomic_write(MANIFEST_FILE, sprintf('%J\n', manifest));
}

function generate() {
	let seeds = load_seeds();
	let mgr = read_manager_state();
	let preload = render_preload(seeds, mgr);
	let whitelist = render_whitelist_txt(seeds);

	ensure_dir(RUNTIME_DIR);
	atomic_write(PRELOAD_FILE, preload);
	atomic_write(WHITELIST_FILE, whitelist);
	// The manifest is written LAST and atomically. A reader that observes a
	// manifest whose recorded lengths/hashes match the files has proof that
	// the whole generation completed.
	write_manifest(preload, whitelist);

	printf('orchestra preload generated: %s\n', PRELOAD_FILE);
	exit(0);
}

function check() {
	let raw = readfile(MANIFEST_FILE);
	if (raw == null)
		fail('manifest missing: ' + MANIFEST_FILE);
	let m;
	try {
		m = json(raw);
	}
	catch (e) {
		fail('invalid manifest JSON: ' + e);
	}
	if (m.schema_version != 1)
		fail('manifest: schema_version must be 1');

	let problems = [];
	function verify(name, path, entry) {
		let data = readfile(path);
		if (data == null) {
			push(problems, name + ' missing: ' + path);
			return;
		}
		if (length(data) != entry.bytes)
			push(problems, name + ' size mismatch: expected ' + entry.bytes + ' got ' + length(data));
		let h = sprintf('%08x', hash31(data));
		if (h != entry.hash)
			push(problems, name + ' hash mismatch: expected ' + entry.hash + ' got ' + h);
	}
	verify('preload', PRELOAD_FILE, m.preload);
	verify('whitelist', WHITELIST_FILE, m.whitelist);

	if (length(problems) > 0) {
		for (let i = 0; i < length(problems); i++)
			warn('zapret2-orchestra preload: ' + problems[i]);
		exit(1);
	}
	exit(0);
}

let mode = length(ARGV) > 0 ? ARGV[0] : 'generate';
if (mode == 'generate')
	generate();
else if (mode == 'check')
	check();
else
	fail('unknown mode "' + mode + '"; expected "generate" or "check"');
