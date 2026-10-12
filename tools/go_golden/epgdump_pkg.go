package src

import (
	"encoding/json"
	"os"
	"path/filepath"
	"time"

	"threadfin/src/internal/imgcache"
)

type epgCase struct {
	TZ       string                 `json:"tz"`
	Settings map[string]interface{} `json:"settings"`
	XMLTV    map[string]string      `json:"xmltv"`
	XEPG     map[string]interface{} `json:"xepg"`
}

type epgOut struct {
	GeneratedAt string `json:"generatedAt"`
	TZ          string `json:"tz"`
	XML         string `json:"xml"`
}

// DumpEPG runs the real createXMLTVFile() over prepared XMLTV files and an XEPG database.
// Go reads the clock itself, so the moment of generation is recorded for the Python side.
func DumpEPG(dir, input string) (string, error) {
	content, err := os.ReadFile(input)
	if err != nil {
		return "", err
	}
	var c epgCase
	if err = json.Unmarshal(content, &c); err != nil {
		return "", err
	}
	loc, err := time.LoadLocation(c.TZ)
	if err != nil {
		return "", err
	}
	time.Local = loc

	System.Name = "Threadfin"
	System.Version = "1.0.0"
	System.Branch = "main"
	System.Flag.Info = true
	System.UnfilteredChannelLimit = 480
	System.Folder.Data = filepath.Join(dir, "data") + "/"
	System.Folder.ImagesCache = filepath.Join(dir, "images") + "/"
	System.File.XML = filepath.Join(dir, "threadfin.xml")
	System.Compressed.GZxml = filepath.Join(dir, "threadfin.xml.gz")
	os.MkdirAll(System.Folder.Data, 0755)
	os.MkdirAll(System.Folder.ImagesCache, 0755)
	for name, body := range c.XMLTV {
		if err := os.WriteFile(System.Folder.Data+name, []byte(body), 0644); err != nil {
			return "", err
		}
	}

	settingsJSON, _ := json.Marshal(c.Settings)
	Settings = SettingsStruct{}
	json.Unmarshal(settingsJSON, &Settings)
	Data.Cache.Images, _ = imgcache.New(System.Folder.ImagesCache, "http://localhost/images/", false)
	Data.Cache.XMLTV = make(map[string]XMLTV)
	Data.XEPG.Channels = c.XEPG
	Data.XMLTV.Files = []string{"x"}

	now := time.Now()
	if err := createXMLTVFile(); err != nil {
		return "", err
	}
	xmlBytes, err := os.ReadFile(System.File.XML)
	if err != nil {
		return "", err
	}
	b, err := json.MarshalIndent(epgOut{now.Format(time.RFC3339Nano), c.TZ, string(xmlBytes)}, "", "  ")
	return string(b), err
}
