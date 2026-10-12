// m3udump runs the original Threadfin M3U parser over fixture files and
// writes golden JSON next to them. It is copied into the Go checkout
// (src/m3udump/) by run_m3u_golden.sh because the parser lives in an
// internal package.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	m3u "threadfin/src/internal/m3u-parser"
)

type result struct {
	Error   *string             `json:"error"`
	Streams []map[string]string `json:"streams"`
}

func toResult(streams []interface{}, err error) result {
	r := result{Streams: []map[string]string{}}
	if err != nil {
		msg := err.Error()
		r.Error = &msg
	}
	for _, s := range streams {
		r.Streams = append(r.Streams, s.(map[string]string))
	}
	return r
}

func write(path string, r result) {
	b, err := json.MarshalIndent(r, "", "  ") // map keys are sorted by encoding/json
	if err != nil {
		panic(err)
	}
	if err := os.WriteFile(path, append(b, '\n'), 0644); err != nil {
		panic(err)
	}
}

func main() {
	dir, outOptimizedDir := os.Args[1], os.Args[2]
	files, _ := filepath.Glob(filepath.Join(dir, "*.m3u"))
	sort.Strings(files)
	for _, f := range files {
		content, err := os.ReadFile(f)
		if err != nil {
			panic(err)
		}
		base := strings.TrimSuffix(f, ".m3u")
		// Default dispatch: files <= 10MB use the original implementation.
		write(base+".golden.json", toResult(m3u.MakeInterfaceFromM3U(content)))
		// The optimized (>10MB) implementation, recorded only to document divergence.
		name := strings.TrimSuffix(filepath.Base(f), ".m3u")
		write(filepath.Join(outOptimizedDir, name+".optimized.json"), toResult(m3u.MakeInterfaceFromM3UOptimized(content)))
		fmt.Println("dumped", name)
	}
}
