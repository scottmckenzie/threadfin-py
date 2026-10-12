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
		out, err := src.DumpEPG(work, f)
		os.RemoveAll(work)
		if err != nil {
			fmt.Println("FAILED", name, err)
			os.Exit(1)
		}
		os.WriteFile(filepath.Join(dir, name+".golden.json"), []byte(out+"\n"), 0644)
		fmt.Println("dumped", name)
	}
}
