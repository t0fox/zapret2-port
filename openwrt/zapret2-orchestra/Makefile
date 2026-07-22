include $(TOPDIR)/rules.mk

PKG_NAME:=zapret2-orchestra
PKG_VERSION:=0.1.0
PKG_RELEASE:=4
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
  TITLE:=Read-only rpcd backend and preload generator for Zapret2 Orchestra
  MAINTAINER:=t0fox
  DEPENDS:= \
    +zapret2 \
    +rpcd \
    +rpcd-mod-ucode \
    +ucode \
    +ucode-mod-fs \
    +ucode-mod-uci
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
	$(INSTALL_DATA) $(CURDIR)/files/usr/share/zapret2-orchestra/profiles.tsv $(1)/usr/share/zapret2-orchestra/profiles.tsv
	$(INSTALL_DIR) $(1)/usr/sbin
	$(INSTALL_BIN) $(CURDIR)/files/usr/sbin/zapret2-orchestra-preload $(1)/usr/sbin/zapret2-orchestra-preload
	$(INSTALL_BIN) $(CURDIR)/files/usr/sbin/zapret2-orchestra-apply $(1)/usr/sbin/zapret2-orchestra-apply
	# working-prototype CLI: ready profile selection. Lists/shows/validates/
	# enables/disables profiles by dispatching to the transactional manager
	# (never evals profile content).
	$(INSTALL_BIN) $(CURDIR)/files/usr/sbin/zapret2-orchestra-profile $(1)/usr/sbin/zapret2-orchestra-profile
	# boot hook (one-shot backup regeneration, START=20 runs before zapret2 START=21)
	$(INSTALL_DIR) $(1)/etc/init.d
	$(INSTALL_BIN) $(CURDIR)/files/etc/init.d/zapret2-orchestra $(1)/etc/init.d/zapret2-orchestra
	# TLS runtime extension
	$(INSTALL_DIR) $(1)/opt/zapret2/lua/orchestra-extra
	$(INSTALL_DATA) $(CURDIR)/files/opt/zapret2/lua/orchestra-extra/*.lua $(1)/opt/zapret2/lua/orchestra-extra/
	# persistent JSON state seeds (also listed in conffiles)
	$(INSTALL_DIR) $(1)/etc/zapret2-orchestra
	$(INSTALL_DATA) $(CURDIR)/files/etc/zapret2-orchestra/*.json $(1)/etc/zapret2-orchestra/
endef

define Package/zapret2-orchestra/postinst
#!/bin/sh
if [ -z "$${IPKG_INSTROOT}" ]; then
	/etc/init.d/zapret2-orchestra enable
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
	if [ -x /etc/init.d/rpcd ]; then
		/etc/init.d/rpcd restart
	fi
fi
exit 0
endef

$(eval $(call BuildPackage,zapret2-orchestra))
