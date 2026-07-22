# blockcheck2 → Orchestra integration (architectural note)

This note describes how `blockcheck2` is shipped in the `zapret2` APK, how it
must be run safely on a router, how its result is captured, and how — later — a
successful strategy can become an Orchestra candidate profile. It is the design
rationale behind the package-contract and static-check tests in
`tests/test_package_contract.py` (`Blockcheck2PackageContractTest`,
`Blockcheck2StaticCheckTest`).

Scope of this change: **shipping and safe static verification only.** blockcheck2
results are **diagnostic**. Orchestra does not auto-apply them. Promoting a
strategy to an active profile is a separate, explicitly-confirmed step (see
§5).

## 0. What is shipped

Source: `bol-van/zapret2` at the pinned SHA
`8a0f53f3cf2c92ddeaa66995ee63a35c1210c410` (the same SHA the Makefile pins via
`PKG_SOURCE_VERSION`; the `zapret2-core` submodule is checked out at that SHA
and is used by the source-content tests as a faithful reference for what
`PKG_BUILD_DIR` will contain).

Install tree (from `openwrt/zapret2/Makefile`):

```
/opt/zapret2/blockcheck2.sh                         # 0755, entry point (#!/bin/sh)
/opt/zapret2/blockcheck2.d/standard/*.sh            # 0755, strategy modules (sourced)
/opt/zapret2/blockcheck2.d/standard/def.inc         # 0644, sourced variables
/opt/zapret2/blockcheck2.d/custom/*.sh              # 0755, custom strategy module
/opt/zapret2/blockcheck2.d/custom/*.txt             # 0644, strategy lists + README
```

`blockcheck2.sh` derives `ZAPRET_BASE` from `dirname "$0"`. Because it ships at
`/opt/zapret2/blockcheck2.sh`, `ZAPRET_BASE` resolves to `/opt/zapret2`, so every
expected tool path resolves against the package root:

- `NFQWS2 = $ZAPRET_BASE/nfq2/nfqws2` → `/opt/zapret2/nfq2/nfqws2` (shipped)
- `MDIG  = $ZAPRET_BASE/mdig/mdig`   → `/opt/zapret2/mdig/mdig` (shipped)
- `common/*.sh` are sourced from `$ZAPRET_BASE/common/` (shipped)
- `BLOCKCHECK2D = $ZAPRET_BASE/blockcheck2.d` (shipped by this change)

The `blockcheck2.d/*.sh` modules have **no shebang** and are **sourced** by
`blockcheck2.sh` via `. "$script"` (loop at the `TESTDIR/*.sh` glob). Upstream
marks them `0644`; we install them `0755` per the package contract so each is
individually runnable for diagnostics, but they are never invoked directly —
sourcing does not require the exec bit, so this is harmless and matches the
ipset-scripts convention already used in this Makefile.

Runtime tool expectations (no new package deps were added — see Makefile
comment): `nft` and `curl` are already `DEPENDS`; `ip`, `nslookup`, `seq`,
`head`, `sort` come from the busybox base; `mdig` and `nfqws2` ship in this
package; `iptables`/`ipset` are used **only** on the legacy non-`fw4` fwtype
(fallback), not on modern nftables-based OpenWrt, so they are not hard deps.

## 1. Run blockcheck2 only after stopping legacy zapret and zapret2

blockcheck2 is not a passive observer. To test a DPI-bypass strategy it
**rewrites nftables**: it creates its own table `inet blockcheck<PID>` with
`postnat` / `prenat` / `predefrag` chains, adds `queue num $QNUM` rules to
divert traffic into `nfqws2`, and drops ICMP time-exceeded to disable IPv6
defrag during the test. While it runs, it owns a slice of the firewall and the
NFQUEUE pipeline.

If the `zapret2` service (or the legacy `zapret` service) is running at the same
time, both sides mutate nftables and compete for the same packet path. The
results are wrong (traffic is shaped by the live daemon before blockcheck sees
it) and the firewall state can be corrupted (two owners of queue rules / marks).
So the mandatory run order is:

