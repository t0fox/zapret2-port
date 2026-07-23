include $(TOPDIR)/rules.mk

PKG_NAME:=zapret2-orchestra
PKG_VERSION:=0.1.0
PKG_RELEASE:=7
PKGARCH:=all

include $(INCLUDE_DIR)/package.mk

# The package is self-contained: every file installed into the image lives
# under $(CURDIR)/files/ at its target path. There is no dependency on the
# package directory's location relative to a feed or Git root. The canonical
# development copies of the Lua runtime and JSON seeds are kept at the
# repository root (lua/orchestra-extra, etc/zapret2-orchestra) for testing;
# tests/test_package_contract.py enforces that those copies stay byte-identical
# to the files shipped here.

define Package/zapret2-orchestra
  SECTION:=net
  CATEGORY:=Network
  SUBMENU:=Zapret2
  TITLE:=Orchestra runtime learner, rpcd backend and preload generator
  MAINTAINER:=t0fox
  DEPENDS:= \
    +zapret2 \
    +rpcd \
    +rpcd-mod-ucode \
    +ucode \
    +ucode-mod-fs \
    +ucode-mod-uci \
    +procd
endef

define Package/zapret2-orchestra/description
Read-only rpcd backend exposing Zapret2 Orchestra status and validation, the
TLS runtime extension under /opt/zapret2/lua/orchestra-extra, persistent JSON
state seeds under /etc/zapret2-orchestra, and a ucode preload generator that
renders /tmp/zapret2-orchestra/preload.lua, whitelist.txt and a generation
manifest. The generator is a standalone command
(/usr/sbin/zapret2-orchestra-preload) that a runtime manager can invoke before
nfqws2; a boot hook (/etc/init.d/zapret2-orchestra) is a backup regeneration
step. The package does not start, stop, or reconfigure the zapret2 service.
endef

define Build/Compile
endef

define Package/zapret2-orchestra/conffiles
/etc/zapret2-orchestra/blocked.json
/etc/zapret2-orchestra/learned.json
/etc/zapret2-orchestra/manual-locks.json
/etc/zapret2-orchestra/whitelist.json
endef

