#!/bin/sh
# Install (or uninstall with --uninstall) Labelix system-wide.
# Usage: sudo ./install.sh [--uninstall] [--prefix-root DIR]
set -e

ROOT=""
UNINSTALL=0
while [ $# -gt 0 ]; do
    case "$1" in
        --uninstall) UNINSTALL=1 ;;
        --prefix-root) ROOT="$2"; shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

LIB="$ROOT/usr/lib/labelix"
SHARE="$ROOT/usr/share/labelix"
BIN="$ROOT/usr/bin/labelix"
FAKE="$ROOT/usr/bin/labelix-fake-printer"
DESKTOP="$ROOT/usr/share/applications/labelix.desktop"
ICON="$ROOT/usr/share/icons/hicolor/scalable/apps/labelix.svg"
MIME="$ROOT/usr/share/mime/packages/labelix.xml"

if [ "$UNINSTALL" = 1 ]; then
    rm -rf "$LIB" "$SHARE" "$BIN" "$FAKE" "$DESKTOP" "$ICON" "$MIME"
    echo "Labelix uninstalled."
    exit 0
fi

if [ -z "$ROOT" ] && [ "$(id -u)" != 0 ]; then
    echo "Please run as root: sudo $0"
    exit 1
fi

SRC="$(cd "$(dirname "$0")" && pwd)"

install -d "$LIB/labelix" "$SHARE/icons"
install -m 644 "$SRC"/labelix/*.py "$LIB/labelix/"
install -m 644 "$SRC"/data/icons/*.svg "$SHARE/icons/"
install -D -m 755 "$SRC/data/labelix-launcher" "$BIN"
sed '1s|.*|#!/usr/bin/python3|' "$SRC/tools/fake_printer.py" > "$FAKE"
chmod 755 "$FAKE"
install -D -m 644 "$SRC/data/labelix.desktop" "$DESKTOP"
install -D -m 644 "$SRC/data/labelix.svg" "$ICON"
install -D -m 644 "$SRC/data/labelix-mime.xml" "$MIME"

if [ -z "$ROOT" ]; then
    command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true
    command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
    command -v update-mime-database >/dev/null && update-mime-database /usr/share/mime || true
    echo "Labelix installed. Launch it from the menu (Office > Labelix) or run: labelix"
fi
