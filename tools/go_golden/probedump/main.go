package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"threadfin/src"
)

type result struct {
	Error         bool   `json:"error"`
	Resolution    string `json:"resolution"`
	FrameRate     string `json:"frame_rate"`
	AudioChannels string `json:"audio_channels"`
}

func main() {
	dir, fakeFFmpeg := os.Args[1], os.Args[2]
	files, _ := filepath.Glob(filepath.Join(dir, "*.input.json"))
	sort.Strings(files)
	for _, f := range files {
		name := strings.TrimSuffix(filepath.Base(f), ".input.json")
		res, fps, audio, err := src.DumpProbe(fakeFFmpeg, f)
		out, _ := json.MarshalIndent(result{err != nil, res, fps, audio}, "", "  ")
		os.WriteFile(filepath.Join(dir, name+".golden.json"), append(out, '\n'), 0644)
	}
}
