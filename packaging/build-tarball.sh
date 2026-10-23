#!/bin/sh
# Build a distro-independent install archive: dist/zplinux-<version>.tar.gz
# Users extract it and run: sudo ./install.sh
# This is also the release asset the Arch PKGBUILD downloads.
set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)"
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/zplinux/__init__.py")
mkdir -p "$SRC/dist"
OUT="$SRC/dist/zplinux-$VERSION.tar.gz"
if git -C "$SRC" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git -C "$SRC" archive --format=tar.gz --prefix="zplinux-$VERSION/" -o "$OUT" HEAD
else
    tar -C "$SRC" --exclude=./dist --exclude=./received --exclude='__pycache__' \
        --transform "s|^\.|zplinux-$VERSION|" -czf "$OUT" .
fi
echo "$OUT"
sha256sum "$OUT"
