#!/bin/bash
# Build dummyRF.app with PyInstaller and install it into ~/Applications.
#
#   packaging/build_app.sh          -> ~/Applications/dummyRF.app
#   packaging/build_app.sh --zip    -> also a .zip to copy to another Mac
#
# Builds outside the project on purpose: in an iCloud/OneDrive-synced folder the
# sync attributes on the files break macOS code signing, and an unsigned app
# won't start on Apple Silicon.  Override with DUMMYRF_APP_DIR / DUMMYRF_BUILD_DIR.
#
# Uses the virtualenv in $VIRTUAL_ENV, else ~/.venvs/dummyrf.  PyInstaller is
# installed into it if missing.  The app is a snapshot: rebuild after changes.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${VIRTUAL_ENV:-$HOME/.venvs/dummyrf}"
PY="$VENV/bin/python"
DATA="dummyrf/data"
WORK="${DUMMYRF_BUILD_DIR:-$HOME/Library/Caches/dummyRF-build}"
DEST="${DUMMYRF_APP_DIR:-$HOME/Applications}"

if [[ ! -x "$PY" ]]; then
  echo "No virtualenv at $VENV — create it first (see README → Install)." >&2
  exit 1
fi
if ! "$PY" -c "import PyInstaller" 2>/dev/null; then
  echo "Installing PyInstaller into $VENV …"
  "$PY" -m pip install --quiet pyinstaller
fi

cd "$ROOT"
VERSION="$("$PY" -c 'import tomllib;print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')"
ARCH="$(uname -m)"
echo "Building dummyRF $VERSION for macOS $ARCH …"

# --paths     : dummyrf is installed in editable mode, which PyInstaller can't
#               follow; without this the app contains no dummyRF at all.
# --add-data  : only the runtime data, not the UKE source CSVs.  Absolute
#               paths: PyInstaller resolves relative ones from --specpath.
"$PY" -m PyInstaller --noconfirm --clean --log-level WARN --windowed \
  --name dummyRF \
  --osx-bundle-identifier com.dummyrf.app \
  --paths "$ROOT" \
  --add-data "$ROOT/$DATA/devices.json:$DATA" \
  --add-data "$ROOT/$DATA/channel_plans.json:$DATA" \
  --add-data "$ROOT/$DATA/transmitters_pl.json:$DATA" \
  --add-data "$ROOT/$DATA/postcodes_pl.tsv.gz:$DATA" \
  --exclude-module PySide6.QtWebEngineCore \
  --exclude-module PySide6.QtWebEngineWidgets \
  --exclude-module PySide6.Qt3DCore \
  --distpath "$WORK/dist" --workpath "$WORK/build" --specpath "$WORK" \
  "$ROOT/packaging/dummyRF_launcher.py"

APP="$WORK/dist/dummyRF.app"
# Fail loudly if dummyRF itself didn't make it into the bundle.
if grep -q "missing module named dummyrf" "$WORK/build/dummyRF/warn-dummyRF.txt" 2>/dev/null; then
  echo "ERROR: dummyrf package missing from the build." >&2
  exit 1
fi
for f in devices.json channel_plans.json transmitters_pl.json postcodes_pl.tsv.gz; do
  [[ -f "$APP/Contents/Resources/$DATA/$f" ]] || { echo "ERROR: $f missing from $APP" >&2; exit 1; }
done

if ! codesign --verify --deep --strict "$APP" 2>/dev/null; then
  echo "ERROR: the app's signature is invalid — is DUMMYRF_BUILD_DIR in a synced folder?" >&2
  exit 1
fi

mkdir -p "$DEST"
rm -rf "$DEST/dummyRF.app"
ditto "$APP" "$DEST/dummyRF.app"
if ! codesign --verify --deep --strict "$DEST/dummyRF.app" 2>/dev/null; then
  echo "ERROR: signature broke when copying to $DEST (synced folder?). The working build is in $APP" >&2
  exit 1
fi
echo "Installed $DEST/dummyRF.app ($(du -sh "$DEST/dummyRF.app" | cut -f1)) — open it from Launchpad or Finder"

if [[ "${1:-}" == "--zip" ]]; then
  ZIP="$WORK/dummyRF-$VERSION-macos-$ARCH.zip"
  rm -f "$ZIP"
  ditto -c -k --keepParent "$APP" "$ZIP"
  echo "Zipped    $ZIP ($(du -sh "$ZIP" | cut -f1))"
fi
