# Original Zapret2GUI Orchestra TLS Circular Pool — exact recovery

**Status:** read-only `/investigate` recovery of the EXACT original TLS circular pool from
the pinned source `youtubediscord/zapret` @ `9d57e55d6751587d9d52b52147a05a0a8fcc9fd8`
(submodule `zapret2gui/`). No production code changed to produce this document. Companion to
`docs/ORCHESTRA_PARITY_SPEC.md` (model) and `docs/ORCHESTRA_R7_CONTRACTS.md` (port contract).

## 0. Source provenance

| Item | Value |
|---|---|
| Preset | `zapret2gui/src/presets/builtin/winws2/Default (circular).txt` @ 9d57e55 (163 lines) |
| Catalog | `zapret2gui/src/profile/strategy_catalogs/winws2/tcp.txt` @ 9d57e55 (2850 lines, ~200+ chains) |
| Numbering | `zapret2gui/src/utils/circular_strategy_numbering.py` @ 9d57e55 |
| Catalog parser | `zapret2gui/src/profile/strategy_catalog.py` @ 9d57e55 |
| Original Lua | `reference/desktop-orchestra/lua/{zapret-auto,strategy-lock-manager,strategy-stats,combined-detector,learned-strategies}.lua` |
| Core nfqws2 Lua | `zapret2-core/lua/{zapret-antidpi,zapret-auto,zapret-lib}.lua` @ 8a0f53f |
| DBPD | `zapret2gui/src/orchestra/blocked_strategies_manager.py:65-102` @ 9d57e55 (59 domains) |

## 1. Pool container (block) — exact

The TLS circular pool is the SECOND `--new` block of `Default (circular).txt`, lines 97-138,
named `Все сайты (айпи)`. Exact global options of that block (verbatim, before the selector):

```
--name=Все сайты (айпи)
--filter-tcp=80,443-65535
--ipset-exclude=lists/ipset-ru.txt
--ipset-exclude=lists/ipset-dns.txt
--ipset-exclude=lists/ipset-exclude.txt
--out-range=-s9656
--in-range=-s3508
--payload=all
```

The FIRST block (lines 88-95, `Исключения (RU сайты)`) is a `pass` block for RU/dns/exclude
ipsets — NOT part of the circular pool; it is an exclusion filter that runs BEFORE the pool
block. The THIRD block (lines 140-163, `Все сайты UDP (айпи)`) is the UDP circular pool
(13 UDP strategies, `fake:blob=quic_*` — out of scope for the TLS pool).

### Selector (exact)

```
--lua-desync=circular:fails=3:retrans=3:maxseq=8192:inseq=2048:nld=3
```

Selector args: `fails=3` (3 failures → rotate to next strategy), `retrans=3` (retransmission
threshold), `maxseq=8192`, `inseq=2048` (detector window params), `nld=3` (NLD cut).

NOTE: the original uses `circular` (not `circular_quality`). The port's `orchestrator.lua`
implements `circular_quality` (a richer variant with `key`/`lock_successes`/`unlock_fails`/
`lock_tests`/`lock_rate`). The CORE `circular` (zapret-auto.lua:312) and the port
`circular_quality` (orchestrator.lua:54) use the SAME `plan_instance_pop`/`plan_instance_execute`
loop (confirmed in the prior `/investigate`), so the cutoff behavior is identical.

### DEFAULT_BLOCKED_PASS_DOMAINS

59 domains (exact set already imported into the port's `strategy-sources/default-blocked-pass-domains.json`
with provenance `src/orchestra/blocked_strategies_manager.py:65-102` @ 9d57e55). Applied at
load: strategy=1 (the `pass` strategy in the FIRST block, and the first strategy in the pool)
is blacklisted for these domains on TLS. `discord.com` IS in this set. The port's
`blocked.json` seeds `slm_preload_blocked("tls","discord.com",{1})` — verified on router.

