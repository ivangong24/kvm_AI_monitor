#!/bin/sh
# Build a universal menu bar companion into desktop/dist/KVM AI Monitor.app.
# KVM_CODESIGN_IDENTITY can name a Developer ID identity; otherwise the local build is ad-hoc
# signed. Distribution builds must still be notarized (see desktop/README.md).
set -eu

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
APP="$DIR/dist/KVM AI Monitor.app"
BINARY="$APP/Contents/MacOS/KVM AI Monitor"
BUILD_DIR=$(mktemp -d "${TMPDIR:-/tmp}/kvm-ai-monitor-desktop.XXXXXX")
trap 'rm -rf "$BUILD_DIR"' EXIT INT TERM

# Prefer a full Xcode install when present. A just-updated Command Line Tools install can briefly
# contain a compiler/SDK version mismatch; Xcode keeps the matching pair together.
if [ -d /Applications/Xcode.app/Contents/Developer ]; then
    DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
    export DEVELOPER_DIR
fi
CLANG_MODULE_CACHE_PATH="$BUILD_DIR/clang-cache"
SWIFT_MODULECACHE_PATH="$BUILD_DIR/swift-cache"
export CLANG_MODULE_CACHE_PATH SWIFT_MODULECACHE_PATH
SDK=$(xcrun --sdk macosx --show-sdk-path)

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$DIR/Info.plist" "$APP/Contents/Info.plist"

xcrun swift "$DIR/make-icon.swift" "$BUILD_DIR/AppIcon-1024.png"
ASSET_CATALOG="$BUILD_DIR/Assets.xcassets"
ICONSET="$ASSET_CATALOG/AppIcon.appiconset"
mkdir -p "$ICONSET"
cp "$DIR/AppIcon.appiconset/Contents.json" "$ICONSET/Contents.json"
for spec in "16:icon_16x16.png" "32:icon_16x16@2x.png" "32:icon_32x32.png" \
            "64:icon_32x32@2x.png" "128:icon_128x128.png" "256:icon_128x128@2x.png" \
            "256:icon_256x256.png" "512:icon_256x256@2x.png" "512:icon_512x512.png" \
            "1024:icon_512x512@2x.png"; do
    pixels=${spec%%:*}
    name=${spec#*:}
    sips -z "$pixels" "$pixels" "$BUILD_DIR/AppIcon-1024.png" --out "$ICONSET/$name" >/dev/null
done
xcrun actool "$ASSET_CATALOG" --compile "$APP/Contents/Resources" --platform macosx \
    --minimum-deployment-target 13.0 --app-icon AppIcon \
    --output-partial-info-plist "$BUILD_DIR/asset-info.plist" >/dev/null

# Bundle the provider logos so the companion app's live touchscreen can draw them.
mkdir -p "$APP/Contents/Resources/providers"
cp "$DIR/../kvm-agent/providers/"*.png "$APP/Contents/Resources/providers/"

for arch in arm64 x86_64; do
    xcrun swiftc -O -parse-as-library -sdk "$SDK" -target "$arch-apple-macosx13.0" \
        "$DIR/main.swift" -o "$BUILD_DIR/KVM-AI-Monitor-$arch"
done
xcrun lipo -create "$BUILD_DIR/KVM-AI-Monitor-arm64" "$BUILD_DIR/KVM-AI-Monitor-x86_64" \
    -output "$BINARY"

IDENTITY=${KVM_CODESIGN_IDENTITY:--}
if [ "$IDENTITY" = "-" ]; then
    codesign --force --sign - "$APP"
else
    codesign --force --options runtime --timestamp --sign "$IDENTITY" "$APP"
fi

echo "Built: $APP"
echo "Architectures: $(xcrun lipo -archs "$BINARY")"
echo "Run with: open \"$APP\""
