#!/bin/sh
# Install (or uninstall with --uninstall) ZPLinux system-wide.
# Usage: sudo ./install.sh [--uninstall] [--prefix-root DIR] [--prefix /usr]
set -e

ROOT=""
PREFIX="/usr"
UNINSTALL=0
while [ $# -gt 0 ]; do
    case "$1" in
        --uninstall) UNINSTALL=1 ;;
        --prefix-root) ROOT="$2"; shift ;;
        --prefix) PREFIX="$2"; shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

LIB="$ROOT$PREFIX/lib/zplinux"
SHARE="$ROOT$PREFIX/share/zplinux"
BIN="$ROOT$PREFIX/bin/zplinux"
FAKE="$ROOT$PREFIX/bin/zplinux-fake-printer"
DESKTOP="$ROOT$PREFIX/share/applications/zplinux.desktop"
ICON="$ROOT$PREFIX/share/icons/hicolor/scalable/apps/zplinux.svg"
MIME="$ROOT$PREFIX/share/mime/packages/zplinux.xml"

if [ "$UNINSTALL" = 1 ]; then
    rm -rf "$LIB" "$SHARE" "$BIN" "$FAKE" "$DESKTOP" "$ICON" "$MIME"
    echo "ZPLinux uninstalled."
    exit 0
fi

SYSTEM=0
[ -z "$ROOT" ] && [ "$PREFIX" = /usr ] && SYSTEM=1

if [ "$SYSTEM" = 1 ] && [ "$(id -u)" != 0 ]; then
    echo "Please run as root: sudo $0"
    exit 1
fi

SRC="$(cd "$(dirname "$0")" && pwd)"

install -d "$LIB/zplinux" "$SHARE/icons/hicolor/scalable/actions" "$(dirname "$BIN")"
install -m 644 "$SRC"/zplinux/*.py "$LIB/zplinux/"
# a theme layout: GTK 4 recolors symbolic icons (white on the blue bar) only there
install -m 644 "$SRC"/data/icons/hicolor/scalable/actions/*.svg "$SHARE/icons/hicolor/scalable/actions/"
sed "s|/usr/lib/zplinux|$PREFIX/lib/zplinux|" "$SRC/data/zplinux-launcher" > "$BIN"
chmod 755 "$BIN"
sed '1s|.*|#!/usr/bin/python3|' "$SRC/tools/fake_printer.py" > "$FAKE"
chmod 755 "$FAKE"
install -D -m 644 "$SRC/data/zplinux.desktop" "$DESKTOP"
install -D -m 644 "$SRC/data/zplinux.svg" "$ICON"
install -D -m 644 "$SRC/data/zplinux-mime.xml" "$MIME"

if [ "$SYSTEM" = 1 ]; then
    for c in gtk-update-icon-cache gtk4-update-icon-cache; do
        command -v $c >/dev/null && { $c -q -t /usr/share/icons/hicolor || true; break; }
    done
    command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
    command -v update-mime-database >/dev/null && update-mime-database /usr/share/mime || true
    echo "ZPLinux installed. Launch it from the menu (Office > ZPLinux) or run: zplinux"
fi