define Package/zapret2-orchestra/install
	# rpcd backend and ACL
	$(INSTALL_DIR) $(1)/usr/share/rpcd/ucode
	$(INSTALL_DIR) $(1)/usr/share/rpcd/acl.d
	$(INSTALL_BIN) $(CURDIR)/files/usr/share/rpcd/ucode/zapret2.orchestra $(1)/usr/share/rpcd/ucode/zapret2.orchestra
	$(INSTALL_DATA) $(CURDIR)/files/usr/share/rpcd/acl.d/zapret2-orchestra.json $(1)/usr/share/rpcd/acl.d/zapret2-orchestra.json
	# preload generator, manifest checker and shell wrapper
	$(INSTALL_DIR) $(1)/usr/share/zapret2-orchestra
	$(INSTALL_DATA) $(CURDIR)/files/usr/share/zapret2-orchestra/generate-preload.uc $(1)/usr/share/zapret2-orchestra/generate-preload.uc
	# runtime manager (Phase 1A): safe NFQWS2_OPT parser/transformer,
	# atomic JSON state, mkdir lock, profile validator, CLI dispatcher.
	# Never sources/evals the config; the CLI wrapper runs `sh -n` on a
	# temp copy only.
	$(INSTALL_DATA) $(CURDIR)/files/usr/share/zapret2-orchestra/apply.uc $(1)/usr/share/zapret2-orchestra/apply.uc
	# builtin profiles (user overrides live under /etc and are not shipped).
	# The *.opt wildcard picks up every ready nfqws2 profile; profiles.tsv is
	# the simple TSV manifest (id<TAB>name<TAB>transport<TAB>source) consumed by
	# the zapret2-orchestra-profile list command — pure shell, no extra deps.
	$(INSTALL_DIR) $(1)/usr/share/zapret2-orchestra/profiles
	$(INSTALL_DATA) $(CURDIR)/files/usr/share/zapret2-orchestra/profiles/*.opt $(1)/usr/share/zapret2-orchestra/profiles/
	# r7 adaptive profile sidecars (chain_id_for_strategy maps, contract §4).
	$(INSTALL_DATA) $(CURDIR)/files/usr/share/zapret2-orchestra/profiles/*.json $(1)/usr/share/zapret2-orchestra/profiles/
	$(INSTALL_DATA) $(CURDIR)/files/usr/share/zapret2-orchestra/profiles.tsv $(1)/usr/share/zapret2-orchestra/profiles.tsv
	$(INSTALL_DIR) $(1)/usr/sbin
	$(INSTALL_BIN) $(CURDIR)/files/usr/sbin/zapret2-orchestra-preload $(1)/usr/sbin/zapret2-orchestra-preload
	$(INSTALL_BIN) $(CURDIR)/files/usr/sbin/zapret2-orchestra-apply $(1)/usr/sbin/zapret2-orchestra-apply
	# working-prototype CLI: ready profile selection. Lists/shows/validates/
	# enables/disables profiles by dispatching to the transactional manager
	# (never evals profile content).
	$(INSTALL_BIN) $(CURDIR)/files/usr/sbin/zapret2-orchestra-profile $(1)/usr/sbin/zapret2-orchestra-profile
	# working-prototype CLI: batch blockcheck. Runs the pinned blockcheck2 in
	# batch mode for a single domain; it does NOT stop services or touch
	# packet-filter rules — upstream blockcheck2 owns its own cleanup.
	$(INSTALL_BIN) $(CURDIR)/files/usr/sbin/zapret2-orchestra-blockcheck $(1)/usr/sbin/zapret2-orchestra-blockcheck
	# r7 closed-loop learner: a ucode long-running daemon that tails
	# events.ndjson, persists learned/blocked/learner-state, and reloads nfqws2
	# (debounced) when a lock/blocked-strategy changes.  The daemon program
	# lives under /usr/share; the shell wrapper invokes ucode on it.
	$(INSTALL_DATA) $(CURDIR)/files/usr/share/zapret2-orchestra/learner.uc $(1)/usr/share/zapret2-orchestra/learner.uc
	$(INSTALL_BIN) $(CURDIR)/files/usr/sbin/zapret2-orchestra-learner $(1)/usr/sbin/zapret2-orchestra-learner
	# reload helper: the zapret2 service restart reference lives here (procd
	# layer), NOT in apply.uc or learner.uc (test_working_prototype prohibition).
	$(INSTALL_BIN) $(CURDIR)/files/usr/sbin/zapret2-orchestra-reload $(1)/usr/sbin/zapret2-orchestra-reload
	# boot hook (one-shot backup regeneration, START=20 runs before zapret2 START=21)
	$(INSTALL_DIR) $(1)/etc/init.d
	$(INSTALL_BIN) $(CURDIR)/files/etc/init.d/zapret2-orchestra $(1)/etc/init.d/zapret2-orchestra
	# learner procd service (START=22, after zapret2 START=21 so the events sink
	# and datapath exist before the learner begins).
	$(INSTALL_BIN) $(CURDIR)/files/etc/init.d/zapret2-orchestra-learner $(1)/etc/init.d/zapret2-orchestra-learner
	# TLS runtime extension
	$(INSTALL_DIR) $(1)/opt/zapret2/lua/orchestra-extra
	$(INSTALL_DATA) $(CURDIR)/files/opt/zapret2/lua/orchestra-extra/*.lua $(1)/opt/zapret2/lua/orchestra-extra/
	# init_vars.lua (r7 discord-v5 / discord-adaptive asset): provides tls_google
	# and other tls_mod blobs via the nfqws2 builtin tls_mod. Verbatim from
	# youtubediscord/zapret2-youtube-discord @ 4d75c70b (lua/init_vars.lua).
	$(INSTALL_DATA) $(CURDIR)/files/opt/zapret2/lua/init_vars.lua $(1)/opt/zapret2/lua/init_vars.lua
	# custom_funcs.lua (r7 discord-adaptive strategy=1 "Default old" asset):
	# defines tls_multisplit_sni (and other user-override desync functions),
	# self-contained with helpers in the shipped core lua. Verbatim from
	# youtubediscord/zapret2-youtube-discord @ 4d75c70b (lua/custom_funcs.lua).
	# strategy=1 is blocked for discord.com by DEFAULT_BLOCKED_PASS_DOMAINS so it
	# is not executed on the live Discord path, but the --lua-init must load.
	$(INSTALL_DATA) $(CURDIR)/files/opt/zapret2/lua/custom_funcs.lua $(1)/opt/zapret2/lua/custom_funcs.lua
	# bin/stun.bin (r7 discord-adaptive strategy=1 stun_pat blob). Verbatim from
	# youtubediscord/zapret2-youtube-discord @ 4d75c70b (bin/stun.bin).
	# bin/tls_clienthello_{1,5,7}.bin (r7 original-parity pool blobs tls1/tls5/tls7,
	# used by 7 of the 24 circular-compatible strategies). Verbatim from the same pinned repo.
	$(INSTALL_DIR) $(1)/opt/zapret2/bin
	$(INSTALL_DATA) $(CURDIR)/files/opt/zapret2/bin/stun.bin $(1)/opt/zapret2/bin/stun.bin
	$(INSTALL_DATA) $(CURDIR)/files/opt/zapret2/bin/tls_clienthello_1.bin $(1)/opt/zapret2/bin/tls_clienthello_1.bin
	$(INSTALL_DATA) $(CURDIR)/files/opt/zapret2/bin/tls_clienthello_5.bin $(1)/opt/zapret2/bin/tls_clienthello_5.bin
	$(INSTALL_DATA) $(CURDIR)/files/opt/zapret2/bin/tls_clienthello_7.bin $(1)/opt/zapret2/bin/tls_clienthello_7.bin
	# persistent JSON state seeds (also listed in conffiles)
	$(INSTALL_DIR) $(1)/etc/zapret2-orchestra
	$(INSTALL_DATA) $(CURDIR)/files/etc/zapret2-orchestra/*.json $(1)/etc/zapret2-orchestra/
	# r7 discord ipset (Discord IP ranges) for the discord-v5 / discord-adaptive
	# profiles. Verbatim from the pinned repo (lists/ipset-discord.txt).
	$(INSTALL_DIR) $(1)/etc/zapret2-orchestra/lists
	$(INSTALL_DATA) $(CURDIR)/files/etc/zapret2-orchestra/lists/ipset-discord.txt $(1)/etc/zapret2-orchestra/lists/ipset-discord.txt
endef

define Package/zapret2-orchestra/postinst
#!/bin/sh
if [ -z "$${IPKG_INSTROOT}" ]; then
	/etc/init.d/zapret2-orchestra enable
	/etc/init.d/zapret2-orchestra-learner enable
	/usr/sbin/zapret2-orchestra-preload
	if [ -x /etc/init.d/rpcd ]; then
		/etc/init.d/rpcd restart
	fi
fi
exit 0
endef

define Package/zapret2-orchestra/postrm
#!/bin/sh
if [ -z "$${IPKG_INSTROOT}" ]; then
	/etc/init.d/zapret2-orchestra disable
	/etc/init.d/zapret2-orchestra-learner disable
	if [ -x /etc/init.d/rpcd ]; then
		/etc/init.d/rpcd restart
	fi
fi
exit 0
endef

$(eval $(call BuildPackage,zapret2-orchestra))
