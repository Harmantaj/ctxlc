#!/usr/bin/env bash
# Builds the release artifacts into dist/release/ and checks them the way a new user would get them:
# wheel installed into a fresh venv with a throwaway HOME, sdist tests run from the unpacked tarball, and a
# plugin zip with no sync folder baked in. Publishes nothing.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-python3}"
OUT="$ROOT/dist/release"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

step() { printf '\n== %s\n' "$1"; }

step "build wheel and sdist"
rm -rf "$OUT" "$ROOT/build" "$ROOT"/*.egg-info
mkdir -p "$OUT"
"$PY" -m venv "$WORK/buildenv"
"$WORK/buildenv/bin/pip" -q install --upgrade pip build
"$WORK/buildenv/bin/python" -m build --outdir "$OUT" "$ROOT" >/dev/null
rm -rf "$ROOT/build" "$ROOT"/*.egg-info
VERSION="$("$PY" -c "import sys; sys.path.insert(0, '$ROOT'); import ctxlc; print(ctxlc.__version__)")"
WHEEL="$OUT/ctxlc-$VERSION-py3-none-any.whl"
SDIST="$OUT/ctxlc-$VERSION.tar.gz"
ls "$WHEEL" "$SDIST"

step "clean install from the wheel"
"$PY" -m venv "$WORK/v"
"$WORK/v/bin/pip" -q install "$WHEEL"
CTX="$WORK/v/bin/ctx"
PROJ="$WORK/proj"
mkdir -p "$PROJ" "$WORK/home"
(
  export HOME="$WORK/home"
  cd "$PROJ"
  "$CTX" install --project "$PROJ" >/dev/null
  grep -q "$CTX\\\\\" hook" .claude/settings.local.json
  "$CTX" note requirement "Invoice numbers use the prefix INV-2026-" >/dev/null
  HOOK="{\"hook_event_name\":\"SessionStart\",\"source\":\"clear\",\"session_id\":\"s1\",\"cwd\":\"$PROJ\",\"transcript_path\":\"\"}"
  echo "$HOOK" | "$CTX" hook | grep -q "INV-2026-"
  test ! -e .claude/context/errors.log
  "$CTX" uninstall --project "$PROJ" >/dev/null
  test "$(cat .claude/settings.local.json)" = "{}"
)
echo "install, restore and uninstall work"

step "tests from the sdist"
tar xzf "$SDIST" -C "$WORK"
(cd "$WORK/ctxlc-$VERSION" && "$PY" -W ignore -m unittest discover -s tests 2>&1 | tail -1)

step "Cowork plugin zip (no sync folder)"
PYTHONPATH="$ROOT" "$PY" -m ctxlc plugin --out "$OUT/ctxlc-plugin.zip" >/dev/null
if unzip -l "$OUT/ctxlc-plugin.zip" | grep -q plugin_config.json; then
  echo "plugin zip carries a sync folder" >&2; exit 1
fi
if unzip -l "$OUT/ctxlc-plugin.zip" | grep -q " bin/"; then
  echo "plugin zip ships bin/, which claude.ai rejects" >&2; exit 1
fi
unzip -l "$OUT/ctxlc-plugin.zip" | tail -1

step "release artifacts"
ls -l "$OUT"
