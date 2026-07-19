#!/bin/sh
# Build the menu bar companion into desktop/dist/KVM AI Monitor.app (ad-hoc signed).
# Requires Xcode Command Line Tools. For distribution outside this Mac, re-sign with a
# Developer ID certificate and notarize; see desktop/README.md.
set -eu

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
APP="$DIR/dist/KVM AI Monitor.app"
BINARY="$APP/Contents/MacOS/KVM AI Monitor"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp "$DIR/Info.plist" "$APP/Contents/Info.plist"

swiftc -O -parse-as-library "$DIR/main.swift" -o "$BINARY"
codesign --force --sign - "$APP"

echo "Built: $APP"
echo "Run with: open \"$APP\""
