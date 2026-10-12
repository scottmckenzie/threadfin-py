package src

import (
	"encoding/json"
	"os"
	"path/filepath"

	"threadfin/src/internal/imgcache"
)

type xepgCase struct {
	Settings map[string]interface{}            `json:"settings"`
	XMLTV    map[string]string                 `json:"xmltv"`
	XEPG     map[string]interface{}            `json:"xepg"`
	Active   []map[string]string               `json:"active"`
	Inactive []map[string]string               `json:"inactive"`
}

type xepgOut struct {
	XEPG    map[string]interface{} `json:"xepg"`
	Count   int64                  `json:"xepgCount"`
	Mapping map[string]interface{} `json:"mapping"`
}

// DumpXEPG runs createXEPGMapping, createXEPGDatabase, mapping and cleanupXEPG
// (the database half of buildXEPG) over files prepared from the case.
func DumpXEPG(dir, input string) (string, error) {
	content, err := os.ReadFile(input)
	if err != nil {
		return "", err
	}
	var c xepgCase
	if err = json.Unmarshal(content, &c); err != nil {
		return "", err
	}

	System.Name = "Threadfin"
	System.AppName = "threadfin"
	System.Flag.Info = true
	System.PlexChannelLimit = 480
	System.UnfilteredChannelLimit = 480
	System.ConfigurationWizard = true
	System.Folder.Data = filepath.Join(dir, "data") + "/"
	System.Folder.ImagesCache = filepath.Join(dir, "images") + "/"
	System.File.Settings = filepath.Join(dir, "settings.json")
	System.File.XEPG = filepath.Join(dir, "xepg.json")
	System.File.URLS = filepath.Join(dir, "urls.json")
	os.MkdirAll(System.Folder.Data, 0755)
	os.MkdirAll(System.Folder.ImagesCache, 0755)

	for id, body := range c.XMLTV {
		if err := os.WriteFile(System.Folder.Data+id+".xml", []byte(body), 0644); err != nil {
			return "", err
		}
	}
	settingsJSON, _ := json.Marshal(c.Settings)
	os.WriteFile(System.File.Settings, settingsJSON, 0644)
	xepgJSON, _ := json.Marshal(c.XEPG)
	os.WriteFile(System.File.XEPG, xepgJSON, 0644)

	Settings = SettingsStruct{}
	json.Unmarshal(settingsJSON, &Settings)

	Data.Cache.Images, _ = imgcache.New(System.Folder.ImagesCache, "http://localhost/images/", false)
	Data.Cache.XMLTV = make(map[string]XMLTV)

	Data.Streams.Active = nil
	for _, s := range c.Active {
		Data.Streams.Active = append(Data.Streams.Active, s)
	}
	Data.Streams.Inactive = nil
	for _, s := range c.Inactive {
		Data.Streams.Inactive = append(Data.Streams.Inactive, s)
	}

	createXEPGMapping()
	if err := createXEPGDatabase(); err != nil {
		return "", err
	}
	mapping()
	cleanupXEPG()

	out := xepgOut{Count: Data.XEPG.XEPGCount, Mapping: Data.XMLTV.Mapping}
	saved, err := os.ReadFile(System.File.XEPG)
	if err != nil {
		return "", err
	}
	if err = json.Unmarshal(saved, &out.XEPG); err != nil {
		return "", err
	}
	b, err := json.MarshalIndent(out, "", "  ")
	return string(b), err
}
