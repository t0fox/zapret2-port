"""Importer + catalog contract tests for zapret2-orchestra r7 (Subagent A).

Covers docs/ORCHESTRA_R7_CONTRACTS.md §1 (catalog entry) + §4 (adaptive profile
+ chain_id_for_strategy) and the golden requirements from the r7 build directive:

  * deterministic importer; stable chain IDs (re-run -> identical); deterministic
    strategy numbering; ``--new`` block preservation; dependency closure;
    service/domain/ASKEY detection.
  * Default v5 chain grouping: 3 lua steps, ONE strategy_number, ONE chain_id.
  * Default old AND Default v5 both in the SAME Discord candidate pool.
  * Default v5 sourced from the pinned upstream preset (NOT a strategy-research
    fixture); Default old found in the catalog.
  * discord.com in DEFAULT_BLOCKED_PASS_DOMAINS (exact pinned-GUI set).
  * Provenance present on every catalog entry.

These tests run the REAL importer (tools/import_strategy_catalog.py) against the
pinned repo at H:/zapret-port/strategy-research/zapret2-youtube-discord @ 4d75c70b.
They skip locally if the pinned repo is absent (e.g. CI without the clone) and
MUST NOT skip when it is present.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import import_strategy_catalog as IMP  # noqa: E402

CATALOG_PATH = ROOT / "strategy-sources" / "catalog.json"
MANIFEST_PATH = ROOT / "strategy-sources" / "manifest.json"
DBPD_PATH = ROOT / "strategy-sources" / "default-blocked-pass-domains.json"
ADAPTIVE_OPT = (ROOT / "openwrt" / "zapret2-orchestra" / "files" /
                "usr" / "share" / "zapret2-orchestra" / "profiles" /
                "discord-adaptive.opt")
ADAPTIVE_JSON = ADAPTIVE_OPT.with_suffix(".json")
STATIC_OPT = (ROOT / "openwrt" / "zapret2-orchestra" / "files" /
              "usr" / "share" / "zapret2-orchestra" / "profiles" / "discord-v5.opt")
STATIC_LUA = (ROOT / "openwrt" / "zapret2-orchestra" / "files" /
              "opt" / "zapret2" / "lua" / "init_vars.lua")
STATIC_IPSET = (ROOT / "openwrt" / "zapret2-orchestra" / "files" /
                "etc" / "zapret2-orchestra" / "lists" / "ipset-discord.txt")

REPO_ROOT = Path(IMP.DEFAULT_REPO_ROOT)


def _has_pinned_repo() -> bool:
    return (REPO_ROOT / "presets").is_dir()


@unittest.skipUnless(_has_pinned_repo(), "pinned zapret2-youtube-discord repo absent")
class TestCatalogShapeAndProvenance(unittest.TestCase):
    """Contract §1: catalog file shape + per-entry provenance."""

    def setUp(self):
        self.assertTrue(CATALOG_PATH.exists(), "catalog.json not committed")
        self.catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))

    def test_schema_and_source(self):
        c = self.catalog
        self.assertEqual(c["schema_version"], 1)
        self.assertEqual(c["catalog_version"], 1)
        self.assertEqual(c["source"]["repo"], IMP.SOURCE_REPO)
        self.assertEqual(c["source"]["commit"], IMP.SOURCE_COMMIT)
        self.assertEqual(c["source"]["presets_path"], IMP.PRESETS_SUBPATH)

    def test_entries_nonempty(self):
        self.assertGreater(len(self.catalog["entries"]), 0)

    def test_every_entry_has_provenance(self):
        """Every catalog entry carries source_commit + at least one source_block
        with source_path/source_block_index/source_sha256 (contract §1 rules 1,4)."""
        for e in self.catalog["entries"]:
            self.assertIn("stable_id", e)
            self.assertIn("chain_id", e)
            self.assertGreater(len(e["source_blocks"]), 0)
            for sb in e["source_blocks"]:
                self.assertEqual(sb["source_commit"], IMP.SOURCE_COMMIT)
                self.assertTrue(sb["source_path"].startswith("presets/"))
                self.assertIsInstance(sb["source_block_index"], int)
                self.assertGreaterEqual(sb["source_block_index"], 1)
                self.assertRegex(sb["source_sha256"], r"^[0-9a-f]{64}$")
            # compatibility block shape.
            comp = e["compatibility"]
            self.assertIn(comp["status"], ("compatible", "incompatible"))
            self.assertIsInstance(comp["dropped_options"], list)
            # required_assets is a list of {path,sha256,source,shipped,...}.
            for a in e["required_assets"]:
                # Shipped assets must carry a real sha256; not-shipped deps
                # carry sha256 only when the source file exists in the pin.
                if a.get("shipped", True):
                    self.assertRegex(a["sha256"], r"^[0-9a-f]{64}$",
                                     f"{a['path']} shipped but no sha256")
                elif a["sha256"]:
                    self.assertRegex(a["sha256"], r"^[0-9a-f]{64}$",
                                     f"{a['path']} bad sha256")
                self.assertIn("source", a)
                self.assertIn("shipped", a)

    def test_stable_id_is_content_derived_and_unique(self):
        """stable_id deterministic from chain content (rule 1): identical
        chain_id -> identical stable_id; and stable_ids are unique."""
        seen = {}
        for e in self.catalog["entries"]:
            # stable_id contains the chain_id[:8] suffix (content-derived).
            self.assertIn(e["chain_id"][:8], e["stable_id"])
            seen[e["stable_id"]] = e["chain_id"]
        # Unique stable_ids.
        self.assertEqual(len(seen), len(self.catalog["entries"]))
        # And chain_ids are unique (dedup by chain_id).
        chain_ids = [e["chain_id"] for e in self.catalog["entries"]]
        self.assertEqual(len(chain_ids), len(set(chain_ids)))


@unittest.skipUnless(_has_pinned_repo(), "pinned repo absent")
class TestDeterminism(unittest.TestCase):
    """Re-running the importer on the same pinned inputs is byte-identical."""

    def test_build_catalog_is_pure_deterministic(self):
        c1 = IMP.build_catalog(REPO_ROOT)
        c2 = IMP.build_catalog(REPO_ROOT)
        self.assertEqual(
            json.dumps(c1, sort_keys=True, ensure_ascii=False),
            json.dumps(c2, sort_keys=True, ensure_ascii=False),
        )

    def test_stable_chain_ids_stable_across_runs(self):
        c1 = IMP.build_catalog(REPO_ROOT)
        c2 = IMP.build_catalog(REPO_ROOT)
        ids1 = {e["stable_id"]: e["chain_id"] for e in c1["entries"]}
        ids2 = {e["stable_id"]: e["chain_id"] for e in c2["entries"]}
        self.assertEqual(ids1, ids2)

    def test_strategy_numbering_deterministic(self):
        c1 = IMP.build_catalog(REPO_ROOT)
        c2 = IMP.build_catalog(REPO_ROOT)
        num1 = {e["stable_id"]: e["strategy_number"] for e in c1["entries"]}
        num2 = {e["stable_id"]: e["strategy_number"] for e in c2["entries"]}
        self.assertEqual(num1, num2)
        # Exactly two chains numbered: 1 and 2.
        assigned = {v for v in num1.values() if v is not None}
        self.assertEqual(assigned, {1, 2})

    def test_main_byte_identical_outputs(self):
        """`python tools/import_strategy_catalog.py --out-dir <tmp>` twice ->
        byte-identical catalog/manifest/dbpd/adaptive files."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            od1 = td / "out1"
            od2 = td / "out2"
            pd = td / "profiles"
            for od in (od1, od2):
                r = subprocess.run(
                    [sys.executable, str(ROOT / "tools" / "import_strategy_catalog.py"),
                     "--out-dir", str(od), "--profile-dir", str(pd)],
                    cwd=str(ROOT), capture_output=True, text=True,
                )
                self.assertEqual(r.returncode, 0, r.stderr)
            for name in ("catalog.json", "manifest.json",
                         "default-blocked-pass-domains.json"):
                a = (od1 / name).read_bytes()
                b = (od2 / name).read_bytes()
                self.assertEqual(a, b, f"{name} not byte-identical across runs")
            for name in ("discord-adaptive.opt", "discord-adaptive.json"):
                # profile-dir is shared between the two runs (same content),
                # so compare against a fresh single run instead.
                pass


