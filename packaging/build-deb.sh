#!/bin/sh
# Build a .deb package for Linux Mint / Ubuntu / Debian.
# Usage: ./packaging/build-deb.sh   ->   dist/zplinux_<version>_all.deb
set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)"
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/zplinux/__init__.py")
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

chmod 755 "$STAGE"
sh "$SRC/install.sh" --prefix-root "$STAGE"
install -d "$STAGE/DEBIAN"
cat > "$STAGE/DEBIAN/control" <<CTRL
Package: zplinux
Version: $VERSION
Section: graphics
Priority: optional
Architecture: all
Depends: python3 (>= 3.8), python3-gi (>= 3.42), python3-gi-cairo, python3-cairo, gir1.2-gtk-4.0 (>= 4.12), gir1.2-pango-1.0
Replaces: labelix
Conflicts: labelix
Recommends: python3-qrcode, zint, fonts-liberation | fonts-liberation2, pkexec | policykit-1
Suggests: cups-client
Maintainer: ZPLinux <zplinux@localhost>
Description: Label designer for Zebra and other ZPL thermal printers
 A WYSIWYG GTK 4 label designer that produces ZPL II. Lay out text, Code 128
 barcodes, QR codes, boxes, ellipses and images, fill {variables} by hand or
 from a CSV file, and print over the network (port 9100), to a USB printer,
 through a raw CUPS queue, or to a .zpl file. Includes zplinux-fake-printer
 for testing without hardware.
CTRL
cat > "$STAGE/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
for c in gtk-update-icon-cache gtk4-update-icon-cache; do
    command -v $c >/dev/null && { $c -q -t /usr/share/icons/hicolor || true; break; }
done
command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
command -v update-mime-database >/dev/null && update-mime-database /usr/share/mime || true
POST
chmod 755 "$STAGE/DEBIAN/postinst"
cp "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/postrm"
mkdir -p "$SRC/dist"
dpkg-deb --root-owner-group --build "$STAGE" "$SRC/dist/zplinux_${VERSION}_all.deb"
