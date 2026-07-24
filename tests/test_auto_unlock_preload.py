"""Auto-unlock + preload-regen tests for the r7 closed-loop learner.

Covers the two CI failures Agent C fixed in learner.uc:

  * FAILURE 5 (auto-unlock): after 3 FAIL events on an auto-locked strategy
    ``auto_lock`` is removed (the key is truly absent, not ``null``) and
    ``changed_lock`` is True.  A user-locked host is protected — 3 FAILs do
    NOT remove its lock.
  * FAILURE 6 (preload debounce): when a lock/blocked change occurs, the
    preload generation counter (``learner-state.json::last_preload_gen``)
    advances.  ``test-process`` mode flushes the regen immediately before
    exit (no debounce); ``process-once`` likewise bumps the gen once per
    pass that changed a lock.

Two layers of tests:

  1. Python reference (run everywhere, no ucode required): exercises the
     ``process_event`` mirror imported from ``test_orchestra_learner`` plus a
     small model of the ``test-process`` gen-bump-on-changed_lock flush.
  2. ucode-runtime (skipped locally when ``ucode`` is absent; CI runs them on
     Linux): drives the real ``learner.uc`` ``test-process`` / ``process-once``
     modes and asserts on the on-disk ``learned.json`` / ``learner-state.json``.

Contract ref: contracts §3 "Lock/unlock policy" — auto-UNLOCK after
``unlock_fails=3`` cumulative FAIL on an auto-locked strategy; NEVER
auto-unlock a user-locked strategy.
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

# Reuse the frozen Python reference of process_event + its constants/helpers.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_orchestra_learner as L  # noqa: E402


# ---------------------------------------------------------------------------
# Python reference: auto-unlock-on-FAIL semantics (runs everywhere)
# ---------------------------------------------------------------------------

def _ev(etype: str, host: str, strategy: int, ts: int, askey: str = "tls",
        run_id: str = "run1") -> dict:
    return {"schema_version": 1, "ts": ts, "type": etype, "askey": askey,
            "host": host, "strategy": strategy, "run_id": run_id}


def _fresh() -> tuple[dict, dict, dict]:
    return ({"schema_version": 1, "protocols": {}},
            {"schema_version": 1, "protocols": {}},
            {"schema_version": 1, "protocols": {}})


def _lock(learned: dict, blocked: dict, manual: dict, host: str, strategy: int,
          seen: set[str]) -> None:
    """Drive 3 TCP SUCCESS events to establish auto_lock=strategy on host."""
    for i in range(3):
        L.process_event(learned, blocked, manual, _ev("success", host, strategy, ts=i), seen)


class AutoUnlockPythonReferenceTest(unittest.TestCase):
    """Exercises the process_event FAIL-based auto-unlock via the Python mirror."""

    def test_3_fail_on_auto_locked_removes_lock_and_signals_change(self) -> None:
        # 3 SUCCESS lock strategy 2; then 3 FAIL on the locked strategy
        # auto-unlock: auto_lock removed (key absent) and changed_lock=True
        # on the 3rd FAIL.
        learned, blocked, manual = _fresh()
        seen: set[str] = set()
        _lock(learned, blocked, manual, "a.com", 2, seen)
        self.assertEqual(learned["protocols"]["tls"]["a.com"]["auto_lock"], 2)
        # First two FAILs: history only (failures < UNLOCK_FAILS=3).
        r1 = L.process_event(learned, blocked, manual, _ev("fail", "a.com", 2, ts=10), seen)
        r2 = L.process_event(learned, blocked, manual, _ev("fail", "a.com", 2, ts=11), seen)
        self.assertFalse(r1["changed_lock"], "1 FAIL does not unlock")
        self.assertFalse(r2["changed_lock"], "2 FAILs do not unlock")
        self.assertEqual(learned["protocols"]["tls"]["a.com"]["auto_lock"], 2,
                         "auto_lock persists until UNLOCK_FAILS=3")
        # Third FAIL: auto-unlock fires.
        r3 = L.process_event(learned, blocked, manual, _ev("fail", "a.com", 2, ts=12), seen)
        self.assertTrue(r3["changed_lock"], "3rd FAIL sets changed_lock=True")
        rec = learned["protocols"]["tls"]["a.com"]
        self.assertNotIn("auto_lock", rec,
                         "auto_lock key must be truly absent after 3 FAILs")
        # history is retained (the unlock clears the lock, not the history).
        self.assertEqual(rec["strategies"]["2"]["failures"], 3)

    def test_fail_on_different_strategy_does_not_unlock_locked_one(self) -> None:
        # FAILs on a strategy OTHER than the auto-locked one must not unlock
        # the locked strategy (the guard requires auto_lock == strategy).
        learned, blocked, manual = _fresh()
        seen: set[str] = set()
        _lock(learned, blocked, manual, "a.com", 2, seen)
        # 3 FAILs on strategy 5 (not the locked strategy 2).
        for i in range(3):
            L.process_event(learned, blocked, manual, _ev("fail", "a.com", 5, ts=10 + i), seen)
        self.assertEqual(learned["protocols"]["tls"]["a.com"]["auto_lock"], 2,
                         "FAILs on a non-locked strategy do not unlock the lock")

    def test_3_fail_on_user_locked_does_not_remove_lock(self) -> None:
        # A user-locked host is protected: 3 FAILs do NOT auto-unlock.  Seed
        # both an auto_lock AND a user lock on the same host (the user lock
        # is the guard); the FAIL-based auto-unlock must skip it.
        learned, blocked, manual = _fresh()
        manual["protocols"]["tls"] = {"a.com": 2}
        # Seed a learned auto_lock=2 directly (a pre-existing auto-lock that
        # a user lock was later placed over).
        learned["protocols"]["tls"] = {"a.com": {"auto_lock": 2,
                                                 "strategies": {"2": {"successes": 3, "failures": 0}}}}
        seen: set[str] = set()
        for i in range(3):
            r = L.process_event(learned, blocked, manual, _ev("fail", "a.com", 2, ts=10 + i), seen)
            self.assertFalse(r["changed_lock"], "user-locked host is never auto-unlocked")
        # auto_lock is untouched (the user-lock guard held).
        self.assertEqual(learned["protocols"]["tls"]["a.com"]["auto_lock"], 2,
                         "user lock protects the auto_lock from FAIL-based unlock")
        # the user lock itself is untouched in manual-locks.json
        self.assertEqual(manual["protocols"]["tls"]["a.com"], 2)
        # failures were still recorded (history updates regardless of lock).
        self.assertEqual(learned["protocols"]["tls"]["a.com"]["strategies"]["2"]["failures"], 3)


# ---------------------------------------------------------------------------
# Python reference model of the test-process "flush" gen bump (runs everywhere)
# ---------------------------------------------------------------------------

def _flush_gen(learned: dict, blocked: dict, manual: dict, events: list[dict],
               lstate: dict) -> bool:
    """Mirror of learner.uc mode_test_process final flush.

    Processes ``events`` through process_event, then — iff any event set
    changed_lock OR changed_blocked — bumps ``lstate['last_preload_gen']``
    exactly once (the test-process flush: no debounce, one regen per pass).
    Returns whether a flush (gen bump) occurred.
    """
    seen: set[str] = set()
    changed = False
    for ev in events:
        r = L.process_event(learned, blocked, manual, ev, seen)
        if r["changed_lock"] or r["changed_blocked"]:
            changed = True
    if changed:
        lstate["last_preload_gen"] = int(lstate.get("last_preload_gen", 0)) + 1
    return changed


class PreloadFlushPythonReferenceTest(unittest.TestCase):
    """Models the test-process gen-bump-on-changed_lock flush in Python."""

    def test_auto_unlock_advances_preload_gen(self) -> None:
        # After an auto-unlock (3 FAILs on a locked strategy), the preload
        # generation advances (the regen trigger fires on changed_lock).
        learned, blocked, manual = _fresh()
        lstate = {"last_preload_gen": 0}
        _lock(learned, blocked, manual, "a.com", 2, set())
        self.assertEqual(lstate["last_preload_gen"], 0, "no flush yet")
        # The lock-establish pass would have bumped gen once; model it.
        lock_events = [_ev("success", "a.com", 2, ts=i) for i in range(3)]
        learned, blocked, manual = _fresh()
        lstate = {"last_preload_gen": 0}
        _flush_gen(learned, blocked, manual, lock_events, lstate)
        self.assertEqual(lstate["last_preload_gen"], 1, "lock change -> gen+1")
        # Now the 3-FAIL auto-unlock pass.
        fail_events = [_ev("fail", "a.com", 2, ts=10 + i) for i in range(3)]
        flushed = _flush_gen(learned, blocked, manual, fail_events, lstate)
        self.assertTrue(flushed, "auto-unlock flushes the preload regen")
        self.assertEqual(lstate["last_preload_gen"], 2,
                         "auto-unlock advances the gen a second time")

    def test_no_lock_change_no_gen_bump(self) -> None:
        # A pass with only history-only events (SUCCESS below the lock
        # threshold, or FAIL on a non-locked host) does NOT bump the gen.
        learned, blocked, manual = _fresh()
        lstate = {"last_preload_gen": 0}
        # 2 successes on strategy 2: no lock (threshold=3), no gen bump.
        events = [_ev("success", "a.com", 2, ts=i) for i in range(2)]
        flushed = _flush_gen(learned, blocked, manual, events, lstate)
        self.assertFalse(flushed, "no lock change -> no flush")
        self.assertEqual(lstate["last_preload_gen"], 0,
                         "history-only pass leaves gen unchanged")

    def test_burst_lock_change_bumps_gen_at_most_once_per_pass(self) -> None:
        # A burst of events in ONE pass that changes the lock once (3rd
        # success) bumps the gen exactly once (debounce coalesces within a
        # pass).  Mirrors test_preload_debounce_on_burst at the model level.
        learned, blocked, manual = _fresh()
        lstate = {"last_preload_gen": 0}
        events = [_ev("success", "a.com", 2, ts=1753290001 + k) for k in range(6)]
        _flush_gen(learned, blocked, manual, events, lstate)
        self.assertEqual(lstate["last_preload_gen"], 1,
                         "one lock change in a burst -> gen advances once")


# ---------------------------------------------------------------------------
# ucode-runtime: drive the real learner.uc (skipped when ucode absent)
# ---------------------------------------------------------------------------

class AutoUnlockPreloadUcodeRuntimeTest(unittest.TestCase):
    """Executes the real learner.uc; skipped when ucode is not on PATH."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.ucode = shutil.which("ucode")

    def setUp(self) -> None:
        if self.ucode is None:
            self.skipTest("ucode executable not found on PATH")
        self.tmp = Path(tempfile.mkdtemp(prefix="orch-autounlock-"))
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir()
        self.runtime = self.tmp / "runtime"
        self.runtime.mkdir()
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
        p.write_text("".join(ln + "\n" for ln in lines), encoding="utf-8")
        return p

    def _env(self) -> dict:
        env = os.environ.copy()
        env["ORCHESTRA_STATE_DIR"] = str(self.state_dir)
        env["ORCHESTRA_RUNTIME_DIR"] = str(self.runtime)
        env["ORCHESTRA_EVENTS_FILE"] = str(self.runtime / "events.ndjson")
        env["LEARNER_STATE_FILE"] = str(self.state_dir / "learner-state.json")
        env["LEARNER_LOCK_DIR"] = str(self.runtime / "learner.lock")
        env["LEARNER_LOG_FILE"] = str(self.runtime / "learner.log")
        return env

    def _run(self, mode: str, events_path: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.ucode, str(LEARNER_UC), mode, str(events_path)],
            env=self._env(), capture_output=True, text=True, timeout=10,
        )

    def _load(self, name: str) -> dict:
        return json.loads((self.state_dir / name).read_text(encoding="utf-8"))

    def _succ(self, host: str, strategy: int, ts: int) -> str:
        return json.dumps({"schema_version": 1, "ts": ts, "type": "success",
                           "askey": "tls", "host": host, "strategy": strategy, "run_id": "r"})

    def _fail(self, host: str, strategy: int, ts: int) -> str:
        return json.dumps({"schema_version": 1, "ts": ts, "type": "fail",
                           "askey": "tls", "host": host, "strategy": strategy, "run_id": "r"})

    def test_ucode_3_fail_removes_auto_lock(self) -> None:
        # 3 SUCCESS (lock) then 3 FAIL (auto-unlock) via test-process.
        ef = self._events_file([self._succ("a.com", 2, ts=i) for i in range(1, 4)]
                               + [self._fail("a.com", 2, ts=i) for i in range(10, 13)])
        r = self._run("test-process", ef)
        self.assertEqual(r.returncode, 0, r.stderr)
        learned = self._load("learned.json")
        rec = learned.get("protocols", {}).get("tls", {}).get("a.com", {})
        self.assertNotIn("auto_lock", rec,
                         f"auto_lock must be absent after 3 FAILs, got {rec}")
        # history retained
        self.assertEqual(rec["strategies"]["2"]["failures"], 3)
        self.assertEqual(rec["strategies"]["2"]["successes"], 3)

    def test_ucode_3_fail_on_user_locked_keeps_lock(self) -> None:
        # Seed an auto_lock AND a user lock; 3 FAILs must not remove the
        # auto_lock (user-lock guard).
        learned_doc = {"schema_version": 1, "protocols": {"tls": {
            "a.com": {"auto_lock": 2,
                      "strategies": {"2": {"successes": 3, "failures": 0}}}}}}
        self._write_seeds({
            "blocked": {"schema_version": 1, "protocols": {"tls": {"global": [], "hosts": {}}}},
            "learned": learned_doc,
            "manual-locks": {"schema_version": 1, "protocols": {"tls": {"a.com": 2}}},
            "whitelist": {"schema_version": 1, "hosts": []},
        })
        ef = self._events_file([self._fail("a.com", 2, ts=i) for i in range(10, 13)])
        r = self._run("test-process", ef)
        self.assertEqual(r.returncode, 0, r.stderr)
        learned = self._load("learned.json")
        self.assertEqual(learned["protocols"]["tls"]["a.com"]["auto_lock"], 2,
                         "user-locked host: auto_lock NOT removed by 3 FAILs")
        manual = self._load("manual-locks.json")
        self.assertEqual(manual["protocols"]["tls"]["a.com"], 2,
                         "user lock untouched")

    def test_ucode_auto_unlock_advances_preload_gen(self) -> None:
        # After an auto-unlock, last_preload_gen advances (the regen
        # trigger fires on changed_lock).  Two test-process passes: lock,
        # then auto-unlock.  The gen must end >= 2 (one per changed pass).
        # Pass 1: 3 SUCCESS -> lock (changed_lock -> gen bump).
        ef1 = self._events_file([self._succ("a.com", 2, ts=i) for i in range(1, 4)])
        r1 = self._run("test-process", ef1)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        gen_after_lock = self._load("learner-state.json").get("last_preload_gen", 0)
        self.assertGreaterEqual(gen_after_lock, 1,
                                f"lock change must bump gen, got {gen_after_lock}")
        # Pass 2: 3 FAIL on the locked strategy -> auto-unlock (changed_lock
        # -> gen bump again).
        ef2 = self._events_file([self._fail("a.com", 2, ts=i) for i in range(10, 13)])
        r2 = self._run("test-process", ef2)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        gen_after_unlock = self._load("learner-state.json").get("last_preload_gen", 0)
        self.assertGreaterEqual(gen_after_unlock, gen_after_lock + 1,
                                f"auto-unlock must advance gen past {gen_after_lock}, "
                                f"got {gen_after_unlock}")
        # And the lock is gone.
        learned = self._load("learned.json")
        self.assertNotIn("auto_lock",
                         learned["protocols"]["tls"]["a.com"],
                         "auto_lock removed after the unlock pass")

    def test_ucode_test_process_flushes_preload_gen_on_lock_change(self) -> None:
        # test-process flushes pending preload regen before exit: a single
        # pass that changes a lock bumps last_preload_gen (no debounce in
        # test-process).  Contrast: a pass with NO lock change does not bump.
        ef_lock = self._events_file([self._succ("a.com", 2, ts=i) for i in range(1, 4)])
        r = self._run("test-process", ef_lock)
        self.assertEqual(r.returncode, 0, r.stderr)
        gen = self._load("learner-state.json").get("last_preload_gen", 0)
        self.assertGreaterEqual(gen, 1,
                                f"test-process must flush a regen on lock change, gen={gen}")

    def test_ucode_test_process_no_gen_bump_without_lock_change(self) -> None:
        # A test-process pass with only history-only events (2 successes,
        # below the lock threshold) must NOT bump the gen.
        ef = self._events_file([self._succ("a.com", 2, ts=i) for i in range(1, 3)])
        r = self._run("test-process", ef)
        self.assertEqual(r.returncode, 0, r.stderr)
        gen = self._load("learner-state.json").get("last_preload_gen", 0)
        self.assertEqual(gen, 0,
                         f"no lock change -> no gen bump, got {gen}")

    def test_ucode_process_once_advances_preload_gen_on_burst(self) -> None:
        # The CI test_preload_debounce_on_burst path: process-once over a
        # burst of 6 successes (one lock change at the 3rd) bumps the gen
        # at least once.
        ef = self._events_file([self._succ("a.com", 2, ts=1753290001 + k) for k in range(6)])
        r = self._run("process-once", ef)
        self.assertEqual(r.returncode, 0, r.stderr)
        gen = self._load("learner-state.json").get("last_preload_gen", 0)
        self.assertIsInstance(gen, int)
        self.assertGreaterEqual(gen, 1,
                                f"process-once must bump gen on a lock-changing burst, gen={gen}")


if __name__ == "__main__":
    unittest.main()
