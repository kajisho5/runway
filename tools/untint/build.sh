#!/bin/bash
# Untint.app を作る（Apple Silicon と Intel の両対応）。mac-notarize から呼ばれる。署名は runway 側で行う
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="$here/build"
rm -rf "$out"
mkdir -p "$out/Untint.app/Contents/MacOS"
for arch in arm64 x86_64; do
  xcrun swiftc -O -target "${arch}-apple-macos11.0" -o "$out/Untint-$arch" "$here/main.swift"
done
lipo -create "$out/Untint-arm64" "$out/Untint-x86_64" -output "$out/Untint.app/Contents/MacOS/Untint"
cp "$here/Info.plist" "$out/Untint.app/Contents/Info.plist"
plutil -lint "$out/Untint.app/Contents/Info.plist"
lipo -info "$out/Untint.app/Contents/MacOS/Untint"
