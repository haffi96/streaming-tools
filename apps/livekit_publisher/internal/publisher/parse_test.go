package publisher

import (
	"testing"
)

func TestParsePublishTarget(t *testing.T) {
	target, err := ParsePublishTarget("h264://FRONT_CAMERA@127.0.0.1:5004")
	if err != nil {
		t.Fatalf("ParsePublishTarget returned error: %v", err)
	}
	if target.Codec != "h264" || target.Name != "FRONT_CAMERA" || target.Address != "127.0.0.1:5004" {
		t.Fatalf("unexpected target: %+v", target)
	}
	if target.HasDimensions() {
		t.Fatalf("expected no dimensions, got %+v", target)
	}
}

func TestParsePublishTargetWithDimensions(t *testing.T) {
	target, err := ParsePublishTarget("h265://REAR_CAMERA@127.0.0.1:5006/1280x720")
	if err != nil {
		t.Fatalf("ParsePublishTarget returned error: %v", err)
	}
	if target.Width != 1280 || target.Height != 720 {
		t.Fatalf("unexpected dimensions: %+v", target)
	}
}

func TestParsePublishTargetRejectsInvalid(t *testing.T) {
	cases := []string{
		"h264://127.0.0.1:5004",
		"h264://CAMERA@127.0.0.1",
		"h264://CAMERA@127.0.0.1:5004/notadim",
	}
	for _, input := range cases {
		if _, err := ParsePublishTarget(input); err == nil {
			t.Fatalf("expected parse error for %q", input)
		}
	}
}

func TestGroupPublishTargets(t *testing.T) {
	targets := mustTargets(t,
		"h264://FRONT_CAMERA@127.0.0.1:5004",
		"h264://REAR_CAMERA@127.0.0.1:5005",
	)
	groups, err := GroupPublishTargets(targets)
	if err != nil {
		t.Fatalf("GroupPublishTargets returned error: %v", err)
	}
	if len(groups) != 2 {
		t.Fatalf("expected 2 groups, got %d", len(groups))
	}
	for _, group := range groups {
		if group.Kind != PublishGroupIndependent {
			t.Fatalf("expected independent group, got %s", group.Kind)
		}
	}
}

func TestGroupPublishTargetsSimulcast(t *testing.T) {
	targets := mustTargets(t,
		"h264://FRONT_CAMERA@127.0.0.1:5005/1920x1080",
		"h264://FRONT_CAMERA@127.0.0.1:5006/1280x720",
		"h264://FRONT_CAMERA@127.0.0.1:5007/640x480",
	)
	groups, err := GroupPublishTargets(targets)
	if err != nil {
		t.Fatalf("GroupPublishTargets returned error: %v", err)
	}
	if len(groups) != 1 {
		t.Fatalf("expected 1 group, got %d", len(groups))
	}
	if groups[0].Kind != PublishGroupSimulcast {
		t.Fatalf("expected simulcast group, got %s", groups[0].Kind)
	}
}

func TestGroupPublishTargetsRejectsMixedCodecSimulcast(t *testing.T) {
	targets := mustTargets(t,
		"h264://FRONT_CAMERA@127.0.0.1:5005/1920x1080",
		"h265://FRONT_CAMERA@127.0.0.1:5006/1280x720",
	)
	if _, err := GroupPublishTargets(targets); err == nil {
		t.Fatal("expected mixed codec error")
	}
}

func TestGroupPublishTargetsRejectsDuplicateIndependentNames(t *testing.T) {
	targets := mustTargets(t,
		"h264://FRONT_CAMERA@127.0.0.1:5005",
		"h264://FRONT_CAMERA@127.0.0.1:5006",
	)
	if _, err := GroupPublishTargets(targets); err == nil {
		t.Fatal("expected duplicate name error")
	}
}

func mustTargets(t *testing.T, raws ...string) []PublishTarget {
	t.Helper()
	targets := make([]PublishTarget, 0, len(raws))
	for _, raw := range raws {
		target, err := ParsePublishTarget(raw)
		if err != nil {
			t.Fatalf("ParsePublishTarget(%q) error: %v", raw, err)
		}
		targets = append(targets, target)
	}
	return targets
}