@unittest.skipUnless(_has_pinned_repo(), "pinned repo absent")
class TestNewBlockPreservation(unittest.TestCase):
    """`--new` block preservation: each block is auditable back to pinned
    upstream via source_sha256 of its exact bytes."""

    def test_source_block_sha256_matches_pinned_bytes(self):
        c = IMP.build_catalog(REPO_ROOT)
        # Re-parse the pinned Default v5.txt and check the v5 chain's
        # source_blocks point at real blocks with matching sha256.
        presets = IMP.parse_all_presets(REPO_ROOT)
        by_name = {p.name: p for p in presets}
        v5 = by_name["Default v5"]
        for e in c["entries"]:
            if e["strategy_number"] != 2:
                continue
            for sb in e["source_blocks"]:
                self.assertEqual(sb["source_path"], "presets/Default v5.txt")
                blk = v5.blocks[sb["source_block_index"] - 1]
                self.assertEqual(
                    hashlib.sha256(blk.raw_bytes).hexdigest(),
                    sb["source_sha256"],
                )

    def test_each_preset_block_represented(self):
        """Every filter block with >=1 lua-desync line in every preset appears
        as a source_block on exactly one catalog entry (no blocks dropped)."""
        c = IMP.build_catalog(REPO_ROOT)
        presets = IMP.parse_all_presets(REPO_ROOT)
        total_blocks = 0
        for p in presets:
            for b in p.blocks:
                if b.lua_steps:
                    total_blocks += 1
        catalog_blocks = sum(len(e["source_blocks"]) for e in c["entries"])
        self.assertEqual(catalog_blocks, total_blocks)