1. Stop the legacy `zapret` service if present: `/etc/init.d/zapret stop`.
2. Stop `zapret2`: `/etc/init.d/zapret2 stop`.
3. Confirm neither is holding nftables / NFQUEUE:
   `nft list tables | grep -E 'zapret|blockcheck'` should show no live zapret
   table, and no leftover `blockcheck<PID>` table from a previous interrupted
   run (see §2).
4. Run blockcheck2: `BATCH=1 /opt/zapret2/blockcheck2.sh` (see §3 for why
   `BATCH=1`).
5. After blockcheck2 exits and its nft table is torn down, restart
   `zapret2`: `/etc/init.d/zapret2 start`.

`BATCH=1` disables the interactive `press enter to continue` prompt that
`exitp` otherwise emits, so the run is non-interactive and its stdout is a clean,
machine-parseable transcript.

This ordering is **not enforced by the package**. blockcheck2 ships as a
standalone diagnostic tool with no init/hotplug/cron wiring (the contract test
`test_blockcheck2_not_wired_into_init_hotplug_or_cron` guards this). Any
Orchestra-driven invocation must implement the stop-test-restart sequence
itself and must never run blockcheck2 while a zapret daemon is active.

## 2. Guarantee nftables cleanup on an interrupted run

blockcheck2 names its table with the process id: `NFT_TABLE=blockcheck$$`. This
makes the table unique per run and is what makes safe cleanup possible.

Normal and Ctrl-C paths both tear the table down:

- On `SIGINT` (Ctrl-C) during the main test loop, the `sigint_cleanup` trap
  fires, which calls `unprepare_all()` and then `exit 1`. For the nftables
  fwtype, `unprepare_all` runs `nft delete table inet $NFT_TABLE 2>/dev/null`.
- On a normal exit, the script clears the traps (`trap - INT/PIPE/HUP`) and
  reaches `cleanup` / `exitp 0` after printing the summary; the per-test
  `unprepare` calls have already deleted the table.

The gap is an **abrupt termination** that the trap cannot catch: `SIGKILL`,
losing the SSH/serial session (SIGHUP may be delivered to a child that does not
forward it), a power loss, or an OOM kill. In those cases the
`inet blockcheck<PID>` table is left in the kernel, still holding queue rules
that divert traffic into a dead `nfqws2` — i.e. traffic for the tested domains
stops flowing.

Residual-state hygiene (what an Orchestra wrapper must do before and after):

