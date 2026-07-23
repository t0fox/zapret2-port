"""Router acceptance harness for the r7 Orchestra scenarios A-G (Step 6).

This is a HARNESS / FIXTURE BUILDER, not a router runner. It defines the
scenario steps and PASS criteria as DATA so a later router-side runner (or a
CI integration stage with router access) can execute them. It does NOT connect
to the router, does NOT run nfqws2/nft/curl against a live network, and does
NOT assert network outcomes as universal truths.

Two layers:

  1. ``ACCEPTANCE_SCENARIOS`` — a data table of the 7 router scenarios (A-G)
     with, for each: the name, the precondition, the steps, the PASS criteria,
     and which contract section it exercises. A router runner consumes this
     table; the harness here validates the table is well-formed and internally
     consistent (every PASS criterion is anchored to a contract section, no
     scenario asserts a universal network outcome).

  2. ``REGRESSION_RULE`` — the r6→r7 regression contract: the r6 sentinel tests,
     the r6 ready-profile-contract, and the r6 package-contract MUST stay green
     after r6→r7. This module asserts that the r7 reconciliation did NOT drop
     or weaken those r6 guards: it statically inspects the reconciled test
     modules + workflow to confirm the r6 protections are still present (now
     expecting r7 values where the contract bumped).

Binding rules honored:
  - Default old / Default v5 network results are ROUTER acceptance evidence.
    The PASS criteria assert the CHAIN STRUCTURE and PROVENANCE (the v5 chain
    is applied; nfqws2 log shows syndata desync; the adaptive profile rotates),
    NOT "Default v5 always gives HTTP 200". The HTTP-200 criterion is scoped to
    the router scenario (precondition: baseline discord TIMEOUT ≥2/3 → after
    enable ≥2/3), which is acceptance evidence on THAT router, not a universal
    unit test.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

# The reconciled r6→r7 test modules + workflow this harness guards.
SENTINEL_TEST = ROOT / "tests" / "test_cli_sentinel.py"
READY_PROFILE_TEST = ROOT / "tests" / "test_ready_profile_contract.py"
PACKAGE_TEST = ROOT / "tests" / "test_package_contract.py"
WORKING_PROTOTYPE_TEST = ROOT / "tests" / "test_working_prototype.py"
WORKFLOW = ROOT / ".github" / "workflows" / "build-apk.yml"
ORCH_MAKEFILE = ROOT / "openwrt" / "zapret2-orchestra" / "Makefile"
Z2_MAKEFILE = ROOT / "openwrt" / "zapret2" / "Makefile"


# ---------------------------------------------------------------------------
# Acceptance scenarios A-G (Step 6) — DATA, not executed here
# ---------------------------------------------------------------------------

ACCEPTANCE_SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "A",
        "name": "baseline-discord-blocked",
        "contract_ref": "spec §4 golden scenario (router-only evidence)",
        "precondition": "legacy zapret + zapret2 stopped; orchestra not enabled",
        "steps": [
            "stop legacy zapret and zapret2",
            "with no DPI bypass active, curl -sS -o /dev/null -w '%{http_code}' https://discord.com (×3)",
            "record the baseline HTTP codes",
        ],
        "pass_criteria": [
            "baseline discord.com is TIMEOUT or non-200 for >=2/3 probes (confirms the block exists)",
        ],
        "asserts_universal_network_outcome": False,
    },
    {
        "id": "B",
        "name": "enable-discord-adaptive-activates-datapath",
        "contract_ref": "contracts §5 enable/disable lifecycle",
        "precondition": "scenario A recorded a baseline; orchestra installed",
        "steps": [
            "zapret2-orchestra-profile enable discord-adaptive",
            "verify NFQWS2_ENABLE=1 in /opt/zapret2/config",
            "verify /etc/init.d/zapret2 is running (procd)",
            "verify the learner service is running",
            "verify nft table inet zapret2 exists and NFQUEUE rule is installed",
        ],
        "pass_criteria": [
            "enable is ONE step → running, bypassing datapath (parity with OrchestraRunner.start())",
            "NFQWS2_ENABLE=1 set in config",
            "nfqws2 process present with the discord-adaptive argv",
            "learner process present",
        ],
        "asserts_universal_network_outcome": False,
    },
    {
        "id": "C",
        "name": "discord-v5-chain-applied-and-bypasses",
        "contract_ref": "spec §4 golden scenario; contracts §1 Default v5 chain",
        "precondition": "scenario B complete (datapath active with discord-adaptive)",
        "steps": [
            "curl -sS -o /dev/null -w '%{http_code}' https://discord.com (×3)",
            "curl -sS -o /dev/null -w '%{http_code}' https://discordapp.com (×3)",
            "curl -sS -o /dev/null -w '%{http_code}' https://example.com (×1 control)",
            "logread / nfqws2 debug: grep for syndata desync + 'LUA: syndata'",
        ],
        "pass_criteria": [
            "discord.com HTTP >=2/3 of {200,301,3xx} (router acceptance evidence, NOT a universal unit assertion)",
            "control example.com reachable (no over-blocking)",
            "nfqws2 log shows syndata_1_2/1_3 desync (the v5 chain executed)",
        ],
        "asserts_universal_network_outcome": False,  # scoped to this router's baseline
    },
    {
        "id": "D",
        "name": "circular-rotation-skips-default-blocked-strategy-one",
        "contract_ref": "contracts §3 DEFAULT_BLOCKED_PASS_DOMAINS; §4 rotation skip",
        "precondition": "scenario B complete; discord.com in DEFAULT_BLOCKED_PASS_DOMAINS",
        "steps": [
            "inspect blocked.json: strategy=1 blocked for discord.com (DEFAULT_BLOCKED_PASS_DOMAINS seed)",
            "observe the circular rotation in events.ndjson: ROTATE events never select strategy=1 for discord.com",
        ],
        "pass_criteria": [
            "strategy=1 is blocked for discord.com at load (cannot be unblocked by user)",
            "no APPLIED/ROTATE event selects strategy=1 for discord.com",
            "rotation moves to strategy=2 (Default v5) — the proven winner",
        ],
        "asserts_universal_network_outcome": False,
    },
    {
        "id": "E",
        "name": "auto-lock-persists-v5-after-three-successes",
        "contract_ref": "contracts §3 lock/unlock policy; §2 SUCCESS events",
        "precondition": "scenario B complete; events.ndjson accumulating SUCCESS",
        "steps": [
            "generate >=3 successful discord.com flows (curl ×3 from C)",
            "inspect events.ndjson: >=3 SUCCESS events for discord.com strategy=2",
            "inspect learned.json: protocols.tls.discord.com.auto_lock == 2",
            "restart zapret2 (procd) and the learner",
            "re-inspect learned.json: auto_lock still == 2 (persisted)",
            "observe the v5 chain (strategy=2) is preloaded/applied after restart (no rediscovery)",
        ],
        "pass_criteria": [
            "auto_lock=2 persisted in learned.json after 3 TCP successes",
            "learned strategy reused after restart (no full rediscovery)",
            "preload regen happened once (debounced), not per-event",
        ],
        "asserts_universal_network_outcome": False,
    },
    {
        "id": "F",
        "name": "auto-unlock-after-three-failures-re-learns",
        "contract_ref": "contracts §3 unlock_fails=3; user-lock protected",
        "precondition": "scenario E complete (auto_lock=2 for discord.com)",
        "steps": [
            "simulate 3 consecutive FAIL events on discord.com strategy=2 (e.g. network change)",
            "inspect learned.json: auto_lock removed for discord.com (back to LEARNING)",
            "observe rotation resumes (ROTATE events to other strategies)",
            "set a USER lock on a host via CLI; drive 3 FAILs; confirm user lock NOT auto-unlocked",
        ],
        "pass_criteria": [
            "auto_lock removed after 3 consecutive FAILs",
            "rotation resumes (domain back to LEARNING)",
            "user-locked host stays locked across auto-unlock events (NEVER auto-unlocked)",
        ],
        "asserts_universal_network_outcome": False,
    },
    {
        "id": "G",
        "name": "disable-restores-config-and-tears-down-datapath",
        "contract_ref": "contracts §5 disable lifecycle",
        "precondition": "orchestra enabled (scenario B/E)",
        "steps": [
            "zapret2-orchestra-profile disable",
            "verify NFQWS2_ENABLE=0 in /opt/zapret2/config",
            "verify nfqws2 process absent",
            "verify no nft table inet zapret2",
            "verify learner stopped",
            "verify config restored from backup",
        ],
        "pass_criteria": [
            "disable is ONE step → no nfqws2, no table inet zapret2",
            "NFQWS2_ENABLE=0",
            "config restored from the pre-enable backup",
            "enabled=false in manager-state.json",
        ],
        "asserts_universal_network_outcome": False,
    },
]


# ---------------------------------------------------------------------------
# Regression rule: r6 guards stay present (now expecting r7 values)
# ---------------------------------------------------------------------------

REGRESSION_RULE = {
    "r6_sentinel_tests_must_stay_green": (
        "test_cli_sentinel.py: the r6 argv-sentinel fix (optional leading '--' "
        "normalization in apply.uc) must not regress. The reconciled module keeps "
        "the static sentinel guards and the runtime wrapper/profile-frontend checks."
    ),
    "r6_ready_profile_contract_must_stay_green": (
        "test_ready_profile_contract.py: the ready-set + per-profile content "
        "contract stays green after r7 (ready set grows 6→8 with discord-adaptive "
        "+ discord-v5; circular_quality required for the 7 circular profiles, "
        "NOT required for the native discord-v5)."
    ),
    "r6_package_contract_must_stay_green": (
        "test_package_contract.py: orchestra PKG_RELEASE 6→7; zapret2 stays r3; "
        "APK name regex zapret2-orchestra-.*-r7.apk$; the r6 lifecycle/ipkg-instroot/"
        "no-uci/no-nft guards remain."
    ),
}


# ---------------------------------------------------------------------------
# Harness self-tests: the scenario table + regression rule are well-formed
# ---------------------------------------------------------------------------

class AcceptanceScenarioTableTest(unittest.TestCase):
    """The acceptance scenario DATA is well-formed and internally consistent.
    These run here (no router) — they validate the harness, not the router."""

    def test_seven_scenarios_a_through_g(self) -> None:
        ids = [s["id"] for s in ACCEPTANCE_SCENARIOS]
        self.assertEqual(ids, ["A", "B", "C", "D", "E", "F", "G"],
                         f"expected scenarios A-G in order, got {ids}")

    def test_every_scenario_has_required_fields(self) -> None:
        required = {"id", "name", "contract_ref", "precondition", "steps", "pass_criteria",
                    "asserts_universal_network_outcome"}
        for s in ACCEPTANCE_SCENARIOS:
            missing = required - set(s)
            self.assertFalse(missing, f"scenario {s.get('id')!r} missing fields: {missing}")

    def test_every_scenario_anchored_to_a_contract_section(self) -> None:
        for s in ACCEPTANCE_SCENARIOS:
            ref = s["contract_ref"]
            self.assertIsInstance(ref, str)
            self.assertTrue(len(ref) > 0, f"scenario {s['id']} has empty contract_ref")

    def test_no_scenario_asserts_a_universal_network_outcome(self) -> None:
        # The binding rule: network results are router acceptance evidence,
        # NOT universal unit-test assertions. Every scenario must flag this.
        for s in ACCEPTANCE_SCENARIOS:
            self.assertFalse(s["asserts_universal_network_outcome"],
                             f"scenario {s['id']} must not assert a universal network outcome")

    def test_pass_criteria_assert_chain_structure_or_provenance_not_http200_globally(self) -> None:
        # Any HTTP-200 criterion must be scoped (mentions 'router'/'evidence'/
        # 'baseline'), not a bare universal 'always 200'.
        for s in ACCEPTANCE_SCENARIOS:
            for crit in s["pass_criteria"]:
                if "200" in crit or "HTTP" in crit:
                    self.assertTrue(
                        any(w in crit.lower() for w in ("router", "evidence", "baseline", "scoped", ">=2/3")),
                        f"scenario {s['id']} has an unscoped HTTP criterion: {crit!r}",
                    )

    def test_steps_and_pass_criteria_are_non_empty_lists(self) -> None:
        for s in ACCEPTANCE_SCENARIOS:
            self.assertIsInstance(s["steps"], list)
            self.assertGreater(len(s["steps"]), 0, f"scenario {s['id']} has no steps")
            self.assertIsInstance(s["pass_criteria"], list)
            self.assertGreater(len(s["pass_criteria"]), 0, f"scenario {s['id']} has no pass criteria")

    def test_enable_and_disable_are_one_step_parity(self) -> None:
        # contracts §5: enable/disable MUST be one-step (parity with
        # OrchestraRunner.start()). The scenario steps must reflect that.
        enable = next(s for s in ACCEPTANCE_SCENARIOS if s["id"] == "B")
        self.assertTrue(any("enable discord-adaptive" in step for step in enable["steps"]),
                        "scenario B must call `zapret2-orchestra-profile enable discord-adaptive`")
        disable = next(s for s in ACCEPTANCE_SCENARIOS if s["id"] == "G")
        self.assertTrue(any("disable" in step for step in disable["steps"]),
                        "scenario G must call disable")


class RegressionRuleTest(unittest.TestCase):
    """The r6→r7 reconciliation did NOT drop or weaken the r6 guards. These
    are STATIC checks on the reconciled test modules + workflow — they run
    here (no router, no ucode) and confirm the r6 protections are still
    present, now expecting r7 values where the contract bumped."""

    # --- r6 sentinel fix must not regress (test_cli_sentinel.py) ---

    def test_sentinel_test_module_present(self) -> None:
        self.assertTrue(SENTINEL_TEST.is_file(), "test_cli_sentinel.py must remain")

    def test_sentinel_static_guards_retained(self) -> None:
        text = SENTINEL_TEST.read_text(encoding="utf-8")
        # The r6 sentinel fix's deterministic static guards (apply.uc offset /
        # ARGV[0]=='--' / single-if-not-while) must still be asserted.
        self.assertIn("ARGV[0]", text)
        self.assertIn("'--'", text)
        self.assertIn("offset", text)
        # The runtime wrapper/profile-frontend checks must still be present.
        self.assertIn("ShippedCliSentinelRuntimeTest", text)
        self.assertIn("zapret2-orchestra-apply", text)
        self.assertIn("zapret2-orchestra-profile", text)

    def test_sentinel_test_does_not_require_new_r7_profiles(self) -> None:
        # The sentinel test must NOT have been broken by adding the new r7
        # profiles to its hardcoded validate list. It should still validate the
        # r6 set (a subset is fine). Confirm it does NOT reference discord-v5/
        # discord-adaptive as a required sentinel-validation target (those are
        # ready-profile-contract concerns, not sentinel concerns).
        text = SENTINEL_TEST.read_text(encoding="utf-8")
        # It is OK for the sentinel test to validate the 6 r6 profiles; it must
        # not FAIL if discord-adaptive/v5 are absent (those are post-integration).
        # Assert the r6 six are still the validated set (no hardcoded new ids).
        self.assertNotIn('"discord-adaptive"', text)
        self.assertNotIn('"discord-v5"', text)

    # --- r6 ready-profile-contract stays green after r7 ---

    def test_ready_profile_test_present(self) -> None:
        self.assertTrue(READY_PROFILE_TEST.is_file(), "test_ready_profile_contract.py must remain")

    def test_ready_profile_test_reconciled_to_r7_release(self) -> None:
        text = READY_PROFILE_TEST.read_text(encoding="utf-8")
        # The r6 release assertion (PKG_RELEASE:=6) must be bumped to 7.
        self.assertRegex(text, r"PKG_RELEASE:=7",
                         "test_ready_profile_contract.py must assert orchestra PKG_RELEASE=7")
        self.assertNotRegex(text, r"test_orchestra_release_is_six",
                            "the r6 'release_is_six' test name must be reconciled to r7")

    def test_ready_profile_test_allows_native_discord_v5_without_circular_quality(self) -> None:
        # The reconciled content test must NOT require circular_quality for
        # every ready profile: the native discord-v5 is allowed without it.
        # We assert the module distinguishes circular vs native profiles.
        text = READY_PROFILE_TEST.read_text(encoding="utf-8")
        # Either a NATIVE set / exclusion, or the circular_quality assertion is
        # scoped to circular profiles only.
        self.assertTrue(
            "discord-v5" in text or "NATIVE" in text or "native" in text.lower(),
            "test_ready_profile_contract.py must account for the native discord-v5 profile",
        )

    # --- r6 package-contract stays green after r7 ---

    def test_package_test_present(self) -> None:
        self.assertTrue(PACKAGE_TEST.is_file(), "test_package_contract.py must remain")

    def test_package_test_reconciled_to_r7_release(self) -> None:
        text = PACKAGE_TEST.read_text(encoding="utf-8")
        self.assertRegex(text, r'"PKG_RELEASE":\s*"7"',
                         "test_package_contract.py must expect orchestra PKG_RELEASE=7")
        # zapret2 stays r3.
        self.assertRegex(text, r'"PKG_RELEASE":\s*"3"')

    def test_package_test_r6_lifecycle_guards_retained(self) -> None:
        text = PACKAGE_TEST.read_text(encoding="utf-8")
        # The r6 lifecycle guards (ipkg-instroot, no /etc/init.d/zapret2 in the
        # orchestra Makefile, no uci/nft/firewall/apk) must still be asserted.
        self.assertIn("IPKG_INSTROOT", text)
        self.assertIn("rpcd restart", text)
        self.assertIn("zapret2-orchestra enable", text)

    # --- workflow Stage 3 reconciled to r7 ---

    def test_workflow_stage3_release_regex_is_r7(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        # The orchestra release regex must be r7 (not r6). The YAML escapes the
        # dots in its grep -E regexes, so the literal text carries '\.'; the
        # patterns match that literal backslash-dot.
        self.assertRegex(text, r"zapret2-orchestra-\.\*-r7\\.apk",
                         "workflow Stage 3 must expect the r7 APK name regex")
        self.assertRegex(text, r"\^PKG_RELEASE:=7\$",
                         "workflow Stage 3 must grep PKG_RELEASE:=7")
        # The r6 regex must be gone (match the escaped form the workflow uses).
        self.assertNotRegex(text, r"zapret2-orchestra-\.\*-r6\\.apk",
                            "workflow Stage 3 must not still expect r6")

    def test_workflow_audits_new_r7_assets(self) -> None:
        # The workflow's APK extraction audit must check the new r7 assets are
        # present in the built APK: discord-adaptive.opt, discord-adaptive.json,
        # discord-v5.opt, init_vars.lua, ipset-discord.txt, learner.uc, learner
        # init.d. (These are contract §6 test hooks.)
        text = WORKFLOW.read_text(encoding="utf-8")
        for asset in (
            "discord-adaptive.opt",
            "discord-adaptive.json",
            "discord-v5.opt",
            "init_vars.lua",
            "ipset-discord.txt",
        ):
            self.assertIn(asset, text, f"workflow must audit the new r7 asset {asset}")

    def test_workflow_has_learner_service_presence_check(self) -> None:
        # The workflow must include a learner-service presence check (init.d
        # or sbin) appropriate to B's learner. We assert the workflow mentions
        # the learner service name.
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertTrue(
            "learner" in text.lower(),
            "workflow must include a learner-service presence check",
        )

    def test_workflow_validate_and_build_jobs_not_broken(self) -> None:
        # The existing validate + build-apk job structure must remain.
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("validate:", text)
        self.assertIn("build-apk:", text)
        self.assertIn("needs: validate", text)
        self.assertIn("workflow_dispatch", text)
        self.assertIn("submodules: recursive", text)
        self.assertIn("package/zapret2/compile", text)
        self.assertIn("package/zapret2-orchestra/compile", text)
        self.assertIn("staging_dir/host/bin/apk", text)


if __name__ == "__main__":
    unittest.main()
