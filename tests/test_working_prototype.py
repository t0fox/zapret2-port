"""Tests for the zapret2-orchestra working prototype.

Covers the ready nfqws2 profiles, the profile-selection CLI
(``zapret2-orchestra-profile``), and the batch blockcheck wrapper
(``zapret2-orchestra-blockcheck``).

Design rules (from the prototype spec):
  * Profiles ship only nfqws2-relevant params (no Windows interception,
    no WinDivert, no .bat/.cmd, no @bin/@lua GUI asset refs).
  * Every profile references ``circular_quality`` (the Orchestra runtime
    marker the existing validator requires) and uses only strategies/options
    defined in the pinned zapret2-core Lua or the Orchestra extension.
  * The CLIs never ``eval``/``source`` profile content and never interpolate
    user text into a shell string.
  * Tests never run the real nft / blockcheck2 / nfqws2 / init scripts. Shell
    behavior is exercised with a temp PATH of mock commands and path env
    overrides; static checks are pure text/regex.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Reuse the NFQWS2_OPT parser oracle (the spec apply.uc must mirror).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _nfqws2_parser as P  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "openwrt" / "zapret2-orchestra"
SHARE_FILES = PACKAGE / "files/usr/share/zapret2-orchestra"
PROFILES_DIR = SHARE_FILES / "profiles"
MANIFEST = SHARE_FILES / "profiles.tsv"
PROFILE_CLI = PACKAGE / "files/usr/sbin/zapret2-orchestra-profile"
BLOCKCHECK_CLI = PACKAGE / "files/usr/sbin/zapret2-orchestra-blockcheck"
APPLY_WRAPPER = PACKAGE / "files/usr/sbin/zapret2-orchestra-apply"
MAKEFILE = PACKAGE / "Makefile"
# Pinned upstream source (zapret2-core submodule) = what PKG_BUILD_DIR contains.
UPSTREAM = ROOT / "zapret2-core"
ORCH_LUA = PACKAGE / "files/opt/zapret2/lua/orchestra-extra"

# ---------------------------------------------------------------------------
# nfqws2 option/strategy/arg contract — derived from the pinned source.
#   * --options: from the nfqws2 --help text (zapret2-core/nfq2/nfqws.c).
#   * strategy names: `function <name>(` in the pinned Lua (zapret-antidpi,
#     zapret-auto) plus the Orchestra extension (circular_quality, detectors).
#   * strategy args: parameter keys read via `desync.arg.<key>` in the pinned
#     Lua, plus the circular/circular_quality selector args.
#   * pos markers: protocol.c posmarker_names[].
# ---------------------------------------------------------------------------

KNOWN_OPTIONS = {
    # multi-strategy / profile
    "--new", "--name", "--skip", "--template", "--import", "--cookie",
    # filters
    "--filter-l3", "--filter-tcp", "--filter-udp", "--filter-icmp",
    "--filter-ipp", "--filter-l7", "--filter-ssid",
    # ipset / hostlist
    "--ipset", "--ipset-ip", "--ipset-exclude", "--ipset-exclude-ip",
    "--hostlist", "--hostlist-domains", "--hostlist-exclude",
    "--hostlist-exclude-domains", "--hostlist-auto",
    "--hostlist-auto-fail-threshold", "--hostlist-auto-fail-time",
    "--hostlist-auto-retrans-threshold", "--hostlist-auto-retrans-maxseq",
    "--hostlist-auto-retrans-reset", "--hostlist-auto-incoming-maxseq",
    "--hostlist-auto-udp-out", "--hostlist-auto-udp-in",
    "--hostlist-auto-debug",
    # lua pass mode / desync
    "--payload", "--out-range", "--in-range", "--lua-desync",
    # global init
    "--ctrack-disable", "--ctrack-timeouts", "--ipcache-lifetime",
    "--ipcache-hostname", "--reasm-disable", "--payload-disable",
    "--blob", "--lua-init", "--lua-gc", "--writable", "--server",
}

# Strategy function names defined in pinned core Lua + Orchestra extension.
# Built by scanning `^function <name>(` over the relevant Lua files at import.
def _collect_lua_functions() -> set[str]:
    names: set[str] = set()
    globs = [
        UPSTREAM / "lua/zapret-antidpi.lua",
        UPSTREAM / "lua/zapret-auto.lua",
        UPSTREAM / "lua/zapret-lib.lua",
        UPSTREAM / "lua/zapret-obfs.lua",
    ]
    for g in globs:
        if g.is_file():
            names.update(re.findall(r"^function ([a-z_]+)\(", g.read_text("utf-8"), re.M))
    for g in sorted(ORCH_LUA.glob("*.lua")):
        names.update(re.findall(r"^function ([a-z_]+)\(", g.read_text("utf-8"), re.M))
    return names


KNOWN_STRATEGIES = _collect_lua_functions()

# Strategy parameter keys. Derived authoritatively from the pinned Lua
# (every `desync.arg.<key>` and `fooling_options.<key>` the source reads) so the
# contract tracks the source, plus the circular/circular_quality selector keys
# and standard rawsend/fooling keys that are not read via those two prefixes.
def _collect_strategy_args() -> set[str]:
    keys: set[str] = set()
    for g in [
        UPSTREAM / "lua/zapret-antidpi.lua",
        UPSTREAM / "lua/zapret-auto.lua",
        UPSTREAM / "lua/zapret-lib.lua",
        UPSTREAM / "lua/zapret-obfs.lua",
    ]:
        if not g.is_file():
            continue
        txt = g.read_text("utf-8")
        keys.update(re.findall(r"desync\.arg\.([a-z_]+)", txt))
        keys.update(re.findall(r"fooling_options\.([a-z_]+)", txt))
    # standard rawsend/fooling/direction keys read via other paths, plus the
    # circular / circular_quality selector keys (read as desync.arg.<key> in
    # orchestrator.lua, so already collected, but listed explicitly for clarity).
    keys |= {
        "fooling", "rawsend", "reconstruct", "ipfrag", "direction", "repeats",
        "fwmark", "ip_ttl", "ip_id",
        "key", "fails", "time", "retrans", "nld", "inseq",
        "failure_detector", "success_detector",
        "lock_successes", "unlock_fails", "lock_tests", "lock_rate",
        "strategy",
    }
    return keys


KNOWN_STRATEGY_ARGS = _collect_strategy_args()

# pos markers (protocol.c posmarker_names[]), with optional +/-N offsets.
KNOWN_POS_MARKERS = {"abs", "host", "endhost", "sld", "midsld", "endsld",
                     "method", "extlen", "sniext"}

FORBIDDEN_PROFILE_PATTERNS = [
    r"--wf-",                # WinDivert filters (Windows interception)
    r"WinDivert", r"windivert",
    r"[A-Za-z]:\\",          # Windows drive paths
    r"\.bat\b", r"\.cmd\b",  # Windows batch
    r"@bin/",                # GUI bin asset refs
    r"@lua/",                # GUI lua asset refs
]


def _opt_files() -> list[Path]:
    return sorted(PROFILES_DIR.glob("*.opt"))


def _manifest_ids() -> list[str]:
    ids: list[str] = []
    if MANIFEST.is_file():
        for line in MANIFEST.read_text("utf-8").splitlines():
            if not line.strip():
                continue
            pid = line.split("\t")[0]
            if pid == "id":
                continue
            ids.append(pid)
    return ids


def _argv_preserves_newline() -> bool:
    """Probe whether an embedded newline survives argv as a single argument.

    On Windows/Git-Bash the command-line layer splits a newline-containing
    argument into separate argv entries, so a ``run "foo\\nbar"`` invocation
    never delivers a newline to the script. On Linux (the CI runner) argv
    preserves it. Runtime newline-rejection tests that depend on delivering a
    newline through argv use this to skip on platforms that mangle it; the
    static no-$'\\n' contract test runs everywhere regardless.
    """
    if not shutil.which("sh"):
        return False
    probe = "foo\nbar"
    r = subprocess.run(
        ["sh", "-c", 'printf "%s" "$1"', "_", probe],
        capture_output=True, text=True, timeout=5,
    )
    return r.returncode == 0 and r.stdout == probe


# ===========================================================================
# Profile content + manifest + contract tests
# ===========================================================================

class ProfileSetTest(unittest.TestCase):
    """The ready profiles and their TSV manifest are consistent and clean."""

    def test_at_least_six_opt_profiles(self) -> None:
        opts = _opt_files()
        self.assertGreaterEqual(len(opts), 6, f"need >=6 profiles, got {len(opts)}: {opts}")

    def test_unique_profile_ids(self) -> None:
        ids = [p.stem for p in _opt_files()]
        self.assertEqual(len(ids), len(set(ids)), f"duplicate profile ids: {ids}")

    def test_every_manifest_id_has_opt(self) -> None:
        for pid in _manifest_ids():
            self.assertTrue((PROFILES_DIR / f"{pid}.opt").is_file(),
                            f"manifest id '{pid}' has no .opt file")

    def test_every_opt_listed_in_manifest(self) -> None:
        manifest = set(_manifest_ids())
        for p in _opt_files():
            self.assertIn(p.stem, manifest, f"{p.name} not listed in manifest")

    def test_manifest_ids_unique(self) -> None:
        ids = _manifest_ids()
        self.assertEqual(len(ids), len(set(ids)), f"duplicate manifest ids: {ids}")

    def test_manifest_is_real_tsv_with_tabs(self) -> None:
        self.assertTrue(MANIFEST.is_file(), "profiles.tsv missing")
        for line in MANIFEST.read_text("utf-8").splitlines():
            if not line.strip() or line.startswith("id\t"):
                continue
            self.assertIn("\t", line, f"manifest line not tab-separated: {line!r}")
            self.assertGreaterEqual(len(line.split("\t")), 4,
                                    f"manifest needs >=4 fields: {line!r}")


class ProfileCleanlinessTest(unittest.TestCase):
    """Profiles contain no Windows interception or GUI-only asset references."""

    def _values(self) -> list[tuple[str, str]]:
        out = []
        for p in _opt_files():
            try:
                v = P.extract(p.read_text("utf-8")).value
            except P.ParseError as e:
                self.fail(f"{p.name} parse error: {e}")
            out.append((p.name, v))
        return out

    def test_no_forbidden_patterns(self) -> None:
        for name, val in self._values():
            for pat in FORBIDDEN_PROFILE_PATTERNS:
                self.assertIsNone(re.search(pat, val),
                                  f"{name}: forbidden pattern {pat} in profile")

    def test_every_profile_references_circular_quality(self) -> None:
        # The existing validator (apply.uc validate_profile) requires this.
        for name, val in self._values():
            self.assertIn("circular_quality", val,
                          f"{name}: must reference circular_quality (validator requirement)")

    def test_every_profile_passes_profile_value_ok(self) -> None:
        # Mirror apply.uc profile_value_ok: reject NUL/CR/$()/`/;/|/&& and
        # unclosed quotes. < and > are allowed (<HOSTLIST> placeholders).
        for name, val in self._values():
            self.assertNotIn("\x00", val, f"{name}: NUL")
            self.assertNotIn("\r", val, f"{name}: CR")
            self.assertNotIn("$(", val, f"{name}: command substitution")
            self.assertNotIn("`", val, f"{name}: backtick")
            self.assertNotIn(";", val, f"{name}: ;")
            self.assertNotIn("|", val, f"{name}: |")
            self.assertNotIn("&&", val, f"{name}: &&")
            self.assertEqual(val.count('"') % 2, 0, f"{name}: unclosed double quote")
            self.assertEqual(val.count("'") % 2, 0, f"{name}: unclosed single quote")

    def test_profiles_parse_as_valid_shell_assignment(self) -> None:
        # The runtime sh -n's the whole config; each profile is an
        # NFQWS2_OPT="..." assignment that must be valid shell syntax.
        if not shutil.which("sh"):
            self.skipTest("sh not on PATH")
        for p in _opt_files():
            r = subprocess.run(["sh", "-n", str(p)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{p.name}: sh -n failed: {r.stderr}")


class ProfileContractTest(unittest.TestCase):
    """Profiles use only nfqws2 options/strategies/args known to the pinned
    source, and reference only assets that exist in the package source tree."""

    def _desync_terms(self, val: str) -> list[tuple[str, str]]:
        """Return (strategy_name, raw_arg_blob) for each --lua-desync= in val."""
        terms = []
        for m in re.finditer(r"--lua-desync=([^\s]+)", val):
            chunk = m.group(1)
            parts = chunk.split(":", 1)
            terms.append((parts[0], parts[1] if len(parts) > 1 else ""))
        return terms

    def _arg_keys(self, arg_blob: str) -> list[str]:
        # arg blob is colon-separated key=val; first token may be a bare value
        # (e.g. pos marker list). Collect the key= keys.
        keys = []
        for tok in arg_blob.split(":"):
            if "=" in tok:
                keys.append(tok.split("=", 1)[0])
        return keys

    def test_only_known_options(self) -> None:
        for p in _opt_files():
            val = P.extract(p.read_text("utf-8")).value
            opts = set(re.findall(r"(--[a-z][a-z0-9-]*)=", val))
            opts |= set(re.findall(r"(--[a-z][a-z0-9-]*)\s", val))
            # bare --new (no =, may be line-end)
            opts |= set(re.findall(r"(--[a-z][a-z0-9-]*)(?:\s|$)", val))
            unknown = opts - KNOWN_OPTIONS
            self.assertFalse(unknown, f"{p.name}: unknown nfqws2 options: {unknown}")

    def test_only_known_strategies(self) -> None:
        for p in _opt_files():
            val = P.extract(p.read_text("utf-8")).value
            strategies = {name for name, _ in self._desync_terms(val)}
            unknown = strategies - KNOWN_STRATEGIES
            self.assertFalse(unknown, f"{p.name}: unknown lua-desync strategies: {unknown}")

    def test_only_known_strategy_args(self) -> None:
        for p in _opt_files():
            val = P.extract(p.read_text("utf-8")).value
            keys: set[str] = set()
            for _, blob in self._desync_terms(val):
                keys.update(self._arg_keys(blob))
            unknown = keys - KNOWN_STRATEGY_ARGS
            self.assertFalse(unknown, f"{p.name}: unknown strategy args: {unknown}")

    def test_strategy_functions_defined_in_pinned_lua_or_extension(self) -> None:
        # Test 7: every strategy referenced is defined in pinned core or
        # Orchestra extension Lua.
        for p in _opt_files():
            val = P.extract(p.read_text("utf-8")).value
            strategies = {name for name, _ in self._desync_terms(val)}
            for s in strategies:
                self.assertIn(s, KNOWN_STRATEGIES,
                              f"{p.name}: strategy '{s}' not defined in pinned Lua/extension")

    def test_opt_zapret2_file_assets_exist_in_package_source(self) -> None:
        # Test 6: any /opt/zapret2/files/.../X.bin or /opt/zapret2/lua/X.lua
        # reference must point at a file present in the pinned source tree
        # (which is what the package installs from PKG_BUILD_DIR).
        for p in _opt_files():
            val = P.extract(p.read_text("utf-8")).value
            for m in re.finditer(r"/opt/zapret2/files/fake/([^\s\"']+)", val):
                self.assertTrue((UPSTREAM / "files/fake" / m.group(1)).is_file(),
                                f"{p.name}: missing fake asset {m.group(1)}")
            for m in re.finditer(r"/opt/zapret2/lua/([^\s\"']+\.lua)", val):
                # Either pinned core lua (at /opt/zapret2/lua/<f>) or the
                # Orchestra extension (at /opt/zapret2/lua/orchestra-extra/<f>).
                # An "orchestra-extra/<f>" reference resolves under ORCH_LUA
                # with the dir prefix stripped.
                rel = m.group(1)
                orch_rel = rel.removeprefix("orchestra-extra/")
                self.assertTrue(
                    (UPSTREAM / "lua" / rel).is_file()
                    or (ORCH_LUA / orch_rel).is_file(),
                    f"{p.name}: missing lua asset {rel}",
                )

    def test_pos_markers_known(self) -> None:
        # pos= and midhost= markers must be in protocol.c posmarker_names[].
        for p in _opt_files():
            val = P.extract(p.read_text("utf-8")).value
            for m in re.finditer(r"(?:pos|midhost)=([^\s:]+)", val):
                for tok in m.group(1).split(","):
                    base = re.sub(r"[+\-][0-9]+$", "", tok)
                    if base and not base.lstrip("-").isdigit():
                        self.assertIn(base, KNOWN_POS_MARKERS,
                                      f"{p.name}: unknown pos marker '{base}'")


# ===========================================================================
# Makefile install rules for the new artifacts
# ===========================================================================

class MakefileInstallTest(unittest.TestCase):
    """The orchestra Makefile installs the new CLI scripts and the manifest."""

    def setUp(self) -> None:
        self.body = MAKEFILE.read_text("utf-8")

    def test_profiles_tsv_installed_data(self) -> None:
        self.assertIn(
            "$(INSTALL_DATA) $(CURDIR)/files/usr/share/zapret2-orchestra/profiles.tsv "
            "$(1)/usr/share/zapret2-orchestra/profiles.tsv",
            self.body,
        )

    def test_profile_cli_installed_bin(self) -> None:
        self.assertIn(
            "$(INSTALL_BIN) $(CURDIR)/files/usr/sbin/zapret2-orchestra-profile "
            "$(1)/usr/sbin/zapret2-orchestra-profile",
            self.body,
        )

    def test_blockcheck_cli_installed_bin(self) -> None:
        self.assertIn(
            "$(INSTALL_BIN) $(CURDIR)/files/usr/sbin/zapret2-orchestra-blockcheck "
            "$(1)/usr/sbin/zapret2-orchestra-blockcheck",
            self.body,
        )

    def test_release_number_is_six(self) -> None:
        # zapret2-orchestra PKG_RELEASE is bumped to 6 for the shipped-CLI
        # argv-sentinel fix in apply.uc (r6). r3/r5 was the executable profile
        # set + blockcheck batch CLI release.
        self.assertRegex(self.body, r"(?m)^PKG_RELEASE:=6$")

    def test_new_opt_picked_up_by_existing_wildcard(self) -> None:
        self.assertIn(
            "$(INSTALL_DATA) $(CURDIR)/files/usr/share/zapret2-orchestra/profiles/*.opt "
            "$(1)/usr/share/zapret2-orchestra/profiles/",
            self.body,
        )


# ===========================================================================
# Profile CLI smoke tests (mock apply wrapper, temp PATH)
# ===========================================================================

class ProfileCliSmokeTest(unittest.TestCase):
    """Exercises zapret2-orchestra-profile with a mock apply and temp dirs.
    Never runs the real ucode manager."""

    @classmethod
    def setUpClass(cls) -> None:
        if not shutil.which("sh"):
            raise unittest.SkipTest("sh not on PATH")

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="orch-profile-cli-")
        self.bindir = Path(self.tmp) / "bin"
        self.bindir.mkdir()
        self.user_profiles = Path(self.tmp) / "user-profiles"
        self.user_profiles.mkdir()
        # Mock apply wrapper: record argv, emit JSON, exit 0; an enable/validate
        # for the sentinel "FAILME" exits 1 so we can observe validation gates.
        apply = self.bindir / "zapret2-orchestra-apply"
        apply.write_text(
            "#!/bin/sh\n"
            "echo \"APPLY $*\" >&2\n"
            "case \"$1\" in\n"
            "  validate-profile) [ \"$2\" = FAILME ] && { echo '{\"ok\":false}' ; exit 1 ; } ; "
            "echo '{\"ok\":true,\"profile\":\"'\"$2\"'\",\"problems\":[]}' ; exit 0 ;;\n"
            "  enable) echo '{\"ok\":true,\"command\":\"enable\",\"profile\":\"'\"$2\"'\"}' ; exit 0 ;;\n"
            "  status) echo '{\"ok\":true,\"command\":\"status\"}' ; exit 0 ;;\n"
            "  disable) echo '{\"ok\":true,\"command\":\"disable\"}' ; exit 0 ;;\n"
            "  *) echo \"apply-unknown $1\" >&2 ; exit 2 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        apply.chmod(0o755)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self) -> dict:
        env = os.environ.copy()
        env["PATH"] = f"{self.bindir}{os.pathsep}{env.get('PATH', '')}"
        env["ZAPRET2_SHARE_DIR"] = str(SHARE_FILES)
        env["ZAPRET2_BUILTIN_PROFILES_DIR"] = str(PROFILES_DIR)
        env["ZAPRET2_USER_PROFILES_DIR"] = str(self.user_profiles)
        env["ZAPRET2_APPLY"] = str(self.bindir / "zapret2-orchestra-apply")
        return env

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", str(PROFILE_CLI), *args],
            env=self._env(), capture_output=True, text=True, timeout=10,
        )

    def test_list_outputs_all_profiles(self) -> None:
        r = self._run("list")
        self.assertEqual(r.returncode, 0, r.stderr)
        ids = [line.split("\t")[0] for line in r.stdout.splitlines() if line.strip()]
        self.assertCountEqual(ids, [p.stem for p in _opt_files()])

    def test_show_valid_prints_opt_without_executing(self) -> None:
        pid = "gui-tls-multisplit"
        r = self._run("show", pid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("NFQWS2_OPT=", r.stdout)
        self.assertIn("--lua-desync=multisplit", r.stdout)
        # The mock apply must NOT have been called for show.
        self.assertNotIn("APPLY", r.stderr)

    def test_show_invalid_id_rejected(self) -> None:
        for bad in ("bad/id", "with space", "with;semi", "with|pipe", "with&amp",
                    "with`tick", "with$(x)", "..", ".hidden"):
            r = self._run("show", bad)
            self.assertNotEqual(r.returncode, 0, f"show {bad!r} should fail")
            self.assertNotIn("APPLY", r.stderr, f"show {bad!r} leaked to apply")

    def test_show_traversal_rejected(self) -> None:
        r = self._run("show", "..", "etc", "passwd")
        self.assertNotEqual(r.returncode, 0, r.stderr)
        # Must not have read a file outside the profiles dir.
        self.assertNotIn("root:", r.stdout)

    def test_show_nonexistent_profile(self) -> None:
        r = self._run("show", "does-not-exist")
        self.assertNotEqual(r.returncode, 0, r.stderr)

    # --- B4: leading-dash IDs are rejected as profile IDs -------------------

    def test_show_rejects_leading_dash_ids(self) -> None:
        # IDs starting with '-' (option-like: -e, --help, -foo) must be rejected
        # at the id-validation layer, never reaching profile resolution.
        for bad in ("-e", "--help", "-foo"):
            r = self._run("show", bad)
            self.assertNotEqual(r.returncode, 0, f"show {bad!r} should fail")
            self.assertIn("invalid id", r.stderr,
                          f"show {bad!r} should report invalid id: {r.stderr!r}")
            self.assertNotIn("APPLY", r.stderr, f"show {bad!r} leaked to apply")

    def test_validate_rejects_leading_dash_ids_without_invoking_apply(self) -> None:
        # A rejected ID must NOT dispatch to the apply manager.
        for bad in ("-e", "--help", "-foo"):
            r = self._run("validate", bad)
            self.assertNotEqual(r.returncode, 0, f"validate {bad!r} should fail")
            self.assertIn("invalid id", r.stderr,
                          f"validate {bad!r} should report invalid id: {r.stderr!r}")
            self.assertNotIn("APPLY", r.stderr,
                             f"validate {bad!r} must not invoke apply: {r.stderr!r}")

    def test_enable_rejects_leading_dash_ids_without_invoking_apply(self) -> None:
        for bad in ("-e", "--help", "-foo"):
            r = self._run("enable", bad)
            self.assertNotEqual(r.returncode, 0, f"enable {bad!r} should fail")
            self.assertIn("invalid id", r.stderr,
                          f"enable {bad!r} should report invalid id: {r.stderr!r}")
            # Neither validate-profile nor enable may be invoked.
            self.assertNotIn("APPLY", r.stderr,
                             f"enable {bad!r} must not invoke apply: {r.stderr!r}")

    def test_valid_id_still_works_after_leading_dash_rejection(self) -> None:
        # A valid ID must still resolve and dispatch after the rejection path.
        r = self._run("validate", "gui-circular")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("APPLY validate-profile gui-circular", r.stderr)

    def test_validate_dispatches_to_apply(self) -> None:
        r = self._run("validate", "gui-circular")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("APPLY validate-profile gui-circular", r.stderr)

    def test_enable_validates_then_enables(self) -> None:
        r = self._run("enable", "gui-circular")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("APPLY validate-profile gui-circular", r.stderr)
        self.assertIn("APPLY enable gui-circular", r.stderr)

    def test_enable_aborts_when_validation_fails(self) -> None:
        # FAILME is a real-looking id shape but the mock apply rejects it; the
        # CLI must not proceed to enable. First plant a fake .opt so the local
        # existence check passes and we actually reach the manager validation.
        (self.user_profiles / "FAILME.opt").write_text(
            'NFQWS2_OPT="\n--lua-desync=circular_quality:key=tls\n"\n', encoding="utf-8")
        r = self._run("enable", "FAILME")
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("APPLY validate-profile FAILME", r.stderr)
        self.assertNotIn("APPLY enable", r.stderr)

    def test_status_dispatches_to_apply(self) -> None:
        r = self._run("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("APPLY status", r.stderr)

    def test_disable_dispatches_to_apply(self) -> None:
        r = self._run("disable")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("APPLY disable", r.stderr)

    def test_no_args_shows_usage(self) -> None:
        r = self._run()
        self.assertNotEqual(r.returncode, 0, r.stderr)

    def test_cli_never_uses_eval_or_source(self) -> None:
        text = PROFILE_CLI.read_text("utf-8")
        # No eval / source / ". " sourcing of profile content. (A literal
        # comment mention is fine; we check for executable usage.)
        for forbidden in (r"\beval\b", r"(\bsource\b|\b\. )\s"):
            # Allow the word only inside a comment line.
            for line in text.splitlines():
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                self.assertIsNone(re.search(forbidden, line),
                                  f"profile CLI uses forbidden {forbidden}: {line!r}")


# ===========================================================================
# Blockcheck wrapper tests (static + mock smoke)
# ===========================================================================

class BlockcheckStaticTest(unittest.TestCase):
    """The blockcheck wrapper has the required safety structure (no run)."""

    def setUp(self) -> None:
        self.text = BLOCKCHECK_CLI.read_text("utf-8")

    def test_syntax_ok(self) -> None:
        if not shutil.which("sh"):
            self.skipTest("sh not on PATH")
        r = subprocess.run(["sh", "-n", str(BLOCKCHECK_CLI)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"sh -n failed: {r.stderr}")

    def test_no_eval_no_source_no_service_stop_no_arbitrary_nft_delete(self) -> None:
        for forbidden in (
            r"\beval\b",
            r"\bsource\b",
            r"\. /opt",                       # no sourcing of package files
            r"/etc/init\.d/[A-Za-z0-9_-]+\s+(stop|restart|reload)",
            r"killall",
            r"nft\s+delete\s+table",          # never deletes tables on normal path
        ):
            for line in self.text.splitlines():
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                self.assertIsNone(re.search(forbidden, line),
                                  f"blockcheck CLI forbidden {forbidden}: {line!r}")

    def test_invokes_blockcheck_with_batch_env(self) -> None:
        self.assertIn("BATCH=1", self.text)
        self.assertRegex(self.text, r"DOMAINS=.*")
        self.assertIn("TEST=standard", self.text)
        self.assertIn("IPVS=4", self.text)
        # The pinned blockcheck2.sh is the only blockcheck invoked.
        self.assertIn('"$BC_SH"', self.text)

    def test_domain_rejects_metacharacters(self) -> None:
        # The domain_ok function rejects whitespace and ; | & ` $(
        self.assertIn("domain must not contain whitespace", self.text)
        self.assertIn("domain contains shell metacharacters", self.text)
        for tok in ("`", "$(", ";", "|", "&"):
            # each forbidden token is referenced in the rejection case
            self.assertIn(tok, self.text, f"domain_ok does not reject {tok!r}")

    def test_no_bashism_dollar_quote_newline(self) -> None:
        # BusyBox ash does not support the $'...' quoting extension. The
        # shipped scripts must not contain $'\n' (or any $'...') so they parse
        # under POSIX/BusyBox ash the same way they parse under bash. Newline
        # matching must use a literal newline or a case glob, not $'\n'.
        self.assertNotIn("$'\\n'", self.text,
                         "blockcheck CLI must not use $'\\n' (BusyBox ash bashism)")
        self.assertNotIn("$'\\t'", self.text,
                         "blockcheck CLI must not use $'\\t' (BusyBox ash bashism)")
        # Broader: no $'...' dollar-quoted strings at all.
        self.assertNotRegex(self.text, r"\$'",
                            "blockcheck CLI must not use $'...' dollar-quoting")

    def test_atomic_mkdir_lock_and_trap(self) -> None:
        self.assertIn('mkdir "$LOCK"', self.text)
        self.assertRegex(self.text, r"trap\s+lock_release\s+INT\s+TERM")
        self.assertRegex(self.text, r"trap\s+.lock_release.\s+EXIT")

    def test_atomic_log_rename(self) -> None:
        self.assertIn("LOG_TMP", self.text)
        self.assertIn('mv -f "$LOG_TMP" "$LOG"', self.text)

    def test_does_not_stop_services_message(self) -> None:
        # When DPI bypass is active it bails out with this exact message.
        self.assertIn("Stop zapret/zapret2 before blockcheck.", self.text)

    def test_no_arbitrary_nft_table_deletion_only_diagnostic(self) -> None:
        # On crash it surfaces a diagnostic, never an automatic delete.
        self.assertIn("nft list tables | grep blockcheck", self.text)

    def test_checks_dependencies(self) -> None:
        for tok in ("blockcheck2.sh", "blockcheck2.d/standard", "nfqws2", "mdig",
                    "nft", "curl", "nslookup"):
            self.assertIn(tok, self.text, f"missing dep check for {tok}")


class BlockcheckCliSmokeTest(unittest.TestCase):
    """Runs the wrapper with mocks; never runs real blockcheck2/nft/nfqws2."""

    @classmethod
    def setUpClass(cls) -> None:
        if not shutil.which("sh"):
            raise unittest.SkipTest("sh not on PATH")

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="orch-blockcheck-cli-")
        b = Path(self.tmp)
        (b / "blockcheck2.d/standard").mkdir(parents=True)
        # mock blockcheck2.sh — records the batch env it was given.
        (b / "blockcheck2.sh").write_text(
            "#!/bin/sh\n"
            'echo "BC BATCH=[$BATCH] DOMAINS=[$DOMAINS] TEST=[$TEST] IPVS=[$IPVS]"\n'
            "echo argv: $*\n"
            "exit ${MOCK_BC_RC:-0}\n",
            encoding="utf-8",
        )
        (b / "blockcheck2.sh").chmod(0o755)
        for name in ("nfqws2", "mdig"):
            f = b / name
            f.write_text("#!/bin/sh\necho mock\n", encoding="utf-8")
            f.chmod(0o755)
        for name in ("nft", "curl", "ip", "nslookup"):
            f = b / name
            f.write_text("#!/bin/sh\necho mock-$0\n", encoding="utf-8")
            f.chmod(0o755)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self) -> dict:
        env = os.environ.copy()
        env["PATH"] = f"{self.tmp}{os.pathsep}{env.get('PATH', '')}"
        env["ZAPRET2_BLOCKCHECK_SH"] = str(Path(self.tmp) / "blockcheck2.sh")
        env["ZAPRET2_BLOCKCHECK_DIR"] = str(Path(self.tmp) / "blockcheck2.d")
        env["ZAPRET2_NFQWS2"] = str(Path(self.tmp) / "nfqws2")
        env["ZAPRET2_MDIG"] = str(Path(self.tmp) / "mdig")
        env["ZAPRET2_ORCH_BLOCKCHECK_LOCK"] = str(Path(self.tmp) / "lock")
        env["ZAPRET2_ORCH_BLOCKCHECK_OUTDIR"] = str(Path(self.tmp) / "out")
        return env

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", str(BLOCKCHECK_CLI), *args],
            env=self._env(), capture_output=True, text=True, timeout=10,
        )

    def test_run_passes_batch_env_and_domain(self) -> None:
        r = self._run("run", "rutracker.org")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("BATCH=[1]", r.stdout)
        self.assertIn("DOMAINS=[rutracker.org]", r.stdout)
        self.assertIn("TEST=[standard]", r.stdout)
        self.assertIn("IPVS=[4]", r.stdout)

    def test_run_accepts_host_path_uri(self) -> None:
        r = self._run("run", "example.com/forum/index.php")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DOMAINS=[example.com/forum/index.php]", r.stdout)

    def test_run_rejects_whitespace_domain(self) -> None:
        r = self._run("run", "foo bar")
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("whitespace", r.stderr)

    def test_run_rejects_tab_in_domain(self) -> None:
        # A tab is whitespace; blockcheck2 DOMAINS is space-separated so any
        # whitespace would split the argument into a second domain.
        r = self._run("run", "foo\tbar")
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("whitespace", r.stderr)

    def test_run_rejects_newline_in_domain(self) -> None:
        # A newline is whitespace and must be rejected compatibly with
        # POSIX/BusyBox ash (no $'\n' bashism in the shipped script). Skip on
        # platforms whose argv layer splits an embedded newline into separate
        # arguments (Windows/Git-Bash): the newline never reaches the script
        # there, so the rejection is not observable through argv. The static
        # no-$'\n' contract test covers the bashism on every platform.
        if not _argv_preserves_newline():
            self.skipTest("platform argv does not preserve embedded newline")
        r = self._run("run", "foo\nbar")
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("whitespace", r.stderr)

    def test_run_rejects_command_substitution(self) -> None:
        for bad in ("foo$(id)", "foo`id`", "foo;id", "foo|id", "foo&id"):
            r = self._run("run", bad)
            self.assertNotEqual(r.returncode, 0, f"run {bad!r} should fail: {r.stdout}{r.stderr}")
            self.assertIn("metacharacters", r.stderr)

    def test_run_rejects_shell_metacharacter_domain(self) -> None:
        # Combined guard: a domain with a shell metacharacter is rejected with
        # the metacharacters message, never reaching blockcheck2.
        for bad in ("foo|bar", "foo&bar", "foo;bar"):
            r = self._run("run", bad)
            self.assertNotEqual(r.returncode, 0, f"run {bad!r} should fail")
            self.assertIn("metacharacters", r.stderr)

    def test_run_publishes_log_and_last_reads_it(self) -> None:
        r = self._run("run", "rutracker.org")
        self.assertEqual(r.returncode, 0, r.stderr)
        r2 = self._run("last")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("BC BATCH=[1]", r2.stdout)

    def test_last_without_log_is_nonzero(self) -> None:
        # Point at a fresh outdir with no log yet.
        env = self._env()
        fresh = Path(self.tmp) / "fresh-out"
        env["ZAPRET2_ORCH_BLOCKCHECK_OUTDIR"] = str(fresh)
        r = subprocess.run(["sh", str(BLOCKCHECK_CLI), "last"],
                           env=env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(r.returncode, 0, r.stderr)

    def test_run_preserves_real_exit_code(self) -> None:
        env = self._env()
        env["MOCK_BC_RC"] = "5"
        r = subprocess.run(["sh", str(BLOCKCHECK_CLI), "run", "some.org"],
                           env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 5, r.stderr)
        # Crash-path diagnostic, but never an automatic table delete.
        self.assertIn("nft list tables | grep blockcheck", r.stderr)

    def test_run_releases_lock(self) -> None:
        self._run("run", "rutracker.org")
        self.assertFalse((Path(self.tmp) / "lock").exists(), "lock not released")

    def test_run_does_not_invoke_real_nft_or_init(self) -> None:
        # The mock nft writes mock-<path>; confirm blockcheck2 was the only
        # "real" thing invoked and no init script path appears.
        r = self._run("run", "rutracker.org")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("/etc/init.d", r.stdout + r.stderr)
        self.assertNotIn("/opt/zapret2/blockcheck2.sh", r.stderr)


# ===========================================================================
# Meta: tests must not run the real dangerous binaries
# ===========================================================================

class NoRealBinariesInvokedTest(unittest.TestCase):
    """Guard: these tests never run real nft / blockcheck2 / nfqws2 / init."""

    def test_tests_do_not_call_real_blockcheck_or_nft_directly(self) -> None:
        # Inspect this test module's own source: it must not shell out to the
        # real /opt/zapret2/blockcheck2.sh, nft, or init.d.
        src = Path(__file__).read_text("utf-8")
        for forbidden in (
            r"/opt/zapret2/blockcheck2\.sh",
            r"\bnft\b(?!\s+list\s+tables)",      # allow the diagnostic string
            r"/etc/init\.d/zapret2(?:-orchestra)?\s+(start|stop|restart)",
        ):
            # Allow occurrences inside string literals that are clearly the
            # diagnostic message; the real-invoke risk is subprocess.run([...])
            # with those as the program. We only assert no subprocess.run call
            # uses them as the executable.
            self.assertNotRegex(
                src,
                r"subprocess\.run\(\s*\[?\s*['\"]" + forbidden.lstrip("\\"),
                f"test module may invoke real binary: {forbidden}",
            )


if __name__ == "__main__":
    unittest.main()
