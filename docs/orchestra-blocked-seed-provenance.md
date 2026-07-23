# blocked.json seed provenance

The 59 host entries under `protocols.tls.hosts` (each `[1]`) are the EXACT
`DEFAULT_BLOCKED_PASS_DOMAINS` set imported verbatim from the pinned GUI source:

  repo:   youtubediscord/zapret
  commit: 9d57e55d6751587d9d52b52147a05a0a8fcc9fd8
  path:   zapret2gui/src/orchestra/blocked_strategies_manager.py:65-102

On the original desktop Orchestra, `strategy=1` (the pass-through chain) is
default-blacklisted for these domains because they are known-blocked by RKN, so
trying strategy=1 wastes a rotation slot. `discord.com` MUST be present
(contract §1 rule 7; a parity test asserts it). These are DEFAULT blocks: per
`blocked_strategies_manager.py:450` they cannot be unblocked by a user action.
In the port, `generate-preload.uc` emits them as `slm_preload_blocked("tls",
<host>, {1})` so `slm_is_blocked` skips strategy=1 during circular rotation and
the runtime moves to the next strategy (e.g. strategy=2, the Default v5 chain).

This file is a seed note only; `blocked.json` itself is the conffile the
package installs. The set is NOT derived from autohostlist — it is the literal
pinned set, kept in lockstep with the importer (Subagent A) which reads the same
pinned source into `strategy-sources/catalog.json`'s
`default_blocked_pass_domains.domains`.
