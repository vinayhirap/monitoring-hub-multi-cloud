package logfile

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestPrefixesTimestampAndAppendsAcrossOpens(t *testing.T) {
	p := filepath.Join(t.TempDir(), "agent.log")
	w, err := Open(p, 0)
	if err != nil {
		t.Fatal(err)
	}
	w.now = func() time.Time { return time.Date(2026, 10, 23, 7, 0, 0, 0, time.UTC) }
	if n, err := w.Write([]byte("level=INFO msg=hello\n")); err != nil || n != len("level=INFO msg=hello\n") {
		t.Fatalf("n=%d err=%v (Write must report the caller's length, not the prefixed length)", n, err)
	}
	w.Close()
	w2, _ := Open(p, 0)
	w2.Write([]byte("second\n"))
	w2.Close()
	b, _ := os.ReadFile(p)
	if !strings.HasPrefix(string(b), "2026-10-23T07:00:00Z level=INFO msg=hello\n") || !strings.Contains(string(b), "Z second\n") {
		t.Fatalf("got %q", b)
	}
}

func TestRotatesAtTheCapAndKeepsOneBackup(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "agent.log")
	w, _ := Open(p, 200)
	defer w.Close()
	for i := 0; i < 40; i++ {
		if _, err := w.Write([]byte("0123456789012345678901234567890123456789\n")); err != nil {
			t.Fatal(err)
		}
	}
	cur, _ := os.Stat(p)
	bak, err := os.Stat(p + ".1")
	if err != nil {
		t.Fatalf("no backup: %v", err)
	}
	if cur.Size() > 200 || bak.Size() > 200 {
		t.Fatalf("cap exceeded: %d / %d", cur.Size(), bak.Size())
	}
	if _, err := os.Stat(p + ".2"); err == nil {
		t.Fatal("only one backup may be kept")
	}
}

func TestOpenFailsOnUnwritablePath(t *testing.T) {
	if _, err := Open(filepath.Join(t.TempDir(), "no", "such", "dir", "a.log"), 0); err == nil {
		t.Fatal("want error")
	}
}
