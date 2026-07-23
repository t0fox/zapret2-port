"""Ready TLS profile set + per-profile contract + release versions (TDD).

Encodes the runtime contract that the runtime branch (A) implements:
  * Exactly 8 ready TLS profiles with fixed IDs; the manifest (profiles.tsv)
    and the installed .opt set must match; gui-quic-fake is NOT ready/install.
    The 8 = the 6 r6 circular profiles + ``discord-adaptive`` (circular, r7) +
    ``discord-v5`` (NATIVE, r7 — static nfqws2, no circular_quality).
  * Each CIRCULAR ready profile contains EXACTLY ONE
    ``--lua-init=@.../init.lua`` line, located BEFORE the first
    ``--lua-desync=circular_quality`` (the selector); has at least one
    ``strategy=N``; numbering starts at 1, is unique and contiguous (no gaps);
    the selector appears before any strategy instance; the NFQWS2_OPT
    assignment parses as valid shell.
  * The NATIVE ready profile (discord-v5) does NOT reference circular_quality,
    has NO strategy=N, and uses ``--lua-init=.../init_vars.lua`` (not the
    orchestra-extra init). It is allowed by the r7-relaxed validator
    (apply.uc validate_profile no longer requires circular_quality for native
    profiles that do not load the orchestra runtime).
  * orchestra-tls-mvp contains ``strategy=1``.
  * Releases: zapret2 PKG_RELEASE=3, zapret2-orchestra PKG_RELEASE=7.

These tests are RED on the pre-merge base (0b046fa) and GREEN after the
runtime branch lands the contract. The ucode runtime check uses the REAL
pinned host ucode (85922056) via ``ucode apply.uc validate-profile <id>``;
it skips locally when ucode is absent (Windows) and MUST NOT skip in GitHub
Actions (the workflow's zero-skip gate greps for the skip reason string).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _nfqws2_parser as P  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "openwrt" / "zapret2-orchestra"
SHARE_FILES = PACKAGE / "files/usr/share/zapret2-orchestra"
PROFILES_DIR = SHARE_FILES / "profiles"
MANIFEST = SHARE_FILES / "profiles.tsv"
APPLY_UC = SHARE_FILES / "apply.uc"
ORCH_LUA_DIR = PACKAGE / "files/opt/zapret2/lua/orchestra-extra"
INIT_LUA = ORCH_LUA_DIR / "init.lua"

Z2_MAKEFILE = ROOT / "openwrt" / "zapret2" / "Makefile"
ORCH_MAKEFILE = PACKAGE / "Makefile"

# The exact ready TLS profile set (r7 = 8). gui-quic-fake is deliberately
# excluded: it is a UDP/QUIC profile, not a ready TLS install profile.
READY_IDS = (
    "orchestra-tls-mvp",
    "gui-tls-multisplit",
    "gui-tls-multidisorder",
    "gui-tls-hostfakesplit",
    "gui-tls-syndata",
    "gui-circular",
    "discord-adaptive",
    "discord-v5",
)

# Circular profiles load the orchestra runtime (init.lua + circular_quality).
# The native profile (discord-v5) is a static nfqws2 chain: no circular_quality,
# no strategy=N, --lua-init=init_vars.lua. The r7 validator relaxation
# (contracts §6 / spec §4) allows the native profile WITHOUT circular_quality.
CIRCULAR_READY_IDS = (
    "orchestra-tls-mvp",
    "gui-tls-multisplit",
    "gui-tls-multidisorder",
    "gui-tls-hostfakesplit",
    "gui-tls-syndata",
    "gui-circular",
    "discord-adaptive",
)
NATIVE_READY_IDS = ("discord-v5",)

LUA_INIT_LINE = "--lua-init=@/opt/zapret2/lua/orchestra-extra/init.lua"


def _manifest_ids() -> list[str]:
    ids: list[str] = []
    for line in MANIFEST.read_text("utf-8").splitlines():
        if not line.strip() or line.startswith("id\t"):
            continue
        ids.append(line.split("\t")[0])
    return ids


def _opt_files() -> list[Path]:
    return sorted(PROFILES_DIR.glob("*.opt"))


# ---------------------------------------------------------------------------
# B1 — exact ready set
# ---------------------------------------------------------------------------


class ReadyProfileSetTest(unittest.TestCase):
    """The ready TLS profile set is exactly the 8 fixed IDs (r7), and the
    manifest matches the shipped .opt files. gui-quic-fake is not a ready
    profile."""

    def test_exactly_eight_opt_files(self) -> None:
        opts = _opt_files()
        self.assertEqual(
            len(opts), 8,
            f"expected exactly 8 ready .opt profiles (r7), got {len(opts)}: "
            f"{[p.name for p in opts]}",
        )

    def test_opt_ids_are_the_exact_ready_set(self) -> None:
        ids = {p.stem for p in _opt_files()}
        self.assertEqual(ids, set(READY_IDS), f"opt ids mismatch: {ids}")

    def test_manifest_lists_exactly_the_ready_set(self) -> None:
        ids = _manifest_ids()
        self.assertEqual(
            ids, list(READY_IDS),
            f"manifest ids must be the ready set in order, got {ids}",
        )

    def test_manifest_and_opt_set_match(self) -> None:
        manifest_ids = set(_manifest_ids())
        opt_ids = {p.stem for p in _opt_files()}
        self.assertEqual(manifest_ids, opt_ids,
                         f"manifest {manifest_ids} != opt set {opt_ids}")

    def test_gui_quic_fake_is_not_ready(self) -> None:
        # gui-quic-fake must NOT ship as a ready/install profile.
        self.assertFalse(
            (PROFILES_DIR / "gui-quic-fake.opt").is_file(),
            "gui-quic-fake.opt must not be a ready/install profile",
        )
        self.assertNotIn("gui-quic-fake", _manifest_ids(),
                         "gui-quic-fake must not be in the manifest")

    def test_every_ready_id_has_an_opt_file(self) -> None:
        for pid in READY_IDS:
            self.assertTrue(
                (PROFILES_DIR / f"{pid}.opt").is_file(),
                f"ready id '{pid}' has no .opt file",
            )


# ---------------------------------------------------------------------------
# B2 — per-profile content contract
# ---------------------------------------------------------------------------


class ReadyProfileContentTest(unittest.TestCase):
    """Per-profile content contract, split by profile kind:

    * CIRCULAR profiles (the 6 r6 profiles + discord-adaptive): exactly one
      orchestra-extra --lua-init before the first circular_quality selector,
      at least one strategy=N, contiguous numbering from 1, selector before
      strategies, and circular_quality referenced.
    * NATIVE profile (discord-v5): a static nfqws2 chain — NO circular_quality,
      NO strategy=N, --lua-init=init_vars.lua. The r7 validator relaxation
      allows it without circular_quality (contracts §6 / spec §4).
    * Universal (all 8): valid shell assignment + profile_value_ok.
    """

    def _profile_text(self, pid: str) -> str:
        return (PROFILES_DIR / f"{pid}.opt").read_text("utf-8")

    def _value(self, pid: str) -> str:
        return P.extract(self._profile_text(pid)).value

    # --- universal (all SHIPPED ready profiles) -------------------------
    # These run over the profiles actually present on disk (via _opt_files()),
    # so the r6 content-safety guards stay green on the 6 r6 profiles now and
    # automatically cover the 8 r7 profiles once A ships them. The cardinality
    # contract (exactly 8) is enforced separately by ReadyProfileSetTest.

    def test_profiles_parse_as_valid_shell_assignment(self) -> None:
        if not shutil.which("sh"):
            self.skipTest("sh not on PATH")
        for p in _opt_files():
            r = subprocess.run(["sh", "-n", str(p)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{p.name}: sh -n failed: {r.stderr}")

    def test_profiles_pass_profile_value_ok_contract(self) -> None:
        # Mirror apply.uc profile_value_ok: reject NUL/CR/$()/`/;/|/&& and
        # unclosed quotes. < and > are allowed (<HOSTLIST> placeholders).
        for p in _opt_files():
            val = P.extract(p.read_text("utf-8")).value
            pid = p.stem
            self.assertNotIn("\x00", val, f"{pid}: NUL")
            self.assertNotIn("\r", val, f"{pid}: CR")
            self.assertNotIn("$(", val, f"{pid}: command substitution")
            self.assertNotIn("`", val, f"{pid}: backtick")
            self.assertNotIn(";", val, f"{pid}: ;")
            self.assertNotIn("|", val, f"{pid}: |")
            self.assertNotIn("&&", val, f"{pid}: &&")
            self.assertEqual(val.count('"') % 2, 0, f"{pid}: unclosed double quote")
            self.assertEqual(val.count("'") % 2, 0, f"{pid}: unclosed single quote")

    # --- circular-only (the 6 r6 profiles + discord-adaptive) -----------

    def test_each_circular_profile_has_exactly_one_orchestra_lua_init(self) -> None:
        for pid in CIRCULAR_READY_IDS:
            text = self._profile_text(pid)
            n = text.count(LUA_INIT_LINE)
            self.assertEqual(
                n, 1,
                f"{pid}: expected exactly one orchestra-extra --lua-init line, got {n}",
            )

    def test_circular_lua_init_before_first_circular_quality(self) -> None:
        for pid in CIRCULAR_READY_IDS:
            text = self._profile_text(pid)
            init_idx = text.find(LUA_INIT_LINE)
            cq_idx = text.find("--lua-desync=circular_quality")
            self.assertGreater(init_idx, -1, f"{pid}: no --lua-init line")
            self.assertGreater(cq_idx, -1, f"{pid}: no circular_quality selector")
            self.assertLess(
                init_idx, cq_idx,
                f"{pid}: --lua-init must precede the first circular_quality",
            )

    def test_each_circular_profile_has_at_least_one_strategy(self) -> None:
        for pid in CIRCULAR_READY_IDS:
            val = self._value(pid)
            strategies = re.findall(r"strategy=(\d+)", val)
            self.assertGreaterEqual(
                len(strategies), 1,
                f"{pid}: must have at least one strategy=N",
            )

    def test_circular_strategy_numbering_starts_at_one_contiguous_unique(self) -> None:
        for pid in CIRCULAR_READY_IDS:
            val = self._value(pid)
            nums = [int(n) for n in re.findall(r"strategy=(\d+)", val)]
            self.assertEqual(
                nums, list(range(1, len(nums) + 1)),
                f"{pid}: strategy numbers must start at 1 and be contiguous "
                f"and unique, got {nums}",
            )

    def test_circular_selector_appears_before_strategy_instances(self) -> None:
        for pid in CIRCULAR_READY_IDS:
            text = self._profile_text(pid)
            cq_idx = text.find("--lua-desync=circular_quality")
            first_strategy = text.find("strategy=")
            self.assertGreater(cq_idx, -1, f"{pid}: no circular_quality selector")
            self.assertGreater(first_strategy, -1, f"{pid}: no strategy=N")
            self.assertLess(
                cq_idx, first_strategy,
                f"{pid}: circular_quality selector must precede strategy instances",
            )

    def test_each_circular_profile_references_circular_quality(self) -> None:
        for pid in CIRCULAR_READY_IDS:
            val = self._value(pid)
            self.assertIn("circular_quality", val,
                          f"{pid}: must reference circular_quality")

    def test_mvp_contains_strategy_one(self) -> None:
        val = self._value("orchestra-tls-mvp")
        self.assertIn("strategy=1", val,
                      "orchestra-tls-mvp must contain strategy=1")

    def test_orchestra_init_lua_present(self) -> None:
        # The orchestra-extra init.lua must exist so circular profiles resolve
        # the Orchestra extension at runtime.
        self.assertTrue(INIT_LUA.is_file(),
                        "orchestra-extra/init.lua missing from package source")

    # --- native-only (discord-v5) ---------------------------------------

    def test_native_discord_v5_has_no_circular_quality(self) -> None:
        for pid in NATIVE_READY_IDS:
            val = self._value(pid)
            self.assertNotIn("circular_quality", val,
                             f"{pid}: native profile must NOT reference circular_quality")

    def test_native_discord_v5_has_no_strategy_n(self) -> None:
        for pid in NATIVE_READY_IDS:
            val = self._value(pid)
            self.assertIsNone(re.search(r"strategy=\d", val),
                              f"{pid}: native profile must NOT have strategy=N")

    def test_native_discord_v5_uses_init_vars_lua(self) -> None:
        # The native profile's --lua-init points at init_vars.lua (provides
        # tls_google via the nfqws2 tls_mod builtin), NOT orchestra-extra.
        for pid in NATIVE_READY_IDS:
            text = self._profile_text(pid)
            self.assertIn("init_vars.lua", text,
                          f"{pid}: native profile must --lua-init=init_vars.lua")

    def test_native_discord_v5_has_send_syndata_tls_google(self) -> None:
        # The proven golden chain: send + syndata:tls_google + syndata.
        for pid in NATIVE_READY_IDS:
            val = self._value(pid)
            self.assertIn("send", val)
            self.assertIn("syndata", val)
            self.assertIn("tls_google", val)

    def test_native_discord_v5_uses_ipset_not_hostlist(self) -> None:
        # Default v5 is IP-based (--ipset=ipset-discord.txt), not hostlist.
        for pid in NATIVE_READY_IDS:
            val = self._value(pid)
            self.assertIn("--ipset", val)
            self.assertIn("ipset-discord", val)


# ---------------------------------------------------------------------------
# B2 (runtime) — real pinned ucode validate-profile for each ready profile
# ---------------------------------------------------------------------------


class ReadyProfileUcodeRuntimeTest(unittest.TestCase):
    """Runs the REAL pinned host ucode ``apply.uc validate-profile <id>`` for
    each ready profile. Skips locally when ucode is absent; MUST NOT skip in
    GitHub Actions (the workflow zero-skip gate greps for this reason)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.ucode = shutil.which("ucode")

    def setUp(self) -> None:
        if self.ucode is None:
            self.skipTest("ucode executable not found on PATH")
        self.tmp = tempfile.mkdtemp(prefix="orch-ready-uc-")
        self.runtime = Path(self.tmp) / "runtime"
        self.runtime.mkdir()
        self.orch = Path(self.tmp) / "orch"
        self.orch.mkdir()
        self.opt = Path(self.tmp) / "opt" / "zapret2"
        self.opt.mkdir(parents=True)
        self.user_profiles = Path(self.tmp) / "user-profiles"
        self.user_profiles.mkdir()
        self.builtin_profiles = Path(self.tmp) / "builtin-profiles"
        self.builtin_profiles.mkdir()
        self.orch_lua = Path(self.tmp) / "orch-lua"
        self.orch_lua.mkdir(parents=True)
        # Seed the orchestra-extra lua the profile --lua-init resolves against.
        for f in ORCH_LUA_DIR.glob("*.lua"):
            (self.orch_lua / f.name).write_text(f.read_text("utf-8"), "utf-8")
        # Persistent JSON seeds so validate-profile / preload checks find them.
        for jf in ("blocked.json", "learned.json", "manual-locks.json", "whitelist.json"):
            src = PACKAGE / "files/etc/zapret2-orchestra" / jf
            if src.is_file():
                (self.orch / jf).write_text(src.read_text("utf-8"), "utf-8")
        # Fake preload wrapper so the manager's preload check passes.
        fake_preload = Path(self.tmp) / "fake-preload.sh"
        fake_preload.write_text(
            "#!/bin/sh\n"
            "mkdir -p \"$ORCHESTRA_RUNTIME_DIR\"\n"
            'case "$1" in\n'
            "  generate)\n"
            '    echo "preload" > "$ORCHESTRA_RUNTIME_DIR/preload.lua"\n'
            '    echo "whitelist" > "$ORCHESTRA_RUNTIME_DIR/whitelist.txt"\n'
            '    echo \'{"schema_version":1}\' > "$ORCHESTRA_RUNTIME_DIR/manifest.json"\n'
            "    exit 0 ;;\n"
            "  check)\n"
            '    [ -f "$ORCHESTRA_RUNTIME_DIR/preload.lua" ] && exit 0 || exit 1 ;;\n'
            "  *) exit 1 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_preload.chmod(0o755)
        (self.opt / "config").write_text(
            'NFQWS2_ENABLE=0\nNFQWS2_OPT="--lua-desync=circular_quality:key=tls"\n'
            'MODE_FILTER=none\n',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self) -> dict:
        env = os.environ.copy()
        env["ZAPRET2_RUNTIME_DIR"] = str(self.runtime)
        env["ZAPRET2_ORCHESTRA_DIR"] = str(self.orch)
        env["ZAPRET2_SHARE_DIR"] = str(Path(self.tmp) / "share")
        env["ZAPRET2_USER_PROFILES_DIR"] = str(self.user_profiles)
        env["ZAPRET2_BUILTIN_PROFILES_DIR"] = str(self.builtin_profiles)
        env["ZAPRET2_ORCHESTRA_LUA"] = str(self.orch_lua)
        env["ZAPRET2_STATE_FILE"] = str(self.orch / "manager-state.json")
        env["ZAPRET2_LOCK_DIR"] = str(self.runtime / "apply.lock")
        env["ZAPRET2_VALIDATE_OUT"] = str(self.runtime / "validate-config.cfg")
        env["ZAPRET2_CANDIDATE_FILE"] = str(self.opt / ".config.orchestra.tmp")
        env["ZAPRET2_BACKUP_DIR"] = str(self.orch / "backup")
        env["ZAPRET2_CONFIG"] = str(self.opt / "config")
        env["ZAPRET2_PRELOAD_WRAPPER"] = str(Path(self.tmp) / "fake-preload.sh")
        env["ORCHESTRA_STATE_DIR"] = str(self.orch)
        env["ORCHESTRA_RUNTIME_DIR"] = str(self.runtime)
        env["ORCHESTRA_PRELOAD_FILE"] = str(self.runtime / "preload.lua")
        env["ORCHESTRA_WHITELIST_FILE"] = str(self.runtime / "whitelist.txt")
        env["ORCHESTRA_MANIFEST_FILE"] = str(self.runtime / "manifest.json")
        return env

    def _run_validate(self, pid: str) -> subprocess.CompletedProcess:
        # Copy the shipped builtin profile into the test builtin dir so ucode
        # can find it, exactly as the package installs it.
        src = PROFILES_DIR / f"{pid}.opt"
        (self.builtin_profiles / f"{pid}.opt").write_text(
            src.read_text("utf-8"), "utf-8")
        r = subprocess.run(
            [self.ucode, str(APPLY_UC), "--", "validate-profile", pid],
            env=self._env(), capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0 or not r.stdout.strip():
            sys.stderr.write(
                f"[validate-profile {pid} rc={r.returncode}] "
                f"stdout={r.stdout!r} stderr={r.stderr!r}\n")
        return r

    def test_every_ready_profile_validates_under_real_ucode(self) -> None:
        for pid in READY_IDS:
            with self.subTest(profile=pid):
                r = self._run_validate(pid)
                self.assertEqual(r.returncode, 0,
                                 f"{pid}: validate-profile failed: {r.stderr}")
                doc = json.loads(r.stdout)
                self.assertTrue(doc["ok"],
                                f"{pid}: validate-profile ok=false: {doc}")
                self.assertEqual(doc["source_type"], "builtin",
                                 f"{pid}: expected builtin source_type")

    def test_mvp_validates_under_real_ucode(self) -> None:
        r = self._run_validate("orchestra-tls-mvp")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertTrue(doc["ok"], f"MVP validate failed: {doc}")


# ---------------------------------------------------------------------------
# B3 — release versions
# ---------------------------------------------------------------------------


class ReleaseVersionTest(unittest.TestCase):
    """zapret2 PKG_RELEASE=3, zapret2-orchestra PKG_RELEASE=7 (r7)."""

    def test_zapret2_release_is_three(self) -> None:
        text = Z2_MAKEFILE.read_text("utf-8")
        self.assertRegex(text, r"(?m)^PKG_RELEASE:=3$",
                         "zapret2 PKG_RELEASE must be 3")

    def test_orchestra_release_is_seven(self) -> None:
        text = ORCH_MAKEFILE.read_text("utf-8")
        self.assertRegex(text, r"(?m)^PKG_RELEASE:=7$",
                         "zapret2-orchestra PKG_RELEASE must be 7 (r7)")


if __name__ == "__main__":
    unittest.main()
