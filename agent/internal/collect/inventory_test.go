package collect

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"cloudops-agent/internal/payload"
)

// Real `systemctl` output shapes (Ubuntu 22.04 / Amazon Linux 2023).
const sysUnits = `nginx.service                       loaded    active   running A high performance web server and a reverse proxy server
cron.service                        loaded    active   running Regular background program processing daemon
apparmor.service                    loaded    failed   failed  Load AppArmor profiles
● snapd.seeded.service              loaded    failed   failed  Wait until snapd is fully seeded
getty@tty1.service                  loaded    active   running Getty on tty1
ssh.service                         loaded    activating start  start OpenBSD Secure Shell server
ghost.service                       not-found inactive dead    ghost.service
blocked.service                     masked    inactive dead    blocked.service
`

const sysFiles = `nginx.service                          enabled         enabled
cron.service                           enabled         enabled
mysql.service                          disabled        enabled
getty@.service                         enabled         enabled
systemd-journald.service               static          -
dbus.service                           alias           -
blocked.service                        masked          enabled
apparmor.service                       enabled         enabled
`

func byNameInv(items []payload.InventoryItem) map[string]payload.InventoryItem {
	m := map[string]payload.InventoryItem{}
	for _, it := range items {
		m[it.Name] = it
	}
	return m
}

func TestSystemdInventoryMergesLoadedAndInstalledUnits(t *testing.T) {
	items := mergeSystemd(parseSystemdUnits(sysUnits), parseSystemdUnitFiles(sysFiles))
	m := byNameInv(items)
	want := map[string]payload.InventoryItem{
		"nginx":            {Name: "nginx", State: "running", Startup: "enabled", Description: "A high performance web server and a reverse proxy server"},
		"apparmor":         {Name: "apparmor", State: "failed", Startup: "enabled", Description: "Load AppArmor profiles"},
		"mysql":            {Name: "mysql", State: "stopped", Startup: "disabled"}, // installed, never started: not in list-units
		"systemd-journald": {Name: "systemd-journald", State: "stopped", Startup: "static"},
		"snapd.seeded":     {Name: "snapd.seeded", State: "failed", Startup: "generated", Description: "Wait until snapd is fully seeded"},
		"getty@tty1":       {Name: "getty@tty1", State: "running", Startup: "generated", Description: "Getty on tty1"},
		"ssh":              {Name: "ssh", State: "unknown", Startup: "generated", Description: "OpenBSD Secure Shell server"},
	}
	for n, w := range want {
		if m[n] != w {
			t.Errorf("%s = %+v, want %+v", n, m[n], w)
		}
	}
	for _, gone := range []string{"ghost", "blocked", "dbus", "getty@"} {
		if _, ok := m[gone]; ok {
			t.Errorf("%s must not be offered (not-found / masked / alias / template)", gone)
		}
	}
}

func TestInventoryNamesAreAlwaysWatchable(t *testing.T) {
	items := capInventory([]payload.InventoryItem{
		{Name: "nginx", State: "running"}, {Name: "nginx", State: "stopped"}, {Name: "-evil", State: "running"},
		{Name: "bad name", State: "running"}, {Name: strings.Repeat("a", 101), State: "running"}, {Name: "a;b", State: "running"},
		{Name: "ok@1", State: "running", Description: "  many   spaces\nhere "},
	})
	if len(items) != 2 || items[0].Name != "nginx" || items[0].State != "running" || items[1].Name != "ok@1" {
		t.Fatalf("got %+v", items)
	}
	if items[1].Description != "many spaces here" {
		t.Errorf("description = %q", items[1].Description)
	}
}

func TestInventoryCapKeepsInstalledServicesAheadOfPlumbing(t *testing.T) {
	var in []payload.InventoryItem
	for i := 0; i < maxInventory; i++ {
		in = append(in, payload.InventoryItem{Name: "plumb" + itoa(i), State: "running", Startup: "static"})
	}
	in = append(in, payload.InventoryItem{Name: "zz-mine", State: "running", Startup: "enabled"})
	out := capInventory(in)
	if len(out) != maxInventory {
		t.Fatalf("len = %d", len(out))
	}
	if _, ok := byNameInv(out)["zz-mine"]; !ok {
		t.Error("an enabled service must survive truncation ahead of static plumbing")
	}
}

