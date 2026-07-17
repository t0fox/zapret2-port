"""Host-side checks that do not need a router or a Lua interpreter."""

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "lua" / "orchestra-extra"


class TlsRuntimeStaticTests(unittest.TestCase):
    def test_state_seed_is_schema_v1(self):
        state_dir = ROOT / "etc" / "zapret2-orchestra"
        for name in ("learned.json", "blocked.json", "whitelist.json", "manual-locks.json"):
            state = json.loads((state_dir / name).read_text(encoding="utf-8"))
            self.assertEqual(state["schema_version"], 1)
        self.assertEqual(set(json.loads((state_dir / "blocked.json").read_text(encoding="utf-8"))["protocols"]), {"tls"})


    def test_runtime_has_the_required_tls_entry_points(self):
        source = "\n".join(path.read_text(encoding="utf-8") for path in RUNTIME.glob("*.lua"))
        for name in (
            "circular_quality", "combined_failure_detector", "combined_success_detector",
            "orchestra_set_manual_lock", "orchestra_clear_manual_lock", "slm_should_lock",
        ):
            self.assertIn("function " + name, source)


    def test_packet_runtime_never_writes_persistent_state(self):
        sources = {path.name: path.read_text(encoding="utf-8") for path in RUNTIME.glob("*.lua")}
        source = "\n".join(sources.values())
        self.assertNotIn("os.execute", source)
        self.assertNotIn("/etc/", source)
        for name, text in sources.items():
            if name != "events.lua":
                self.assertNotIn("io.open", text)
        self.assertIn('/tmp/zapret2-orchestra/events.ndjson', sources["events.lua"])
        for persistent_name in ("learned.json", "blocked.json", "whitelist.json", "manual-locks.json"):
            self.assertNotIn(persistent_name, source)


    def test_detector_preserves_requested_transport_rules(self):
        source = (RUNTIME / "detectors.lua").read_text(encoding="utf-8")
        self.assertIn("crec.orchestra_success_confirmed", source)
        self.assertNotIn("check_http_status", source)


    def test_router_upstream_was_not_replaced(self):
        router = ROOT / "router-baseline" / "opt" / "zapret2" / "lua"
        desktop = ROOT / "reference" / "desktop-orchestra" / "lua"
        if not router.is_dir():
            self.skipTest("router-baseline is local untracked evidence and is unavailable")
        self.assertEqual((router / "zapret-auto.lua").read_bytes(), (desktop / "zapret-auto.lua").read_bytes())