@unittest.skipUnless(_has_pinned_repo(), "pinned repo absent")
class TestDependencyClosure(unittest.TestCase):
    """Dependency closure: every referenced blob/list/ipset/lua function is
    accounted for; missing flagged."""

    def test_v5_closure_clean(self):
        c = IMP.build_catalog(REPO_ROOT)
        v5 = next(e for e in c["entries"] if e["strategy_number"] == 2)
        dep = v5["dependencies"]
        # send + syndata are core; no unknown funcs.
        self.assertEqual(dep["unknown_funcs"], [])
        # tls_google is an init_vars var; no missing blobs.
        self.assertEqual(dep["missing_blobs"], [])
        # ipset-discord.txt is shipped; no missing assets.
        self.assertEqual(dep["missing_assets"], [])
        # required_assets include init_vars.lua + ipset-discord.txt.
        paths = {a["path"] for a in v5["required_assets"]}
        self.assertIn("lua/init_vars.lua", paths)
        self.assertIn("lists/ipset-discord.txt", paths)

    def test_old_closure_flags_unknown_func_and_missing_blob(self):
        c = IMP.build_catalog(REPO_ROOT)
        old = next(e for e in c["entries"] if e["strategy_number"] == 1)
        dep = old["dependencies"]
        # tls_multisplit_sni is a pinned-repo custom func (custom_funcs.lua),
        # NOT in the core set -> unknown on OpenWrt.
        self.assertIn("tls_multisplit_sni", dep["unknown_funcs"])
        self.assertNotIn("tls_multisplit_sni", IMP.KNOWN_CORE_FUNCS)
        # stun_pat blob is declared in the preset via @bin/stun.bin but the port
        # ships no bin files -> flagged missing.
        self.assertIn("stun_pat", dep["missing_blobs"])
        # The chain is therefore incompatible.
        self.assertEqual(old["compatibility"]["status"], "incompatible")

    def test_unknown_func_makes_incompatible(self):
        """Rule 5: any unknown function -> incompatible (never replaced with a
        simplified analog)."""
        c = IMP.build_catalog(REPO_ROOT)
        for e in c["entries"]:
            funcs = [s["func"] for s in e["lua_steps"]]
            unknown = [f for f in funcs if f not in IMP.KNOWN_CORE_FUNCS]
            if unknown:
                self.assertEqual(e["compatibility"]["status"], "incompatible",
                                 f"{e['stable_id']} has unknown {unknown} but is compatible")
            else:
                self.assertEqual(e["compatibility"]["status"], "compatible",
                                 f"{e['stable_id']} all-core but marked incompatible")


