# blocked.json seed provenance

The 59 host entries under `protocols.tls.hosts_chain` (each
`["discord-send-syndata-tls_multisplit_sni-44860d17"]`) are the EXACT
`DEFAULT_BLOCKED_PASS_DOMAINS` set imported verbatim from the pinned GUI source:

  repo:   youtubediscord/zapret
  commit: 9d57e55d6751587d9d52b52147a05a0a8fcc9fd8
  path:   zapret2gui/src/orchestra/blocked_strategies_manager.py:65-102

On the original desktop Orchestra, `strategy=1` (the pass-through chain) is
default-blacklisted for these domains because they are known-blocked by RKN, so
trying strategy=1 wastes a rotation slot. `discord.com` MUST be present
(contract §1 rule 7; a parity test asserts it). These are DEFAULT blocks: per
`blocked_strategies_manager.py:450` they cannot be unblocked by a user action.

## r7 stable-identity form

The port previously stored these as `protocols.tls.hosts.<host> = [1]` and
`generate-preload.uc` emitted `slm_preload_blocked("tls", <host>, {1})` so
`slm_is_blocked` skipped runtime strategy=1. That numeric form was profile-
dependent: in `discord-adaptive-original-pool` the strategy numbers are
renumbered contiguously from 1 (orchestrator.lua requires contiguous-from-1)
and the original strategy=1 (pass) is EXCLUDED, so runtime strategy=1 became
the WINNER (`chain-tls_multisplit_sni-70576793`) — the numeric block
accidentally blocked the winner.

The seed now stores blocks by STABLE CHAIN ID under `hosts_chain` (contract §4).
The pinned id `discord-send-syndata-tls_multisplit_sni-44860d17` is the OLD
adaptive profile's strategy-1 chain (Default old — the "pass-like" chain). It
is present in the `discord-adaptive` (2-strategy) sidecar's
`strategy_for_chain_id` (-> runtime strategy 1, Default old, harmless in
circular) and ABSENT from `discord-adaptive-original-pool` (24-strategy).
`generate-preload.uc` resolves each stable id to the runtime strategy number the
ACTIVE profile's sidecar assigns and DROPS ids whose chain is absent from the
active profile — so the block never transfers to a different chain that happens
to share the same runtime number. In the original-parity pool the pass-like
chain is absent -> the block is dropped -> the winner (runtime strategy 1) is
NOT blocked. In the 2-strategy profile the block resolves to strategy 1 (Default
old) and rotation skips it, moving to strategy 2 (Default v5).

This file is a seed note only; `blocked.json` itself is the conffile the
package installs. The set is NOT derived from autohostlist — it is the literal
pinned set, kept in lockstep with the importer (Subagent A) which reads the same
pinned source into `strategy-sources/catalog.json`'s
`default_blocked_pass_domains.domains`.
