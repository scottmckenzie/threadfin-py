#!/usr/bin/env bash
# Regenerate tests/golden/probe/*.golden.json from the real Go probeChannel().
# A fake ffprobe prints each *.input.json, so parsing edge cases are exact.
# Usage: tools/go_golden/run_probe_golden.sh /path/to/Threadfin/checkout
set -euo pipefail
GO_SRC=${1:?path to Threadfin Go checkout}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
# Go rewrites the first "ffmpeg" in the path to "ffprobe": keep it out of the dir name.
mkdir -p "$WORK/bin"
touch "$WORK/bin/ffmpeg"
printf '#!/bin/sh\nfor a; do last="$a"; done\ncat "$last"\n' > "$WORK/bin/ffprobe"
chmod +x "$WORK/bin/ffprobe"
mkdir -p "$WORK/go"
cp -r "$GO_SRC/src" "$GO_SRC/go.mod" "$GO_SRC/go.sum" "$GO_SRC/html" "$WORK/go/"
cp "$HERE/probedump_pkg.go" "$WORK/go/src/zz_probedump.go"
mkdir -p "$WORK/go/cmd/probedump"
cp "$HERE/probedump/main.go" "$WORK/go/cmd/probedump/main.go"
cat >> "$WORK/go/go.mod" <<'MOD'

replace golang.org/x/text => github.com/golang/text v0.21.0
replace golang.org/x/net => github.com/golang/net v0.32.0
replace golang.org/x/sys => github.com/golang/sys v0.28.0
MOD
(cd "$WORK/go" && GOFLAGS=-mod=mod GOPROXY=direct GOSUMDB=off go run ./cmd/probedump "$ROOT/tests/golden/probe" "$WORK/bin/ffmpeg")