### Rotation conditions

- Start at strategy 1 (or the host's last-tried from history).
- On `fails=3` consecutive failures (RST/retransmission detected by
  `standard_failure_detector`/`combined_failure_detector`) → rotate to next strategy
  `(nstrategy % ctstrategy) + 1`, skipping blocked strategies.
- On `lock_successes=3` (TCP) / `1` (UDP) consecutive successes → auto-LOCK that strategy
  for the host (persist to `locked_by_askey`).
- On `unlock_fails=3` failures on a locked strategy → auto-UNLOCK (re-learn).
- Blocked strategies are skipped in rotation (`slm_is_blocked` → `selected_next`).

## 2. Blobs declared by the preset (global, lines 27-86)

The preset declares these blobs at the top (global, all blocks share them). Only the blobs
referenced by the TLS pool strategies are needed for the port:

| Blob name | Source file | Port status |
|---|---|---|
| `tls_google` | `@bin/tls_clienthello_www_google_com.bin` | provided by `init_vars.lua` (`tls_mod` builtin) OR ship the .bin |
| `stun_pat` | `@bin/stun.bin` | SHIPPED (r7 `files/opt/zapret2/bin/stun.bin`) |
| `tls1` | `@bin/tls_clienthello_1.bin` | NOT shipped (need .bin OR init_vars equivalent) |
| `tls5` | `@bin/tls_clienthello_5.bin` | NOT shipped |
| `tls7` | `@bin/tls_clienthello_7.bin` | NOT shipped |
| `fake_default_tls` | builtin (nfqws2 C engine) | OK (builtin) |
| `fake_default_http` | builtin (nfqws2 C engine) | OK (builtin, used by strategy 14) |
| `fake_default_quic` | builtin | OK (UDP only) |

**Port gap:** `tls1`/`tls5`/`tls7` are referenced by pool strategies 13 (`tls7`), 16/26
(`tls5`), 20/27 (`tls5`), 21 (`tls1`). The port currently ships `init_vars.lua` which
provides `tls_google` via the `tls_mod` builtin but does NOT define `tls1`/`tls5`/`tls7`.
To run the original pool verbatim, the port must EITHER ship `bin/tls_clienthello_{1,5,7}.bin`
verbatim from the pinned preset repo OR define `tls1`/`tls5`/`tls7` in `init_vars.lua` via
`tls_mod`. The pinned repo `youtubediscord/zapret2-youtube-discord @ 4d75c70b` has
`bin/tls_clienthello_1.bin` etc. — ship them verbatim with provenance.

## 3. Required Lua init files (preset lines 5-12)

```
--lua-init=@lua/zapret-lib.lua
--lua-init=@lua/zapret-antidpi.lua
--lua-init=@lua/zapret-auto.lua
--lua-init=@lua/custom_funcs.lua
--lua-init=@lua/custom_diag.lua
--lua-init=@lua/zapret-multishake.lua
--lua-init=@lua/fakemultisplit.lua
--lua-init=@lua/fakemultidisorder.lua
```

Port mapping (OpenWrt `/opt/zapret2/lua/`):
- `zapret-lib.lua`, `zapret-antidpi.lua`, `zapret-auto.lua` — CORE (shipped by `zapret2` package r3).
- `custom_funcs.lua` — SHIPPED by r7 (defines `tls_multisplit_sni`, `hostfakesplit_multi`, etc.).
- `custom_diag.lua`, `zapret-multishake.lua`, `fakemultisplit.lua`, `fakemultidisorder.lua` —
  NOT shipped by the port. The TLS pool strategies 1-29 use `send`/`syndata`/`hostfakesplit_multi`/
  `tls_multisplit_sni`/`multisplit`/`multidisorder`/`fake`/`pktmod` — all in core +
  `custom_funcs.lua`. `fakemultisplit`/`fakemultidisorder`/`zapret-multishake` are used by
  OTHER catalog chains (tcp.txt) but NOT by the Default (circular) pool strategies 1-29.
  `custom_diag.lua` is diagnostic only. So the pool strategies 1-29 need only
  `custom_funcs.lua` (already shipped) + core. **No additional lua-init needed for the pool.**

## 4. The 29 TLS circular strategies — exact table

Source: `Default (circular).txt` lines 108-136. Each strategy = the `--lua-desync=...:strategy=N`
lines that share the same N (multiple `--lua-desync` on the same logical line share N per
`circular_strategy_numbering.py`). Order is the original file order (strategy 1 first, 29 last).

| # | Source line | Exact chain (original, nfqws2 syntax) | Required funcs | Required blobs | Has `send`? | Classification | Warnings |
|---|---|---|---|---|---|---|---|
| 1 | 108 | `send:repeats=2` + `syndata:blob=stun_pat` + `hostfakesplit_multi:hosts=google.com,vimeo.com:tcp_ts=-1000:tcp_md5:repeats=2` | send, syndata, hostfakesplit_multi | stun_pat | YES | static-only | VOLUNTARY_CUTOFF: send cutoffs syndata + hostfakesplit_multi inside circular (proven on router) |
| 2 | 109 | `hostfakesplit_multi:hosts=google.com,vimeo.com:tcp_ts=-1000:tcp_md5:repeats=2` | hostfakesplit_multi | (none) | NO | circular-compatible | requires custom_funcs.lua (shipped) |
| 3 | 110 | `tls_multisplit_sni:seqovl=652:seqovl_pattern=tls_google` | tls_multisplit_sni | tls_google | NO | circular-compatible | SEQOVL_MAY_CANCEL: seqovl=652 >= SNI pos ~122 for discord.com (proven: Default old FAILED with this) |
| 4 | 111 | `send:repeats=2` + `syndata:blob=stun_pat` + `tls_multisplit_sni:seqovl=652:seqovl_pattern=tls_google` | send, syndata, tls_multisplit_sni | stun_pat, tls_google | YES | static-only | VOLUNTARY_CUTOFF + SEQOVL_MAY_CANCEL |
| 5 | 112 | `multisplit:pos=1:seqovl=740:seqovl_pattern=stun_pat` | multisplit | stun_pat | NO | circular-compatible | SEQOVL_MAY_CANCEL: seqovl=740 large |
| 6 | 113 | `send:repeats=2` + `syndata:blob=stun_pat` + `multisplit:pos=1:seqovl=740:seqovl_pattern=stun_pat` | send, syndata, multisplit | stun_pat | YES | static-only | VOLUNTARY_CUTOFF |
| 7 | 114 | `send:repeats=2` + `syndata:blob=tls_google` + `pktmod:tcp_flags_unset=ack` | send, syndata, pktmod | tls_google | YES | static-only | VOLUNTARY_CUTOFF |
| 8 | 115 | `fake:blob=fake_default_tls:tls_mod=rnd,dupsid,sni=www.google.com:repeats=8:tcp_seq=1000` | fake | fake_default_tls (builtin) | NO | circular-compatible | (fake may set cutoff — verify on router via probe-pool) |
| 9 | 116 | `multisplit:pos=2:seqovl=681:seqovl_pattern=tls_google:repeats=8` | multisplit | tls_google | NO | circular-compatible | SEQOVL_MAY_CANCEL: seqovl=681 large |
| 10 | 117 | `fake:blob=fake_default_tls:tls_mod=rnd,dupsid,sni=www.google.com:repeats=8:tcp_seq=1000` + `multisplit:pos=2:seqovl=681:seqovl_pattern=tls_google:repeats=8` | fake, multisplit | fake_default_tls, tls_google | NO | circular-compatible | fake→multisplit ordering: verify fake does NOT cutoff multisplit on router |
| 11 | 118 | `multisplit:pos=host` | multisplit | (none) | NO | circular-compatible | pos=host (SNI host marker) |
| 12 | 119 | `multidisorder:pos=1,host+2,sld+2,sld+5,sniext+1,sniext+2,endhost-2:seqovl=1` | multidisorder | (none) | NO | circular-compatible | seqovl=1 safe (< 122) |
| 13 | 120 | `multisplit:pos=2,midsld-2:seqovl=1:seqovl_pattern=tls7` | multisplit | tls7 | NO | circular-compatible | seqovl=1 safe; requires tls7 blob (NOT shipped — gap) |
| 14 | 121 | `fake:blob=fake_default_http:repeats=4:ip_autottl=2,3-20:ip6_autottl=2,3-20:tcp_md5` | fake | fake_default_http (builtin) | NO | circular-compatible | HTTP fake blob (builtin); verify on router |
| 15 | 122 | `multidisorder:pos=host+1` | multidisorder | (none) | NO | circular-compatible | |
| 16 | 123 | `multisplit:pos=2:seqovl=211:seqovl_pattern=tls5` | multisplit | tls5 | NO | circular-compatible | SEQOVL_MAY_CANCEL: 211 > 122; requires tls5 (NOT shipped — gap) |
| 17 | 124 | `multisplit:pos=2:seqovl=652:seqovl_pattern=tls_google` | multisplit | tls_google | NO | circular-compatible | SEQOVL_MAY_CANCEL: 652 >> 122 |
| 18 | 125 | `multisplit:pos=10:seqovl=625:seqovl_pattern=tls5` | multisplit | tls5 | NO | circular-compatible | SEQOVL_MAY_CANCEL; requires tls5 (gap) |
| 19 | 126 | `multisplit:pos=10:seqovl=700:seqovl_pattern=tls_google` | multisplit | tls_google | NO | circular-compatible | SEQOVL_MAY_CANCEL: 700 >> 122 |
| 20 | 127 | `multisplit:pos=234:seqovl=700:seqovl_pattern=tls5` | multisplit | tls5 | NO | circular-compatible | SEQOVL_MAY_CANCEL; pos=234 (absolute); requires tls5 (gap) |
| 21 | 128 | `multisplit:pos=234:seqovl=700:seqovl_pattern=tls1` | multisplit | tls1 | NO | circular-compatible | SEQOVL_MAY_CANCEL; requires tls1 (gap) |
| 22 | 129 | `multisplit:pos=234:seqovl=1000:seqovl_pattern=tls_google` | multisplit | tls_google | NO | circular-compatible | SEQOVL_MAY_CANCEL: 1000 >> 122 |
| 23 | 130 | `multisplit:seqovl=500:seqovl_pattern=tls_google` | multisplit | tls_google | NO | circular-compatible | SEQOVL_MAY_CANCEL: 500 > 122 |
| 24 | 131 | `multisplit:seqovl=650:seqovl_pattern=tls_google` | multisplit | tls_google | NO | circular-compatible | SEQOVL_MAY_CANCEL: 650 >> 122 |
| 25 | 132 | `multisplit:seqovl=800:seqovl_pattern=tls_google` | multisplit | tls_google | NO | circular-compatible | SEQOVL_MAY_CANCEL: 800 >> 122 |
| 26 | 133 | `multisplit:seqovl=211:seqovl_pattern=tls5` | multisplit | tls5 | NO | circular-compatible | SEQOVL_MAY_CANCEL; requires tls5 (gap) |
| 27 | 134 | `multisplit:seqovl=625:seqovl_pattern=tls5` | multisplit | tls5 | NO | circular-compatible | SEQOVL_MAY_CANCEL; requires tls5 (gap) |
| 28 | 135 | `multisplit:seqovl=625:seqovl_pattern=tls_google` | multisplit | tls_google | NO | circular-compatible | SEQOVL_MAY_CANCEL: 625 >> 122 |
| 29 | 136 | `multisplit:seqovl=700:seqovl_pattern=tls_google` | multisplit | tls_google | NO | circular-compatible | SEQOVL_MAY_CANCEL: 700 >> 122 |

## 5. Classification summary

| Class | Count | Strategies | Meaning |
|---|---|---|---|
| **circular-compatible** | 25 | 2, 3, 5, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29 | No `send` (the only proven voluntary-cutoff tripper for later steps). All steps are reachable inside `plan_instance_execute`. Candidates for the adaptive circular pool. |
| **static-only** | 4 | 1, 4, 6, 7 | Contain `send` BEFORE later steps (`syndata`/`hostfakesplit_multi`/`tls_multisplit_sni`/`multisplit`/`pktmod`). `send` sets a voluntary cutoff (proven on router: `send_1_5` → `syndata_1_6/1_7` "not calling because of voluntary cutoff"). Later steps are UNREACHABLE inside circular → only the first step (`send`) executes. Work as STATIC (direct, no circular) but NOT as circular pool candidates. |
| **incompatible** | 0 | (none) | All required functions exist: `send`/`syndata`/`multisplit`/`multidisorder`/`fake`/`pktmod` = core; `hostfakesplit_multi`/`tls_multisplit_sni` = `custom_funcs.lua` (shipped). |

### Blob-shipping gaps (blockers for verbatim original pool)

Strategies 13, 16, 18, 20, 21, 26, 27 require `tls1`/`tls5`/`tls7` blobs which the port
does NOT ship. To include these 7 strategies verbatim, ship `bin/tls_clienthello_{1,5,7}.bin`
from the pinned preset repo (`youtubediscord/zapret2-youtube-discord @ 4d75c70b/bin/`) with
provenance, OR define `tls1`/`tls5`/`tls7` in `init_vars.lua` via `tls_mod`. The remaining
18 circular-compatible strategies (2, 3, 5, 8, 9, 10, 11, 12, 14, 15, 17, 19, 22, 23, 24,
25, 28, 29) use only `tls_google`/`stun_pat`/`fake_default_tls`/`fake_default_http`/none —
all available (tls_google via init_vars, stun_pat shipped, fakes builtin).

### SEQOVL_MAY_CANCEL analysis

`seqovl` cancels when `seqovl >= pos[1]-1` (the first split position minus 1). For
`discord.com` the SNI is at ~byte 122, so:
- `seqovl=1` (strategies 12, 13) → SAFE (< 122).
- `seqovl=211` (16, 26) → MAY CANCEL (211 > 122).
- `seqovl=500`/`625`/`650`/`652`/`681`/`700`/`740`/`800`/`1000` → WILL CANCEL for
  short-SNI domains like discord.com (proven: Default old strategy with seqovl=652 failed
  with "seqovl cancelled, too large").

NOTE: `seqovl` cancel depends on whether `pos` is an SNI-relative marker (`host`, `sniext`,
`midsld`) or an absolute byte offset. For `multisplit:pos=N:seqovl=M`, the cancel condition
is `seqovl >= pos-1`. `pos=1` → cancel if `seqovl >= 0` (always? — needs core verification).
`pos=2` → cancel if `seqovl >= 1`. `pos=host`/`pos=10`/`pos=234` → different. The
`probe-pool` (Step 4) will determine empirically which strategies actually bypass Discord
on the router, regardless of the static seqovl prediction.

## 6. Default v5 (discord-v5) classification

The `Default v5` Discord block (`youtubediscord/zapret2-youtube-discord @ 4d75c70b`,
`presets/Default v5.txt` lines 129-135) chain:
```
send:repeats=3 + syndata:blob=tls_google + syndata
```

Classification:
- **static-compatible**: YES — works as a static (direct, no circular) nfqws2 profile
  (proven: discord.com 200 3/3 on router).
- **circular-compatible**: NO — contains `send` before `syndata`; `send` sets voluntary
  cutoff → `syndata` unreachable inside circular (proven on router: adaptive profile
  strategy=2 = this chain, `syndata_1_6/1_7` cutoff).
- **static-only**: YES — use ONLY as a static fallback profile (`discord-v5.opt`), NOT as
  a circular pool candidate.
- **incompatible**: NO — `send`/`syndata` are core, `tls_google` via `init_vars.lua`.

Per task §8: keep `discord-v5.opt` as the emergency/manual fallback (it works statically);
do NOT use it as adaptive PASS proof; do NOT put it in the circular pool without a
source-backed conversion that removes the cutoff (which would mean rewriting the upstream
strategy — forbidden without provenance).

## 7. OpenWrt path mapping for the original pool

To generate a port profile from the original pool, map the original paths to OpenWrt:

| Original (preset) | OpenWrt (port) |
|---|---|
| `@lua/zapret-lib.lua` | `/opt/zapret2/lua/zapret-lib.lua` (core, zapret2 pkg) |
| `@lua/zapret-antidpi.lua` | `/opt/zapret2/lua/zapret-antidpi.lua` (core) |
| `@lua/zapret-auto.lua` | `/opt/zapret2/lua/zapret-auto.lua` (core) |
| `@lua/custom_funcs.lua` | `/opt/zapret2/lua/custom_funcs.lua` (r7 shipped) |
| `@bin/stun.bin` | `/opt/zapret2/bin/stun.bin` (r7 shipped) |
| `@bin/tls_clienthello_www_google_com.bin` | `init_vars.lua` provides `tls_google` via `tls_mod` builtin |
| `@bin/tls_clienthello_{1,5,7}.bin` | NOT shipped (gap — ship verbatim or define via tls_mod) |
| `lists/ipset-ru.txt` etc. | not needed for Discord-only pool (the pool filters by ipset-exclude; for a Discord-only adaptive profile use `--ipset=ipset-discord.txt` instead) |
| `--filter-tcp=80,443-65535` | `--filter-tcp=80,443,1080,2053,2083,2087,2096,8443` (Discord ports) or keep original |
| `--payload=all` | keep `--payload=all` (original) OR `--payload=tls_client_hello` (port adaptive uses tls_client_hello — but original uses `all` so HTTP strategies like 14 work) |

The original `--out-range=-s9656 --in-range=-s3508` are nfqws2 sequence-range filters; keep
them verbatim (they gate which packets the desync applies to).

## 8. Chain boundaries + numbering rule

Per `circular_strategy_numbering.py` (confirmed in spec §1.3):
- The `circular` selector line is NOT numbered.
- Each subsequent `--lua-desync=` gets `:strategy=N`.
- N increments per chain, resets on `--new` and on a new `--payload=` group.
- **Multiple `--lua-desync=` on the same logical line share the same N** — that is exactly
  "one strategy chain = several desync functions executed together" (e.g. strategy 1 =
  `send` + `syndata` + `hostfakesplit_multi` on line 108).

The original pool strategies 1-29 are contiguous from 1 (no gaps), as required by
`circular_quality: strategies must be contiguous from 1` (orchestrator.lua:23).

## 9. Suitability inside circular — verdict

**The original TLS circular pool has 25 circular-compatible strategies** (no `send`). The
port r7 adaptive profile used only 2 strategies, BOTH `send`-based (Default old strategy=1
+ Default v5 strategy=2) → both static-only → no working strategy in the pool. The fix is
to regenerate the adaptive pool from the original 25 circular-compatible strategies
(excluding the 4 static-only `send` strategies 1, 4, 6, 7), preserving original order and
numbering, and let the Orchestra rotate + auto-lock whichever one bypasses Discord on the
target ISP. The `probe-pool` (Step 4) will test each statically on the router to find the
winner(s) before the full Orchestra scenarios A-G.

NO UNRESOLVED DECISIONS