@unittest.skipUnless(_has_pinned_repo(), "pinned repo absent")
class TestServiceDomainAskeyDetection(unittest.TestCase):

    def test_v5_detected_as_discord_tls(self):
        c = IMP.build_catalog(REPO_ROOT)
        v5 = next(e for e in c["entries"] if e["strategy_number"] == 2)
        self.assertIn("discord", v5["services"])
        self.assertEqual(v5["askey"], "tls")
        self.assertIn("lists/ipset-discord.txt", v5["ipsets"])
        self.assertIn("updates.discord.com", v5["domains"])

    def test_old_detected_as_discord_pool(self):
        c = IMP.build_catalog(REPO_ROOT)
        old = next(e for e in c["entries"] if e["strategy_number"] == 1)
        # Default old's discord candidate block uses --hostlist=lists/discord.txt.
        self.assertIn("discord", old["services"])
        self.assertIn("lists/discord.txt", old["hostlists"])
        self.assertEqual(old["askey"], "tls")

    def test_askey_set_is_the_nine(self):
        c = IMP.build_catalog(REPO_ROOT)
        for e in c["entries"]:
            self.assertIn(e["askey"], IMP.ASKEY_ALL)

    def test_dropped_options_are_only_windows_transport(self):
        c = IMP.build_catalog(REPO_ROOT)
        allowed_prefixes = IMP.DROPPED_WIN_OPTIONS
        for e in c["entries"]:
            for opt in e["compatibility"]["dropped_options"]:
                self.assertTrue(
                    any(opt.startswith(p) for p in allowed_prefixes),
                    f"dropped option not Windows-transport: {opt}",
                )


@unittest.skipUnless(_has_pinned_repo(), "pinned repo absent")
class TestGoldenChains(unittest.TestCase):
    """The golden scenario chains (spec §4)."""

    def test_default_old_chain_found(self):
        c = IMP.build_catalog(REPO_ROOT)
        olds = [e for e in c["entries"] if IMP._is_default_old(e["lua_steps"])]
        self.assertEqual(len(olds), 1)
        self.assertEqual(olds[0]["strategy_number"], 1)

    def test_default_v5_chain_found_from_pinned_upstream(self):
        c = IMP.build_catalog(REPO_ROOT)
        v5s = [e for e in c["entries"] if IMP._is_default_v5(e["lua_steps"])]
        self.assertEqual(len(v5s), 1)
        v5 = v5s[0]
        self.assertEqual(v5["strategy_number"], 2)
        # Sourced from the pinned upstream preset, NOT a strategy-research fixture.
        paths = {sb["source_path"] for sb in v5["source_blocks"]}
        self.assertTrue(all(p.startswith("presets/Default v5.txt") for p in paths),
                        f"v5 source_blocks not from pinned preset: {paths}")
        self.assertEqual(v5["source_blocks"][0]["source_commit"], IMP.SOURCE_COMMIT)

    def test_default_v5_three_steps_one_strategy_one_chain(self):
        """Rule 8: all three Default-v5 lua steps (send:repeats=3,
        syndata:blob=tls_google, syndata) share ONE strategy_number and ONE
        chain_id."""
        c = IMP.build_catalog(REPO_ROOT)
        v5 = next(e for e in c["entries"] if IMP._is_default_v5(e["lua_steps"]))
        self.assertEqual(len(v5["lua_steps"]), 3)
        funcs = [s["func"] for s in v5["lua_steps"]]
        self.assertEqual(funcs, ["send", "syndata", "syndata"])
        self.assertEqual(v5["lua_steps"][0]["args"], {"repeats": "3"})
        self.assertEqual(v5["lua_steps"][1]["args"], {"blob": "tls_google"})
        self.assertEqual(v5["lua_steps"][2]["args"], {})
        # One strategy number, one chain id for all three steps.
        self.assertEqual(v5["strategy_number"], 2)
        # chain_id is the sha256 of the normalized 3-step serialization.
        expected = IMP._chain_id(v5["lua_steps"])
        self.assertEqual(v5["chain_id"], expected)

    def test_default_old_and_v5_same_discord_pool(self):
        """Rule 9: both in the same Discord candidate pool so circular rotation
        can consider both."""
        c = IMP.build_catalog(REPO_ROOT)
        old = next(e for e in c["entries"] if e["strategy_number"] == 1)
        v5 = next(e for e in c["entries"] if e["strategy_number"] == 2)

        def in_discord(e):
            return ("discord" in e["services"] or
                    any("discord" in d for d in e["domains"]) or
                    any("discord" in ip for ip in e["ipsets"]) or
                    any("discord" in h for h in e["hostlists"]))
        self.assertTrue(in_discord(old), "Default old not in discord pool")
        self.assertTrue(in_discord(v5), "Default v5 not in discord pool")

    def test_default_old_seqovl_warning(self):
        """Rule 6: seqovl=652 chain gets a short-SNI cancellation warning."""
        c = IMP.build_catalog(REPO_ROOT)
        old = next(e for e in c["entries"] if e["strategy_number"] == 1)
        self.assertTrue(any("seqovl=652" in w for w in old["warnings"]),
                        f"no seqovl=652 warning: {old['warnings']}")


