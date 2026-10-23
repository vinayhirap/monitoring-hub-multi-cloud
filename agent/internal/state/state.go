// Package state persists the little the agent must remember across restarts
// and that the (read-only) /etc config cannot hold: the ingest sequence
// counter (so seq stays monotonic) and the last config pushed by the hub.
package state

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sync"

	"cloudops-agent/internal/config"
	"cloudops-agent/internal/payload"
)

type State struct {
	Seq           int64                 `json:"seq"`
	ConfigVersion int                   `json:"config_version"`
	Config        *payload.ServerConfig `json:"config,omitempty"`
}

type Store struct {
	path string
	mu   sync.Mutex
	s    State
}

// Open loads <dir>/state.json, creating dir (0700) if needed. A missing or
// corrupt file starts from zero: the agent must never refuse to start
// because of its own scratch state.
func Open(dir string) (*Store, error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	st := &Store{path: filepath.Join(dir, "state.json")}
	if b, err := os.ReadFile(st.path); err == nil {
		var s State
		if json.Unmarshal(b, &s) == nil && s.Seq >= 0 {
			st.s = s
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	return st, nil
}

func (s *Store) Get() State {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.s
}

// Raise lifts the counter to at least n (used with the highest seq still
// sitting in the disk queue, e.g. if state.json was lost).
func (s *Store) Raise(n int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if n > s.s.Seq {
		s.s.Seq = n
	}
}

// NextSeq returns the next sequence number and persists it BEFORE it is
// used, so a crash can never cause a seq to be handed out twice. The
// in-memory counter advances even if the write fails; the error is returned
// for logging only.
func (s *Store) NextSeq() (int64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.s.Seq++
	return s.s.Seq, s.saveLocked()
}

func (s *Store) SetConfig(version int, c payload.ServerConfig) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.s.ConfigVersion = version
	cc := c
	s.s.Config = &cc
	return s.saveLocked()
}

func (s *Store) saveLocked() error {
	b, err := json.Marshal(s.s)
	if err != nil {
		return err
	}
	return config.WriteFileAtomic(s.path, b, 0o600)
}
