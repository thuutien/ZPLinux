#!/bin/sh
# Build a distro-independent install archive: dist/labelix-<version>.tar.gz
# Users extract it and run: sudo ./install.sh
# This is also the release asset the Arch PKGBUILD downloads.
set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)"
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/labelix/__init__.py")
mkdir -p "$SRC/dist"
OUT="$SRC/dist/labelix-$VERSION.tar.gz"
if git -C "$SRC" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git -C "$SRC" archive --format=tar.gz --prefix="labelix-$VERSION/" -o "$OUT" HEAD
else
    tar -C "$SRC" --exclude=./dist --exclude=./received --exclude='__pycache__' \
        --transform "s|^\.|labelix-$VERSION|" -czf "$OUT" .
fi
echo "$OUT"
sha256sum "$OUT"
