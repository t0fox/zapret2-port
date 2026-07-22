include $(TOPDIR)/rules.mk

PKG_NAME:=zapret2
PKG_VERSION:=0.9.20260307
PKG_RELEASE:=2

# Official source: bol-van/zapret2 at the pinned submodule SHA.
# The OpenWrt build system downloads this via the standard source contract.
PKG_SOURCE_PROTO:=git
PKG_SOURCE_URL:=https://github.com/bol-van/zapret2.git
PKG_SOURCE_VERSION:=8a0f53f3cf2c92ddeaa66995ee63a35c1210c410
PKG_SOURCE_DATE:=2026-03-07

LUA_JIT?=1

ifeq ($(LUA_JIT),1)
  LUAJIT_VER?=2.1
  LUAJIT_LUA_VER?=5.1
  LUA_DEP:=luajit
  LUA_INCLUDE:=-I$(STAGING_DIR)/usr/include/luajit-$(LUAJIT_VER)
  LUA_LIBRARY:=-L$(STAGING_DIR)/usr/lib -lluajit-$(LUAJIT_LUA_VER)
else
  LUA_VER?=5.3
  LUA_DEP:=lua$(LUA_VER)
  LUA_INCLUDE:=-I$(STAGING_DIR)/usr/include/lua$(LUA_VER)
  LUA_LIBRARY:=-L$(STAGING_DIR)/usr/lib -llua$(LUA_VER)
endif

include $(INCLUDE_DIR)/package.mk

define Package/zapret2
  SECTION:=net
  CATEGORY:=Network
  SUBMENU:=Zapret2
  TITLE:=Zapret2 runtime with nfqws2, ip2net, mdig
  MAINTAINER:=t0fox
  DEPENDS:= \
    +nftables \
    +curl \
    +gzip \
    +coreutils \
    +coreutils-sort \
    +coreutils-sleep \
    +kmod-nft-nat \
    +kmod-nft-offload \
    +kmod-nft-queue \
    +libnetfilter-queue \
    +libmnl \
    +libcap \
    +zlib \
    +$(LUA_DEP)
endef

define Package/zapret2/description
Zapret2 runtime from bol-van/zapret2 (SHA 8a0f53f): nfqws2, ip2net, mdig
binaries cross-compiled for the target architecture, plus upstream Lua modules,
OpenWrt init script, hotplug hook, firewall include, shell helpers, ipset
scripts, fake packet data and default config.
endef

MAKE_FLAGS += \
	$(TARGET_CONFIGURE_OPTS) \
	CFLAGS="$(TARGET_CFLAGS)" \
	LDFLAGS="$(TARGET_LDFLAGS)"

define Build/Compile
	$(MAKE) -C $(PKG_BUILD_DIR)/nfq2 $(MAKE_FLAGS) \
		LUA_CFLAGS="$(LUA_INCLUDE)" LUA_LIB="$(LUA_LIBRARY)"
	$(MAKE) -C $(PKG_BUILD_DIR)/ip2net $(MAKE_FLAGS)
	$(MAKE) -C $(PKG_BUILD_DIR)/mdig $(MAKE_FLAGS)
endef

define Package/zapret2/conffiles
/opt/zapret2/config
endef

