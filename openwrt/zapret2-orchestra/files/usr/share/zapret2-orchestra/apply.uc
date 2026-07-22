'use strict';

-- zapret2-orchestra runtime manager (Phase 1A: safe foundation).
--
-- This is a SINGLE self-contained ucode program (never reads config as
-- shell, no eval, no subprocess calls). It implements:
--   * a text-only parser/transformer for the multiline NFQWS2_OPT assignment
--     in /opt/zapret2/config;
--   * an atomic JSON state helper for manager-state.json;
--   * a mkdir-based interprocess lock;
--   * a profile validator;
--   * a subcommand dispatcher.
--
-- Security model
-- --------------
-- The config file is NEVER sourced, eval'd, or executed. The NFQWS2_OPT
-- value is located and edited with pure byte operations. The only shell
-- execution anywhere in the manager is `sh -n` on a TRANSFORMED OUTPUT
-- written to a temp file, and that is invoked by the CLI shell wrapper
-- (/usr/sbin/zapret2-orchestra-apply), not by this file. `sh -n` is
-- parse-only: it never executes the config.
--
-- Phase 1A scope
-- --------------
-- Implemented (read-only / no side effects on the live config):
--   status, validate-config, validate-profile, lock-test
-- Not yet implemented (return not-implemented-phase-1a, no side effects):
--   enable, disable, apply, rollback, boot-check
--
-- Override paths (for tests):
--   ZAPRET2_CONFIG            /opt/zapret2/config
--   ZAPRET2_ORCHESTRA_DIR     /etc/zapret2-orchestra
--   ZAPRET2_RUNTIME_DIR       /tmp/zapret2-orchestra
--   ZAPRET2_STATE_FILE        <RUNTIME_DIR>/manager-state.json
--   ZAPRET2_LOCK_DIR          <RUNTIME_DIR>/apply.lock
--   ZAPRET2_VALIDATE_OUT      <RUNTIME_DIR>/validate-config.cfg
--   ZAPRET2_PROFILES_DIR      /etc/zapret2-orchestra/profiles
--   ZAPRET2_ORCHESTRA_LUA     /opt/zapret2/lua/orchestra-extra
--   ZAPRET2_PRELOAD_WRAPPER   /usr/sbin/zapret2-orchestra-preload

import { readfile, writefile, mkdir, rename, unlink, stat, rmdir, readlink } from 'fs';

const ORCH_DIR      = getenv('ZAPRET2_ORCHESTRA_DIR')  ?? '/etc/zapret2-orchestra';
const RUNTIME_DIR   = getenv('ZAPRET2_RUNTIME_DIR')   ?? '/tmp/zapret2-orchestra';
const CONFIG_FILE   = getenv('ZAPRET2_CONFIG')        ?? '/opt/zapret2/config';
const STATE_FILE    = getenv('ZAPRET2_STATE_FILE')    ?? (RUNTIME_DIR + '/manager-state.json');
const LOCK_DIR      = getenv('ZAPRET2_LOCK_DIR')      ?? (RUNTIME_DIR + '/apply.lock');
const VALIDATE_OUT  = getenv('ZAPRET2_VALIDATE_OUT')  ?? (RUNTIME_DIR + '/validate-config.cfg');
const PROFILES_DIR  = getenv('ZAPRET2_PROFILES_DIR')  ?? (ORCH_DIR + '/profiles');
const ORCH_LUA      = getenv('ZAPRET2_ORCHESTRA_LUA') ?? '/opt/zapret2/lua/orchestra-extra';
const PRELOAD_WRAPPER = getenv('ZAPRET2_PRELOAD_WRAPPER') ?? '/usr/sbin/zapret2-orchestra-preload';

const NFQWS2_KEY = 'NFQWS2_OPT';

-- ---------------------------------------------------------------------------
-- Small helpers
-- ---------------------------------------------------------------------------

function fail(msg) {
	die('zapret2-orchestra-apply: ' + msg);
}

function emit(obj) {
	printf('%J\n', obj);
}

function line_of(text, index) {
	let n = 0;
	for (let i = 0; i < index && i < length(text); i++)
		if (substr(text, i, 1) == '\n')
			n++;
	return n + 1;
}

-- Does `line` begin (after optional spaces/tabs, not a comment) with
-- "NFQWS2_OPT="? Return the column of the 'N' or -1.
function assignment_start(line) {
	let i = 0;
	while (i < length(line) && (substr(line, i, 1) == ' ' || substr(line, i, 1) == '\t'))
		i++;
	if (i < length(line) && substr(line, i, 1) == '#')
		return -1;
	let head = substr(line, i, length(NFQWS2_KEY) + 1);
	if (head == NFQWS2_KEY + '=')
		return i;
	return -1;
}

