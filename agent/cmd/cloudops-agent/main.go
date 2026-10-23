// Command cloudops-agent is the CloudOps host agent: it collects metrics from
// /proc and /sys and pushes them to the hub. It never listens on a port.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"runtime"
	"syscall"
	"time"

	"cloudops-agent/internal/collect"
	"cloudops-agent/internal/config"
	"cloudops-agent/internal/enroll"
	"cloudops-agent/internal/runner"
)

// version is stamped at build time: -ldflags "-X main.version=0.1.0".
var version = "0.1.0-dev"

const usage = `cloudops-agent ` + "%s" + `

Usage:
  cloudops-agent enroll --url https://<hub> --key cak_... [--hostname NAME] [--config PATH] [--force] [--insecure-dev]
  cloudops-agent run    [--config PATH] [--data-dir DIR] [--queue-mb N] [--log-level info|debug]
  cloudops-agent check  [--config PATH] [--watch svc1,svc2]     (prints what would be sent; no network)
  cloudops-agent version

The enrollment key may also be given via CLOUDOPS_AGENT_ENROLL_KEY (keeps it out of ps output).
`

func main() { os.Exit(realMain(os.Args[1:], os.Stdout, os.Stderr)) }

func realMain(args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		fmt.Fprintf(stderr, usage, version)
		return 2
	}
	switch args[0] {
	case "version", "--version", "-v":
		fmt.Fprintf(stdout, "cloudops-agent %s (%s/%s, %s)\n", version, runtime.GOOS, runtime.GOARCH, runtime.Version())
		return 0
	case "enroll":
		return cmdEnroll(args[1:], stdout, stderr)
	case "run":
		return cmdRun(args[1:], stderr)
	case "check":
		return cmdCheck(args[1:], stdout, stderr)
	case "help", "--help", "-h":
		fmt.Fprintf(stdout, usage, version)
		return 0
	default:
		fmt.Fprintf(stderr, "unknown command %q\n\n", args[0])
		fmt.Fprintf(stderr, usage, version)
		return 2
	}
}

func cmdEnroll(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("enroll", flag.ContinueOnError)
	fs.SetOutput(stderr)
	url := fs.String("url", "", "hub base URL (https://...)")
	key := fs.String("key", os.Getenv("CLOUDOPS_AGENT_ENROLL_KEY"), "enrollment key (cak_...)")
	host := fs.String("hostname", "", "override the detected hostname")
	cfgPath := fs.String("config", config.DefaultPath, "where to write agent.json")
	force := fs.Bool("force", false, "overwrite an existing agent.json")
	insecure := fs.Bool("insecure-dev", false, "allow http:// hub URLs (DEV only)")
	owner := fs.String("owner", enroll.DefaultOwner, "chown agent.json to this user when run as root (empty to skip)")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	err := enroll.Run(ctx, enroll.Options{
		URL: *url, Key: *key, Hostname: *host, ConfigPath: *cfgPath, Force: *force, InsecureDev: *insecure,
		Version: version, Owner: *owner, Out: stdout, Warn: stderr,
	})
	if err != nil {
		fmt.Fprintln(stderr, "error:", err)
		return 1
	}
	return 0
}

func newLogger(w io.Writer, level string) *slog.Logger {
	var l slog.Level
	if err := l.UnmarshalText([]byte(level)); err != nil {
		l = slog.LevelInfo
	}
	// journald adds its own timestamp; keep lines short and key=value.
	return slog.New(slog.NewTextHandler(w, &slog.HandlerOptions{
		Level: l,
		ReplaceAttr: func(_ []string, a slog.Attr) slog.Attr {
			if a.Key == slog.TimeKey {
				return slog.Attr{}
			}
			return a
		},
	}))
}

// Exit codes: 0 clean stop, 1 fatal config/IO error, 3 hub rejected the token.
// The systemd unit lists 3 in RestartPreventExitStatus so a revoked agent
// stays down instead of restart-looping against the hub.
const exitUnauthorized = 3

