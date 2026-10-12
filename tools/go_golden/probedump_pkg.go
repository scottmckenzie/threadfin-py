package src

// DumpProbe runs the real probeChannel() with Settings.FFmpegPath pointing at a
// fake "ffmpeg" next to a fake "ffprobe" script that prints the given JSON file.
// Copied into the Go checkout by run_probe_golden.sh.
func DumpProbe(fakeFFmpegPath, jsonFile string) (resolution, frameRate, audioChannels string, err error) {
	Settings.FFmpegPath = fakeFFmpegPath
	return probeChannel(RequestStruct{ProbeURL: jsonFile})
}
