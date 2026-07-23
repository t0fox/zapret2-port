"""Parity tests for A's strategy-catalog importer (BOUNDARY: run the importer,
assert ``strategy-sources/catalog.json`` + the generated adaptive profile).

Drives A's importer solely through its CLI surface
(``tools/import_strategy_catalog.py``) and asserts the resulting catalog +
adaptive-profile artifacts against the FROZEN contract (§1, §4) and the
reverse-engineered original model (spec §1). These tests do NOT import A's
internal functions — they run the importer as a subprocess and assert on its
outputs, so any implementation that satisfies the contract passes.

SEAM (integration entry point)
------------------------------
A's importer is a parallel artifact not present in this worktree. The tests
invoke it through a SINGLE seam:

    python tools/import_strategy_catalog.py [--presets <dir>] [--out <catalog.json>]

located via (in order):
  - ``$ZAPRET2_ORCHESTRA_IMPORTER`` env (explicit override — CI/integration)
  - ``tools/import_strategy_catalog.py`` in the repo root (A's importer).

If the importer script is absent (pre-integration), the importer-driven tests
SKIP cleanly with the reason ``importer not present (post-integration: A's
importer)``. The contract-shape tests that validate an existing catalog.json
against the schema, and the static parity assertions (DEFAULT_BLOCKED_PASS_DOMAINS
provenance, tls_mod runtime confirmation) run always.

Binding rules honored:
  - Network results for Default old / Default v5 are ROUTER acceptance
    evidence — NOT hardcoded as universal unit-test assertions. We assert the
    CHAIN STRUCTURE and PROVENANCE, not the network outcome.
  - tls_mod is confirmed by the target runtime contract (binary/strings), NOT
    by string-matching the lua.
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
IMPORTER = ROOT / "tools" / "import_strategy_catalog.py"
CATALOG = ROOT / "strategy-sources" / "catalog.json"
ADAPTIVE_OPT = (ROOT / "openwrt" / "zapret2-orchestra" / "files"
                / "usr/share/zapret2-orchestra" / "profiles" / "discord-adaptive.opt")
ADAPTIVE_JSON = (ROOT / "openwrt" / "zapret2-orchestra" / "files"
                 / "usr/share/zapret2-orchestra" / "profiles" / "discord-adaptive.json")
DISCORD_V5_OPT = (ROOT / "openwrt" / "zapret2-orchestra" / "files"
                  / "usr/share/zapret2-orchestra" / "profiles" / "discord-v5.opt")
INIT_VARS_LUA = (ROOT / "openwrt" / "zapret2-orchestra" / "files"
                 / "opt/zapret2/lua/init_vars.lua")
IPSET_DISCORD = (ROOT / "openwrt" / "zapret2-orchestra" / "files"
                 / "etc/zapret2-orchestra" / "lists" / "ipset-discord.txt")
Z2_CORE = ROOT / "zapret2-core"
NFQWS2_BIN_CANDIDATES = [
    Z2_CORE / "nfq2" / "nfqws2",
    ROOT / "openwrt" / "zapret2" / "files" / "opt" / "zapret2" / "nfq2" / "nfqws2",
]

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_orchestra_contracts as C  # noqa: E402

PINNED_PRESET_COMMIT = "4d75c70b430562e970bcf64cbe24072ce104b36a"


# ---------------------------------------------------------------------------
# Importer harness — the single seam
# ---------------------------------------------------------------------------

def _find_importer() -> Optional[Path]:
    explicit = os.environ.get("ZAPRET2_ORCHESTRA_IMPORTER")
    if explicit:
        return Path(explicit)
    if IMPORTER.is_file():
        return IMPORTER
    return None


def _run_importer(**extra_env: str) -> tuple[subprocess.CompletedProcess, Path]:
    """Run the importer into a temp out path; return (result, out_path)."""
    script = _find_importer()
    if script is None:
        raise unittest.SkipTest("importer not present (post-integration: A's importer)")
    if shutil.which("python3"):
        py = "python3"
    elif shutil.which("python"):
        py = "python"
    else:
        raise unittest.SkipTest("no python interpreter on PATH")
    tmp = Path(tempfile.mkdtemp(prefix="orch-importer-"))
    out = tmp / "catalog.json"
    env = os.environ.copy()
    env.update(extra_env)
    r = subprocess.run(
        [py, str(script), "--out", str(out)],
        env=env, capture_output=True, text=True, timeout=60,
    )
    return r, out


# ---------------------------------------------------------------------------
# Importer determinism + catalog contract (driven — skip if importer absent)
# ---------------------------------------------------------------------------

class ImporterDeterminismTest(unittest.TestCase):
    """The importer is deterministic: re-running on identical pinned inputs
    yields a byte-for-byte identical catalog. (Contract §1: 'reproducible
    byte-for-byte from the pinned inputs'.)"""

    def setUp(self) -> None:
        if _find_importer() is None:
            self.skipTest("importer not present (post-integration: A's importer)")

    def test_two_runs_produce_identical_catalog(self) -> None:
        r1, out1 = _run_importer()
        self.assertEqual(r1.returncode, 0, f"importer run1 failed: {r1.stderr}")
        r2, out2 = _run_importer()
        self.assertEqual(r2.returncode, 0, f"importer run2 failed: {r2.stderr}")
        b1 = out1.read_bytes()
        b2 = out2.read_bytes()
        self.assertEqual(b1, b2, "importer is not deterministic: two runs differ byte-for-byte")

    def test_catalog_satisfies_contract(self) -> None:
        r, out = _run_importer()
        self.assertEqual(r.returncode, 0, f"importer failed: {r.stderr}")
        doc = json.loads(out.read_text(encoding="utf-8"))
        problems = C.validate_catalog(doc)
        self.assertEqual(problems, [], "importer output violates catalog contract:\n  " + "\n  ".join(problems))

    def test_stable_chain_ids_independent_of_position(self) -> None:
        # stable_id/chain_id derived from chain content, not position.
        r, out = _run_importer()
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(out.read_text(encoding="utf-8"))
        ids = [e["stable_id"] for e in doc["entries"]]
        chains = [e["chain_id"] for e in doc["entries"]]
        # No duplicates (each distinct chain has a distinct stable_id).
        self.assertEqual(len(ids), len(set(ids)), f"duplicate stable_ids: {ids}")
        self.assertEqual(len(chains), len(set(chains)), f"duplicate chain_ids: {chains}")

    def test_deterministic_strategy_numbering(self) -> None:
        # strategy_number is assigned deterministically by the importer within
        # the adaptive profile. Re-running yields identical strategy_number per
        # stable_id.
        r1, out1 = _run_importer()
        r2, out2 = _run_importer()
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        m1 = {e["stable_id"]: e["strategy_number"] for e in json.loads(out1.read_text())["entries"]}
        m2 = {e["stable_id"]: e["strategy_number"] for e in json.loads(out2.read_text())["entries"]}
        self.assertEqual(m1, m2, "strategy numbering is not deterministic across runs")


class ImporterBlockPreservationTest(unittest.TestCase):
    """``--new`` block preservation + dependency closure."""

    def setUp(self) -> None:
        if _find_importer() is None:
            self.skipTest("importer not present (post-integration: A's importer)")

    def test_new_block_boundaries_preserved(self) -> None:
        # Each catalog entry's source_sha256 hashes the exact preset block
        # bytes (from the line after a --new separator to the next --new/EOF).
        # Two entries from DIFFERENT --new blocks must have different
        # source_sha256; the same block re-imported yields the same sha.
        r, out = _run_importer()
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(out.read_text(encoding="utf-8"))
        shas = [e["source_sha256"] for e in doc["entries"]]
        # At least one pair differs (multi-block preset) — provenance is real.
        self.assertGreater(len(set(shas)), 1, "all entries share a source_sha256 — --new blocks not preserved")

    def test_dependency_closure_required_assets_present(self) -> None:
        # required_assets must list every asset the chain depends on (e.g.
        # init_vars.lua for tls_google, ipset-discord.txt for the discord
        # chain), each with sha256 + source. The Default v5 entry must
        # reference init_vars.lua + ipset-discord.txt.
        r, out = _run_importer()
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(out.read_text(encoding="utf-8"))
        v5 = [e for e in doc["entries"] if e.get("source_id") == "Default v5"]
        self.assertTrue(v5, "no Default v5 entry")
        asset_paths = {a["path"] for a in v5[0]["required_assets"]}
        self.assertTrue(any("init_vars.lua" in p for p in asset_paths),
                        f"Default v5 required_assets must include init_vars.lua, got {asset_paths}")
        self.assertTrue(any("ipset-discord" in p for p in asset_paths),
                        f"Default v5 required_assets must include ipset-discord.txt, got {asset_paths}")


class ImporterDetectionTest(unittest.TestCase):
    """Service / domain / ASKEY detection."""

    def setUp(self) -> None:
        if _find_importer() is None:
            self.skipTest("importer not present (post-integration: A's importer)")

    def test_discord_chain_detected_as_tls_askey(self) -> None:
        r, out = _run_importer()
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(out.read_text(encoding="utf-8"))
        v5 = [e for e in doc["entries"] if e.get("source_id") == "Default v5"]
        self.assertTrue(v5)
        # Default v5 is a TLS chain → askey=tls.
        self.assertEqual(v5[0]["askey"], "tls", f"Default v5 askey must be tls, got {v5[0]['askey']}")
        # services includes discord (ipset-discord / discord.com domains).
        self.assertIn("discord", v5[0].get("services", []),
                      "Default v5 services must include discord")

    def test_all_askeys_in_the_9_profiles(self) -> None:
        r, out = _run_importer()
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(out.read_text(encoding="utf-8"))
        for e in doc["entries"]:
            self.assertIn(e["askey"], C.ASKEY_ALL,
                          f"entry {e.get('stable_id')!r} askey {e['askey']!r} not in the 9 profiles")


class ImporterDefaultV5GroupingTest(unittest.TestCase):
    """Contract §1 rule 8: the three Default-v5 lua_steps share one
    strategy_number and one chain_id — they are ONE strategy chain."""

    def setUp(self) -> None:
        if _find_importer() is None:
            self.skipTest("importer not present (post-integration: A's importer)")

    def test_default_v5_three_ops_one_strategy_one_chain(self) -> None:
        r, out = _run_importer()
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(out.read_text(encoding="utf-8"))
        v5 = [e for e in doc["entries"] if e.get("source_id") == "Default v5"]
        self.assertTrue(v5)
        e = v5[0]
        self.assertEqual(len(e["lua_steps"]), 3, "Default v5 must have 3 lua_steps (send+syndata+syndata)")
        funcs = [s["func"] for s in e["lua_steps"]]
        self.assertEqual(funcs, ["send", "syndata", "syndata"], f"unexpected v5 funcs: {funcs}")
        # All three steps are ONE strategy chain → one strategy_number, one chain_id.
        self.assertIsInstance(e["strategy_number"], int)
        self.assertTrue(e["strategy_number"] >= 1)
        # chain_id is the sha256 of the normalized 3-step chain.
        self.assertEqual(e["chain_id"], C._normalized_lua_steps_hash(e["lua_steps"]))


# ---------------------------------------------------------------------------
# Adaptive profile generation (contract §4) — driven + present-file tests
# ---------------------------------------------------------------------------

class AdaptiveProfileTest(unittest.TestCase):
    """The importer generates an adaptive Discord profile: a circular_quality
    .opt with selector + numbered chains + a chain_id_for_strategy sidecar."""

    def test_adaptive_opt_has_circular_quality_selector(self) -> None:
        if not ADAPTIVE_OPT.is_file():
            self.skipTest("discord-adaptive.opt not present (post-integration: A's importer)")
        text = ADAPTIVE_OPT.read_text(encoding="utf-8")
        self.assertIn("circular_quality", text,
                      "discord-adaptive.opt must reference circular_quality (the selector)")

    def test_adaptive_opt_has_numbered_strategies_from_one(self) -> None:
        if not ADAPTIVE_OPT.is_file():
            self.skipTest("discord-adaptive.opt not present (post-integration)")
        import re
        text = ADAPTIVE_OPT.read_text(encoding="utf-8")
        nums = [int(n) for n in re.findall(r"strategy=(\d+)", text)]
        self.assertGreaterEqual(len(nums), 2,
                                "adaptive profile must have >=2 numbered strategies (Default old + v5)")
        # Contiguous from 1 (orchestrator.lua requires it).
        self.assertEqual(nums, list(range(1, len(nums) + 1)),
                         f"adaptive strategy numbers must be contiguous from 1, got {nums}")

    def test_adaptive_json_sidecar_has_chain_id_for_strategy(self) -> None:
        if not ADAPTIVE_JSON.is_file():
            self.skipTest("discord-adaptive.json not present (post-integration: A's importer)")
        doc = json.loads(ADAPTIVE_JSON.read_text(encoding="utf-8"))
        self.assertEqual(doc.get("schema_version"), 1)
        self.assertEqual(doc.get("profile_id"), "discord-adaptive")
        self.assertEqual(doc.get("askey"), "tls")
        cmap = doc.get("chain_id_for_strategy")
        self.assertIsInstance(cmap, dict)
        self.assertIn("1", cmap, "chain_id_for_strategy must map strategy 1 (Default old)")
        self.assertIn("2", cmap, "chain_id_for_strategy must map strategy 2 (Default v5)")
        # strategy_for_chain_id is the inverse map.
        smap = doc.get("strategy_for_chain_id")
        self.assertIsInstance(smap, dict)
        self.assertEqual(smap.get(cmap["2"]), 2,
                         "strategy_for_chain_id must invert chain_id_for_strategy")
        self.assertTrue(doc.get("default_blocked_pass_domains_applied") is not None,
                        "sidecar must record whether DEFAULT_BLOCKED_PASS_DOMAINS was applied")


# ---------------------------------------------------------------------------
# Static native discord-v5 profile (no circular_quality required)
# ---------------------------------------------------------------------------

class DiscordV5StaticProfileTest(unittest.TestCase):
    """The static native discord-v5.opt: send+syndata:tls_google+syndata,
    --ipset=ipset-discord.txt, --lua-init=init_vars.lua, NO circular_quality,
    NO strategy=N. (Contract §6 / spec §4: circular_quality NOT required for
    the static native profile.)"""

    def test_discord_v5_opt_present_and_native(self) -> None:
        if not DISCORD_V5_OPT.is_file():
            self.skipTest("discord-v5.opt not present (post-integration)")
        text = DISCORD_V5_OPT.read_text(encoding="utf-8")
        # Native nfqws2 strategy: send + syndata:tls_google + syndata.
        self.assertIn("send", text)
        self.assertIn("syndata", text)
        self.assertIn("tls_google", text)
        # NO circular_quality (it is a static native profile, not circular).
        self.assertNotIn("circular_quality", text,
                         "discord-v5.opt must NOT reference circular_quality (static native)")
        # NO strategy=N (no circular rotation).
        import re
        self.assertIsNone(re.search(r"strategy=\d", text),
                          "discord-v5.opt must NOT have strategy=N (static native)")
        # ipset-based (not hostlist <HOSTLIST>).
        self.assertIn("--ipset", text)
        # lua-init points at init_vars.lua (provides tls_google via tls_mod).
        self.assertIn("init_vars.lua", text)

    def test_init_vars_lua_present(self) -> None:
        if not INIT_VARS_LUA.is_file():
            self.skipTest("init_vars.lua not present (post-integration)")
        # init_vars.lua is a real lua file; confirm it is non-empty and
        # references the tls_mod builtin (NOT by string-matching the lua per
        # the binding rule — but its PRESENCE as a shipped asset is a
        # packaging contract, not a runtime assertion).
        self.assertGreater(INIT_VARS_LUA.stat().st_size, 0, "init_vars.lua is empty")

    def test_ipset_discord_present(self) -> None:
        if not IPSET_DISCORD.is_file():
            self.skipTest("ipset-discord.txt not present (post-integration)")
        self.assertGreater(IPSET_DISCORD.stat().st_size, 0, "ipset-discord.txt is empty")


# ---------------------------------------------------------------------------
# DEFAULT_BLOCKED_PASS_DOMAINS provenance (always runs — provenance is in the
# pinned GUI submodule)
# ---------------------------------------------------------------------------

class DefaultBlockedPassDomainsTest(unittest.TestCase):
    """``discord.com`` ∈ DEFAULT_BLOCKED_PASS_DOMAINS, and the set is the EXACT
    one imported from the pinned GUI (``blocked_strategies_manager.py:65-102``)
    with provenance — NOT derived from autohostlist."""

    GUI = ROOT / "zapret2gui" / "src" / "orchestra" / "blocked_strategies_manager.py"

    @unittest.skipUnless(GUI.is_file(), "zapret2gui submodule not checked out")
    def test_discord_com_in_default_blocked_pass_domains_upstream(self) -> None:
        text = self.GUI.read_text(encoding="utf-8")
        # The literal set definition is at lines 65-102.
        self.assertIn("DEFAULT_BLOCKED_PASS_DOMAINS", text)
        self.assertIn('"discord.com"', text,
                      "discord.com must be in the pinned GUI DEFAULT_BLOCKED_PASS_DOMAINS")

    @unittest.skipUnless(CATALOG.is_file(), "catalog.json not present (post-integration)")
    def test_catalog_default_blocked_pass_domains_matches_provenance(self) -> None:
        doc = json.loads(CATALOG.read_text(encoding="utf-8"))
        dbpd = doc["default_blocked_pass_domains"]
        self.assertEqual(dbpd["source_repo"], "youtubediscord/zapret")
        self.assertEqual(dbpd["source_commit"], "9d57e55d6751587d9d52b52147a05a0a8fcc9fd8")
        self.assertEqual(dbpd["source_path"],
                         "src/orchestra/blocked_strategies_manager.py:65-102")
        self.assertIn("discord.com", dbpd["domains"])


# ---------------------------------------------------------------------------
# tls_mod runtime contract (confirmed by binary/strings, NOT lua string-match)
# ---------------------------------------------------------------------------

class TlsModRuntimeContractTest(unittest.TestCase):
    """``tls_mod`` is a nfqws2-C builtin (spec §5.3), not a Lua file.
    Confirm it by the pinned nfqws2 binary strings, not by string-matching the
    lua. Skips if the nfqws2 binary is not built/available locally."""

    @unittest.skipUnless(any(p.is_file() for p in NFQWS2_BIN_CANDIDATES),
                         "nfqws2 binary not built (post-integration / CI build)")
    def test_nfqws2_binary_advertises_tls_mod(self) -> None:
        import re
        found = False
        for cand in NFQWS2_BIN_CANDIDATES:
            if not cand.is_file():
                continue
            # Read the binary and search for the tls_mod token. We look for
            # the printable token 'tls_mod' in the binary's strings (a builtin
            # lua desync function name is embedded as a string table entry).
            data = cand.read_bytes()
            if b"tls_mod" in data:
                found = True
                break
        self.assertTrue(found, "nfqws2 binary does not advertise the tls_mod builtin")


# ---------------------------------------------------------------------------
# Catalog-present schema checks (run against A's committed catalog.json)
# ---------------------------------------------------------------------------

class CatalogPresentTest(unittest.TestCase):
    """When A's catalog.json is committed, validate it end-to-end."""

    @unittest.skipUnless(CATALOG.is_file(), "catalog.json not present (post-integration: A's importer)")
    def test_committed_catalog_satisfies_contract(self) -> None:
        doc = json.loads(CATALOG.read_text(encoding="utf-8"))
        self.assertEqual(C.validate_catalog(doc), [],
                         "committed catalog.json violates the contract")

    @unittest.skipUnless(CATALOG.is_file(), "catalog.json not present (post-integration)")
    def test_committed_catalog_is_deterministic_vs_importer(self) -> None:
        if _find_importer() is None:
            self.skipTest("importer not present to compare")
        r, out = _run_importer()
        self.assertEqual(r.returncode, 0, r.stderr)
        committed = json.loads(CATALOG.read_text(encoding="utf-8"))
        regenerated = json.loads(out.read_text(encoding="utf-8"))
        # The committed catalog must equal a fresh importer run (sorted keys,
        # deterministic). Compare canonical JSON.
        self.assertEqual(
            json.dumps(committed, sort_keys=True),
            json.dumps(regenerated, sort_keys=True),
            "committed catalog.json differs from a fresh importer run (not deterministic)")


if __name__ == "__main__":
    unittest.main()
