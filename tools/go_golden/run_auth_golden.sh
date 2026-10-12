#!/usr/bin/env bash
# Regenerate tests/golden/auth/*.golden.json from the real Go authentication functions.
# Needs network access to fetch Go modules (the repo's vendor/ dir is incomplete).
# Usage: tools/go_golden/run_auth_golden.sh /path/to/Threadfin/checkout
set -euo pipefail
GO_SRC=${1:?path to Threadfin Go checkout}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cp -r "$GO_SRC/src" "$GO_SRC/go.mod" "$GO_SRC/go.sum" "$GO_SRC/html" "$WORK/"
cp "$HERE/authdump_pkg.go" "$WORK/src/zz_authdump.go"
mkdir -p "$WORK/cmd/authdump"
cp "$HERE/authdump/main.go" "$WORK/cmd/authdump/main.go"
# vendor/ is incomplete upstream: fetch modules straight from GitHub instead.
cat >> "$WORK/go.mod" <<'MOD'

replace golang.org/x/text => github.com/golang/text v0.21.0
replace golang.org/x/net => github.com/golang/net v0.32.0
replace golang.org/x/sys => github.com/golang/sys v0.28.0
MOD
(cd "$WORK" && GOFLAGS=-mod=mod GOPROXY=direct GOSUMDB=off go run ./cmd/authdump "$ROOT/tests/golden/auth")