- **Before run:** `nft list tables | grep '^table inet blockcheck'`. If any
  `blockcheck<PID>` table exists, the previous run was interrupted. Delete it:
  `nft delete table inet blockcheck<PID>` for each. Do **not** delete any other
  table (zapret2's own table belongs to the daemon, which is stopped per §1 but
  must not be touched by blockcheck's cleanup logic).
- **After run (defensive):** re-run the same `nft list tables | grep blockcheck`
  check. A clean exit leaves nothing. If a `blockcheck<PID>` table remains,
  delete it.
- **Always** key the table name off the PID (blockcheck2 already does this); an
  Orchestra wrapper must never inject a fixed table name, or concurrent/aborted
  runs would collide.

The static check `test_all_shipped_shell_scripts_pass_syntax_check` only runs
`sh -n` — it never executes blockcheck2, never stops a service, and never
touches nftables. The contract test
`test_static_checks_never_invoke_firewall_or_real_blockcheck` (AST-based
meta-guard) enforces that the test module itself never shells out to `nft`,
`iptables`, a service manager, or the real `blockcheck2.sh`.

## 3. Result save format

blockcheck2 writes **no result file**. Its output is a human-readable transcript
on stdout. The machine-relevant lines are:

- During the run, on each successful strategy:
  `!!!!! {testfn}: working strategy found for ipv{N} {domain} : {daemon} {strategy} !!!!!`
  (e.g. `!!!!! check_domain_https_tls12: working strategy found for ipv4 rutracker.org : nfqws2 --filter-udp=443 ... !!!!!`).
- On failure: `{testfn}: {daemon} strategy for ipv{N} {domain} not found`.
- At the end, a `* SUMMARY` block (one line per recorded result:
  `{testfn} ipv{N} {domain} : {daemon} {strategy}` or `... not working`).
- For multi-domain runs, a `* COMMON` block: the intersection of strategies that
  worked for **all** domains (`result_intersection_print`). Note blockcheck2's
  own caveat: with the default scan level it skips strategies considered
  useless, so `COMMON` can miss strategies that would in fact work for all
  domains; a `force` scan level is required for a trustworthy intersection.

Capture contract for Orchestra (diagnostic-only):

- Run with `BATCH=1` and redirect stdout+stderr to a timestamped file, e.g.
  `/opt/zapret2/blockcheck2-results/<iso-ts>.log` (create the dir on first use).
  Exit code is non-zero only on hard setup failures (`exitp 5/6`); a "no
  strategy found" result is still exit 0, so the file, not the exit code, is the
  source of truth.
- Parse the `!!!!! working strategy found ...` lines into a structured
  diagnostic record. A minimal schema (JSON, diagnostic only — **not** the
  Orchestra state schema):

  ```json
  {
    "schema": "blockcheck2-diagnostic-1",
    "run_ts": "<iso-8601>",
    "pinned_sha": "8a0f53f3cf2c92ddeaa66995ee63a35c1210c410",
    "fwtype": "nftables",
    "findings": [
      {"domain": "rutracker.org", "ipv": 4, "test": "check_domain_https_tls12",
       "daemon": "nfqws2", "strategy": "--filter-udp=443 ...", "raw": "<full line>"}
    ]
  }
  ```

- Keep the raw log alongside the parsed JSON so a human can audit what
  blockcheck2 actually saw. The parsed JSON is a **diagnostic artifact**, not an
  input to Orchestra's runtime.

## 4. Converting a successful strategy to an Orchestra candidate profile

This is a **future** step. Nothing in this change auto-generates or writes an
Orchestra profile. The intended path, when we choose to build it:

1. A human reviews the diagnostic JSON from §3 and picks one or more
   `findings` to promote. blockcheck2 strategies are
   ISP/region/time-dependent; a strategy that works today may not work tomorrow,
   and one that fixes site A may break site B. Selection is a human judgment.
2. Each selected `{daemon, strategy}` is mapped to the nfqws2 argument set
   Orchestra already understands (the same args the runtime manager and
   `generate-preload.uc` consume today). This mapping must be validated, not
   assumed — blockcheck2's `strategy` string can include `PKTWS_EXTRA_*` pre/post
   splicing (`strategy_append_extra_pktws`) that is not a plain nfqws2 argv.
3. The mapped args become a **candidate** entry, written into a staging area
   (e.g. a `candidates.json` under the Orchestra state dir), conforming to the
   Orchestra state schema (`docs/orchestra-state-schema.md`). It is marked
   `candidate`, not `active`.
4. Orchestra's SLM loop may then observe the candidate's behavior in production
   before any promotion. Promotion to an active/learned seed is a separate
   confirmed action (§5).

Until steps 2–4 are implemented and tested, blockcheck2 output stays in the
diagnostic bucket only. The contract tests assert the *shipping* contract; they
do not assert any apply-path behavior, because there is none yet.

## 5. Why application must require separate confirmation

Three reasons, all load-bearing:

- **Strategies are not universally safe.** A working strategy for one blocked
  site on one ISP can break other sites (the same desync that defeats the DPI
  for site A can corrupt a TLS handshake for site B), and can stop working as
  the ISP changes equipment. Auto-applying a freshly-found strategy to a router
  that is someone's uplink can brick connectivity with no warning. Confirmation
  forces a human to own that risk.
- **blockcheck2 runs with the daemons stopped (§1).** Its result is measured in
  a cleanroom firewall state. The production router runs `zapret2` with its own
  nftables table and queue rules. A strategy that tests well in the cleanroom
  can interact badly with the live daemon's rules; the production apply must be
  a deliberate, observable step, not a side effect of running the test.
- **Auditability.** Promoting a strategy changes what traffic the router shapes
  and for which domains. That is a configuration change to a network device. It
  must be a logged, reviewable action with a known before/after — exactly what a
  separate confirmation step provides. Silently turning a diagnostic into a
  policy makes the router's behavior unattributable.

For these reasons this change deliberately stops at "ship + diagnose." Wiring
blockcheck2 results into Orchestra's apply path (§4) is explicitly left for a
follow-up that will add its own confirmation gate and tests.
