package main

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"threadfin/src"
)

func main() {
	dir := os.Args[1]
	files, _ := filepath.Glob(filepath.Join(dir, "*.input.json"))
	sort.Strings(files)
	for _, f := range files {
		name := strings.TrimSuffix(filepath.Base(f), ".input.json")
		work, _ := os.MkdirTemp("", "tfgolden")
		out, err := src.DumpSettings(work, f)
		os.RemoveAll(work)
		target := filepath.Join(dir, name+".golden.json")
		if err != nil {
			// Errors are recorded as a marker file; the message text is Go-specific.
			os.WriteFile(target, []byte("{\"__error__\": true}\n"), 0644)
			fmt.Println("dumped", name, "(error)")
			continue
		}
		os.WriteFile(target, []byte(out), 0644)
		fmt.Println("dumped", name)
	}
}
