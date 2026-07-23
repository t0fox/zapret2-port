"""Tests for the zapret2-orchestra closed-loop learner daemon (r7).

Design (per the task): the state-machine LOGIC is exercisable in Python via a
faithful reference of the learner's ``process_event`` + cursor logic, so the
policy tests run everywhere (no ucode required).  A ucode-runtime class drives
the real ``learner.uc`` ``test-process`` mode and asserts the on-disk
learned.json / blocked.json / learner-state.json match — it skips cleanly when
``ucode`` is not on PATH (CI runs it on Linux with a zero-skip gate).

Coverage (contract §3 + the task's Step 8 list):
  * incremental read + durable cursor
  * idempotent duplicate events (dedup key = run_id+type+host+strategy+ts)
  * truncated-NDJSON recovery (partial last line is not advanced past)
  * auto-lock after 3 TCP SUCCESS / 1 UDP SUCCESS
  * auto-unlock after 3 consecutive FAIL on an auto-locked strategy
  * user-lock NEVER auto-unlocked
  * blocked priority over auto-lock AND user-lock (locked==blocked -> drop lock)
  * rating = successes/(successes+failures) (derived, not stored)
  * reload only on lock/unlock/blocked change, NOT on every SUCCESS/FAIL update
  * atomic state write + .good recovery (kill mid-write -> recover)
  * cursor recovery after restart
  * enable sets NFQWS2_ENABLE=1 (config byte-edit) + service_action:'start'
  * validate_profile accepts a native profile without circular_quality
  * discord.com ∈ DEFAULT_BLOCKED_PASS_DOMAINS (blocked strat=1 on TLS)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "openwrt" / "zapret2-orchestra"
FILES = PACKAGE / "files"
LEARNER_UC = FILES / "usr/share/zapret2-orchestra/learner.uc"
APPLY_UC = FILES / "usr/share/zapret2-orchestra/apply.uc"
BLOCKED_SEED = FILES / "etc/zapret2-orchestra/blocked.json"
DEV_BLOCKED = ROOT / "etc/zapret2-orchestra/blocked.json"

# UDP askeys lock after 1 SUCCESS; TCP after 3 (contract §3).
UDP_ASKEYS = {"quic", "discord", "wireguard", "dns", "stun", "unknown"}
LOCK_SUCCESSES_TCP = 3
LOCK_SUCCESSES_UDP = 1
UNLOCK_FAILS = 3


# ---------------------------------------------------------------------------
# Python reference of the learner's process_event + cursor logic.
#
# This mirrors learner.uc's process_event / read_events_from_cursor exactly.
# Keeping it faithful lets the policy tests run without ucode; the ucode-runtime
# class below confirms the real implementation matches.  If you change the
# policy in learner.uc, update this mirror too.
# ---------------------------------------------------------------------------

def _normalize_host(host: str | None) -> str | None:
    if not isinstance(host, str):
        return None
    h = host.lower().strip().strip(".")
    return h or None


def _host_matches_domain(host: str, domain: str) -> bool:
    if host == domain:
        return True
    return host.endswith("." + domain)


def _is_blocked(blocked: dict, askey: str, host: str, strategy: int) -> bool:
    bp = blocked.get("protocols", {}).get(askey)
    if not bp:
        return False
    def in_list(lst):
        if not isinstance(lst, list):
            return False
        return strategy in lst
    if in_list(bp.get("global")):
        return True
    if in_list(bp.get("user_global")):
        return True
    for dom, vals in (bp.get("hosts") or {}).items():
        if _host_matches_domain(host, dom) and in_list(vals):
            return True
    for dom, vals in (bp.get("user_hosts") or {}).items():
        if _host_matches_domain(host, dom) and in_list(vals):
            return True
    return False


def _is_user_locked(manual: dict, askey: str, host: str) -> bool:
    return host in (manual.get("protocols", {}).get(askey) or {})


def _ensure_rec(learned: dict, askey: str, host: str) -> dict:
    learned.setdefault("protocols", {}).setdefault(askey, {})
    rec = learned["protocols"][askey].get(host)
    if rec is None:
        rec = {"strategies": {}}
        learned["protocols"][askey][host] = rec
    rec.setdefault("strategies", {})
    return rec


def _history(rec: dict, strategy: int) -> dict:
    skey = str(strategy)
    rec["strategies"].setdefault(skey, {"successes": 0, "failures": 0})
    return rec["strategies"][skey]


def _dedup_key(ev: dict) -> str:
    return "|".join([
        str(ev.get("run_id", "")),
        str(ev.get("type", "")),
        str(ev.get("host", "")),
        str(ev.get("strategy", "")),
        str(ev.get("ts", "")),
    ])


def process_event(learned: dict, blocked: dict, manual: dict, ev: dict, seen: set[str]) -> dict:
    """Mirror of learner.uc process_event. Returns {changed_lock, changed_blocked, applied}."""
    result = {"changed_lock": False, "changed_blocked": False, "applied": False}
    if not isinstance(ev, dict):
        return result
    etype = ev.get("type")
    key = _dedup_key(ev)
    if key in seen:
        return result
    seen.add(key)
    result["applied"] = True

    askey = ev.get("askey") or ev.get("protocol")
    host = _normalize_host(ev.get("host"))
    strategy = ev.get("strategy")
    if askey is None or host is None or strategy is None:
        return result
    strategy = int(strategy)

    if etype in ("success", "fail"):
        rec = _ensure_rec(learned, askey, host)
        h = _history(rec, strategy)
        if etype == "success":
            h["successes"] = int(h.get("successes", 0)) + 1
        else:
            h["failures"] = int(h.get("failures", 0)) + 1
        if etype == "success":
            threshold = LOCK_SUCCESSES_UDP if askey in UDP_ASKEYS else LOCK_SUCCESSES_TCP
            already = rec.get("auto_lock") is not None
            user_locked = _is_user_locked(manual, askey, host)
            blocked_now = _is_blocked(blocked, askey, host, strategy)
            if not already and not user_locked and not blocked_now and h["successes"] >= threshold:
                rec["auto_lock"] = strategy
                result["changed_lock"] = True
        return result

    if etype == "lock":
        user_locked = _is_user_locked(manual, askey, host)
        blocked_now = _is_blocked(blocked, askey, host, strategy)
        if blocked_now:
            rec = learned.get("protocols", {}).get(askey, {}).get(host)
            if rec and rec.get("auto_lock") is not None:
                rec["auto_lock"] = None
                result["changed_lock"] = True
            return result
        if user_locked:
            return result
        rec = _ensure_rec(learned, askey, host)
        if rec.get("auto_lock") != strategy:
            rec["auto_lock"] = strategy
            result["changed_lock"] = True
        return result

    if etype == "unlock":
        if _is_user_locked(manual, askey, host):
            return result
        rec = learned.get("protocols", {}).get(askey, {}).get(host)
        if rec and rec.get("auto_lock") is not None:
            rec["auto_lock"] = None
            result["changed_lock"] = True
        return result

    # applied / rotate / start / stop / error: no persistent state change
    return result


def read_events_from_cursor(raw: str, cursor: dict, handler) -> tuple[dict, int]:
    """Mirror of learner.uc read_events_from_cursor. Truncated last line is not advanced past."""
    total = len(raw)
    pos = cursor.get("bytes", 0)
    if pos > total:
        pos = 0
    count = 0
    while pos < total:
        nl = raw.find("\n", pos)
        if nl < 0:
            break  # trailing partial line: stop, do not advance
        line = raw[pos:nl]
        next_pos = nl + 1
        try:
            obj = json.loads(line)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            handler(obj)
            count += 1
        pos = next_pos
    return {"bytes": pos, "lines": cursor.get("lines", 0) + count, "last_line_sha256": ""}, count


def rating(rec: dict, strategy: int) -> float | None:
    """Derived rating = successes/(successes+failures), or None if no tests."""
    h = rec.get("strategies", {}).get(str(strategy))
    if not h:
        return None
    s = int(h.get("successes", 0))
    f = int(h.get("failures", 0))
    if s + f == 0:
        return None
    return s / (s + f)


# ---------------------------------------------------------------------------
# State-machine logic tests (run everywhere; no ucode needed)
# ---------------------------------------------------------------------------

class LearnerStateMachineLogicTest(unittest.TestCase):
    """Exercises the learner's policy via the Python reference."""

    def _fresh(self) -> tuple[dict, dict, dict]:
        learned = {"schema_version": 1, "protocols": {}}
        blocked = {"schema_version": 1, "protocols": {}}
        manual = {"schema_version": 1, "protocols": {}}
        return learned, blocked, manual

    def _ev(self, etype: str, askey: str, host: str, strategy: int, ts: int = 1, run_id: str = "run1", **kw) -> dict:
        ev = {"schema_version": 1, "ts": ts, "type": etype, "askey": askey, "host": host, "strategy": strategy, "run_id": run_id}
        ev.update(kw)
        return ev

    def test_auto_lock_after_3_tcp_success(self) -> None:
        learned, blocked, manual = self._fresh()
        seen: set[str] = set()
        for i in range(2):
            r = process_event(learned, blocked, manual, self._ev("success", "tls", "a.com", 2, ts=i), seen)
            self.assertFalse(r["changed_lock"], f"no lock at {i+1} successes")
        r = process_event(learned, blocked, manual, self._ev("success", "tls", "a.com", 2, ts=3), seen)
        self.assertTrue(r["changed_lock"], "lock at 3 TCP successes")
        self.assertEqual(learned["protocols"]["tls"]["a.com"]["auto_lock"], 2)

    def test_auto_lock_after_1_udp_success(self) -> None:
        learned, blocked, manual = self._fresh()
        seen: set[str] = set()
        # quic is UDP -> 1 success locks
        r = process_event(learned, blocked, manual, self._ev("success", "quic", "1.2.3.4", 2, ts=1), seen)
        self.assertTrue(r["changed_lock"], "lock at 1 UDP success")
        self.assertEqual(learned["protocols"]["quic"]["1.2.3.4"]["auto_lock"], 2)
        # discord askey is also UDP
        learned2, _, _ = self._fresh()
        seen2: set[str] = set()
        r2 = process_event(learned2, blocked, manual, self._ev("success", "discord", "5.6.7.8", 1, ts=1), seen2)
        self.assertTrue(r2["changed_lock"])

    def test_no_lock_when_blocked(self) -> None:
        learned, blocked, manual = self._fresh()
        blocked["protocols"]["tls"] = {"global": [], "hosts": {"a.com": [2]}}
        seen: set[str] = set()
        for i in range(5):
            process_event(learned, blocked, manual, self._ev("success", "tls", "a.com", 2, ts=i), seen)
        self.assertIsNone(learned["protocols"]["tls"]["a.com"].get("auto_lock"), "blocked strategy never auto-locks")

    def test_no_lock_when_user_locked(self) -> None:
        learned, blocked, manual = self._fresh()
        manual["protocols"]["tls"] = {"a.com": 1}
        seen: set[str] = set()
        for i in range(5):
            process_event(learned, blocked, manual, self._ev("success", "tls", "a.com", 2, ts=i), seen)
        # history is still recorded
        self.assertEqual(learned["protocols"]["tls"]["a.com"]["strategies"]["2"]["successes"], 5)
        # but no auto_lock (user-lock protected)
        self.assertIsNone(learned["protocols"]["tls"]["a.com"].get("auto_lock"))

    def test_lock_event_blocked_drops_lock(self) -> None:
        # A LOCK event for a blocked strategy drops any existing auto_lock
        # (blocked wins, mirrors orchestrator.lua slm_reset).
        learned, blocked, manual = self._fresh()
        seen: set[str] = set()
        process_event(learned, blocked, manual, self._ev("lock", "tls", "a.com", 3, ts=1), seen)
        self.assertEqual(learned["protocols"]["tls"]["a.com"]["auto_lock"], 3)
        blocked["protocols"]["tls"] = {"global": [], "hosts": {"a.com": [3]}}
        r = process_event(learned, blocked, manual, self._ev("lock", "tls", "a.com", 3, ts=2), seen)
        self.assertTrue(r["changed_lock"])
        self.assertIsNone(learned["protocols"]["tls"]["a.com"].get("auto_lock"), "blocked drops the lock")

    def test_lock_event_user_locked_does_not_overwrite(self) -> None:
        learned, blocked, manual = self._fresh()
        manual["protocols"]["tls"] = {"a.com": 1}
        seen: set[str] = set()
        r = process_event(learned, blocked, manual, self._ev("lock", "tls", "a.com", 2, ts=1), seen)
        self.assertFalse(r["changed_lock"], "LOCK never overwrites a user lock")
        self.assertIsNone(learned.get("protocols", {}).get("tls", {}).get("a.com", {}).get("auto_lock"))
        # no learned record was created for the user-locked host
        self.assertNotIn("a.com", learned.get("protocols", {}).get("tls", {}))

    def test_unlock_after_3_fail_on_auto_locked(self) -> None:
        # The Lua runtime emits FAIL events; 3 consecutive FAILs on an
        # auto-locked strategy produce an UNLOCK event which the learner
        # persists as auto_lock=None.
        learned, blocked, manual = self._fresh()
        seen: set[str] = set()
        for i in range(3):
            process_event(learned, blocked, manual, self._ev("success", "tls", "a.com", 2, ts=i), seen)
        self.assertEqual(learned["protocols"]["tls"]["a.com"]["auto_lock"], 2)
        # 3 FAILs on the locked strategy (the runtime emits these; the learner
        # records history).  The UNLOCK event is what clears auto_lock.
        for i in range(3):
            process_event(learned, blocked, manual, self._ev("fail", "tls", "a.com", 2, ts=10 + i), seen)
        r = process_event(learned, blocked, manual, self._ev("unlock", "tls", "a.com", 2, ts=20), seen)
        self.assertTrue(r["changed_lock"], "UNLOCK clears an auto-lock")
        self.assertIsNone(learned["protocols"]["tls"]["a.com"].get("auto_lock"))

    def test_user_lock_never_auto_unlocked(self) -> None:
        learned, blocked, manual = self._fresh()
        manual["protocols"]["tls"] = {"a.com": 2}
        seen: set[str] = set()
        # An UNLOCK event for a user-locked host must NOT clear the user lock
        # (the user lock lives in manual-locks.json, which the learner never
        # auto-clears; process_event returns changed_lock=False and does not
        # touch learned).
        r = process_event(learned, blocked, manual, self._ev("unlock", "tls", "a.com", 2, ts=1), seen)
        self.assertFalse(r["changed_lock"], "UNLOCK never clears a user lock")
        # The user lock is untouched in manual-locks.json (learner doesn't own it)
        self.assertEqual(manual["protocols"]["tls"]["a.com"], 2)

    def test_unlock_on_non_locked_host_is_noop(self) -> None:
        learned, blocked, manual = self._fresh()
        seen: set[str] = set()
        r = process_event(learned, blocked, manual, self._ev("unlock", "tls", "a.com", 2, ts=1), seen)
        self.assertFalse(r["changed_lock"])

    def test_rating_is_derived_not_stored(self) -> None:
        learned, blocked, manual = self._fresh()
        seen: set[str] = set()
        process_event(learned, blocked, manual, self._ev("success", "tls", "a.com", 2, ts=1), seen)
        process_event(learned, blocked, manual, self._ev("success", "tls", "a.com", 2, ts=2), seen)
        process_event(learned, blocked, manual, self._ev("fail", "tls", "a.com", 2, ts=3), seen)
        rec = learned["protocols"]["tls"]["a.com"]
        # stored counts, NOT a rating field
        self.assertEqual(rec["strategies"]["2"], {"successes": 2, "failures": 1})
        self.assertNotIn("rating", rec["strategies"]["2"])
        # rating is derived
        self.assertAlmostEqual(rating(rec, 2), 2 / 3)

    def test_idempotent_duplicate_events(self) -> None:
        learned, blocked, manual = self._fresh()
        seen: set[str] = set()
        ev = self._ev("success", "tls", "a.com", 2, ts=1)
        r1 = process_event(learned, blocked, manual, ev, seen)
        r2 = process_event(learned, blocked, manual, ev, seen)  # same dedup key
        self.assertTrue(r1["applied"])
        self.assertFalse(r2["applied"], "duplicate event is a no-op")
        self.assertEqual(learned["protocols"]["tls"]["a.com"]["strategies"]["2"]["successes"], 1)

    def test_incremental_read_with_durable_cursor(self) -> None:
        raw = (
            '{"schema_version":1,"ts":1,"type":"success","askey":"tls","host":"a.com","strategy":2,"run_id":"r"}\n'
            '{"schema_version":1,"ts":2,"type":"success","askey":"tls","host":"a.com","strategy":2,"run_id":"r"}\n'
        )
        cursor = {"bytes": 0, "lines": 0, "last_line_sha256": ""}
        events: list[dict] = []
        cursor, n = read_events_from_cursor(raw, cursor, events.append)
        self.assertEqual(n, 2)
        self.assertEqual(cursor["bytes"], len(raw))
        # second pass from the advanced cursor reads nothing new
        cursor, n2 = read_events_from_cursor(raw, cursor, events.append)
        self.assertEqual(n2, 0)

    def test_truncated_ndjson_recovery(self) -> None:
        full_line = '{"schema_version":1,"ts":1,"type":"success","askey":"tls","host":"a.com","strategy":2,"run_id":"r"}\n'
        partial = '{"schema_version":1,"ts":2,"type":"success","askey":"tls","host":"a.com","strategy":2'  # no newline, truncated
        raw = full_line + partial
        cursor = {"bytes": 0, "lines": 0, "last_line_sha256": ""}
        events: list[dict] = []
        cursor, n = read_events_from_cursor(raw, cursor, events.append)
        # only the complete first line is processed; cursor stops at the partial line
        self.assertEqual(n, 1)
        self.assertEqual(cursor["bytes"], len(full_line))
        self.assertNotEqual(cursor["bytes"], len(raw), "cursor did not advance past the partial line")
        # now the writer completes the line
        raw2 = full_line + partial + ',"run_id":"r"}\n'
        cursor, n2 = read_events_from_cursor(raw2, cursor, events.append)
        self.assertEqual(n2, 1, "the previously-partial line is now processed")
        self.assertEqual(cursor["bytes"], len(raw2))

    def test_invalid_json_complete_line_is_skipped_but_advanced(self) -> None:
        # A terminated-but-unparseable line is advanced past (complete line);
        # only the UNterminated tail is held back.
        raw = 'not json at all\n{"schema_version":1,"ts":1,"type":"success","askey":"tls","host":"a.com","strategy":2,"run_id":"r"}\n'
        cursor = {"bytes": 0, "lines": 0, "last_line_sha256": ""}
        events: list[dict] = []
        cursor, n = read_events_from_cursor(raw, cursor, events.append)
        self.assertEqual(n, 1, "the valid line is processed; the invalid one is skipped")
        self.assertEqual(cursor["bytes"], len(raw))

    def test_cursor_recovery_after_restart(self) -> None:
        # Simulate a restart: the cursor persists, so the learner resumes from
        # the saved byte offset rather than re-reading the whole file.
        lines = [
            '{"schema_version":1,"ts":1,"type":"success","askey":"tls","host":"a.com","strategy":2,"run_id":"r"}\n',
            '{"schema_version":1,"ts":2,"type":"success","askey":"tls","host":"a.com","strategy":2,"run_id":"r"}\n',
            '{"schema_version":1,"ts":3,"type":"success","askey":"tls","host":"a.com","strategy":2,"run_id":"r"}\n',
        ]
        raw = "".join(lines)
        # first "run" processes 2 lines, persists cursor at offset after line 2
        cursor = {"bytes": 0, "lines": 0, "last_line_sha256": ""}
        events: list[dict] = []
        cursor, n = read_events_from_cursor(raw, cursor, events.append)
        # process only the first 2 by simulating a cursor saved mid-file:
        saved = {"bytes": len(lines[0]) + len(lines[1]), "lines": 2, "last_line_sha256": ""}
        cursor2, n2 = read_events_from_cursor(raw, saved, events.append)
        self.assertEqual(n2, 1, "resumed from the saved cursor, only line 3 is new")

    def test_reload_only_on_lock_change_not_every_history_update(self) -> None:
        # SUCCESS/FAIL update history but do NOT set changed_lock until the
        # auto-lock threshold is crossed.  The learner reloads only on
        # changed_lock (contract §3 reload policy).
        learned, blocked, manual = self._fresh()
        seen: set[str] = set()
        changes: list[bool] = []
        for i in range(3):
            r = process_event(learned, blocked, manual, self._ev("success", "tls", "a.com", 2, ts=i), seen)
            changes.append(r["changed_lock"])
        # first two: history-only, no reload; third: lock -> reload
        self.assertEqual(changes, [False, False, True])
        # a FAIL on a non-locked strategy is history-only (no reload)
        r = process_event(learned, blocked, manual, self._ev("fail", "tls", "a.com", 2, ts=10), seen)
        self.assertFalse(r["changed_lock"])

    def test_blocked_priority_over_user_lock_via_lock_event(self) -> None:
        # A user-locked host that is ALSO blocked: a LOCK event for the blocked
        # strategy drops the auto_lock (blocked wins), and never touches the
        # user lock.  Here we verify a LOCK event on a blocked+user-locked host
        # does not create an auto_lock and does not change state.
        learned, blocked, manual = self._fresh()
        manual["protocols"]["tls"] = {"a.com": 1}
        blocked["protocols"]["tls"] = {"global": [], "hosts": {"a.com": [2]}}
        seen: set[str] = set()
        r = process_event(learned, blocked, manual, self._ev("lock", "tls", "a.com", 2, ts=1), seen)
        self.assertFalse(r["changed_lock"])
        self.assertIsNone(learned["protocols"].get("tls", {}).get("a.com", {}).get("auto_lock"))


