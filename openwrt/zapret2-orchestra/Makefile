include $(TOPDIR)/rules.mk

PKG_NAME:=zapret2-orchestra
PKG_VERSION:=0.1.0
PKG_RELEASE:=1
PKGARCH:=all

include $(INCLUDE_DIR)/package.mk

define Package/zapret2-orchestra
  SECTION:=net
  CATEGORY:=Network
  SUBMENU:=Zapret2
  TITLE:=Read-only rpcd backend for Zapret2 Orchestra
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
Read-only rpcd backend exposing Zapret2 Orchestra status and validation.
endef

define Build/Compile
endef

define Package/zapret2-orchestra/install
	$(INSTALL_DIR) $(1)/usr/share/rpcd/ucode
	$(INSTALL_DIR) $(1)/usr/share/rpcd/acl.d
	$(INSTALL_BIN) $(CURDIR)/files/usr/share/rpcd/ucode/zapret2.orchestra $(1)/usr/share/rpcd/ucode/zapret2.orchestra
	$(INSTALL_DATA) $(CURDIR)/files/usr/share/rpcd/acl.d/zapret2-orchestra.json $(1)/usr/share/rpcd/acl.d/zapret2-orchestra.json
endef

define Package/zapret2-orchestra/postinst
#!/bin/sh
if [ -z "$${IPKG_INSTROOT}" ] && [ -x /etc/init.d/rpcd ]; then
	/etc/init.d/rpcd restart
fi
exit 0
endef

define Package/zapret2-orchestra/postrm
#!/bin/sh
if [ -z "$${IPKG_INSTROOT}" ] && [ -x /etc/init.d/rpcd ]; then
	/etc/init.d/rpcd restart
fi
exit 0
endef

$(eval $(call BuildPackage,zapret2-orchestra))
