package src

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"

	"threadfin/src/internal/authentication"
)

type authStep struct {
	Op         string                 `json:"op"`
	Secret     string                 `json:"secret"`
	Username   string                 `json:"username"`
	Password   string                 `json:"password"`
	Header     *string                `json:"header"`
	Level      string                 `json:"level"`
	Type       string                 `json:"type"`
	Query      string                 `json:"query"`
	UserData   map[string]interface{} `json:"userData"`
	DeleteUser bool                   `json:"deleteUser"`
}

type authCase struct {
	Settings map[string]interface{} `json:"settings"`
	DB       json.RawMessage        `json:"db"`
	Steps    []authStep             `json:"steps"`
}

type authStepOut struct {
	Op     string          `json:"op"`
	Result interface{}     `json:"result"`
	Err    string          `json:"err"`
	Panic  string          `json:"panic"`
	DB     json.RawMessage `json:"db"`
}

func runAuthStep(s authStep) (out authStepOut) {
	out.Op = s.Op
	defer func() {
		if r := recover(); r != nil {
			out.Panic = fmt.Sprint(r)
		}
	}()
	var err error
	switch s.Op {
	case "hash":
		out.Result = authentication.SHA256(s.Secret, "ignored")
	case "init":
		err = activatedSystemAuthentication()
	case "firstUser":
		var token string
		token, err = createFirstUserForAuthentication(s.Username, s.Password)
		out.Result = token != ""
	case "newUser":
		err = saveNewUser(RequestStruct{UserData: s.UserData})
	case "saveUserData":
		err = saveUserData(RequestStruct{UserData: s.UserData, DeleteUser: s.DeleteUser})
	case "basic":
		r, _ := http.NewRequest("GET", "http://x/lineup.json", nil)
		if s.Header != nil {
			r.Header.Set("Authorization", *s.Header)
		}
		var username string
		username, err = basicAuth(r, s.Level)
		out.Result = username
	case "url":
		r, _ := http.NewRequest("GET", "http://x/m3u/threadfin.m3u?"+s.Query, nil)
		err = urlAuth(r, s.Type)
	case "login":
		var token string
		token, err = authentication.UserAuthentication(s.Username, s.Password)
		if err == nil && s.Level != "" {
			err = checkAuthorizationLevel(token, s.Level)
		}
		out.Result = token != ""
	case "tokenRotate":
		var token, fresh string
		token, err = authentication.UserAuthentication(s.Username, s.Password)
		if err != nil {
			break
		}
		fresh, err = authentication.CheckTheValidityOfTheToken(token)
		if err != nil {
			break
		}
		_, oldErr := authentication.CheckTheValidityOfTheToken(token)
		_, freshErr := authentication.CheckTheValidityOfTheToken(fresh)
		out.Result = map[string]interface{}{"rotated": fresh != token, "oldErr": fmt.Sprint(oldErr), "freshErr": fmt.Sprint(freshErr)}
	default:
		panic("unknown op " + s.Op)
	}
	if err != nil {
		out.Err = err.Error()
	}
	return
}

// DumpAuth runs a sequence of real authentication calls against one authentication.json.
func DumpAuth(dir, input string) (string, error) {
	content, err := os.ReadFile(input)
	if err != nil {
		return "", err
	}
	var c authCase
	if err = json.Unmarshal(content, &c); err != nil {
		return "", err
	}
	System.Folder.Config = dir + string(os.PathSeparator)
	System.ConfigurationWizard = false
	dbFile := filepath.Join(dir, "authentication.json")
	if len(c.DB) > 0 && string(c.DB) != "null" {
		os.WriteFile(dbFile, c.DB, 0600)
	}
	settingsJSON, _ := json.Marshal(c.Settings)
	Settings = SettingsStruct{}
	json.Unmarshal(settingsJSON, &Settings)

	var outs []authStepOut
	for _, s := range c.Steps {
		o := runAuthStep(s)
		if b, err := os.ReadFile(dbFile); err == nil {
			o.DB = b
		} else {
			o.DB = json.RawMessage("null")
		}
		outs = append(outs, o)
	}
	b, err := json.MarshalIndent(outs, "", " ")
	return string(b), err
}
