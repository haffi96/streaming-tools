package publisher

import (
	"testing"
	"time"
)

func TestSessionStateExitAfterPublishWaitsForAllGroups(t *testing.T) {
	state := newSessionState(true, []PublishGroup{
		{Name: "front"},
		{Name: "rear"},
	})

	state.markGroupComplete("front")

	select {
	case <-state.done:
		t.Fatal("done channel closed too early")
	default:
	}

	state.markGroupComplete("rear")

	select {
	case <-state.done:
	case <-time.After(100 * time.Millisecond):
		t.Fatal("done channel did not close after all groups completed")
	}
}

func TestShouldRetry(t *testing.T) {
	if !shouldRetry(-1, 100) {
		t.Fatal("expected infinite retry to keep retrying")
	}
	if !shouldRetry(3, 0) {
		t.Fatal("expected first retry to be allowed for limit 3")
	}
	if !shouldRetry(3, 2) {
		t.Fatal("expected third retry slot to be allowed for limit 3")
	}
	if shouldRetry(3, 3) {
		t.Fatal("expected retry 4 to be rejected once 3 retries were used")
	}
	if shouldRetry(0, 0) {
		t.Fatal("expected zero reconnect attempts to disable retries")
	}
}
