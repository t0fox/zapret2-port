'use strict';

// zapret2-orchestra runtime manager (Phase 1B: transactional commands).
//
// This is a SINGLE self-contained ucode program (never reads config as
// shell, no eval, no string-form popen). It implements:--   * a text-only parser/transformer for the multiline NFQWS2_OPT assignment
//     in /opt/zapret2/config;
//   * an atomic JSON state helper for manager-state.json;
//   * a mkdir-based interprocess lock;
//   * a profile validator;
//   * transactional commands: apply, enable, disable, rollback, boot-check;
//   * a subcommand dispatcher.
//
// Security model
// --------------
// The config file is NEVER sourced, eval'd, or executed. The NFQWS2_OPT
// value is located and edited with pure byte operations, and the user value
// is never placed on a command line — it lives only inside the candidate
// FILE, which `sh -n` validates as file content (parse-only, no execution).
//
// ucode has no array-form popen: fs.popen() always invokes /bin/sh -c (see
// lib/fs.c — it calls popen(ucv_string_get(comm), ..)). So the two subprocess
// calls use the mechanism that fits each case:
//   sh -n <candidate>          via system([..]) — execvp directly, NO shell.
//   zapret2-orchestra-preload  via popen(.., 'r') — shell, but the command
//                              string is only shell-quoted controlled paths
//                              and a literal mode; no user input is on the
//                              command line, so there is no injection vector.
//                              popen('r') is used here (not system()) so the
//                              wrapper's stdout is captured rather than
//                              polluting this program's JSON emit() output.
//
// Phase 1B scope
// --------------
// Implemented:
//   status, validate-config, validate-profile, lock-test (read-only)
//   apply, enable, disable, rollback, boot-check (transactional)
//
// Override paths (for tests):
//   ZAPRET2_CONFIG              /opt/zapret2/config
//   ZAPRET2_ORCHESTRA_DIR       /etc/zapret2-orchestra
//   ZAPRET2_RUNTIME_DIR         /tmp/zapret2-orchestra
//   ZAPRET2_STATE_FILE          <ORCH_DIR>/manager-state.json
//   ZAPRET2_LOCK_DIR            /var/lock/zapret2-orchestra-apply.lock
//   ZAPRET2_CANDIDATE_FILE      /opt/zapret2/.config.orchestra.tmp
//   ZAPRET2_BACKUP_DIR          <ORCH_DIR>/backup
//   ZAPRET2_USER_PROFILES_DIR   /etc/zapret2-orchestra/profiles
//   ZAPRET2_BUILTIN_PROFILES_DIR /usr/share/zapret2-orchestra/profiles
//   ZAPRET2_ORCHESTRA_LUA       /opt/zapret2/lua/orchestra-extra
//   ZAPRET2_PRELOAD_WRAPPER     /usr/sbin/zapret2-orchestra-preload

import { readfile, writefile, mkdir, rename, unlink, stat, rmdir, dirname, opendir, popen } from 'fs';

const ORCH_DIR      = getenv('ZAPRET2_ORCHESTRA_DIR')  ?? '/etc/zapret2-orchestra';
const RUNTIME_DIR   = getenv('ZAPRET2_RUNTIME_DIR')   ?? '/tmp/zapret2-orchestra';
const SHARE_DIR     = getenv('ZAPRET2_SHARE_DIR')     ?? '/usr/share/zapret2-orchestra';
const CONFIG_FILE   = getenv('ZAPRET2_CONFIG')        ?? '/opt/zapret2/config';
const STATE_FILE    = getenv('ZAPRET2_STATE_FILE')    ?? (ORCH_DIR + '/manager-state.json');
const LOCK_DIR      = getenv('ZAPRET2_LOCK_DIR')      ?? '/var/lock/zapret2-orchestra-apply.lock';
const CANDIDATE_FILE = getenv('ZAPRET2_CANDIDATE_FILE') ?? '/opt/zapret2/.config.orchestra.tmp';
const BACKUP_DIR    = getenv('ZAPRET2_BACKUP_DIR')    ?? (ORCH_DIR + '/backup');
const VALIDATE_OUT  = getenv('ZAPRET2_VALIDATE_OUT')  ?? (RUNTIME_DIR + '/validate-config.cfg');
const USER_PROFILES_DIR    = getenv('ZAPRET2_USER_PROFILES_DIR')    ?? (ORCH_DIR + '/profiles');
const BUILTIN_PROFILES_DIR = getenv('ZAPRET2_BUILTIN_PROFILES_DIR') ?? (SHARE_DIR + '/profiles');
const ORCH_LUA      = getenv('ZAPRET2_ORCHESTRA_LUA') ?? '/opt/zapret2/lua/orchestra-extra';
const PRELOAD_WRAPPER = getenv('ZAPRET2_PRELOAD_WRAPPER') ?? '/usr/sbin/zapret2-orchestra-preload';

const MAX_BACKUPS = 3;
const NFQWS2_KEY = 'NFQWS2_OPT';

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

function fail(msg) {
	die('zapret2-orchestra-apply: ' + msg);
}

function emit(obj) {
	printf('%J\n', obj);
}

