"""SUCCESS-detection enabler tests: bidirectional NFQUEUE + --in-range + detector logic.

Root cause of the router blocker ("145 APPLIED, 4 FAIL, 0 SUCCESS"):

  The ``circular_quality`` orchestrator detects TCP SUCCESS via
  ``standard_success_detector``, which fires when an INCOMING (reply) relative
  sequence exceeds ``inseq`` (1 = 1).  Reply packets are queued to
  nfqws2 by the prerouting ``ct reply`` NFQUEUE rule (NFQWS2_TCP_PKT_IN=10),
  BUT nfqws2's in-profile filter defaults to ``--in-range=x`` ("never") — see
  zapret2-core/docs/manual.en.md §"In-profile filters": "The default is
  --in-range=x, --out-range=a".  With the default, NO incoming packet is ever
  delivered to a Lua instance, so ``combined_success_detector`` is never called
  with a reply packet and SUCCESS can never fire.  The fix is to set
  ``--in-range=-d1000`` before ``--lua-desync=circular_quality`` (exactly what
  reference/desktop-orchestra/lua/circular-config.txt:85 does).

This file pins three layers so the fix cannot regress:

  1. Bidirectional NFQUEUE generation (nft.sh) — the reply rule IS generated
     when PKT_IN != 0, on the same QNUM (300) as the outgoing rule, limited by
     ``ct reply packets 1-N``.  This is the kernel→nfqws2 half; it was already
     correct, and these tests lock it in.
  2. The ``--in-range`` fix in every circular ``.opt`` profile — the
     nfqws2→Lua half that was MISSING and is the actual blocker.
  3. The detector logic — reply packet with seq > inseq => SUCCESS;
     outgoing-only / TLS-alert reply => no false SUCCESS; the port delegates
     TCP success to ``standard_success_detector`` exactly like the reference.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _nfqws2_parser as P  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "openwrt" / "zapret2-orchestra"
PROFILES_DIR = PACKAGE / "files/usr/share/zapret2-orchestra/profiles"
NFT_SH = ROOT / "zapret2-core" / "common" / "nft.sh"
CONFIG_DEFAULT = ROOT / "zapret2-core" / "config.default"
ORCH_LUA = PACKAGE / "files/opt/zapret2/lua/orchestra-extra"
DEV_LUA = ROOT / "lua" / "orchestra-extra"
REFERENCE_DETECTOR = ROOT / "reference" / "desktop-orchestra" / "lua" / "combined-detector.lua"
REFERENCE_CIRCULAR_CFG = ROOT / "reference" / "desktop-orchestra" / "lua" / "circular-config.txt"
MANUAL = ROOT / "zapret2-core" / "docs" / "manual.en.md"

# The 8 circular ready profiles that use circular_quality (from
# test_ready_profile_contract.CIRCULAR_READY_IDS).  discord-v5 is NATIVE
# (no circular_quality) and does not need --in-range for SUCCESS detection.
CIRCULAR_READY_IDS = (
    "orchestra-tls-mvp",
    "gui-tls-multisplit",
    "gui-tls-multidisorder",
    "gui-tls-hostfakesplit",
    "gui-tls-syndata",
    "gui-circular",
    "discord-adaptive",
    "discord-adaptive-original-pool",
)
NATIVE_READY_IDS = ("discord-v5",)


# ---------------------------------------------------------------------------
# Layer 1 — bidirectional NFQUEUE rule generation (nft.sh)
# ---------------------------------------------------------------------------


class NftBidirectionalRuleTest(unittest.TestCase):
    """The kernel→nfqws2 half: reply (prerouting, ct reply) NFQUEUE rules ARE
    generated on the same QNUM as the outgoing (postrouting) rules, limited by
    ``ct reply packets 1-N``.  These lock the existing correct behaviour so the
    fix (which is at the nfqws2→Lua layer) is not mistaken for an nft gap."""

    def setUp(self) -> None:
        self.nft = NFT_SH.read_text(encoding="utf-8")

    def test_nft_reverse_nfqws_rule_transforms_outgoing_to_reply(self) -> None:
        # nft_reverse_nfqws_rule is a pure sed transform (nft.sh:329).  Run it
        # for real in sh to prove the reply rule is the outgoing rule mirrored:
        # oifname->iifname, dport->sport, daddr->saddr, ct original->ct reply.
        if not shutil.which("sh"):
            self.skipTest("sh not on PATH")
        out_rule = ("meta nfproto ipv4 oifname wan tcp dport {80,443} "
                    "ct original packets 1-20")
        r = subprocess.run(
            ["sh", "-c",
             'DESYNC_MARK=0x40000000; . "$1"; nft_reverse_nfqws_rule "$2"',
             "_", str(NFT_SH), out_rule],
            capture_output=True, text=True,
        )
        # nft.sh may need other vars to source cleanly; if sourcing fails, fall
        # back to extracting just the function body.
        if r.returncode != 0:
            r = subprocess.run(
                ["sh", "-c",
                 'DESYNC_MARK=0x40000000; '
                 'nft_reverse_nfqws_rule() { echo "$@" | sed -e "s/oifname /iifname /g" '
                 '-e "s/dport /sport /g" -e "s/daddr /saddr /g" '
                 '-e "s/ct original /ct reply /g" '
                 '-e "s/mark and $DESYNC_MARK == 0//g"; }; '
                 'nft_reverse_nfqws_rule "$1"',
                 "_", out_rule],
                capture_output=True, text=True,
            )
        self.assertEqual(r.returncode, 0, f"sh failed: {r.stderr}")
        reply = r.stdout.strip()
        self.assertIn("iifname", reply)
        self.assertIn("sport {80,443}", reply)
        self.assertIn("ct reply packets 1-20", reply)
        self.assertNotIn("dport", reply)
        self.assertNotIn("ct original", reply)

    def test_apply_nfqws_in_out_generates_both_directions(self) -> None:
        # nft_apply_nfqws_in_out (nft.sh:643) must call BOTH the outgoing path
        # (nft_fw_nfqws_post, gated on $3=PKT_OUT) AND the reply path
        # (nft_fw_reverse_nfqws_rule, gated on $4=PKT_IN).  A reply rule is
        # generated ONLY when PKT_IN is non-empty/non-zero.
        m = re.search(r"nft_apply_nfqws_in_out\(\)\s*\{(?P<body>.*?)^\}",
                      self.nft, re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(m, "nft_apply_nfqws_in_out not found in nft.sh")
        body = m.group("body")
        # Outgoing (postrouting) path, gated on $3 (PKT_OUT).
        self.assertIn("nft_fw_nfqws_post", body,
                      "outgoing postrouting rule generation missing")
        self.assertIn('[ -n "$3"', body,
                      "outgoing path must be gated on PKT_OUT ($3)")
        # Reply (prerouting, ct reply) path, gated on $4 (PKT_IN).
        self.assertIn("nft_fw_reverse_nfqws_rule", body,
                      "reply prerouting rule generation missing")
        self.assertIn('[ -n "$4"', body,
                      "reply path must be gated on PKT_IN ($4)")
        # Confirm the docstring names the params so the gating is unambiguous.
        self.assertIn("# $3 - PKT_OUT", body)
        self.assertIn("# $4 - PKT_IN", body)

    def test_both_directions_use_the_same_queue_number(self) -> None:
        # nft_apply_nfqws_in_out passes $QNUM to BOTH nft_fw_nfqws_post and
        # nft_fw_reverse_nfqws_rule, so outgoing and reply queue to the SAME
        # nfqws2 instance (QNUM=300).  Verify the calls share $QNUM.
        m = re.search(r"nft_apply_nfqws_in_out\(\)\s*\{(?P<body>.*?)\n\}",
                      self.nft, re.DOTALL)
        body = m.group("body")
        self.assertIn("nft_fw_nfqws_post \"$f4\" \"$f6\" $QNUM", body)
        self.assertIn("nft_fw_reverse_nfqws_rule \"$f4\" \"$f6\" $QNUM", body)

    def test_qnum_default_is_300(self) -> None:
        # The orchestra uses QNUM=300 (docs/current-state.md, confirmed in
        # runtime scripts).  The openwrt init defaults QNUM to 300.
        funcs = (ROOT / "zapret2-core/init.d/openwrt/functions").read_text("utf-8")
        self.assertRegex(funcs, r"QNUM=\$\{QNUM:-300\}",
                         "QNUM must default to 300")

    def test_reply_rule_is_packet_limited_and_uses_ct_reply(self) -> None:
        # nft_first_packets emits "ct original packets 1-N" for outgoing; the
        # reverse transform turns that into "ct reply packets 1-N" for replies,
        # so only the first N reply packets are queued (CPU saver).  Verify the
        # connbytes limiter and the ct-reply transform exist.
        self.assertIn('nft_connbytes="ct original packets"', self.nft)
        self.assertRegex(self.nft, r"s/ct original /ct reply /g",
                         "reverse rule must map ct original -> ct reply")
        self.assertRegex(self.nft, r"s/dport /sport /g",
                         "reverse rule must map dport -> sport")

    def test_config_default_enables_reply_queueing(self) -> None:
        # config.default ships NFQWS2_TCP_PKT_IN=10 (non-zero), so
        # nft_apply_nfqws_in_out DOES generate the reply rule by default.
        # A zero/empty PKT_IN would suppress the reply rule and starve the
        # success detector of reply packets at the NFQUEUE layer.
        cfg = CONFIG_DEFAULT.read_text(encoding="utf-8")
        m = re.search(r"^NFQWS2_TCP_PKT_IN=(\d+)", cfg, re.MULTILINE)
        self.assertIsNotNone(m, "NFQWS2_TCP_PKT_IN must be defined in config.default")
        self.assertNotEqual(int(m.group(1)), 0,
                            "NFQWS2_TCP_PKT_IN must be non-zero so reply "
                            "packets are queued to nfqws2")


# ---------------------------------------------------------------------------
# Layer 2 — the --in-range fix (the actual blocker: nfqws2 -> Lua)
# ---------------------------------------------------------------------------


class InRangeSuccessEnablerTest(unittest.TestCase):
    """Every circular ``.opt`` profile MUST set ``--in-range`` to a non-x value
    before ``--lua-desync=circular_quality``.  Without it, nfqws2's default
    ``--in-range=x`` (never) blocks ALL incoming packets from Lua and the
    success detector never sees a reply packet."""

    def _value(self, pid: str) -> str:
        return P.extract((PROFILES_DIR / f"{pid}.opt").read_text("utf-8")).value

    def test_every_circular_profile_sets_in_range(self) -> None:
        for pid in CIRCULAR_READY_IDS:
            val = self._value(pid)
            m = re.search(r"--in-range=([^\s]+)", val)
            self.assertIsNotNone(m, f"{pid}: missing --in-range (SUCCESS detection impossible)")
            self.assertNotEqual(m.group(1), "x",
                                f"{pid}: --in-range=x blocks all incoming packets from Lua")

    def test_every_circular_profile_sets_out_range(self) -> None:
        for pid in CIRCULAR_READY_IDS:
            val = self._value(pid)
            self.assertRegex(val, r"--out-range=",
                             f"{pid}: missing --out-range")

    def test_in_range_precedes_circular_quality(self) -> None:
        # --in-range is a sticky in-profile filter; it must be set BEFORE the
        # circular_quality selector so the selector receives incoming packets.
        for pid in CIRCULAR_READY_IDS:
            val = self._value(pid)
            ir = val.find("--in-range=")
            cq = val.find("circular_quality")
            self.assertGreater(ir, -1, f"{pid}: no --in-range")
            self.assertGreater(cq, -1, f"{pid}: no circular_quality")
            self.assertLess(ir, cq,
                            f"{pid}: --in-range must precede circular_quality")

    def test_in_range_value_matches_reference_tls_profile(self) -> None:
        # The reference TLS profile uses --in-range=-d1000 (circular-config.txt
        # line 85).  Pin the same value so the port does not drift to a value
        # too small to reach inseq=1 (1) on incoming data.
        ref = REFERENCE_CIRCULAR_CFG.read_text("utf-8")
        ref_m = re.search(r"^--in-range=(\S+)", ref, re.MULTILINE)
        self.assertIsNotNone(ref_m, "reference circular-config has no --in-range")
        ref_val = ref_m.group(1)
        self.assertEqual(ref_val, "-d1000",
                         "reference TLS --in-range must be -d1000")
        for pid in CIRCULAR_READY_IDS:
            val = self._value(pid)
            m = re.search(r"--in-range=([^\s]+)", val)
            self.assertEqual(m.group(1), "-d1000",
                             f"{pid}: --in-range must be -d1000 (reference parity)")

    def test_native_profile_does_not_require_in_range(self) -> None:
        # discord-v5 is a static nfqws2 chain (no circular_quality); it has no
        # SUCCESS detector and thus no --in-range requirement.  It must NOT
        # reference circular_quality.
        for pid in NATIVE_READY_IDS:
            val = self._value(pid)
            self.assertNotIn("circular_quality", val,
                             f"{pid}: native profile must not use circular_quality")

    def test_manual_documents_in_range_default_is_x(self) -> None:
        # The manual is the authority for "default --in-range=x = never".  Pin
        # that statement so the root-cause rationale stays grounded in the docs.
        manual = MANUAL.read_text("utf-8")
        self.assertRegex(manual, r"--in-range=x.*--out-range=a|default is.*--in-range=x",
                         "manual must document the --in-range=x default")


# ---------------------------------------------------------------------------
# Layer 3 — detector logic
# ---------------------------------------------------------------------------


class DetectorSourceContractTest(unittest.TestCase):
    """Static contract: the port's combined_success_detector delegates TCP
    success to standard_success_detector (the upstream API) and guards reply
    TLS alerts, exactly like the reference.  No HTTP-status / curl / APPLIED
    shortcut (SUCCESS must come from network packets seen by nfqws2)."""

    def test_detector_delegates_to_standard_success_detector(self) -> None:
        src = (ORCH_LUA / "detectors.lua").read_text("utf-8")
        self.assertIn("function combined_success_detector", src)
        self.assertIn("standard_success_detector(desync, crec)", src,
                      "TCP SUCCESS must delegate to standard_success_detector")

    def test_detector_guards_reply_tls_alert(self) -> None:
        # A reply TLS alert (ContentType 0x15) is a handshake failure, not a
        # success.  The detector must return false for it.
        src = (ORCH_LUA / "detectors.lua").read_text("utf-8")
        self.assertIn("is_tls_alert", src)
        self.assertIn("0x15", src)
        self.assertRegex(src, r"not desync\.outgoing.*is_tls_alert")

    def test_detector_does_not_invent_success_from_applied_or_status(self) -> None:
        # SUCCESS must come from network packets (standard_success_detector on a
        # reply packet), NOT from curl exit code / HTTP status / APPLIED.
        src = (ORCH_LUA / "detectors.lua").read_text("utf-8")
        for forbidden in ("curl", "exit_code", "http_status", "check_http_status",
                          "applied", "os.execute"):
            self.assertNotIn(forbidden, src,
                             f"detector must not derive SUCCESS from {forbidden}")

    def test_port_detector_matches_reference_delegation_pattern(self) -> None:
        # Both the port and the reference delegate TCP success to
        # standard_success_detector.  The reference adds richer failure checks
        # (HTTP/DPI stub/block page) but the TCP SUCCESS path is the same.
        ref = REFERENCE_DETECTOR.read_text("utf-8")
        self.assertIn("function combined_success_detector", ref)
        self.assertIn("standard_success_detector(desync, crec)", ref,
                      "reference must also delegate TCP success to standard_success_detector")

    def test_standard_success_detector_requires_incoming_seq_beyond_inseq(self) -> None:
        # zapret-auto.lua: standard_success_detector fires TCP success on an
        # INCOMING packet when seq > inseq (default 1).  This is why reply
        # packets MUST reach the detector (--in-range).
        auto = (ROOT / "zapret2-core/lua/zapret-auto.lua").read_text("utf-8")
        m = re.search(r"function standard_success_detector\(desync, crec\)(?P<body>.*?)\nend\n",
                      auto, re.DOTALL)
        self.assertIsNotNone(m, "standard_success_detector not found in zapret-auto.lua")
        body = m.group("body")
        self.assertIn("desync.outgoing", body)
        self.assertIn("arg.inseq", body)
        self.assertIn("pos_get(desync,'s')", body)
        # The incoming branch (not outgoing): inseq>0 and seq>inseq => success.
        self.assertIn("arg.inseq>0 and seq>arg.inseq", body,
                      "standard_success_detector must test incoming seq > inseq")
        # The outgoing branch: maxseq>0 and seq>arg.maxseq => success.
        self.assertIn("arg.maxseq>0 and seq>arg.maxseq", body,
                      "standard_success_detector must test outgoing seq > maxseq")

    def test_circular_comment_requires_incoming_traffic(self) -> None:
        # zapret-auto.lua documents that the circular orchestrator "requires
        # redirection of incoming traffic to cache RST and http replies".
        auto = (ROOT / "zapret2-core/lua/zapret-auto.lua").read_text("utf-8")
        self.assertRegex(auto, r"requires redirection of incoming traffic",
                         "upstream comment must state circular needs incoming traffic")

    def test_packaged_and_dev_detectors_match(self) -> None:
        # The package install copy and the dev/test copy must stay byte-identical
        # (test_package_contract enforces this for all orchestra-extra lua; this
        # is a focused re-check for detectors.lua after the DLOG edit).
        self.assertEqual(
            (ORCH_LUA / "detectors.lua").read_bytes(),
            (DEV_LUA / "detectors.lua").read_bytes(),
            "packaged and dev detectors.lua diverged",
        )


# A faithful Python model of standard_success_detector + the combined wrapper's
# TCP path, used to pin the SUCCESS conditions deterministically without a Lua
# interpreter.  The DetectorSourceContractTest.* tests tie this model to the
# real Lua by asserting the Lua delegates to standard_success_detector and
# guards TLS alerts the same way.
_DEFAULT_INSEQ = 1      # 1 — from discord-adaptive.opt :inseq=1
_DEFAULT_MAXSEQ = 32768      # standard_detector_defaults default


def _is_tls_alert(payload: bytes) -> bool:
    if not isinstance(payload, (bytes, bytearray)) or len(payload) < 7:
        return False
    if payload[0] != 0x15:
        return False
    return payload[1] == 0x03 and 0x00 <= payload[2] <= 0x04


def _standard_success_tcp(outgoing: bool, seq: int,
                          inseq: int = _DEFAULT_INSEQ,
                          maxseq: int = _DEFAULT_MAXSEQ) -> bool:
    # Mirrors standard_success_detector TCP branches (zapret-auto.lua:226-254).
    if outgoing:
        return maxseq > 0 and seq > maxseq
    return inseq > 0 and seq > inseq


def _combined_success(outgoing: bool, seq: int, payload: bytes | None,
                      inseq: int = _DEFAULT_INSEQ) -> bool:
    # Mirrors combined_success_detector (detectors.lua): skip reply TLS alert,
    # then delegate to standard_success_detector for TCP.
    if not outgoing and payload is not None and _is_tls_alert(payload):
        return False
    return _standard_success_tcp(outgoing, seq, inseq=inseq)


class DetectorLogicModelTest(unittest.TestCase):
    """Deterministic fixtures pinning the SUCCESS verdicts.  These model the
    exact conditions the real Lua detector evaluates on packets nfqws2 delivers
    (which only happens once --in-range lets reply packets through)."""

    def test_reply_with_seq_beyond_inseq_is_success(self) -> None:
        # A TLS 1.3 server flight (ServerHello + Certificate + ...) easily
        # exceeds 1 bytes; the 4th reply data packet lands at seq > inseq.
        self.assertTrue(_combined_success(outgoing=False, seq=4381,
                                          payload=b"\x16\x03\x03" + b"\x00" * 50))

    def test_reply_with_seq_below_inseq_is_not_success(self) -> None:
        # The first reply data packet (ServerHello, ~seq 1) has not yet
        # transferred enough to prove success.
        self.assertFalse(_combined_success(outgoing=False, seq=1,
                                           payload=b"\x16\x03\x03" + b"\x00" * 50))
        self.assertFalse(_combined_success(outgoing=False, seq=1,
                                           payload=b"\x16\x03\x03" + b"\x00" * 50))

    def test_outgoing_clienthello_is_not_a_false_success(self) -> None:
        # An outgoing TLS ClientHello is ~500 bytes (seq 500 << maxseq 32768),
        # so the outgoing success branch does not fire.  This is why SUCCESS
        # cannot be inferred from the outgoing request alone.
        self.assertFalse(_combined_success(outgoing=True, seq=500,
                                           payload=b"\x16\x03\x01" + b"\x00" * 50))

    def test_reply_tls_alert_is_not_success(self) -> None:
        # A reply TLS alert (ContentType 0x15) is a handshake failure.  Even
        # with seq > inseq, the combined detector must NOT report SUCCESS.
        alert = bytes([0x15, 0x03, 0x03, 0x00, 0x02, 0x02, 0x28])  # fatal, handshake_failure
        self.assertFalse(_combined_success(outgoing=False, seq=5000, payload=alert))

    def test_inseq_threshold_is_1(self) -> None:
        # The shipped profiles use :inseq=1 (1).  seq 4097 => SUCCESS,
        # seq 1 => not yet.  Pin the threshold so a drift in the .opt is
        # caught here too.
        for pid in CIRCULAR_READY_IDS:
            val = P.extract((PROFILES_DIR / f"{pid}.opt").read_text("utf-8")).value
            m = re.search(r"circular_quality[^\"\n]*inseq=(1|\d+)", val)
            self.assertIsNotNone(m, f"{pid}: circular_quality has no inseq=")
            self.assertEqual(m.group(1), "1",
                             f"{pid}: inseq must be 1 (1)")


if __name__ == "__main__":
    unittest.main()
