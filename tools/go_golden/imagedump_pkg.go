package src

import (
	"crypto/md5"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"threadfin/src/internal/imgcache"
)

// One case: cache folder contents, settings, and the image URLs looked up (each twice: Go answers
// the second lookup of a cached image from memory, with the domain rewrites).
type imgGoldenCase struct {
	Caching     bool     `json:"caching"`
	CacheURL    string   `json:"cacheURL"`
	Files       []string `json:"files"` // file names in the cache folder; "md5:<url>" = md5 name of that URL
	HTTPDomain  string   `json:"httpDomain"`
	Port        string   `json:"port"`
	ForceHTTPS  bool     `json:"forceHttps"`
	HTTPSPort   int      `json:"httpsPort"`
	HTTPSDomain string   `json:"httpsDomain"`
	Sources     []string `json:"sources"`
}

type imgLookup struct {
	Source string `json:"source"`
	First  string `json:"first"`
	Second string `json:"second"`
}

type imgGoldenOut struct {
	Lookups []imgLookup `json:"lookups"`
	Queue   []string    `json:"queue"`
}

func imgMD5Name(spec string) string {
	if !strings.HasPrefix(spec, "md5:") {
		return spec
	}
	url := strings.TrimPrefix(spec, "md5:")
	sum := md5.Sum([]byte(strings.Split(url, "?")[0]))
	return hex.EncodeToString(sum[:]) + filepath.Ext(strings.Split(url, "?")[0])
}

// DumpImages writes <case>.golden.json for every <case>.input.json in dir.
func DumpImages(dir string) error {
	files, _ := filepath.Glob(filepath.Join(dir, "*.input.json"))
	sort.Strings(files)
	for _, f := range files {
		name := strings.TrimSuffix(filepath.Base(f), ".input.json")
		raw, _ := os.ReadFile(f)
		var c imgGoldenCase
		if err := json.Unmarshal(raw, &c); err != nil {
			return fmt.Errorf("%s: %w", name, err)
		}
		work, _ := os.MkdirTemp("", "tfimg")
		folder := work + string(os.PathSeparator)
		for _, spec := range c.Files {
			os.WriteFile(folder+imgMD5Name(spec), []byte("x"), 0644)
		}
		cache, err := imgcache.New(folder, c.CacheURL, c.Caching)
		if err != nil {
			return fmt.Errorf("%s: %w", name, err)
		}
		var out imgGoldenOut
		for _, src := range c.Sources {
			get := func() string {
				return cache.Image.GetURL(src, c.HTTPDomain, c.Port, c.ForceHTTPS, c.HTTPSPort, c.HTTPSDomain)
			}
			first := get()
			out.Lookups = append(out.Lookups, imgLookup{src, first, get()})
		}
		out.Queue = append([]string{}, cache.Queue...)
		os.RemoveAll(work)
		b, _ := json.MarshalIndent(out, "", "  ")
		os.WriteFile(filepath.Join(dir, name+".golden.json"), append(b, '\n'), 0644)
		fmt.Println("dumped", name)
	}
	return nil
}