// Single-quote a string for a /bin/sh -c context. Embedded single quotes are
// closed, escaped, and reopened (the standard '..'\''..' trick). Used only on
// controlled paths/literals (never user NFQWS2_OPT input, which lives in the
// candidate FILE, not on any command line).
function shell_quote(s) {
	return "'" + replace(s, "'", "'\\''") + "'";
}

function line_of(text, index) {
	let n = 0;
	for (let i = 0; i < index && i < length(text); i++)
		if (substr(text, i, 1) == '\n')
			n++;
	return n + 1;
}

// Does `line` begin (after optional spaces/tabs, not a comment) with
// "NFQWS2_OPT="? Return the column of the 'N' or -1.
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

// ---------------------------------------------------------------------------
// NFQWS2_OPT parser (mirrors tests/_nfqws2_parser.py exactly)
// ---------------------------------------------------------------------------

// Returns { ok, error, value, raw, head, tail, open_line, close_line }.
// On success ok=true; on failure ok=false and error is set.
function parse_nfqws2_opt(text) {
	// Collect the absolute start index of every NFQWS2_OPT= assignment line.
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

// Return the full config text with NFQWS2_OPT replaced by new_value.
// Raises (fail) on parse error.
function transform_nfqws2_opt(text, new_value) {
	let p = parse_nfqws2_opt(text);
	if (!p.ok)
		fail(p.error);
	return p.head + escape_value(new_value) + p.tail;
}

// 31-bit rolling hash (djb2 variant), identical to generate-preload.uc.
function hash31(data) {
	let h = 5381;
	for (let i = 0; i < length(data); i++)
		h = (h * 33 + ord(substr(data, i, 1))) & 0x7fffffff;
	return h;
}

// ---------------------------------------------------------------------------
// Subprocess helpers (array-form popen only — no shell, execvp directly)
// ---------------------------------------------------------------------------

// Run `sh -n <path>` (parse-only syntax check, never executes the file).
// Uses system() with an array — execvp directly, NO shell. sh -n writes any
// syntax error to its (inherited) stderr; we only need the exit code.
// Returns { ok: bool, rc: int, stderr: string }.
function run_sh_n(path) {
	let rc = system(['sh', '-n', path]);
	if (rc == null)
		return { ok: false, rc: -1, stderr: 'system() failed' };
	return { ok: rc == 0, rc: rc, stderr: '' };
}

// Run the preload wrapper with the given mode ('generate' or 'check').
// ucode's fs.popen is shell-based (/bin/sh -c); the command string is only
// shell-quoted controlled paths + a literal mode (no user input on the
// command line). popen('r') captures the wrapper's stdout so its
// "orchestra preload generated: ..." line does not pollute our JSON emit().
// Returns { ok: bool, rc: int, stdout: string, stderr: string }.
function run_preload(mode) {
	let proc = popen(shell_quote(PRELOAD_WRAPPER) + ' ' + shell_quote(mode), 'r');
	if (proc == null)
		return { ok: false, rc: -1, stdout: '', stderr: 'popen failed' };
	let out = proc.read('all');
	let rc = proc.close();
	return { ok: rc == 0, rc: rc, stdout: out ?? '', stderr: '' };
}

// ---------------------------------------------------------------------------
// Atomic JSON state helper (manager-state.json)
// ---------------------------------------------------------------------------

// manager-state.json schema:
// {
//   "schema_version": 1,
//   "states": ["idle"],            -- current state stack
//   "generation": 0,               -- monotonically increasing (only after success)
//   "previous_state": null,        -- last committed state
//   "enabled": false,              -- is Orchestra enabled?
//   "profile": null,               -- last applied profile name
//   "applying_gen": null,          -- generation at start of an interrupted apply
//   "applying_backup": null,       -- backup filename of an interrupted apply
//   "hash_algorithm": "djb2-31",
//   "hashes": {
//     "nfqws2_opt": "00000000",    -- hash of NFQWS2_OPT (NOT the full text)
//     "preload":   "00000000",     -- hash of /tmp/zapret2-orchestra/preload.lua
//     "whitelist": "00000000",     -- hash of /tmp/zapret2-orchestra/whitelist.txt
//     "manifest":  "00000000"      -- hash of /tmp/zapret2-orchestra/manifest.json
//   },
//   "updated_at": 0,               -- unix ts of last successful write
//   "last_error": null,            -- last error string, or null
//   "warnings": []                 -- non-fatal warnings
// }
//
// The FULL NFQWS2_OPT text is never stored; only its hash.
// manager-state.json lives under /etc/zapret2-orchestra/ (persistent) and
// is NOT a conffile — it is rebuilt by the manager and may be safely
// deleted; the manager recreates it with the default state on next read.

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
		enabled: false,
		profile: null,
		applying_gen: null,
		applying_backup: null,
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
	// enabled and profile are optional (absent = false/null in older state)
	return null;
}

// Ensure the parent directory of STATE_FILE exists (the persistent
// /etc/zapret2-orchestra/ is created by the package install, but tests
// may override the path to a temp location).
function ensure_state_dir() {
	let parent = dirname(STATE_FILE);
	let info = stat(parent);
	if (info == null) {
		if (!mkdir(parent))
			fail('cannot create state dir ' + parent);
		info = stat(parent);
	}
	if (info?.type != 'directory')
		fail(parent + ' is not a directory');
}

