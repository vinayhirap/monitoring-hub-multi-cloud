package enroll

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"cloudops-agent/internal/config"
)

const (
	testKey = "cak_0123456789abcdef0123456789abcdef"
	testTok = "cah_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmn"
)

type hub struct {
	srv  *httptest.Server
	got  map[string]any
	hits int
}

func newHub(t *testing.T, status int, body string) *hub {
	t.Helper()
	h := &hub{got: map[string]any{}}
	h.srv = httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h.hits++
		if r.URL.Path != EnrollPath || r.Method != http.MethodPost {
			t.Errorf("unexpected %s %s", r.Method, r.URL.Path)
		}
		_ = json.NewDecoder(r.Body).Decode(&h.got)
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(h.srv.Close)
	return h
}

const okBody = `{"host_agent_id": 17, "agent_token": "` + testTok + `", "config": {"interval_seconds": 45, "watch_services": ["nginx"], "disk_exclude_fstypes": ["tmpfs"], "max_batch_metrics": 200}}`

func opts(t *testing.T, h *hub) (Options, *bytes.Buffer) {
	out := &bytes.Buffer{}
	return Options{URL: h.srv.URL, Key: testKey, Hostname: "web-1", ConfigPath: filepath.Join(t.TempDir(), "etc", "agent.json"),
		Version: "0.1.0", HTTP: h.srv.Client(), IMDSBase: "http://127.0.0.1:1", Out: out, Warn: out}, out
}

func TestEnrollWritesConfig0600AndNeverLeaksSecrets(t *testing.T) {
	h := newHub(t, 200, okBody)
	o, out := opts(t, h)
	if err := Run(context.Background(), o); err != nil {
		t.Fatal(err)
	}
	// request body
	if h.got["enrollment_key"] != testKey || h.got["hostname"] != "web-1" || h.got["os"] != "linux" || h.got["agent_version"] != "0.1.0" || h.got["arch"] == "" {
		t.Errorf("request = %v", h.got)
	}
	if _, has := h.got["instance_id"]; has {
		t.Errorf("instance_id must be omitted when IMDS is unavailable: %v", h.got)
	}
	// file
	fi, err := os.Stat(o.ConfigPath)
	if err != nil || fi.Mode().Perm() != 0o600 {
		t.Fatalf("stat=%v err=%v", fi, err)
	}
	f, err := config.Load(o.ConfigPath)
	if err != nil {
		t.Fatal(err)
	}
	if f.AgentToken != testTok || f.HostAgentID != 17 || f.URL != h.srv.URL || f.Hostname != "web-1" ||
		f.ConfigVersion != 1 || f.Config.IntervalSeconds != 45 || f.Config.WatchServices[0] != "nginx" {
		t.Errorf("file = %#v", f)
	}
	raw, _ := os.ReadFile(o.ConfigPath)
	if strings.Contains(string(raw), testKey) {
		t.Error("enrollment key must never be written to disk")
	}
	// console output
	if strings.Contains(out.String(), testTok) || strings.Contains(out.String(), testKey) {
		t.Errorf("output leaks a secret: %s", out.String())
	}
	if !strings.Contains(out.String(), "host_agent_id=17") {
		t.Errorf("output = %q", out.String())
	}
}

func TestRefusesOverwriteWithoutForce(t *testing.T) {
	h := newHub(t, 200, okBody)
	o, _ := opts(t, h)
	if err := os.MkdirAll(filepath.Dir(o.ConfigPath), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(o.ConfigPath, []byte("existing"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := Run(context.Background(), o); err == nil || !strings.Contains(err.Error(), "--force") {
		t.Errorf("err = %v", err)
	}
	if h.hits != 0 {
		t.Error("must not call the hub (and burn the key) when the file already exists")
	}
	o.Force = true
	if err := Run(context.Background(), o); err != nil {
		t.Fatal(err)
	}
}

func TestHTTPRefusedUnlessInsecureDev(t *testing.T) {
	plain := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte(okBody)) }))
	defer plain.Close()
	o := Options{URL: plain.URL, Key: testKey, Hostname: "h", ConfigPath: filepath.Join(t.TempDir(), "a.json"), IMDSBase: "http://127.0.0.1:1"}
	err := Run(context.Background(), o)
	if err == nil || !strings.Contains(err.Error(), "refusing http://") {
		t.Fatalf("err = %v", err)
	}
	o.InsecureDev = true
	if err := Run(context.Background(), o); err != nil {
		t.Fatalf("--insecure-dev should allow http: %v", err)
	}
}

