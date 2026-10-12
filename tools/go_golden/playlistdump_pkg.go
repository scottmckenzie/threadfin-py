package src

import (
	"encoding/json"
	"os"
	"path/filepath"

	"threadfin/src/internal/imgcache"
)

type playlistCase struct {
	Settings    map[string]interface{} `json:"settings"`
	GroupLabels []string               `json:"groupLabels"`
	Groups      []string               `json:"groups"`
	XEPG        map[string]interface{} `json:"xepg"`
}

type playlistOut struct {
	M3U      string                 `json:"m3u"`
	Registry map[string]StreamInfo `json:"registry"`
}

// DumpPlaylist runs the real buildM3U() and returns the playlist plus the stream registry it filled.
func DumpPlaylist(dir, input string) (string, error) {
	content, err := os.ReadFile(input)
	if err != nil {
		return "", err
	}
	var c playlistCase
	if err = json.Unmarshal(content, &c); err != nil {
		return "", err
	}
	System.Name = "Threadfin"
	System.Domain = "localhost:34400"
	System.ServerProtocol.M3U = "http"
	System.ServerProtocol.XML = "http"
	System.Folder.Data = filepath.Join(dir, "data") + "/"
	System.Folder.ImagesCache = filepath.Join(dir, "images") + "/"
	os.MkdirAll(System.Folder.Data, 0755)
	os.MkdirAll(System.Folder.ImagesCache, 0755)
	settingsJSON, _ := json.Marshal(c.Settings)
	Settings = SettingsStruct{}
	json.Unmarshal(settingsJSON, &Settings)
	Data.Cache.Images, _ = imgcache.New(System.Folder.ImagesCache, "http://localhost/images/", false)
	Data.Cache.StreamingURLS = make(map[string]StreamInfo)
	Data.XEPG.Channels = c.XEPG
	Data.Playlist.M3U.Groups.Text = c.GroupLabels

	m3u, err := buildM3U(c.Groups)
	if err != nil {
		return "", err
	}
	if len(c.Groups) == 0 {
		b, err := os.ReadFile(System.Folder.Data + "threadfin.m3u")
		if err != nil {
			return "", err
		}
		m3u = string(b)
	}
	b, err := json.MarshalIndent(playlistOut{m3u, Data.Cache.StreamingURLS}, "", "  ")
	return string(b), err
}
