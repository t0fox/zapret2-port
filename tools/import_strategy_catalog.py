#!/usr/bin/env python3
"""Deterministic strategy-catalog importer for zapret2-orchestra r7.

Reads the pinned nfqws2 presets (``presets/*.txt``) from
``youtubediscord/zapret2-youtube-discord`` @ ``4d75c70b`` and emits:

  * ``strategy-sources/manifest.json``                -- source manifest
  * ``strategy-sources/catalog.json``                -- the chain catalog
  * ``strategy-sources/default-blocked-pass-domains.json``
  * ``profiles/discord-adaptive.opt`` + ``.json``    -- adaptive profile + sidecar

The importer is deterministic: sorted keys, no timestamps, no randomness.
Re-running on the same pinned inputs yields byte-identical output.  It uses
the Python standard library only (no network, no third-party deps).

Contract: ``docs/ORCHESTRA_R7_CONTRACTS.md`` §1 (catalog entry) + §4 (adaptive
profile + ``chain_id_for_strategy``).  Spec: ``docs/ORCHESTRA_PARITY_SPEC.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Pinned source provenance (contracts §1 / spec §0)
# ---------------------------------------------------------------------------

SOURCE_REPO = "youtubediscord/zapret2-youtube-discord"
SOURCE_COMMIT = "4d75c70b430562e970bcf64cbe24072ce104b36a"
PRESETS_SUBPATH = "presets/"

# Original GUI (DEFAULT_BLOCKED_PASS_DOMAINS source).
GUI_REPO = "youtubediscord/zapret"
GUI_COMMIT = "9d57e55d6751587d9d52b52147a05a0a8fcc9fd8"
# Provenance path is relative to the PINNED GUI repo root (src/orchestra/...),
# not the port's submodule mount path (zapret2gui/src/...). The contract §1 +
# the parity tests assert the GUI-repo-relative path so provenance identifies
# the location inside youtubediscord/zapret @ 9d57e55 regardless of how the
# port mounts it as a submodule.
GUI_SOURCE_PATH = "src/orchestra/blocked_strategies_manager.py:65-102"

# zapret2-core (lua desync functions: send/syndata/multisplit/...).  Used to
# confirm core function names for the compat/closure report.  Not copied.
CORE_LUA_REPO = "zapret2-core"
CORE_LUA_COMMIT = "8a0f53f3cf2c92ddeaa66995ee63a35c1210c410"

# ---------------------------------------------------------------------------
# Windows-specific transport options that are dropped on OpenWrt (contract §1
# rule 5).  ONLY these are removed; the meaning of --filter/--payload/
# --out-range/--lua-desync/--blob/--hostlist/--ipset/--new is never altered.
# ---------------------------------------------------------------------------

DROPPED_WIN_OPTIONS = ("--wf-tcp-out", "--wf-udp-out", "--wf-raw-part")
# WinDivert-specific filter references (the --wf-raw-part=@windivert.filter/
# payloads) are captured under the --wf-raw-part drop with their target path.

# ---------------------------------------------------------------------------
# Core lua desync functions available on the OpenWrt port runtime.
# Derived from zapret2-core/lua/zapret-antidpi.lua @ 8a0f53f3 (function defs)
# plus ``pass`` from zapret-lib.lua.  A chain whose every func is in this set is
# ``compatible``; any func outside it is unknown -> ``incompatible`` (contract
# §1 rule 5: never replace an unknown function with a simplified analog).
# ---------------------------------------------------------------------------

KNOWN_CORE_FUNCS = frozenset({
    "drop", "send", "send_timer_delayed", "pktmod",
    "http_domcase", "http_hostcase", "http_methodeol", "http_unixeol",
    "synack_split", "synack", "wsize", "wssize",
    "tls_client_hello_clone", "syndata", "rst", "fake",
    "multisplit", "multidisorder", "multidisorder_send", "multidisorder_legacy",
    "hostfakesplit", "fakedsplit", "fakeddisorder", "tcpseg", "oob", "udplen",
    "dht_dn", "pass",
})

# nfqws2 C built-in blob names (always available, no --blob= declaration needed).
BUILTIN_BLOBS = frozenset({
    "fake_default_tls", "fake_default_quic", "fake_default_http",
    "fake_default_udp",
})

# Lua variables defined by the shipped ``init_vars.lua`` (verbatim from the
# pinned repo @ 4d75c70b, lua/init_vars.lua) usable as ``:blob=`` targets.
# Sourced from the file body: ``tls_google = tls_mod(...)`` etc.
INIT_VARS_BLOBS = frozenset({
    "tls_google", "bin_max", "fake_max",
    "tls_rnd", "tls_rndsni", "tls_rnd_google", "tls_rnd_dupsid",
    "tls_rnd_dupsid_google", "tls_padencap", "tls_padencap_google",
    "tls_vk", "tls_sber", "tls_yandex", "tls_mail",
    "tls_cloudflare", "tls_discord", "tls_youtube", "fake_inverted_tls",
})

# ---------------------------------------------------------------------------
# Service / askey detection tables (spec §1.2)
# ---------------------------------------------------------------------------

# The 9 ASKEY protocol profiles (locked_strategies_manager.py:47).
ASKEY_ALL = ("tls", "http", "quic", "discord", "wireguard",
             "mtproto", "dns", "stun", "unknown")

# hostlist/ipset filename keyword -> service label.
SERVICE_FILE_KEYWORDS = {
    "discord": "discord", "youtube": "youtube", "googlevideo": "youtube",
    "youtubeq": "youtube", "youtubegv": "youtube",
    "google": "google", "claude": "claude",
    "twitch": "twitch", "twitter": "twitter", "x": "twitter",
    "instagram": "instagram", "facebook": "facebook", "meta": "facebook",
    "whatsapp": "whatsapp", "tiktok": "tiktok", "spotify": "spotify",
    "netflix": "netflix", "steam": "steam", "roblox": "roblox",
    "reddit": "reddit", "github": "github", "rutracker": "rutracker",
    "telegram": "telegram", "soundcloud": "soundcloud",
    "russia-youtube": "youtube", "russia-discord": "discord",
    "lol": "leagueoflegends", "ovh": "ovh", "amazon": "amazon",
    "tankix": "tankix", "anydesk": "anydesk", "warp": "cloudflare",
    "cloudflare": "cloudflare", "censorliber": "censorliber",
    "melbicom": "melbicom", "timeweb": "timeweb",
    "zapretkvn": "zapretkvn", "porn": "porn",
}

# domain -> service (for --hostlist-domains=...).  Matched by substring.
SERVICE_DOMAIN_KEYWORDS = {
    "discord": "discord", "discordapp": "discord", "discord.gg": "discord",
    "youtube": "youtube", "googlevideo": "youtube", "ytimg": "youtube",
    "youtu.be": "youtube", "ggpht": "youtube", "googleusercontent": "google",
    "google.": "google", "googleapis": "google", "gstatic": "google",
    "twitch": "twitch", "twitchcdn": "twitch",
    "twitter": "twitter", "x.com": "twitter", "twimg": "twitter",
    "instagram": "instagram", "cdninstagram": "instagram", "igcdn": "instagram",
    "facebook": "facebook", "fbcdn": "facebook",
    "whatsapp": "whatsapp", "tiktok": "tiktok", "spotify": "spotify",
    "netflix": "netflix", "nflxvideo": "netflix",
    "steam": "steam", "steamcommunity": "steam", "steamstatic": "steam",
    "roblox": "roblox", "rbxcdn": "roblox",
    "reddit": "reddit", "redd.it": "reddit",
    "github": "github", "rutracker": "rutracker",
    "telegram": "telegram", "soundcloud": "soundcloud",
    "speedtest": "speedtest", "ooklaserver": "speedtest",
    "obsidian": "obsidian", "ntc.party": "ntcparty",
    "txrevive": "txrevive", "vk.com": "vk", "max.ru": "max",
    "deepseek": "deepseek", "4pda": "4pda", "gosuslugi": "gosuslugi",
    "sberbank": "sber", "yandex": "yandex", "mail.ru": "mailru",
}

# ---------------------------------------------------------------------------
# Canonical chain content signatures (used to identify Default old / Default v5
# deterministically by CONTENT, never by preset name/position -- contract §1
# rule 1).  strategy_number is assigned by the importer ONLY (contract §1
# rule 3): strategy=1 = Default old, strategy=2 = Default v5 (contract §4).
# ---------------------------------------------------------------------------

DEFAULT_V5_FUNC_SIG = ("send", "syndata", "syndata")
DEFAULT_OLD_FUNC_SIG = ("send", "syndata", "tls_multisplit_sni")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_bytes(path: Path) -> bytes:
    """The canonical (Git-blob) bytes of a text asset: CRLF -> LF.

    The pinned repo's .gitattributes sets ``* text=auto eol=crlf`` plus
    ``*.lua``/``*.txt`` ``eol=crlf``, so the Windows working-tree copies are
    CRLF but the canonical Git blob is LF.  Provenance sha256 and shipped bytes
    must reference the LF canonical form so they match the committed blob (and
    survive a fresh checkout)."""
    return path.read_bytes().replace(b"\r\n", b"\n")


def sha256_canonical(path: Path) -> str:
    return sha256_bytes(canonical_bytes(path))


def _slug(text: str) -> str:
    """Lowercase slug keeping alnum, underscore, hyphen; others -> hyphen."""
    text = text.strip().lower()
    out = []
    for ch in text:
        if ch.isalnum() or ch in ("_", "-"):
            out.append(ch)
        else:
            out.append("-")
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")


def _parse_kv(func_body: str) -> tuple[str, dict[str, str]]:
    """Parse ``func:k=v:k=v`` into (func, {k: v}).  Positional flag segments
    (no ``=``) are stored under key ``_flag_<n>`` so they survive round-trip."""
    parts = func_body.split(":")
    func = parts[0].strip()
    args: dict[str, str] = {}
    flag_i = 0
    for seg in parts[1:]:
        seg = seg.strip()
        if not seg:
            continue
        if "=" in seg:
            k, v = seg.split("=", 1)
            args[k.strip()] = v.strip()
        else:
            args[f"_flag_{flag_i}"] = seg
            flag_i += 1
    return func, args


def _normalize_lua_steps(steps: list[dict[str, Any]]) -> str:
    """Canonical JSON for chain_id: list of {func, args(sorted)}, in order."""
    norm = []
    for s in steps:
        args = dict(sorted((k, v) for k, v in s["args"].items()))
        norm.append({"func": s["func"], "args": args})
    return json.dumps(norm, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _chain_id(steps: list[dict[str, Any]]) -> str:
    return sha256_bytes(_normalize_lua_steps(steps).encode("utf-8"))


def _stable_id(services: list[str], steps: list[dict[str, Any]], chain_id: str) -> str:
    prefix = services[0] if services else "chain"
    funcs = "-".join(s["func"] for s in steps)
    return f"{_slug(prefix)}-{_slug(funcs)}-{chain_id[:8]}"


# ---------------------------------------------------------------------------
# Preset + block parsing
# ---------------------------------------------------------------------------

GLOBAL_OPTION_RE = re.compile(
    r"^\s*(--lua-init|--ctrack-disable|--ipcache-lifetime|--ipcache-hostname|"
    r"--wf-tcp-out|--wf-udp-out|--wf-raw-part|--blob)=?"
)
COMMENT_RE = re.compile(r"^\s*#")
NEW_SEP = "--new"


class Block:
    """A --new-separated filter block within a preset."""

    def __init__(self, index: int, lines: list[str]):
        self.index = index  # 1-based among filter blocks in the preset
        self.lines = lines  # raw text lines (no trailing newline), in order
        self.lua_steps: list[dict[str, Any]] = []
        self.filter_lines: list[str] = []
        self.hostlists: list[str] = []
        self.hostlist_domains: list[str] = []
        self.hostlist_exclude: list[str] = []
        self.ipsets: list[str] = []
        self.ipset_ips: list[str] = []
        self.payload: str | None = None
        self.out_range: str | None = None
        self.filter_l7: list[str] = []
        self.filter_tcp: list[str] = []
        self.filter_udp: list[str] = []
        self.other_opts: list[str] = []
        self._parse()

    @property
    def raw_text(self) -> str:
        return "\n".join(self.lines) + "\n"

    @property
    def raw_bytes(self) -> bytes:
        return self.raw_text.encode("utf-8")

    def _parse(self) -> None:
        for line in self.lines:
            s = line.strip()
            if not s or COMMENT_RE.match(line):
                continue
            if s.startswith("--lua-desync="):
                func_body = s[len("--lua-desync="):]
                func, args = _parse_kv(func_body)
                # ``strategy=N`` is assigned by the importer in the adaptive
                # profile; native presets carry none.  Strip any stray one from
                # the canonical step so it never perturbs chain_id.
                args.pop("strategy", None)
                self.lua_steps.append({"func": func, "args": args})
            elif s.startswith("--filter-l7="):
                self.filter_l7.extend(
                    v.strip() for v in s[len("--filter-l7="):].split(",") if v.strip()
                )
                self.filter_lines.append(s)
            elif s.startswith("--filter-tcp="):
                self.filter_tcp.append(s[len("--filter-tcp="):])
                self.filter_lines.append(s)
            elif s.startswith("--filter-udp="):
                self.filter_udp.append(s[len("--filter-udp="):])
                self.filter_lines.append(s)
            elif s.startswith("--hostlist-domains="):
                self.hostlist_domains.extend(
                    v.strip() for v in s[len("--hostlist-domains="):].split(",") if v.strip()
                )
                self.filter_lines.append(s)
            elif s.startswith("--hostlist-exclude="):
                self.hostlist_exclude.extend(
                    v.strip() for v in s[len("--hostlist-exclude="):].split(",") if v.strip()
                )
                self.filter_lines.append(s)
            elif s.startswith("--hostlist="):
                self.hostlists.append(s[len("--hostlist="):].strip())
                self.filter_lines.append(s)
            elif s.startswith("--ipset-ip="):
                self.ipset_ips.append(s[len("--ipset-ip="):].strip())
                self.filter_lines.append(s)
            elif s.startswith("--ipset="):
                self.ipsets.append(s[len("--ipset="):].strip())
                self.filter_lines.append(s)
            elif s.startswith("--payload="):
                self.payload = s[len("--payload="):].strip()
                self.filter_lines.append(s)
            elif s.startswith("--out-range="):
                self.out_range = s[len("--out-range="):].strip()
                self.filter_lines.append(s)
            else:
                self.other_opts.append(s)


class Preset:
    def __init__(self, rel_path: str, text: str, sha: str, size: int):
        self.rel_path = rel_path  # e.g. "presets/Default v5.txt"
        self.name = Path(rel_path).stem  # source_id (filename stem)
        self.text = text
        self.sha256 = sha
        self.size = size
        self.header_lines: list[str] = []
        self.lua_init: list[str] = []
        self.blob_decls: dict[str, str] = {}  # name -> raw value (path or 0x..)
        self.dropped_options: list[str] = []
        self.blocks: list[Block] = []
        self._parse()

    def _parse(self) -> None:
        # Split on lines that are exactly ``--new``.
        segments: list[list[str]] = [[]]
        for line in self.text.splitlines():
            if line.strip() == NEW_SEP:
                segments.append([])
            else:
                segments[-1].append(line)
        # Segment 0 = header (global opts) + possibly the first filter block.
        first = segments[0]
        # A filter block begins at the first line that is a block content line
        # (filter/hostlist/ipset/payload/out-range/lua-desync) AND is not a
        # global option.  Global options live in the header.
        split_at = len(first)
        for i, line in enumerate(first):
            s = line.strip()
            if not s or COMMENT_RE.match(line):
                continue
            if GLOBAL_OPTION_RE.match(line):
                continue
            # First non-global, non-comment content line -> block 1 starts here.
            split_at = i
            break
        header_lines = first[:split_at]
        block1_lines = first[split_at:]
        self._parse_header(header_lines)
        block_segs: list[list[str]] = []
        if block1_lines:
            block_segs.append(block1_lines)
        block_segs.extend(segments[1:])
        idx = 0
        for seg in block_segs:
            # Skip segments with no content (trailing --new) and segments that
            # are purely global options (shouldn't happen post-header, but be
            # safe): a block must have a filter/desync/hostlist/ipset line.
            has_content = any(
                (l.strip().startswith("--filter-") or
                 l.strip().startswith("--lua-desync") or
                 l.strip().startswith("--hostlist") or
                 l.strip().startswith("--ipset") or
                 l.strip().startswith("--payload"))
                for l in seg if l.strip() and not COMMENT_RE.match(l)
            )
            if not has_content:
                continue
            idx += 1
            self.blocks.append(Block(idx, seg))

    def _parse_header(self, header_lines: list[str]) -> None:
        for line in header_lines:
            self.header_lines.append(line)
            s = line.strip()
            if not s or COMMENT_RE.match(line):
                continue
            if s.startswith("--lua-init="):
                self.lua_init.append(s[len("--lua-init="):].strip())
            elif s.startswith("--blob="):
                # nfqws2 blob syntax: --blob=<name>:<value> where <value> is a
                # @bin/<file> path or a 0x.. hex literal.  Split on the FIRST
                # colon (name : value), not '='.
                body = s[len("--blob="):]
                if ":" in body:
                    name, val = body.split(":", 1)
                    self.blob_decls[name.strip()] = val.strip()
            elif any(s.startswith(opt + "=") or s.startswith(opt) for opt in DROPPED_WIN_OPTIONS):
                # Record the verbatim dropped Windows option.
                self.dropped_options.append(s)


# ---------------------------------------------------------------------------
# Service / askey / domain detection
# ---------------------------------------------------------------------------

def detect_services(block: Block) -> list[str]:
    services: list[str] = []
    seen: set[str] = set()

    def add(label: str | None) -> None:
        if label and label not in seen:
            seen.add(label)
            services.append(label)

    # hostlist / ipset filenames.
    for path in block.hostlists + block.ipsets:
        base = path.split("/")[-1].lower()
        stem = base.rsplit(".", 1)[0] if "." in base else base
        for kw, label in SERVICE_FILE_KEYWORDS.items():
            if kw in stem:
                add(label)
                break
    # hostlist-domains entries.
    for dom in block.hostlist_domains:
        d = dom.lower()
        for kw, label in SERVICE_DOMAIN_KEYWORDS.items():
            if kw in d:
                add(label)
                break
    return services


def detect_domains(block: Block) -> list[str]:
    return sorted(set(d.lower() for d in block.hostlist_domains))


def detect_askey(block: Block) -> str:
    # --filter-l7= wins (discord/stun/tls/http).
    l7 = set(block.filter_l7)
    if "discord" in l7:
        return "discord"
    if "stun" in l7:
        return "stun"
    if "http" in l7:
        return "http"
    if "tls" in l7:
        return "tls"
    # mtproto: telegram port 5222.
    for spec in block.filter_tcp:
        if "5222" in spec:
            return "mtproto"
    # dns: udp 53.
    for spec in block.filter_udp:
        if spec.strip() == "53" or "53" in spec.split(","):
            return "dns"
    # wireguard: udp 51820.
    for spec in block.filter_udp:
        if "51820" in spec:
            return "wireguard"
    # quic: udp 443.
    if block.filter_udp:
        return "quic"
    # http: tcp 80 only (no 443).
    if block.filter_tcp:
        tcp_specs = ",".join(block.filter_tcp)
        if "443" in tcp_specs:
            return "tls"
        if "80" in tcp_specs:
            return "http"
        return "tls"
    if block.payload:
        return "unknown"
    return "unknown"


def detect_protocol_transport(block: Block) -> str:
    if block.filter_udp and not block.filter_tcp:
        return "udp"
    return "tcp"


def detect_ports(block: Block) -> list[str]:
    ports: list[str] = []
    for spec in block.filter_tcp:
        ports.append(f"tcp:{spec}")
    for spec in block.filter_udp:
        ports.append(f"udp:{spec}")
    return ports


# ---------------------------------------------------------------------------
# Dependency closure
# ---------------------------------------------------------------------------

def _blobs_referenced(steps: list[dict[str, Any]]) -> list[str]:
    blobs: list[str] = []
    for s in steps:
        v = s["args"].get("blob")
        if v:
            blobs.append(v)
    return blobs


def _seqovl_steps(steps: list[dict[str, Any]]) -> list[int]:
    vals: list[int] = []
    for s in steps:
        v = s["args"].get("seqovl")
        if v is not None:
            try:
                vals.append(int(v))
            except ValueError:
                pass
    return vals


# ---------------------------------------------------------------------------
# Circular-pool compatibility classification (Step 2).
#
# A separate axis from ``compatibility.status`` (the static-compatible axis,
# which stays as-is).  ``circular_compatibility`` says whether a chain can be a
# candidate for the circular pool, given that inside ``plan_instance_execute``
# a voluntary cutoff (set by ``send`` -- zapret-antidpi.lua:91) makes later
# steps unreachable.
# ---------------------------------------------------------------------------

# discord.com's SNI sits at ~byte 122; seqovl cancels when seqovl >= pos-1, so
# any seqovl >= 122 may cancel on a short-SNI domain (proven: Default old
# seqovl=652 failed with "seqovl cancelled, too large").
SEQOVL_SHORT_SNI_THRESHOLD = 122

CIRCULAR_COMPAT_INCOMPATIBLE = "incompatible"
CIRCULAR_COMPAT_STATIC_ONLY = "static_only"
CIRCULAR_COMPAT_CIRCULAR_COMPATIBLE = "circular_compatible"
CIRCULAR_COMPAT_ALL = (
    CIRCULAR_COMPAT_CIRCULAR_COMPATIBLE,
    CIRCULAR_COMPAT_STATIC_ONLY,
    CIRCULAR_COMPAT_INCOMPATIBLE,
)


def _send_not_last(steps: list[dict[str, Any]]) -> bool:
    """True if any ``send`` step is followed by a later step.  ``send`` calls
    ``direction_cutoff_opposite`` (zapret-antidpi.lua:91) which sets a
    voluntary cutoff; any step after such a ``send`` is unreachable inside
    ``plan_instance_execute`` (the circular execution loop)."""
    last = len(steps) - 1
    for i, s in enumerate(steps):
        if s["func"] == "send" and i < last:
            return True
    return False


def _classify_circular_compatibility(
    steps: list[dict[str, Any]],
    shipped_custom_funcs: set[str] | None = None,
) -> str:
    """Classify a chain for the circular pool (Step 2).

    * ``incompatible``        -- uses a function not in core + shipped
                                 ``custom_funcs.lua`` (cannot run on the port
                                 at all, so it is not a circular candidate).
    * ``static_only``         -- contains ``send`` NOT as the last step;
                                 ``send`` sets a voluntary cutoff so later
                                 steps are unreachable inside circular.  Works
                                 as STATIC (direct, no circular) but NOT as a
                                 circular pool candidate.
    * ``circular_compatible`` -- no ``send``, or ``send`` IS the last/only
                                 step; all steps reachable inside circular.

    Precedence: incompatible > static_only > circular_compatible.
    """
    shipped = (shipped_custom_funcs if shipped_custom_funcs is not None
               else _PINNED_CUSTOM_FUNCS)
    funcs = [s["func"] for s in steps]
    unavailable = [f for f in funcs
                   if f not in KNOWN_CORE_FUNCS and f not in shipped]
    if unavailable:
        return CIRCULAR_COMPAT_INCOMPATIBLE
    if _send_not_last(steps):
        return CIRCULAR_COMPAT_STATIC_ONLY
    return CIRCULAR_COMPAT_CIRCULAR_COMPATIBLE


def _circular_compatibility_warnings(
    steps: list[dict[str, Any]],
    circ_compat: str,
) -> list[str]:
    """Step 2 warning tokens for the circular axis (added alongside the
    existing seqovl warning the importer already emits)."""
    out: list[str] = []
    if circ_compat == CIRCULAR_COMPAT_STATIC_ONLY:
        out.append(
            "VOLUNTARY_CUTOFF_MAKES_LATER_STEPS_UNREACHABLE: chain contains "
            "`send` not as the last step; `send` calls direction_cutoff_opposite "
            "(zapret-antidpi.lua:91) so later steps are cutoff (unreachable) "
            "inside circular plan_instance_execute; works as STATIC only"
        )
    for sv in _seqovl_steps(steps):
        if sv >= SEQOVL_SHORT_SNI_THRESHOLD:
            out.append(
                f"SEQOVL_MAY_CANCEL_ON_SHORT_SNI: seqovl={sv} >= "
                f"{SEQOVL_SHORT_SNI_THRESHOLD} (discord.com SNI ~122 bytes); "
                f"seqovl >= pos-1 cancels (proven: Default old seqovl=652 "
                f"failed with 'seqovl cancelled, too large')"
            )
    return out


def build_dependencies(
    steps: list[dict[str, Any]],
    block: Block,
    preset: Preset,
    pinned_lua_funcs: set[str],
) -> dict[str, Any]:
    """Closure report: every referenced blob/list/ipset/lua function accounted
    for; missing flagged.  ``available`` means resolvable on the OpenWrt port
    runtime (core func / builtin blob / init_vars var / shipped list)."""
    funcs = [s["func"] for s in steps]
    unknown_funcs = [f for f in funcs if f not in KNOWN_CORE_FUNCS]
    pinned_only_funcs = [f for f in unknown_funcs if f in pinned_lua_funcs]

    blobs = sorted(set(_blobs_referenced(steps)))
    blob_status: dict[str, str] = {}
    missing_blobs: list[str] = []
    for b in blobs:
        if b in BUILTIN_BLOBS or b in INIT_VARS_BLOBS:
            blob_status[b] = "builtin-or-init-vars"
        elif b in preset.blob_decls:
            # Declared via --blob=@bin/... in the preset; the port ships no bin
            # files, so this is available only if the bin is shipped.
            blob_status[b] = "preset-blob-declared"
            missing_blobs.append(b)
        else:
            blob_status[b] = "missing"
            missing_blobs.append(b)

    lists = sorted(set(block.hostlists + block.hostlist_exclude))
    ipsets = sorted(set(block.ipsets))
    missing_assets: list[str] = []
    # ipset-discord.txt is shipped by r7; other lists are not.
    shipped_lists = {"lists/ipset-discord.txt"}
    for lst in lists + ipsets:
        if lst not in shipped_lists:
            missing_assets.append(lst)

    return {
        "funcs": funcs,
        "unknown_funcs": sorted(set(unknown_funcs)),
        "pinned_only_funcs": sorted(set(pinned_only_funcs)),
        "blobs": blobs,
        "blob_status": blob_status,
        "missing_blobs": sorted(set(missing_blobs)),
        "hostlists": lists,
        "ipsets": ipsets,
        "missing_assets": sorted(set(missing_assets)),
        "lua_init": sorted(set(preset.lua_init)),
    }


def required_assets_for(
    steps: list[dict[str, Any]],
    block: Block,
    repo_root: Path,
    preset_blob_decls: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Assets the chain needs to run on the OpenWrt port, each marked shipped or
    not.  Shipped assets carry a sha256; not-shipped deps carry ``shipped:false``
    so the catalog explicitly declares what an incompatible chain needs."""
    assets: list[dict[str, Any]] = []
    blobs = set(_blobs_referenced(steps))
    ipsets = set(block.ipsets)
    funcs = [s["func"] for s in steps]
    src = f"{SOURCE_REPO}@{SOURCE_COMMIT[:7]}"
    preset_blob_decls = preset_blob_decls or {}

    # init_vars.lua is required if any referenced blob is an init_vars var.
    if blobs & INIT_VARS_BLOBS:
        p = repo_root / "lua" / "init_vars.lua"
        if p.exists():
            assets.append({
                "path": "lua/init_vars.lua",
                "sha256": sha256_canonical(p),
                "source": src,
                "shipped": True,
                "role": "provides tls_google and other tls_mod blobs",
            })
    # ipset-discord.txt is shipped.
    for ip in sorted(ipsets):
        if ip == "lists/ipset-discord.txt":
            p = repo_root / "lists" / "ipset-discord.txt"
            if p.exists():
                assets.append({
                    "path": "lists/ipset-discord.txt",
                    "sha256": sha256_canonical(p),
                    "source": src,
                    "shipped": True,
                    "role": "discord ipset target",
                })
    # Not-shipped deps: lua files defining custom funcs.
    needs_custom_funcs = any(f in funcs for f in _PINNED_CUSTOM_FUNCS)
    needs_multishake = any(f in funcs for f in _PINNED_MULTISHAKE_FUNCS)
    for rel, role, needed in (
        ("lua/custom_funcs.lua", "defines custom desync functions", needs_custom_funcs),
        ("lua/zapret-multishake.lua", "defines hostfakesplit_multi etc.", needs_multishake),
    ):
        if needed:
            p = repo_root / rel
            assets.append({
                "path": rel,
                "sha256": sha256_file(p) if p.exists() else "",
                "source": src,
                "shipped": False,
                "role": role + " (NOT shipped by the port)",
            })
    # Not-shipped bin blobs: resolve the bin file via the preset's --blob= decl
    # (e.g. stun_pat -> @bin/stun.bin).  Only flag blobs that are neither builtin
    # nor init_vars vars (those are available without a bin file).
    for b in sorted(blobs):
        if b in BUILTIN_BLOBS or b in INIT_VARS_BLOBS:
            continue
        decl = preset_blob_decls.get(b, "")
        # decl is like "@bin/stun.bin" or "0x..."; only @bin/ refs need a file.
        bin_rel = ""
        if decl.startswith("@bin/"):
            bin_rel = "bin/" + decl[len("@bin/"):]
        if bin_rel:
            p = repo_root / bin_rel
            assets.append({
                "path": bin_rel,
                "sha256": sha256_file(p) if p.exists() else "",
                "source": src,
                "shipped": False,
                "role": f"provides blob '{b}' (NOT shipped by the port)",
            })
    # De-dup by path.
    seen: set[str] = set()
    out = []
    for a in assets:
        if a["path"] in seen:
            continue
        seen.add(a["path"])
        out.append(a)
    out.sort(key=lambda a: a["path"])
    return out


