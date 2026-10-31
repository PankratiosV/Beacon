#!/bin/bash
# Builds the Mac installer in one go:
#   1) bundles the app into dist/Beacon.app (no Python needed on customers' Macs)
#   2) wraps it into installer/Output/Beacon-<version>.dmg (drag to Applications)
#      and installer/Output/Beacon-<version>.pkg (a classic installer)
# Needs (once): Python 3.12 or 3.13 from python.org and the Xcode Command Line
# Tools (xcode-select --install). The bundling runs in its own environment
# (build/buildenv-mac) with the versions in requirements-mac.txt.
#
# The app is built for this Mac's processor (Intel or Apple Silicon). Build
# on each kind of Mac for both, or run it on GitHub's macos runners.
#
# Code signing (optional; without it macOS says the app is from an
# unidentified developer). With an Apple Developer ID set:
#   export SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
#   export INSTALLER_IDENTITY="Developer ID Installer: Your Name (TEAMID)"
# and to notarize too (after `xcrun notarytool store-credentials beacon`):
#   export NOTARY_PROFILE=beacon
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=$(sed -n 's/^APP_VERSION = "\([^"]*\)".*/\1/p' laser_status_app.py)
BUNDLE_ID="com.beacon.lasertools"
ENV=build/buildenv-mac
PY="$ENV/bin/python"
OUT=installer/Output

if [ ! -x "$PY" ]; then
    echo "Creating the build environment - first time only..."
    BASE=""
    for p in python3.13 python3.12 /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
             /Library/Frameworks/Python.framework/Versions/3.12/bin/python3; do
        if command -v "$p" >/dev/null 2>&1; then BASE="$p"; break; fi
    done
    if [ -z "$BASE" ]; then
        echo "Python 3.12 or 3.13 isn't installed. Get it from https://www.python.org/downloads/macos/"
        exit 1
    fi
    "$BASE" -m venv "$ENV"
    "$PY" -m pip install --upgrade pip
    "$PY" -m pip install -r installer/requirements-mac.txt pyinstaller==6.22.3
fi

echo "[1/4] Making the icon..."
mkdir -p build
"$PY" - <<'EOF'
from PIL import Image
ico = Image.open("laser_status.ico").ico
img = ico.getimage(max(ico.sizes()))   # the largest picture in the .ico
img = img.convert("RGBA").resize((1024, 1024), Image.LANCZOS)
img.save("build/Beacon.icns")
EOF

echo "[2/4] Bundling the app..."
# --windowed makes a .app; --add-data ships the .ico (the menu-bar icon) next
# to the code, found via sys._MEIPASS; --collect-data customtkinter: its
# themes and fonts.
"$PY" -m PyInstaller --noconfirm --clean --windowed \
    --name "Beacon" \
    --icon "$PWD/build/Beacon.icns" \
    --osx-bundle-identifier "$BUNDLE_ID" \
    --add-data "$PWD/laser_status.ico:." \
    --collect-data customtkinter \
    --hidden-import beacon.macsys --hidden-import beacon.maccapture \
    --hidden-import Vision --hidden-import ScreenCaptureKit --hidden-import UserNotifications \
    --specpath build \
    laser_status_app.py

# Info.plist: version, minimum macOS (ScreenCaptureKit needs 12.3) and the
# sentences macOS shows when it asks for the camera and the local network
# (the ESP32 light, smart plugs and the phone dashboard).
PLIST=dist/Beacon.app/Contents/Info.plist
set_key() { /usr/libexec/PlistBuddy -c "Delete :$1" "$PLIST" 2>/dev/null || true
            /usr/libexec/PlistBuddy -c "Add :$1 $2 $3" "$PLIST"; }
set_key CFBundleShortVersionString string "$VERSION"
set_key CFBundleVersion string "$VERSION"
set_key CFBundleDisplayName string "Beacon"
set_key LSMinimumSystemVersion string "12.3"
set_key NSHighResolutionCapable bool true
set_key NSCameraUsageDescription string "Beacon can attach a webcam picture to job-finished messages."
set_key NSLocalNetworkUsageDescription string "Beacon talks to your Wi-Fi stack light, smart plugs and phone dashboard on your local network."
set_key NSHumanReadableCopyright string "Copyright (c) 2026 the Beacon project. PolyForm Noncommercial 1.0.0."

echo "[3/4] Signing..."
# Changing Info.plist breaks PyInstaller's signature, so the app is always
# signed again: with your Developer ID if set, else ad hoc ("-"), which macOS
# accepts for apps the user opens with Open Anyway.
if [ -n "${SIGN_IDENTITY:-}" ]; then
    codesign --force --deep --options runtime --timestamp --sign "$SIGN_IDENTITY" dist/Beacon.app
else
    codesign --force --deep --sign - dist/Beacon.app
fi

echo "[4/4] Building the installers..."
mkdir -p "$OUT"
DMG="$OUT/Beacon-$VERSION.dmg"
PKG="$OUT/Beacon-$VERSION.pkg"
STAGE=build/dmg
rm -rf "$STAGE" "$DMG" "$PKG"
mkdir -p "$STAGE"
ditto dist/Beacon.app "$STAGE/Beacon.app"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "Beacon $VERSION" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null

if [ -n "${INSTALLER_IDENTITY:-}" ]; then
    pkgbuild --component dist/Beacon.app --install-location /Applications \
             --identifier "$BUNDLE_ID" --version "$VERSION" --sign "$INSTALLER_IDENTITY" "$PKG"
else
    pkgbuild --component dist/Beacon.app --install-location /Applications \
             --identifier "$BUNDLE_ID" --version "$VERSION" "$PKG"
fi

if [ -n "${SIGN_IDENTITY:-}" ]; then
    codesign --force --sign "$SIGN_IDENTITY" "$DMG"
fi
if [ -n "${NOTARY_PROFILE:-}" ]; then
    for f in "$DMG" "$PKG"; do
        xcrun notarytool submit "$f" --keychain-profile "$NOTARY_PROFILE" --wait
        xcrun stapler staple "$f"
    done
fi

echo
echo "Done: $OUT"
ls -1 "$OUT"
