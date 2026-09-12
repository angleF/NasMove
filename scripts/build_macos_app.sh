#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_root"

signing_identity="${NASMOVE_SIGNING_IDENTITY:-NasMove Local Signing}"
if [[ "$signing_identity" == "-" ]]; then
    echo "NasMove requires a persistent signing identity; ad-hoc signing is not supported." >&2
    exit 1
fi
# Check the identity before removing a previous usable bundle or compiling.
available_identities="$(security find-identity -v -p codesigning)"
if ! resolved_signing_identity="$(/usr/bin/awk -v identity="$signing_identity" '
    $2 == identity || index($0, "\"" identity "\"") {
        if (found && resolved != $2) {
            ambiguous = 1
        }
        found = 1
        resolved = $2
    }
    END {
        if (!found || ambiguous) {
            exit 1
        }
        print resolved
    }
' <<< "$available_identities")"; then
    echo "No valid signing identity found: $signing_identity" >&2
    echo "Create a persistent code-signing identity or set NASMOVE_SIGNING_IDENTITY." >&2
    echo "The existing application bundle has been preserved." >&2
    exit 1
fi

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

codesign --force --deep --options runtime \
    --entitlements deployment/entitlements.plist \
    --sign "$resolved_signing_identity" "$bundle"
codesign --verify --deep --strict --verbose=2 "$bundle"

if [[ -n "${NASMOVE_NOTARY_PROFILE:-}" ]]; then
    archive="$project_root/dist/NasMove.zip"
    ditto -c -k --keepParent "$bundle" "$archive"
    xcrun notarytool submit "$archive" \
        --keychain-profile "$NASMOVE_NOTARY_PROFILE" --wait
    xcrun stapler staple "$bundle"
fi