// Atomic write: tmp + rename in the same directory. Validate the serialized
// bytes by re-parsing before installing.
function atomic_write_state(doc) {
	ensure_state_dir();
	let err = validate_state(doc);
	if (err)
		fail('state validation failed: ' + err);
	let payload = sprintf('%J\n', doc);
	// Re-parse to prove the bytes are valid JSON and round-trip the schema.
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
		return null;  // corrupted; caller decides
	}
	if (validate_state(doc) != null)
		return null;
	return doc;
}

// ---------------------------------------------------------------------------
// mkdir lock
// ---------------------------------------------------------------------------

// mkdir is atomic on POSIX filesystems: exactly one caller succeeds.
// The lock is a directory at LOCK_DIR. After a successful mkdir the holder
// immediately writes a `pid` file inside it. To avoid the race between
// mkdir and writefile (a second caller could see the dir with no pid and
// wrongly treat it as stale) we NEVER remove a lock that has no pid file —
// it may belong to a holder still inside the writefile window.
//
// Stale recovery is safe: we remove only when the pid file IS present AND
// the recorded PID is confirmed dead (no /proc entry or cmdline mismatch).
// Before removing we re-read the pid file to ensure it hasn't changed
// (another process may have already recovered and a new holder may have
// written a new pid). Release removes only the pid file and the lock dir
// (no bulk removal), and only if the recorded PID is still ours (guards
// against PID reuse).

function my_pid() {
	let p = readfile('/proc/self/stat');
	if (p != null) {
		let sp = split(p, ' ');
		if (length(sp) > 0)
			return int(sp[0]);
	}
	// fallback: env override for tests
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
	// cmdline is NUL-separated; look for our marker.
	return index(cmd, 'zapret2-orchestra-apply') >= 0 || index(cmd, 'apply.uc') >= 0;
}

function lock_acquire() {
	if (mkdir(LOCK_DIR)) {
		// we won the mkdir; record our pid immediately.
		let pid = my_pid();
		if (writefile(LOCK_DIR + '/pid', '' + pid + '\n') == null) {
			rmdir(LOCK_DIR);
			return { ok: false, error: 'cannot write pid file' };
		}
		return { ok: true, pid: pid, stale_recovered: false };
	}

	// mkdir failed: the lock dir already exists. Inspect the holder.
	let pidraw = readfile(LOCK_DIR + '/pid');
	if (pidraw == null) {
		// No pid file: the holder may still be inside the writefile
		// window between mkdir and writefile. Do NOT remove — treat as
		// busy. This closes the mkdir/writefile race.
		return { ok: false, error: 'lock busy (no pid file)' };
	}

	let holder = int(trim(pidraw));
	if (holder > 0 && !pid_alive_and_is_manager(holder)) {
		// Stale: the holder PID is dead or not our executable. Safe
		// re-check: re-read the pid file to confirm it hasn't changed
		// (another process may have already recovered this lock).
		let pidraw2 = readfile(LOCK_DIR + '/pid');
		if (pidraw2 == null || int(trim(pidraw2)) != holder) {
			// The pid file changed (or was removed) between our two
			// reads: someone else already recovered. Report busy.
			return { ok: false, error: 'lock busy (recovered by another)' };
		}
		// Confirmed stale: unlink the pid file and rmdir, then retry
		// the mkdir exactly once. This removes only the dead holder's
		// pid file and the dir — never a live holder's lock.
		unlink(LOCK_DIR + '/pid');
		rmdir(LOCK_DIR);
		if (mkdir(LOCK_DIR)) {
			let pid = my_pid();
			if (writefile(LOCK_DIR + '/pid', '' + pid + '\n') == null) {
				rmdir(LOCK_DIR);
				return { ok: false, error: 'cannot write pid file' };
			}
			return { ok: true, pid: pid, stale_recovered: true };
		}
		// Lost the retry race to another recoverer.
		return { ok: false, error: 'lock busy (lost retry)' };
	}

	// Live holder.
	return { ok: false, error: 'lock busy', holder: holder };
}

// Release only if the recorded PID equals ours.
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

// ---------------------------------------------------------------------------
// Backup helpers
// ---------------------------------------------------------------------------

function ensure_backup_dir() {
	let info = stat(BACKUP_DIR);
	if (info == null) {
		if (!mkdir(BACKUP_DIR))
			fail('cannot create backup dir ' + BACKUP_DIR);
		info = stat(BACKUP_DIR);
	}
	if (info?.type != 'directory')
		fail(BACKUP_DIR + ' is not a directory');
}

// Return the backup filename for a given generation.
function backup_path(gen) {
	return BACKUP_DIR + '/config.gen-' + gen + '.bak';
}

// Copy the current config to a backup file named after the generation.
// Uses readfile+writefile (not cp) so no shell is involved.
function backup_config(gen) {
	ensure_backup_dir();
	let data = readfile(CONFIG_FILE);
	if (data == null)
		fail('cannot read config for backup');
	let path = backup_path(gen);
	let tmp = path + '.tmp';
	if (writefile(tmp, data) == null)
		fail('cannot write backup tmp ' + tmp);
	if (!rename(tmp, path)) {
		unlink(tmp);
		fail('cannot install backup ' + path);
	}
	return path;
}

