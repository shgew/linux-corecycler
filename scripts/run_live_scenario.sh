#!/usr/bin/env bash
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME="${XDG_RUNTIME_DIR:-/tmp}"
RESULT="${RESULT:-$RUNTIME/cc-result.json}"
RUNNER_PID=""
CAMPAIGN_HOME=""

mkdir -p "$(dirname "$RESULT")"

abort_for_conflict() {
  local backend="$1"
  echo "{\"verdict\":\"ABORT\",\"reason\":\"a conflicting $backend process is already running\"}" >"$RESULT"
  exit 0
}

pgrep -x mprime >/dev/null && abort_for_conflict "mprime"
pgrep -x y-cruncher >/dev/null && abort_for_conflict "y-cruncher"
pgrep -x stress-ng >/dev/null && abort_for_conflict "stress-ng"
pgrep -x stressapptest >/dev/null && abort_for_conflict "stressapptest"
pgrep -f '[s]cripts/live_scenarios.py' >/dev/null && abort_for_conflict "CoreCycler campaign"

cleanup() {
  if [ -n "$RUNNER_PID" ] && kill -0 "$RUNNER_PID" 2>/dev/null; then
    kill -TERM -- "-$RUNNER_PID" 2>/dev/null || true
    sleep 1
    kill -KILL -- "-$RUNNER_PID" 2>/dev/null || true
    wait "$RUNNER_PID" 2>/dev/null || true
  fi
  if [ -n "$CAMPAIGN_HOME" ]; then
    rm -rf -- "$CAMPAIGN_HOME"
  fi
}
trap cleanup EXIT INT TERM

OUT="$(cd "$REPO" && NIXPKGS_ALLOW_UNFREE=1 nix build .#full --impure --no-link --print-out-paths 2>/dev/null)"
if [ -z "$OUT" ]; then
  echo '{"verdict":"ERROR","reason":"could not build .#full"}' >"$RESULT"
  exit 1
fi

CAMPAIGN_HOME="$(mktemp -d "$RUNTIME/corecycler-live.XXXXXX")"

mp=$(nix-store -qR "$OUT" | grep -m1 -- "-mprime-31")
yc=$(nix-store -qR "$OUT" | grep -m1 -- "-y-cruncher-")
sng=$(nix-store -qR "$OUT" | grep -m1 -- "-stress-ng-")
sat=$(nix-store -qR "$OUT" | grep -m1 -- "-stressapptest-")

export CORECYCLER_MPRIME_BIN="$mp/bin/mprime"
export CORECYCLER_Y_CRUNCHER_BIN="$yc/bin/y-cruncher"
export PATH="$sng/bin:$sat/bin:$PATH"

cd "$REPO" || exit 1
rm -f "$RESULT"
setsid timeout 260 nix run nixpkgs#xvfb-run -- -a \
  nix develop .#packages.x86_64-linux.full -c \
  python3 scripts/live_scenarios.py --home "$CAMPAIGN_HOME" "$@" >"$RESULT" 2>"$RUNTIME/cc-scenario.err" &
RUNNER_PID=$!
wait "$RUNNER_PID"
rc=$?
RUNNER_PID=""
FIRST_BYTE=$(head -c1 "$RESULT" || true)
if [ ! -s "$RESULT" ] || ! grep -q '{' <<<"$FIRST_BYTE"; then
  echo "{\"verdict\":\"ERROR\",\"rc\":$rc,\"stderr_tail\":\"$(tail -c 300 "$RUNTIME/cc-scenario.err" | tr '\n' ' ' | sed 's/"/\x27/g')\"}" >"$RESULT"
fi
exit "$rc"