func cmdRun(args []string, stderr io.Writer) int {
	fs := flag.NewFlagSet("run", flag.ContinueOnError)
	fs.SetOutput(stderr)
	cfgPath := fs.String("config", config.DefaultPath, "path to agent.json")
	dataDir := fs.String("data-dir", config.DefaultDataDir, "state + offline queue directory")
	queueMB := fs.Int("queue-mb", 20, "offline queue budget in MB (oldest dropped first)")
	level := fs.String("log-level", "info", "debug|info|warn|error")
	insecure := fs.Bool("insecure-dev", false, "allow an http:// hub URL (DEV only)")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	log := newLogger(stderr, *level)
	id, err := config.Load(*cfgPath)
	if err != nil {
		log.Error("cannot load config", "err", err)
		return 1
	}
	if _, err := config.ValidateURL(id.URL, *insecure); err != nil {
		log.Error("refusing to start", "err", err)
		return 1
	}
	r, err := runner.New(runner.Options{
		Identity: id, DataDir: *dataDir, Version: version, Logger: log, QueueBytes: int64(*queueMB) << 20,
	})
	if err != nil {
		log.Error("cannot initialise agent", "err", err)
		return 1
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	switch err := r.Run(ctx); {
	case err == nil:
		return 0
	case errors.Is(err, runner.ErrUnauthorized):
		return exitUnauthorized
	default:
		log.Error("agent failed", "err", err)
		return 1
	}
}

// cmdCheck collects twice (so rate metrics appear) and prints the exact
// payload that `run` would send. It never touches the network and never
// needs agent.json (it uses it, read-only, only to pick up watch_services
// and the hostname when present).
func cmdCheck(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("check", flag.ContinueOnError)
	fs.SetOutput(stderr)
	cfgPath := fs.String("config", config.DefaultPath, "optional agent.json (watch_services, hostname)")
	watch := fs.String("watch", "", "comma-separated systemd units to include (overrides config)")
	wait := fs.Duration("wait", 2*time.Second, "pause between the two samples used for rates")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	cfg := config.DefaultServerConfig()
	hostname, _ := os.Hostname()
	instance := ""
	if f, err := config.Load(*cfgPath); err == nil {
		cfg = f.Config
		hostname, instance = f.Hostname, f.InstanceID
	} else {
		fmt.Fprintf(stderr, "note: no usable %s (%v); using defaults\n", *cfgPath, err)
	}
	if *watch != "" {
		cfg.WatchServices = splitCSV(*watch)
	}
	c := collect.New(collect.Options{})
	s := collect.Settings{WatchServices: cfg.WatchServices, DiskExcludeFstypes: cfg.DiskExcludeFstypes}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	c.Collect(ctx, s) // baseline for rates
	select {
	case <-time.After(*wait):
	case <-ctx.Done():
		return 1
	}
	res := c.Collect(ctx, s)

	var iid *string
	if instance != "" {
		iid = &instance
	}
	out := map[string]any{
		"note":          "dry run: nothing was sent",
		"host":          map[string]any{"hostname": hostname, "instance_id": iid, "os": runtime.GOOS, "arch": runtime.GOARCH, "boot_time": res.BootTime},
		"agent_version": version, "schema": 1, "interval_seconds": cfg.IntervalSeconds,
		"metrics": nonNil(res.Metrics), "services": res.Services,
	}
	enc := json.NewEncoder(stdout)
	enc.SetIndent("", "  ")
	_ = enc.Encode(out)
	for _, e := range res.Errs {
		fmt.Fprintln(stderr, "collector problem:", e)
	}
	if len(cfg.WatchServices) > 0 && !collect.SystemctlAvailable() {
		fmt.Fprintln(stderr, "warning: watch_services is set but systemctl was not found; services report as unknown")
	}
	return 0
}

func nonNil[T any](s []T) []T {
	if s == nil {
		return []T{}
	}
	return s
}

func splitCSV(s string) []string {
	var out []string
	cur := ""
	for _, r := range s {
		if r == ',' {
			if cur != "" {
				out = append(out, cur)
			}
			cur = ""
			continue
		}
		if r != ' ' {
			cur += string(r)
		}
	}
	if cur != "" {
		out = append(out, cur)
	}
	return out
}