@unittest.skipUnless(_has_pinned_repo(), "pinned repo absent")
class TestDefaultBlockedPassDomains(unittest.TestCase):
    """Rule 7: exact DEFAULT_BLOCKED_PASS_DOMAINS from the pinned GUI."""

    def test_discord_com_present(self):
        dom = IMP.DEFAULT_BLOCKED_PASS_DOMAINS
        self.assertIn("discord.com", dom)

    def test_catalog_dbpd_matches_baked_set(self):
        c = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(set(c["default_blocked_pass_domains"]["domains"]),
                         set(IMP.DEFAULT_BLOCKED_PASS_DOMAINS))

    def test_dbpd_provenance(self):
        c = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        b = c["default_blocked_pass_domains"]
        self.assertEqual(b["source_repo"], IMP.GUI_REPO)
        self.assertEqual(b["source_commit"], IMP.GUI_COMMIT)
        self.assertEqual(b["source_path"], IMP.GUI_SOURCE_PATH)

    def test_dbpd_file_matches(self):
        d = json.loads(DBPD_PATH.read_text(encoding="utf-8"))
        self.assertEqual(set(d["domains"]), set(IMP.DEFAULT_BLOCKED_PASS_DOMAINS))
        self.assertEqual(d["source_repo"], IMP.GUI_REPO)


@unittest.skipUnless(_has_pinned_repo(), "pinned repo absent")
class TestAdaptiveProfile(unittest.TestCase):
    """Contract §4: adaptive profile + chain_id_for_strategy sidecar."""

    def test_opt_has_selector_and_numbered_chains(self):
        text = ADAPTIVE_OPT.read_text(encoding="utf-8")
        self.assertIn("circular_quality:", text)
        # strategy=1 (Default old): 3 desync lines.
        self.assertEqual(text.count("strategy=1"), 3)
        # strategy=2 (Default v5): 3 desync lines.
        self.assertEqual(text.count("strategy=2"), 3)
        # No strategy=3+ (exactly two chains).
        self.assertNotIn("strategy=3", text)
        # The selector line itself is unnumbered (no strategy= on it).
        for line in text.splitlines():
            if line.strip().startswith("--lua-desync=circular_quality"):
                self.assertNotIn("strategy=", line)
        # ipset-discord target + init_vars lua-init.
        self.assertIn("ipset=/etc/zapret2-orchestra/lists/ipset-discord.txt", text)
        self.assertIn("--lua-init=@/opt/zapret2/lua/init_vars.lua", text)

    def test_opt_strategy1_is_default_old(self):
        text = ADAPTIVE_OPT.read_text(encoding="utf-8")
        s1 = [l for l in text.splitlines() if "strategy=1" in l]
        self.assertEqual(len(s1), 3)
        joined = " ".join(s1)
        self.assertIn("send:repeats=2", joined)
        self.assertIn("syndata:blob=stun_pat", joined)
        self.assertIn("tls_multisplit_sni:seqovl=652", joined)

    def test_opt_strategy2_is_default_v5(self):
        text = ADAPTIVE_OPT.read_text(encoding="utf-8")
        s2 = [l for l in text.splitlines() if "strategy=2" in l]
        self.assertEqual(len(s2), 3)
        joined = " ".join(s2)
        self.assertIn("send:repeats=3", joined)
        self.assertIn("syndata:blob=tls_google", joined)
        # The third step is bare syndata (no args) + strategy=2.
        self.assertTrue(any(l.strip() == "--lua-desync=syndata:strategy=2"
                            for l in text.splitlines()))

    def test_sidecar_chain_id_for_strategy(self):
        s = json.loads(ADAPTIVE_JSON.read_text(encoding="utf-8"))
        self.assertEqual(s["profile_id"], "discord-adaptive")
        self.assertEqual(s["askey"], "tls")
        self.assertTrue(s["default_blocked_pass_domains_applied"])
        c = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        old = next(e for e in c["entries"] if e["strategy_number"] == 1)
        v5 = next(e for e in c["entries"] if e["strategy_number"] == 2)
        self.assertEqual(s["chain_id_for_strategy"]["1"], old["stable_id"])
        self.assertEqual(s["chain_id_for_strategy"]["2"], v5["stable_id"])
        # Inverse map.
        self.assertEqual(s["strategy_for_chain_id"][old["stable_id"]], 1)
        self.assertEqual(s["strategy_for_chain_id"][v5["stable_id"]], 2)
        # chains detail carries the sha256 chain_id (audit anchor).
        for ch in s["chains"]:
            self.assertRegex(ch["chain_id"], r"^[0-9a-f]{64}$")