func itoa(i int) string {
	const d = "0123456789"
	if i == 0 {
		return "0"
	}
	s := ""
	for ; i > 0; i /= 10 {
		s = string(d[i%10]) + s
	}
	return s
}

func TestLinuxInventoryRunsTwoReadOnlyCallsAndReportsNilWhenBothFail(t *testing.T) {
	h := newFakeHost(t)
	baseline(h)
	now := time.Unix(1760000000, 0)
	var calls [][]string
	run := func(_ context.Context, name string, args ...string) ([]byte, error) {
		calls = append(calls, append([]string{name}, args...))
		if args[0] == "list-units" {
			return []byte(sysUnits), nil
		}
		return []byte(sysFiles), nil
	}
	c := newCollector(h, &now, run)
	items := c.inventory(context.Background())
	if len(calls) != 2 || calls[0][1] != "list-units" || calls[1][1] != "list-unit-files" {
		t.Fatalf("calls = %v", calls)
	}
	for _, call := range calls {
		if call[0] != "systemctl" || !contains(call, "--no-pager") {
			t.Errorf("unexpected command %v", call)
		}
	}
	if len(items) == 0 || items[0].Name > items[len(items)-1].Name {
		t.Errorf("want a non-empty list sorted by name, got %d", len(items))
	}
	c2 := newCollector(h, &now, func(context.Context, string, ...string) ([]byte, error) { return nil, errors.New("boom") })
	if got := c2.inventory(context.Background()); got != nil {
		t.Errorf("a failed scan must be nil (hub keeps the old snapshot), got %+v", got)
	}
}

func contains(ss []string, s string) bool {
	for _, x := range ss {
		if x == s {
			return true
		}
	}
	return false
}

func TestWindowsInventoryKeepsKeyNameCaseAndDisplayName(t *testing.T) {
	m := byNameInv(parseScInventory(scOut))
	if got := m["Spooler"]; got != (payload.InventoryItem{Name: "Spooler", State: "running", Description: "Print Spooler"}) {
		t.Errorf("Spooler = %+v", got)
	}
	if m["W32Time"].State != "stopped" || m["BITS"].State != "unknown" || m["BITS"].Description != "Background Intelligent Transfer Service" {
		t.Errorf("W32Time/BITS = %+v / %+v", m["W32Time"], m["BITS"])
	}
	// the watch-list reporter and the inventory must agree on every state
	watch := parseScQuery(scOut)
	for n, it := range m {
		if watch[strings.ToLower(n)] != it.State {
			t.Errorf("%s: watch %q vs inventory %q", n, watch[strings.ToLower(n)], it.State)
		}
	}
}

func TestMacInventoryMarksAppleLabelsAsSystem(t *testing.T) {
	out := "PID\tStatus\tLabel\n412\t0\tcom.example.web\n-\t0\tcom.apple.Spotlight\n-\t78\tcom.example.crashed\n-\t0\tapplication.com.foo.1.2\n"
	m := byNameInv(launchctlInventory(parseLaunchctlList(out)))
	if m["com.example.web"] != (payload.InventoryItem{Name: "com.example.web", State: "running"}) {
		t.Errorf("web = %+v", m["com.example.web"])
	}
	if m["com.example.crashed"].State != "failed" || m["com.apple.Spotlight"].Startup != "system" || m["application.com.foo.1.2"].Startup != "system" {
		t.Errorf("got %+v", m)
	}
}

func TestInventoryOnlyRunsWhenRequested(t *testing.T) {
	h := newFakeHost(t)
	baseline(h)
	now := time.Unix(1760000000, 0)
	n := 0
	run := func(_ context.Context, name string, args ...string) ([]byte, error) {
		if len(args) > 0 && strings.HasPrefix(args[0], "list-unit") {
			n++
		}
		return []byte(sysUnits), nil
	}
	c := newCollector(h, &now, run)
	if r := c.Collect(context.Background(), Settings{}); r.Inventory != nil || n != 0 {
		t.Fatalf("no inventory unless asked: %+v calls=%d", r.Inventory, n)
	}
	if r := c.Collect(context.Background(), Settings{WithInventory: true}); len(r.Inventory) == 0 || n != 2 {
		t.Fatalf("inventory requested: %d items, %d calls", len(r.Inventory), n)
	}
}
