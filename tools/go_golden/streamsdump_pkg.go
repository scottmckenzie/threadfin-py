package src

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strconv"
)

type dbProvider struct {
	Kind    string `json:"kind"`
	ID      string `json:"id"`
	Name    string `json:"name"`
	Content string `json:"content"`
	Missing bool   `json:"missing"`
}

type dbCase struct {
	Filters       map[string]map[string]interface{} `json:"filters"`
	IgnoreFilters bool                              `json:"ignoreFilters"`
	Providers     []dbProvider                      `json:"providers"`
}

type dbOut struct {
	Error         bool                      `json:"error"`
	All           []map[string]string       `json:"all"`
	Active        []map[string]string       `json:"active"`
	Inactive      []map[string]string       `json:"inactive"`
	PreviewActive []string                  `json:"previewActive"`
	PreviewInact  []string                  `json:"previewInactive"`
	GroupsText    []string                  `json:"groupsText"`
	GroupsValue   []string                  `json:"groupsValue"`
	Compat        map[string]map[string]int `json:"compatibility"`
}

func toStreams(in []interface{}) []map[string]string {
	out := []map[string]string{}
	for _, s := range in {
		out = append(out, s.(map[string]string))
	}
	// Providers are visited in random map order: sort so output is comparable.
	sort.SliceStable(out, func(i, j int) bool { return out[i]["_file.m3u.id"] < out[j]["_file.m3u.id"] })
	return out
}

// DumpStreams runs the real buildDatabaseDVR() over local provider copies.
func DumpStreams(dir, input string) (string, error) {
	content, err := os.ReadFile(input)
	if err != nil {
		return "", err
	}
	var c dbCase
	if err = json.Unmarshal(content, &c); err != nil {
		return "", err
	}

	System.Name = "Threadfin"
	System.AppName = "threadfin"
	System.Flag.Info = true
	System.PlexChannelLimit = 480
	System.UnfilteredChannelLimit = 480
	System.Folder.Backup = filepath.Join(dir, "backup") + "/"
	System.Folder.Temp = filepath.Join(dir, "tmp") + "/"
	System.Folder.Data = filepath.Join(dir, "data") + "/"
	System.File.Settings = filepath.Join(dir, "settings.json")
	os.MkdirAll(System.Folder.Data, 0755)

	Settings = SettingsStruct{}
	Settings.Files.M3U = map[string]interface{}{}
	Settings.Files.HDHR = map[string]interface{}{}
	Settings.IgnoreFilters = c.IgnoreFilters
	Settings.Filter = map[int64]interface{}{}
	keys := []int{}
	for k := range c.Filters {
		n, _ := strconv.Atoi(k)
		keys = append(keys, n)
	}
	sort.Ints(keys)
	for i, n := range keys {
		Settings.Filter[int64(i)] = c.Filters[strconv.Itoa(n)]
	}

	for _, p := range c.Providers {
		rec := map[string]interface{}{"name": p.Name}
		ext := ".m3u"
		target := Settings.Files.M3U
		if p.Kind == "hdhr" {
			ext = ".json"
			target = Settings.Files.HDHR
		}
		target[p.ID] = rec
		if !p.Missing {
			if err := os.WriteFile(System.Folder.Data+p.ID+ext, []byte(p.Content), 0644); err != nil {
				return "", err
			}
		}
	}

	out := dbOut{Compat: map[string]map[string]int{}}
	if err := buildDatabaseDVR(); err != nil {
		out.Error = true
	}
	out.All = toStreams(Data.Streams.All)
	out.Active = toStreams(Data.Streams.Active)
	out.Inactive = toStreams(Data.Streams.Inactive)
	out.PreviewActive = append([]string{}, Data.StreamPreviewUI.Active...)
	out.PreviewInact = append([]string{}, Data.StreamPreviewUI.Inactive...)
	out.GroupsText = append([]string{}, Data.Playlist.M3U.Groups.Text...)
	out.GroupsValue = append([]string{}, Data.Playlist.M3U.Groups.Value...)
	for _, m := range []map[string]interface{}{Settings.Files.M3U, Settings.Files.HDHR} {
		for id, rec := range m {
			if r, ok := rec.(map[string]interface{}); ok {
				if cm, ok := r["compatibility"].(map[string]int); ok {
					out.Compat[id] = cm
				}
			}
		}
	}
	b, err := json.MarshalIndent(out, "", "  ")
	return string(b), err
}
