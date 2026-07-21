"""Tests for the zapret2-orchestra preload generator.

Four layers:
  1. Static analysis of the ucode generator source (always runs).
  2. A Python oracle that replicates the generator algorithm (rendering and
     the djb2 31-bit hash) and verifies determinism and output shape against
     the actual seed files (always runs).
  3. A golden-fixture test that compares the Python oracle output to a
     hand-written expected preload.lua and whitelist.txt under
     tests/fixtures/. The golden files are independent of the oracle so a
     shared bug in the Python and ucode implementations is caught (always
     runs).
  4. Runtime tests that execute the real ucode generator and the `check`
     mode in a temporary directory. Skipped when ``ucode`` is not on PATH.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "openwrt/zapret2-orchestra/files/usr/share/zapret2-orchestra/generate-preload.uc"
STATE_SRC = ROOT / "etc" / "zapret2-orchestra"
FIXTURE_SEEDS = ROOT / "tests/fixtures/seeds"
FIXTURE_EXPECTED = ROOT / "tests/fixtures/expected"
GOLDEN_PRELOAD = FIXTURE_EXPECTED / "preload.lua"
GOLDEN_WHITELIST = FIXTURE_EXPECTED / "whitelist.txt"

EXPECTED_SEEDS = ("blocked.json", "learned.json", "manual-locks.json", "whitelist.json")
ALLOWED_PRELOAD_CALLS = {
    "slm_preload_blocked",
    "slm_preload_locked",
    "slm_preload_history",
}
DEFAULT_STATE_DIR = "/etc/zapret2-orchestra"
GOLDEN_STATE_DIR = "/etc/zapret2-orchestra"


def lua_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def lua_int_list(values: list[int]) -> str:
    return "{" + ", ".join(str(v) for v in values) + "}"


def sorted_unique_strategies(values: list, ctx: str) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for v in values:
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError(f"{ctx}: expected integer")
        if v < 1:
            raise ValueError(f"{ctx}: strategy must be a positive integer")
        if v not in seen:
            seen.add(v)
            out.append(v)
    return sorted(out)


def sorted_unique_hosts(values: list) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for h in values:
        if not isinstance(h, str) or len(h) == 0:
            raise ValueError("whitelist: host must be a non-empty string")
        if h not in seen:
            seen.add(h)
            out.append(h)
    return sorted(out)


def sorted_keys(d: dict) -> list[str]:
    return sorted((d or {}).keys())


def render_whitelist_table(seeds: dict) -> str:
    hosts = sorted_unique_hosts(seeds["whitelist"].get("hosts", []))
    if len(hosts) == 0:
        return "ORCHESTRA_WHITELIST = {}"
    entries = [f"[{lua_quote(h)}]=true" for h in hosts]
    return "ORCHESTRA_WHITELIST = { " + ", ".join(entries) + " }"


def render_blocked(seeds: dict) -> list[str]:
    lines: list[str] = []
    for askey in sorted_keys(seeds["blocked"].get("protocols", {})):
        bp = seeds["blocked"]["protocols"][askey] or {}
        global_vals = sorted_unique_strategies(bp.get("global", []), f"blocked.{askey}.global")
        if global_vals:
            lines.append(f"slm_preload_blocked({lua_quote(askey)}, \"*\", {lua_int_list(global_vals)})")
        for host in sorted_keys(bp.get("hosts", {})):
            vals = sorted_unique_strategies(bp["hosts"][host], f"blocked.{askey}.{host}")
            if vals:
                lines.append(f"slm_preload_blocked({lua_quote(askey)}, {lua_quote(host)}, {lua_int_list(vals)})")
    return lines


def render_learned(seeds: dict) -> list[str]:
    lines: list[str] = []
    for askey in sorted_keys(seeds["learned"].get("protocols", {})):
        lhosts_map = seeds["learned"]["protocols"][askey] or {}
        for host in sorted_keys(lhosts_map):
            rec = lhosts_map[host] or {}
            for skey in sorted_keys(rec.get("strategies", {})):
                s = int(skey)
                if s < 1:
                    raise ValueError(f"learned.{askey}.{host}: strategy must be a positive integer")
                cnt = rec["strategies"][skey] or {}
                succ = int(cnt.get("successes", 0))
                fcount = int(cnt.get("failures", 0))
                lines.append(
                    f"slm_preload_history({lua_quote(askey)}, {lua_quote(host)}, {s}, {succ}, {fcount})"
                )
            if rec.get("auto_lock") is not None:
                al = int(rec["auto_lock"])
                if al >= 1:
                    lines.append(
                        f"slm_preload_locked({lua_quote(askey)}, {lua_quote(host)}, {al}, false)"
                    )
    return lines


def render_manual_locks(seeds: dict) -> list[str]:
    lines: list[str] = []
    for askey in sorted_keys(seeds["manual_locks"].get("protocols", {})):
        mhosts_map = seeds["manual_locks"]["protocols"][askey] or {}
        for host in sorted_keys(mhosts_map):
            strat = int(mhosts_map[host])
            if strat < 1:
                raise ValueError(f"manual-locks.{askey}.{host}: strategy must be a positive integer")
            lines.append(
                f"slm_preload_locked({lua_quote(askey)}, {lua_quote(host)}, {strat}, true)"
            )
    return lines


def render_preload(seeds: dict, state_dir: str = DEFAULT_STATE_DIR) -> str:
    lines = [
        "-- Auto-generated by zapret2-orchestra preload generator. Do not edit.",
        f"-- Source: {state_dir}/*.json",
        render_whitelist_table(seeds),
    ]
    lines.extend(render_blocked(seeds))
    lines.extend(render_learned(seeds))
    lines.extend(render_manual_locks(seeds))
    return "\n".join(lines) + "\n"


def render_whitelist_txt(seeds: dict) -> str:
    hosts = sorted_unique_hosts(seeds["whitelist"].get("hosts", []))
    if len(hosts) == 0:
        return ""
    return "\n".join(hosts) + "\n"


def hash31(data: str) -> str:
    h = 5381
    for b in data.encode("utf-8"):
        h = (h * 33 + b) & 0x7FFFFFFF
    return f"{h:08x}"


def render_manifest(seeds: dict, state_dir: str = DEFAULT_STATE_DIR) -> dict:
    preload = render_preload(seeds, state_dir)
    whitelist = render_whitelist_txt(seeds)
    return {
        "schema_version": 1,
        "generated_at": 0,
        "state_dir": state_dir,
        "preload": {"bytes": len(preload.encode("utf-8")), "hash": hash31(preload)},
        "whitelist": {"bytes": len(whitelist.encode("utf-8")), "hash": hash31(whitelist)},
    }


def load_seeds(state_dir: Path) -> dict:
    return {
        "blocked": json.loads((state_dir / "blocked.json").read_text(encoding="utf-8")),
        "learned": json.loads((state_dir / "learned.json").read_text(encoding="utf-8")),
        "manual_locks": json.loads((state_dir / "manual-locks.json").read_text(encoding="utf-8")),
        "whitelist": json.loads((state_dir / "whitelist.json").read_text(encoding="utf-8")),
    }


class PreloadGeneratorStaticTest(unittest.TestCase):
    def setUp(self) -> None:
        self.source = GENERATOR.read_text(encoding="utf-8")

    def test_imports_fs_functions_needed_for_atomic_write(self) -> None:
        self.assertIn("import { readfile, writefile, mkdir, rename, unlink, stat } from 'fs';", self.source)

    def test_reads_all_four_seeds_and_validates_schema(self) -> None:
        for seed in EXPECTED_SEEDS:
            self.assertIn(f"read_seed('{seed}')", self.source)
        self.assertIn("schema_version", self.source)
        self.assertIn("schema_version must be 1", self.source)

    def test_uses_atomic_write_with_temp_and_rename_in_same_directory(self) -> None:
        self.assertIn("atomic_write", self.source)
        self.assertIn("'.tmp'", self.source)
        self.assertIn("rename(tmp, path)", self.source)
        self.assertIn("unlink(tmp)", self.source)
        self.assertIn("same filesystem", self.source)

    def test_writes_manifest_last_and_atomically(self) -> None:
        self.assertIn("write_manifest", self.source)
        idx_preload = self.source.index("atomic_write(PRELOAD_FILE")
        idx_whitelist = self.source.index("atomic_write(WHITELIST_FILE")
        idx_manifest_call = self.source.index("write_manifest(preload, whitelist);")
        self.assertLess(idx_preload, idx_whitelist)
        self.assertLess(idx_whitelist, idx_manifest_call)
        self.assertIn("manifest is written LAST", self.source)

    def test_generates_only_allowed_preload_calls(self) -> None:
        for call in ALLOWED_PRELOAD_CALLS:
            self.assertIn(call, self.source)
        self.assertIn("ORCHESTRA_WHITELIST", self.source)

    def test_does_not_write_under_etc_or_invoke_shell(self) -> None:
        for forbidden in ("system(", "popen(", "exec(", "fs.open("):
            self.assertNotIn(forbidden, self.source)
        atomic_targets = [t for t in re.findall(r"atomic_write\(([^,]+),", self.source) if t != "path"]
        self.assertEqual(set(atomic_targets), {"PRELOAD_FILE", "WHITELIST_FILE", "MANIFEST_FILE"})
        for target in atomic_targets:
            self.assertNotIn("/etc/", target)
        self.assertNotIn("/etc/zapret2-orchestra", self.source.replace("'/etc/zapret2-orchestra'", "<STATE>"))

    def test_supports_environment_overrides_for_testing(self) -> None:
        for var in (
            "ORCHESTRA_STATE_DIR",
            "ORCHESTRA_RUNTIME_DIR",
            "ORCHESTRA_PRELOAD_FILE",
            "ORCHESTRA_WHITELIST_FILE",
            "ORCHESTRA_MANIFEST_FILE",
        ):
            self.assertIn(var, self.source)

    def test_supports_generate_and_check_modes(self) -> None:
        self.assertIn("ARGV[0]", self.source)
        self.assertIn("mode == 'generate'", self.source)
        self.assertIn("mode == 'check'", self.source)
        self.assertIn("exit(0)", self.source)
        self.assertIn("die(", self.source)

    def test_paths_default_to_production_locations(self) -> None:
        self.assertIn("'/etc/zapret2-orchestra'", self.source)
        self.assertIn("'/tmp/zapret2-orchestra'", self.source)
        self.assertIn("'/preload.lua'", self.source)
        self.assertIn("'/whitelist.txt'", self.source)
        self.assertIn("'/manifest.json'", self.source)

    def test_hash_function_is_pure_ucode_without_extra_modules(self) -> None:
        self.assertIn("function hash31", self.source)
        self.assertIn("5381", self.source)
        self.assertIn("0x7fffffff", self.source)
        for forbidden_module in ("import { digest", "import { sha", "import { md5", "require("):
            self.assertNotIn(forbidden_module, self.source)


class PreloadOracleTest(unittest.TestCase):
    """Python replica of the ucode generator; verifies determinism and shape."""

    def test_empty_seeds_produce_minimal_valid_preload(self) -> None:
        seeds = load_seeds(STATE_SRC)
        for name in EXPECTED_SEEDS:
            key = name.replace(".json", "").replace("-", "_")
            self.assertEqual(seeds[key]["schema_version"], 1)
        preload = render_preload(seeds)
        self.assertIn("ORCHESTRA_WHITELIST = {}", preload)
        self.assertNotIn("slm_preload_blocked", preload)
        self.assertNotIn("slm_preload_locked", preload)
        self.assertNotIn("slm_preload_history", preload)
        wl = render_whitelist_txt(seeds)
        self.assertEqual(wl, "")

    def test_output_is_deterministic(self) -> None:
        seeds = load_seeds(STATE_SRC)
        self.assertEqual(render_preload(seeds), render_preload(seeds))
        self.assertEqual(render_whitelist_txt(seeds), render_whitelist_txt(seeds))
        self.assertEqual(render_manifest(seeds), render_manifest(seeds))

    def test_generated_preload_contains_no_forbidden_patterns(self) -> None:
        seeds = load_seeds(STATE_SRC)
        preload = render_preload(seeds)
        allowed_prefixes = tuple(ALLOWED_PRELOAD_CALLS) + ("ORCHESTRA_WHITELIST",)
        for line in preload.strip().split("\n"):
            if line.startswith("--"):
                continue
            for forbidden in ("os.execute", "io.open", "dofile", "require"):
                self.assertNotIn(forbidden, line)
            self.assertTrue(
                line.startswith(allowed_prefixes),
                f"unexpected line: {line}",
            )

    def test_manifest_hash_matches_rendered_output(self) -> None:
        seeds = load_seeds(STATE_SRC)
        m = render_manifest(seeds)
        preload = render_preload(seeds)
        whitelist = render_whitelist_txt(seeds)
        self.assertEqual(m["preload"]["bytes"], len(preload.encode("utf-8")))
        self.assertEqual(m["preload"]["hash"], hash31(preload))
        self.assertEqual(m["whitelist"]["bytes"], len(whitelist.encode("utf-8")))
        self.assertEqual(m["whitelist"]["hash"], hash31(whitelist))


class PreloadGoldenFixtureTest(unittest.TestCase):
    """Compares the Python oracle to hand-written golden expected files.

    The golden files are authored independently of the oracle so that a shared
    bug in the Python and ucode implementations is caught here.
    """

    def test_oracle_preload_matches_golden_fixture(self) -> None:
        seeds = load_seeds(FIXTURE_SEEDS)
        actual = render_preload(seeds, state_dir=GOLDEN_STATE_DIR)
        expected = GOLDEN_PRELOAD.read_text(encoding="utf-8")
        self.assertEqual(actual, expected)

    def test_oracle_whitelist_matches_golden_fixture(self) -> None:
        seeds = load_seeds(FIXTURE_SEEDS)
        actual = render_whitelist_txt(seeds)
        expected = GOLDEN_WHITELIST.read_text(encoding="utf-8")
        self.assertEqual(actual, expected)

    def test_golden_fixture_seeds_are_schema_v1(self) -> None:
        for name in EXPECTED_SEEDS:
            doc = json.loads((FIXTURE_SEEDS / name).read_text(encoding="utf-8"))
            self.assertEqual(doc["schema_version"], 1)

    def test_golden_fixture_exercises_blocked_learned_manual_whitelist(self) -> None:
        golden = GOLDEN_PRELOAD.read_text(encoding="utf-8")
        self.assertIn("slm_preload_blocked(", golden)
        self.assertIn('slm_preload_blocked("tls", "*"', golden)
        self.assertIn("slm_preload_history(", golden)
        self.assertIn("slm_preload_locked(", golden)
        self.assertIn("ORCHESTRA_WHITELIST = {", golden)

    def test_golden_fixture_strategy_keys_are_sorted_lexically(self) -> None:
        golden = GOLDEN_PRELOAD.read_text(encoding="utf-8")
        host10_lines = [l for l in golden.split("\n") if l.startswith('slm_preload_history("tls", "host10.example"')]
        strategies = [int(re.search(r", (\d+),", l).group(1)) for l in host10_lines]
        self.assertEqual(strategies, [1, 10, 2])


class PreloadGeneratorRuntimeTest(unittest.TestCase):
    """Executes the real ucode generator; skipped when ucode is absent."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.ucode = shutil.which("ucode")

    def setUp(self) -> None:
        if self.ucode is None:
            self.skipTest("ucode executable not found on PATH")
        self.tmp = tempfile.mkdtemp(prefix="orchestra-gen-")
        self.state_dir = Path(self.tmp) / "state"
        self.runtime_dir = Path(self.tmp) / "runtime"
        self.state_dir.mkdir()
        self.runtime_dir.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_seeds(self, seeds: dict) -> None:
        for name, doc in seeds.items():
            (self.state_dir / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8")

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["ORCHESTRA_STATE_DIR"] = str(self.state_dir)
        env["ORCHESTRA_RUNTIME_DIR"] = str(self.runtime_dir)
        env["ORCHESTRA_PRELOAD_FILE"] = str(self.runtime_dir / "preload.lua")
        env["ORCHESTRA_WHITELIST_FILE"] = str(self.runtime_dir / "whitelist.txt")
        env["ORCHESTRA_MANIFEST_FILE"] = str(self.runtime_dir / "manifest.json")
        return subprocess.run(
            [self.ucode, str(GENERATOR), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_empty_seeds_match_oracle_and_manifest_is_consistent(self) -> None:
        self._write_seeds({
            "blocked": {"schema_version": 1, "protocols": {"tls": {"global": [], "hosts": {}}}},
            "learned": {"schema_version": 1, "protocols": {"tls": {}}},
            "manual-locks": {"schema_version": 1, "protocols": {"tls": {}}},
            "whitelist": {"schema_version": 1, "hosts": []},
        })
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        preload = (self.runtime_dir / "preload.lua").read_text(encoding="utf-8")
        wl = (self.runtime_dir / "whitelist.txt").read_text(encoding="utf-8")
        manifest = json.loads((self.runtime_dir / "manifest.json").read_text(encoding="utf-8"))
        seeds = load_seeds(self.state_dir)
        self.assertEqual(preload, render_preload(seeds, state_dir=str(self.state_dir)))
        self.assertEqual(wl, render_whitelist_txt(seeds))
        self.assertEqual(manifest["preload"]["hash"], hash31(preload))
        self.assertEqual(manifest["whitelist"]["hash"], hash31(wl))
        cr = self._run("check")
        self.assertEqual(cr.returncode, 0, cr.stderr)

    def test_golden_fixture_matches_real_ucode_output(self) -> None:
        for name in EXPECTED_SEEDS:
            (self.state_dir / name).write_text((FIXTURE_SEEDS / name).read_text(encoding="utf-8"), encoding="utf-8")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        preload = (self.runtime_dir / "preload.lua").read_text(encoding="utf-8")
        wl = (self.runtime_dir / "whitelist.txt").read_text(encoding="utf-8")
        self.assertEqual(preload, render_preload(load_seeds(self.state_dir), state_dir=str(self.state_dir)))
        self.assertEqual(wl, render_whitelist_txt(load_seeds(self.state_dir)))

    def test_check_detects_truncated_preload(self) -> None:
        self._write_seeds({
            "blocked": {"schema_version": 1, "protocols": {"tls": {"global": [1], "hosts": {}}}},
            "learned": {"schema_version": 1, "protocols": {"tls": {}}},
            "manual-locks": {"schema_version": 1, "protocols": {"tls": {}}},
            "whitelist": {"schema_version": 1, "hosts": []},
        })
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        p = self.runtime_dir / "preload.lua"
        p.write_text(p.read_text(encoding="utf-8")[:-5], encoding="utf-8")
        cr = self._run("check")
        self.assertNotEqual(cr.returncode, 0)
        self.assertIn("preload", cr.stderr)

    def test_check_fails_when_manifest_missing(self) -> None:
        self._write_seeds({
            "blocked": {"schema_version": 1, "protocols": {"tls": {}}},
            "learned": {"schema_version": 1, "protocols": {"tls": {}}},
            "manual-locks": {"schema_version": 1, "protocols": {"tls": {}}},
            "whitelist": {"schema_version": 1, "hosts": []},
        })
        cr = self._run("check")
        self.assertNotEqual(cr.returncode, 0)
        self.assertIn("manifest", cr.stderr)

    def test_bad_schema_version_exits_non_zero(self) -> None:
        self._write_seeds({
            "blocked": {"schema_version": 2, "protocols": {}},
            "learned": {"schema_version": 1, "protocols": {}},
            "manual-locks": {"schema_version": 1, "protocols": {}},
            "whitelist": {"schema_version": 1, "hosts": []},
        })
        r = self._run()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("schema_version", r.stderr)

    def test_missing_seed_exits_non_zero(self) -> None:
        (self.state_dir / "learned.json").write_text('{"schema_version":1,"protocols":{}}', encoding="utf-8")
        (self.state_dir / "manual-locks.json").write_text('{"schema_version":1,"protocols":{}}', encoding="utf-8")
        (self.state_dir / "whitelist.json").write_text('{"schema_version":1,"hosts":[]}', encoding="utf-8")
        r = self._run()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("blocked.json", r.stderr)


if __name__ == "__main__":
    unittest.main()