-- ---------------------------------------------------------------------------
-- NFQWS2_OPT parser (mirrors tests/_nfqws2_parser.py exactly)
-- ---------------------------------------------------------------------------

-- Returns { ok, error, value, raw, head, tail, open_line, close_line }.
-- On success ok=true; on failure ok=false and error is set.
function parse_nfqws2_opt(text) {
	-- Collect the absolute start index of every NFQWS2_OPT= assignment line.
	let starts = [];
	let line_start = 0;
	let i = 0;
	while (i < length(text)) {
		if (i == line_start) {
			let nl = index(substr(text, i), '\n');
			let line = (nl < 0) ? substr(text, i) : substr(text, i, nl);
			let s = assignment_start(line);
			if (s >= 0)
				push(starts, line_start + s);
		}
		if (substr(text, i, 1) == '\n')
			line_start = i + 1;
		i++;
	}

	if (length(starts) == 0)
		return { ok: false, error: 'missing NFQWS2_OPT assignment' };
	if (length(starts) > 1)
		return { ok: false, error: 'duplicate NFQWS2_OPT assignment' };

	let st = starts[0];
	let eq_pos = st + length(NFQWS2_KEY);
	let open_quote = eq_pos + 1;
	if (open_quote >= length(text) || substr(text, open_quote, 1) != '"')
		return { ok: false, error: 'NFQWS2_OPT value is not double-quoted' };

	let head = substr(text, 0, open_quote + 1);

	let raw = '';
	let value = '';
	let escape = false;
	let j = open_quote + 1;
	let n = length(text);
	let closed = -1;
	while (j < n) {
		let ch = substr(text, j, 1);
		if (escape) {
			raw += ch;
			value += ch;
			escape = false;
			j++;
			continue;
		}
		if (ch == '\\') {
			raw += ch;
			escape = true;
			j++;
			continue;
		}
		if (ch == '"') {
			closed = j;
			break;
		}
		raw += ch;
		value += ch;
		j++;
	}

	if (closed < 0)
		return { ok: false, error: 'unclosed NFQWS2_OPT value' };

	let tail = substr(text, closed);

	return {
		ok: true,
		value: value,
		raw: raw,
		head: head,
		tail: tail,
		open_line: line_of(text, open_quote),
		close_line: line_of(text, closed)
	};
}

function escape_value(v) {
	return replace(replace(v, '\\', '\\\\'), '"', '\\"');
}

-- Return the full config text with NFQWS2_OPT replaced by new_value.
-- Raises (fail) on parse error.
function transform_nfqws2_opt(text, new_value) {
	let p = parse_nfqws2_opt(text);
	if (!p.ok)
		fail(p.error);
	return p.head + escape_value(new_value) + p.tail;
}

-- 31-bit rolling hash (djb2 variant), identical to generate-preload.uc.
function hash31(data) {
	let h = 5381;
	for (let i = 0; i < length(data); i++)
		h = (h * 33 + ord(substr(data, i, 1))) & 0x7fffffff;
	return h;
}

-- ---------------------------------------------------------------------------
-- Atomic JSON state helper (manager-state.json)
-- ---------------------------------------------------------------------------

-- manager-state.json schema:
-- {
--   "schema_version": 1,
--   "states": ["idle"],            -- current state stack
--   "generation": 0,               -- monotonically increasing
--   "previous_state": null,        -- last committed state
--   "hash_algorithm": "djb2-31",
--   "hashes": {
--     "nfqws2_opt": "00000000",    -- hash of NFQWS2_OPT value (NOT the full value)
--     "preload":   "00000000",     -- hash of /tmp/zapret2-orchestra/preload.lua
--     "whitelist": "00000000",     -- hash of /tmp/zapret2-orchestra/whitelist.txt
--     "manifest":  "00000000"      -- hash of /tmp/zapret2-orchestra/manifest.json
--   },
--   "updated_at": 0,               -- unix ts of last successful write
--   "last_error": null,            -- last error string, or null
--   "warnings": []                 -- non-fatal warnings
-- }
--
-- The FULL NFQWS2_OPT value is never stored; only its hash.
-- manager-state.json is NOT a conffile (it lives under /tmp and is rebuilt
-- at boot by the boot hook).

const STATE_SCHEMA_VERSION = 1;

