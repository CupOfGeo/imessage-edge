#!/usr/bin/env bash
# Build, sign and notarize ImessageEdge.app, the bundle that holds Full
# Disk Access for the Homebrew install. Run on the release Mac (Apple
# Silicon) with a Developer ID Application certificate in the keychain.
#
# Usage:
#   launcher/build.sh <out-dir> [--skip-notarize]
#
# Writes <out-dir>/ImessageEdge.app.zip and prints its sha256.
#
# The grant follows the code requirement (bundle id + Team ID), not the
# path or hash, so a rebuilt bundle keeps the user's grant as long as it
# is signed by the same team. Paths are the stable Homebrew `opt` ones
# for that reason: nothing here may change per version.

set -euo pipefail

out=""
notarize=true
for arg in "$@"; do
  case "$arg" in
    --skip-notarize) notarize=false ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *) out="$arg" ;;
  esac
done
[[ -n "$out" ]] || { echo "usage: $0 <out-dir> [--skip-notarize]" >&2; exit 2; }

here="$(cd "$(dirname "$0")" && pwd)"
version="$(sed -n 's/^version = "\(.*\)"/\1/p' "$here/../pyproject.toml")"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
app="$work/ImessageEdge.app"
mkdir -p "$app/Contents/MacOS" "$out"

cp "$here/Info.plist" "$app/Contents/Info.plist"
plutil -replace CFBundleShortVersionString -string "$version" "$app/Contents/Info.plist"
plutil -replace CFBundleVersion -string "$version" "$app/Contents/Info.plist"

cc -O2 -arch arm64 -o "$app/Contents/MacOS/imessage-edged" \
  -DEDGE_PYTHON='"/opt/homebrew/opt/imessage-edge/libexec/bin/python"' \
  -DEDGE_CONFIG='"/opt/homebrew/etc/imessage-edge/config.env"' \
  "$here/imessage-edged.c"

# Hardened runtime (dyld ignores DYLD_*) is also a notarization requirement.
codesign --force --options runtime --timestamp --sign "Developer ID Application" "$app"
codesign --verify --strict --verbose=2 "$app"

zip="$out/ImessageEdge.app.zip"
rm -f "$zip"
if $notarize; then
  ditto -c -k --keepParent "$app" "$work/submit.zip"
  xcrun notarytool submit "$work/submit.zip" --keychain-profile notary --wait
  xcrun stapler staple "$app"
  spctl --assess --type execute --verbose=2 "$app"
fi
ditto -c -k --keepParent "$app" "$zip"

echo "built $zip ($version)"
shasum -a 256 "$zip" | cut -d' ' -f1