define Package/zapret2/install
	# Compiled binaries
	$(INSTALL_DIR) $(1)/opt/zapret2/nfq2
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/nfq2/nfqws2 $(1)/opt/zapret2/nfq2/nfqws2
	$(INSTALL_DIR) $(1)/opt/zapret2/ip2net
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/ip2net/ip2net $(1)/opt/zapret2/ip2net/ip2net
	$(INSTALL_DIR) $(1)/opt/zapret2/mdig
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/mdig/mdig $(1)/opt/zapret2/mdig/mdig
	# Lua runtime
	$(INSTALL_DIR) $(1)/opt/zapret2/lua
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/lua/*.lua $(1)/opt/zapret2/lua/
	# Shell helpers
	$(INSTALL_DIR) $(1)/opt/zapret2/common
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/common/*.sh $(1)/opt/zapret2/common/
	# Init helpers
	$(INSTALL_DIR) $(1)/opt/zapret2/init.d/openwrt/custom.d
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/init.d/openwrt/functions $(1)/opt/zapret2/init.d/openwrt/functions
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/init.d/openwrt/custom.d/.keep $(1)/opt/zapret2/init.d/openwrt/custom.d/.keep
	# Fake binary data
	$(INSTALL_DIR) $(1)/opt/zapret2/files/fake
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/files/fake/*.bin $(1)/opt/zapret2/files/fake/
	# ipset scripts and data. Upstream marks every *.sh (except def.sh) as
	# executable (0755): create_ipset.sh is invoked directly by the firewall
	# include via functions ("$IPSET_CR" "$@"), clear_lists.sh and the
	# get_*.sh scripts are executed directly by cron/systemd/init. They MUST
	# be installed with INSTALL_BIN (0755) or the post-install firewall reload
	# fails with "create_ipset.sh: Permission denied" (router blocker).
	# def.sh has no shebang and is sourced (not executed); upstream marks it
	# 0644, so it stays INSTALL_DATA. Data files (antifilter.helper, *.default)
	# are INSTALL_DATA.
	$(INSTALL_DIR) $(1)/opt/zapret2/ipset
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/ipset/*.sh $(1)/opt/zapret2/ipset/
	# def.sh is sourced, not executed — keep it non-executable (0644) per upstream.
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/ipset/def.sh $(1)/opt/zapret2/ipset/def.sh
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/ipset/antifilter.helper $(1)/opt/zapret2/ipset/antifilter.helper
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/ipset/zapret-hosts-user-exclude.txt.default $(1)/opt/zapret2/ipset/
	# Config
	$(INSTALL_DIR) $(1)/opt/zapret2
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/config.default $(1)/opt/zapret2/config
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/config.default $(1)/opt/zapret2/config.default
	# blockcheck2 — DPI bypass strategy tester.
	# blockcheck2.sh is the entry point (#!/bin/sh, upstream 0755) → INSTALL_BIN.
	# blockcheck2.d/ holds strategy modules that blockcheck2.sh SOURCES via
	# `. "$script"` (no shebangs; upstream marks every file there 0644). Because
	# they are sourced (`. "$script"`), not executed, the *.sh modules MUST be
	# INSTALL_DATA (0644) — matching upstream — never INSTALL_BIN. The data
	# files (def.inc, README, *.txt strategy lists) are also INSTALL_DATA (0644).
	# Upstream path structure is preserved under
	# /opt/zapret2/blockcheck2.d/{standard,custom}/. blockcheck2.sh derives
	# ZAPRET_BASE from `dirname "$0"` → /opt/zapret2, so its expected tool paths
	# (nfq2/nfqws2, mdig/mdig, common/*.sh, the blockcheck2.d tree) all resolve
	# against the package root. No new runtime deps are needed: nft/curl/gzip/
	# sort/sleep are already DEPENDS below, ip/nslookup/seq/head come from the
	# busybox base, mdig/nfqws2 ship in this package, and iptables/ipset are
	# only used on the legacy non-fw4 fwtype.
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/blockcheck2.sh $(1)/opt/zapret2/blockcheck2.sh
	$(INSTALL_DIR) $(1)/opt/zapret2/blockcheck2.d/standard
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/blockcheck2.d/standard/*.sh $(1)/opt/zapret2/blockcheck2.d/standard/
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/blockcheck2.d/standard/def.inc $(1)/opt/zapret2/blockcheck2.d/standard/def.inc
	$(INSTALL_DIR) $(1)/opt/zapret2/blockcheck2.d/custom
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/blockcheck2.d/custom/*.sh $(1)/opt/zapret2/blockcheck2.d/custom/
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/blockcheck2.d/custom/*.txt $(1)/opt/zapret2/blockcheck2.d/custom/
	# Init script
	$(INSTALL_DIR) $(1)/etc/init.d
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/init.d/openwrt/zapret2 $(1)/etc/init.d/zapret2
	# Hotplug hook
	$(INSTALL_DIR) $(1)/etc/hotplug.d/iface
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/init.d/openwrt/90-zapret2 $(1)/etc/hotplug.d/iface/90-zapret2
	# Firewall include
	$(INSTALL_DIR) $(1)/etc
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/init.d/openwrt/firewall.zapret2 $(1)/etc/firewall.zapret2
endef

$(eval $(call BuildPackage,zapret2))
