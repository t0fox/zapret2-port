include $(TOPDIR)/rules.mk

PKG_NAME:=zapret2
PKG_VERSION:=0.9.20260307
PKG_RELEASE:=1

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
	# ipset scripts
	$(INSTALL_DIR) $(1)/opt/zapret2/ipset
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/ipset/* $(1)/opt/zapret2/ipset/
	# Config
	$(INSTALL_DIR) $(1)/opt/zapret2
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/config.default $(1)/opt/zapret2/config
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/config.default $(1)/opt/zapret2/config.default
	# blockcheck
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/blockcheck2.sh $(1)/opt/zapret2/blockcheck2.sh
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