func TestErrorMapping(t *testing.T) {
	cases := []struct {
		status int
		body   string
		want   string
	}{
		{404, `{"detail":"Not Found"}`, "not enabled"},
		{429, `{"detail":"slow down"}`, "rate limited"},
		{401, `{"detail":"bad key"}`, "invalid, expired, revoked or used up"},
		{403, `{"detail":"bad key"}`, "invalid, expired, revoked or used up"},
		{500, `oops`, "HTTP 500"},
	}
	for _, tc := range cases {
		h := newHub(t, tc.status, tc.body)
		o, _ := opts(t, h)
		err := Run(context.Background(), o)
		if err == nil || !strings.Contains(err.Error(), tc.want) {
			t.Errorf("status %d: err = %v, want substring %q", tc.status, err, tc.want)
			continue
		}
		if strings.Contains(err.Error(), testKey) {
			t.Errorf("error leaks key: %v", err)
		}
		if _, statErr := os.Stat(o.ConfigPath); statErr == nil {
			t.Errorf("status %d: agent.json must not be written on failure", tc.status)
		}
	}
}

func TestMalformedSuccessResponseRejected(t *testing.T) {
	for _, body := range []string{`{}`, `{"host_agent_id":1,"agent_token":"nope"}`, `{"host_agent_id":0,"agent_token":"` + testTok + `"}`} {
		h := newHub(t, 200, body)
		o, _ := opts(t, h)
		if err := Run(context.Background(), o); err == nil {
			t.Errorf("body %s should be rejected", body)
		}
	}
}

func TestMissingKey(t *testing.T) {
	h := newHub(t, 200, okBody)
	o, _ := opts(t, h)
	o.Key = "  "
	if err := Run(context.Background(), o); err == nil {
		t.Error("expected error")
	}
}

func imds(t *testing.T, tokenStatus int, id string) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPut && r.URL.Path == "/latest/api/token":
			if r.Header.Get("X-aws-ec2-metadata-token-ttl-seconds") == "" {
				t.Error("missing ttl header")
			}
			w.WriteHeader(tokenStatus)
			_, _ = w.Write([]byte("TOKEN123"))
		case r.Method == http.MethodGet && r.URL.Path == "/latest/meta-data/instance-id":
			if r.Header.Get("X-aws-ec2-metadata-token") != "TOKEN123" {
				w.WriteHeader(401) // IMDSv2 only: no token, no answer
				return
			}
			_, _ = w.Write([]byte(id))
		default:
			w.WriteHeader(404)
		}
	}))
}

func TestInstanceIDFromIMDSv2(t *testing.T) {
	s := imds(t, 200, "i-0abc123def4567890")
	defer s.Close()
	if got := InstanceID(context.Background(), s.URL); got != "i-0abc123def4567890" {
		t.Errorf("got %q", got)
	}
	// And it is sent to the hub when enrolling.
	h := newHub(t, 200, okBody)
	o, _ := opts(t, h)
	o.IMDSBase = s.URL
	if err := Run(context.Background(), o); err != nil {
		t.Fatal(err)
	}
	if h.got["instance_id"] != "i-0abc123def4567890" {
		t.Errorf("instance_id not sent: %v", h.got)
	}
	f, _ := config.Load(o.ConfigPath)
	if f.InstanceID != "i-0abc123def4567890" {
		t.Errorf("instance id not stored: %q", f.InstanceID)
	}
}

func TestInstanceIDFailuresAreSilentAndFast(t *testing.T) {
	bad := imds(t, 200, "<html>not an id</html>")
	defer bad.Close()
	denied := imds(t, 403, "i-0abc123def4567890") // IMDS disabled / hop limit
	defer denied.Close()
	slow := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { time.Sleep(2 * time.Second) }))
	defer slow.Close()
	for name, base := range map[string]string{"malformed": bad.URL, "denied": denied.URL, "unreachable": "http://127.0.0.1:1", "slow": slow.URL} {
		start := time.Now()
		if got := InstanceID(context.Background(), base); got != "" {
			t.Errorf("%s: got %q", name, got)
		}
		if d := time.Since(start); d > 4*time.Second {
			t.Errorf("%s: took %v (must be bounded by the short timeout)", name, d)
		}
	}
}
