package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

const tok = "cah_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmn"

func run(args ...string) (int, string, string) {
	var o, e bytes.Buffer
	c := realMain(args, &o, &e)
	return c, o.String(), e.String()
}

func TestVersionAndUsage(t *testing.T) {
	c, out, _ := run("version")
	if c != 0 || !strings.Contains(out, "cloudops-agent") || !strings.Contains(out, runtime.GOARCH) {
		t.Errorf("code=%d out=%q", c, out)
	}
	if c, _, _ := run(); c != 2 {
		t.Errorf("no args code = %d", c)
	}
	if c, _, e := run("bogus"); c != 2 || !strings.Contains(e, "unknown command") {
		t.Errorf("bogus code=%d err=%q", c, e)
	}
}

func TestEnrollCLI(t *testing.T) {
	var gotBody map[string]any
	hub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&gotBody)
		_, _ = w.Write([]byte(`{"host_agent_id":3,"agent_token":"` + tok + `","config":{"interval_seconds":60}}`))
	}))
	defer hub.Close()
	cfg := filepath.Join(t.TempDir(), "agent.json")

	// https-only by default
	if c, _, e := run("enroll", "--url", hub.URL, "--key", "cak_x", "--config", cfg, "--owner", ""); c != 1 || !strings.Contains(e, "refusing http://") {
		t.Fatalf("code=%d err=%q", c, e)
	}
	c, out, e := run("enroll", "--url", hub.URL, "--key", "cak_secretsecret", "--hostname", "box1", "--config", cfg, "--insecure-dev", "--owner", "")
	if c != 0 {
		t.Fatalf("code=%d err=%q", c, e)
	}
	if strings.Contains(out+e, tok) || strings.Contains(out+e, "cak_secretsecret") {
		t.Error("secrets printed")
	}
	if gotBody["hostname"] != "box1" || gotBody["enrollment_key"] != "cak_secretsecret" {
		t.Errorf("body = %v", gotBody)
	}
	if fi, _ := os.Stat(cfg); fi.Mode().Perm() != 0o600 {
		t.Errorf("mode %v", fi.Mode().Perm())
	}
	// key from env keeps it out of argv
	t.Setenv("CLOUDOPS_AGENT_ENROLL_KEY", "cak_fromenv")
	if c, _, e := run("enroll", "--url", hub.URL, "--config", cfg, "--insecure-dev", "--force", "--owner", ""); c != 0 {
		t.Fatalf("env key: code=%d err=%q", c, e)
	}
	if gotBody["enrollment_key"] != "cak_fromenv" {
		t.Errorf("env key not used: %v", gotBody)
	}
	// no key at all
	t.Setenv("CLOUDOPS_AGENT_ENROLL_KEY", "")
	if c, _, _ := run("enroll", "--url", hub.URL, "--config", cfg, "--insecure-dev", "--force"); c != 1 {
		t.Errorf("missing key code = %d", c)
	}
}

func TestCheckPrintsPayloadWithoutNetwork(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("linux only")
	}
	// Any HTTP client use would fail on this unroutable proxy; check must not touch the network.
	t.Setenv("HTTP_PROXY", "http://127.0.0.1:1")
	t.Setenv("HTTPS_PROXY", "http://127.0.0.1:1")
	c, out, _ := run("check", "--config", filepath.Join(t.TempDir(), "missing.json"), "--wait", "300ms")
	if c != 0 {
		t.Fatalf("code=%d", c)
	}
	var doc struct {
		Note    string `json:"note"`
		Metrics []struct {
			Name   string            `json:"name"`
			Value  float64           `json:"value"`
			Labels map[string]string `json:"labels"`
		} `json:"metrics"`
		Services []any `json:"services"`
	}
	if err := json.Unmarshal([]byte(out), &doc); err != nil {
		t.Fatalf("output is not JSON: %v\n%s", err, out)
	}
	have := map[string]bool{}
	for _, m := range doc.Metrics {
		have[m.Name] = true
		if m.Name == "net_rx_bytes_per_s" && m.Labels["iface"] == "lo" {
			t.Error("lo must be skipped")
		}
	}
	for _, n := range []string{"cpu_percent", "load1", "load5", "load15", "mem_used_percent", "mem_available_bytes", "disk_used_percent", "proc_count", "uptime_seconds"} {
		if !have[n] {
			t.Errorf("metric %s missing from check output", n)
		}
	}
	if !strings.Contains(doc.Note, "nothing was sent") || doc.Services == nil {
		t.Errorf("note=%q services=%v", doc.Note, doc.Services)
	}
}

func TestRunRefusesMissingConfigAndPlainHTTP(t *testing.T) {
	dir := t.TempDir()
	if c, _, e := run("run", "--config", filepath.Join(dir, "none.json"), "--data-dir", dir); c != 1 || !strings.Contains(e, "cannot load config") {
		t.Errorf("code=%d err=%q", c, e)
	}
	cfg := filepath.Join(dir, "agent.json")
	_ = os.WriteFile(cfg, []byte(`{"url":"http://hub.example","agent_token":"`+tok+`"}`), 0o600)
	c, _, e := run("run", "--config", cfg, "--data-dir", dir)
	if c != 1 || !strings.Contains(e, "refusing") {
		t.Errorf("code=%d err=%q", c, e)
	}
	if strings.Contains(e, tok) {
		t.Error("token in log output")
	}
}

func TestRunExitsCode3On401(t *testing.T) {
	hub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(401) }))
	defer hub.Close()
	dir := t.TempDir()
	cfg := filepath.Join(dir, "agent.json")
	_ = os.WriteFile(cfg, []byte(`{"url":"`+hub.URL+`","agent_token":"`+tok+`","hostname":"h"}`), 0o600)
	c, _, e := run("run", "--config", cfg, "--data-dir", dir, "--insecure-dev")
	if c != exitUnauthorized {
		t.Errorf("exit code = %d, want %d\n%s", c, exitUnauthorized, e)
	}
	if strings.Contains(e, tok) {
		t.Error("token in log output")
	}
}

func TestSplitCSV(t *testing.T) {
	got := splitCSV(" nginx, sshd ,,app.service")
	if len(got) != 3 || got[0] != "nginx" || got[1] != "sshd" || got[2] != "app.service" {
		t.Errorf("got %v", got)
	}
}
