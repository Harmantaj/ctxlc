#!/usr/bin/env bash
# Builds the release artifacts into dist/release/ and checks them the way a new user would get them:
# wheel installed into a fresh venv with a throwaway HOME, sdist tests run from the unpacked tarball, and a
# plugin zip with no sync folder baked in. Publishes nothing.
set -Eeuo pipefail
trap 'echo "release_check: failed at line $LINENO: $BASH_COMMAND" >&2; cat .claude/settings.local.json .claude/context/errors.log >&2 2>/dev/null || true' ERR
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-python3}"
OUT="$ROOT/dist/release"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
# Windows (Git Bash): venv executables live in Scripts/, and native programs need C:/... paths inside strings.
BIN=bin
native() { printf '%s' "$1"; }
case "${OSTYPE:-}" in msys*|cygwin*) BIN=Scripts; native() { cygpath -m "$1"; } ;; esac

step() { printf '\n== %s\n' "$1"; }

step "build wheel and sdist"
rm -rf "$OUT" "$ROOT/build" "$ROOT"/*.egg-info
mkdir -p "$OUT"
"$PY" -m venv "$WORK/buildenv"
"$WORK/buildenv/$BIN/python" -m pip -q install --upgrade pip build
"$WORK/buildenv/$BIN/python" -m build --outdir "$OUT" "$ROOT" >/dev/null
rm -rf "$ROOT/build" "$ROOT"/*.egg-info
VERSION="$(cd "$ROOT" && "$PY" -c "import ctxlc; print(ctxlc.__version__)")"
WHEEL="$OUT/ctxlc-$VERSION-py3-none-any.whl"
SDIST="$OUT/ctxlc-$VERSION.tar.gz"
ls "$WHEEL" "$SDIST"

step "clean install from the wheel"
"$PY" -m venv "$WORK/v"
"$WORK/v/$BIN/python" -m pip -q install "$WHEEL"
CTX="$WORK/v/$BIN/ctx"
PROJ="$WORK/proj"
mkdir -p "$PROJ" "$WORK/home"
(
  export HOME="$WORK/home" USERPROFILE="$(native "$WORK/home")"
  cd "$PROJ"
  "$CTX" install --project "$PROJ" >/dev/null
  CTX_IN_SETTINGS="$(native "$CTX")"
  # Windows may spell the temp folder as an 8.3 short name (RUNNER~1) on one side only: compare the tail.
  if [ "$BIN" = Scripts ]; then CTX_IN_SETTINGS="/v/Scripts/ctx.exe"; fi
  grep -qF "$CTX_IN_SETTINGS\\\" hook" .claude/settings.local.json
  "$CTX" note requirement "Invoice numbers use the prefix INV-2026-" >/dev/null
  HOOK="{\"hook_event_name\":\"SessionStart\",\"source\":\"clear\",\"session_id\":\"s1\",\"cwd\":\"$(native "$PROJ")\",\"transcript_path\":\"\"}"
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
