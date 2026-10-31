#!/bin/bash
# Builds the Mac installer in one go:
#   1) bundles the app into dist/Beacon.app (no Python needed on customers' Macs)
#   2) wraps it into installer/Output/Beacon-<version>.pkg, an installer with
#      welcome, license and "finish setting up" pages that opens Beacon when
#      done, and installer/Output/Beacon-<version>.dmg (drag to Applications)
# Needs (once): Python 3.12 or 3.13 from python.org and the Xcode Command Line
# Tools (xcode-select --install). The bundling runs in its own environment
# (build/buildenv-mac) with the versions in requirements-mac.txt.
#
# The app is universal: it runs natively on Apple Silicon and Intel Macs,
# whichever kind builds it. python.org's Python and pyobjc already are; the
# packages that only publish one-processor wheels (Pillow, psutil) are
# downloaded for both and fused into one (delocate-merge).
#
# Code signing. macOS remembers permissions (Screen Recording above all) by
# the app's signature. Ad hoc, each build has a new identity and users must
# allow Beacon again after every update, so the app is signed with the
# self-signed certificate "Beacon Self-Signed" when it's in the keychain
# (its identity stays the same from build to build; keep a backup of it, see
# the README). macOS still calls the app's developer unidentified; only an
# Apple Developer ID changes that. With one set:
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
    "$PY" -m pip install -r installer/requirements-mac.txt pyinstaller==6.22.3 delocate==0.13.0
fi

# Universal Pillow and psutil (see the top). Skipped once they are.
if ! lipo -archs "$(ls "$ENV"/lib/python3.*/site-packages/PIL/_imaging.*.so)" | grep -q arm64 ||
   ! lipo -archs "$(ls "$ENV"/lib/python3.*/site-packages/PIL/_imaging.*.so)" | grep -q x86_64 ||
   ! lipo -archs "$(ls "$ENV"/lib/python3.*/site-packages/psutil/_psutil_osx*.so)" | grep -q arm64 ||
   ! lipo -archs "$(ls "$ENV"/lib/python3.*/site-packages/psutil/_psutil_osx*.so)" | grep -q x86_64; then
    echo "Making Pillow and psutil universal - first time only..."
    "$PY" -m pip install delocate==0.13.0
    PYVER=$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
    W=build/wheels
    rm -rf "$W"
    for arch in arm64 x86_64; do
        "$PY" -m pip download --only-binary=:all: --no-deps --python-version "$PYVER" \
            --platform "macosx_11_0_$arch" --platform "macosx_10_15_$arch" \
            --platform "macosx_10_13_$arch" --platform "macosx_10_9_$arch" \
            -d "$W/$arch" $(grep -E '^(pillow|psutil)==' installer/requirements-mac.txt)
    done
    for pkg in pillow psutil; do
        "$ENV/bin/delocate-merge" "$W"/arm64/$pkg-*.whl "$W"/x86_64/$pkg-*.whl -w "$W/universal2"
    done
    "$PY" -m pip install --force-reinstall --no-deps "$W"/universal2/*.whl
fi

if ! ls firmware/laser_stack_light-*.hex >/dev/null 2>&1; then
    echo "firmware/ is empty: run installer/build_firmware.sh first."
    exit 1
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
# --windowed makes a .app; --target-arch universal2 checks every part runs on
# both processors; --add-data ships the .ico (the menu-bar icon) next to the
# code, found via sys._MEIPASS, and so does the Arduino firmware Install on
# Arduino uploads (firmware/) and the sketches and wiring guide (Arduino/);
# --collect-data customtkinter: its themes and
# fonts; certifi: the trusted certificates HTTPS needs (see SSL_CERT_FILE in
# laser_status_app.py).
"$PY" -m PyInstaller --noconfirm --clean --windowed --target-arch universal2 \
    --name "Beacon" \
    --icon "$PWD/build/Beacon.icns" \
    --osx-bundle-identifier "$BUNDLE_ID" \
    --add-data "$PWD/laser_status.ico:." \
    --add-data "$PWD/firmware:firmware" \
    --add-data "$PWD/laser_stack_light/laser_stack_light.ino:Arduino/laser_stack_light" \
    --add-data "$PWD/esp32_stack_light/esp32_stack_light.ino:Arduino/esp32_stack_light" \
    --add-data "$PWD/docs/hardware-kit.md:Arduino" \
    --collect-data customtkinter --collect-data certifi \
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
# signed again: with your Developer ID if set; else with the self-signed
# certificate (no hardened runtime: without a Team ID it would refuse to load
# the app's own libraries); else ad hoc ("-"). macOS accepts all three for
# apps the user opens with Open Anyway.
SELF_SIGNED="Beacon Self-Signed"
if [ -n "${SIGN_IDENTITY:-}" ]; then
    codesign --force --deep --options runtime --timestamp --sign "$SIGN_IDENTITY" dist/Beacon.app
elif security find-certificate -c "$SELF_SIGNED" >/dev/null 2>&1; then
    codesign --force --deep --sign "$SELF_SIGNED" dist/Beacon.app
else
    echo "WARNING: \"$SELF_SIGNED\" isn't in the keychain, so the app is signed ad hoc and users"
    echo "         will have to allow Screen Recording again after updating to this build."
    codesign --force --deep --sign - dist/Beacon.app
fi
codesign -d -r- dist/Beacon.app 2>&1 | grep designated

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

# The .pkg is built in two steps. pkgbuild packs the app with the
# preinstall / postinstall scripts (installer/mac/scripts). It's told the app
# is not relocatable: otherwise, if a Beacon.app exists anywhere else on the
# Mac, Installer "upgrades" that copy instead of putting one in
# /Applications. Then productbuild wraps it in the installer pages
# (installer/mac/*.html and the license).
ROOT=build/pkgroot
rm -rf "$ROOT" build/pkg
mkdir -p "$ROOT" build/pkg/resources
ditto dist/Beacon.app "$ROOT/Beacon.app"
pkgbuild --analyze --root "$ROOT" build/pkg/component.plist >/dev/null
plutil -replace 0.BundleIsRelocatable -bool NO build/pkg/component.plist
pkgbuild --root "$ROOT" --component-plist build/pkg/component.plist \
         --install-location /Applications --scripts installer/mac/scripts \
         --identifier "$BUNDLE_ID" --version "$VERSION" build/pkg/Beacon-app.pkg >/dev/null
cp installer/mac/welcome.html installer/mac/conclusion.html installer/license.txt \
   build/pkg/resources/
cat > build/pkg/distribution.xml <<EOF
<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="2">
    <title>Beacon $VERSION</title>
    <welcome file="welcome.html" mime-type="text/html"/>
    <license file="license.txt"/>
    <conclusion file="conclusion.html" mime-type="text/html"/>
    <options customize="never" require-scripts="false" hostArchitectures="arm64,x86_64"/>
    <domains enable_anywhere="false" enable_currentUserHome="false" enable_localSystem="true"/>
    <volume-check>
        <allowed-os-versions><os-version min="12.3"/></allowed-os-versions>
    </volume-check>
    <choices-outline>
        <line choice="beacon"/>
    </choices-outline>
    <choice id="beacon" title="Beacon">
        <pkg-ref id="$BUNDLE_ID"/>
    </choice>
    <pkg-ref id="$BUNDLE_ID" version="$VERSION" onConclusion="none">Beacon-app.pkg</pkg-ref>
</installer-gui-script>
EOF
productbuild --distribution build/pkg/distribution.xml --resources build/pkg/resources \
             --package-path build/pkg ${INSTALLER_IDENTITY:+--sign "$INSTALLER_IDENTITY"} "$PKG"

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