# Pinned-repo custom/multishake function sets (scanned at build time in
# build_catalog; these module-level sets are populated there and consulted by
# required_assets_for).  Defaults are empty so the function is safe pre-scan.
_PINNED_CUSTOM_FUNCS: set[str] = set()
_PINNED_MULTISHAKE_FUNCS: set[str] = set()


# ---------------------------------------------------------------------------
# Catalog build
# ---------------------------------------------------------------------------

def scan_pinned_lua_funcs(repo_root: Path) -> set[str]:
    """Function names defined in the pinned repo's lua/*.lua (custom_funcs,
    zapret-multishake, zapret-auto, ...).  A func in this set but NOT in
    KNOWN_CORE_FUNCS is a pinned-repo custom func -> unknown on OpenWrt."""
    funcs: set[str] = set()
    func_re = re.compile(r"^(?:local\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
    lua_dir = repo_root / "lua"
    if not lua_dir.is_dir():
        return funcs
    for p in sorted(lua_dir.glob("*.lua")):
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            m = func_re.match(line)
            if m:
                funcs.add(m.group(1))
    return funcs


def _scan_lua_file_funcs(path: Path) -> set[str]:
    funcs: set[str] = set()
    if not path.is_file():
        return funcs
    func_re = re.compile(r"^(?:local\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = func_re.match(line)
        if m:
            funcs.add(m.group(1))
    return funcs


def parse_all_presets(repo_root: Path) -> list[Preset]:
    presets_dir = repo_root / "presets"
    presets: list[Preset] = []
    for p in sorted(presets_dir.glob("*.txt")):
        data = p.read_bytes()
        text = data.decode("utf-8", errors="replace")
        rel = f"{PRESETS_SUBPATH}{p.name}"
        presets.append(Preset(rel, text, sha256_bytes(data), len(data)))
    return presets


def func_signature(steps: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(s["func"] for s in steps)


def build_catalog(repo_root: Path) -> dict[str, Any]:
    presets = parse_all_presets(repo_root)
    pinned_lua_funcs = scan_pinned_lua_funcs(repo_root)
    # Populate the per-file custom/multishake sets used by required_assets_for
    # to declare not-shipped lua deps for incompatible chains.
    global _PINNED_CUSTOM_FUNCS, _PINNED_MULTISHAKE_FUNCS
    _PINNED_CUSTOM_FUNCS = _scan_lua_file_funcs(repo_root / "lua" / "custom_funcs.lua")
    _PINNED_MULTISHAKE_FUNCS = _scan_lua_file_funcs(repo_root / "lua" / "zapret-multishake.lua")

    # Map chain_id -> aggregated entry.
    entries: dict[str, dict[str, Any]] = {}

    for preset in presets:
        for block in preset.blocks:
            if not block.lua_steps:
                continue
            steps = block.lua_steps
            cid = _chain_id(steps)
            services = detect_services(block)
            domains = detect_domains(block)
            askey = detect_askey(block)
            stable = _stable_id(services, steps, cid)

            deps = build_dependencies(steps, block, preset, pinned_lua_funcs)
            unknown_funcs = deps["unknown_funcs"]
            status = "incompatible" if unknown_funcs else "compatible"

            # circular_compatibility (Step 2): a separate axis from
            # compatibility.status.  Derived purely from the chain content
            # (steps), so it is stable per chain_id -- the same chain always
            # gets the same class regardless of which preset block sourced it.
            circ_compat = _classify_circular_compatibility(steps, _PINNED_CUSTOM_FUNCS)

            warnings: list[str] = []
            for sv in _seqovl_steps(steps):
                warnings.append(
                    f"seqovl={sv}: may cancel on a short SNI when "
                    f"seqovl >= pos[1]-1 (spec §5.2); degrades to seqovl=0 "
                    f"or no-ops for small-SNI domains"
                )
            # Step 2 circular-axis warnings (added alongside the existing
            # seqovl warning above).
            for w in _circular_compatibility_warnings(steps, circ_compat):
                if w not in warnings:
                    warnings.append(w)

            source_block = {
                "source_path": preset.rel_path,
                "source_id": preset.name,
                "source_block_index": block.index,
                "source_sha256": sha256_bytes(block.raw_bytes),
                "source_commit": SOURCE_COMMIT,
            }

            if cid not in entries:
                entries[cid] = {
                    "stable_id": stable,
                    "chain_id": cid,
                    "askey": askey,
                    "transport": detect_protocol_transport(block),
                    "ports": detect_ports(block),
                    "services": services,
                    "domains": domains,
                    "hostlists": sorted(set(block.hostlists)),
                    "ipsets": sorted(set(block.ipsets)),
                    "lua_steps": steps,
                    "source_blocks": [],
                    "compatibility": {
                        "status": status,
                        "dropped_options": sorted(set(preset.dropped_options)),
                        "unknown_funcs": sorted(set(unknown_funcs)),
                        "notes": "",
                    },
                    "circular_compatibility": circ_compat,
                    "warnings": warnings,
                    "dependencies": deps,
                    "required_assets": required_assets_for(
                        steps, block, repo_root, preset.blob_decls),
                    "strategy_number": None,  # assigned in the adaptive profile
                }
            else:
                e = entries[cid]
                # Aggregate across source blocks / presets.
                for s in services:
                    if s not in e["services"]:
                        e["services"].append(s)
                for d in domains:
                    if d not in e["domains"]:
                        e["domains"].append(d)
                for h in block.hostlists:
                    if h not in e["hostlists"]:
                        e["hostlists"].append(h)
                for ip in block.ipsets:
                    if ip not in e["ipsets"]:
                        e["ipsets"].append(ip)
                # Keep the most-specific askey if we encounter a discord one.
                if askey == "discord" and e["askey"] != "discord":
                    e["askey"] = askey
                # Widen the compat unknown_funcs union.
                for f in unknown_funcs:
                    if f not in e["compatibility"]["unknown_funcs"]:
                        e["compatibility"]["unknown_funcs"].append(f)
                if e["compatibility"]["status"] == "compatible" and status == "incompatible":
                    e["compatibility"]["status"] = "incompatible"
                for w in warnings:
                    if w not in e["warnings"]:
                        e["warnings"].append(w)
                # Merge dropped options.
                for d in preset.dropped_options:
                    if d not in e["compatibility"]["dropped_options"]:
                        e["compatibility"]["dropped_options"].append(d)
                # Merge required_assets.
                for a in required_assets_for(steps, block, repo_root, preset.blob_decls):
                    if a["path"] not in [x["path"] for x in e["required_assets"]]:
                        e["required_assets"].append(a)
            entries[cid]["source_blocks"].append(source_block)
            entries[cid]["source_blocks"].sort(
                key=lambda b: (b["source_path"], b["source_block_index"])
            )

    # Build the adaptive profile assignment + final entry list.
    entry_list = _assign_adaptive_and_finalize(entries)
    _finalize_compat_notes(entry_list)
    compat_summary = _compatibility_summary(entry_list)
    circ_summary = _circular_compatibility_summary(entry_list)

    # Compose provenance for the static fallback (shipped verbatim).
    static_fallback = _static_fallback_provenance(repo_root)

    catalog = {
        "schema_version": 1,
        "catalog_version": 1,
        "source": {
            "repo": SOURCE_REPO,
            "commit": SOURCE_COMMIT,
            "presets_path": PRESETS_SUBPATH,
        },
        "default_blocked_pass_domains": _default_blocked_pass_domains_block(),
        "compatibility_reference": {
            "core_lua_repo": CORE_LUA_REPO,
            "core_lua_commit": CORE_LUA_COMMIT,
            "known_core_funcs": sorted(KNOWN_CORE_FUNCS),
            "builtin_blobs": sorted(BUILTIN_BLOBS),
            "init_vars_blobs": sorted(INIT_VARS_BLOBS),
        },
        "compatibility_summary": compat_summary,
        "circular_compatibility_summary": circ_summary,
        "static_fallback_profile": static_fallback,
        "adaptive_profile": _adaptive_profile_meta(entry_list),
        "entries": entry_list,
    }
    return catalog


def _finalize_compat_notes(entry_list: list[dict[str, Any]]) -> None:
    """Populate compatibility.notes so the catalog is self-documenting for the
    runtime learner (B) and the integrator."""
    for e in entry_list:
        comp = e["compatibility"]
        uf = comp["unknown_funcs"]
        missing_blobs = e["dependencies"]["missing_blobs"]
        if comp["status"] == "compatible":
            comp["notes"] = (
                "all lua functions are core (zapret2-core); referenced blobs "
                "resolve via builtin/init_vars.lua and required assets are shipped"
            )
        else:
            parts = []
            if uf:
                parts.append(
                    f"uses pinned-repo custom function(s) {sorted(set(uf))} "
                    f"(NOT shipped on the OpenWrt port runtime; defined in the "
                    f"pinned repo's lua/custom_funcs.lua or lua/zapret-"
                    f"multishake.lua)"
                )
            if missing_blobs:
                parts.append(
                    f"blob(s) {sorted(set(missing_blobs))} require bin assets "
                    f"not shipped by the port"
                )
            comp["notes"] = "; ".join(parts) + (
                ". The chain will not execute on the port runtime until the "
                "defining lua and bin assets are shipped."
            )
        # Adaptive strategy=1 specific note.
        if e["strategy_number"] == 1 and comp["status"] == "incompatible":
            comp["notes"] += (
                " DEFAULT_BLOCKED_PASS_DOMAINS blocks strategy=1 for discord.com "
                "so the live Discord path is strategy=2 (Default v5); the static "
                "discord-v5.opt is the loadable fallback."
            )


def _compatibility_summary(entry_list: list[dict[str, Any]]) -> dict[str, Any]:
    compat = sum(1 for e in entry_list if e["compatibility"]["status"] == "compatible")
    total = len(entry_list)
    incompat = total - compat
    return {
        "total": total,
        "compatible": compat,
        "incompatible": incompat,
        "incompatible_entries": sorted(
            e["stable_id"] for e in entry_list
            if e["compatibility"]["status"] == "incompatible"
        ),
    }


def _circular_compatibility_summary(entry_list: list[dict[str, Any]]) -> dict[str, Any]:
    """Step 2: counts of each circular_compatibility class across the catalog,
    plus the stable_ids of the static-only / incompatible chains (audit anchors
    for the original-pool generator's exclusion set)."""
    total = len(entry_list)
    counts = {c: 0 for c in CIRCULAR_COMPAT_ALL}
    for e in entry_list:
        cc = e.get("circular_compatibility")
        if cc in counts:
            counts[cc] += 1
    return {
        "total": total,
        "circular_compatible": counts[CIRCULAR_COMPAT_CIRCULAR_COMPATIBLE],
        "static_only": counts[CIRCULAR_COMPAT_STATIC_ONLY],
        "incompatible": counts[CIRCULAR_COMPAT_INCOMPATIBLE],
        "static_only_entries": sorted(
            e["stable_id"] for e in entry_list
            if e.get("circular_compatibility") == CIRCULAR_COMPAT_STATIC_ONLY
        ),
        "incompatible_entries": sorted(
            e["stable_id"] for e in entry_list
            if e.get("circular_compatibility") == CIRCULAR_COMPAT_INCOMPATIBLE
        ),
    }


def _is_default_old(steps: list[dict[str, Any]]) -> bool:
    """The canonical Default old Discord chain, identified by EXACT content:
    send:repeats=2 -> syndata:blob=stun_pat -> tls_multisplit_sni with exactly
    seqovl=652:seqovl_pattern=stun_pat (no extra args).  Variants with extra
    args (e.g. ip_autottl) are DIFFERENT chains and must not match."""
    if func_signature(steps) != DEFAULT_OLD_FUNC_SIG:
        return False
    s0, s1, s2 = steps
    return (
        s0["args"] == {"repeats": "2"}
        and s1["args"] == {"blob": "stun_pat"}
        and s2["args"] == {"seqovl": "652", "seqovl_pattern": "stun_pat"}
    )


def _is_default_v5(steps: list[dict[str, Any]]) -> bool:
    """The canonical Default v5 Discord chain, identified by EXACT content:
    send:repeats=3 -> syndata:blob=tls_google -> bare syndata (no extra args)."""
    if func_signature(steps) != DEFAULT_V5_FUNC_SIG:
        return False
    s0, s1, s2 = steps
    return (
        s0["args"] == {"repeats": "3"}
        and s1["args"] == {"blob": "tls_google"}
        and s2["args"] == {}
    )


def _assign_adaptive_and_finalize(
    entries: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Identify Default old + Default v5 by content; assign strategy_number
    1 and 2 (contract §4).  All other chains keep strategy_number=null."""
    old_chain_id = None
    v5_chain_id = None
    for cid, e in entries.items():
        steps = e["lua_steps"]
        # Both Default old and Default v5 must be Discord-pool candidates
        # (rule 9): service contains "discord" OR domains/ipsets target Discord.
        in_discord_pool = (
            "discord" in e["services"]
            or any("discord" in d for d in e["domains"])
            or any("discord" in ip for ip in e["ipsets"])
            or any("discord" in h for h in e["hostlists"])
        )
        if not in_discord_pool:
            continue
        if _is_default_old(steps) and old_chain_id is None:
            old_chain_id = cid
        elif _is_default_v5(steps) and v5_chain_id is None:
            v5_chain_id = cid

    if old_chain_id:
        entries[old_chain_id]["strategy_number"] = 1
    if v5_chain_id:
        entries[v5_chain_id]["strategy_number"] = 2

    # Deterministic ordering: by strategy_number (2,1 first) then stable_id.
    entry_list = list(entries.values())

    # Flat provenance fields (contract §1 / task Step 2 require source_id,
    # source_commit, source_path, source_sha256 at entry top level). A chain
    # may legitimately derive from multiple preset blocks (source_blocks[] is
    # the full multi-block provenance); the flat fields mirror the FIRST block
    # (source_blocks is sorted by source_path then index, so the first is the
    # canonical/earliest preset). Keeping source_blocks[] preserves the richer
    # model; the flat fields satisfy the task's per-entry contract.
    for e in entry_list:
        blocks = e.get("source_blocks") or []
        if blocks:
            b0 = blocks[0]
            e["source_id"] = b0.get("source_id", "")
            e["source_commit"] = b0.get("source_commit", SOURCE_COMMIT)
            e["source_path"] = b0.get("source_path", "")
            e["source_sha256"] = b0.get("source_sha256", "")
            e["source_block_index"] = b0.get("source_block_index")
        else:
            e["source_id"] = ""
            e["source_commit"] = SOURCE_COMMIT
            e["source_path"] = ""
            e["source_sha256"] = ""
            e["source_block_index"] = None

    def sort_key(e: dict[str, Any]) -> tuple:
        sn = e["strategy_number"]
        # Put assigned strategies first (1,2) then null; within, by stable_id.
        sn_key = 0 if sn is not None else 1
        return (sn_key, sn if sn is not None else 0, e["stable_id"])

    entry_list.sort(key=sort_key)
    return entry_list


def _adaptive_profile_meta(entry_list: list[dict[str, Any]]) -> dict[str, Any]:
    chains = []
    for e in entry_list:
        if e["strategy_number"] is None:
            continue
        chains.append({
            "strategy": e["strategy_number"],
            "chain_id": e["chain_id"],
            "stable_id": e["stable_id"],
            "source_id": e["source_blocks"][0]["source_id"] if e["source_blocks"] else "",
            "compatibility": e["compatibility"]["status"],
            "circular_compatibility": e.get("circular_compatibility",
                                            CIRCULAR_COMPAT_CIRCULAR_COMPATIBLE),
        })
    chains.sort(key=lambda c: c["strategy"])
    chain_id_for_strategy = {str(c["strategy"]): c["stable_id"] for c in chains}
    strategy_for_chain_id = {c["stable_id"]: c["strategy"] for c in chains}
    return {
        "profile_id": "discord-adaptive",
        "askey": "tls",
        "chain_id_for_strategy": chain_id_for_strategy,
        "strategy_for_chain_id": strategy_for_chain_id,
        "default_blocked_pass_domains_applied": True,
        "chains": chains,
    }


# ---------------------------------------------------------------------------
# DEFAULT_BLOCKED_PASS_DOMAINS (exact set from the pinned GUI)
# ---------------------------------------------------------------------------

# Exact set imported from zapret2gui/src/orchestra/blocked_strategies_manager.py
# lines 65-102 @ 9d57e55 (the DEFAULT_BLOCKED_PASS_DOMAINS set).  discord.com is
# present.  NOT derived from autohostlist.
DEFAULT_BLOCKED_PASS_DOMAINS = [
    # Discord
    "discord.com", "discordapp.com", "discord.gg", "discord.media", "discordapp.net",
    # YouTube / Google Video
    "youtube.com", "googlevideo.com", "ytimg.com", "yt3.ggpht.com", "youtu.be",
    "ggpht.com", "googleusercontent.com", "youtube-nocookie.com",
    # Google
    "google.com", "google.ru", "googleapis.com", "gstatic.com",
    "googleadservices.com", "googlesyndication.com", "googletagmanager.com",
    "googleanalytics.com", "google-analytics.com", "doubleclick.net",
    "dns.google", "withgoogle.com", "withyoutube.com",
    # Twitch
    "twitch.tv", "twitchcdn.net",
    # Twitter/X
    "twitter.com", "x.com", "twimg.com",
    # Instagram
    "instagram.com", "cdninstagram.com", "igcdn.com", "ig.me",
    # Facebook / Meta
    "facebook.com", "fbcdn.net", "fb.com", "fb.me",
    # WhatsApp
    "whatsapp.com", "whatsapp.net",
    # TikTok
    "tiktok.com", "tiktokcdn.com", "musical.ly",
    # Spotify
    "spotify.com", "spotifycdn.com",
    # Netflix
    "netflix.com", "nflxvideo.net",
    # Steam
    "steampowered.com", "steamcommunity.com", "steamstatic.com",
    # Roblox
    "roblox.com", "rbxcdn.com",
    # Reddit
    "reddit.com", "redd.it", "redditmedia.com",
    # GitHub
    "github.com", "githubusercontent.com",
    # Rutracker
    "rutracker.org",
]


def _default_blocked_pass_domains_block() -> dict[str, Any]:
    return {
        "source_repo": GUI_REPO,
        "source_commit": GUI_COMMIT,
        "source_path": GUI_SOURCE_PATH,
        "domains": sorted(DEFAULT_BLOCKED_PASS_DOMAINS),
    }


# ---------------------------------------------------------------------------
# Adaptive profile (.opt) + sidecar (.json) generation
# ---------------------------------------------------------------------------

ADAPTIVE_SELECTOR = (
    "circular_quality:key=tls:fails=1:"
    "failure_detector=combined_failure_detector:"
    "success_detector=combined_success_detector:"
    "lock_successes=3:unlock_fails=3:lock_tests=5:lock_rate=0.6:"
    "inseq=1:nld=3"
)


def _step_to_opt_line(step: dict[str, Any], strategy: int) -> str:
    parts = [step["func"]]
    for k, v in step["args"].items():
        if k.startswith("_flag_"):
            parts.append(str(v))
        else:
            parts.append(f"{k}={v}")
    parts.append(f"strategy={strategy}")
    return "--lua-desync=" + ":".join(parts)


def build_adaptive_opt(catalog: dict[str, Any]) -> str:
    chains = {c["strategy"]: c for c in catalog["adaptive_profile"]["chains"]}
    lines: list[str] = ['NFQWS2_OPT="']
    # Lua init: orchestra runtime (circular_quality/slm) + init_vars (tls_google
    # for v5) + custom_funcs (tls_multisplit_sni for old -- NOT shipped by the
    # port; see compatibility notes in the catalog).
    lines.append("--lua-init=@/opt/zapret2/lua/orchestra-extra/init.lua")
    lines.append("--lua-init=@/opt/zapret2/lua/init_vars.lua")
    lines.append("--lua-init=@/opt/zapret2/lua/custom_funcs.lua")
    # stun_pat blob for Default old (strategy=1).  bin/stun.bin is NOT shipped
    # by the port; declared here for a complete, intent-faithful profile.
    lines.append("--blob=stun_pat:@/opt/zapret2/bin/stun.bin")
    # Filter: Discord ipset (shipped), TLS client hello.
    lines.append("--filter-tcp=80,443,1080,2053,2083,2087,2096,8443")
    lines.append("--ipset=/etc/zapret2-orchestra/lists/ipset-discord.txt")
    lines.append("--payload=tls_client_hello")
    # --in-range MUST be set (non-x): nfqws2 defaults --in-range=x (never),
    # which blocks ALL incoming packets from Lua and starves
    # combined_success_detector of reply packets, so SUCCESS is never detected
    # and the learning loop never locks.  -d1000 lets the first 1000 incoming
    # data packets reach circular_quality (matches reference circular-config.txt).
    lines.append("--in-range=-d1000")
    lines.append("--out-range=-d10")
    # Selector first (contract §1.3: selector is unnumbered).
    lines.append(f"--lua-desync={ADAPTIVE_SELECTOR}")
    # Strategy 1 = Default old (3 steps, one chain).
    for e in catalog["entries"]:
        if e["strategy_number"] == 1:
            for st in e["lua_steps"]:
                lines.append(_step_to_opt_line(st, 1))
            break
    # Strategy 2 = Default v5 (3 steps, one chain).
    for e in catalog["entries"]:
        if e["strategy_number"] == 2:
            for st in e["lua_steps"]:
                lines.append(_step_to_opt_line(st, 2))
            break
    lines.append('"')
    return "\n".join(lines) + "\n"


def build_adaptive_sidecar(catalog: dict[str, Any]) -> str:
    meta = catalog["adaptive_profile"]
    sidecar = {
        "schema_version": 1,
        "profile_id": meta["profile_id"],
        "askey": meta["askey"],
        "chain_id_for_strategy": meta["chain_id_for_strategy"],
        "strategy_for_chain_id": meta["strategy_for_chain_id"],
        "default_blocked_pass_domains_applied": meta["default_blocked_pass_domains_applied"],
        "chains": meta["chains"],
    }
    return json.dumps(sidecar, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# Step 3: original-parity adaptive pool generated from the GUI Default
# (circular).txt TLS circular pool (the SECOND --new block, strategies 1-29).
#
# Provenance: youtubediscord/zapret @ 9d57e55 (the zapret2gui submodule),
# zapret2gui/src/presets/builtin/winws2/Default (circular).txt.  This is a
# DIFFERENT source from the native-preset catalog above (which comes from
# youtubediscord/zapret2-youtube-discord @ 4d75c70b).  The pool composition is
# the source of truth per docs/ORCHESTRA_ORIGINAL_TLS_POOL.md.
# ---------------------------------------------------------------------------

ORIGINAL_POOL_PROFILE_ID = "discord-adaptive-original-pool"
ORIGINAL_POOL_SOURCE_REPO = GUI_REPO  # youtubediscord/zapret
ORIGINAL_POOL_SOURCE_COMMIT = GUI_COMMIT  # 9d57e55...
ORIGINAL_POOL_SOURCE_PATH = (
    "zapret2gui/src/presets/builtin/winws2/Default (circular).txt"
)
ORIGINAL_POOL_SOURCE_LABEL = "Default (circular).txt @ 9d57e55"

DEFAULT_ORIGINAL_POOL_PRESET = Path(
    "zapret2gui/src/presets/builtin/winws2/Default (circular).txt"
)

# The port circular_quality selector, with fails=3 to keep the original
# pool's rotation spirit (3 consecutive failures -> rotate to the next
# strategy).  The rest of the args are the port's circular_quality knobs
# (detectors, lock/unlock thresholds, inseq, nld) -- the port engine is
# circular_quality, not the original `circular`.
ORIGINAL_POOL_SELECTOR = (
    "circular_quality:key=tls:fails=3:"
    "failure_detector=combined_failure_detector:"
    "success_detector=combined_success_detector:"
    "lock_successes=3:unlock_fails=3:lock_tests=5:lock_rate=0.6:"
    "inseq=1:nld=3"
)

# OpenWrt path mapping for bin blobs referenced by the original pool (the .bin
# files are shipped separately; declared here so the profile is complete).
# tls_google is provided by init_vars.lua (no --blob needed); the
# fake_default_* blobs are nfqws2 builtins (no --blob needed).


def _original_bin_blob_port_path(blob_name: str) -> str | None:
    """Map an original-pool bin blob name to its OpenWrt --blob= path, or None
    if the blob is not a bin-shipped blob (builtin / init_vars / unknown)."""
    if blob_name == "stun_pat":
        return "@/opt/zapret2/bin/stun.bin"
    m = re.fullmatch(r"tls(\d+)", blob_name)
    if m:
        return f"@/opt/zapret2/bin/tls_clienthello_{m.group(1)}.bin"
    return None


_POOL_SELECTOR_RE = re.compile(r"^--lua-desync=(?:circular|circular_quality)(?::|$)")
_DESYNC_TOKEN_RE = re.compile(r"--lua-desync=(\S+)")
_STRATEGY_TAG_RE_POOL = re.compile(r":strategy=(\d+)$")


def parse_original_pool(text: str) -> list[dict[str, Any]]:
    """Parse the TLS circular pool block of ``Default (circular).txt`` and
    return the strategies in original order.

    Each entry is ``{"original_strategy": <int>, "source_line": <int 1-based>,
    "steps": [{"func":..., "args":{...}}, ...]}``.  The selector line is NOT a
    strategy; only the ``:strategy=N`` desync lines are.  Multiple
    ``--lua-desync=`` on the same logical line share the same N (per
    ``circular_strategy_numbering.py``); they are grouped by N here.
    """
    # Split into --new-separated segments (line-anchored).
    segments = re.split(r"(?m)^[ \t]*--new[ \t]*$", text)
    pool_seg: str | None = None
    for seg in segments:
        # The TLS circular pool: has --filter-tcp= AND a `circular` selector.
        # The first block (--lua-desync=pass) and the UDP pool (--filter-udp=)
        # are excluded by this conjunction.
        if "--filter-tcp=" in seg and re.search(
                r"--lua-desync=circular(?!_quality)(?::|$)", seg):
            pool_seg = seg
            break
    if pool_seg is None:
        raise ValueError(
            "original TLS circular pool block not found in preset "
            "(expected a --filter-tcp= block with --lua-desync=circular:)")

    # Collect steps grouped by original strategy number, preserving order.
    grouped: dict[int, list[dict[str, Any]]] = {}
    source_line: dict[int, int] = {}
    line_no = 0
    # Reconstruct the line numbers within the FULL text so source_line is
    # auditable to the original preset.  Find the pool segment's start offset.
    seg_start = text.find(pool_seg)
    base_line = text.count("\n", 0, seg_start) + 1
    for raw in pool_seg.splitlines():
        line_no += 1
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        for body in _DESYNC_TOKEN_RE.findall(raw):
            if _POOL_SELECTOR_RE.match(f"--lua-desync={body}"):
                continue  # the selector is unnumbered
            m = _STRATEGY_TAG_RE_POOL.search(body)
            if not m:
                continue  # not a numbered strategy step
            orig_n = int(m.group(1))
            func_body = _STRATEGY_TAG_RE_POOL.sub("", body)
            func, args = _parse_kv(func_body)
            args.pop("strategy", None)  # safety; already stripped by the sub
            grouped.setdefault(orig_n, []).append({"func": func, "args": args})
            source_line.setdefault(orig_n, base_line + line_no - 1)

    strategies: list[dict[str, Any]] = []
    for orig_n in sorted(grouped.keys()):
        strategies.append({
            "original_strategy": orig_n,
            "source_line": source_line[orig_n],
            "steps": grouped[orig_n],
        })
    return strategies


def _compatibility_status_for(steps: list[dict[str, Any]]) -> str:
    """The existing static-compatible axis (compatibility.status), computed
    standalone for an original-pool chain: incompatible if any func is not in
    KNOWN_CORE_FUNCS, else compatible.  Mirrors build_catalog's logic."""
    funcs = [s["func"] for s in steps]
    unknown = [f for f in funcs if f not in KNOWN_CORE_FUNCS]
    return "incompatible" if unknown else "compatible"


def build_original_pool(
    preset_path: Path,
    shipped_custom_funcs: set[str] | None = None,
) -> dict[str, Any]:
    """Generate the original-parity adaptive pool (Step 3).

    Returns ``{"opt": <str>, "sidecar": <dict>}``.  Parses the original TLS
    circular pool, classifies each strategy by circular_compatibility, EXCLUDES
    static_only and incompatible chains, renumbers the included
    circular_compatible chains contiguously from 1 (circular_quality requires
    contiguous-from-1, orchestrator.lua:23), and records the
    original_strategy -> generated_strategy map for provenance.
    """
    text = preset_path.read_text(encoding="utf-8", errors="replace")
    strategies = parse_original_pool(text)
    shipped = (shipped_custom_funcs if shipped_custom_funcs is not None
               else _PINNED_CUSTOM_FUNCS)

    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for s in strategies:
        steps = s["steps"]
        cc = _classify_circular_compatibility(steps, shipped)
        rec = {
            "original_strategy": s["original_strategy"],
            "source_line": s["source_line"],
            "steps": steps,
            "chain_id": _chain_id(steps),
            "stable_id": _stable_id([], steps, _chain_id(steps)),
            "circular_compatibility": cc,
            "compatibility": _compatibility_status_for(steps),
        }
        if cc == CIRCULAR_COMPAT_CIRCULAR_COMPATIBLE:
            included.append(rec)
        else:
            excluded.append(rec)

    # Renumber the included chains contiguously from 1.
    orig_to_gen: dict[int, int] = {}
    chains: list[dict[str, Any]] = []
    for gen_n, rec in enumerate(included, start=1):
        orig_to_gen[rec["original_strategy"]] = gen_n
        chains.append({**rec, "generated_strategy": gen_n})

    opt = _build_original_pool_opt(chains)
    sidecar = _build_original_pool_sidecar(
        chains, excluded, orig_to_gen, preset_path)
    return {"opt": opt, "sidecar": sidecar}


def _build_original_pool_opt(chains: list[dict[str, Any]]) -> str:
    # Blobs referenced by the included chains that need a --blob= declaration
    # (i.e. neither a builtin nor an init_vars var).  Declared sorted so the
    # output is deterministic.
    referenced: set[str] = set()
    for ch in chains:
        for s in ch["steps"]:
            b = s["args"].get("blob")
            if b:
                referenced.add(b)
            p = s["args"].get("seqovl_pattern")
            if p:
                referenced.add(p)
    bin_blobs = sorted(
        b for b in referenced
        if b not in BUILTIN_BLOBS and b not in INIT_VARS_BLOBS
        and _original_bin_blob_port_path(b) is not None
    )

    lines: list[str] = []
    # Document the packet-selection choice in comments BEFORE the assignment
    # (the NFQWS2_OPT parser skips `#` comment lines; apply.uc mirrors this).
    lines.append(
        f"# {ORIGINAL_POOL_PROFILE_ID}: original-parity adaptive TLS pool "
        "generated from")
    lines.append(
        f"# {ORIGINAL_POOL_SOURCE_PATH} @ {ORIGINAL_POOL_SOURCE_COMMIT[:7]} "
        "(the zapret2gui submodule).")
    lines.append(
        f"# {len(chains)} circular_compatible strategies of the original 29 "
        "(static-only send-cutoff and incompatible chains excluded); "
        "renumbered contiguously from 1.")
    lines.append(
        "# Provenance + the original->generated strategy map: see the .json "
        "sidecar.  Blobs tls1/tls5/tls7 ship separately as")
    lines.append(
        "# /opt/zapret2/bin/tls_clienthello_{1,5,7}.bin; stun_pat as "
        "/opt/zapret2/bin/stun.bin.")
    lines.append(
        "# Packet selection: --in-range=-d1000 --out-range=-d10 (port convention).  "
        "The original used")
    lines.append(
        "# --out-range=-s9656 --in-range=-s3508 (seq-range packet filters); "
        "-d10/-d1000 are used here for")
    lines.append(
        "# consistency with discord-adaptive.opt.  --in-range MUST be set (non-x): "
        "nfqws2 defaults --in-range=x")
    lines.append(
        "# (never), which blocks ALL incoming packets from Lua and starves "
        "combined_success_detector of reply")
    lines.append(
        "# packets, so SUCCESS is never detected and the learning loop never locks.  "
        "--in-range=-d1000 lets the")
    lines.append(
        "# first 1000 incoming data packets reach circular_quality so the success "
        "detector can see the TLS")
    lines.append(
        "# server reply (incoming seq > inseq=1).  --payload=all is kept from "
        "the original so HTTP-fake")
    lines.append(
        "# strategies (e.g. fake_default_http) work.")
    lines.append('NFQWS2_OPT="')
    # Lua init: orchestra runtime (circular_quality/slm) + init_vars (tls_google)
    # + custom_funcs (tls_multisplit_sni).  NOT custom_diag / zapret-multishake /
    # fakemultisplit / fakemultidisorder (not needed by strategies 1-29, not
    # shipped by the port -- see docs/ORCHESTRA_ORIGINAL_TLS_POOL.md §3).
    lines.append("--lua-init=@/opt/zapret2/lua/orchestra-extra/init.lua")
    lines.append("--lua-init=@/opt/zapret2/lua/init_vars.lua")
    lines.append("--lua-init=@/opt/zapret2/lua/custom_funcs.lua")
    for b in bin_blobs:
        lines.append(f"--blob={b}:{_original_bin_blob_port_path(b)}")
    lines.append("--filter-tcp=80,443,1080,2053,2083,2087,2096,8443")
    lines.append("--ipset=/etc/zapret2-orchestra/lists/ipset-discord.txt")
    lines.append("--payload=all")
    # --in-range=-d1000: see discord-adaptive.opt.  Required for SUCCESS detection.
    lines.append("--in-range=-d1000")
    lines.append("--out-range=-d10")
    # Selector first (contract §1.3: selector is unnumbered).
    lines.append(f"--lua-desync={ORIGINAL_POOL_SELECTOR}")
    # Each included chain, in generated-strategy order (contiguous from 1).
    for ch in chains:
        for st in ch["steps"]:
            lines.append(_step_to_opt_line(st, ch["generated_strategy"]))
    lines.append('"')
    return "\n".join(lines) + "\n"


def _build_original_pool_sidecar(
    chains: list[dict[str, Any]],
    excluded: list[dict[str, Any]],
    orig_to_gen: dict[int, int],
    preset_path: Path,
) -> dict[str, Any]:
    chain_id_for_strategy = {
        str(ch["generated_strategy"]): ch["stable_id"] for ch in chains
    }
    strategy_for_chain_id = {
        ch["stable_id"]: ch["generated_strategy"] for ch in chains
    }
    chain_records = []
    for ch in chains:
        chain_records.append({
            "generated_strategy": ch["generated_strategy"],
            "original_strategy_number": ch["original_strategy"],
            "chain_id": ch["chain_id"],
            "stable_id": ch["stable_id"],
            "source": ORIGINAL_POOL_SOURCE_LABEL,
            "circular_compatibility": ch["circular_compatibility"],
            "compatibility": ch["compatibility"],
        })
    chain_records.sort(key=lambda r: r["generated_strategy"])

    excluded_detail = sorted(
        ({
            "original_strategy": e["original_strategy"],
            "circular_compatibility": e["circular_compatibility"],
            "compatibility": e["compatibility"],
            "reason": (
                "send sets a voluntary cutoff; later steps unreachable inside "
                "circular"
                if e["circular_compatibility"] == CIRCULAR_COMPAT_STATIC_ONLY
                else "uses a function not in core + shipped custom_funcs.lua"
            ),
        } for e in excluded),
        key=lambda r: r["original_strategy"],
    )

    return {
        "schema_version": 1,
        "profile_id": ORIGINAL_POOL_PROFILE_ID,
        "askey": "tls",
        "chain_id_for_strategy": chain_id_for_strategy,
        "strategy_for_chain_id": strategy_for_chain_id,
        "default_blocked_pass_domains_applied": True,
        "chains": chain_records,
        "provenance": {
            "source_repo": ORIGINAL_POOL_SOURCE_REPO,
            "source_commit": ORIGINAL_POOL_SOURCE_COMMIT,
            "source_path": ORIGINAL_POOL_SOURCE_PATH,
            "source_label": ORIGINAL_POOL_SOURCE_LABEL,
            "selector": ORIGINAL_POOL_SELECTOR,
            "original_strategy_numbers": {
                "included": sorted(orig_to_gen.keys()),
                "excluded": sorted(e["original_strategy"] for e in excluded),
                "excluded_detail": excluded_detail,
            },
            "original_to_generated_strategy_map": {
                str(k): orig_to_gen[k] for k in sorted(orig_to_gen)
            },
            "note": (
                "Strategy numbers are renumbered contiguously from 1 for the "
                "port circular_quality engine (orchestrator.lua:23 requires "
                "contiguous-from-1).  The original_strategy_number -> "
                "generated_strategy map above preserves provenance."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Static fallback provenance (discord-v5.opt + init_vars.lua + ipset-discord.txt)
# ---------------------------------------------------------------------------

def _static_fallback_provenance(repo_root: Path) -> dict[str, Any]:
    init_vars = repo_root / "lua" / "init_vars.lua"
    ipset = repo_root / "lists" / "ipset-discord.txt"
    return {
        "profile_id": "discord-v5",
        "description": (
            "Static native nfqws2 profile (no circular_quality, no learning) "
            "-- the proven manual fallback. NOT the Orchestra implementation."
        ),
        "source_commit": SOURCE_COMMIT,
        "assets": [
            {
                "path": "profiles/discord-v5.opt",
                "sha256": _SHA_DISCORD_V5_OPT,
                "source": f"{SOURCE_REPO}@{SOURCE_COMMIT[:7]}",
                "derived_from": "presets/Default v5.txt (ipset-discord block, "
                                "path-remapped for OpenWrt)",
            },
            {
                "path": "lua/init_vars.lua",
                "sha256": sha256_canonical(init_vars) if init_vars.exists() else "",
                "source": f"{SOURCE_REPO}@{SOURCE_COMMIT[:7]}",
                "derived_from": "lua/init_vars.lua (verbatim, LF-canonical)",
            },
            {
                "path": "lists/ipset-discord.txt",
                "sha256": sha256_canonical(ipset) if ipset.exists() else "",
                "source": f"{SOURCE_REPO}@{SOURCE_COMMIT[:7]}",
                "derived_from": "lists/ipset-discord.txt (verbatim, LF-canonical)",
            },
        ],
    }


# sha256 of the shipped discord-v5.opt (set after the file is written; updated
# by the importer's main() so the catalog records the exact shipped bytes).
_SHA_DISCORD_V5_OPT = "0ba1577c1881ee208b3e2ac5990bfb6aa32c13175903b05ddaec961a6486d7eb"


# ---------------------------------------------------------------------------
# Source manifest
# ---------------------------------------------------------------------------

def build_manifest(repo_root: Path) -> dict[str, Any]:
    presets_dir = repo_root / "presets"
    files = []
    for p in sorted(presets_dir.glob("*.txt")):
        data = p.read_bytes()
        files.append({
            "path": f"{PRESETS_SUBPATH}{p.name}",
            "sha256": sha256_bytes(data),
            "size": len(data),
        })
    # Also record the pinned lua assets used for closure (init_vars, custom_funcs,
    # zapret-multishake) so the manifest is self-contained.
    for rel in ("lua/init_vars.lua", "lua/custom_funcs.lua",
                "lua/zapret-multishake.lua", "lists/ipset-discord.txt"):
        p = repo_root / rel
        if p.exists():
            data = p.read_bytes()
            files.append({
                "path": rel,
                "sha256": sha256_bytes(data),
                "size": len(data),
            })
    files.sort(key=lambda f: f["path"])
    return {
        "schema_version": 1,
        "repo": SOURCE_REPO,
        "commit": SOURCE_COMMIT,
        "presets_path": PRESETS_SUBPATH,
        "files": files,
    }


# ---------------------------------------------------------------------------
# Output writing (deterministic JSON)
# ---------------------------------------------------------------------------

def _dump_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_outputs(repo_root: Path, out_dir: Path, profile_dir: Path,
                  static_dir: Path, static_src: Path,
                  original_pool_preset: Path | None = None) -> dict[str, Path]:
    catalog = build_catalog(repo_root)
    manifest = build_manifest(repo_root)
    domains = _default_blocked_pass_domains_block()

    # Refresh the static-fallback discord-v5.opt sha256 from the SHIPPED file.
    shipped_v5 = static_dir / "discord-v5.opt"
    if shipped_v5.exists():
        catalog["static_fallback_profile"]["assets"][0]["sha256"] = sha256_file(shipped_v5)

    out_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)
    static_dir.mkdir(parents=True, exist_ok=True)
    # Package files root: profile_dir = <pkg>/usr/share/zapret2-orchestra/profiles
    # so parents[3] = <pkg>/files.  lua + lists live under that root (spec §6).
    pkg_files = profile_dir.parents[3]
    lists_dir = pkg_files / "etc" / "zapret2-orchestra" / "lists"
    lua_dir = pkg_files / "opt" / "zapret2" / "lua"
    lists_dir.mkdir(parents=True, exist_ok=True)
    lua_dir.mkdir(parents=True, exist_ok=True)

    paths: dict[str, Path] = {}

    # Write text outputs as explicit LF bytes so the on-disk file matches the
    # Git-normalized committed version (no Windows CRLF translation).
    def _write(path: Path, text: str) -> None:
        path.write_bytes(text.encode("utf-8"))

    p = out_dir / "manifest.json"
    _write(p, _dump_json(manifest))
    paths["manifest"] = p

    p = out_dir / "catalog.json"
    _write(p, _dump_json(catalog))
    paths["catalog"] = p

    p = out_dir / "default-blocked-pass-domains.json"
    _write(p, _dump_json(domains))
    paths["default_blocked_pass_domains"] = p

    # Adaptive profile + sidecar.
    p = profile_dir / "discord-adaptive.opt"
    _write(p, build_adaptive_opt(catalog))
    paths["discord_adaptive_opt"] = p

    p = profile_dir / "discord-adaptive.json"
    _write(p, build_adaptive_sidecar(catalog))
    paths["discord_adaptive_json"] = p

    # Step 3: original-parity adaptive pool (discord-adaptive-original-pool),
    # generated from the GUI Default (circular).txt TLS circular pool.  This
    # is ADDITIONAL to discord-adaptive.opt above (which stays unchanged for
    # regression comparison).  The original-pool profile uses the port
    # circular_quality engine with the original pool's circular-compatible
    # strategies (static-only send-cutoff and incompatible chains excluded).
    if original_pool_preset is not None and original_pool_preset.is_file():
        orig = build_original_pool(original_pool_preset, _PINNED_CUSTOM_FUNCS)
        p = profile_dir / f"{ORIGINAL_POOL_PROFILE_ID}.opt"
        _write(p, orig["opt"])
        paths["discord_adaptive_original_pool_opt"] = p

        p = profile_dir / f"{ORIGINAL_POOL_PROFILE_ID}.json"
        _write(p, _dump_json(orig["sidecar"]))
        paths["discord_adaptive_original_pool_json"] = p

    # Static fallback: copy from the r7 prepared artifacts, normalized to the
    # LF-canonical form (the pinned repo's .gitattributes forces eol=crlf in the
    # working tree, but the canonical Git blob is LF).  discord-v5.opt is
    # already LF; init_vars.lua/ipset-discord.txt are CRLF in the source working
    # tree and are normalized here so the shipped bytes match the recorded
    # provenance sha256 and the committed Git blob.
    for fname in ("discord-v5.opt", "init_vars.lua", "ipset-discord.txt"):
        src = static_src / fname
        if not src.exists():
            continue
        data = src.read_bytes().replace(b"\r\n", b"\n")
        if fname == "discord-v5.opt":
            dst = profile_dir / fname
        elif fname == "init_vars.lua":
            dst = lua_dir / fname
        else:
            dst = lists_dir / fname
        dst.write_bytes(data)
        paths[f"static_{fname}"] = dst

    return paths


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

DEFAULT_REPO_ROOT = Path(os.environ.get("ZAPRET2_PRESET_REPO_ROOT", "H:/zapret-port/strategy-research/zapret2-youtube-discord"))
DEFAULT_OUT_DIR = Path("strategy-sources")
DEFAULT_PROFILE_DIR = Path(
    "openwrt/zapret2-orchestra/files/usr/share/zapret2-orchestra/profiles"
)
DEFAULT_STATIC_SRC = Path(os.environ.get("ZAPRET2_STATIC_SRC", ""))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT,
                    help="pinned zapret2-youtube-discord repo root")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                    help="strategy-sources output directory")
    ap.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR,
                    help="profiles output directory")
    ap.add_argument("--static-src", type=Path, default=DEFAULT_STATIC_SRC,
                    help="r7 static-fallback artifacts source directory")
    ap.add_argument("--original-pool-preset", type=Path,
                    default=DEFAULT_ORIGINAL_POOL_PRESET,
                    help="path to the GUI Default (circular).txt preset the "
                         "original-parity adaptive pool is generated from")
    args = ap.parse_args(argv)

    if not args.repo_root.is_dir():
        print(f"error: repo root not found: {args.repo_root}", file=sys.stderr)
        return 2

    # repo_root / out_dir / profile_dir resolve relative to CWD by default; when
    # invoked from the worktree root they land in the right place.  Absolute
    # paths are honored as-is.
    repo_root = args.repo_root.resolve()
    out_dir = args.out_dir
    profile_dir = args.profile_dir
    static_src = args.static_src
    if not static_src or not str(static_src).strip() or not static_src.exists():
        static_src = repo_root  # fall back to preset repo root
    original_pool_preset = args.original_pool_preset

    # The static fallback ships into the package profiles/lua/lists tree which
    # is anchored at the profile_dir's grandparent (the share dir).
    share_dir = profile_dir.parent
    static_dir = profile_dir  # discord-v5.opt lives in profiles/

    paths = write_outputs(repo_root, out_dir, profile_dir, static_dir,
                          static_src, original_pool_preset)

    # Summary for humans.
    catalog = json.loads((out_dir / "catalog.json").read_text(encoding="utf-8"))
    entries = catalog["entries"]
    n_chains = len(entries)
    n_compat = sum(1 for e in entries if e["compatibility"]["status"] == "compatible")
    n_incompat = n_chains - n_compat
    presets_n = len([f for f in manifest_files(repo_root)])
    print(f"presets discovered: {presets_n}")
    print(f"chains generated:   {n_chains}")
    print(f"compatible:         {n_compat}")
    print(f"incompatible:       {n_incompat}")
    circ = catalog.get("circular_compatibility_summary", {})
    if circ:
        print(f"circular_compatible: {circ.get('circular_compatible')}")
        print(f"static_only:         {circ.get('static_only')}")
        print(f"circular_incompat:   {circ.get('incompatible')}")
    for e in entries:
        if e["strategy_number"] is not None:
            print(f"  strategy={e['strategy_number']}  {e['stable_id']}  "
                  f"({e['compatibility']['status']})  chain_id={e['chain_id'][:12]}")
    for key in ("manifest", "catalog", "default_blocked_pass_domains",
                "discord_adaptive_opt", "discord_adaptive_json",
                "discord_adaptive_original_pool_opt",
                "discord_adaptive_original_pool_json"):
        if key in paths:
            print(f"wrote: {paths[key]}")
    return 0


def manifest_files(repo_root: Path):
    return list((repo_root / "presets").glob("*.txt"))


if __name__ == "__main__":
    raise SystemExit(main())