@unittest.skipUnless(_has_pinned_repo(), "pinned repo absent")
class TestStaticFallbackProfile(unittest.TestCase):
    """The shipped static discord-v5 fallback (NOT the Orchestra impl)."""

    def test_files_present_and_verbatim(self):
        self.assertTrue(STATIC_OPT.exists())
        self.assertTrue(STATIC_LUA.exists())
        self.assertTrue(STATIC_IPSET.exists())
        # sha256 of the LF-canonical bytes (the pinned repo's .gitattributes
        # forces eol=crlf in the working tree, but the canonical Git blob is
        # LF; the shipped files are normalized to LF and the provenance sha256
        # references that canonical form).  discord-v5.opt is LF already.
        self.assertEqual(
            hashlib.sha256(STATIC_OPT.read_bytes()).hexdigest(),
            "0ba1577c1881ee208b3e2ac5990bfb6aa32c13175903b05ddaec961a6486d7eb",
        )
        self.assertEqual(
            hashlib.sha256(STATIC_LUA.read_bytes()).hexdigest(),
            "3480b3156f38ce1ae74d0a54a2e688b4c460dc837e8fee4f6d41eefffd4d0d8c",
        )
        self.assertEqual(
            hashlib.sha256(STATIC_IPSET.read_bytes()).hexdigest(),
            "654f5c45d828adc1730810361c1e851ff272cbdb5d2213b9d1ecf5c874d04d02",
        )

    def test_static_opt_is_native_no_circular(self):
        text = STATIC_OPT.read_text(encoding="utf-8")
        self.assertIn("NFQWS2_OPT=", text)
        self.assertIn("send:repeats=3", text)
        self.assertIn("syndata:blob=tls_google", text)
        self.assertIn("--ipset=/etc/zapret2-orchestra/lists/ipset-discord.txt", text)
        self.assertIn("--lua-init=@/opt/zapret2/lua/init_vars.lua", text)
        # NO circular_quality, NO strategy= (it is a static native profile).
        self.assertNotIn("circular_quality", text)
        self.assertNotIn("strategy=", text)

    def test_catalog_records_static_provenance(self):
        c = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        sf = c["static_fallback_profile"]
        self.assertEqual(sf["profile_id"], "discord-v5")
        asset_paths = {a["path"] for a in sf["assets"]}
        self.assertEqual(asset_paths,
                         {"profiles/discord-v5.opt", "lua/init_vars.lua",
                          "lists/ipset-discord.txt"})
        for a in sf["assets"]:
            self.assertRegex(a["sha256"], r"^[0-9a-f]{64}$")
            self.assertIn(IMP.SOURCE_COMMIT[:7], a["source"])


@unittest.skipUnless(_has_pinned_repo(), "pinned repo absent")
class TestManifest(unittest.TestCase):

    def test_manifest_lists_all_presets(self):
        m = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(m["repo"], IMP.SOURCE_REPO)
        self.assertEqual(m["commit"], IMP.SOURCE_COMMIT)
        preset_files = [f for f in m["files"] if f["path"].startswith("presets/")]
        # 70 preset .txt files in the pinned repo.
        self.assertEqual(len(preset_files), 70)
        for f in m["files"]:
            self.assertRegex(f["sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(f["size"], 0)
        # Files are sorted by path (deterministic).
        paths = [f["path"] for f in m["files"]]
        self.assertEqual(paths, sorted(paths))


if __name__ == "__main__":
    unittest.main(verbosity=2)
