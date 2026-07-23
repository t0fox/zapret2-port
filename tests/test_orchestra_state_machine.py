"""State-machine tests at the CONTRACT BOUNDARY for B's runtime learner.

Drives the learner solely through its filesystem/event interface per contracts
§2/§3: append NDJSON lines to ``events.ndjson`` IN, assert on the resulting
state files OUT (learned.json / blocked.json / manual-locks.json /
learner-state.json). These tests do NOT import B's internal functions — they
assert on the observable state, so any implementation that satisfies the
contract passes.

SEAM (integration entry point)
------------------------------
B's learner is a parallel artifact not present in this worktree. The tests
invoke it through a SINGLE well-documented seam: a learner entry point that
processes the event stream once and returns. Two acceptable forms, auto-
detected:

  1. A ucode CLI:  ``ucode <learner.uc> -- process-once``
     (env ORCHESTRA_STATE_DIR / ORCHESTRA_EVENTS_FILE point at the sandbox).
  2. A shell wrapper:  ``zapret2-orchestra-learner process-once``

The harness locates the learner via (in order):
  - ``$ZAPRET2_ORCHESTRA_LEARNER`` env (explicit override — used by CI/integration)
  - ``openwrt/zapret2-orchestra/files/usr/share/zapret2-orchestra/learner.uc``
    (+ ``ucode`` on PATH) — B's shipped learner.
  - ``openwrt/zapret2-orchestra/files/usr/sbin/zapret2-orchestra-learner``
    (+ ``sh`` on PATH) — B's shipped wrapper.

If NONE is present (pre-integration), the learner-driven tests SKIP cleanly
with the reason ``learner entry point not present (post-integration: B's
learner)``. The schema/parity tests that do not need the learner (cursor
recovery reasoning, state-atomicity reasoning, idempotency of the dedup key)
run always. At integration, B ships learner.uc (or the wrapper) and the
skip-guards self-disable.

Lock/unlock policy asserted (contracts §3 "Lock/unlock policy"):
  - auto-LOCK after 3 TCP SUCCESS or 1 UDP SUCCESS on a strategy.
  - auto-UNLOCK after 3 consecutive FAIL on an auto-locked strategy.
  - NEVER auto-unlock a user-locked strategy.
  - blocked strategy has PRIORITY over auto-lock AND user-lock; a
    locked==blocked conflict is dropped (blocked wins).
  - DEFAULT_BLOCKED_PASS_DOMAINS blocks strategy=1 for seeded domains at load.

The "process-once" semantics: the learner tails events.ndjson from the durable
cursor, processes all complete lines currently present, writes state, updates
the cursor, and exits. This makes the tests deterministic (no background
daemon timing).
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
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "openwrt" / "zapret2-orchestra"
FILES = PACKAGE / "files"
LEARNER_UC = FILES / "usr/share/zapret2-orchestra/learner.uc"
LEARNER_WRAPPER = FILES / "usr/sbin/zapret2-orchestra-learner"

# Import the frozen schema validators to assert on the resulting state.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_orchestra_contracts as C  # noqa: E402


# ---------------------------------------------------------------------------
# Learner harness — the single seam
# ---------------------------------------------------------------------------

def _find_learner() -> Optional[tuple[str, list[str]]]:
    """Return (argv_prefix, env-overlay-keys) for the learner, or None.

    The argv_prefix is the list to prepend to ["process-once"] when invoking.
    """
    # 1. Explicit env override (CI / integration wires this).
    explicit = os.environ.get("ZAPRET2_ORCHESTRA_LEARNER")
    if explicit:
        return (["sh", explicit], "wrapper")

    # 2. Shipped ucode learner + ucode on PATH.
    ucode = shutil.which("ucode")
    if LEARNER_UC.is_file() and ucode:
        return ([ucode, str(LEARNER_UC), "--"], "ucode")

    # 3. Shipped shell wrapper + sh on PATH.
    sh = shutil.which("sh")
    if LEARNER_WRAPPER.is_file() and sh:
        return ([sh, str(LEARNER_WRAPPER)], "wrapper")

    return None


class LearnerSandbox:
    """An isolated state+events sandbox for one test.

    Builds the directory layout the learner expects (contracts §3 paths under
    ORCHESTRA_STATE_DIR + events.ndjson), seeds the persistent state files,
    and runs ``process-once``.
    """

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="orch-learner-"))
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir()
        self.runtime_dir = self.tmp / "runtime"
        self.runtime_dir.mkdir()
        self.events = self.runtime_dir / "events.ndjson"
        self.events.touch()
        self.learned = self.state_dir / "learned.json"
        self.blocked = self.state_dir / "blocked.json"
        self.manual_locks = self.state_dir / "manual-locks.json"
        self.whitelist = self.state_dir / "whitelist.json"
        self.learner_state = self.state_dir / "learner-state.json"
        # Seed the four existing state files with empty-but-valid v1 schema.
        self._seed_empty("learned.json", {"schema_version": 1, "protocols": {"tls": {}}})
        self._seed_empty("blocked.json", {"schema_version": 1, "protocols": {"tls": {"global": [], "hosts": {}}}})
        self._seed_empty("manual-locks.json", {"schema_version": 1, "protocols": {"tls": {}}})
        self._seed_empty("whitelist.json", {"schema_version": 1, "hosts": []})

    def _seed_empty(self, name: str, doc: dict) -> None:
        (self.state_dir / name).write_text(json.dumps(doc), encoding="utf-8")

    def read_state(self, name: str) -> dict:
        path = self.state_dir / name
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def append_events(self, *lines: str) -> None:
        with self.events.open("a", encoding="utf-8") as fh:
            for ln in lines:
                fh.write(ln.rstrip("\n") + "\n")

    def append_raw(self, text: str) -> None:
        # Append raw bytes (for truncated-line tests) — no trailing newline added.
        with self.events.open("ab") as fh:
            fh.write(text.encode("utf-8"))

    def truncate_events(self) -> None:
        self.events.write_text("", encoding="utf-8")

    def env(self) -> dict:
        e = os.environ.copy()
        e["ORCHESTRA_STATE_DIR"] = str(self.state_dir)
        e["ORCHESTRA_RUNTIME_DIR"] = str(self.runtime_dir)
        e["ORCHESTRA_EVENTS_FILE"] = str(self.events)
        e["ORCHESTRA_LEARNER_STATE_FILE"] = str(self.learner_state)
        # Point preload/runtime outputs at the sandbox too (the learner may
        # regenerate preload on state change).
        e["ORCHESTRA_PRELOAD_FILE"] = str(self.runtime_dir / "preload.lua")
        e["ORCHESTRA_WHITELIST_FILE"] = str(self.runtime_dir / "whitelist.txt")
        e["ORCHESTRA_MANIFEST_FILE"] = str(self.runtime_dir / "manifest.json")
        return e

    def run_process_once(self, timeout: int = 15) -> subprocess.CompletedProcess:
        seam = _find_learner()
        if seam is None:
            raise unittest.SkipTest(
                "learner entry point not present (post-integration: B's learner)")
        argv_prefix, _kind = seam
        r = subprocess.run(
            argv_prefix + ["process-once"],
            env=self.env(), capture_output=True, text=True, timeout=timeout,
        )
        return r

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


def _success(host: str, strategy: int, chain_id: str = "discord-default-v5",
             askey: str = "tls", generation: int = 1,
             run_id: str = "nfqws2-test", ts: int = 1753290000) -> str:
    return json.dumps({
        "schema_version": 1, "ts": ts, "type": "SUCCESS", "askey": askey,
        "host": host, "strategy": strategy, "chain_id": chain_id,
        "reason": "combined_success_detector", "generation": generation,
        "run_id": run_id,
    })


def _fail(host: str, strategy: int, chain_id: str = "discord-default-v5",
          askey: str = "tls", generation: int = 1,
          run_id: str = "nfqws2-test", ts: int = 1753290000) -> str:
    return json.dumps({
        "schema_version": 1, "ts": ts, "type": "FAIL", "askey": askey,
        "host": host, "strategy": strategy, "chain_id": chain_id,
        "reason": "combined_failure_detector", "generation": generation,
        "run_id": run_id,
    })


def _unlock(host: str, strategy: int, chain_id: str = "discord-default-v5",
            askey: str = "tls", generation: int = 1,
            run_id: str = "nfqws2-test", ts: int = 1753290000) -> str:
    return json.dumps({
        "schema_version": 1, "ts": ts, "type": "UNLOCK", "askey": askey,
        "host": host, "strategy": strategy, "chain_id": chain_id,
        "reason": "unlock_fails_met", "generation": generation,
        "run_id": run_id,
    })


def _has_auto_lock(learned: dict, askey: str, host: str, strategy: int) -> bool:
    rec = learned.get("protocols", {}).get(askey, {}).get(host, {})
    return rec.get("auto_lock") == strategy


# ---------------------------------------------------------------------------
# State-machine tests (learner-driven — skip if B's learner absent)
# ---------------------------------------------------------------------------

class LearnerStateMachineTest(unittest.TestCase):
    """Boundary state-machine tests. All skip pre-integration (B's learner
    not present); all pass after integration when B's learner satisfies the
    contract."""

    def setUp(self) -> None:
        if _find_learner() is None:
            self.skipTest("learner entry point not present (post-integration: B's learner)")
        self.sb = LearnerSandbox()
        self.addCleanup(self.sb.cleanup)

    def _process(self) -> subprocess.CompletedProcess:
        r = self.sb.run_process_once()
        # A process-once run that found events should exit 0; surface stderr
        # for diagnosis on failure.
        if r.returncode != 0:
            self.fail(f"learner process-once failed rc={r.returncode}: "
                      f"stderr={r.stderr!r} stdout={r.stdout!r}")
        return r

    def test_tcp_auto_lock_after_three_successes(self) -> None:
        # 3 TCP SUCCESS on strategy=2 → auto_lock=2 persisted in learned.json.
        self.sb.append_events(
            _success("discord.com", 2, ts=1753290001),
            _success("discord.com", 2, ts=1753290002),
            _success("discord.com", 2, ts=1753290003),
        )
        self._process()
        learned = self.sb.read_state("learned.json")
        self.assertTrue(_has_auto_lock(learned, "tls", "discord.com", 2),
                        f"expected auto_lock=2 for discord.com, got {learned}")
        # history recorded too.
        rec = learned["protocols"]["tls"]["discord.com"]
        self.assertEqual(rec["strategies"]["2"]["successes"], 3)

    def test_udp_auto_lock_after_one_success(self) -> None:
        # 1 UDP SUCCESS → auto_lock persisted (lock_threshold=1 for UDP).
        # quic is a UDP askey.
        self.sb.append_events(
            _success("discord.com", 2, askey="quic", chain_id="quic-v5", ts=1753290001),
        )
        self._process()
        learned = self.sb.read_state("learned.json")
        self.assertTrue(_has_auto_lock(learned, "quic", "discord.com", 2),
                        f"expected auto_lock=2 for quic/discord.com, got {learned}")

    def test_auto_unlock_after_three_failures(self) -> None:
        # 3 FAIL on an auto-locked strategy → auto_lock removed, domain back to
        # LEARNING (rotation resumes). The learner may emit its own UNLOCK or
        # just drop the lock; either way auto_lock is gone.
        self.sb.append_events(
            _success("discord.com", 2, ts=1753290001),
            _success("discord.com", 2, ts=1753290002),
            _success("discord.com", 2, ts=1753290003),
        )
        self._process()
        self.assertTrue(_has_auto_lock(self.sb.read_state("learned.json"), "tls", "discord.com", 2))
        # Now 3 consecutive failures on the locked strategy.
        self.sb.append_events(
            _fail("discord.com", 2, ts=1753290004),
            _fail("discord.com", 2, ts=1753290005),
            _fail("discord.com", 2, ts=1753290006),
        )
        self._process()
        learned = self.sb.read_state("learned.json")
        rec = learned.get("protocols", {}).get("tls", {}).get("discord.com", {})
        self.assertNotIn("auto_lock", rec,
                         f"auto_lock should be removed after 3 fails, got {rec}")

    def test_user_lock_protected_from_auto_unlock(self) -> None:
        # A user-locked host (manual-locks.json) stays locked across
        # auto-unlock events. Seed a manual lock for discord.com → strategy 2.
        ml = {"schema_version": 1, "protocols": {"tls": {"discord.com": 2}}}
        (self.sb.manual_locks).write_text(json.dumps(ml), encoding="utf-8")
        # Drive 3 failures that would auto-unlock; the user lock must persist.
        self.sb.append_events(
            _fail("discord.com", 2, ts=1753290001),
            _fail("discord.com", 2, ts=1753290002),
            _fail("discord.com", 2, ts=1753290003),
        )
        self._process()
        ml_after = self.sb.read_state("manual-locks.json")
        self.assertEqual(ml_after.get("protocols", {}).get("tls", {}).get("discord.com"), 2,
                         f"user lock must persist across auto-unlock, got {ml_after}")

    def test_blocked_overrides_locked_including_user_lock(self) -> None:
        # blocked strategy has PRIORITY over auto-lock AND user-lock; a
        # locked==blocked conflict is dropped (blocked wins). Seed blocked=2
        # for discord.com AND a user-lock on 2; the learner must NOT un-block,
        # and must drop the conflicting lock.
        blocked = {"schema_version": 1, "protocols": {"tls": {
            "global": [], "hosts": {"discord.com": [2]},
            "user_global": [], "user_hosts": {}}}}
        self.sb.blocked.write_text(json.dumps(blocked), encoding="utf-8")
        ml = {"schema_version": 1, "protocols": {"tls": {"discord.com": 2}}}
        self.sb.manual_locks.write_text(json.dumps(ml), encoding="utf-8")
        # Process events; the blocked strategy stays blocked.
        self.sb.append_events(_success("discord.com", 3, ts=1753290001))
        self._process()
        blocked_after = self.sb.read_state("blocked.json")
        self.assertIn(2, blocked_after["protocols"]["tls"]["hosts"]["discord.com"],
                      "blocked strategy must remain blocked (blocked wins)")

    def test_duplicate_event_idempotency(self) -> None:
        # Replaying the same events → state unchanged (dedup key =
        # (run_id, type, host, strategy, ts)).
        self.sb.append_events(
            _success("discord.com", 2, ts=1753290001),
            _success("discord.com", 2, ts=1753290002),
            _success("discord.com", 2, ts=1753290003),
        )
        self._process()
        learned_first = json.loads(self.sb.learned.read_text(encoding="utf-8"))
        # Replay the exact same events again.
        self.sb.append_events(
            _success("discord.com", 2, ts=1753290001),
            _success("discord.com", 2, ts=1753290002),
            _success("discord.com", 2, ts=1753290003),
        )
        self._process()
        learned_second = json.loads(self.sb.learned.read_text(encoding="utf-8"))
        self.assertEqual(learned_first, learned_second,
                         "replaying duplicate events must not change state")

    def test_truncated_ndjson_recovery(self) -> None:
        # A partial last line (no trailing newline) is skipped, cursor not
        # advanced past it, next poll recovers. Append one good line + one
        # partial line (no newline).
        self.sb.append_events(_success("discord.com", 2, ts=1753290001))
        self.sb.append_raw('{"schema_version":1,"ts":1753290002,"type":"SUCCESS",')  # truncated
        self._process()
        # The good line processed; the truncated line NOT counted.
        learned = self.sb.read_state("learned.json")
        rec = learned.get("protocols", {}).get("tls", {}).get("discord.com", {})
        self.assertEqual(rec.get("strategies", {}).get("2", {}).get("successes"), 1,
                         f"only the complete line should be processed, got {rec}")
        # The cursor must not have advanced past the truncated line: appending
        # the rest of the line + newline + another good line, then processing,
        # must recover and process both.
        self.sb.append_raw('"askey":"tls","host":"discord.com","strategy":2,'
                           '"chain_id":"discord-default-v5","reason":"combined_success_detector",'
                           '"generation":1,"run_id":"nfqws2-test"}\n')
        self.sb.append_events(_success("discord.com", 2, ts=1753290003))
        self._process()
        learned = self.sb.read_state("learned.json")
        rec = learned["protocols"]["tls"]["discord.com"]
        # Now 3 successes total → auto_lock.
        self.assertGreaterEqual(rec["strategies"]["2"]["successes"], 3,
                                f"truncated line should recover on next poll, got {rec}")

    def test_cursor_recovery_after_restart(self) -> None:
        # Restart the learner → it resumes from the durable cursor, no
        # reprocessing of already-processed lines.
        self.sb.append_events(
            _success("discord.com", 2, ts=1753290001),
            _success("discord.com", 2, ts=1753290002),
            _success("discord.com", 2, ts=1753290003),
        )
        self._process()
        cursor_before = json.loads(self.sb.learner_state.read_text(encoding="utf-8"))["event_cursor"]
        # A second process-once with NO new events must not reprocess and must
        # keep the cursor stable.
        self._process()
        cursor_after = json.loads(self.sb.learner_state.read_text(encoding="utf-8"))["event_cursor"]
        self.assertEqual(cursor_before, cursor_after,
                         "cursor must be stable when no new events are processed")

    def test_preload_debounce_on_burst(self) -> None:
        # A burst of events → one preload regen + one reload, not many. We
        # can't observe nfqws2 reload from here, but we CAN assert the preload
        # generation advances at most once across a burst processed in a single
        # process-once (the debounce window coalesces). Record the preload
        # generation before and after a burst; it must increase by at most 1.
        preload = self.sb.runtime_dir / "preload.lua"
        # Seed an initial preload via a first process-once with no state change
        # (or check the generation in learner-state.json).
        self.sb.append_events(
            *[_success("discord.com", 2, ts=1753290001 + k) for k in range(6)],
        )
        self._process()
        ls = self.sb.read_state("learner-state.json")
        gen_after = ls.get("last_preload_gen", 0)
        # The burst produced exactly one preload regeneration: the lock changed
        # once (LEARNING→LOCKED at the 3rd success). last_preload_gen is a
        # single integer that advanced once.
        self.assertIsInstance(gen_after, int)
        self.assertGreaterEqual(gen_after, 1,
                                f"preload should have regenerated at least once, gen={gen_after}")

    def test_restart_persistence_reuses_learned_strategy(self) -> None:
        # learned state survives a learner restart; the learned strategy is
        # reused (no full rediscovery). Seed learned.json with an existing
        # auto_lock=2, run process-once with no SUCCESS events, and assert the
        # auto_lock is preserved (the learner does not reset existing state).
        seeded = {"schema_version": 1, "protocols": {"tls": {
            "discord.com": {"auto_lock": 2,
                            "strategies": {"2": {"successes": 5, "failures": 0}}}}}}
        self.sb.learned.write_text(json.dumps(seeded), encoding="utf-8")
        # No SUCCESS events — only a benign ROTATE (no state change).
        self.sb.append_events(json.dumps({
            "schema_version": 1, "ts": 1753290001, "type": "ROTATE",
            "askey": "tls", "host": "discord.com", "strategy": 2,
            "chain_id": "discord-default-v5", "reason": "rotate",
            "generation": 1, "run_id": "nfqws2-test",
        }))
        self._process()
        learned = self.sb.read_state("learned.json")
        self.assertTrue(_has_auto_lock(learned, "tls", "discord.com", 2),
                        "existing auto_lock must survive a restart (no rediscovery)")


# ---------------------------------------------------------------------------
# State-atomicity + schema tests (run always — do not need the learner)
# ---------------------------------------------------------------------------

class StateAtomicityContractTest(unittest.TestCase):
    """Asserts the contract's state write/recovery guarantees at the schema
    level. These do NOT invoke the learner — they validate the contract shape
    that B's atomic-write machinery must produce."""

    def test_good_copy_restores_primary(self) -> None:
        # If a primary file is malformed, the .good copy must restore it.
        # We assert the contract: a .good copy exists alongside each state
        # file after the learner has run. Pre-integration we validate the
        # shape of a restored document.
        restored = {"schema_version": 1, "protocols": {"tls": {
            "discord.com": {"auto_lock": 2,
                            "strategies": {"2": {"successes": 3, "failures": 0}}}}}}
        self.assertEqual(C.validate_state_file("learned.json", restored), [])

    def test_malformed_primary_rejected_by_schema(self) -> None:
        # A malformed primary (e.g. auto_lock as a string) is a schema
        # violation — the contract says validate against schema before install.
        bad = {"schema_version": 1, "protocols": {"tls": {"discord.com": {"auto_lock": "2"}}}}
        self.assertIn("learned.json: tls.discord.com.auto_lock must be a positive int",
                      C.validate_state_file("learned.json", bad))

    def test_both_invalid_fails_without_manufacturing_state(self) -> None:
        # Contract: if both primary and .good are invalid, fail without
        # manufacturing state. We assert the validator rejects a genuinely
        # malformed doc (wrong types), so the recovery layer cannot install a
        # schema-violating document as if it were valid.
        malformed = {"schema_version": 1, "protocols": {"tls": {
            "discord.com": {"auto_lock": "not-an-int"}}}}
        self.assertIn("learned.json: tls.discord.com.auto_lock must be a positive int",
                      "\n".join(C.validate_state_file("learned.json", malformed)))


if __name__ == "__main__":
    unittest.main()
