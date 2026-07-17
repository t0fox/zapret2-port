# Zapret2 Orchestra port for OpenWrt

Status: experimental.

This project ports the Orchestra control plane for Zapret2 on OpenWrt. It
currently provides only the read-only `status` and `validate` rpcd methods.
The Zapret2 runtime is not replaced, and upstream Lua is not modified.

Clone with submodules:

```sh
git clone --recurse-submodules https://github.com/t0fox/zapret2-port.git
```

Run local tests:

```sh
python -m unittest discover -s tests -v
```

The package is not release-ready until it has been built with a real OpenWrt
SDK and installed. The TLS Lua prototype is not yet connected to nfqws2.
There is no automatic deployment.
