"""Original-parity adaptive pool tests (Step 3 + Step 9 subset).

Verifies ``discord-adaptive-original-pool.opt`` / ``.json`` -- the adaptive
profile generated from the EXACT original TLS circular pool recovered from
``Default (circular).txt`` @ 9d57e55 (the zapret2gui submodule).  Companion to
``docs/ORCHESTRA_ORIGINAL_TLS_POOL.md`` (the recovered pool composition, the
source of truth) and ``docs/ORCHESTRA_R7_CONTRACTS.md`` §4.

These tests run the REAL importer (``tools/import_strategy_catalog.py``) and
assert the generated original-pool profile:
  * is generated and present on disk;
  * has >2 strategies (the original pool has 24 circular-compatible -- the
    doc's 25 minus strategy 2, whose hostfakesplit_multi is defined in the
    unshipped zapret-multishake.lua, so it is circular-incompatible);
  * every included strategy is circular_compatible (no static_only /
    incompatible in the pool);
  * strategy numbers are contiguous from 1 (circular_quality requires it,
    orchestrator.lua:23);
  * the sidecar carries chain_id_for_strategy + the
    original_strategy_number provenance map;
  * the static fallback ``discord-v5.opt`` still validates (unchanged);
  * the existing ``discord-adaptive.opt`` (2-strategy) is unchanged.

They skip locally if the pinned repo / the GUI preset is absent and MUST NOT
skip when both are present (the worktree has zapret2gui populated).
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import import_strategy_catalog as IMP  # noqa: E402
import _nfqws2_parser as P  # noqa: E402

PROFILE_DIR = (ROOT / "openwrt" / "zapret2-orchestra" / "files" /
               "usr" / "share" / "zapret2-orchestra" / "profiles")
ORIG_OPT = PROFILE_DIR / "discord-adaptive-original-pool.opt"
ORIG_JSON = PROFILE_DIR / "discord-adaptive-original-pool.json"
ADAPTIVE_OPT = PROFILE_DIR / "discord-adaptive.opt"
ADAPTIVE_JSON = PROFILE_DIR / "discord-adaptive.json"
STATIC_OPT = PROFILE_DIR / "discord-v5.opt"

REPO_ROOT = Path(IMP.DEFAULT_REPO_ROOT)
ORIG_PRESET = ROOT / IMP.DEFAULT_ORIGINAL_POOL_PRESET


def _has_inputs() -> bool:
    return (REPO_ROOT / "presets").is_dir() and ORIG_PRESET.is_file()


@unittest.skipUnless(_has_inputs(), "pinned repo or original preset absent")
class TestOriginalPoolGeneration(unittest.TestCase):
    """Step 3: the original-parity adaptive pool is generated from the GUI
    Default (circular).txt TLS circular pool."""

    def setUp(self):
        self.assertTrue(ORIG_OPT.exists(), "discord-adaptive-original-pool.opt not generated")
        self.assertTrue(ORIG_JSON.exists(), "discord-adaptive-original-pool.json not generated")
        self.text = ORIG_OPT.read_text(encoding="utf-8")
        self.val = P.extract(self.text).value
        self.sidecar = json.loads(ORIG_JSON.read_text(encoding="utf-8"))

    def test_opt_is_a_valid_nfqws2_assignment(self):
        # Parses as a valid NFQWS2_OPT assignment (round-trip byte identity).
        self.assertTrue(P.validate_round_trip(self.text),
                        "original-pool.opt is not a valid round-trippable assignment")
        # profile_value_ok mirror: no injection chars in the value.
        self.assertNotIn("\x00", self.val)
        self.assertNotIn("\r", self.val)
        self.assertNotIn("$(", self.val)
        self.assertNotIn("`", self.val)
        self.assertNotIn(";", self.val)
        self.assertNotIn("|", self.val)
        self.assertNotIn("&&", self.val)

    def test_opt_uses_port_circular_quality_selector(self):
        # The port engine is circular_quality (NOT the original `circular`).
        self.assertIn("circular_quality:", self.val)
        # Selector carries the original's fails=3 rotation spirit.
        self.assertIn("fails=3", self.val)
        # The selector line itself is unnumbered.
        for line in self.text.splitlines():
            if line.strip().startswith("--lua-desync=circular_quality"):
                self.assertNotIn("strategy=", line)

    def test_opt_has_more_than_two_strategies(self):
        distinct = sorted({int(n) for n in re.findall(r"strategy=(\d+)", self.val)})
        self.assertGreater(len(distinct), 2,
                           f"original pool should have >2 strategies, got {distinct}")
        # The original pool has 24 circular-compatible strategies (29 minus 4
        # static-only send-cutoff minus 1 incompatible hostfakesplit_multi, see
        # the module docstring + docs/ORCHESTRA_ORIGINAL_TLS_POOL.md).
        self.assertEqual(len(distinct), 24,
                         f"expected 24 circular-compatible strategies, got {len(distinct)}: {distinct}")

    def test_strategy_numbers_contiguous_from_one(self):
        distinct = sorted({int(n) for n in re.findall(r"strategy=(\d+)", self.val)})
        self.assertEqual(distinct, list(range(1, len(distinct) + 1)),
                         f"strategy numbers must be contiguous from 1, got {distinct}")

    def test_selector_before_strategy_instances(self):
        cq_idx = self.val.find("--lua-desync=circular_quality")
        first_strategy = self.val.find("strategy=")
        self.assertGreater(cq_idx, -1)
        self.assertGreater(first_strategy, -1)
        self.assertLess(cq_idx, first_strategy,
                        "circular_quality selector must precede strategy instances")

    def test_no_static_only_or_incompatible_in_pool(self):
        # Every included chain in the sidecar is circular_compatible.
        for ch in self.sidecar["chains"]:
            self.assertEqual(ch["circular_compatibility"], "circular_compatible",
                             f"pool includes non-circular_compatible chain: {ch}")

    def test_opt_loads_orchestra_runtime_and_init_vars(self):
        # --lua-init chain: orchestra-extra init.lua (runtime) + init_vars.lua
        # (tls_google) + custom_funcs.lua (tls_multisplit_sni).  NOT
        # custom_diag / zapret-multishake / fakemultisplit / fakemultidisorder.
        self.assertIn("--lua-init=@/opt/zapret2/lua/orchestra-extra/init.lua", self.val)
        self.assertIn("--lua-init=@/opt/zapret2/lua/init_vars.lua", self.val)
        self.assertIn("--lua-init=@/opt/zapret2/lua/custom_funcs.lua", self.val)
        self.assertNotIn("zapret-multishake", self.val)
        self.assertNotIn("custom_diag", self.val)
        self.assertNotIn("fakemultisplit", self.val)
        self.assertNotIn("fakemultidisorder", self.val)

    def test_opt_filter_and_payload(self):
        # Discord ports + ipset (shipped) + --payload=all (kept from the
        # original so HTTP-fake strategies work) + port-convention --out-range.
        self.assertIn("--filter-tcp=80,443,1080,2053,2083,2087,2096,8443", self.val)
        self.assertIn("--ipset=/etc/zapret2-orchestra/lists/ipset-discord.txt", self.val)
        self.assertIn("--payload=all", self.val)
        self.assertIn("--out-range=-d10", self.val)

    def test_opt_declares_bin_blobs_for_included_strategies(self):
        # stun_pat + tls1/tls5/tls7 are referenced by included strategies and
        # need --blob= declarations (the .bin files ship separately).
        self.assertIn("--blob=stun_pat:@/opt/zapret2/bin/stun.bin", self.val)
        self.assertIn("--blob=tls1:@/opt/zapret2/bin/tls_clienthello_1.bin", self.val)
        self.assertIn("--blob=tls5:@/opt/zapret2/bin/tls_clienthello_5.bin", self.val)
        self.assertIn("--blob=tls7:@/opt/zapret2/bin/tls_clienthello_7.bin", self.val)
        # No @bin/ or @lua/ GUI asset refs (OpenWrt paths only).
        self.assertNotIn("@bin/", self.val)
        self.assertNotIn("@lua/", self.val)

    def test_opt_does_not_include_send_strategies(self):
        # The 4 static-only strategies (1, 4, 6, 7) all contain `send` and are
        # excluded; the included pool has NO `send` step (all steps reachable
        # inside circular).  hostfakesplit_multi (strategies 1, 2) is also
        # excluded (unshipped zapret-multishake).
        self.assertNotIn("send:", self.val)
        self.assertNotIn("hostfakesplit_multi", self.val)

    def test_sidecar_shape(self):
        s = self.sidecar
        self.assertEqual(s["schema_version"], 1)
        self.assertEqual(s["profile_id"], "discord-adaptive-original-pool")
        self.assertEqual(s["askey"], "tls")
        self.assertTrue(s["default_blocked_pass_domains_applied"])

    def test_sidecar_chain_id_for_strategy_and_inverse(self):
        s = self.sidecar
        cfs = s["chain_id_for_strategy"]
        sfc = s["strategy_for_chain_id"]
        # One entry per included strategy, contiguous from 1.
        self.assertEqual(sorted(int(k) for k in cfs),
                         list(range(1, len(s["chains"]) + 1)))
        # Inverse consistency.
        for strat_str, sid in cfs.items():
            self.assertEqual(sfc[sid], int(strat_str))

    def test_sidecar_chains_have_provenance_fields(self):
        s = self.sidecar
        for ch in s["chains"]:
            self.assertIn("generated_strategy", ch)
            self.assertIn("original_strategy_number", ch)
            self.assertRegex(ch["chain_id"], r"^[0-9a-f]{64}$")
            self.assertIn("stable_id", ch)
            self.assertEqual(ch["source"], IMP.ORIGINAL_POOL_SOURCE_LABEL)
            self.assertEqual(ch["circular_compatibility"], "circular_compatible")
            self.assertIn(ch["compatibility"], ("compatible", "incompatible"))

    def test_sidecar_original_strategy_provenance_map(self):
        s = self.sidecar
        prov = s["provenance"]
        self.assertEqual(prov["source_repo"], IMP.ORIGINAL_POOL_SOURCE_REPO)
        self.assertEqual(prov["source_commit"], IMP.ORIGINAL_POOL_SOURCE_COMMIT)
        self.assertEqual(prov["source_path"], IMP.ORIGINAL_POOL_SOURCE_PATH)
        osn = prov["original_strategy_numbers"]
        included = osn["included"]
        excluded = osn["excluded"]
        # 29 original strategies total; included + excluded partition 1..29.
        self.assertEqual(sorted(included + excluded), list(range(1, 30)))
        self.assertEqual(set(included) & set(excluded), set())
        # The 4 static-only (1? no -- 1 is incompatible; 4, 6, 7 are static-only)
        # and the hostfakesplit_multi strategies (1, 2) are excluded.
        self.assertNotIn(2, included)  # hostfakesplit_multi (unshipped)
        self.assertNotIn(4, included)  # send+syndata+tls_multisplit_sni (static-only)
        self.assertNotIn(6, included)  # send+syndata+multisplit (static-only)
        self.assertNotIn(7, included)  # send+syndata+pktmod (static-only)
        # excluded_detail records the class + reason for each excluded strategy.
        detail_by_n = {d["original_strategy"]: d for d in osn["excluded_detail"]}
        self.assertEqual(set(detail_by_n.keys()), set(excluded))
        for d in osn["excluded_detail"]:
            self.assertIn(d["circular_compatibility"], ("static_only", "incompatible"))
            self.assertIn("reason", d)
        # original -> generated strategy map (provenance).
        omap = prov["original_to_generated_strategy_map"]
        self.assertEqual({int(k) for k in omap}, set(included))
        # Generated numbers are contiguous 1..N.
        self.assertEqual(sorted(omap.values()), list(range(1, len(included) + 1)))

    def test_sidecar_generated_strategy_maps_opt_lines(self):
        # The generated_strategy numbers in the sidecar match the strategy=N
        # numbers actually present in the .opt, and the original->generated map
        # is consistent with the chains[] records.
        opt_strats = sorted({int(n) for n in re.findall(r"strategy=(\d+)", self.val)})
        side_strats = sorted(ch["generated_strategy"] for ch in self.sidecar["chains"])
        self.assertEqual(opt_strats, side_strats)
        omap = self.sidecar["provenance"]["original_to_generated_strategy_map"]
        for ch in self.sidecar["chains"]:
            self.assertEqual(omap[str(ch["original_strategy_number"])],
                             ch["generated_strategy"])

    def test_parser_strategy_count_matches_chain_steps(self):
        # Multi-step chains (e.g. original strategy 10 = fake+multisplit) share
        # one :strategy=N, so the number of --lua-desync=...:strategy=N lines
        # for that N equals the chain's step count.  Count lines per generated
        # strategy in the .opt and assert the 2-step chain has 2 lines.
        per_strategy: dict[int, int] = {}
        for line in self.text.splitlines():
            for n in re.findall(r"strategy=(\d+)", line):
                per_strategy[int(n)] = per_strategy.get(int(n), 0) + 1
        # Every generated strategy has at least one line.
        for ch in self.sidecar["chains"]:
            self.assertGreaterEqual(per_strategy[ch["generated_strategy"]], 1)
        # The 2-step chain (original strategy 10 -> generated 5: fake+multisplit).
        gen10 = next(ch for ch in self.sidecar["chains"]
                     if ch["original_strategy_number"] == 10)
        self.assertEqual(per_strategy[gen10["generated_strategy"]], 2)
        # All other included chains are single-step (the original pool has only
        # one multi-step circular-compatible chain: strategy 10).
        multi = {n for n, c in per_strategy.items() if c > 1}
        self.assertEqual(multi, {gen10["generated_strategy"]})


@unittest.skipUnless(_has_inputs(), "pinned repo or original preset absent")
class TestOriginalPoolParser(unittest.TestCase):
    """The parse_original_pool helper recovers all 29 strategies in order."""

    def test_parses_29_strategies_in_order(self):
        text = ORIG_PRESET.read_text(encoding="utf-8")
        strategies = IMP.parse_original_pool(text)
        self.assertEqual(len(strategies), 29)
        self.assertEqual([s["original_strategy"] for s in strategies],
                         list(range(1, 30)))

    def test_strategy_1_is_send_syndata_hostfakesplit_multi(self):
        text = ORIG_PRESET.read_text(encoding="utf-8")
        strategies = IMP.parse_original_pool(text)
        s1 = next(s for s in strategies if s["original_strategy"] == 1)
        funcs = [st["func"] for st in s1["steps"]]
        self.assertEqual(funcs, ["send", "syndata", "hostfakesplit_multi"])

    def test_strategy_10_is_fake_plus_multisplit_two_steps(self):
        # Original strategy 10 = fake + multisplit on one logical line (2 steps
        # sharing strategy=10).
        text = ORIG_PRESET.read_text(encoding="utf-8")
        strategies = IMP.parse_original_pool(text)
        s10 = next(s for s in strategies if s["original_strategy"] == 10)
        self.assertEqual(len(s10["steps"]), 2)
        self.assertEqual([st["func"] for st in s10["steps"]], ["fake", "multisplit"])

    def test_classification_matches_doc(self):
        # docs/ORCHESTRA_ORIGINAL_TLS_POOL.md §5: 25 circular-compatible, 4
        # static-only, 0 incompatible -- EXCEPT strategy 2 (hostfakesplit_multi)
        # is in the unshipped zapret-multishake.lua, so it is circular-
        # incompatible (the doc §3 errs in placing hostfakesplit_multi in
        # custom_funcs.lua).  Net: 24 circular-compatible, 4 static-only (1 is
        # reclassified incompatible), 1 incompatible -> 24+4+1 = 29... wait:
        # strategy 1 (send+syndata+hostfakesplit_multi) is incompatible
        # (unavailable func takes precedence over static-only); strategies 4,6,7
        # are static-only; strategy 2 is incompatible.  So 24 circular, 3
        # static-only, 2 incompatible.
        text = ORIG_PRESET.read_text(encoding="utf-8")
        strategies = IMP.parse_original_pool(text)
        classes = {s["original_strategy"]:
                   IMP._classify_circular_compatibility(s["steps"], IMP._PINNED_CUSTOM_FUNCS)
                   for s in strategies}
        cc = sum(1 for v in classes.values() if v == "circular_compatible")
        so = sum(1 for v in classes.values() if v == "static_only")
        ic = sum(1 for v in classes.values() if v == "incompatible")
        self.assertEqual(cc + so + ic, 29)
        self.assertEqual(cc, 24)
        self.assertEqual(so, 3)   # strategies 4, 6, 7
        self.assertEqual(ic, 2)   # strategies 1, 2 (hostfakesplit_multi unshipped)
        self.assertEqual({n for n, v in classes.items() if v == "incompatible"},
                         {1, 2})
        self.assertEqual({n for n, v in classes.items() if v == "static_only"},
                         {4, 6, 7})


@unittest.skipUnless(_has_inputs(), "pinned repo or original preset absent")
class TestOriginalPoolDeterminism(unittest.TestCase):
    """Re-generating the original pool is byte-identical."""

    def test_build_original_pool_is_pure_deterministic(self):
        r1 = IMP.build_original_pool(ORIG_PRESET, IMP._PINNED_CUSTOM_FUNCS)
        r2 = IMP.build_original_pool(ORIG_PRESET, IMP._PINNED_CUSTOM_FUNCS)
        self.assertEqual(r1["opt"], r2["opt"])
        self.assertEqual(json.dumps(r1["sidecar"], sort_keys=True, ensure_ascii=False),
                         json.dumps(r2["sidecar"], sort_keys=True, ensure_ascii=False))

    def test_on_disk_matches_fresh_build(self):
        # The committed .opt/.json must match a fresh build (build_catalog
        # populates _PINNED_CUSTOM_FUNCS, so build via the full pipeline).
        IMP.build_catalog(REPO_ROOT)  # populates _PINNED_CUSTOM_FUNCS
        r = IMP.build_original_pool(ORIG_PRESET, IMP._PINNED_CUSTOM_FUNCS)
        self.assertEqual(ORIG_OPT.read_bytes(), r["opt"].encode("utf-8"))
        fresh = json.dumps(r["sidecar"], indent=2, sort_keys=True,
                           ensure_ascii=False).encode("utf-8") + b"\n"
        self.assertEqual(ORIG_JSON.read_bytes(), fresh)


@unittest.skipUnless(_has_inputs(), "pinned repo or original preset absent")
class TestOriginalPoolRegression(unittest.TestCase):
    """The existing discord-adaptive.opt (2-strategy) and the static
    discord-v5.opt are unchanged by the original-pool addition."""

    def test_existing_discord_adaptive_opt_unchanged(self):
        # The 2-strategy profile still has exactly strategy 1 + 2 (Default old
        # + Default v5) and no more.
        text = ADAPTIVE_OPT.read_text(encoding="utf-8")
        val = P.extract(text).value
        distinct = sorted({int(n) for n in re.findall(r"strategy=(\d+)", val)})
        self.assertEqual(distinct, [1, 2])
        self.assertIn("send:repeats=2", val)
        self.assertIn("send:repeats=3", val)
        self.assertIn("syndata:blob=tls_google", val)

    def test_static_discord_v5_still_validates(self):
        # The native fallback still parses, has no circular_quality, no strategy=.
        text = STATIC_OPT.read_text(encoding="utf-8")
        self.assertTrue(P.validate_round_trip(text))
        val = P.extract(text).value
        self.assertNotIn("circular_quality", val)
        self.assertIsNone(re.search(r"strategy=\d", val))
        self.assertIn("send:repeats=3", val)
        self.assertIn("syndata:blob=tls_google", val)
        self.assertIn("--ipset=/etc/zapret2-orchestra/lists/ipset-discord.txt", val)

    def test_original_pool_is_additional_not_replacing(self):
        # Both profiles coexist.
        self.assertTrue(ADAPTIVE_OPT.exists())
        self.assertTrue(ORIG_OPT.exists())
        self.assertNotEqual(ADAPTIVE_OPT, ORIG_OPT)
        # The original pool has many more strategies than the 2-strategy adaptive.
        a_val = P.extract(ADAPTIVE_OPT.read_text("utf-8")).value
        o_val = P.extract(ORIG_OPT.read_text("utf-8")).value
        a_n = len({int(n) for n in re.findall(r"strategy=(\d+)", a_val)})
        o_n = len({int(n) for n in re.findall(r"strategy=(\d+)", o_val)})
        self.assertEqual(a_n, 2)
        self.assertGreater(o_n, a_n)


if __name__ == "__main__":
    unittest.main(verbosity=2)
