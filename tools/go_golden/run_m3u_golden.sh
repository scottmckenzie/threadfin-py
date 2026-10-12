#!/usr/bin/env bash
# Regenerate tests/golden/m3u/*.golden.json from the real Go parser.
# Usage: tools/go_golden/run_m3u_golden.sh /path/to/Threadfin/checkout
set -euo pipefail
GO_SRC=${1:?path to Threadfin Go checkout}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cp -r "$GO_SRC/src" "$GO_SRC/go.mod" "$WORK/"
mkdir -p "$WORK/src/m3udump" "$ROOT/tests/golden/m3u_optimized"
cp "$HERE/m3udump.go" "$WORK/src/m3udump/main.go"
# Only stdlib is needed by the parser, so no module downloads are required.
sed -i '/^require/,$d' "$WORK/go.mod"
(cd "$WORK" && go run -mod=mod ./src/m3udump "$ROOT/tests/golden/m3u" "$ROOT/tests/golden/m3u_optimized")
