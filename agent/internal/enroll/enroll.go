// Package enroll implements `cloudops-agent enroll`: exchange a one-time
// enrollment key for an agent token and write /etc/cloudops-agent/agent.json.
package enroll

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/user"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"time"

	"cloudops-agent/internal/config"
	"cloudops-agent/internal/payload"
)

const (
	EnrollPath   = "/api/host-agents/enroll"
	DefaultIMDS  = "http://169.254.169.254"
	imdsTimeout  = 1 * time.Second
	DefaultOwner = "cloudops-agent"
)

var instanceIDRe = regexp.MustCompile(`^i-[0-9a-f]{8,32}$`)

// Options drives Run. HTTP and IMDSBase are injectable for tests.
type Options struct {
	URL         string
	Key         string
	Hostname    string // default os.Hostname()
	ConfigPath  string
	Force       bool
	InsecureDev bool
	Version     string
	Owner       string // chown agent.json to this user when running as root ("" = skip)
	IMDSBase    string
	HTTP        *http.Client
	Out         io.Writer
	Warn        io.Writer
}

type request struct {
	EnrollmentKey string  `json:"enrollment_key"`
	Hostname      string  `json:"hostname"`
	InstanceID    *string `json:"instance_id,omitempty"`
	OS            string  `json:"os"`
	Arch          string  `json:"arch"`
	AgentVersion  string  `json:"agent_version"`
}

// Run performs the enrollment end to end.
func Run(ctx context.Context, o Options) error {
	if o.Out == nil {
		o.Out = io.Discard
	}
	if o.Warn == nil {
		o.Warn = io.Discard
	}
	if o.ConfigPath == "" {
		o.ConfigPath = config.DefaultPath
	}
	if o.IMDSBase == "" {
		o.IMDSBase = DefaultIMDS
	}
	if strings.TrimSpace(o.Key) == "" {
		return errors.New("an enrollment key is required (--key cak_... or CLOUDOPS_AGENT_ENROLL_KEY)")
	}
	base, err := config.ValidateURL(o.URL, o.InsecureDev)
	if err != nil {
		return err
	}
	if _, err := os.Stat(o.ConfigPath); err == nil && !o.Force {
		return fmt.Errorf("%s already exists; this host is enrolled. Use --force to enroll again (the old token stays valid until revoked on the hub)", o.ConfigPath)
	}
	host := o.Hostname
	if host == "" {
		if host, err = os.Hostname(); err != nil || host == "" {
			return errors.New("cannot determine hostname; pass --hostname")
		}
	}
	httpc := o.HTTP
	if httpc == nil {
		httpc = &http.Client{
			Timeout:       15 * time.Second,
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		}
	}

	req := request{EnrollmentKey: strings.TrimSpace(o.Key), Hostname: host, OS: runtime.GOOS, Arch: runtime.GOARCH, AgentVersion: o.Version}
	iid := InstanceID(ctx, o.IMDSBase)
	if iid != "" {
		req.InstanceID = &iid
	}

	resp, err := post(ctx, httpc, base+EnrollPath, req)
	if err != nil {
		return err
	}
	if !strings.HasPrefix(resp.AgentToken, "cah_") || resp.HostAgentID <= 0 {
		return errors.New("hub returned an unexpected enrollment response (missing agent_token/host_agent_id)")
	}
	f := config.File{
		URL: base, AgentToken: resp.AgentToken, HostAgentID: resp.HostAgentID, Hostname: host,
		InstanceID: iid, ConfigVersion: 1, // the enroll response carries no config_version; ingest corrects it
		Config: config.Sanitize(resp.Config),
	}
	if err := config.Save(o.ConfigPath, f); err != nil {
		return fmt.Errorf("write %s: %w", o.ConfigPath, err)
	}
	chownBestEffort(o.ConfigPath, o.Owner, o.Warn)

	fmt.Fprintf(o.Out, "enrolled: host_agent_id=%d hostname=%s instance_id=%s\n", resp.HostAgentID, host, orDash(iid))
	fmt.Fprintf(o.Out, "wrote %s. %s\n", o.ConfigPath, config.StartHint(runtime.GOOS))
	return nil
}

