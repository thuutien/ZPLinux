#!/bin/sh
# Build a noarch .rpm package for Fedora and other RPM distributions.
# Usage: ./packaging/build-rpm.sh   ->   dist/zplinux-<version>-1.noarch.rpm
# Needs rpmbuild (Fedora: rpm-build, Arch: rpm-tools). Packages the committed HEAD.
set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)"
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/zplinux/__init__.py")
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE/SOURCES" "$STAGE/SPECS" "$SRC/dist"
git -C "$SRC" archive --format=tar.gz --prefix="zplinux-$VERSION/" \
    -o "$STAGE/SOURCES/zplinux-$VERSION.tar.gz" HEAD
cat > "$STAGE/SPECS/zplinux.spec" <<SPEC
# Plain Python files: no bytecode for the build machine's Python, no debug package.
%global __os_install_post %{nil}
%global debug_package %{nil}

Name:           zplinux
Version:        $VERSION
Release:        1
Summary:        Label designer for Zebra and other ZPL thermal printers
License:        GPL-3.0-only
URL:            https://github.com/thuutien/ZPLinux
Source0:        zplinux-$VERSION.tar.gz
BuildArch:      noarch
Requires:       python3 >= 3.8, python3-gobject >= 3.42, python3-cairo, gtk4 >= 4.12, pango
Recommends:     python3-qrcode, zint, liberation-sans-fonts, polkit
Suggests:       cups-client

%description
A WYSIWYG GTK 4 label designer that produces ZPL II. Lay out text, Code 128
barcodes, QR codes, boxes, ellipses and images, fill {variables} by hand or
from a CSV file, and print over the network (port 9100), to a USB printer,
through a raw CUPS queue, or to a .zpl file. Includes zplinux-fake-printer
for testing without hardware.

%prep
%setup -q

%install
sh install.sh --prefix-root %{buildroot}

%post
for c in gtk-update-icon-cache gtk4-update-icon-cache; do
    command -v \$c >/dev/null && { \$c -q -t /usr/share/icons/hicolor || true; break; }
done
command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
command -v update-mime-database >/dev/null && update-mime-database /usr/share/mime || true

%postun
for c in gtk-update-icon-cache gtk4-update-icon-cache; do
    command -v \$c >/dev/null && { \$c -q -t /usr/share/icons/hicolor || true; break; }
done
command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
command -v update-mime-database >/dev/null && update-mime-database /usr/share/mime || true

%files
%license LICENSE
/usr/lib/zplinux
/usr/share/zplinux
/usr/bin/zplinux
/usr/bin/zplinux-fake-printer
/usr/share/applications/zplinux.desktop
/usr/share/icons/hicolor/scalable/apps/zplinux.svg
/usr/share/mime/packages/zplinux.xml
SPEC
rpmbuild --define "_topdir $STAGE" -bb "$STAGE/SPECS/zplinux.spec"
OUT="$SRC/dist/zplinux-$VERSION-1.noarch.rpm"
cp "$STAGE/RPMS/noarch/zplinux-$VERSION-1.noarch.rpm" "$OUT"
echo "$OUT"
sha256sum "$OUT"
