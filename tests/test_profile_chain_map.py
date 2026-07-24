"""Profile chain-map + stable block/lock identity tests (r7 fix).

Covers the defect where ``generate-preload.uc`` could bake the WRONG
``ORCHESTRA_CHAIN_ID_FOR_STRATEGY`` map for the active profile, and where the
DEFAULT_BLOCKED_PASS_DOMAINS numeric block (``strategy=1``) accidentally
blocked the WINNER (``tls_multisplit_sni``, runtime strategy 1 in the
original-parity pool) instead of the intended pass chain.

Three layers (mirroring ``test_preload_generator.py``):
  1. Static analysis of the ucode generator source (always runs).
  2. Python-oracle tests for the chain map + stable block resolution (always
     run; the oracle mirrors the ucode generator).
  3. Runtime tests that execute the real ucode generator with sidecars in a
     sandbox.  Skipped when ``ucode`` is not on PATH.

Stable identity approach (contract §4):
  ``blocked.json`` stores DEFAULT_BLOCKED_PASS_DOMAINS blocks by STABLE CHAIN
  ID (``hosts_chain``), not by runtime strategy number.  ``generate-preload.uc``
  resolves each stable id to the runtime strategy number the ACTIVE profile's
  sidecar assigns (via ``strategy_for_chain_id``) and drops ids whose chain is
  absent from the active profile — so a block never transfers to a different
  chain that happens to share the same runtime number.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHARE = ROOT / "openwrt/zapret2-orchestra/files/usr/share/zapret2-orchestra"
GENERATOR = SHARE / "generate-preload.uc"
PROFILES_DIR = SHARE / "profiles"
ORIG_POOL_SIDECAR = PROFILES_DIR / "discord-adaptive-original-pool.json"
OLD_ADAPTIVE_SIDECAR = PROFILES_DIR / "discord-adaptive.json"
SHIPPED_BLOCKED = ROOT / "openwrt/zapret2-orchestra/files/etc/zapret2-orchestra/blocked.json"

# The stable chain id DEFAULT_BLOCKED_PASS_DOMAINS blocks: the OLD adaptive
# profile's strategy-1 chain (Default old).  Present in the discord-adaptive
# (2-strategy) sidecar (-> strategy 1, Default old, harmless in circular) and
# ABSENT from discord-adaptive-original-pool (24-strategy) -> block dropped ->
# winner (runtime strategy 1 = chain-tls_multisplit_sni-70576793) NOT blocked.
PASS_LIKE_CHAIN = "discord-send-syndata-tls_multisplit_sni-44860d17"
# The winner in the original-parity pool: runtime strategy 1.
ORIG_POOL_WINNER = "chain-tls_multisplit_sni-70576793"


def _load_sidecar(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _by_chain(sidecar: dict) -> dict[str, int]:
    """stable_id -> runtime strategy number (the inverse map generate-preload
    uses to resolve hosts_chain entries)."""
    return {str(k): int(v) for k, v in sidecar["strategy_for_chain_id"].items()}


# ---------------------------------------------------------------------------
# Static analysis of generate-preload.uc (always runs — no ucode needed)
# ---------------------------------------------------------------------------

class ProfileChainMapStaticTest(unittest.TestCase):
    """The generator source must read the sidecar for the ACTIVE profile,
    validate its profile_id, and resolve stable chain ids — not bake a
    hardcoded old profile."""

    def setUp(self) -> None:
        self.src = GENERATOR.read_text(encoding="utf-8")

    def test_reads_active_profile_from_manager_state(self) -> None:
        # The chain map must be driven by mgr.profile (from manager-state.json),
        # not by a hardcoded profile name.
        self.assertIn("read_manager_state()", self.src)
        self.assertIn("read_chain_map(mgr.profile)", self.src)

    def test_no_hardcoded_old_adaptive_profile_filename(self) -> None:
        # The generator must NOT hardcode the old adaptive sidecar filename as a
        # fallback.  The sidecar path is derived from the active profile name.
        self.assertNotIn("'discord-adaptive.json'", self.src)
        self.assertNotIn('"discord-adaptive.json"', self.src)
        # sidecar_path builds the path from the profile argument, not a literal.
        self.assertIn("function sidecar_path(profile)", self.src)
        self.assertIn("PROFILES_DIR + '/' + profile + '.json'", self.src)
        self.assertIn("BUILTIN_PROFILES_DIR + '/' + profile + '.json'", self.src)

    def test_validates_sidecar_profile_id_matches_active_profile(self) -> None:
        # Phase 2: a stale/mismatched sidecar (profile_id != active profile)
        # must be rejected explicitly — this is the hardening that prevents the
        # OLD adaptive 2-strategy map from being silently baked for original-pool.
        self.assertIn("profile_id", self.src)
        self.assertIn("does not match active profile", self.src)
        self.assertIn("stale or mismatched sidecar", self.src)

    def test_validates_sidecar_schema_and_chain_map_presence(self) -> None:
        self.assertIn("schema_version must be 1", self.src)
        self.assertIn("has no chain_id_for_strategy map", self.src)

    def test_reads_strategy_for_chain_id_inverse(self) -> None:
        # Phase 3: the inverse map (stable_id -> runtime number) is what
        # resolves hosts_chain entries.  The generator must build by_chain from
        # the sidecar's strategy_for_chain_id (with a cross-check) or invert
        # chain_id_for_strategy.
        self.assertIn("by_chain", self.src)
        self.assertIn("strategy_for_chain_id", self.src)

    def test_renders_hosts_chain_and_global_chain_blocks(self) -> None:
        # The generator must read hosts_chain / global_chain (+ user_*_chain)
        # and resolve them to runtime numbers via the active profile sidecar.
        for key in ("hosts_chain", "global_chain", "user_hosts_chain", "user_global_chain"):
            self.assertIn(key, self.src)
        self.assertIn("resolve_chain_strategy", self.src)
        self.assertIn("resolve_chain_block_list", self.src)

    def test_absent_chain_is_dropped_not_transferred(self) -> None:
        # The resolution must drop chains absent from the active profile rather
        # than falling back to a runtime number — the core stable-identity rule.
        self.assertIn("absent from the active profile", self.src)
        # And it must NOT silently fall back to a numeric transfer: the resolve
        # function returns null for an absent chain (dropped by the caller).
        self.assertIn("return null", self.src)

    def test_chain_map_read_once_and_shared_with_blocked_rendering(self) -> None:
        # render_preload reads the chain map once and passes it to both
        # render_blocked (for stable-id resolution) and render_chain_map.
        self.assertIn("let cm = read_chain_map(mgr.profile);", self.src)
        self.assertIn("render_blocked(lines, seeds, cm)", self.src)
        self.assertIn("render_chain_map_and_generation(seeds, mgr, cm)", self.src)

    def test_native_profile_without_sidecar_still_succeeds(self) -> None:
        # A native profile (no sidecar) must not fail generation: read_chain_map
        # returns null and the chain map is emitted empty.  This preserves the
        # golden-fixture / native-profile behaviour.
        self.assertIn("ORCHESTRA_CHAIN_ID_FOR_STRATEGY = {}", self.src)


# ---------------------------------------------------------------------------
# Python-oracle tests for chain map + stable block resolution (always run)
# ---------------------------------------------------------------------------

class ProfileChainMapOracleTest(unittest.TestCase):
    """Exercises the stable-identity resolution using the repo's real sidecars
    via the Python oracle in test_preload_generator."""

    @classmethod
    def setUpClass(cls) -> None:
        # Import the oracle from the sibling test module (same package).
        import sys
        tests_dir = str(ROOT / "tests")
        if tests_dir not in sys.path:
            sys.path.insert(0, tests_dir)
        from test_preload_generator import (  # type: ignore
            render_blocked,
            render_chain_map_and_generation,
            render_preload,
        )
        cls.render_blocked = staticmethod(render_blocked)
        cls.render_chain_map_and_generation = staticmethod(render_chain_map_and_generation)
        cls.render_preload = staticmethod(render_preload)
        cls.orig_pool = _load_sidecar(ORIG_POOL_SIDECAR)
        cls.old_adaptive = _load_sidecar(OLD_ADAPTIVE_SIDECAR)

    def _seeds(self, chain_id_for_strategy: dict, strategy_for_chain_id: dict, askey: str = "tls") -> dict:
        return {
            "blocked": {"schema_version": 1, "protocols": {"tls": {
                "global": [], "hosts": {},
                "global_chain": [],
                "hosts_chain": {"discord.com": [PASS_LIKE_CHAIN]},
            }}},
            "learned": {"schema_version": 1, "protocols": {"tls": {}}},
            "manual_locks": {"schema_version": 1, "protocols": {"tls": {}}},
            "whitelist": {"schema_version": 1, "hosts": []},
            "_chain_id_for_strategy": chain_id_for_strategy,
            "_strategy_for_chain_id": strategy_for_chain_id,
            "_chain_askey": askey,
        }

    def test_original_pool_bakes_24_strategy_map(self) -> None:
        s = self.orig_pool
        out = self.render_chain_map_and_generation(
            self._seeds(s["chain_id_for_strategy"], s["strategy_for_chain_id"]),
            profile="discord-adaptive-original-pool", generation=1,
        )
        joined = "\n".join(out)
        # 24 entries, one per strategy, keyed under ["tls"].
        self.assertIn('ORCHESTRA_CHAIN_ID_FOR_STRATEGY = { ["tls"] = {', joined)
        # runtime strategy 1 -> the winner chain id
        self.assertIn(f'[1]={PASS_LIKE_CHAIN.replace(PASS_LIKE_CHAIN, ORIG_POOL_WINNER)!r}'.replace("'",
                                                                                                      '"'), joined)
        # count the [n]= entries
        import re
        entries = re.findall(r'\[(\d+)\]=', joined)
        self.assertEqual(len(entries), 24, f"expected 24 chain entries, got {len(entries)}")
        self.assertEqual(sorted(int(e) for e in entries), list(range(1, 25)))

    def test_old_adaptive_bakes_2_strategy_map(self) -> None:
        s = self.old_adaptive
        out = self.render_chain_map_and_generation(
            self._seeds(s["chain_id_for_strategy"], s["strategy_for_chain_id"]),
            profile="discord-adaptive", generation=1,
        )
        joined = "\n".join(out)
        import re
        entries = re.findall(r'\[(\d+)\]=', joined)
        self.assertEqual(len(entries), 2)
        self.assertEqual(sorted(int(e) for e in entries), [1, 2])

    def test_switching_profiles_changes_the_chain_map(self) -> None:
        # The same render call with the two profiles' maps yields different
        # ORCHESTRA_CHAIN_ID_FOR_STRATEGY tables (different chain ids for the
        # same runtime numbers).
        op = self.orig_pool
        old = self.old_adaptive
        a = "\n".join(self.render_chain_map_and_generation(
            self._seeds(op["chain_id_for_strategy"], op["strategy_for_chain_id"]),
            profile="discord-adaptive-original-pool"))
        b = "\n".join(self.render_chain_map_and_generation(
            self._seeds(old["chain_id_for_strategy"], old["strategy_for_chain_id"]),
            profile="discord-adaptive"))
        self.assertNotEqual(a, b)
        # runtime number 1 refers to DIFFERENT chains across the two profiles.
        self.assertIn(ORIG_POOL_WINNER, a)
        self.assertIn(PASS_LIKE_CHAIN, b)
        self.assertNotIn(ORIG_POOL_WINNER, b)
        self.assertNotIn(PASS_LIKE_CHAIN, a)

    def test_same_runtime_number_refers_to_different_chains_across_profiles(self) -> None:
        # Runtime strategy 1 = ORIG_POOL_WINNER in original-pool but
        # PASS_LIKE_CHAIN (Default old) in the 2-strategy profile.
        self.assertEqual(self.orig_pool["chain_id_for_strategy"]["1"], ORIG_POOL_WINNER)
        self.assertEqual(self.old_adaptive["chain_id_for_strategy"]["1"], PASS_LIKE_CHAIN)
        self.assertNotEqual(ORIG_POOL_WINNER, PASS_LIKE_CHAIN)

    def test_block_by_stable_id_does_not_block_different_chain_same_runtime_number(self) -> None:
        # DBPD blocks PASS_LIKE_CHAIN.  In original-pool that chain is ABSENT
        # (it is not in strategy_for_chain_id), so the block is DROPPED — it
        # does NOT transfer to runtime strategy 1 (the winner) even though the
        # old numeric seed used the number 1.
        op_bc = _by_chain(self.orig_pool)
        self.assertNotIn(PASS_LIKE_CHAIN, op_bc)  # absent from original-pool
        seeds = self._seeds(self.orig_pool["chain_id_for_strategy"],
                            self.orig_pool["strategy_for_chain_id"])
        blocked_lines = [l for l in self.render_blocked(seeds, op_bc)
                         if l.startswith('slm_preload_blocked("tls", "discord.com"')]
        self.assertEqual(blocked_lines, [],
                         "winner must NOT be blocked in original-pool; got: " + repr(blocked_lines))

    def test_absent_chain_block_not_transferred_to_replacement_number(self) -> None:
        # Explicit: the pass-like chain is absent from original-pool, so no
        # slm_preload_blocked call is emitted for discord.com at all (the block
        # is dropped, not redirected to runtime number 1 or any other number).
        op_bc = _by_chain(self.orig_pool)
        seeds = self._seeds(self.orig_pool["chain_id_for_strategy"],
                            self.orig_pool["strategy_for_chain_id"])
        all_blocked = self.render_blocked(seeds, op_bc)
        self.assertFalse(any('slm_preload_blocked(' in l and 'discord.com' in l for l in all_blocked))

    def test_winner_not_blocked_when_pass_chain_absent_from_pool(self) -> None:
        # The winner (chain-tls_multisplit_sni-70576793) is runtime strategy 1
        # in original-pool.  With the DBPD block targeting the absent pass-like
        # chain, the winner's runtime number (1) is never emitted as blocked.
        op_bc = _by_chain(self.orig_pool)
        winner_n = op_bc[ORIG_POOL_WINNER]
        self.assertEqual(winner_n, 1)
        seeds = self._seeds(self.orig_pool["chain_id_for_strategy"],
                            self.orig_pool["strategy_for_chain_id"])
        all_blocked = self.render_blocked(seeds, op_bc)
        # No blocked call references strategy 1 for any host.
        for l in all_blocked:
            self.assertNotIn("{1}", l, f"strategy 1 (winner) must not appear blocked: {l}")

    def test_old_adaptive_profile_blocks_strategy_one_default_old(self) -> None:
        # In the 2-strategy profile the pass-like chain IS present (-> strategy
        # 1, Default old).  The block transfers to runtime strategy 1 — which is
        # Default old (harmless in circular), NOT the winner.  This is the
        # "debatable but harmless" case the task calls out.
        old_bc = _by_chain(self.old_adaptive)
        self.assertEqual(old_bc[PASS_LIKE_CHAIN], 1)
        seeds = self._seeds(self.old_adaptive["chain_id_for_strategy"],
                            self.old_adaptive["strategy_for_chain_id"])
        blocked_lines = [l for l in self.render_blocked(seeds, old_bc)
                         if l.startswith('slm_preload_blocked("tls", "discord.com"')]
        self.assertEqual(len(blocked_lines), 1)
        self.assertIn("{1}", blocked_lines[0])

    def test_shipped_blocked_seed_uses_chain_form_not_numeric(self) -> None:
        doc = json.loads(SHIPPED_BLOCKED.read_text(encoding="utf-8"))
        tls = doc["protocols"]["tls"]
        # The DBPD policy is stored by stable chain id, not by runtime number.
        self.assertIn("hosts_chain", tls)
        self.assertIn("discord.com", tls["hosts_chain"])
        self.assertEqual(tls["hosts_chain"]["discord.com"], [PASS_LIKE_CHAIN])
        # Numeric hosts is empty for the DBPD set (canonical policy is chain-based).
        self.assertEqual(tls.get("hosts", {}), {})
        # The domain set is preserved (59 domains) — DBPD not deleted.
        self.assertGreaterEqual(len(tls["hosts_chain"]), 59)


# ---------------------------------------------------------------------------
# ucode-runtime tests: execute the real generator with sidecars (skip if no ucode)
# ---------------------------------------------------------------------------

class ProfileChainMapRuntimeTest(unittest.TestCase):
    """Executes the real ucode generator against the real sidecars in a sandbox.
    Skipped when ``ucode`` is not on PATH."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.ucode = shutil.which("ucode")

    def setUp(self) -> None:
        if self.ucode is None:
            self.skipTest("ucode executable not found on PATH")
        self.tmp = Path(tempfile.mkdtemp(prefix="orchestra-pcm-"))
        self.state_dir = Path(self.tmp) / "state"
        self.runtime_dir = Path(self.tmp) / "runtime"
        self.builtin_profiles = Path(self.tmp) / "builtin-profiles"
        self.state_dir.mkdir()
        self.runtime_dir.mkdir()
        self.builtin_profiles.mkdir()
        # Ship the real sidecars into the sandbox builtin-profiles dir.
        shutil.copy(ORIG_POOL_SIDECAR, self.builtin_profiles / ORIG_POOL_SIDECAR.name)
        shutil.copy(OLD_ADAPTIVE_SIDECAR, self.builtin_profiles / OLD_ADAPTIVE_SIDECAR.name)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    EMPTY_SEEDS = {
        "blocked": {"schema_version": 1, "protocols": {"tls": {"global": [], "hosts": {}}}},
        "learned": {"schema_version": 1, "protocols": {"tls": {}}},
        "manual-locks": {"schema_version": 1, "protocols": {"tls": {}}},
        "whitelist": {"schema_version": 1, "hosts": []},
    }

    DBPD_SEEDS = {
        "blocked": {"schema_version": 1, "protocols": {"tls": {
            "global": [], "hosts": {},
            "global_chain": [],
            "hosts_chain": {"discord.com": [PASS_LIKE_CHAIN]},
        }}},
        "learned": {"schema_version": 1, "protocols": {"tls": {}}},
        "manual-locks": {"schema_version": 1, "protocols": {"tls": {}}},
        "whitelist": {"schema_version": 1, "hosts": []},
    }

    def _write_seeds(self, seeds: dict) -> None:
        for name, doc in seeds.items():
            (self.state_dir / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8")

    def _write_manager_state(self, profile: str | None, generation: int = 1) -> None:
        doc = {"schema_version": 1, "states": ["idle"], "generation": generation,
               "enabled": profile is not None, "profile": profile,
               "hashes": {"nfqws2_opt": "00000000", "preload": "00000000",
                          "whitelist": "00000000", "manifest": "00000000"}}
        (self.state_dir / "manager-state.json").write_text(json.dumps(doc), encoding="utf-8")

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["ORCHESTRA_STATE_DIR"] = str(self.state_dir)
        env["ORCHESTRA_RUNTIME_DIR"] = str(self.runtime_dir)
        env["ORCHESTRA_PRELOAD_FILE"] = str(self.runtime_dir / "preload.lua")
        env["ORCHESTRA_WHITELIST_FILE"] = str(self.runtime_dir / "whitelist.txt")
        env["ORCHESTRA_MANIFEST_FILE"] = str(self.runtime_dir / "manifest.json")
        env["ORCHESTRA_MANAGER_STATE_FILE"] = str(self.state_dir / "manager-state.json")
        env["ORCHESTRA_PROFILES_DIR"] = str(self.state_dir / "profiles")  # empty -> user override absent
        env["ORCHESTRA_BUILTIN_PROFILES_DIR"] = str(self.builtin_profiles)
        env["ORCHESTRA_SHARE_DIR"] = str(self.tmp / "share")
        return subprocess.run([self.ucode, str(GENERATOR), *args],
                              env=env, capture_output=True, text=True, timeout=10)

    def test_original_pool_bakes_24_strategy_map_not_old_2(self) -> None:
        self._write_seeds(self.EMPTY_SEEDS)
        self._write_manager_state("discord-adaptive-original-pool")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        preload = (self.runtime_dir / "preload.lua").read_text(encoding="utf-8")
        # The 24-strategy winner appears; the OLD adaptive's 2-strategy chains do NOT.
        self.assertIn(ORIG_POOL_WINNER, preload)
        self.assertNotIn(PASS_LIKE_CHAIN, preload)
        # 24 [n]= entries under ["tls"].
        import re
        m = re.search(r'ORCHESTRA_CHAIN_ID_FOR_STRATEGY = \{ \["tls"\] = \{([^}]*)\)', preload)
        self.assertIsNotNone(m, "chain map table not found")
        entries = re.findall(r'\[(\d+)\]=', m.group(1))
        self.assertEqual(len(entries), 24)
        self.assertEqual(sorted(int(e) for e in entries), list(range(1, 25)))

    def test_switching_profile_changes_baked_map(self) -> None:
        # original-pool
        self._write_seeds(self.EMPTY_SEEDS)
        self._write_manager_state("discord-adaptive-original-pool")
        r1 = self._run()
        self.assertEqual(r1.returncode, 0, r1.stderr)
        preload_op = (self.runtime_dir / "preload.lua").read_text(encoding="utf-8")
        # old adaptive
        self._write_manager_state("discord-adaptive")
        r2 = self._run()
        self.assertEqual(r2.returncode, 0, r2.stderr)
        preload_old = (self.runtime_dir / "preload.lua").read_text(encoding="utf-8")
        self.assertNotEqual(preload_op, preload_old)
        self.assertIn(ORIG_POOL_WINNER, preload_op)
        self.assertIn(PASS_LIKE_CHAIN, preload_old)

    def test_stale_sidecar_wrong_profile_id_fails(self) -> None:
        # Put a STALE sidecar at the original-pool path whose profile_id says
        # "discord-adaptive" (the old 2-strategy map).  The generator must DIE
        # with a clear message, NOT silently bake the old map.
        self._write_seeds(self.EMPTY_SEEDS)
        self._write_manager_state("discord-adaptive-original-pool")
        stale = json.loads(OLD_ADAPTIVE_SIDECAR.read_text(encoding="utf-8"))
        (self.builtin_profiles / "discord-adaptive-original-pool.json").write_text(
            json.dumps(stale), encoding="utf-8")
        r = self._run()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("does not match active profile", r.stderr)
        self.assertIn("discord-adaptive-original-pool", r.stderr)
        # And the wrong map was NOT written.
        self.assertFalse((self.runtime_dir / "preload.lua").exists())

    def test_missing_sidecar_for_native_profile_succeeds_with_empty_map(self) -> None:
        # A profile with no sidecar (native profile) must still generate
        # successfully, with an empty chain map.
        self._write_seeds(self.EMPTY_SEEDS)
        self._write_manager_state("discord-v5")  # native profile, no sidecar in sandbox
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        preload = (self.runtime_dir / "preload.lua").read_text(encoding="utf-8")
        self.assertIn("ORCHESTRA_CHAIN_ID_FOR_STRATEGY = {}", preload)

    def test_winner_not_blocked_in_original_pool_preload(self) -> None:
        # DBPD blocks the pass-like chain.  In original-pool that chain is
        # absent -> the block is dropped -> NO slm_preload_blocked for
        # discord.com -> the winner (runtime strategy 1) is NOT blocked.
        self._write_seeds(self.DBPD_SEEDS)
        self._write_manager_state("discord-adaptive-original-pool")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        preload = (self.runtime_dir / "preload.lua").read_text(encoding="utf-8")
        self.assertNotIn("slm_preload_blocked", preload,
                         "no blocked calls expected — pass chain absent from original-pool")

    def test_old_adaptive_preload_blocks_strategy_one(self) -> None:
        # In the 2-strategy profile the pass-like chain -> strategy 1 (Default
        # old) -> the block IS emitted as slm_preload_blocked(..., {1}).
        self._write_seeds(self.DBPD_SEEDS)
        self._write_manager_state("discord-adaptive")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        preload = (self.runtime_dir / "preload.lua").read_text(encoding="utf-8")
        self.assertIn('slm_preload_blocked("tls", "discord.com", {1})', preload)

    def test_bad_schema_version_sidecar_fails(self) -> None:
        self._write_seeds(self.EMPTY_SEEDS)
        self._write_manager_state("discord-adaptive-original-pool")
        bad = json.loads(ORIG_POOL_SIDECAR.read_text(encoding="utf-8"))
        bad["schema_version"] = 2
        (self.builtin_profiles / "discord-adaptive-original-pool.json").write_text(
            json.dumps(bad), encoding="utf-8")
        r = self._run()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("schema_version must be 1", r.stderr)


if __name__ == "__main__":
    unittest.main()
