#!/bin/sh
# Build a single-file Flatpak bundle: dist/zplinux-<version>.flatpak
# Needs flatpak-builder (or the org.flatpak.Builder Flatpak) and the GNOME runtime/SDK
# from Flathub. Users install the bundle with: flatpak install --user zplinux-<version>.flatpak
set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)"
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/zplinux/__init__.py")
MANIFEST="$SRC/packaging/flatpak/io.github.thuutien.ZPLinux.yml"
WORK="$SRC/build/flatpak"
if command -v flatpak-builder >/dev/null; then
    BUILDER="flatpak-builder"
else
    BUILDER="flatpak run --filesystem=$SRC org.flatpak.Builder"
fi
mkdir -p "$WORK" "$SRC/dist"
cd "$SRC"
$BUILDER --force-clean --disable-rofiles-fuse --state-dir="$WORK/state" --repo="$WORK/repo" \
    "$WORK/app" "$MANIFEST"
OUT="$SRC/dist/zplinux-$VERSION.flatpak"
flatpak build-bundle --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo \
    "$WORK/repo" "$OUT" io.github.thuutien.ZPLinux
echo "$OUT"
sha256sum "$OUT"
