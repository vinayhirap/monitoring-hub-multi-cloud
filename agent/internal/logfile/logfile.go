// Package logfile is a size-capped, timestamping log file for platforms without journald (Windows scheduled task,
// macOS launchd). One backup is kept: when a write would push the file past the cap, <path> is renamed to <path>.1
// (replacing the previous backup) and a fresh file is started, so disk use is bounded by ~2x the cap.
package logfile

import (
	"os"
	"sync"
	"time"
)

type Writer struct {
	mu   sync.Mutex
	path string
	max  int64
	f    *os.File
	size int64
	now  func() time.Time
}

// Open opens (creating, mode 0600) path for appending. maxBytes <= 0 means 5 MiB.
func Open(path string, maxBytes int64) (*Writer, error) {
	if maxBytes <= 0 {
		maxBytes = 5 << 20
	}
	w := &Writer{path: path, max: maxBytes, now: time.Now}
	if err := w.open(); err != nil {
		return nil, err
	}
	return w, nil
}

func (w *Writer) open() error {
	f, err := os.OpenFile(w.path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	st, err := f.Stat()
	if err != nil {
		f.Close()
		return err
	}
	w.f, w.size = f, st.Size()
	return nil
}

// Write prefixes every call with an RFC 3339 UTC timestamp (slog emits one line per call, and the agent's logger
// drops its own time key because journald normally supplies it).
func (w *Writer) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	line := append([]byte(w.now().UTC().Format("2006-01-02T15:04:05Z")+" "), p...)
	if w.size+int64(len(line)) > w.max && w.size > 0 {
		w.f.Close()
		_ = os.Remove(w.path + ".1") // Windows rename does not always replace; ignore "does not exist"
		if err := os.Rename(w.path, w.path+".1"); err != nil {
			_ = os.Truncate(w.path, 0) // cannot rotate: still honour the cap by starting over
		}
		if err := w.open(); err != nil {
			return 0, err
		}
	}
	n, err := w.f.Write(line)
	w.size += int64(n)
	if err != nil {
		return 0, err
	}
	return len(p), nil
}

func (w *Writer) Close() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.f.Close()
}
