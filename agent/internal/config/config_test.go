package config

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"cloudops-agent/internal/payload"
)

func TestValidateURL(t *testing.T) {
	if u, err := ValidateURL(" https://hub.example.com/ ", false); err != nil || u != "https://hub.example.com" {
		t.Errorf("u=%q err=%v", u, err)
	}
	for _, bad := range []string{"http://hub", "ftp://hub", "hub.example.com", "", "https://"} {
		if _, err := ValidateURL(bad, false); err == nil {
			t.Errorf("%q must be rejected", bad)
		}
	}
	if _, err := ValidateURL("http://127.0.0.1:8000", true); err != nil {
		t.Errorf("insecure-dev should allow http: %v", err)
	}
	if _, err := ValidateURL("ftp://x", true); err == nil {
		t.Error("insecure-dev must not allow other schemes")
	}
}

func TestSanitizeClamps(t *testing.T) {
	cases := []struct{ in, want int }{{0, 60}, {1, 15}, {14, 15}, {15, 15}, {120, 120}, {300, 300}, {301, 300}, {99999, 300}, {-5, 15}}
	for _, c := range cases {
		if got := Sanitize(payload.ServerConfig{IntervalSeconds: c.in}).IntervalSeconds; got != c.want {
			t.Errorf("interval %d -> %d, want %d", c.in, got, c.want)
		}
	}
	s := Sanitize(payload.ServerConfig{})
	if s.MaxBatchMetrics != 500 || s.WatchServices == nil || len(s.DiskExcludeFstypes) == 0 {
		t.Errorf("defaults not filled: %+v", s)
	}
	if Sanitize(payload.ServerConfig{MaxBatchMetrics: 10 << 20}).MaxBatchMetrics != 5000 {
		t.Error("batch not capped")
	}
	// an explicitly empty exclude list from the hub is respected
	if e := Sanitize(payload.ServerConfig{DiskExcludeFstypes: []string{}}); len(e.DiskExcludeFstypes) != 0 {
		t.Errorf("empty exclude list overridden: %v", e.DiskExcludeFstypes)
	}
}

func TestSaveLoadRoundTripAndPerms(t *testing.T) {
	p := filepath.Join(t.TempDir(), "etc", "agent.json")
	in := File{URL: "https://h", AgentToken: "cah_x", HostAgentID: 9, Hostname: "n", Config: DefaultServerConfig()}
	if err := Save(p, in); err != nil {
		t.Fatal(err)
	}
	if fi, _ := os.Stat(p); fi.Mode().Perm() != 0o600 {
		t.Errorf("mode %v", fi.Mode().Perm())
	}
	out, err := Load(p)
	if err != nil || out.AgentToken != "cah_x" || out.ConfigVersion != 1 || out.Config.IntervalSeconds != 60 {
		t.Errorf("out=%+v err=%v", out, err)
	}
	// overwrite stays 0600 and leaves no temp files
	if err := Save(p, in); err != nil {
		t.Fatal(err)
	}
	ents, _ := os.ReadDir(filepath.Dir(p))
	if len(ents) != 1 {
		t.Errorf("leftover files: %v", ents)
	}
}

func TestLoadRejectsIncomplete(t *testing.T) {
	d := t.TempDir()
	for name, body := range map[string]string{"a": `{}`, "b": `{"url":"https://h"}`, "c": `not json`} {
		p := filepath.Join(d, name)
		_ = os.WriteFile(p, []byte(body), 0o600)
		if _, err := Load(p); err == nil {
			t.Errorf("%s should fail", name)
		}
	}
}

func TestTokenNeverPrinted(t *testing.T) {
	f := File{URL: "https://h", AgentToken: "cah_SUPERSECRET"}
	for _, s := range []string{fmt.Sprintf("%v", f), fmt.Sprintf("%+v", f), fmt.Sprintf("%#v", f), fmt.Sprint(f)} {
		if strings.Contains(s, "SUPERSECRET") {
			t.Errorf("token leaked via formatting: %s", s)
		}
	}
}
