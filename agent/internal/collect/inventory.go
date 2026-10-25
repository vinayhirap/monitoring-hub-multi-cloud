package collect

import (
	"context"
	"sort"
	"strings"

	"cloudops-agent/internal/payload"
)

// maxInventory bounds one snapshot (the hub keeps at most this many per host). Services a person installed or enabled are kept
// ahead of OS plumbing when a host has more than this.
const maxInventory = 1000

// inventory lists every service the OS service manager knows about, with names that can be put in watch_services as-is.
// It returns nil (not an empty slice) when the scan failed, so the hub keeps the previous snapshot.
func (c *Collector) inventory(ctx context.Context) []payload.InventoryItem {
	if c.opt.Run == nil {
		return nil
	}
	var items []payload.InventoryItem
	switch c.opt.GOOS {
	case "windows":
		out, err := c.opt.Run(ctx, "sc.exe", "query", "state=", "all")
		if err != nil && len(out) == 0 {
			return nil
		}
		items = parseScInventory(string(out))
	case "darwin":
		out, err := c.opt.Run(ctx, "launchctl", "list")
		if err != nil && len(out) == 0 {
			return nil
		}
		items = launchctlInventory(parseLaunchctlList(string(out)))
	default:
		units, uerr := c.opt.Run(ctx, "systemctl", "list-units", "--type=service", "--all", "--no-legend", "--no-pager", "--plain")
		files, ferr := c.opt.Run(ctx, "systemctl", "list-unit-files", "--type=service", "--no-legend", "--no-pager", "--plain")
		if (uerr != nil && len(units) == 0) && (ferr != nil && len(files) == 0) {
			return nil
		}
		items = mergeSystemd(parseSystemdUnits(string(units)), parseSystemdUnitFiles(string(files)))
	}
	return capInventory(items)
}

