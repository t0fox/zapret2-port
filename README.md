# Zapret2 Orchestra port for OpenWrt

Status: experimental.

This project ports the Orchestra control plane for Zapret2 on OpenWrt. It
provides a read-only `status` and `validate` rpcd backend, the TLS runtime
extension, persistent JSON state seeds, and a ucode preload generator that
renders `/tmp/zapret2-orchestra/preload.lua` and `whitelist.txt` at boot and at
install time. The Zapret2 runtime is not replaced, and upstream Lua is not
modified.

Clone with submodules:

```sh
git clone --recurse-submodules https://github.com/t0fox/zapret2-port.git
```

Run local tests:

```sh
python -m unittest discover -s tests -v
```

The preload generator runtime tests require the `ucode` executable on PATH;
they are skipped automatically when it is absent.

## Package contents (Phase 0)

The package is self-contained: every installed file lives under
`openwrt/zapret2-orchestra/files/` at its target path, so the build does not
depend on the package directory's location relative to a feed or Git root.

| Install path | Source | Role |
|---|---|---|
| `/usr/share/rpcd/ucode/zapret2.orchestra` | `files/usr/share/rpcd/ucode/` | read-only rpcd backend |
| `/usr/share/rpcd/acl.d/zapret2-orchestra.json` | `files/usr/share/rpcd/acl.d/` | least-privilege ACL |
| `/usr/share/zapret2-orchestra/generate-preload.uc` | `files/usr/share/zapret2-orchestra/` | ucode preload generator (`generate`+`check`) |
| `/usr/sbin/zapret2-orchestra-preload` | `files/usr/sbin/` | shell wrapper, passes args through |
| `/etc/init.d/zapret2-orchestra` | `files/etc/init.d/` | boot hook (one-shot backup, `START=20`, before `zapret2`) |
| `/opt/zapret2/lua/orchestra-extra/*.lua` | `files/opt/zapret2/lua/orchestra-extra/` | TLS runtime extension (6 files) |
| `/etc/zapret2-orchestra/*.json` | `files/etc/zapret2-orchestra/` | persistent state seeds (conffiles, 4 files) |

The JSON seeds are listed as conffiles so user edits survive package upgrades.
The package does not start, stop, or reconfigure the `zapret2` service, does
not write UCI, and does not touch the firewall. Lifecycle hooks are guarded by
`IPKG_INSTROOT` so they are skipped during image builds.

The preload generator writes `preload.lua`, `whitelist.txt`, and a
`manifest.json` (length + 31-bit rolling hash of each file) atomically under
`/tmp/zapret2-orchestra/`. `zapret2-orchestra-preload check` verifies the
generated files against the manifest.

The package is not release-ready until it has been built with a real OpenWrt
SDK and installed. The preload generator has not been executed with a real
`ucode` on Windows; the ucode API was verified against the upstream source
instead. The TLS Lua prototype is not yet connected to nfqws2.
