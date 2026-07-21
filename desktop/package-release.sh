#!/bin/sh
# Build and package the app artifact consumed by the Homebrew cask.
set -eu

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$DIR/.." && pwd)
VERSION=$(sed -n 's/.*"version": "\([^"]*\)".*/\1/p' "$ROOT/package.json" | head -n 1)
ARCHIVE="$DIR/dist/KVM-AI-Monitor-v$VERSION.zip"

"$DIR/build.sh"
rm -f "$ARCHIVE"
ditto -c -k --keepParent "$DIR/dist/KVM AI Monitor.app" "$ARCHIVE"

echo "Packaged: $ARCHIVE"
shasum -a 256 "$ARCHIVE"
