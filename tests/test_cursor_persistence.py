"""Cursor persistence + durable-dedup tests for B's runtime learner.

Covers the two CI failure classes fixed in learner.uc:

  * CURSOR PERSISTENCE / durable dedup — replaying the SAME events (appended
    past the durable cursor) must NOT double-apply.  The durable cursor stops
    a same-file re-read, but an appended DUPLICATE event (same run_id/type/
    host/strategy/ts) lands past the cursor and can only be caught by a DURABLE
    dedup set persisted in learner-state.json (``recent_keys``).  This is the
    ``test_duplicate_event_idempotency`` contract (replaying duplicate events
    must not change state).

  * LAST_LINE_SHA256 — ``event_cursor.last_line_sha256`` must be a non-empty
    fingerprint of the last consumed NDJSON line, and must STAY non-empty (be
    preserved, not wiped to '') across an idle re-run that processes no new
    lines.  Wiping it on an idle pass made cursor_before != cursor_after (the
    ``test_cursor_recovery_after_restart`` contract: cursor stable when idle).
    ucode-mod-fs exposes no portable SHA-256, so the field holds an 8-hex
    rolling hash (hash31); the field name is kept for contract compatibility.

The file has two layers:

  1. Python reference tests (always run) — exercise the reference mirror of the
     cursor reader + a durable-dedup model.  These pass without ucode and pin
     the expected behavior.
  2. ucode-runtime tests (skip when ucode absent) — drive the REAL learner.uc
     ``process-once`` through its filesystem/event seam and assert the same
     behavior on the on-disk learner-state.json / learned.json.

The reference layer must stay in sync with learner.uc; if the policy changes
there, update the mirror in test_orchestra_learner.py too.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Reference mirrors of learner.uc logic (kept faithful in test_orchestra_learner).
import test_orchestra_learner as L  # noqa: E402

LEARNER_UC = L.LEARNER_UC


# ---------------------------------------------------------------------------
# Reference helpers: a minimal durable-dedup learner model.
# ---------------------------------------------------------------------------

def _ev(etype: str, host: str, strategy: int, ts: int,
        askey: str = "tls", run_id: str = "r") -> dict:
    return {"schema_version": 1, "ts": ts, "type": etype, "askey": askey,
            "host": host, "strategy": strategy, "run_id": run_id}


class RefLearner:
    """In-memory model of the learner's process-once pass with durable state.

    Mirrors learner.uc: load (learned/blocked/manual/cursor/recent_keys) ->
    read_events_from_cursor -> process_event (dedup against the durable set) ->
    persist cursor + recent_keys.  Used to pin cursor/dedup behavior without
    ucode.
    """

    def __init__(self) -> None:
        self.learned = {"schema_version": 1, "protocols": {}}
        self.blocked = {"schema_version": 1, "protocols": {}}
        self.manual = {"schema_version": 1, "protocols": {}}
        self.cursor = {"bytes": 0, "lines": 0, "last_line_sha256": ""}
        self.recent_keys: set[str] = set()
        self._raw = ""

    def append(self, *events: dict) -> None:
        for ev in events:
            self._raw += json.dumps(ev) + "\n"

    def process(self) -> dict:
        seen = set(self.recent_keys)  # durable: seed from persisted set
        applied = []

        def handler(ev: dict) -> None:
            r = L.process_event(self.learned, self.blocked, self.manual, ev, seen)
            applied.append(r)

        cursor, n = L.read_events_from_cursor(self._raw, self.cursor, handler)
        self.cursor = cursor
        self.recent_keys = seen  # persist (bounded in ucode; unbounded here is fine for tests)
        return {"processed": n, "applied": applied}

    def successes(self, askey: str, host: str, strategy: int) -> int:
        rec = self.learned.get("protocols", {}).get(askey, {}).get(host, {})
        return int(rec.get("strategies", {}).get(str(strategy), {}).get("successes", 0))

    def auto_lock(self, askey: str, host: str) -> Optional[int]:
        rec = self.learned.get("protocols", {}).get(askey, {}).get(host, {})
        return rec.get("auto_lock")


# ---------------------------------------------------------------------------
# Layer 1: Python reference tests (always run).
# ---------------------------------------------------------------------------

class CursorHashReferenceTest(unittest.TestCase):
    """last_line_sha256 is a non-empty hash after processing and is preserved
    across an idle re-run (cursor stable when no new events)."""

    def _line(self, ts: int = 1) -> str:
        return json.dumps(_ev("success", "a.com", 2, ts=ts)) + "\n"

    def test_nonempty_hash_after_processing(self) -> None:
        raw = self._line(1) + self._line(2)
        cursor = {"bytes": 0, "lines": 0, "last_line_sha256": ""}
        cursor, n = L.read_events_from_cursor(raw, cursor, lambda _e: None)
        self.assertEqual(n, 2)
        self.assertEqual(cursor["bytes"], len(raw))
        self.assertNotEqual(cursor["last_line_sha256"], "",
                            "last_line_sha256 must be non-empty after processing lines")
        self.assertEqual(len(cursor["last_line_sha256"]), 8,
                         "hash31 fingerprint is 8 hex chars")

    def test_hash_preserved_on_idle_rerun(self) -> None:
        # The CI failure: after a restart that reads NO new lines, the hash was
        # wiped to '' so cursor_before != cursor_after.  It must be preserved.
        raw = self._line(1) + self._line(2)
        cursor = {"bytes": 0, "lines": 0, "last_line_sha256": ""}
        cursor, _ = L.read_events_from_cursor(raw, cursor, lambda _e: None)
        before = dict(cursor)
        # second pass from the advanced cursor reads nothing new
        cursor2, n2 = L.read_events_from_cursor(raw, cursor, lambda _e: None)
        self.assertEqual(n2, 0)
        self.assertEqual(cursor2, before,
                         "cursor must be byte-identical when no new events are processed")

    def test_hash_changes_when_new_line_appended(self) -> None:
        raw = self._line(1)
        cursor = {"bytes": 0, "lines": 0, "last_line_sha256": ""}
        cursor, _ = L.read_events_from_cursor(raw, cursor, lambda _e: None)
        first_hash = cursor["last_line_sha256"]
        self.assertNotEqual(first_hash, "")
        raw2 = raw + self._line(2)
        cursor, n = L.read_events_from_cursor(raw2, cursor, lambda _e: None)
        self.assertEqual(n, 1)
        self.assertNotEqual(cursor["last_line_sha256"], first_hash,
                            "fingerprint advances when a new last line is consumed")

    def test_cursor_bytes_and_lines_advance(self) -> None:
        lines = [self._line(ts) for ts in (1, 2, 3)]
        raw = "".join(lines)
        cursor = {"bytes": 0, "lines": 0, "last_line_sha256": ""}
        cursor, n = L.read_events_from_cursor(raw, cursor, lambda _e: None)
        self.assertEqual(n, 3)
        self.assertEqual(cursor["bytes"], len(raw), "bytes advance to EOF")
        self.assertEqual(cursor["lines"], 3, "lines count advances by processed count")

    def test_truncated_last_line_not_advanced(self) -> None:
        full = self._line(1)
        partial = json.dumps(_ev("success", "a.com", 2, ts=2))[:-3]  # no newline
        raw = full + partial
        cursor = {"bytes": 0, "lines": 0, "last_line_sha256": ""}
        cursor, n = L.read_events_from_cursor(raw, cursor, lambda _e: None)
        self.assertEqual(n, 1, "only the complete line is processed")
        self.assertEqual(cursor["bytes"], len(full),
                         "cursor stops at the partial line, not EOF")
        self.assertNotEqual(cursor["last_line_sha256"], "")


class DurableDedupReferenceTest(unittest.TestCase):
    """Replaying the SAME events (appended past the cursor) must not change
    state: the durable dedup set (recent_keys) catches the duplicate keys."""

    def test_replay_appended_duplicates_is_idempotent(self) -> None:
        # The test_duplicate_event_idempotency scenario: 3 TCP successes lock
        # strategy 2; appending the SAME 3 events and reprocessing must NOT
        # double successes (6) — the dedup keys match.
        lr = RefLearner()
        evs = [_ev("success", "discord.com", 2, ts=1753290001),
               _ev("success", "discord.com", 2, ts=1753290002),
               _ev("success", "discord.com", 2, ts=1753290003)]
        lr.append(*evs)
        lr.process()
        self.assertEqual(lr.successes("tls", "discord.com", 2), 3)
        self.assertEqual(lr.auto_lock("tls", "discord.com"), 2)
        learned_first = json.loads(json.dumps(lr.learned))
        cursor_first = dict(lr.cursor)
        # Replay the exact same events (appended past the durable cursor).
        lr.append(*evs)
        lr.process()
        self.assertEqual(lr.successes("tls", "discord.com", 2), 3,
                         "replayed duplicates must not double successes")
        self.assertEqual(lr.auto_lock("tls", "discord.com"), 2)
        self.assertEqual(lr.learned, learned_first,
                         "replaying duplicate events must not change learned state")
        # cursor advanced past the appended duplicates (bytes move), but lines
        # reflect only newly-applied events (0 new applied this pass).
        self.assertGreater(lr.cursor["bytes"], cursor_first["bytes"])

    def test_distinct_ts_events_are_not_deduped(self) -> None:
        # Sanity: events with DIFFERENT ts are new events, not duplicates, and
        # must be applied (the dedup key includes ts).
        lr = RefLearner()
        lr.append(_ev("success", "a.com", 2, ts=1))
        lr.process()
        self.assertEqual(lr.successes("tls", "a.com", 2), 1)
        lr.append(_ev("success", "a.com", 2, ts=2))
        lr.process()
        self.assertEqual(lr.successes("tls", "a.com", 2), 2,
                         "a new-ts event is not a duplicate")

    def test_cursor_survives_restart_roundtrip(self) -> None:
        # read -> process -> write -> read again: the cursor + dedup set round-
        # trip through serialization (simulating a restart that reloads state).
        lr = RefLearner()
        lr.append(_ev("success", "a.com", 2, ts=1),
                  _ev("success", "a.com", 2, ts=2))
        lr.process()
        self.assertEqual(lr.successes("tls", "a.com", 2), 2)
        # Serialize the durable state as the learner would write it on disk.
        written = {"event_cursor": lr.cursor, "recent_keys": {k: True for k in lr.recent_keys}}
        # Reload from the serialized form (simulates a fresh process-once after
        # a restart: learned.json + learner-state.json are re-read from disk).
        lr2 = RefLearner()
        lr2._raw = lr._raw  # same events file on disk
        lr2.learned = json.loads(json.dumps(lr.learned))  # reloaded learned.json
        lr2.cursor = json.loads(json.dumps(written["event_cursor"]))
        lr2.recent_keys = set(written["recent_keys"].keys())
        # No new events: reprocessing is a no-op (cursor at EOF, dedup seeded).
        lr2.process()
        self.assertEqual(lr2.successes("tls", "a.com", 2), 2,
                         "reloaded cursor + dedup must not reprocess")
        self.assertEqual(lr2.cursor, lr.cursor,
                         "cursor stable across the reload round-trip")

    def test_rotation_reset_replays_via_dedup(self) -> None:
        # If the file shrinks (rotation), the cursor resets to 0 and all lines
        # are re-read; the durable dedup set must prevent double-application.
        lr = RefLearner()
        lr.append(_ev("success", "a.com", 2, ts=1))
        lr.process()
        self.assertEqual(lr.successes("tls", "a.com", 2), 1)
        # Simulate rotation: file rewritten with the same single line, cursor
        # now past EOF -> read_events_from_cursor resets to 0 and re-reads.
        before_bytes = lr.cursor["bytes"]
        lr.cursor = {"bytes": before_bytes + 100, "lines": lr.cursor["lines"],
                     "last_line_sha256": lr.cursor["last_line_sha256"]}
        lr.process()
        self.assertEqual(lr.successes("tls", "a.com", 2), 1,
                         "rotation reset must not double-apply (durable dedup)")


# ---------------------------------------------------------------------------
# Layer 2: ucode-runtime tests — drive the real learner.uc (skip if no ucode).
# ---------------------------------------------------------------------------

def _find_learner() -> Optional[tuple[str, list[str]]]:
    explicit = None
    ucode = shutil.which("ucode")
    if LEARNER_UC.is_file() and ucode:
        return ([ucode, str(LEARNER_UC), "--"], "ucode")
    return None


class _Sandbox:
    """Minimal state+events sandbox driving the learner's process-once seam."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="orch-cursor-"))
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir()
        self.runtime_dir = self.tmp / "runtime"
        self.runtime_dir.mkdir()
        self.events = self.runtime_dir / "events.ndjson"
        self.events.touch()
        self.learner_state = self.state_dir / "learner-state.json"
        for name, doc in {
            "learned.json": {"schema_version": 1, "protocols": {"tls": {}}},
            "blocked.json": {"schema_version": 1, "protocols": {"tls": {"global": [], "hosts": {}}}},
            "manual-locks.json": {"schema_version": 1, "protocols": {"tls": {}}},
            "whitelist.json": {"schema_version": 1, "hosts": []},
        }.items():
            (self.state_dir / name).write_text(json.dumps(doc), encoding="utf-8")

    def append_events(self, *lines: str) -> None:
        with self.events.open("a", encoding="utf-8") as fh:
            for ln in lines:
                fh.write(ln.rstrip("\n") + "\n")

    def read_state(self, name: str) -> dict:
        path = self.state_dir / name
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def env(self) -> dict:
        e = {}
        for k in ("PATH", "HOME", "SystemRoot", "TEMP", "TMP"):
            if k in __import__("os").environ:
                e[k] = __import__("os").environ[k]
        e["ORCHESTRA_STATE_DIR"] = str(self.state_dir)
        e["ORCHESTRA_RUNTIME_DIR"] = str(self.runtime_dir)
        e["ORCHESTRA_EVENTS_FILE"] = str(self.events)
        e["ORCHESTRA_LEARNER_STATE_FILE"] = str(self.learner_state)
        e["LEARNER_STATE_FILE"] = str(self.learner_state)
        e["LEARNER_LOCK_DIR"] = str(self.runtime_dir / "learner.lock")
        e["LEARNER_LOG_FILE"] = str(self.runtime_dir / "learner.log")
        return e

    def run_process_once(self) -> subprocess.CompletedProcess:
        seam = _find_learner()
        if seam is None:
            raise unittest.SkipTest("ucode learner not present")
        argv_prefix, _kind = seam
        return subprocess.run(
            argv_prefix + ["process-once"], env=self.env(),
            capture_output=True, text=True, timeout=15,
        )

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


