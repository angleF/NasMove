#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_root"

python_bin="${NASMOVE_PYTHON:-python3}"
python_bin="$(command -v "$python_bin")"
"$python_bin" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else "NasMove requires Python 3.12")'
shasum -a 256 -c requirements.lock.sha256
deploy_bin="$(dirname "$python_bin")/pyside6-deploy"
test -x "$deploy_bin"

build_dir="$project_root/build/NasMove"
bundle="$project_root/dist/NasMove.app"
rm -rf "$build_dir"
rm -rf "$bundle"
mkdir -p "$build_dir" "$project_root/dist"

escaped_root=${project_root//&/\\&}
escaped_python=${python_bin//&/\\&}
sed -e "s&@PROJECT_ROOT@&$escaped_root&g" \
    -e "s&@PYTHON@&$escaped_python&g" \
    deployment/pysidedeploy.spec > "$build_dir/pysidedeploy.spec"

"$deploy_bin" \
    -c "$build_dir/pysidedeploy.spec" \
    --nuitka-version=4.1.1 \
    --force \
    src/nasmove/ui/desktop_app.py

test -d "$bundle"
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier com.nasmove.app" \
    "$bundle/Contents/Info.plist"

signing_identity="${NASMOVE_SIGNING_IDENTITY:--}"
codesign --force --deep --options runtime \
    --entitlements deployment/entitlements.plist \
    --sign "$signing_identity" "$bundle"
codesign --verify --deep --strict --verbose=2 "$bundle"

if [[ -n "${NASMOVE_NOTARY_PROFILE:-}" ]]; then
    archive="$project_root/dist/NasMove.zip"
    ditto -c -k --keepParent "$bundle" "$archive"
    xcrun notarytool submit "$archive" \
        --keychain-profile "$NASMOVE_NOTARY_PROFILE" --wait
    xcrun stapler staple "$bundle"
fi
