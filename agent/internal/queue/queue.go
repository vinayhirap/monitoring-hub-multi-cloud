// Package queue is the bounded on-disk buffer used while the hub is
// unreachable. One file per payload, named <seq>_<sent_at>.q, so ordering
// survives restarts without an index. Oldest entries are dropped first when
// the byte budget is exceeded, and entries older than MaxAge are discarded
// instead of replayed (the hub rejects samples older than 24 h anyway).
package queue

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	DefaultMaxBytes = 20 << 20
	DefaultMaxAge   = 23 * time.Hour // hub drops samples > 24 h old; keep a margin
)

var ErrTooLarge = errors.New("payload larger than the whole queue budget")

type Entry struct {
	Seq    int64
	SentAt int64
	Body   []byte
}

type item struct {
	name   string
	seq    int64
	sentAt int64
	size   int64
}

type Queue struct {
	dir      string
	maxBytes int64
	maxAge   time.Duration
	now      func() time.Time

	mu      sync.Mutex
	items   []item // ascending by seq
	total   int64
	dropped uint64 // evicted to stay within maxBytes
	expired uint64 // discarded for age
}

// Open creates dir (0700) and indexes whatever is already queued.
func Open(dir string, maxBytes int64, maxAge time.Duration, now func() time.Time) (*Queue, error) {
	if maxBytes <= 0 {
		maxBytes = DefaultMaxBytes
	}
	if maxAge <= 0 {
		maxAge = DefaultMaxAge
	}
	if now == nil {
		now = time.Now
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	q := &Queue{dir: dir, maxBytes: maxBytes, maxAge: maxAge, now: now}
	ents, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	for _, e := range ents {
		name := e.Name()
		if strings.HasPrefix(name, ".tmp-") { // interrupted write
			_ = os.Remove(filepath.Join(dir, name))
			continue
		}
		seq, sent, ok := parseName(name)
		if !ok || e.IsDir() {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		q.items = append(q.items, item{name: name, seq: seq, sentAt: sent, size: info.Size()})
		q.total += info.Size()
	}
	sort.Slice(q.items, func(i, j int) bool { return q.items[i].seq < q.items[j].seq })
	q.evictLocked(0) // the budget may have been lowered since the last run
	return q, nil
}

func fileName(seq, sentAt int64) string { return fmt.Sprintf("%020d_%012d.q", seq, sentAt) }

func parseName(name string) (seq, sentAt int64, ok bool) {
	base, found := strings.CutSuffix(name, ".q")
	if !found {
		return 0, 0, false
	}
	a, b, found := strings.Cut(base, "_")
	if !found {
		return 0, 0, false
	}
	s, e1 := strconv.ParseInt(a, 10, 64)
	t, e2 := strconv.ParseInt(b, 10, 64)
	if e1 != nil || e2 != nil {
		return 0, 0, false
	}
	return s, t, true
}

// Push appends a payload body, evicting the oldest entries if needed.
func (q *Queue) Push(seq, sentAt int64, body []byte) error {
	q.mu.Lock()
	defer q.mu.Unlock()
	size := int64(len(body))
	if size > q.maxBytes {
		return ErrTooLarge
	}
	q.removeSeqLocked(seq) // same seq re-queued: replace
	q.evictLocked(size)
	name := fileName(seq, sentAt)
	tmp, err := os.CreateTemp(q.dir, ".tmp-*")
	if err != nil {
		return err
	}
	if err := tmp.Chmod(0o600); err == nil {
		_, err = tmp.Write(body)
	}
	if cerr := tmp.Close(); err == nil {
		err = cerr
	}
	if err == nil {
		err = os.Rename(tmp.Name(), filepath.Join(q.dir, name))
	}
	if err != nil {
		_ = os.Remove(tmp.Name())
		return err
	}
	it := item{name: name, seq: seq, sentAt: sentAt, size: size}
	i := sort.Search(len(q.items), func(i int) bool { return q.items[i].seq > seq })
	q.items = append(q.items, item{})
	copy(q.items[i+1:], q.items[i:])
	q.items[i] = it
	q.total += size
	return nil
}

// evictLocked drops oldest entries until `incoming` more bytes fit.
func (q *Queue) evictLocked(incoming int64) {
	for len(q.items) > 0 && q.total+incoming > q.maxBytes {
		q.removeLocked(0)
		q.dropped++
	}
}

func (q *Queue) removeLocked(i int) {
	it := q.items[i]
	_ = os.Remove(filepath.Join(q.dir, it.name))
	q.total -= it.size
	q.items = append(q.items[:i], q.items[i+1:]...)
}

func (q *Queue) removeSeqLocked(seq int64) {
	for i, it := range q.items {
		if it.seq == seq {
			q.removeLocked(i)
			return
		}
	}
}

// Peek returns the oldest replayable entry without removing it. Expired or
// unreadable entries are discarded on the way. ok is false when empty.
func (q *Queue) Peek() (e Entry, ok bool) {
	q.mu.Lock()
	defer q.mu.Unlock()
	cutoff := q.now().Add(-q.maxAge).Unix()
	for len(q.items) > 0 {
		it := q.items[0]
		if it.sentAt < cutoff {
			q.removeLocked(0)
			q.expired++
			continue
		}
		b, err := os.ReadFile(filepath.Join(q.dir, it.name))
		if err != nil || len(b) == 0 {
			q.removeLocked(0)
			q.dropped++
			continue
		}
		return Entry{Seq: it.seq, SentAt: it.sentAt, Body: b}, true
	}
	return Entry{}, false
}

// Ack removes an entry after the hub accepted (or permanently rejected) it.
func (q *Queue) Ack(seq int64) {
	q.mu.Lock()
	defer q.mu.Unlock()
	q.removeSeqLocked(seq)
}

func (q *Queue) Len() int {
	q.mu.Lock()
	defer q.mu.Unlock()
	return len(q.items)
}

func (q *Queue) Bytes() int64 {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.total
}

// Dropped is the number of entries evicted for space (or unreadable).
func (q *Queue) Dropped() uint64 {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.dropped
}

// Expired is the number of entries discarded for exceeding MaxAge.
func (q *Queue) Expired() uint64 {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.expired
}

// MaxSeq is the highest queued seq (0 when empty), used to keep the
// sequence counter monotonic even if state.json was lost.
func (q *Queue) MaxSeq() int64 {
	q.mu.Lock()
	defer q.mu.Unlock()
	if len(q.items) == 0 {
		return 0
	}
	return q.items[len(q.items)-1].seq
}