// Restore a backup by renaming it over the config. Returns {ok, error}.
// This is an atomic rename on the same filesystem only if the backup and
// config share a filesystem; /etc and /opt may differ. When they differ,
// we fall back to readfile+writefile+rename within /opt.
function restore_backup(path) {
	let data = readfile(path);
	if (data == null)
		return { ok: false, error: 'cannot read backup ' + path };
	// Write to the candidate path (same filesystem as config) then rename.
	if (writefile(CANDIDATE_FILE, data) == null)
		return { ok: false, error: 'cannot write restore candidate' };
	if (!rename(CANDIDATE_FILE, CONFIG_FILE)) {
		unlink(CANDIDATE_FILE);
		return { ok: false, error: 'cannot rename restore candidate to config' };
	}
	return { ok: true };
}

// Prune old backups, keeping at most MAX_BACKUPS (the most recent by gen).
function prune_backups(keep_gen) {
	ensure_backup_dir();
	let entries = [];
	let dh = opendir(BACKUP_DIR);
	if (dh == null) return;
	for (let name = dh.read(); name != null; name = dh.read()) {
		if (name == '.' || name == '..') continue;
		let m = match(name, '^config\\.gen-(\\d+)\\.bak$');
		if (m) push(entries, { name: name, gen: int(m[1]) });
	}
	dh.close();
	// Sort by gen descending; keep the top MAX_BACKUPS; remove the rest.
	entries = sort(entries, (a, b) => b.gen - a.gen);
	for (let i = MAX_BACKUPS; i < length(entries); i++)
		unlink(BACKUP_DIR + '/' + entries[i].name);
}

// Find the most recent backup generation. Returns {gen, path} or null.
function latest_backup() {
	ensure_backup_dir();
	let best = null;
	let dh = opendir(BACKUP_DIR);
	if (dh == null) return null;
	for (let name = dh.read(); name != null; name = dh.read()) {
		if (name == '.' || name == '..') continue;
		let m = match(name, '^config\\.gen-(\\d+)\\.bak$');
		if (m) {
			let g = int(m[1]);
			if (best == null || g > best.gen)
				best = { gen: g, path: BACKUP_DIR + '/' + name };
		}
	}
	dh.close();
	return best;
}

// ---------------------------------------------------------------------------
// Hash helpers
// ---------------------------------------------------------------------------

// Compute the hash of the current NFQWS2_OPT value from the config file.
// Returns the 8-hex-char hash, or null if the config is unparseable.
function current_config_hash() {
	let text = readfile(CONFIG_FILE);
	if (text == null) return null;
	let p = parse_nfqws2_opt(text);
	if (!p.ok) return null;
	return sprintf('%08x', hash31(p.value));
}

// Read a runtime file and return its hash, or '00000000' if missing.
function runtime_hash(path) {
	let data = readfile(path);
	if (data == null) return '00000000';
	return sprintf('%08x', hash31(data));
}

// Compute all four hashes from the current on-disk state.
function compute_current_hashes() {
	return {
		nfqws2_opt: current_config_hash() ?? '00000000',
		preload:    runtime_hash(RUNTIME_DIR + '/preload.lua'),
		whitelist:  runtime_hash(RUNTIME_DIR + '/whitelist.txt'),
		manifest:   runtime_hash(RUNTIME_DIR + '/manifest.json')
	};
}

// ---------------------------------------------------------------------------
// Profile validation
// ---------------------------------------------------------------------------

// Reject characters/sequences that have no business in a profile argument:
// command substitution $(), backticks, NUL, CR, unclosed quotes, and shell
// separators (;, |, &&, >, <). A profile is a set of --lua-desync / --filter
// arguments, not an arbitrary shell snippet.
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
	// Note: '<' and '>' are NOT rejected. NFQWS2_OPT values legitimately
	// use <HOSTLIST> placeholder tokens (e.g. "--filter-l7=tls <HOSTLIST>").
	// The value is always placed inside a double-quoted NFQWS2_OPT="..."
	// assignment with proper escaping of '"' and '\', so '<' and '>' inside
	// the quotes cannot act as shell redirects. The unclosed-quote check
	// below and the $()`;|&& checks above are sufficient for injection safety.
	// unclosed single or double quote
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

