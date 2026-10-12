package src

import (
	"os"
	"path/filepath"
)

// DumpSettings runs the real loadSettings() against an input settings.json and
// returns the settings.json it saved. Copied into the Go checkout by
// run_settings_golden.sh; it only sets up the System fields loadSettings needs.
func DumpSettings(dir, input string) (output string, err error) {
	System.Name = "Threadfin"
	System.AppName = "threadfin"
	System.DBVersion = "0.5.0"
	System.Folder.Backup = "/tmp/tfgolden/backup/"
	System.Folder.Temp = "/tmp/tfgolden/tmp/"
	System.FFmpeg.DefaultOptions = "-hide_banner -loglevel error -analyzeduration 1000000 -probesize 1000000 -i [URL] -map 0:v -map 0:a:0 -c:v copy -c:a aac -b:a 192k -ac 2 -c:s copy -f mpegts -fflags +genpts -movflags +faststart -copyts pipe:1"
	System.VLC.DefaultOptions = "-I dummy [URL] --sout #std{mux=ts,access=file,dst=-}"
	System.File.Settings = filepath.Join(dir, "settings.json")

	content, err := os.ReadFile(input)
	if err != nil {
		return "", err
	}
	if err = os.WriteFile(System.File.Settings, content, 0644); err != nil {
		return "", err
	}

	if _, err = loadSettings(); err != nil {
		return "", err
	}

	out, err := os.ReadFile(System.File.Settings)
	return string(out), err
}
