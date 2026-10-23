package queue

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"
	"time"
)

var t0 = time.Unix(1760000000, 0)

func open(t *testing.T, dir string, max int64) *Queue {
	t.Helper()
	q, err := Open(dir, max, time.Hour, func() time.Time { return t0 })
	if err != nil {
		t.Fatal(err)
	}
	return q
}

func TestOrderAndAck(t *testing.T) {
	q := open(t, t.TempDir(), 1<<20)
	for _, s := range []int64{3, 1, 2} { // out-of-order insert still replays by seq
		if err := q.Push(s, t0.Unix(), []byte{byte('a' + s)}); err != nil {
			t.Fatal(err)
		}
	}
	for want := int64(1); want <= 3; want++ {
		e, ok := q.Peek()
		if !ok || e.Seq != want || string(e.Body) != string([]byte{byte('a' + want)}) {
			t.Fatalf("peek = %+v ok=%v, want seq %d", e, ok, want)
		}
		q.Ack(e.Seq)
	}
	if _, ok := q.Peek(); ok || q.Len() != 0 || q.Bytes() != 0 {
		t.Error("queue should be empty")
	}
}

func TestBoundedDropsOldest(t *testing.T) {
	dir := t.TempDir()
	q := open(t, dir, 100)
	body := bytes.Repeat([]byte("x"), 30)
	for s := int64(1); s <= 6; s++ {
		if err := q.Push(s, t0.Unix(), body); err != nil {
			t.Fatal(err)
		}
		if q.Bytes() > 100 {
			t.Fatalf("budget exceeded: %d", q.Bytes())
		}
	}
	if q.Len() != 3 || q.Dropped() != 3 {
		t.Errorf("len=%d dropped=%d, want 3/3", q.Len(), q.Dropped())
	}
	if e, _ := q.Peek(); e.Seq != 4 {
		t.Errorf("oldest remaining = %d, want 4", e.Seq)
	}
	files, _ := os.ReadDir(dir)
	if len(files) != 3 {
		t.Errorf("%d files on disk, want 3", len(files))
	}
	if err := q.Push(99, t0.Unix(), bytes.Repeat([]byte("y"), 101)); err != ErrTooLarge {
		t.Errorf("oversize push err = %v", err)
	}
}

func TestPersistsAcrossReopenAndMaxSeq(t *testing.T) {
	dir := t.TempDir()
	q := open(t, dir, 1<<20)
	_ = q.Push(10, t0.Unix(), []byte("a"))
	_ = q.Push(11, t0.Unix(), []byte("bb"))
	// stray temp file from a crash is cleaned up; junk names are ignored
	_ = os.WriteFile(filepath.Join(dir, ".tmp-123"), []byte("partial"), 0o600)
	_ = os.WriteFile(filepath.Join(dir, "README"), []byte("x"), 0o600)

	q2 := open(t, dir, 1<<20)
	if q2.Len() != 2 || q2.Bytes() != 3 || q2.MaxSeq() != 11 {
		t.Errorf("reopen: len=%d bytes=%d maxseq=%d", q2.Len(), q2.Bytes(), q2.MaxSeq())
	}
	if _, err := os.Stat(filepath.Join(dir, ".tmp-123")); !os.IsNotExist(err) {
		t.Error("temp file not cleaned")
	}
	if e, _ := q2.Peek(); e.Seq != 10 {
		t.Errorf("first = %d", e.Seq)
	}
}

func TestLoweredBudgetEnforcedOnOpen(t *testing.T) {
	dir := t.TempDir()
	q := open(t, dir, 1<<20)
	for s := int64(1); s <= 4; s++ {
		_ = q.Push(s, t0.Unix(), bytes.Repeat([]byte("z"), 40))
	}
	q2 := open(t, dir, 90)
	if q2.Bytes() > 90 || q2.Len() != 2 {
		t.Errorf("bytes=%d len=%d", q2.Bytes(), q2.Len())
	}
	if e, _ := q2.Peek(); e.Seq != 3 {
		t.Errorf("oldest = %d, want 3", e.Seq)
	}
}

func TestExpiredEntriesAreDiscardedNotReplayed(t *testing.T) {
	q := open(t, t.TempDir(), 1<<20)
	_ = q.Push(1, t0.Add(-2*time.Hour).Unix(), []byte("old"))
	_ = q.Push(2, t0.Add(-time.Minute).Unix(), []byte("fresh"))
	e, ok := q.Peek()
	if !ok || e.Seq != 2 || q.Expired() != 1 {
		t.Errorf("peek=%+v ok=%v expired=%d", e, ok, q.Expired())
	}
}

func TestUnreadableEntryIsSkipped(t *testing.T) {
	dir := t.TempDir()
	q := open(t, dir, 1<<20)
	_ = q.Push(1, t0.Unix(), []byte("a"))
	_ = q.Push(2, t0.Unix(), []byte("b"))
	if err := os.Remove(filepath.Join(dir, fileName(1, t0.Unix()))); err != nil {
		t.Fatal(err)
	}
	if e, ok := q.Peek(); !ok || e.Seq != 2 {
		t.Errorf("peek = %+v ok=%v", e, ok)
	}
}

func TestSameSeqReplaces(t *testing.T) {
	q := open(t, t.TempDir(), 1<<20)
	_ = q.Push(5, t0.Unix(), []byte("one"))
	_ = q.Push(5, t0.Unix(), []byte("two!"))
	if q.Len() != 1 || q.Bytes() != 4 {
		t.Errorf("len=%d bytes=%d", q.Len(), q.Bytes())
	}
}

func TestPermissions(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "q")
	q := open(t, dir, 1<<20)
	_ = q.Push(1, t0.Unix(), []byte("a"))
	di, _ := os.Stat(dir)
	if di.Mode().Perm() != 0o700 {
		t.Errorf("dir mode %v", di.Mode().Perm())
	}
	fi, _ := os.Stat(filepath.Join(dir, fileName(1, t0.Unix())))
	if fi.Mode().Perm() != 0o600 {
		t.Errorf("file mode %v", fi.Mode().Perm())
	}
}