func post(ctx context.Context, c *http.Client, url string, body request) (payload.EnrollResponse, error) {
	var out payload.EnrollResponse
	b, _ := json.Marshal(body)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(b))
	if err != nil {
		return out, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", "cloudops-agent/"+body.AgentVersion)
	resp, err := c.Do(req)
	if err != nil {
		return out, fmt.Errorf("enroll request failed: %s", strings.ReplaceAll(err.Error(), body.EnrollmentKey, "[redacted]"))
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode == http.StatusOK {
		if err := json.Unmarshal(raw, &out); err != nil {
			return out, fmt.Errorf("enroll: hub returned HTTP 200 but not JSON: %w", err)
		}
		return out, nil
	}
	msg := detail(raw)
	switch resp.StatusCode {
	case http.StatusNotFound:
		return out, fmt.Errorf("enroll failed: HTTP 404 - host agents are not enabled on the hub (HOST_AGENT_ENABLED) or the url is wrong")
	case http.StatusTooManyRequests:
		return out, fmt.Errorf("enroll failed: HTTP 429 - rate limited, retry later%s", suffix(msg))
	case http.StatusUnauthorized, http.StatusForbidden:
		return out, fmt.Errorf("enroll failed: HTTP %d - enrollment key invalid, expired, revoked or used up%s", resp.StatusCode, suffix(msg))
	default:
		return out, fmt.Errorf("enroll failed: HTTP %d%s", resp.StatusCode, suffix(msg))
	}
}

func suffix(m string) string {
	if m == "" {
		return ""
	}
	return ": " + m
}

func detail(raw []byte) string {
	var e struct {
		Detail any `json:"detail"`
	}
	s := ""
	if json.Unmarshal(raw, &e) == nil && e.Detail != nil {
		if str, ok := e.Detail.(string); ok {
			s = str
		} else {
			b, _ := json.Marshal(e.Detail)
			s = string(b)
		}
	} else {
		s = string(raw)
	}
	s = strings.Join(strings.Fields(s), " ")
	if len(s) > 200 {
		s = s[:200] + "..."
	}
	return s
}

func orDash(s string) string {
	if s == "" {
		return "-"
	}
	return s
}

// InstanceID asks IMDSv2 for the EC2 instance id. It returns "" on ANY
// problem (not on EC2, IMDS disabled, slow, malformed): enrollment must never
// fail because of it. IMDSv1 is deliberately not used, and the request
// bypasses any HTTP proxy configured in the environment.
func InstanceID(ctx context.Context, base string) string {
	ctx, cancel := context.WithTimeout(ctx, 2*imdsTimeout)
	defer cancel()
	c := &http.Client{
		Timeout:       imdsTimeout,
		Transport:     &http.Transport{Proxy: nil, DisableKeepAlives: true},
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
	treq, err := http.NewRequestWithContext(ctx, http.MethodPut, strings.TrimRight(base, "/")+"/latest/api/token", nil)
	if err != nil {
		return ""
	}
	treq.Header.Set("X-aws-ec2-metadata-token-ttl-seconds", "60")
	tresp, err := c.Do(treq)
	if err != nil {
		return ""
	}
	tb, _ := io.ReadAll(io.LimitReader(tresp.Body, 1024))
	tresp.Body.Close()
	if tresp.StatusCode != http.StatusOK || len(tb) == 0 {
		return ""
	}
	ireq, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(base, "/")+"/latest/meta-data/instance-id", nil)
	if err != nil {
		return ""
	}
	ireq.Header.Set("X-aws-ec2-metadata-token", strings.TrimSpace(string(tb)))
	iresp, err := c.Do(ireq)
	if err != nil {
		return ""
	}
	defer iresp.Body.Close()
	ib, _ := io.ReadAll(io.LimitReader(iresp.Body, 256))
	id := strings.TrimSpace(string(ib))
	if iresp.StatusCode != http.StatusOK || !instanceIDRe.MatchString(id) {
		return ""
	}
	return id
}

// chownBestEffort hands agent.json to the service user. `enroll` normally runs
// as root (it writes /etc), while the service runs as cloudops-agent and must
// be able to read the 0600 file.
func chownBestEffort(path, owner string, warn io.Writer) {
	if owner == "" || os.Geteuid() != 0 {
		return
	}
	u, err := user.Lookup(owner)
	if err != nil {
		fmt.Fprintf(warn, "warning: user %q not found; %s stays root-owned and the service will not be able to read it\n", owner, path)
		return
	}
	uid, _ := strconv.Atoi(u.Uid)
	gid, _ := strconv.Atoi(u.Gid)
	if err := os.Chown(path, uid, gid); err != nil {
		fmt.Fprintf(warn, "warning: chown %s to %s failed: %v\n", path, owner, err)
	}
}
