package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"

	"threadfin/src"
)

// Each case runs in its own process: the Go authentication package keeps its database and
// tokens in package globals, and json.Unmarshal merges into the old map.
func main() {
	dir := os.Args[1]
	if len(os.Args) == 3 {
		f := os.Args[2]
		name := strings.TrimSuffix(filepath.Base(f), ".input.json")
		work, _ := os.MkdirTemp("", "tfgolden")
		out, err := src.DumpAuth(work, f)
		os.RemoveAll(work)
		if err != nil {
			fmt.Println("FAILED", name, err)
			os.Exit(1)
		}
		os.WriteFile(filepath.Join(dir, name+".golden.json"), []byte(out+"\n"), 0644)
		fmt.Println("dumped", name)
		return
	}
	files, _ := filepath.Glob(filepath.Join(dir, "*.input.json"))
	sort.Strings(files)
	for _, f := range files {
		cmd := exec.Command(os.Args[0], dir, f)
		cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
		if err := cmd.Run(); err != nil {
			os.Exit(1)
		}
	}
}