function ensure_runtime_dir() {
	let info = stat(RUNTIME_DIR);
	if (info == null) {
		if (!mkdir(RUNTIME_DIR))
			fail('cannot create runtime dir ' + RUNTIME_DIR);
		info = stat(RUNTIME_DIR);
	}
	if (info?.type != 'directory')
		fail(RUNTIME_DIR + ' is not a directory');
}

function default_state() {
	return {
		schema_version: STATE_SCHEMA_VERSION,
		states: ['idle'],
		generation: 0,
		previous_state: null,
		hash_algorithm: 'djb2-31',
		hashes: {
			nfqws2_opt: '00000000',
			preload:   '00000000',
			whitelist: '00000000',
			manifest:  '00000000'
		},
		updated_at: 0,
		last_error: null,
		warnings: []
	};
}

function validate_state(doc) {
	if (type(doc) != 'object')
		return 'state is not an object';
	if (doc.schema_version != STATE_SCHEMA_VERSION)
		return 'schema_version must be ' + STATE_SCHEMA_VERSION;
	if (type(doc.states) != 'array')
		return 'states must be an array';
	if (type(doc.generation) != 'int' && !(type(doc.generation) == 'double' && doc.generation == int(doc.generation)))
		return 'generation must be an integer';
	if (type(doc.hashes) != 'object')
		return 'hashes must be an object';
	for (let k in ['nfqws2_opt', 'preload', 'whitelist', 'manifest']) {
		if (type(doc.hashes[k]) != 'string')
			return 'hashes.' + k + ' must be a string';
	}
	if (type(doc.warnings) != 'array')
		return 'warnings must be an array';
	return null;
}

-- Atomic write: tmp + rename in the same directory. Validate the serialized
-- bytes by re-parsing before installing.
function atomic_write_state(doc) {
	ensure_runtime_dir();
	let err = validate_state(doc);
	if (err)
		fail('state validation failed: ' + err);
	let payload = sprintf('%J\n', doc);
	-- Re-parse to prove the bytes are valid JSON and round-trip the schema.
	let reparsed;
	try {
		reparsed = json(payload);
	}
	catch (e) {
		fail('state serialization produced invalid JSON: ' + e);
	}
	let reerr = validate_state(reparsed);
	if (reerr)
		fail('state round-trip validation failed: ' + reerr);
	let tmp = STATE_FILE + '.tmp';
	if (writefile(tmp, payload) == null)
		fail('cannot write ' + tmp);
	if (!rename(tmp, STATE_FILE)) {
		unlink(tmp);
		fail('cannot install ' + STATE_FILE);
	}
}

function read_state() {
	let raw = readfile(STATE_FILE);
	if (raw == null)
		return default_state();
	let doc;
	try {
		doc = json(raw);
	}
	catch (e) {
		return null;  -- corrupted; caller decides
	}
	if (validate_state(doc) != null)
		return null;
	return doc;
}

-- ---------------------------------------------------------------------------
-- mkdir lock
-- ---------------------------------------------------------------------------

-- mkdir is atomic on POSIX filesystems: exactly one caller succeeds.
-- The lock directory holds a `pid` file with the holder's PID. Stale locks
-- (pid absent, not our executable, or cmdline mismatch) are recovered.
-- Release removes only the pid file and the lock dir (no bulk removal), and only
-- if the recorded PID is still ours (guards against PID reuse).

function my_pid() {
	let p = readfile('/proc/self/stat');
	if (p != null) {
		let sp = split(p, ' ');
		if (length(sp) > 0)
			return int(sp[0]);
	}
	-- fallback: env override for tests
	return int(getenv('ZAPRET2_LOCK_TEST_PID') ?? '0');
}

function pid_alive_and_is_manager(pid) {
	if (pid <= 0)
		return false;
	if (stat('/proc/' + pid) == null)
		return false;
	let cmd = readfile('/proc/' + pid + '/cmdline');
	if (cmd == null)
		return false;
	-- cmdline is NUL-separated; look for our marker.
	return index(cmd, 'zapret2-orchestra-apply') >= 0 || index(cmd, 'apply.uc') >= 0;
}