// A profile is a single .opt file containing an NFQWS2_OPT assignment.
// The user override at /etc/zapret2-orchestra/profiles/<name>.opt takes
// priority over the builtin at /usr/share/zapret2-orchestra/profiles/<name>.opt.
// The profile value must pass profile_value_ok, and the Orchestra runtime
// markers (init.lua, whitelist seed, circular_quality) must be present.
function validate_profile(name) {
	let problems = [];
	if (name == null || length(name) == 0) {
		push(problems, 'profile name is empty');
		return { ok: false, problems: problems };
	}
	// Reject path traversal in the name.
	if (index(name, '/') >= 0 || index(name, '..') >= 0 || index(name, '\x00') >= 0) {
		push(problems, 'profile name contains path traversal');
		return { ok: false, problems: problems };
	}

	let user_path = USER_PROFILES_DIR + '/' + name + '.opt';
	let builtin_path = BUILTIN_PROFILES_DIR + '/' + name + '.opt';
	let chosen = null;
	let source_type = null;
	if (stat(user_path)?.type == 'file') {
		chosen = user_path;
		source_type = 'user';
	}
	else if (stat(builtin_path)?.type == 'file') {
		chosen = builtin_path;
		source_type = 'builtin';
	}
	if (chosen == null) {
		push(problems, 'profile not found in user or builtin directory');
		return { ok: false, problems: problems };
	}

	let text = readfile(chosen);
	if (text == null) {
		push(problems, 'cannot read ' + chosen);
		return { ok: false, problems: problems };
	}

	let p = parse_nfqws2_opt(text);
	if (!p.ok) {
		push(problems, 'profile parse error: ' + p.error);
		return { ok: false, problems: problems, source: chosen, source_type: source_type };
	}
	let verr = profile_value_ok(p.value);
	if (verr != null)
		push(problems, 'profile value rejected: ' + verr);

	// Orchestra runtime markers: init.lua and the whitelist seed must exist,
	// and circular_quality must be referenced by the profile value.
	if (stat(ORCH_LUA + '/init.lua')?.type != 'file')
		push(problems, 'orchestra init.lua missing');
	if (stat(ORCH_DIR + '/whitelist.json')?.type != 'file')
		push(problems, 'orchestra whitelist.json seed missing');
	if (index(p.value, 'circular_quality') < 0)
		push(problems, 'profile does not reference circular_quality');

	// The profile is NOT applied to the config in Phase 1A.
	return { ok: length(problems) == 0, problems: problems, source: chosen, source_type: source_type, value_bytes: length(p.value) };
}

// ---------------------------------------------------------------------------
// Subcommands
// ---------------------------------------------------------------------------

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
		phase: '1b',
		config: { path: CONFIG_FILE, exists: cfg_exists },
		nfqws2_opt: nfqws2_opt,
		parse_error: parse_error,
		state: { path: STATE_FILE, exists: state_file_exists, valid: state_ok, states: state?.states, generation: state?.generation, enabled: state?.enabled, profile: state?.profile, applying_gen: state?.applying_gen },
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
	// Re-emit the config with the SAME value to prove the transformer is
	// byte-stable and produces valid shell. Write ONLY to the temp path
	// (never to CONFIG_FILE). The CLI wrapper runs `sh -n` on this temp.
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
	emit({ ok: r.ok, profile: name, source: r.source, source_type: r.source_type, problems: r.problems, value_bytes: r.value_bytes });
	exit(r.ok ? 0 : 1);
}

