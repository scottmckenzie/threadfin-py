package src

import (
	"archive/zip"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"syscall"
	"time"
)

type backupArchiveEntry struct {
	Name    string `json:"name"`
	Content string `json:"content"`
	Dir     bool   `json:"dir"`
	Mode    string `json:"mode"`
}

type backupCase struct {
	Op      string               `json:"op"`
	Files   map[string]string    `json:"files"`
	Modes   map[string]string    `json:"modes"`
	Dirs    []string             `json:"dirs"`
	Keep    int                  `json:"keep"`
	Path    string               `json:"path"`
	Port    string               `json:"port"`
	URLBase string               `json:"urlBase"`
	Archive []backupArchiveEntry `json:"archive"`
}

type backupZipEntry struct {
	Name    string `json:"name"`
	Dir     bool   `json:"dir"`
	Mode    string `json:"mode"`
	Method  uint16 `json:"method"`
	Content string `json:"content"`
}

type backupTreeEntry struct {
	Dir     bool             `json:"dir,omitempty"`
	Mode    string           `json:"mode,omitempty"`
	Content *string          `json:"content,omitempty"`
	Zip     []backupZipEntry `json:"zip,omitempty"`
}

type backupOut struct {
	Result string                     `json:"result"`
	Err    string                     `json:"err"`
	Panic  string                     `json:"panic"`
	Tree   map[string]backupTreeEntry `json:"tree"`
}

// The archive names carry the time of the run; only those stamps are normalised, so old
// backups in the fixtures keep their names.
var backupStamps []string

func normBackupStamp(s string) string {
	for _, stamp := range backupStamps {
		s = strings.ReplaceAll(s, stamp, "{ts}")
	}
	return s
}

func parseBackupMode(s string, def os.FileMode) os.FileMode {
	if s == "" {
		return def
	}
	var m uint32
	fmt.Sscanf(s, "%o", &m)
	return os.FileMode(m)
}

func dumpBackupZip(path string) ([]backupZipEntry, error) {
	r, err := zip.OpenReader(path)
	if err != nil {
		return nil, err
	}
	defer r.Close()
	out := make([]backupZipEntry, 0)
	for _, f := range r.File {
		e := backupZipEntry{Name: normBackupStamp(f.Name), Dir: f.FileInfo().IsDir(), Method: f.Method}
		if !e.Dir {
			e.Mode = fmt.Sprintf("%04o", f.Mode().Perm())
			rc, err := f.Open()
			if err != nil {
				return nil, err
			}
			b, _ := io.ReadAll(rc)
			rc.Close()
			e.Content = string(b)
		}
		out = append(out, e)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out, nil
}

func dumpBackupTree(work string) map[string]backupTreeEntry {
	tree := make(map[string]backupTreeEntry)
	filepath.Walk(work, func(path string, info os.FileInfo, err error) error {
		if err != nil || path == work {
			return err
		}
		rel, _ := filepath.Rel(work, path)
		rel = normBackupStamp(filepath.ToSlash(rel))
		if info.IsDir() {
			tree[rel] = backupTreeEntry{Dir: true}
			return nil
		}
		e := backupTreeEntry{Mode: fmt.Sprintf("%04o", info.Mode().Perm())}
		if strings.HasSuffix(path, ".zip") {
			z, zerr := dumpBackupZip(path)
			if zerr == nil {
				e.Zip = z
				tree[rel] = e
				return nil
			}
		}
		b, _ := os.ReadFile(path)
		s := string(b)
		e.Content = &s
		tree[rel] = e
		return nil
	})
	return tree
}

func writeBackupArchive(target string, entries []backupArchiveEntry) error {
	f, err := os.Create(target)
	if err != nil {
		return err
	}
	defer f.Close()
	w := zip.NewWriter(f)
	for _, e := range entries {
		h := &zip.FileHeader{Name: e.Name, Method: zip.Deflate}
		if e.Dir {
			h.Method = zip.Store
			h.SetMode(os.ModeDir | 0755)
		} else {
			h.SetMode(parseBackupMode(e.Mode, 0644))
		}
		wr, err := w.CreateHeader(h)
		if err != nil {
			return err
		}
		if !e.Dir {
			wr.Write([]byte(e.Content))
		}
	}
	return w.Close()
}

// DumpBackup runs one backup case in the empty folder work and returns the JSON result.
func DumpBackup(work, caseFile string) (string, error) {
	raw, err := os.ReadFile(caseFile)
	if err != nil {
		return "", err
	}
	var c backupCase
	if err := json.Unmarshal(raw, &c); err != nil {
		return "", err
	}
	syscall.Umask(0022)
	sep := string(os.PathSeparator)
	sub := func(s string) string { return strings.ReplaceAll(s, "{work}", work) }

	for _, d := range c.Dirs {
		os.MkdirAll(filepath.Join(work, d), 0755)
	}
	for name, content := range c.Files {
		p := filepath.Join(work, name)
		os.MkdirAll(filepath.Dir(p), 0755)
		os.WriteFile(p, []byte(content), 0644)
		os.Chmod(p, parseBackupMode(c.Modes[name], 0644))
	}

	System.Folder.Config = work + sep + "cfg" + sep
	System.Folder.Backup = System.Folder.Config + "backup" + sep
	System.Folder.Data = System.Folder.Config + "data" + sep
	System.Folder.ImagesUpload = System.Folder.Data + "images" + sep
	System.Folder.Temp = work + sep + "tmp" + sep
	System.Compatibility = "0.1.0"
	System.URLBase = c.URLBase
	Settings.BackupKeep = c.Keep
	Settings.BackupPath = sub(c.Path)
	Settings.Port = c.Port

	var out backupOut
	backupStamps = []string{time.Now().Format("20060102_1504")}
	func() {
		defer func() {
			if r := recover(); r != nil {
				out.Panic = fmt.Sprint(r)
			}
		}()
		switch c.Op {
		case "auto":
			err = ThreadfinAutoBackup()
		case "manual":
			out.Result, err = ThreadfinBackup()
		case "restore":
			archive := filepath.Join(work, "upload.zip")
			if err = writeBackupArchive(archive, c.Archive); err == nil {
				out.Result, err = ThreadfinRestore(archive)
			}
		default:
			err = fmt.Errorf("unknown op %q", c.Op)
		}
		if err != nil {
			out.Err = err.Error()
		}
	}()
	backupStamps = append(backupStamps, time.Now().Format("20060102_1504"))
	out.Result = normBackupStamp(strings.ReplaceAll(out.Result, work, "{work}"))
	out.Err = strings.ReplaceAll(out.Err, work, "{work}")
	out.Tree = dumpBackupTree(work)

	b, err := json.MarshalIndent(out, "", "  ")
	return string(b), err
}