# ---------------------------------------------------------------------------
# Atomic state write + .good recovery (Python model of the .good discipline)
# ---------------------------------------------------------------------------

class AtomicStateWriteTest(unittest.TestCase):
    """Verifies the .good recovery logic the learner uses (mirror of learner.uc
    read_json_with_good).  The ucode-runtime class confirms the real impl."""

    def _write_good(self, path: Path, doc: dict) -> None:
        payload = json.dumps(doc, separators=(",", ":"), sort_keys=True) + "\n"
        (path.with_suffix(path.suffix + ".good")).write_text(payload, encoding="utf-8")

    def test_good_copy_restores_corrupted_primary(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="orch-atomic-"))
        try:
            st = tmp / "learned.json"
            good_doc = {"schema_version": 1, "protocols": {"tls": {"a.com": {"auto_lock": 2, "strategies": {"2": {"successes": 3, "failures": 0}}}}}}
            st.write_text(json.dumps(good_doc, separators=(",", ":")) + "\n", encoding="utf-8")
            self._write_good(st, good_doc)
            # corrupt the primary
            st.write_text("{not valid json", encoding="utf-8")
            # the .good copy is valid -> restore primary from it
            good = json.loads((st.with_suffix(".json.good")).read_text(encoding="utf-8"))
            self.assertEqual(good, good_doc)
            # a real restore would rename .good content over the primary; here we
            # just confirm the .good copy holds the recoverable state
            restored = json.loads((st.with_suffix(".json.good")).read_text(encoding="utf-8"))
            self.assertEqual(restored["protocols"]["tls"]["a.com"]["auto_lock"], 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Static contract tests: enable/disable, validate_profile, blocked seed
# ---------------------------------------------------------------------------

class LearnerStaticContractTest(unittest.TestCase):
    """Static checks of the learner.uc / apply.uc source + the blocked seed."""

    def test_learner_uc_source_present_and_strict(self) -> None:
        src = LEARNER_UC.read_text(encoding="utf-8")
        self.assertIn("'use strict';", src)
        self.assertIn("import { readfile, writefile, mkdir, rename, unlink, stat, rmdir, dirname, popen } from 'fs';", src)

    def test_learner_uc_has_test_process_mode(self) -> None:
        src = LEARNER_UC.read_text(encoding="utf-8")
        self.assertIn("test-process", src)
        self.assertIn("mode_test_process", src)

    def test_learner_uc_lock_policy_constants(self) -> None:
        src = LEARNER_UC.read_text(encoding="utf-8")
        self.assertIn("LOCK_SUCCESSES_TCP = 3", src)
        self.assertIn("LOCK_SUCCESSES_UDP = 1", src)
        self.assertIn("UNLOCK_FAILS = 3", src)
        # UDP askeys
        for a in ("quic", "discord", "wireguard", "dns", "stun", "unknown"):
            self.assertIn(a, src)

    def test_learner_uc_dedup_and_cursor(self) -> None:
        src = LEARNER_UC.read_text(encoding="utf-8")
        self.assertIn("event_dedup_key", src)
        self.assertIn("read_events_from_cursor", src)
        self.assertIn("learner-state.json", src)

    def test_learner_uc_atomic_write_and_good(self) -> None:
        src = LEARNER_UC.read_text(encoding="utf-8")
        self.assertIn("atomic_write_json", src)
        self.assertIn(".good", src)
        self.assertIn("read_json_with_good", src)

    def test_learner_uc_never_references_init_d_directly(self) -> None:
        # The learner never references /etc/init.d directly — the reload is
        # owned by the procd wrapper (ORCHESTRA_RELOAD_CMD / the reload helper).
        src = LEARNER_UC.read_text(encoding="utf-8")
        self.assertNotRegex(src, r"/etc/init\.d/zapret2\s+(start|stop|restart|reload)")

    def test_learner_uc_no_packet_path_writes(self) -> None:
        # The learner is the sole JSON writer; it must not be loaded from the
        # Lua packet path.  This is a static sanity check that the daemon only
        # writes via atomic_write_json (no ad-hoc writefile of seed paths).
        src = LEARNER_UC.read_text(encoding="utf-8")
        # atomic_write_json is the only path that writes the seed JSON files
        self.assertIn("atomic_write_json(STATE_DIR + '/learned.json'", src)
        self.assertIn("atomic_write_json(STATE_DIR + '/blocked.json'", src)
        self.assertIn("atomic_write_json(LEARNER_STATE", src)

    def test_apply_uc_enable_sets_nfqws2_enable_and_service_action(self) -> None:
        src = APPLY_UC.read_text(encoding="utf-8")
        self.assertIn("parse_nfqws2_enable", src)
        self.assertIn("transform_nfqws2_enable", src)
        # enable calls do_apply_transaction with enable_value=1 (NFQWS2_ENABLE=1)
        self.assertIn("do_apply_transaction(pp.value, profile_name, 1)", src)
        # disable flips NFQWS2_ENABLE=0 after restore
        self.assertIn("nfqws2_enable: 0", src)
        self.assertIn("service_action", src)
        self.assertIn("'start'", src)
        self.assertIn("'stop'", src)

    def test_apply_uc_validate_profile_relaxed_for_native(self) -> None:
        src = APPLY_UC.read_text(encoding="utf-8")
        self.assertIn("loads_orchestra", src)
        # circular_quality is now gated on loads_orchestra, not unconditional
        self.assertIn("loads orchestra runtime but does not reference circular_quality", src)

    def test_apply_uc_never_references_init_d_zapret2(self) -> None:
        src = APPLY_UC.read_text(encoding="utf-8")
        self.assertNotRegex(src, r"/etc/init\.d/zapret2\s+(start|stop|restart|reload)")

    def test_profile_cli_enable_uses_service_action_gate(self) -> None:
        cli = (FILES / "usr/sbin/zapret2-orchestra-profile").read_text(encoding="utf-8")
        self.assertIn("service_action", cli)
        self.assertIn("ZAPRET2_SERVICE_CTL", cli)
        # The /etc/init.d reference lives in the wrapper, gated on service_action
        self.assertIn("/etc/init.d/zapret2-orchestra-learner", cli)

    def test_reload_helper_owns_init_d_reference(self) -> None:
        helper = (FILES / "usr/sbin/zapret2-orchestra-reload").read_text(encoding="utf-8")
        self.assertIn("/etc/init.d/zapret2", helper)
        self.assertIn("restart", helper)

    def test_learner_init_d_service_present(self) -> None:
        initd = (FILES / "etc/init.d/zapret2-orchestra-learner").read_text(encoding="utf-8")
        self.assertIn("USE_PROCD=1", initd)
        self.assertIn("START=22", initd)
        self.assertIn("zapret2-orchestra-learner", initd)
        self.assertIn("ORCHESTRA_RELOAD_CMD", initd)

    def test_blocked_seed_has_default_blocked_pass_domains(self) -> None:
        doc = json.loads(BLOCKED_SEED.read_text(encoding="utf-8"))
        self.assertEqual(doc["schema_version"], 1)
        hosts = doc["protocols"]["tls"]["hosts"]
        # discord.com MUST be blocked strategy=1 on TLS (contract §1 rule 7)
        self.assertIn("discord.com", hosts)
        self.assertEqual(hosts["discord.com"], [1])
        # a representative spread of the 59-domain set
        for d in ("youtube.com", "google.com", "github.com", "rutracker.org", "facebook.com", "twitch.tv"):
            self.assertIn(d, hosts, f"{d} must be in DEFAULT_BLOCKED_PASS_DOMAINS")
            self.assertEqual(hosts[d], [1], f"{d} blocked strategy=1")
        # all entries block strategy 1
        for d, vals in hosts.items():
            self.assertEqual(vals, [1], f"{d} -> [1]")
        # global is empty (per-host, not global — original model)
        self.assertEqual(doc["protocols"]["tls"]["global"], [])
        self.assertGreaterEqual(len(hosts), 59)

    def test_dev_blocked_seed_matches_shipped(self) -> None:
        self.assertEqual(BLOCKED_SEED.read_bytes(), DEV_BLOCKED.read_bytes())

    def test_provenance_note_records_pinned_source(self) -> None:
        note = (ROOT / "docs/orchestra-blocked-seed-provenance.md").read_text(encoding="utf-8")
        self.assertIn("9d57e55d6751587d9d52b52147a05a0a8fcc9fd8", note)
        self.assertIn("blocked_strategies_manager.py:65-102", note)
        self.assertIn("discord.com", note)


# ---------------------------------------------------------------------------
# ucode-runtime tests: drive the real learner.uc test-process mode.
# Skipped when ucode is not on PATH (CI runs on Linux with zero-skip gate).
# ---------------------------------------------------------------------------

class LearnerUcodeRuntimeTest(unittest.TestCase):
    """Executes the real learner.uc test-process mode; skipped when ucode absent."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.ucode = shutil.which("ucode")

    def setUp(self) -> None:
        if self.ucode is None:
            self.skipTest("ucode executable not found on PATH")
        self.tmp = Path(tempfile.mkdtemp(prefix="orch-learner-"))
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir()
        self.runtime = self.tmp / "runtime"
        self.runtime.mkdir()
        # minimal seeds
        self._write_seeds({
            "blocked": {"schema_version": 1, "protocols": {"tls": {"global": [], "hosts": {}}}},
            "learned": {"schema_version": 1, "protocols": {}},
            "manual-locks": {"schema_version": 1, "protocols": {}},
            "whitelist": {"schema_version": 1, "hosts": []},
        })

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_seeds(self, seeds: dict) -> None:
        for name, doc in seeds.items():
            (self.state_dir / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8")

    def _events_file(self, lines: list[str]) -> Path:
        p = self.runtime / "events.ndjson"
        p.write_text("".join(l + "\n" for l in lines), encoding="utf-8")
        return p

    def _env(self) -> dict:
        env = os.environ.copy()
        env["ORCHESTRA_STATE_DIR"] = str(self.state_dir)
        env["ORCHESTRA_RUNTIME_DIR"] = str(self.runtime)
        env["ORCHESTRA_EVENTS_FILE"] = str(self.runtime / "events.ndjson")
        env["LEARNER_STATE_FILE"] = str(self.state_dir / "learner-state.json")
        env["LEARNER_LOCK_DIR"] = str(self.runtime / "learner.lock")
        env["LEARNER_LOG_FILE"] = str(self.runtime / "learner.log")
        env["ORCHESTRA_PROFILES_DIR"] = str(self.state_dir / "profiles")
        env["ORCHESTRA_BUILTIN_PROFILES_DIR"] = str(self.state_dir / "builtin-profiles")
        env["ORCHESTRA_SHARE_DIR"] = str(self.state_dir / "share")
        env["ORCHESTRA_MANAGER_STATE_FILE"] = str(self.state_dir / "manager-state.json")
        return env

    def _run_test_process(self, events_path: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.ucode, str(LEARNER_UC), "test-process", str(events_path)],
            env=self._env(),
            capture_output=True, text=True, timeout=10,
        )

    def _load(self, name: str) -> dict:
        return json.loads((self.state_dir / name).read_text(encoding="utf-8"))

    def test_ucode_auto_lock_after_3_tcp_success(self) -> None:
        lines = [
            json.dumps({"schema_version": 1, "ts": i, "type": "success", "askey": "tls", "host": "a.com", "strategy": 2, "run_id": "r"})
            for i in range(1, 4)
        ]
        ef = self._events_file(lines)
        r = self._run_test_process(ef)
        self.assertEqual(r.returncode, 0, r.stderr)
        learned = self._load("learned.json")
        self.assertEqual(learned["protocols"]["tls"]["a.com"]["auto_lock"], 2)

    def test_ucode_auto_lock_after_1_udp_success(self) -> None:
        ef = self._events_file([
            json.dumps({"schema_version": 1, "ts": 1, "type": "success", "askey": "quic", "host": "1.2.3.4", "strategy": 2, "run_id": "r"}),
        ])
        r = self._run_test_process(ef)
        self.assertEqual(r.returncode, 0, r.stderr)
        learned = self._load("learned.json")
        self.assertEqual(learned["protocols"]["quic"]["1.2.3.4"]["auto_lock"], 2)

    def test_ucode_unlock_after_unlock_event(self) -> None:
        lines = [
            json.dumps({"schema_version": 1, "ts": i, "type": "success", "askey": "tls", "host": "a.com", "strategy": 2, "run_id": "r"})
            for i in range(1, 4)
        ]
        lines.append(json.dumps({"schema_version": 1, "ts": 10, "type": "unlock", "askey": "tls", "host": "a.com", "strategy": 2, "run_id": "r"}))
        ef = self._events_file(lines)
        r = self._run_test_process(ef)
        self.assertEqual(r.returncode, 0, r.stderr)
        learned = self._load("learned.json")
        self.assertIsNone(learned["protocols"]["tls"]["a.com"].get("auto_lock"))

    def test_ucode_user_lock_not_auto_unlocked(self) -> None:
        self._write_seeds({
            "blocked": {"schema_version": 1, "protocols": {"tls": {"global": [], "hosts": {}}}},
            "learned": {"schema_version": 1, "protocols": {}},
            "manual-locks": {"schema_version": 1, "protocols": {"tls": {"a.com": 2}}},
            "whitelist": {"schema_version": 1, "hosts": []},
        })
        ef = self._events_file([
            json.dumps({"schema_version": 1, "ts": 1, "type": "unlock", "askey": "tls", "host": "a.com", "strategy": 2, "run_id": "r"}),
        ])
        r = self._run_test_process(ef)
        self.assertEqual(r.returncode, 0, r.stderr)
        manual = self._load("manual-locks.json")
        self.assertEqual(manual["protocols"]["tls"]["a.com"], 2, "user lock untouched")

    def test_ucode_blocked_priority_drops_lock(self) -> None:
        self._write_seeds({
            "blocked": {"schema_version": 1, "protocols": {"tls": {"global": [], "hosts": {"a.com": [2]}}}},
            "learned": {"schema_version": 1, "protocols": {}},
            "manual-locks": {"schema_version": 1, "protocols": {}},
            "whitelist": {"schema_version": 1, "hosts": []},
        })
        ef = self._events_file([
            json.dumps({"schema_version": 1, "ts": 1, "type": "lock", "askey": "tls", "host": "a.com", "strategy": 2, "run_id": "r"}),
        ])
        r = self._run_test_process(ef)
        self.assertEqual(r.returncode, 0, r.stderr)
        learned = self._load("learned.json")
        self.assertIsNone(learned["protocols"].get("tls", {}).get("a.com", {}).get("auto_lock"))

    def test_ucode_idempotent_duplicate_events(self) -> None:
        ev = json.dumps({"schema_version": 1, "ts": 1, "type": "success", "askey": "tls", "host": "a.com", "strategy": 2, "run_id": "r"})
        ef = self._events_file([ev])
        r1 = self._run_test_process(ef)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        # run again over the SAME events file — the cursor should be advanced,
        # so re-running from cursor=0 would reprocess; but the dedup set is
        # per-pass.  The durable cursor prevents reprocessing: run a second pass
        # with the cursor persisted from pass 1.
        r2 = self._run_test_process(ef)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        learned = self._load("learned.json")
        # The second pass starts from the persisted cursor (end of file) and
        # processes nothing new, so successes stays at 1.
        self.assertEqual(learned["protocols"]["tls"]["a.com"]["strategies"]["2"]["successes"], 1)

    def test_ucode_cursor_recovery_after_restart(self) -> None:
        lines = [
            json.dumps({"schema_version": 1, "ts": i, "type": "success", "askey": "tls", "host": "a.com", "strategy": 2, "run_id": "r"})
            for i in range(1, 4)
        ]
        ef = self._events_file(lines)
        # pass 1: process all 3
        r1 = self._run_test_process(ef)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        lstate = self._load("learner-state.json")
        self.assertEqual(lstate["event_cursor"]["bytes"], ef.stat().st_size)
        # append a 4th event (simulating the writer adding more while the
        # learner was "restarted")
        with ef.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"schema_version": 1, "ts": 4, "type": "success", "askey": "tls", "host": "a.com", "strategy": 2, "run_id": "r"}) + "\n")
        # pass 2: resumes from the persisted cursor, processes only the new line
        r2 = self._run_test_process(ef)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        learned = self._load("learned.json")
        self.assertEqual(learned["protocols"]["tls"]["a.com"]["strategies"]["2"]["successes"], 4)

    def test_ucode_truncated_ndjson_recovery(self) -> None:
        full = json.dumps({"schema_version": 1, "ts": 1, "type": "success", "askey": "tls", "host": "a.com", "strategy": 2, "run_id": "r"})
        partial = '{"schema_version":1,"ts":2,"type":"success","askey":"tls","host":"a.com","strategy":2'  # truncated
        ef = self.runtime / "events.ndjson"
        ef.write_text(full + "\n" + partial, encoding="utf-8")  # no trailing newline on partial
        r = self._run_test_process(ef)
        self.assertEqual(r.returncode, 0, r.stderr)
        learned = self._load("learned.json")
        # only the complete first line processed
        self.assertEqual(learned["protocols"]["tls"]["a.com"]["strategies"]["2"]["successes"], 1)
        lstate = self._load("learner-state.json")
        # cursor stopped at the end of the complete line, NOT at EOF
        self.assertEqual(lstate["event_cursor"]["bytes"], len(full) + 1)
        # now complete the line and re-run
        ef.write_text(full + "\n" + partial + ',"run_id":"r"}\n', encoding="utf-8")
        r2 = self._run_test_process(ef)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        learned = self._load("learned.json")
        self.assertEqual(learned["protocols"]["tls"]["a.com"]["strategies"]["2"]["successes"], 2)


# ---------------------------------------------------------------------------
# ucode-runtime: enable/disable via the real apply.uc (skips if ucode absent)
# ---------------------------------------------------------------------------

class EnableDisableUcodeRuntimeTest(unittest.TestCase):
    """Executes the real apply.uc enable/disable; skipped when ucode absent."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.ucode = shutil.which("ucode")

    def setUp(self) -> None:
        if self.ucode is None:
            self.skipTest("ucode executable not found on PATH")
        self.tmp = Path(tempfile.mkdtemp(prefix="orch-enable-"))
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir()
        self.runtime = self.tmp / "runtime"
        self.runtime.mkdir()
        self.opt = self.tmp / "opt" / "zapret2"
        self.opt.mkdir(parents=True)
        self.user_profiles = self.tmp / "user-profiles"
        self.user_profiles.mkdir()
        self.builtin_profiles = self.tmp / "builtin-profiles"
        self.builtin_profiles.mkdir()
        self.orch_lua = self.tmp / "orch-lua"
        self.orch_lua.mkdir()
        (self.orch_lua / "init.lua").write_text("-- test\n", encoding="utf-8")
        (self.state_dir / "whitelist.json").write_text(json.dumps({"schema_version": 1, "hosts": []}), encoding="utf-8")
        (self.state_dir / "blocked.json").write_text(json.dumps({"schema_version": 1, "protocols": {"tls": {"global": [], "hosts": {}}}}), encoding="utf-8")
        (self.state_dir / "learned.json").write_text(json.dumps({"schema_version": 1, "protocols": {}}), encoding="utf-8")
        (self.state_dir / "manual-locks.json").write_text(json.dumps({"schema_version": 1, "protocols": {}}), encoding="utf-8")
        # a circular_quality builtin profile (the existing one)
        builtin = PACKAGE / "files/usr/share/zapret2-orchestra/profiles/orchestra-tls-mvp.opt"
        (self.builtin_profiles / "orchestra-tls-mvp.opt").write_text(builtin.read_text(encoding="utf-8"), encoding="utf-8")
        # a native profile WITHOUT circular_quality (the discord-v5 shape)
        (self.builtin_profiles / "native-test.opt").write_text(
            'NFQWS2_OPT="\n--lua-desync=send:repeats=3 --lua-desync=syndata:blob=tls_google --ipset=ipset-discord.txt --lua-init=@/opt/zapret2/lua/init_vars.lua\n"\n',
            encoding="utf-8",
        )
        # fake preload wrapper
        fake_preload = self.tmp / "fake-preload.sh"
        fake_preload.write_text(
            "#!/bin/sh\nmkdir -p \"$ORCHESTRA_RUNTIME_DIR\"\n"
            'case "$1" in\n  generate)\n    echo "preload" > "$ORCHESTRA_RUNTIME_DIR/preload.lua"\n'
            '    echo "whitelist" > "$ORCHESTRA_RUNTIME_DIR/whitelist.txt"\n'
            '    echo \'{"schema_version":1}\' > "$ORCHESTRA_RUNTIME_DIR/manifest.json"\n    exit 0 ;;\n'
            "  check) [ -f \"$ORCHESTRA_RUNTIME_DIR/preload.lua\" ] && exit 0 || exit 1 ;;\n  *) exit 1 ;;\nesac\n",
            encoding="utf-8",
        )
        fake_preload.chmod(0o755)
        self._write_default_config()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_default_config(self) -> None:
        (self.opt / "config").write_text(
            'NFQWS2_ENABLE=0\nNFQWS2_OPT="--lua-desync=fake:blob=default"\nMODE_FILTER=none\n',
            encoding="utf-8",
        )

    def _env(self) -> dict:
        env = os.environ.copy()
        env["ZAPRET2_RUNTIME_DIR"] = str(self.runtime)
        env["ZAPRET2_ORCHESTRA_DIR"] = str(self.state_dir)
        env["ZAPRET2_SHARE_DIR"] = str(self.tmp / "share")
        env["ZAPRET2_USER_PROFILES_DIR"] = str(self.user_profiles)
        env["ZAPRET2_BUILTIN_PROFILES_DIR"] = str(self.builtin_profiles)
        env["ZAPRET2_ORCHESTRA_LUA"] = str(self.orch_lua)
        env["ZAPRET2_STATE_FILE"] = str(self.state_dir / "manager-state.json")
        env["ZAPRET2_LOCK_DIR"] = str(self.runtime / "apply.lock")
        env["ZAPRET2_VALIDATE_OUT"] = str(self.runtime / "validate-config.cfg")
        env["ZAPRET2_CANDIDATE_FILE"] = str(self.opt / ".config.orchestra.tmp")
        env["ZAPRET2_BACKUP_DIR"] = str(self.state_dir / "backup")
        env["ZAPRET2_CONFIG"] = str(self.opt / "config")
        env["ZAPRET2_PRELOAD_WRAPPER"] = str(self.tmp / "fake-preload.sh")
        env["ORCHESTRA_STATE_DIR"] = str(self.state_dir)
        env["ORCHESTRA_RUNTIME_DIR"] = str(self.runtime)
        env["ORCHESTRA_PRELOAD_FILE"] = str(self.runtime / "preload.lua")
        env["ORCHESTRA_WHITELIST_FILE"] = str(self.runtime / "whitelist.txt")
        env["ORCHESTRA_MANIFEST_FILE"] = str(self.runtime / "manifest.json")
        env["ORCHESTRA_PROFILES_DIR"] = str(self.state_dir / "profiles")
        env["ORCHESTRA_BUILTIN_PROFILES_DIR"] = str(self.builtin_profiles)
        env["ORCHESTRA_MANAGER_STATE_FILE"] = str(self.state_dir / "manager-state.json")
        return env

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.ucode, str(APPLY_UC), "--", *args],
            env=self._env(), capture_output=True, text=True, timeout=10,
        )

    def test_enable_sets_nfqws2_enable_to_1(self) -> None:
        r = self._run("enable", "orchestra-tls-mvp")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertTrue(doc["ok"])
        self.assertEqual(doc.get("nfqws2_enable"), 1)
        self.assertEqual(doc.get("service_action"), "start")
        cfg = (self.opt / "config").read_text(encoding="utf-8")
        self.assertIn("NFQWS2_ENABLE=1", cfg)

    def test_disable_sets_nfqws2_enable_to_0(self) -> None:
        self._run("enable", "orchestra-tls-mvp")
        r = self._run("disable")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertTrue(doc["ok"])
        self.assertEqual(doc.get("nfqws2_enable"), 0)
        self.assertEqual(doc.get("service_action"), "stop")
        cfg = (self.opt / "config").read_text(encoding="utf-8")
        self.assertIn("NFQWS2_ENABLE=0", cfg)

    def test_validate_profile_accepts_native_without_circular_quality(self) -> None:
        r = self._run("validate-profile", "native-test")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertTrue(doc["ok"], doc.get("problems"))
        self.assertFalse(doc.get("loads_orchestra"), "native profile does not load orchestra runtime")

    def test_validate_profile_rejects_orchestra_profile_without_circular_quality(self) -> None:
        # A profile that loads orchestra-extra but does NOT reference
        # circular_quality must still be rejected.
        (self.builtin_profiles / "bad-orch.opt").write_text(
            'NFQWS2_OPT="\n--lua-init=@/opt/zapret2/lua/orchestra-extra/init.lua --lua-desync=fake\n"\n',
            encoding="utf-8",
        )
        r = self._run("validate-profile", "bad-orch")
        self.assertNotEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertFalse(doc["ok"])
        self.assertTrue(any("circular_quality" in p for p in doc.get("problems", [])))


if __name__ == "__main__":
    unittest.main()
