package config

import "testing"

func TestPathsForEveryOS(t *testing.T) {
	cases := []struct {
		goos, pd, cfg, data string
	}{
		{"linux", "", "/etc/cloudops-agent/agent.json", "/var/lib/cloudops-agent"},
		{"darwin", "", "/etc/cloudops-agent/agent.json", "/var/lib/cloudops-agent"},
		{"windows", `C:\ProgramData`, `C:\ProgramData\CloudOpsAgent\agent.json`, `C:\ProgramData\CloudOpsAgent\data`},
		{"windows", `D:\PD\`, `D:\PD\CloudOpsAgent\agent.json`, `D:\PD\CloudOpsAgent\data`},
		{"windows", "", `C:\ProgramData\CloudOpsAgent\agent.json`, `C:\ProgramData\CloudOpsAgent\data`},
	}
	for _, c := range cases {
		got := pathsFor(c.goos, c.pd)
		if got[0] != c.cfg || got[1] != c.data {
			t.Errorf("%s/%q: got %v want %s %s", c.goos, c.pd, got, c.cfg, c.data)
		}
	}
}

func TestStartHintNamesTheRightServiceManager(t *testing.T) {
	for goos, want := range map[string]string{"linux": "systemctl", "darwin": "launchctl", "windows": "ScheduledTask"} {
		if h := StartHint(goos); !contains(h, want) {
			t.Errorf("%s hint %q lacks %q", goos, h, want)
		}
	}
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