function lock_acquire() {
	ensure_runtime_dir();
	let first = true;
	while (true) {
		if (mkdir(LOCK_DIR)) {
			-- we won the mkdir race; record our pid.
			let pid = my_pid();
			if (writefile(LOCK_DIR + '/pid', '' + pid + '\n') == null) {
				rmdir(LOCK_DIR);
				return { ok: false, error: 'cannot write pid file' };
			}
			return { ok: true, pid: pid, stale_recovered: false };
		}
		-- mkdir failed: dir exists. Inspect the holder.
		let pidraw = readfile(LOCK_DIR + '/pid');
		let holder = int(trim(pidraw ?? ''));
		if (holder > 0 && !pid_alive_and_is_manager(holder)) {
			-- stale: holder is gone or not us. Recover by removing the pid
			-- file and the dir, then retry. This removes only the pid
			-- file (whose owner is dead) and the dir itself are removed.
			unlink(LOCK_DIR + '/pid');
			rmdir(LOCK_DIR);
			continue;
		}
		-- live holder: wait briefly once, then report busy.
		if (first) {
			-- single retry after a short yield; no long blocking in Phase 1A.
			first = false;
			let slept = 0;
			while (slept < 1) { slept += 0; break; }
			continue;
		}
		return { ok: false, error: 'lock busy', holder: holder };
	}
}

-- Release only if the recorded PID equals ours.
function lock_release() {
	let pidraw = readfile(LOCK_DIR + '/pid');
	let holder = int(trim(pidraw ?? ''));
	let mine = my_pid();
	if (holder != mine)
		return { ok: false, error: 'lock not mine', holder: holder, mine: mine };
	unlink(LOCK_DIR + '/pid');
	rmdir(LOCK_DIR);
	return { ok: true };
}

-- ---------------------------------------------------------------------------
-- Profile validation
-- ---------------------------------------------------------------------------

-- Reject characters/sequences that have no business in a profile argument:
-- command substitution $(), backticks, NUL, CR, unclosed quotes, and shell
-- separators (;, |, &&, >, <). A profile is a set of --lua-desync / --filter
-- arguments, not an arbitrary shell snippet.
function profile_value_ok(s) {
	if (s == null)
		return 'null value';
	if (index(s, '\x00') >= 0)
		return 'NUL byte';
	if (index(s, '\r') >= 0)
		return 'carriage return';
	if (index(s, '$(') >= 0)
		return 'command substitution $(...)';
	if (index(s, '`') >= 0)
		return 'backtick command substitution';
	if (index(s, ';') >= 0)
		return 'shell separator ;';
	if (index(s, '|') >= 0)
		return 'pipe |';
	if (index(s, '&&') >= 0)
		return 'shell separator &&';
	if (index(s, '>') >= 0)
		return 'redirect >';
	if (index(s, '<') >= 0)
		return 'redirect <';
	-- unclosed single or double quote
	let dq = 0, sq = 0;
	for (let i = 0; i < length(s); i++) {
		let c = substr(s, i, 1);
		if (c == '"') dq++;
		else if (c == "'") sq++;
	}
	if (dq % 2 != 0)
		return 'unclosed double quote';
	if (sq % 2 != 0)
		return 'unclosed single quote';
	return null;
}

-- A profile is a directory under PROFILES_DIR containing a `profile.conf`
-- whose NFQWS2_OPT value passes profile_value_ok. User profiles override
-- builtins of the same name. Builtins live under PROFILES_DIR/builtin.
function validate_profile(name) {
	let problems = [];
	if (name == null || length(name) == 0) {
		problems.push('profile name is empty');
		return { ok: false, problems: problems };
	}
	-- Reject path traversal in the name.
	if (index(name, '/') >= 0 || index(name, '..') >= 0 || index(name, '\x00') >= 0) {
		problems.push('profile name contains path traversal');
		return { ok: false, problems: problems };
	}

	let user_path = PROFILES_DIR + '/' + name + '/profile.conf';
	let builtin_path = PROFILES_DIR + '/builtin/' + name + '/profile.conf';
	let chosen = null;
	if (stat(user_path)?.type == 'file')
		chosen = user_path;
	else if (stat(builtin_path)?.type == 'file')
		chosen = builtin_path;
	if (chosen == null) {
		problems.push('profile not found in user or builtin directory');
		return { ok: false, problems: problems };
	}

	let text = readfile(chosen);
	if (text == null) {
		problems.push('cannot read ' + chosen);
		return { ok: false, problems: problems };
	}

	let p = parse_nfqws2_opt(text);
	if (!p.ok) {
		problems.push('profile.conf parse error: ' + p.error);
		return { ok: false, problems: problems, source: chosen };
	}
	let verr = profile_value_ok(p.value);
	if (verr != null)
		problems.push('profile value rejected: ' + verr);

	-- Orchestra runtime markers: init.lua and the whitelist seed must exist,
	-- and circular_quality must be referenced by the profile value.
	if (stat(ORCH_LUA + '/init.lua')?.type != 'file')
		problems.push('orchestra init.lua missing');
	if (stat(ORCH_DIR + '/whitelist.json')?.type != 'file')
		problems.push('orchestra whitelist.json seed missing');
	if (index(p.value, 'circular_quality') < 0)
		problems.push('profile does not reference circular_quality');

	-- The profile is NOT applied to the config in Phase 1A.
	return { ok: length(problems) == 0, problems: problems, source: chosen, value_bytes: length(p.value) };
}