function cmd_lock_test() {
	// Acquire, report, release. Used by tests to prove the lock works and
	// that release only removes when mine.
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

// ---------------------------------------------------------------------------
// Parse a backup filename "config.gen-<N>.bak" → generation int, or -1.
// ---------------------------------------------------------------------------

function parse_backup_gen(name) {
	let prefix = 'config.gen-';
	let suffix = '.bak';
	if (substr(name, 0, length(prefix)) != prefix) return -1;
	if (length(name) <= length(prefix) + length(suffix)) return -1;
	if (substr(name, length(name) - length(suffix)) != suffix) return -1;
	let mid = substr(name, length(prefix), length(name) - length(prefix) - length(suffix));
	let g = int(mid);
	if (g <= 0) return -1;
	return g;
}

// ---------------------------------------------------------------------------
// Transaction core
// ---------------------------------------------------------------------------

// Internal rollback: restore the backup over the config. Called WITHOUT
// re-acquiring the lock (the caller already holds it). Returns {ok, error}.
function internal_rollback(backup_path) {
	if (backup_path == null || stat(backup_path)?.type != 'file')
		return { ok: false, error: 'backup not found: ' + (backup_path ?? 'null') };
	return restore_backup(backup_path);
}

// The core apply transaction. ASSUMES THE LOCK IS ALREADY HELD.
// Steps:
//   1. read current state + config
//   2. parse NFQWS2_OPT, build candidate
//   3. byte-preservation sanity (re-emit same value == original)
//   4. write candidate to CANDIDATE_FILE (same FS as config)
//   5. sh -n on candidate (parse-only, no execution)
//   6. write state=applying (persistent, survives reboot)
//   7. backup current config
//   8. rename candidate → config (atomic, same FS)
//   9. regenerate + check preload
//  10. commit state: idle, generation++, hashes updated
//  11. prune old backups
// On failure BEFORE rename: unlink candidate, state=idle, no config change.
// On failure AFTER rename: internal_rollback (restore backup), state=idle.
// Generation is incremented ONLY on full success.
function do_apply_transaction(new_value, profile_name) {
	let state = read_state();
	if (state == null) state = default_state();
	let gen = state.generation;

	// 1. Read current config
	let config_text = readfile(CONFIG_FILE);
	if (config_text == null)
		return { ok: false, error: 'cannot read config ' + CONFIG_FILE };

	// 2. Parse current NFQWS2_OPT
	let p = parse_nfqws2_opt(config_text);
	if (!p.ok)
		return { ok: false, error: 'config parse: ' + p.error };

	// 3. Build candidate text (transform NFQWS2_OPT value)
	let candidate_text = p.head + escape_value(new_value) + p.tail;

	// 4. Write candidate to CANDIDATE_FILE (same filesystem as config)
	if (writefile(CANDIDATE_FILE, candidate_text) == null)
		return { ok: false, error: 'cannot write candidate ' + CANDIDATE_FILE };

	// 5. sh -n on candidate (parse-only, never executes)
	let shn = run_sh_n(CANDIDATE_FILE);
	if (!shn.ok) {
		unlink(CANDIDATE_FILE);
		return { ok: false, error: 'sh -n rejected candidate: ' + shn.stderr, sh_n_ok: false };
	}

	// 6. Write state=applying (persistent marker — survives reboot/power loss)
	state.states = ['applying'];
	state.applying_gen = gen;
	state.applying_backup = null;
	state.last_error = null;
	atomic_write_state(state);

	// 7. Backup current config (before the rename)
	let bpath = backup_config(gen);
	state.applying_backup = bpath;
	atomic_write_state(state);

	// 8. Rename candidate → config (atomic on same filesystem)
	if (!rename(CANDIDATE_FILE, CONFIG_FILE)) {
		unlink(CANDIDATE_FILE);
		state.states = ['idle'];
		state.applying_gen = null;
		state.applying_backup = null;
		state.last_error = 'cannot rename candidate to config';
		atomic_write_state(state);
		return { ok: false, error: 'cannot rename candidate to config' };
	}

	// 9. Regenerate + check preload (post-rename)
	let pre = run_preload('generate');
	if (!pre.ok) {
		// ERROR AFTER RENAME: internal rollback (restore backup, no re-lock)
		let rb = internal_rollback(bpath);
		run_preload('generate');  // regenerate preload from restored config
		state.states = ['idle'];
		state.applying_gen = null;
		state.applying_backup = null;
		state.last_error = 'preload generate failed (rc=' + pre.rc + '), rolled back';
		atomic_write_state(state);
		return { ok: false, error: 'preload generate failed', rollback: rb };
	}
	let chk = run_preload('check');
	if (!chk.ok) {
		let rb = internal_rollback(bpath);
		run_preload('generate');
		state.states = ['idle'];
		state.applying_gen = null;
		state.applying_backup = null;
		state.last_error = 'preload check failed, rolled back';
		atomic_write_state(state);
		return { ok: false, error: 'preload check failed', rollback: rb };
	}

	// 10. Commit: state=idle, generation++, update hashes, enabled/profile
	state.states = ['idle'];
	state.generation = gen + 1;
	state.previous_state = 'applying';
	state.last_error = null;
	state.enabled = (profile_name != null);
	state.profile = profile_name;
	state.applying_gen = null;
	state.applying_backup = null;
	state.hashes = compute_current_hashes();
	state.updated_at = time();
	atomic_write_state(state);

	// 11. Prune old backups (keep most recent MAX_BACKUPS)
	prune_backups(gen + 1);

	return { ok: true, generation: gen + 1, backup: bpath };
}

// ---------------------------------------------------------------------------
// cmd_apply: apply a profile value to the config (transactional)
// ---------------------------------------------------------------------------

function cmd_apply() {
	let profile_name = ARGV[0];
	if (profile_name == null)
		fail('usage: apply <profile-name>');
	// Validate the profile first (read-only, no lock needed)
	let pv = validate_profile(profile_name);
	if (!pv.ok)
		fail('profile invalid: ' + join('; ', pv.problems));
	// Read the profile value
	let ptext = readfile(pv.source);
	if (ptext == null)
		fail('cannot read profile ' + pv.source);
	let pp = parse_nfqws2_opt(ptext);
	if (!pp.ok)
		fail('profile parse error: ' + pp.error);

	// Acquire lock, run transaction, release
	let acq = lock_acquire();
	if (!acq.ok)
		fail('lock: ' + acq.error);
	let result = do_apply_transaction(pp.value, profile_name);
	lock_release();
	if (!result.ok) {
		emit({ ok: false, error: result.error, sh_n_ok: result.sh_n_ok ?? true, rollback: result.rollback });
		exit(1);
	}
	emit({ ok: true, command: 'apply', profile: profile_name, source: pv.source, source_type: pv.source_type, generation: result.generation, backup: result.backup });
	exit(0);
}

// ---------------------------------------------------------------------------
// cmd_enable: apply a profile and mark Orchestra enabled (idempotent)
// ---------------------------------------------------------------------------

function cmd_enable() {
	let profile_name = ARGV[0] ?? 'orchestra-tls-mvp';
	let state = read_state();
	if (state == null) state = default_state();

	// Idempotent: if already enabled with the same profile, no-op
	if (state.enabled == true && state.profile == profile_name) {
		emit({ ok: true, command: 'enable', profile: profile_name, idempotent: true, generation: state.generation });
		exit(0);
	}

	// Validate the profile
	let pv = validate_profile(profile_name);
	if (!pv.ok)
		fail('profile invalid: ' + join('; ', pv.problems));
	let ptext = readfile(pv.source);
	if (ptext == null)
		fail('cannot read profile ' + pv.source);
	let pp = parse_nfqws2_opt(ptext);
	if (!pp.ok)
		fail('profile parse error: ' + pp.error);

	// Acquire lock, run transaction, release
	let acq = lock_acquire();
	if (!acq.ok)
		fail('lock: ' + acq.error);
	let result = do_apply_transaction(pp.value, profile_name);
	lock_release();
	if (!result.ok) {
		emit({ ok: false, command: 'enable', error: result.error, rollback: result.rollback });
		exit(1);
	}
	emit({ ok: true, command: 'enable', profile: profile_name, source: pv.source, source_type: pv.source_type, generation: result.generation, backup: result.backup });
	exit(0);
}

// ---------------------------------------------------------------------------
// cmd_disable: restore the last backup and mark Orchestra disabled
// (idempotent, does not delete user data)
// ---------------------------------------------------------------------------

function cmd_disable() {
	let state = read_state();
	if (state == null) state = default_state();

	// Idempotent: if not enabled, no-op
	if (state.enabled != true) {
		emit({ ok: true, command: 'disable', idempotent: true, generation: state.generation });
		exit(0);
	}

	// Find the latest backup to restore
	let bk = latest_backup();
	if (bk == null)
		fail('no backup available to disable — use rollback with --force or re-apply a safe profile');

	let acq = lock_acquire();
	if (!acq.ok)
		fail('lock: ' + acq.error);

	// Check for drift: if the current config hash doesn't match the state
	// hash, the config was modified externally. We still restore (disable
	// is an explicit user request) but record a warning.
	let cur_hash = current_config_hash();
	let drift = (cur_hash != null && state.hashes.nfqws2_opt != '00000000' && cur_hash != state.hashes.nfqws2_opt);

	// Write state=applying (persistent marker)
	state.states = ['applying'];
	state.applying_gen = state.generation;
	state.applying_backup = bk.path;
	atomic_write_state(state);

	// Restore backup
	let rb = restore_backup(bk.path);
	if (!rb.ok) {
		state.states = ['idle'];
		state.applying_gen = null;
		state.applying_backup = null;
		state.last_error = 'disable: restore failed: ' + rb.error;
		atomic_write_state(state);
		lock_release();
		emit({ ok: false, command: 'disable', error: rb.error });
		exit(1);
	}

	// Regenerate preload from restored config
	run_preload('generate');

	// Commit state
	state.states = ['idle'];
	state.generation = state.generation + 1;
	state.previous_state = 'disabled';
	state.enabled = false;
	state.profile = null;
	state.applying_gen = null;
	state.applying_backup = null;
	state.hashes = compute_current_hashes();
	state.updated_at = time();
	state.last_error = null;
	if (drift)
		push(state.warnings, 'disable: config drift detected before restore');
	atomic_write_state(state);

	lock_release();
	emit({ ok: true, command: 'disable', restored_from: bk.path, generation: state.generation, drift: drift });
	exit(0);
}

// ---------------------------------------------------------------------------
// cmd_rollback: restore a backup generation (default: latest)
// --force: restore without drift check
// ---------------------------------------------------------------------------

function cmd_rollback() {
	let force = false;
	let target_gen = null;
	for (let i = 0; i < length(ARGV); i++) {
		if (ARGV[i] == '--force') force = true;
		else if (ARGV[i] == '--gen') target_gen = int(ARGV[i + 1] ?? '0');
	}

	let state = read_state();
	if (state == null) state = default_state();

	// Find the backup to restore
	let bk;
	if (target_gen != null && target_gen > 0) {
		let path = backup_path(target_gen);
		if (stat(path)?.type != 'file')
			fail('backup generation ' + target_gen + ' not found at ' + path);
		bk = { gen: target_gen, path: path };
	} else {
		bk = latest_backup();
		if (bk == null)
			fail('no backup available to rollback');
	}

	// Drift check (unless --force): if the current config hash doesn't match
	// the state hash, the config was modified externally after the last
	// apply. Without --force, we refuse to overwrite and report rollback-conflict.
	let cur_hash = current_config_hash();
	let drift = (cur_hash != null && state.hashes.nfqws2_opt != '00000000' && cur_hash != state.hashes.nfqws2_opt);
	if (drift && !force) {
		state.last_error = 'rollback-conflict: config modified externally';
		push(state.warnings, 'rollback-conflict: current config hash does not match state; use --force to override');
		atomic_write_state(state);
		emit({ ok: false, command: 'rollback', error: 'rollback-conflict', detail: 'config was modified externally; use --force to override', current_hash: cur_hash, state_hash: state.hashes.nfqws2_opt });
		exit(1);
	}

	// Acquire lock
	let acq = lock_acquire();
	if (!acq.ok)
		fail('lock: ' + acq.error);

	// Write state=applying
	state.states = ['applying'];
	state.applying_gen = state.generation;
	state.applying_backup = bk.path;
	atomic_write_state(state);

	// Restore
	let rb = restore_backup(bk.path);
	if (!rb.ok) {
		state.states = ['idle'];
		state.applying_gen = null;
		state.applying_backup = null;
		state.last_error = 'rollback: restore failed: ' + rb.error;
		atomic_write_state(state);
		lock_release();
		emit({ ok: false, command: 'rollback', error: rb.error });
		exit(1);
	}

	// Regenerate preload
	run_preload('generate');

	// Commit state (generation does NOT increase on rollback per spec)
	state.states = ['idle'];
	state.previous_state = 'rolled_back';
	state.enabled = false;
	state.profile = null;
	state.applying_gen = null;
	state.applying_backup = null;
	state.hashes = compute_current_hashes();
	state.updated_at = time();
	state.last_error = null;
	if (drift)
		push(state.warnings, 'rollback: forced restore over drifted config');
	atomic_write_state(state);

	lock_release();
	emit({ ok: true, command: 'rollback', restored_from: bk.path, generation: bk.gen, forced: force, drift: drift });
	exit(0);
}

// ---------------------------------------------------------------------------
// cmd_boot_check: detect interrupted apply, recover, no health-check
// ---------------------------------------------------------------------------

function cmd_boot_check() {
	let state = read_state();
	if (state == null) state = default_state();
	let warnings = [];

	// 1. Detect persistent state=applying (interrupted apply / power loss)
	let was_applying = false;
	for (let i = 0; i < length(state.states); i++) {
		if (state.states[i] == 'applying') was_applying = true;
	}

	if (was_applying) {
		// The last apply was interrupted. Try to restore from the backup
		// recorded in applying_backup. If that's missing, try the latest
		// backup. If no backup at all, leave the config as-is and warn.
		let bpath = state.applying_backup;
		if (bpath == null || stat(bpath)?.type != 'file') {
			let bk = latest_backup();
			bpath = bk?.path;
		}
		if (bpath != null && stat(bpath)?.type == 'file') {
			// Check if the config was already renamed (post-rename interruption)
			let cur_hash = current_config_hash();
			let backup_data = readfile(bpath);
			let backup_p = parse_nfqws2_opt(backup_data ?? '');
			let backup_hash = (backup_p.ok) ? sprintf('%08x', hash31(backup_p.value)) : null;
			if (cur_hash != null && cur_hash != backup_hash) {
				// Config differs from backup → the rename likely succeeded
				// (post-rename interruption). Accept the current config and
				// update the state to match. This is the "consistent config,
				// stale state" recovery path.
				push(warnings, 'interrupted apply: config differs from backup (post-rename); accepting current config');
				state.states = ['idle'];
				state.previous_state = 'applying-recovered-post-rename';
			} else {
				// Config matches backup (or is unparseable) → the rename did
				// not happen (pre-rename interruption). Restore the backup.
				push(warnings, 'interrupted apply: restoring backup ' + bpath);
				restore_backup(bpath);
				state.states = ['idle'];
				state.previous_state = 'applying-recovered-pre-rename';
			}
		} else {
			push(warnings, 'interrupted apply: no backup found; leaving config as-is');
			state.states = ['idle'];
			state.previous_state = 'applying-no-backup';
		}
		state.applying_gen = null;
		state.applying_backup = null;
	}

	// 2. Drift detection: compare current hashes with state hashes
	let cur_hashes = compute_current_hashes();
	if (state.hashes.nfqws2_opt != '00000000' && cur_hashes.nfqws2_opt != state.hashes.nfqws2_opt)
		push(warnings, 'drift: nfqws2_opt hash mismatch (state=' + state.hashes.nfqws2_opt + ' current=' + cur_hashes.nfqws2_opt + ')');
	if (state.hashes.preload != '00000000' && cur_hashes.preload != state.hashes.preload)
		push(warnings, 'drift: preload hash mismatch');

	// 3. Regenerate preload if missing or drifted (backup guarantee)
	let preload_exists = stat(RUNTIME_DIR + '/preload.lua')?.type == 'file';
	if (!preload_exists) {
		push(warnings, 'preload missing; regenerating');
		run_preload('generate');
	}

	// 4. Write updated state
	state.hashes = compute_current_hashes();
	state.updated_at = time();
	for (let i = 0; i < length(warnings); i++)
		push(state.warnings, warnings[i]);
	// Keep only the last 20 warnings to avoid unbounded growth
	if (length(state.warnings) > 20)
		state.warnings = slice(state.warnings, length(state.warnings) - 20);
	atomic_write_state(state);

	// 5. NO health check — zapret2 has not started yet at boot-check time
	//    (boot-check runs at START=20, before zapret2 at START=21).
	//    Absence of the service is NOT a successful health check.
	emit({ ok: true, command: 'boot-check', was_applying: was_applying, warnings: warnings, generation: state.generation, enabled: state.enabled, health_check: 'not-run (zapret2 not started)' });
	exit(0);
}

// ---------------------------------------------------------------------------
// Dispatch
// ---------------------------------------------------------------------------

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
	case 'apply':
		cmd_apply();
	case 'enable':
		cmd_enable();
	case 'disable':
		cmd_disable();
	case 'rollback':
		cmd_rollback();
	case 'boot-check':
		cmd_boot_check();
	default:
		emit({ ok: false, error: 'unknown subcommand', command: sub });
		exit(2);
}
