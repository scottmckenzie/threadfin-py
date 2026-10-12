package src

import (
	"encoding/json"
	"os"
	"path/filepath"
)

type hdhrCase struct {
	Settings       map[string]interface{}   `json:"settings"`
	Name           string                   `json:"name"`
	Version        string                   `json:"version"`
	Domain         string                   `json:"domain"`
	ScanInProgress int                      `json:"scanInProgress"`
	XEPG           map[string]interface{}   `json:"xepg"`
	Streams        []interface{}            `json:"streams"`
	PMS            map[string]interface{}   `json:"pms"`
	NoPMSFile      bool                     `json:"noPmsFile"`
}

type hdhrOut struct {
	Discover     string                `json:"discover"`
	LineupStatus string                `json:"lineupStatus"`
	Lineup       string                `json:"lineup"`
	Capability   string                `json:"capability"`
	PMS          map[string]string     `json:"pms"`
	Registry     map[string]StreamInfo `json:"registry"`
}

// DumpHDHR runs the real HDHomeRun emulation functions.
func DumpHDHR(dir, input string) (string, error) {
	content, err := os.ReadFile(input)
	if err != nil {
		return "", err
	}
	var c hdhrCase
	if err = json.Unmarshal(content, &c); err != nil {
		return "", err
	}
	System.Name = c.Name
	System.AppName = "threadfin"
	System.Version = c.Version
	System.Domain = c.Domain
	System.ServerProtocol.WEB = "http"
	System.ServerProtocol.DVR = "http"
	System.ScanInProgress = c.ScanInProgress
	System.Folder.Config = filepath.Join(dir, "cfg") + "/"
	os.MkdirAll(System.Folder.Config, 0755)
	System.File.PMS = filepath.Join(dir, "pms.json")
	System.File.URLS = filepath.Join(dir, "urls.json")
	if c.PMS == nil {
		c.PMS = map[string]interface{}{}
	}
	if !c.NoPMSFile {
		saveMapToJSONFile(System.File.PMS, c.PMS)
	}
	settingsJSON, _ := json.Marshal(c.Settings)
	Settings = SettingsStruct{}
	json.Unmarshal(settingsJSON, &Settings)
	setDeviceID()
	Data.Cache.StreamingURLS = make(map[string]StreamInfo)
	Data.Cache.PMS = nil
	Data.XEPG.Channels = c.XEPG
	Data.Streams.Active = c.Streams

	var out hdhrOut
	b, err := getDiscover()
	if err != nil {
		return "", err
	}
	out.Discover = string(b)
	b, err = getLineupStatus()
	if err != nil {
		return "", err
	}
	out.LineupStatus = string(b)
	b, err = getCapability()
	if err != nil {
		return "", err
	}
	out.Capability = string(b)
	b, err = getLineup()
	if err != nil {
		return "", err
	}
	out.Lineup = string(b)
	out.Registry = Data.Cache.StreamingURLS
	if pms, err := loadJSONFileToMap(System.File.PMS); err == nil {
		out.PMS = map[string]string{}
		for k, v := range pms {
			out.PMS[k] = v.(string)
		}
	}
	res, err := json.MarshalIndent(out, "", "  ")
	return string(res), err
}
