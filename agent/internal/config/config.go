// Package config holds the on-disk agent identity (agent.json, written by
// `enroll`) and the defaults / sanity limits for the hub-controlled config.
package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"

	"cloudops-agent/internal/payload"
)

const (
	DefaultPath    = "/etc/cloudops-agent/agent.json"
	DefaultDataDir = "/var/lib/cloudops-agent"

	MinInterval = 15
	MaxInterval = 300
)

// File is /etc/cloudops-agent/agent.json. It never contains the enrollment key.
type File struct {
	URL           string               `json:"url"`
	AgentToken    string               `json:"agent_token"`
	HostAgentID   int64                `json:"host_agent_id"`
	Hostname      string               `json:"hostname"`
	InstanceID    string               `json:"instance_id,omitempty"`
	ConfigVersion int                  `json:"config_version"`
	Config        payload.ServerConfig `json:"config"`
	// NoGzip disables request compression (escape hatch for a hub that does not
	// decode Content-Encoding: gzip). Default false.
	NoGzip bool `json:"no_gzip,omitempty"`
}

// String / GoString redact the token so a stray %v or %+v can never leak it.
func (f File) String() string {
	return fmt.Sprintf("config.File{url=%s host_agent_id=%d hostname=%s token=[redacted]}", f.URL, f.HostAgentID, f.Hostname)
}
func (f File) GoString() string { return f.String() }

// DefaultServerConfig is used until the hub says otherwise.
func DefaultServerConfig() payload.ServerConfig {
	return payload.ServerConfig{
		IntervalSeconds:    60,
		WatchServices:      []string{},
		DiskExcludeFstypes: []string{"tmpfs", "devtmpfs", "overlay", "squashfs", "proc", "sysfs"},
		MaxBatchMetrics:    500,
	}
}

// Sanitize fills zero fields with defaults and clamps out-of-range values,
// so a bad hub response can never make the agent spin or stall.
func Sanitize(c payload.ServerConfig) payload.ServerConfig {
	d := DefaultServerConfig()
	if c.IntervalSeconds == 0 {
		c.IntervalSeconds = d.IntervalSeconds
	}
	if c.IntervalSeconds < MinInterval {
		c.IntervalSeconds = MinInterval
	}
	if c.IntervalSeconds > MaxInterval {
		c.IntervalSeconds = MaxInterval
	}
	if c.MaxBatchMetrics <= 0 {
		c.MaxBatchMetrics = d.MaxBatchMetrics
	}
	if c.MaxBatchMetrics > 5000 {
		c.MaxBatchMetrics = 5000
	}
	if c.DiskExcludeFstypes == nil {
		c.DiskExcludeFstypes = d.DiskExcludeFstypes
	}
	if c.WatchServices == nil {
		c.WatchServices = []string{}
	}
	return c
}

// ValidateURL enforces "HTTPS only unless --insecure-dev".
func ValidateURL(raw string, insecureDev bool) (string, error) {
	raw = strings.TrimRight(strings.TrimSpace(raw), "/")
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" {
		return "", fmt.Errorf("invalid hub url %q", raw)
	}
	switch u.Scheme {
	case "https":
	case "http":
		if !insecureDev {
			return "", errors.New("refusing http:// hub url (token would travel in clear text); use https or pass --insecure-dev on a dev box")
		}
	default:
		return "", fmt.Errorf("unsupported url scheme %q", u.Scheme)
	}
	return raw, nil
}

// Load reads and validates agent.json.
func Load(path string) (File, error) {
	var f File
	b, err := os.ReadFile(path)
	if err != nil {
		return f, err
	}
	if err := json.Unmarshal(b, &f); err != nil {
		return f, fmt.Errorf("parse %s: %w", path, err)
	}
	if f.URL == "" || f.AgentToken == "" {
		return f, fmt.Errorf("%s: url and agent_token are required (run `cloudops-agent enroll`)", path)
	}
	if f.ConfigVersion == 0 {
		f.ConfigVersion = 1
	}
	f.Config = Sanitize(f.Config)
	return f, nil
}

// Save writes agent.json atomically with mode 0600.
func Save(path string, f File) error {
	b, err := json.MarshalIndent(f, "", "  ")
	if err != nil {
		return err
	}
	return WriteFileAtomic(path, append(b, '\n'), 0o600)
}

// WriteFileAtomic writes via a temp file in the same directory + rename.
func WriteFileAtomic(path string, data []byte, perm os.FileMode) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".tmp-*")
	if err != nil {
		return err
	}
	name := tmp.Name()
	cleanup := func() { _ = os.Remove(name) }
	if err := tmp.Chmod(perm); err != nil {
		tmp.Close()
		cleanup()
		return err
	}
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		cleanup()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		cleanup()
		return err
	}
	if err := tmp.Close(); err != nil {
		cleanup()
		return err
	}
	if err := os.Rename(name, path); err != nil {
		cleanup()
		return err
	}
	return nil
}
