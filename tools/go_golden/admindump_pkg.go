package src

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

type adminCase struct {
	Op       string                 `json:"op"`
	Settings map[string]interface{} `json:"settings"`
	Request  json.RawMessage        `json:"request"`
	Files    []string               `json:"files"`
	Dirs     []string               `json:"dirs"`
}

type adminOut struct {
	UnmarshalErr string                 `json:"unmarshalErr"`
	Err          string                 `json:"err"`
	Panic        string                 `json:"panic"`
	Settings     map[string]interface{} `json:"settings"`
	Dirs         []string               `json:"dirs"`
}

// DumpAdmin runs updateServerSettings / saveFilter (web UI "saveSettings" / "saveFilter") on one
// case in the empty folder work and returns the JSON result: the error and settings.json afterwards.
func DumpAdmin(work, caseFile string) (string, error) {
	raw, err := os.ReadFile(caseFile)
	if err != nil {
		return "", err
	}
	sub := func(s string) string { return strings.ReplaceAll(s, "{work}", work) }
	var c adminCase
	if err := json.Unmarshal([]byte(sub(string(raw))), &c); err != nil {
		return "", err
	}
	sep := string(os.PathSeparator)
	for _, d := range c.Dirs {
		os.MkdirAll(filepath.Join(work, d), 0755)
	}
	for _, f := range c.Files {
		p := filepath.Join(work, f)
		os.MkdirAll(filepath.Dir(p), 0755)
		os.WriteFile(p, []byte("#!/bin/sh\n"), 0755)
	}

	System.Name = "Threadfin"
	System.AppName = "threadfin"
	System.DBVersion = "0.5.0"
	System.Folder.Config = work + sep + "cfg" + sep
	System.Folder.Backup = System.Folder.Config + "backup" + sep
	System.Folder.Data = System.Folder.Config + "data" + sep
	System.Folder.Cache = System.Folder.Config + "cache" + sep
	System.Folder.ImagesCache = System.Folder.Cache + "images" + sep
	System.Folder.ImagesUpload = System.Folder.Data + "images" + sep
	System.Folder.Temp = work + sep + "tmp" + sep
	for _, d := range []string{System.Folder.Backup, System.Folder.Data, System.Folder.ImagesCache, System.Folder.ImagesUpload, System.Folder.Temp} {
		os.MkdirAll(d, 0755)
	}
	System.File.Settings = System.Folder.Config + "settings.json"
	System.File.XEPG = System.Folder.Config + "xepg.json"
	System.File.URLS = System.Folder.Config + "urls.json"
	System.File.PMS = System.Folder.Config + "pms.json"
	System.FFmpeg.DefaultOptions = "-hide_banner -loglevel error -analyzeduration 1000000 -probesize 1000000 -i [URL] -map 0:v -map 0:a:0 -c:v copy -c:a aac -b:a 192k -ac 2 -c:s copy -f mpegts -fflags +genpts -movflags +faststart -copyts pipe:1"
	System.VLC.DefaultOptions = "-I dummy [URL] --sout #std{mux=ts,access=file,dst=-}"
	System.UnfilteredChannelLimit = 480
	System.PlexChannelLimit = 480
	for _, name := range []string{"xepg.json", "urls.json", "pms.json"} {
		os.WriteFile(System.Folder.Config+name, []byte("{}"), 0644)
	}

	initial, _ := json.Marshal(c.Settings)
	if err := os.WriteFile(System.File.Settings, initial, 0644); err != nil {
		return "", err
	}
	if Settings, err = loadSettings(); err != nil {
		return "", err
	}

	var out adminOut
	var request RequestStruct
	if err := json.Unmarshal(c.Request, &request); err != nil {
		out.UnmarshalErr = err.Error()
	} else {
		func() {
			defer func() {
				if r := recover(); r != nil {
					out.Panic = fmt.Sprint(r)
				}
			}()
			switch c.Op {
			case "settings":
				_, err = updateServerSettings(request)
			case "filter":
				_, err = saveFilter(request)
			default:
				err = fmt.Errorf("unknown op %q", c.Op)
			}
			if err != nil {
				out.Err = strings.ReplaceAll(err.Error(), work, "{work}")
			}
		}()
	}

	saved, err := os.ReadFile(System.File.Settings)
	if err != nil {
		return "", err
	}
	if err := json.Unmarshal([]byte(strings.ReplaceAll(string(saved), work, "{work}")), &out.Settings); err != nil {
		return "", err
	}
	filepath.Walk(work, func(path string, info os.FileInfo, err error) error {
		if err == nil && info.IsDir() && path != work {
			rel, _ := filepath.Rel(work, path)
			out.Dirs = append(out.Dirs, filepath.ToSlash(rel))
		}
		return nil
	})
	sort.Strings(out.Dirs)

	b, err := json.MarshalIndent(out, "", "  ")
	return string(b), err
}
