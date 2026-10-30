#!/usr/bin/env bash
# Build "<TITLE>.app" (a notifier applet with the project icon) into ~/Applications. TITLE comes from config.py.
# Notifications sent through it show the Bubble Detector icon instead of Script Editor's.
# Requires: macOS, plus rsvg-convert (brew install librsvg) or Google Chrome to rasterise the SVG icon. Re-run after changing assets/icons/icon.svg.
set -euo pipefail
[ "$(uname -s)" = "Darwin" ] || { echo "error: the notifier app is macOS-only" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$HERE/.venv/bin/python"; [ -x "$PY" ] || PY=python3
TITLE="$(cd "$HERE" && "$PY" -c 'from bubble_detector import config as c; print(c.TITLE)')"
APP_DIR="$HOME/Applications"; APP="$APP_DIR/$TITLE.app"
SVG="$HERE/assets/icons/icon.svg"; BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
mkdir -p "$APP_DIR"

# 1. SVG -> 1024px PNG with transparent background (rsvg-convert if present, else headless Chrome)
if command -v rsvg-convert >/dev/null 2>&1; then
  rsvg-convert -w 1024 -h 1024 "$SVG" -o "$BUILD/icon_1024.png"
elif [ -x "$CHROME" ]; then
  cat > "$BUILD/icon.html" <<HTML
<!doctype html><html><head><style>html,body{margin:0;background:transparent}img{display:block;width:1024px;height:1024px}</style></head>
<body><img src="file://$SVG"></body></html>
HTML
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --default-background-color=00000000 \
    --window-size=1024,1024 --screenshot="$BUILD/icon_1024.png" "file://$BUILD/icon.html" >/dev/null 2>&1
else
  echo "error: need rsvg-convert (brew install librsvg) or Google Chrome to rasterise the icon" >&2; exit 1
fi
[ -s "$BUILD/icon_1024.png" ] || { echo "error: icon rasterisation failed" >&2; exit 1; }

# 2. iconset -> icns
ICONSET="$BUILD/icon.iconset"; mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s "$BUILD/icon_1024.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  d=$((s*2)); sips -z $d $d "$BUILD/icon_1024.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$BUILD/icon.icns"
cp "$BUILD/icon.icns" "$HERE/assets/icons/icon.icns"

# 3. compile the applet and brand it
rm -rf "$APP"
osacompile -o "$APP" "$HERE/assets/notifier.applescript"
PLIST="$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier com.bubble-detector.notifier" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string com.bubble-detector.notifier" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleName $TITLE" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleName string $TITLE" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleDisplayName string $TITLE" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$PLIST" 2>/dev/null || true   # no Dock icon while it runs
# modern applets ship an asset catalog (Assets.car) referenced by CFBundleIconName; drop it so our .icns is used
/usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$PLIST" 2>/dev/null || true
rm -f "$APP/Contents/Resources/Assets.car"
ICON_NAME="$(/usr/libexec/PlistBuddy -c "Print :CFBundleIconFile" "$PLIST" 2>/dev/null || echo applet.icns)"
[[ "$ICON_NAME" == *.icns ]] || ICON_NAME="$ICON_NAME.icns"
cp "$BUILD/icon.icns" "$APP/Contents/Resources/$ICON_NAME"
codesign --force --sign - "$APP" >/dev/null 2>&1 || true
touch "$APP"
echo "Built $APP"
echo "Test:  BD_TITLE=\"$TITLE\" BD_SUBTITLE=Test BD_MESSAGE=\"Hello\" \"$APP/Contents/MacOS/applet\""
