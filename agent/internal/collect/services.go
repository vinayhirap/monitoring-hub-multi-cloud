package collect

import (
	"context"
	"os/exec"
	"regexp"
	"strings"

	"cloudops-agent/internal/payload"
)

// Valid systemd unit-name characters (plus @ for template instances). Anything
// else is never passed to systemctl, so a bad watch_services entry cannot
// become an option or a shell/argv injection.
var unitNameRe = regexp.MustCompile(`^[A-Za-z0-9:_.@\\-]+$`)

func validUnit(n string) bool {
	return len(n) > 0 && len(n) <= 256 && n[0] != '-' && unitNameRe.MatchString(n)
}

func mapState(s string) string {
	switch s {
	case "active":
		return "running"
	case "inactive":
		return "stopped"
	case "failed":
		return "failed"
	default: // activating, deactivating, reloading, "unknown", errors
		return "unknown"
	}
}

// services reports the state of each watched unit with ONE `systemctl
// is-active` call (it prints one line per unit, in argument order). The
// result slice is never nil so the payload serialises as [] not null.
func (c *Collector) services(ctx context.Context, names []string) []payload.Service {
	out := make([]payload.Service, 0, len(names))
	if len(names) == 0 {
		return out
	}
	seen := map[string]bool{}
	var valid []string
	for _, n := range names {
		n = strings.TrimSpace(n)
		if seen[n] {
			continue
		}
		seen[n] = true
		if validUnit(n) {
			valid = append(valid, n)
		} else {
			out = append(out, payload.Service{Name: n, State: "unknown"})
		}
	}
	states := map[string]string{}
	if len(valid) > 0 {
		states = c.systemctlStates(ctx, valid)
	}
	res := make([]payload.Service, 0, len(valid)+len(out))
	for _, n := range valid {
		res = append(res, payload.Service{Name: n, State: states[n]})
	}
	return append(res, out...)
}

func (c *Collector) systemctlStates(ctx context.Context, units []string) map[string]string {
	states := make(map[string]string, len(units))
	unknown := func() map[string]string {
		for _, u := range units {
			states[u] = "unknown"
		}
		return states
	}
	if c.opt.Run == nil {
		return unknown()
	}
	args := append([]string{"is-active", "--"}, units...)
	out, err := c.opt.Run(ctx, "systemctl", args...)
	if err != nil && len(out) == 0 {
		return unknown()
	}
	lines := strings.Split(strings.TrimSpace(string(out)), "\n")
	if len(lines) != len(units) {
		return unknown()
	}
	for i, u := range units {
		states[u] = mapState(strings.TrimSpace(lines[i]))
	}
	return states
}

// SystemctlAvailable reports whether systemctl is on PATH (used by `check`).
func SystemctlAvailable() bool {
	_, err := exec.LookPath("systemctl")
	return err == nil
}