// capInventory drops entries the watch list could not accept, de-duplicates by name, keeps the services a person is likely to
// want first when truncating, and returns the result sorted by name. Never returns nil for a successful scan.
func capInventory(in []payload.InventoryItem) []payload.InventoryItem {
	seen := map[string]bool{}
	out := make([]payload.InventoryItem, 0, len(in))
	for _, it := range in {
		if !validUnit(it.Name) || len(it.Name) > 100 || seen[it.Name] {
			continue
		}
		seen[it.Name] = true
		it.Description = clip(it.Description, 160)
		out = append(out, it)
	}
	if len(out) > maxInventory {
		sort.SliceStable(out, func(i, j int) bool { return !plumbing(out[i].Startup) && plumbing(out[j].Startup) })
		out = out[:maxInventory]
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

func plumbing(startup string) bool {
	switch startup {
	case "static", "indirect", "generated", "transient", "system":
		return true
	}
	return false
}

func clip(s string, n int) string {
	s = strings.Join(strings.Fields(s), " ")
	if len(s) <= n {
		return s
	}
	return strings.TrimSpace(s[:n])
}

// ── systemd ──────────────────────────────────────────────────────────────────────────────────────────────────

type unitInfo struct{ active, desc string }

// parseSystemdUnits parses `systemctl list-units --type=service --all --no-legend --plain`:
// "UNIT LOAD ACTIVE SUB DESCRIPTION...". Units systemd could not load or that are masked are left out. A leading status
// bullet (older systemd prints one for failed units even with --plain) is ignored.
func parseSystemdUnits(out string) map[string]unitInfo {
	res := map[string]unitInfo{}
	for _, l := range strings.Split(out, "\n") {
		f := strings.Fields(l)
		if len(f) > 0 && (f[0] == "●" || f[0] == "*" || f[0] == "○" || f[0] == "×") {
			f = f[1:]
		}
		if len(f) < 4 || !strings.HasSuffix(f[0], ".service") {
			continue
		}
		if f[1] == "not-found" || f[1] == "masked" || f[1] == "bad-setting" || f[1] == "error" {
			continue
		}
		desc := f[4:]
		// A unit in the middle of a transition has a JOB column (start, stop, reload, restart) between SUB and DESCRIPTION.
		if len(desc) > 1 && (f[2] == "activating" || f[2] == "deactivating" || f[2] == "reloading") {
			switch desc[0] {
			case "start", "stop", "reload", "restart", "try-restart", "reload-or-restart":
				desc = desc[1:]
			}
		}
		res[strings.TrimSuffix(f[0], ".service")] = unitInfo{active: f[2], desc: strings.Join(desc, " ")}
	}
	return res
}

// parseSystemdUnitFiles parses `systemctl list-unit-files --type=service --no-legend --plain`: "UNIT STATE [PRESET]".
// Masked, alias and broken entries are dropped: nothing useful can be watched there.
func parseSystemdUnitFiles(out string) map[string]string {
	res := map[string]string{}
	for _, l := range strings.Split(out, "\n") {
		f := strings.Fields(l)
		if len(f) < 2 || !strings.HasSuffix(f[0], ".service") {
			continue
		}
		switch f[1] {
		case "masked", "masked-runtime", "alias", "bad", "not-found", "linked-runtime":
			continue
		}
		res[strings.TrimSuffix(f[0], ".service")] = f[1]
	}
	return res
}

// mergeSystemd joins both views: a unit file nobody has loaded (installed but never started) is "stopped"; a loaded unit with
// no unit file (generated or transient) is reported with that startup class. Template units (foo@.service) are skipped, running
// instances (foo@bar) are kept because they are real, watchable services.
func mergeSystemd(units map[string]unitInfo, files map[string]string) []payload.InventoryItem {
	names := map[string]bool{}
	for n := range units {
		names[n] = true
	}
	for n := range files {
		names[n] = true
	}
	out := make([]payload.InventoryItem, 0, len(names))
	for n := range names {
		if strings.HasSuffix(n, "@") {
			continue
		}
		it := payload.InventoryItem{Name: n, State: "stopped", Startup: files[n]}
		if u, ok := units[n]; ok {
			it.State = mapState(u.active)
			it.Description = u.desc
			if it.Startup == "" {
				it.Startup = "generated"
			}
		}
		out = append(out, it)
	}
	return out
}

// ── Windows ──────────────────────────────────────────────────────────────────────────────────────────────────

// parseScInventory parses `sc query state= all` keeping the service KEY name's original case (the watch list matches
// case-insensitively) and the DISPLAY_NAME as the description.
func parseScInventory(out string) []payload.InventoryItem {
	var res []payload.InventoryItem
	var cur *payload.InventoryItem
	for _, line := range strings.Split(out, "\n") {
		l := strings.TrimSpace(line)
		switch {
		case strings.HasPrefix(l, "SERVICE_NAME:"):
			res = append(res, payload.InventoryItem{Name: strings.TrimSpace(strings.TrimPrefix(l, "SERVICE_NAME:")), State: "unknown"})
			cur = &res[len(res)-1]
		case cur != nil && strings.HasPrefix(l, "DISPLAY_NAME:"):
			cur.Description = strings.TrimSpace(strings.TrimPrefix(l, "DISPLAY_NAME:"))
		case cur != nil && strings.HasPrefix(l, "STATE"):
			if _, rest, ok := strings.Cut(l, ":"); ok {
				if f := strings.Fields(rest); len(f) > 0 {
					cur.State = scState(f[0])
				}
			}
			cur = nil
		}
	}
	return res
}

// ── macOS ────────────────────────────────────────────────────────────────────────────────────────────────────

// launchctlInventory turns parseLaunchctlList's label -> state map into inventory items. Apple's own labels and launchd's
// ephemeral per-app labels are marked "system" so the picker hides them by default (they stay selectable).
func launchctlInventory(states map[string]string) []payload.InventoryItem {
	out := make([]payload.InventoryItem, 0, len(states))
	for label, st := range states {
		it := payload.InventoryItem{Name: label, State: st}
		if strings.HasPrefix(label, "com.apple.") || strings.HasPrefix(label, "application.") || strings.HasPrefix(label, "0x") {
			it.Startup = "system"
		}
		out = append(out, it)
	}
	return out
}
