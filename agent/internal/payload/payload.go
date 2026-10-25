// Package payload defines the wire types shared by the collectors, the
// sender and the enroll client. They mirror INGEST PAYLOAD v1 and the
// CONFIG object of the hub contract exactly.
package payload

import "math"

// SchemaVersion is the only ingest schema this agent speaks.
const SchemaVersion = 1

type Metric struct {
	Name   string            `json:"name"`
	Value  float64           `json:"value"`
	Ts     int64             `json:"ts"`
	Labels map[string]string `json:"labels,omitempty"`
}

type Service struct {
	Name  string `json:"name"`
	State string `json:"state"` // running | stopped | failed | unknown
}

// InventoryItem is one service that EXISTS on the host (not just a watched one): the hub stores the latest snapshot so the
// UI can offer a picker. Name is exactly what can be put in watch_services. Startup and Description are optional hints.
type InventoryItem struct {
	Name        string `json:"name"`
	State       string `json:"state"`                 // running | stopped | failed | unknown
	Startup     string `json:"startup,omitempty"`     // systemd unit-file state (enabled, disabled, static, ...) or "system" on macOS
	Description string `json:"description,omitempty"` // systemd Description / Windows display name
}

type Host struct {
	Hostname   string  `json:"hostname"`
	InstanceID *string `json:"instance_id"`
	OS         string  `json:"os"`
	Arch       string  `json:"arch"`
	BootTime   int64   `json:"boot_time"`
}

type Payload struct {
	Schema        int       `json:"schema"`
	AgentVersion  string    `json:"agent_version"`
	ConfigVersion int       `json:"config_version"`
	Seq           int64     `json:"seq"`
	SentAt        int64     `json:"sent_at"`
	Host          Host      `json:"host"`
	Metrics       []Metric  `json:"metrics"`
	Services      []Service `json:"services"`
	// ServiceInventory is sent on the first report after start and then about every 30 minutes, never on every report.
	// Absent (omitted) means "no news"; the hub never reads an absent list as "no services exist".
	ServiceInventory []InventoryItem `json:"service_inventory,omitempty"`
}

// ServerConfig is the hub-controlled configuration.
type ServerConfig struct {
	IntervalSeconds    int      `json:"interval_seconds"`
	WatchServices      []string `json:"watch_services"`
	DiskExcludeFstypes []string `json:"disk_exclude_fstypes"`
	MaxBatchMetrics    int      `json:"max_batch_metrics"`
}

// IngestResponse is the 200 body of POST /api/host-agents/ingest.
type IngestResponse struct {
	Accepted      int           `json:"accepted"`
	Rejected      int           `json:"rejected"`
	ConfigVersion int           `json:"config_version"`
	Config        *ServerConfig `json:"config,omitempty"`
}

// EnrollResponse is the 200 body of POST /api/host-agents/enroll.
type EnrollResponse struct {
	HostAgentID int64        `json:"host_agent_id"`
	AgentToken  string       `json:"agent_token"`
	Config      ServerConfig `json:"config"`
}

// Finite reports whether v can be encoded as JSON and accepted by the hub
// (NaN and +/-Inf are rejected server side and cannot be marshalled anyway).
func Finite(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) }
