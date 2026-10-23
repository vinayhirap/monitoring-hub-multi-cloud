package state

import (
	"os"
	"path/filepath"
	"testing"

	"cloudops-agent/internal/payload"
)

func TestSeqMonotonicAcrossRestart(t *testing.T) {
	dir := t.TempDir()
	s, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	var last int64
	for i := 0; i < 5; i++ {
		n, err := s.NextSeq()
		if err != nil || n <= last {
			t.Fatalf("seq %d after %d err=%v", n, last, err)
		}
		last = n
	}
	s2, _ := Open(dir)
	n, _ := s2.NextSeq()
	if n != last+1 {
		t.Errorf("after restart seq = %d, want %d", n, last+1)
	}
}

func TestRaiseFromQueueFloor(t *testing.T) {
	s, _ := Open(t.TempDir())
	s.Raise(100)
	if n, _ := s.NextSeq(); n != 101 {
		t.Errorf("seq = %d", n)
	}
	s.Raise(5) // never lowers
	if n, _ := s.NextSeq(); n != 102 {
		t.Errorf("seq = %d", n)
	}
}

func TestConfigPersisted(t *testing.T) {
	dir := t.TempDir()
	s, _ := Open(dir)
	cfg := payload.ServerConfig{IntervalSeconds: 30, WatchServices: []string{"nginx"}, MaxBatchMetrics: 100}
	if err := s.SetConfig(7, cfg); err != nil {
		t.Fatal(err)
	}
	g := func() State { s2, _ := Open(dir); return s2.Get() }()
	if g.ConfigVersion != 7 || g.Config == nil || g.Config.IntervalSeconds != 30 || g.Config.WatchServices[0] != "nginx" {
		t.Errorf("got %+v", g)
	}
	fi, _ := os.Stat(filepath.Join(dir, "state.json"))
	if fi.Mode().Perm() != 0o600 {
		t.Errorf("mode %v", fi.Mode().Perm())
	}
}

func TestCorruptStateStartsFresh(t *testing.T) {
	dir := t.TempDir()
	_ = os.WriteFile(filepath.Join(dir, "state.json"), []byte("{not json"), 0o600)
	s, err := Open(dir)
	if err != nil {
		t.Fatalf("corrupt state must not prevent start: %v", err)
	}
	if n, _ := s.NextSeq(); n != 1 {
		t.Errorf("seq = %d", n)
	}
}