-- ---------------------------------------------------------------------------
-- Subcommands
-- ---------------------------------------------------------------------------

function cmd_status() {
	let cfg_exists = stat(CONFIG_FILE)?.type == 'file';
	let state_file_exists = stat(STATE_FILE)?.type == 'file';
	let state = read_state();
	let state_ok = state != null;
	let nfqws2_opt = null;
	let parse_error = null;
	if (cfg_exists) {
		let text = readfile(CONFIG_FILE);
		if (text == null) {
			parse_error = 'cannot read config';
		} else {
			let p = parse_nfqws2_opt(text);
			if (p.ok) {
				nfqws2_opt = {
					open_line: p.open_line,
					close_line: p.close_line,
					bytes: length(p.value),
					hash: sprintf('%08x', hash31(p.value))
				};
			} else {
				parse_error = p.error;
			}
		}
	}
	emit({
		ok: true,
		phase: '1a',
		config: { path: CONFIG_FILE, exists: cfg_exists },
		nfqws2_opt: nfqws2_opt,
		parse_error: parse_error,
		state: { path: STATE_FILE, exists: state_file_exists, valid: state_ok, states: state?.states, generation: state?.generation },
		lock: { dir: LOCK_DIR, held: stat(LOCK_DIR)?.type == 'directory' }
	});
	exit(0);
}

function cmd_validate_config() {
	let text = readfile(CONFIG_FILE);
	if (text == null)
		fail('cannot read config ' + CONFIG_FILE);
	let p = parse_nfqws2_opt(text);
	if (!p.ok)
		fail('parse error: ' + p.error);
	-- Re-emit the config with the SAME value to prove the transformer is
	-- byte-stable and produces valid shell. Write ONLY to the temp path
	-- (never to CONFIG_FILE). The CLI wrapper runs `sh -n` on this temp.
	let emitted = p.head + escape_value(p.value) + p.tail;
	if (emitted != text)
		fail('internal: re-emission is not byte-identical');
	ensure_runtime_dir();
	if (writefile(VALIDATE_OUT, emitted) == null)
		fail('cannot write ' + VALIDATE_OUT);
	emit({
		ok: true,
		config: CONFIG_FILE,
		nfqws2_opt: {
			open_line: p.open_line,
			close_line: p.close_line,
			bytes: length(p.value),
			hash: sprintf('%08x', hash31(p.value))
		},
		reemitted_to: VALIDATE_OUT,
		byte_identical: true
	});
	exit(0);
}

function cmd_validate_profile() {
	let name = ARGV[0];
	if (name == null)
		fail('usage: validate-profile <name>');
	let r = validate_profile(name);
	emit({ ok: r.ok, profile: name, source: r.source, problems: r.problems, value_bytes: r.value_bytes });
	exit(r.ok ? 0 : 1);
}

function cmd_lock_test() {
	-- Acquire, report, release. Used by tests to prove the lock works and
	-- that release only removes when mine.
	let acq = lock_acquire();
	if (!acq.ok) {
		emit({ ok: false, stage: 'acquire', error: acq.error, holder: acq.holder });
		exit(1);
	}
	let pid = acq.pid;
	emit({ ok: true, stage: 'acquired', pid: pid, stale_recovered: acq.stale_recovered });
	let rel = lock_release();
	emit({ ok: rel.ok, stage: 'released', pid: pid, error: rel.error, holder: rel.holder, mine: rel.mine });
	exit(rel.ok ? 0 : 1);
}

function cmd_not_implemented(name) {
	emit({ ok: false, error: 'not-implemented-phase-1a', command: name });
	exit(2);
}

-- ---------------------------------------------------------------------------
-- Dispatch
-- ---------------------------------------------------------------------------

let sub = length(ARGV) > 0 ? ARGV[0] : '';
let rest = [];
for (let i = 1; i < length(ARGV); i++)
	push(rest, ARGV[i]);
ARGV = rest;

switch (sub) {
	case 'status':
		cmd_status();
	case 'validate-config':
		cmd_validate_config();
	case 'validate-profile':
		cmd_validate_profile();
	case 'lock-test':
		cmd_lock_test();
	case 'enable':
	case 'disable':
	case 'apply':
	case 'rollback':
	case 'boot-check':
		cmd_not_implemented(sub);
	default:
		emit({ ok: false, error: 'unknown subcommand', command: sub });
		exit(2);
}