def _success(host: str, strategy: int, ts: int, run_id: str = "nfqws2-test") -> str:
    return json.dumps({"schema_version": 1, "ts": ts, "type": "SUCCESS",
                       "askey": "tls", "host": host, "strategy": strategy,
                       "chain_id": "discord-default-v5", "reason": "combined_success_detector",
                       "generation": 1, "run_id": run_id})


class CursorPersistenceUcodeTest(unittest.TestCase):
    """Drives the real learner.uc process-once and asserts cursor/dedup behavior
    on the on-disk state.  Skips when ucode is not on PATH (CI runs it)."""

    def setUp(self) -> None:
        if _find_learner() is None:
            self.skipTest("ucode learner not present")
        self.sb = _Sandbox()
        self.addCleanup(self.sb.cleanup)

    def _process(self) -> subprocess.CompletedProcess:
        r = self.sb.run_process_once()
        if r.returncode != 0:
            self.fail(f"learner process-once failed rc={r.returncode}: "
                      f"stderr={r.stderr!r} stdout={r.stdout!r}")
        return r

    def _cursor(self) -> dict:
        return self.sb.read_state("learner-state.json").get("event_cursor", {})

    def test_cursor_persisted_with_nonempty_last_line_sha256(self) -> None:
        self.sb.append_events(
            _success("discord.com", 2, ts=1753290001),
            _success("discord.com", 2, ts=1753290002),
            _success("discord.com", 2, ts=1753290003),
        )
        self._process()
        cur = self._cursor()
        self.assertEqual(cur.get("bytes"), self.sb.events.stat().st_size,
                         "cursor bytes advance to EOF")
        self.assertNotEqual(cur.get("last_line_sha256"), "",
                            "last_line_sha256 must be non-empty after processing")
        self.assertEqual(len(cur.get("last_line_sha256", "")), 8,
                         "hash31 fingerprint is 8 hex chars")

    def test_cursor_stable_across_idle_rerun(self) -> None:
        # The test_cursor_recovery_after_restart contract: a second process-once
        # with NO new events keeps the cursor byte-identical (incl. the hash).
        self.sb.append_events(
            _success("discord.com", 2, ts=1753290001),
            _success("discord.com", 2, ts=1753290002),
            _success("discord.com", 2, ts=1753290003),
        )
        self._process()
        before = self._cursor()
        self._process()
        after = self._cursor()
        self.assertEqual(before, after,
                         "cursor must be stable when no new events are processed")

    def test_replay_appended_duplicates_is_idempotent(self) -> None:
        # The test_duplicate_event_idempotency contract: appending the SAME
        # events and reprocessing must not change learned state (durable dedup).
        evs = (
            _success("discord.com", 2, ts=1753290001),
            _success("discord.com", 2, ts=1753290002),
            _success("discord.com", 2, ts=1753290003),
        )
        self.sb.append_events(*evs)
        self._process()
        learned_first = self.sb.read_state("learned.json")
        self.sb.append_events(*evs)  # replay the exact same events
        self._process()
        learned_second = self.sb.read_state("learned.json")
        self.assertEqual(learned_first, learned_second,
                         "replaying duplicate events must not change state")

    def test_cursor_resumes_after_restart_with_new_events(self) -> None:
        # Process 3, append a 4th, reprocess: only the 4th is new (cursor +
        # dedup resume); successes == 4, not a reprocess of the first 3.
        self.sb.append_events(
            _success("discord.com", 2, ts=1753290001),
            _success("discord.com", 2, ts=1753290002),
            _success("discord.com", 2, ts=1753290003),
        )
        self._process()
        self.assertEqual(self._cursor().get("bytes"), self.sb.events.stat().st_size)
        self.sb.append_events(_success("discord.com", 2, ts=1753290004))
        self._process()
        learned = self.sb.read_state("learned.json")
        rec = learned["protocols"]["tls"]["discord.com"]
        self.assertEqual(rec["strategies"]["2"]["successes"], 4,
                         "resumed cursor processes only the new 4th event")


if __name__ == "__main__":
    unittest.main()
