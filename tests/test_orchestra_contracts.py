"""Schema validation for the FROZEN r7 contracts (catalog / events / state).

Asserts the three contracts pinned in ``docs/ORCHESTRA_R7_CONTRACTS.md``:

  * §1 catalog entry schema — every required field present and well-typed;
    ``stable_id`` independent of position; ``chain_id`` = sha256 of normalized
    ``lua_steps``; ``strategy_number`` a positive int; compatibility status in
    the allowed set; provenance fields present; required_assets carry sha256 +
    source.
  * §2 machine event schema — each NDJSON line is schema_version=1 with ts,
    type in the allowed set, askey in the 9 profiles, normalized host, positive
    int strategy, chain_id, reason, generation, run_id. Packet payloads/dumps
    are rejected.
  * §3 persistent state schema — learned.json (auto_lock + strategies with
    successes/failures), blocked.json (global/hosts + NEW user_global/
    user_hosts), whitelist.json, manual-locks.json, learner-state.json
    (event_cursor{bytes,lines,last_line_sha256}, last_preload_gen, run_id).
    schema_version=1 everywhere.

These are pure schema tests: they validate JSON *shapes* against the contract.
They do NOT import A's importer or B's learner (those are parallel). The
catalog/events fixtures come from A's ``strategy-sources/catalog.json`` and
B's runtime event stream; when those artifacts are absent (pre-integration)
the catalog/event tests skip cleanly with a documented reason, and the
state-schema tests run against synthetic in-memory documents built from the
contract so the schema validators themselves are always exercised.

Integration note: after A lands ``strategy-sources/catalog.json`` and B lands
the extended event emitter + learner-state writer, remove the skip-guards
(or they self-disable when the artifacts appear). The schema validators
(``validate_catalog``, ``validate_event_line``, ``validate_state_file``) are
the frozen contract surface A/B converge on.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "strategy-sources" / "catalog.json"

# The 9 ASKEY protocol profiles (contracts §2 / spec §1.2).
ASKEY_ALL = (
    "tls", "http", "quic", "discord", "wireguard", "mtproto", "dns", "stun",
    "unknown",
)

# Event types (contracts §2). SUCCESS/FAIL/LOCK/UNLOCK/APPLIED/ROTATE are the
# r7 learning signals; error/start/stop are the retained lifecycle types.
EVENT_TYPES = {
    "SUCCESS", "FAIL", "LOCK", "UNLOCK", "APPLIED", "ROTATE",
    "error", "start", "stop",
}

# The event types that require the full askey/host/strategy/chain_id field set.
FULL_FIELD_TYPES = {"SUCCESS", "FAIL", "LOCK", "UNLOCK", "APPLIED", "ROTATE"}

# Pinned upstream provenance (contracts header). Tests assert the catalog's
# source provenance matches these exact pins, so the catalog is auditable back
# to the frozen upstream.
PINNED_PRESET_REPO = "youtubediscord/zapret2-youtube-discord"
PINNED_PRESET_COMMIT = "4d75c70b430562e970bcf64cbe24072ce104b36a"
PINNED_GUI_REPO = "youtubediscord/zapret"
PINNED_GUI_COMMIT = "9d57e55d6751587d9d52b52147a05a0a8fcc9fd8"
PINNED_GUI_PATH = "src/orchestra/blocked_strategies_manager.py:65-102"


# ---------------------------------------------------------------------------
# Schema validators (the frozen contract surface — A/B converge here)
# ---------------------------------------------------------------------------

def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_str(v: Any) -> bool:
    return isinstance(v, str) and len(v) > 0


def _is_sha256(v: Any) -> bool:
    return (
        isinstance(v, str)
        and len(v) == 64
        and all(c in "0123456789abcdef" for c in v)
    )


def _normalized_lua_steps_hash(lua_steps: Any) -> str:
    """sha256 of the canonical, normalized lua_steps serialization
    (sorted args keys, canonical JSON) — the contract's chain_id definition."""
    if not isinstance(lua_steps, list):
        raise ValueError("lua_steps must be a list")
    norm = []
    for step in lua_steps:
        if not isinstance(step, dict):
            raise ValueError("each lua_step must be an object")
        func = step.get("func")
        args = step.get("args", {})
        if not _is_str(func):
            raise ValueError("lua_step.func must be a non-empty string")
        if not isinstance(args, dict):
            raise ValueError("lua_step.args must be an object")
        # sorted args keys, canonical JSON (separators to mimic compact form).
        norm.append({"func": func, "args": dict(sorted(args.items()))})
    canonical = json.dumps(norm, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_catalog(doc: Any) -> list[str]:
    """Return a list of contract violations for a catalog document (empty = ok)."""
    problems: list[str] = []
    if not isinstance(doc, dict):
        return ["catalog is not a JSON object"]
    if doc.get("schema_version") != 1:
        problems.append("catalog.schema_version must be 1")
    if doc.get("catalog_version") != 1:
        problems.append("catalog.catalog_version must be 1")

    src = doc.get("source") or {}
    if not isinstance(src, dict):
        problems.append("catalog.source must be an object")
    else:
        if src.get("repo") != PINNED_PRESET_REPO:
            problems.append(f"source.repo must be {PINNED_PRESET_REPO!r}")
        if src.get("commit") != PINNED_PRESET_COMMIT:
            problems.append("source.commit must be the pinned preset SHA")
        if not _is_str(src.get("presets_path")):
            problems.append("source.presets_path must be a non-empty string")

    dbpd = doc.get("default_blocked_pass_domains") or {}
    if not isinstance(dbpd, dict):
        problems.append("default_blocked_pass_domains must be an object")
    else:
        if dbpd.get("source_repo") != PINNED_GUI_REPO:
            problems.append(f"default_blocked_pass_domains.source_repo must be {PINNED_GUI_REPO!r}")
        if dbpd.get("source_commit") != PINNED_GUI_COMMIT:
            problems.append("default_blocked_pass_domains.source_commit must be the pinned GUI SHA")
        if dbpd.get("source_path") != PINNED_GUI_PATH:
            problems.append("default_blocked_pass_domains.source_path must be the pinned GUI path")
        domains = dbpd.get("domains")
        if not isinstance(domains, list) or not all(_is_str(d) for d in domains):
            problems.append("default_blocked_pass_domains.domains must be a list of non-empty strings")
        elif "discord.com" not in domains:
            problems.append("default_blocked_pass_domains.domains must contain discord.com")

    entries = doc.get("entries")
    if not isinstance(entries, list) or len(entries) == 0:
        problems.append("catalog.entries must be a non-empty list")
        return problems
    seen_stable_ids: set[str] = set()
    seen_chain_ids: set[str] = set()
    for i, entry in enumerate(entries):
        problems.extend(_validate_entry(entry, i, seen_stable_ids, seen_chain_ids))
    return problems


def _validate_entry(entry: Any, i: int, seen_stable_ids: set[str],
                    seen_chain_ids: set[str]) -> list[str]:
    problems: list[str] = []
    ctx = f"entries[{i}]"
    if not isinstance(entry, dict):
        return [f"{ctx}: entry must be a JSON object"]
    # stable_id
    sid = entry.get("stable_id")
    if not _is_str(sid):
        problems.append(f"{ctx}: stable_id must be a non-empty string")
    elif sid in seen_stable_ids:
        problems.append(f"{ctx}: stable_id {sid!r} duplicated")
    else:
        seen_stable_ids.add(sid)
    # source provenance
    for field in ("source_id", "source_commit", "source_path", "source_sha256"):
        v = entry.get(field)
        if not _is_str(v):
            problems.append(f"{ctx}: {field} must be a non-empty string")
    if _is_str(entry.get("source_sha256")) and not _is_sha256(entry["source_sha256"]):
        problems.append(f"{ctx}: source_sha256 must be a 64-hex sha256")
    if _is_str(entry.get("source_commit")) and entry["source_commit"] != PINNED_PRESET_COMMIT:
        problems.append(f"{ctx}: source_commit must be the pinned preset SHA")
    if _is_int(entry.get("source_block_index")) is False:
        # optional but if present must be a non-negative int
        v = entry.get("source_block_index")
        if v is not None and not (_is_int(v) and v >= 0):
            problems.append(f"{ctx}: source_block_index must be a non-negative int if present")
    # chain_id = sha256(normalized lua_steps)
    chain_id = entry.get("chain_id")
    if not _is_sha256(chain_id):
        problems.append(f"{ctx}: chain_id must be a 64-hex sha256")
    lua_steps = entry.get("lua_steps")
    if isinstance(lua_steps, list):
        try:
            expected = _normalized_lua_steps_hash(lua_steps)
            if _is_str(chain_id) and chain_id != expected:
                problems.append(
                    f"{ctx}: chain_id {chain_id!r} != sha256(normalized lua_steps) {expected!r}"
                )
        except ValueError as e:
            problems.append(f"{ctx}: lua_steps invalid: {e}")
    else:
        problems.append(f"{ctx}: lua_steps must be a list")
    if _is_sha256(chain_id):
        if chain_id in seen_chain_ids:
            # Duplicate chain_id is allowed ONLY when stable_id is also the
            # same (the same chain legitimately appears once). Two DIFFERENT
            # stable_ids sharing a chain_id means the importer collapsed two
            # distinct chains.
            problems.append(f"{ctx}: chain_id {chain_id!r} duplicated across entries")
        else:
            seen_chain_ids.add(chain_id)
    # strategy_number
    sn = entry.get("strategy_number")
    if not _is_int(sn) or sn < 1:
        problems.append(f"{ctx}: strategy_number must be a positive int")
    # askey
    if entry.get("askey") not in ASKEY_ALL:
        problems.append(f"{ctx}: askey must be one of the 9 profiles")
    # services / domains / hostlists / ipsets
    for field in ("services", "domains", "hostlists", "ipsets"):
        v = entry.get(field)
        if not isinstance(v, list) or not all(_is_str(x) for x in v):
            problems.append(f"{ctx}: {field} must be a list of non-empty strings")
    # required_assets
    assets = entry.get("required_assets")
    if not isinstance(assets, list):
        problems.append(f"{ctx}: required_assets must be a list")
    else:
        for j, asset in enumerate(assets):
            if not isinstance(asset, dict):
                problems.append(f"{ctx}: required_assets[{j}] must be an object")
                continue
            if not _is_str(asset.get("path")):
                problems.append(f"{ctx}: required_assets[{j}].path must be a non-empty string")
            if not _is_sha256(asset.get("sha256")):
                problems.append(f"{ctx}: required_assets[{j}].sha256 must be a 64-hex sha256")
            if not _is_str(asset.get("source")):
                problems.append(f"{ctx}: required_assets[{j}].source must be a non-empty string")
    # compatibility
    compat = entry.get("compatibility")
    if not isinstance(compat, dict):
        problems.append(f"{ctx}: compatibility must be an object")
    else:
        status = compat.get("status")
        if status not in ("compatible", "incompatible"):
            problems.append(f"{ctx}: compatibility.status must be compatible|incompatible")
        dropped = compat.get("dropped_options")
        if not isinstance(dropped, list) or not all(_is_str(x) for x in dropped):
            problems.append(f"{ctx}: compatibility.dropped_options must be a list of strings")
    # warnings
    warnings = entry.get("warnings")
    if not isinstance(warnings, list) or not all(_is_str(x) for x in warnings):
        problems.append(f"{ctx}: warnings must be a list of strings")
    return problems


def validate_event_line(line: str) -> list[str]:
    """Return contract violations for one NDJSON event line (empty = ok)."""
    problems: list[str] = []
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return ["event line is not valid JSON"]
    if not isinstance(ev, dict):
        return ["event is not a JSON object"]
    if ev.get("schema_version") != 1:
        problems.append("schema_version must be 1")
    if not _is_int(ev.get("ts")) or ev["ts"] < 0:
        problems.append("ts must be a non-negative int (unix seconds)")
    etype = ev.get("type")
    if etype not in EVENT_TYPES:
        problems.append(f"type must be one of {sorted(EVENT_TYPES)}")
    # Forbidden payloads (contracts §2 + existing schema rule).
    for forbidden_key in ("payload", "packet", "dissect", "dump", "bytes", "raw"):
        if forbidden_key in ev:
            problems.append(f"event must not carry {forbidden_key!r} (no packet payloads/dumps)")
    if etype in FULL_FIELD_TYPES:
        if ev.get("askey") not in ASKEY_ALL:
            problems.append(f"askey must be one of the 9 profiles (got {ev.get('askey')!r})")
        host = ev.get("host")
        if not _is_str(host):
            problems.append("host must be a non-empty string")
        elif host != host.lower() or host != host.strip(".") or host.startswith(".") or host.endswith("."):
            problems.append(f"host must be normalized (lowercase, no leading/trailing dot): {host!r}")
        elif any(ch.isspace() for ch in host):
            problems.append(f"host must not contain whitespace: {host!r}")
        if not _is_int(ev.get("strategy")) or ev["strategy"] < 1:
            problems.append("strategy must be a positive int")
        if not _is_str(ev.get("chain_id")):
            problems.append("chain_id must be a non-empty string")
        if not _is_str(ev.get("reason")):
            problems.append("reason must be a non-empty string")
        if not _is_int(ev.get("generation")) or ev["generation"] < 0:
            problems.append("generation must be a non-negative int")
        if not _is_str(ev.get("run_id")):
            problems.append("run_id must be a non-empty string")
    return problems


def validate_state_file(name: str, doc: Any) -> list[str]:
    """Return contract violations for a persistent state document."""
    problems: list[str] = []
    if not isinstance(doc, dict):
        return [f"{name}: state must be a JSON object"]
    if doc.get("schema_version") != 1:
        problems.append(f"{name}: schema_version must be 1")
    if name == "learned.json":
        problems.extend(_validate_learned(doc))
    elif name == "blocked.json":
        problems.extend(_validate_blocked(doc))
    elif name == "whitelist.json":
        if not isinstance(doc.get("hosts"), list) or not all(_is_str(h) for h in doc["hosts"]):
            problems.append("whitelist.json: hosts must be a list of non-empty strings")
    elif name == "manual-locks.json":
        problems.extend(_validate_manual_locks(doc))
    elif name == "learner-state.json":
        problems.extend(_validate_learner_state(doc))
    return problems


def _validate_protocols_shape(doc: Any, name: str) -> list[str]:
    problems: list[str] = []
    protocols = doc.get("protocols")
    if not isinstance(protocols, dict) or not protocols:
        problems.append(f"{name}: protocols must be a non-empty object")
    return problems


def _validate_learned(doc: Any) -> list[str]:
    problems: list[str] = []
    protocols = doc.get("protocols")
    if not isinstance(protocols, dict):
        return ["learned.json: protocols must be an object"]
    for askey, hosts in protocols.items():
        if askey not in ASKEY_ALL:
            problems.append(f"learned.json: askey {askey!r} not in the 9 profiles")
        if not isinstance(hosts, dict):
            problems.append(f"learned.json: protocols.{askey} must be an object")
            continue
        for host, rec in hosts.items():
            if not _is_str(host):
                problems.append(f"learned.json: {askey} host key must be a non-empty string")
            if not isinstance(rec, dict):
                problems.append(f"learned.json: {askey}.{host} must be an object")
                continue
            if "auto_lock" in rec:
                al = rec["auto_lock"]
                if not _is_int(al) or al < 1:
                    problems.append(f"learned.json: {askey}.{host}.auto_lock must be a positive int")
            strats = rec.get("strategies")
            if not isinstance(strats, dict):
                problems.append(f"learned.json: {askey}.{host}.strategies must be an object")
                continue
            for skey, cnt in strats.items():
                if not skey.isdigit() or int(skey) < 1:
                    problems.append(f"learned.json: {askey}.{host}.strategies key {skey!r} must be a positive-int string")
                if not isinstance(cnt, dict):
                    problems.append(f"learned.json: {askey}.{host}.strategies.{skey} must be an object")
                    continue
                if not _is_int(cnt.get("successes")) or cnt["successes"] < 0:
                    problems.append(f"learned.json: {askey}.{host}.{skey}.successes must be a non-negative int")
                if not _is_int(cnt.get("failures")) or cnt["failures"] < 0:
                    problems.append(f"learned.json: {askey}.{host}.{skey}.failures must be a non-negative int")
    return problems


def _validate_blocked(doc: Any) -> list[str]:
    problems: list[str] = []
    protocols = doc.get("protocols")
    if not isinstance(protocols, dict):
        return ["blocked.json: protocols must be an object"]
    for askey, bp in protocols.items():
        if askey not in ASKEY_ALL:
            problems.append(f"blocked.json: askey {askey!r} not in the 9 profiles")
        if not isinstance(bp, dict):
            problems.append(f"blocked.json: protocols.{askey} must be an object")
            continue
        # global + hosts (existing)
        for key in ("global", "user_global"):
            if key in bp:
                vals = bp[key]
                if not isinstance(vals, list) or not all(_is_int(s) and s > 0 for s in vals):
                    problems.append(f"blocked.json: {askey}.{key} must be a list of positive ints")
        for key in ("hosts", "user_hosts"):
            if key in bp:
                hh = bp[key]
                if not isinstance(hh, dict):
                    problems.append(f"blocked.json: {askey}.{key} must be an object")
                    continue
                for host, vals in hh.items():
                    if not _is_str(host):
                        problems.append(f"blocked.json: {askey}.{key} host key must be a non-empty string")
                    if not isinstance(vals, list) or not all(_is_int(s) and s > 0 for s in vals):
                        problems.append(f"blocked.json: {askey}.{key}.{host} must be a list of positive ints")
    return problems


def _validate_manual_locks(doc: Any) -> list[str]:
    problems: list[str] = []
    protocols = doc.get("protocols")
    if not isinstance(protocols, dict):
        return ["manual-locks.json: protocols must be an object"]
    for askey, hosts in protocols.items():
        if askey not in ASKEY_ALL:
            problems.append(f"manual-locks.json: askey {askey!r} not in the 9 profiles")
        if not isinstance(hosts, dict):
            problems.append(f"manual-locks.json: protocols.{askey} must be an object")
            continue
        for host, strat in hosts.items():
            if not _is_str(host):
                problems.append(f"manual-locks.json: {askey} host key must be a non-empty string")
            if not _is_int(strat) or strat < 1:
                problems.append(f"manual-locks.json: {askey}.{host} must be a positive int")
    return problems


def _validate_learner_state(doc: Any) -> list[str]:
    problems: list[str] = []
    cursor = doc.get("event_cursor")
    if not isinstance(cursor, dict):
        return ["learner-state.json: event_cursor must be an object"]
    if not _is_int(cursor.get("bytes")) or cursor["bytes"] < 0:
        problems.append("learner-state.json: event_cursor.bytes must be a non-negative int")
    if not _is_int(cursor.get("lines")) or cursor["lines"] < 0:
        problems.append("learner-state.json: event_cursor.lines must be a non-negative int")
    if not _is_sha256(cursor.get("last_line_sha256")):
        # last_line_sha256 may be empty/zero only when no lines processed yet;
        # accept a 64-hex sha256 OR the literal "" for the cold-start case.
        if cursor.get("last_line_sha256") not in ("", None):
            problems.append("learner-state.json: event_cursor.last_line_sha256 must be a 64-hex sha256 (or '' cold-start)")
    if "last_preload_gen" in doc:
        if not _is_int(doc["last_preload_gen"]) or doc["last_preload_gen"] < 0:
            problems.append("learner-state.json: last_preload_gen must be a non-negative int")
    if "last_run_id" in doc:
        if not _is_str(doc["last_run_id"]):
            problems.append("learner-state.json: last_run_id must be a non-empty string")
    if "updated_at" in doc:
        if not _is_int(doc["updated_at"]) or doc["updated_at"] < 0:
            problems.append("learner-state.json: updated_at must be a non-negative int")
    return problems


# ---------------------------------------------------------------------------
# Catalog schema (§1) — runs against A's catalog.json when present
# ---------------------------------------------------------------------------

class CatalogSchemaTest(unittest.TestCase):
    """Validates A's ``strategy-sources/catalog.json`` against contract §1.

    Skips pre-integration (catalog not yet committed); the schema validator
    itself is exercised by CatalogSchemaValidatorTest below against synthetic
    contract-shaped documents."""

    @unittest.skipUnless(CATALOG.is_file(), "strategy-sources/catalog.json not present (post-integration: A's importer)")
    def test_catalog_satisfies_contract(self) -> None:
        doc = json.loads(CATALOG.read_text(encoding="utf-8"))
        problems = validate_catalog(doc)
        self.assertEqual(problems, [], "catalog contract violations:\n  " + "\n  ".join(problems))

    @unittest.skipUnless(CATALOG.is_file(), "strategy-sources/catalog.json not present (post-integration)")
    def test_stable_id_independent_of_position(self) -> None:
        # Reorder entries; stable_ids must be unchanged and chain_ids unchanged.
        doc = json.loads(CATALOG.read_text(encoding="utf-8"))
        entries = doc["entries"]
        original = [(e["stable_id"], e["chain_id"]) for e in entries]
        reordered = list(reversed(entries))
        ids_after = [(e["stable_id"], e["chain_id"]) for e in reordered]
        # As a set, the (stable_id, chain_id) pairs must be identical.
        self.assertEqual(set(original), set(ids_after))

    @unittest.skipUnless(CATALOG.is_file(), "strategy-sources/catalog.json not present (post-integration)")
    def test_default_v5_chain_grouping(self) -> None:
        # Contract §1 rule 8: the three Default-v5 lua_steps (send, syndata
        # tls_google, syndata) share one strategy_number and one chain_id.
        doc = json.loads(CATALOG.read_text(encoding="utf-8"))
        v5 = [
            e for e in doc["entries"]
            if e.get("source_id") == "Default v5" or e.get("stable_id") == "discord-default-v5"
        ]
        self.assertTrue(v5, "catalog has no Default v5 entry")
        # The v5 entry's lua_steps are exactly send + syndata:tls_google + syndata.
        e = v5[0]
        funcs = [s["func"] for s in e["lua_steps"]]
        self.assertEqual(funcs, ["send", "syndata", "syndata"],
                         f"Default v5 lua_steps must be send+syndata+syndata, got {funcs}")
        # The syndata steps' args: first has blob=tls_google, second is bare.
        self.assertEqual(e["lua_steps"][0]["args"].get("repeats"), "3")
        self.assertEqual(e["lua_steps"][1]["args"].get("blob"), "tls_google")

    @unittest.skipUnless(CATALOG.is_file(), "strategy-sources/catalog.json not present (post-integration)")
    def test_discord_candidate_pool_contains_both_defaults(self) -> None:
        # Contract §1 rule 9: Default old AND Default v5 both target Discord
        # (services contains "discord" OR domains/ipsets target Discord).
        doc = json.loads(CATALOG.read_text(encoding="utf-8"))
        discord_entries = []
        for e in doc["entries"]:
            if "discord" in e.get("services", []):
                discord_entries.append(e["source_id"])
                continue
            targets = set(e.get("domains", [])) | set(e.get("ipsets", []))
            if any("discord" in t for t in targets):
                discord_entries.append(e["source_id"])
        self.assertIn("Default old", discord_entries, "Default old must be in the Discord candidate pool")
        self.assertIn("Default v5", discord_entries, "Default v5 must be in the Discord candidate pool")


# ---------------------------------------------------------------------------
# Catalog schema validator — always runs against synthetic documents
# ---------------------------------------------------------------------------

class CatalogSchemaValidatorTest(unittest.TestCase):
    """Exercises the frozen schema validator against synthetic documents so the
    contract surface is always tested, even before A's catalog exists."""

    def _v5_entry(self) -> dict:
        steps = [
            {"func": "send", "args": {"repeats": "3"}},
            {"func": "syndata", "args": {"blob": "tls_google"}},
            {"func": "syndata", "args": {}},
        ]
        return {
            "stable_id": "discord-default-v5",
            "source_id": "Default v5",
            "source_commit": PINNED_PRESET_COMMIT,
            "source_path": "presets/Default v5.txt",
            "source_block_index": 1,
            "source_sha256": "a" * 64,
            "chain_id": _normalized_lua_steps_hash(steps),
            "strategy_number": 2,
            "askey": "tls",
            "services": ["discord"],
            "domains": ["discord.com", "discordapp.com"],
            "hostlists": [],
            "ipsets": ["lists/ipset-discord.txt"],
            "lua_steps": steps,
            "required_assets": [
                {"path": "lua/init_vars.lua", "sha256": "b" * 64,
                 "source": "youtubediscord/zapret2-youtube-discord@4d75c70b"},
                {"path": "lists/ipset-discord.txt", "sha256": "c" * 64,
                 "source": "youtubediscord/zapret2-youtube-discord@4d75c70b"},
            ],
            "compatibility": {"status": "compatible", "dropped_options": [], "notes": ""},
            "warnings": [],
        }

    def _good_catalog(self) -> dict:
        return {
            "schema_version": 1,
            "catalog_version": 1,
            "source": {"repo": PINNED_PRESET_REPO, "commit": PINNED_PRESET_COMMIT,
                        "presets_path": "presets/"},
            "default_blocked_pass_domains": {
                "source_repo": PINNED_GUI_REPO, "source_commit": PINNED_GUI_COMMIT,
                "source_path": PINNED_GUI_PATH,
                "domains": ["discord.com", "youtube.com", "google.com"],
            },
            "entries": [self._v5_entry()],
        }

    def test_good_catalog_passes(self) -> None:
        self.assertEqual(validate_catalog(self._good_catalog()), [])

    def test_missing_schema_version_fails(self) -> None:
        doc = self._good_catalog()
        del doc["schema_version"]
        self.assertIn("catalog.schema_version must be 1", validate_catalog(doc))

    def test_bad_chain_id_fails(self) -> None:
        doc = self._good_catalog()
        doc["entries"][0]["chain_id"] = "0" * 64  # wrong hash
        problems = validate_catalog(doc)
        self.assertTrue(any("chain_id" in p and "sha256(normalized lua_steps)" in p for p in problems),
                        f"expected chain_id mismatch violation, got {problems}")

    def test_chain_id_is_sha256_of_normalized_steps(self) -> None:
        entry = self._v5_entry()
        # Arg-key ordering must not change the hash (normalized = sorted args).
        steps_rev = [
            {"func": "send", "args": {"repeats": "3"}},
            {"func": "syndata", "args": {"blob": "tls_google"}},
            {"func": "syndata", "args": {}},
        ]
        self.assertEqual(_normalized_lua_steps_hash(entry["lua_steps"]),
                         _normalized_lua_steps_hash(steps_rev))
        # And the entry's chain_id equals that hash.
        self.assertEqual(entry["chain_id"], _normalized_lua_steps_hash(entry["lua_steps"]))

    def test_bad_compatibility_status_fails(self) -> None:
        doc = self._good_catalog()
        doc["entries"][0]["compatibility"]["status"] = "maybe"
        self.assertIn("entries[0]: compatibility.status must be compatible|incompatible",
                      validate_catalog(doc))

    def test_bad_askey_fails(self) -> None:
        doc = self._good_catalog()
        doc["entries"][0]["askey"] = "tls2"
        self.assertIn("entries[0]: askey must be one of the 9 profiles",
                      validate_catalog(doc))

    def test_nonpositive_strategy_number_fails(self) -> None:
        doc = self._good_catalog()
        doc["entries"][0]["strategy_number"] = 0
        self.assertIn("entries[0]: strategy_number must be a positive int",
                      validate_catalog(doc))

    def test_missing_provenance_fails(self) -> None:
        doc = self._good_catalog()
        del doc["entries"][0]["source_sha256"]
        self.assertIn("entries[0]: source_sha256 must be a non-empty string",
                      validate_catalog(doc))

    def test_required_asset_missing_sha256_fails(self) -> None:
        doc = self._good_catalog()
        del doc["entries"][0]["required_assets"][0]["sha256"]
        self.assertIn("entries[0]: required_assets[0].sha256 must be a 64-hex sha256",
                      validate_catalog(doc))

    def test_discord_com_missing_from_default_blocked_pass_domains_fails(self) -> None:
        doc = self._good_catalog()
        doc["default_blocked_pass_domains"]["domains"] = ["youtube.com", "google.com"]
        self.assertIn("default_blocked_pass_domains.domains must contain discord.com",
                      validate_catalog(doc))


# ---------------------------------------------------------------------------
# Event schema (§2) — always runs against synthetic NDJSON; against B's
# events.ndjson when present
# ---------------------------------------------------------------------------

class EventSchemaValidatorTest(unittest.TestCase):
    """Exercises the frozen event-line validator against synthetic lines."""

    def _good_line(self, **overrides) -> str:
        ev = {
            "schema_version": 1, "ts": 1753290000, "type": "SUCCESS",
            "askey": "tls", "host": "discord.com", "strategy": 2,
            "chain_id": "discord-default-v5", "reason": "combined_success_detector",
            "generation": 3, "run_id": "nfqws2-20260723T203000Z",
        }
        ev.update(overrides)
        return json.dumps(ev)

    def test_good_success_line_passes(self) -> None:
        self.assertEqual(validate_event_line(self._good_line()), [])

    def test_good_fail_line_passes(self) -> None:
        self.assertEqual(validate_event_line(self._good_line(type="FAIL", reason="combined_failure_detector")), [])

    def test_good_lock_line_passes(self) -> None:
        self.assertEqual(validate_event_line(self._good_line(type="LOCK", reason="lock_successes_met")), [])

    def test_good_rotate_line_passes(self) -> None:
        self.assertEqual(validate_event_line(self._good_line(type="ROTATE", reason="rotate")), [])

    def test_lifecycle_start_line_passes_without_full_fields(self) -> None:
        # start/stop/error do not require askey/host/strategy.
        line = json.dumps({"schema_version": 1, "ts": 1753290000, "type": "start",
                           "run_id": "nfqws2-20260723T203000Z"})
        self.assertEqual(validate_event_line(line), [])

    def _problems(self, **overrides) -> str:
        return "\n".join(validate_event_line(self._good_line(**overrides)))

    def test_bad_schema_version_fails(self) -> None:
        self.assertIn("schema_version must be 1", self._problems(schema_version=2))

    def test_bad_type_fails(self) -> None:
        self.assertIn("type must be one of", self._problems(type="WIN"))

    def test_bad_askey_fails(self) -> None:
        self.assertIn("askey must be one of the 9 profiles", self._problems(askey="tls2"))

    def test_unnormalized_host_fails(self) -> None:
        for bad in ("Discord.com", ".discord.com", "discord.com."):
            self.assertIn("host must be normalized", self._problems(host=bad),
                          f"host {bad!r} should be rejected as unnormalized")

    def test_host_with_whitespace_fails(self) -> None:
        # A normalized hostname contains no whitespace.
        self.assertIn("host must not contain whitespace", self._problems(host="discord .com"))

    def test_nonpositive_strategy_fails(self) -> None:
        self.assertIn("strategy must be a positive int", self._problems(strategy=0))

    def test_missing_chain_id_fails(self) -> None:
        line = self._good_line()
        ev = json.loads(line)
        del ev["chain_id"]
        self.assertIn("chain_id must be a non-empty string",
                      "\n".join(validate_event_line(json.dumps(ev))))

    def test_packet_payload_rejected(self) -> None:
        for key in ("payload", "packet", "dissect", "dump", "bytes", "raw"):
            ev = json.loads(self._good_line())
            ev[key] = "x"
            self.assertIn(f"event must not carry {key!r}",
                          "\n".join(validate_event_line(json.dumps(ev))))

    def test_invalid_json_line_rejected(self) -> None:
        self.assertEqual(validate_event_line("{not json"), ["event line is not valid JSON"])


class EventStreamFileTest(unittest.TestCase):
    """Validates B's events.ndjson stream when present (post-integration)."""

    EVENTS = ROOT / "tests" / "fixtures" / "events.ndjson"

    @unittest.skipUnless(EVENTS.is_file(),
                         "events.ndjson fixture not present (post-integration: B's emitter)")
    def test_every_line_satisfies_contract(self) -> None:
        lines = [ln for ln in EVENTS.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertGreater(len(lines), 0, "events fixture is empty")
        for i, line in enumerate(lines):
            problems = validate_event_line(line)
            self.assertEqual(problems, [],
                             f"events.ndjson line {i} violations: {problems}\n  line={line!r}")


# ---------------------------------------------------------------------------
# State schema (§3) — always runs against synthetic documents; against B's
# state files when present
# ---------------------------------------------------------------------------

class StateSchemaValidatorTest(unittest.TestCase):
    """Exercises the frozen state-file validators against synthetic documents."""

    def test_good_learned_passes(self) -> None:
        doc = {"schema_version": 1, "protocols": {"tls": {
            "discord.com": {"auto_lock": 2,
                            "strategies": {"2": {"successes": 3, "failures": 1}}}}}}
        self.assertEqual(validate_state_file("learned.json", doc), [])

    def test_learned_bad_auto_lock_fails(self) -> None:
        doc = {"schema_version": 1, "protocols": {"tls": {"discord.com": {"auto_lock": 0}}}}
        self.assertIn("learned.json: tls.discord.com.auto_lock must be a positive int",
                      validate_state_file("learned.json", doc))

    def test_learned_bad_strategy_count_fails(self) -> None:
        doc = {"schema_version": 1, "protocols": {"tls": {
            "discord.com": {"strategies": {"2": {"successes": -1, "failures": 0}}}}}}
        self.assertIn("learned.json: tls.discord.com.2.successes must be a non-negative int",
                      validate_state_file("learned.json", doc))

    def test_good_blocked_with_user_keys_passes(self) -> None:
        # §3 NEW: user_global / user_hosts alongside global / hosts.
        doc = {"schema_version": 1, "protocols": {"tls": {
            "global": [1], "hosts": {"discord.com": [1]},
            "user_global": [3], "user_hosts": {"example.com": [2]}}}}
        self.assertEqual(validate_state_file("blocked.json", doc), [])

    def test_blocked_bad_global_fails(self) -> None:
        doc = {"schema_version": 1, "protocols": {"tls": {"global": [0]}}}
        self.assertIn("blocked.json: tls.global must be a list of positive ints",
                      validate_state_file("blocked.json", doc))

    def test_good_whitelist_passes(self) -> None:
        self.assertEqual(validate_state_file("whitelist.json",
                                             {"schema_version": 1, "hosts": ["safe.example"]}), [])

    def test_whitelist_bad_host_fails(self) -> None:
        self.assertIn("whitelist.json: hosts must be a list of non-empty strings",
                      validate_state_file("whitelist.json", {"schema_version": 1, "hosts": ["", 1]}))

    def test_good_manual_locks_passes(self) -> None:
        doc = {"schema_version": 1, "protocols": {"tls": {"manual.example": 2}}}
        self.assertEqual(validate_state_file("manual-locks.json", doc), [])

    def test_good_learner_state_passes(self) -> None:
        doc = {"schema_version": 1,
               "event_cursor": {"bytes": 4096, "lines": 87, "last_line_sha256": "d" * 64},
               "last_preload_gen": 3, "last_run_id": "nfqws2-20260723T203000Z",
               "updated_at": 1753290120}
        self.assertEqual(validate_state_file("learner-state.json", doc), [])

    def test_learner_state_cold_start_empty_sha_allowed(self) -> None:
        doc = {"schema_version": 1,
               "event_cursor": {"bytes": 0, "lines": 0, "last_line_sha256": ""},
               "last_preload_gen": 0}
        self.assertEqual(validate_state_file("learner-state.json", doc), [])

    def test_learner_state_bad_cursor_fails(self) -> None:
        doc = {"schema_version": 1, "event_cursor": {"bytes": -1, "lines": 0, "last_line_sha256": ""}}
        self.assertIn("learner-state.json: event_cursor.bytes must be a non-negative int",
                      validate_state_file("learner-state.json", doc))


class StateFilePresentTest(unittest.TestCase):
    """Validates B's on-disk state files when present (post-integration)."""

    STATE = ROOT / "openwrt" / "zapret2-orchestra" / "files" / "etc" / "zapret2-orchestra"

    def _check(self, name: str) -> None:
        path = self.STATE / name
        if not path.is_file():
            self.skipTest(f"{name} not present (post-integration: B's learner state writer)")
        doc = json.loads(path.read_text(encoding="utf-8"))
        problems = validate_state_file(name, doc)
        self.assertEqual(problems, [], f"{name} violations:\n  " + "\n  ".join(problems))

    def test_learned_json_satisfies_contract(self) -> None:
        self._check("learned.json")

    def test_blocked_json_satisfies_contract(self) -> None:
        self._check("blocked.json")

    def test_whitelist_json_satisfies_contract(self) -> None:
        self._check("whitelist.json")

    def test_manual_locks_json_satisfies_contract(self) -> None:
        self._check("manual-locks.json")

    @unittest.skipUnless((STATE / "learner-state.json").is_file(),
                         "learner-state.json not present (post-integration: B's learner)")
    def test_learner_state_json_satisfies_contract(self) -> None:
        self._check("learner-state.json")


if __name__ == "__main__":
    unittest.main()
