package telemetry

import (
	"context"
	"os"
	"testing"
	"time"

	lksdk "github.com/livekit/server-sdk-go/v2"
	"github.com/pion/webrtc/v4"
	"github.com/pion/webrtc/v4/pkg/media"
)

func TestClassifyICEPair(t *testing.T) {
	tests := []struct {
		name string
		pair *webrtc.ICECandidatePair
		want string
	}{
		{name: "missing", want: "unknown"},
		{name: "direct udp", pair: &webrtc.ICECandidatePair{Local: &webrtc.ICECandidate{Protocol: webrtc.ICEProtocolUDP, Typ: webrtc.ICECandidateTypeHost}, Remote: &webrtc.ICECandidate{Protocol: webrtc.ICEProtocolUDP, Typ: webrtc.ICECandidateTypeHost}}, want: "udp"},
		{name: "turn", pair: &webrtc.ICECandidatePair{Local: &webrtc.ICECandidate{Protocol: webrtc.ICEProtocolUDP, Typ: webrtc.ICECandidateTypeRelay}, Remote: &webrtc.ICECandidate{Protocol: webrtc.ICEProtocolUDP, Typ: webrtc.ICECandidateTypeHost}}, want: "turn"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := ClassifyICEPair(test.pair).ConnectionType; got != test.want {
				t.Fatalf("connection type = %q, want %q", got, test.want)
			}
		})
	}
}

func TestOTLPExport(t *testing.T) {
	if os.Getenv("LIVEKIT_PUBLISHER_TEST_OTLP") == "" {
		t.Skip("set LIVEKIT_PUBLISHER_TEST_OTLP=1 to exercise a running collector")
	}
	m, err := New(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	observer := m.NewTrackObserver(TrackConfig{Name: "otel-test", Codec: "h264", Room: "testing", Identity: "test"})
	now := time.Now()
	sample := media.Sample{Data: []byte{1, 2, 3}, Duration: 33 * time.Millisecond}
	observer.OnSampleRead(sample, now)
	observer.OnSampleWriteComplete(sample, lksdk.SampleWriteResult{ReadCompletedAt: now, WriteStartedAt: now.Add(time.Millisecond), WriteCompletedAt: now.Add(2 * time.Millisecond)})
	observer.OnSamplePacingLag(sample, time.Millisecond)
	time.Sleep(2 * time.Second)
	CloseTrackObserver(observer)
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := m.Shutdown(shutdownCtx); err != nil {
		t.Fatal(err)
	}
}

func TestDisabledManagerIsNoOp(t *testing.T) {
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
	t.Setenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "")
	m, err := New(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if m.Enabled() {
		t.Fatal("manager unexpectedly enabled")
	}
	if observer := m.NewTrackObserver(TrackConfig{Name: "camera"}); observer != nil {
		t.Fatal("disabled manager returned observer")
	}
	if err := m.Shutdown(t.Context()); err != nil {
		t.Fatal(err)
	}
}
