package telemetry

import (
	"context"
	"errors"
	"os"
	"runtime/debug"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	lksdk "github.com/livekit/server-sdk-go/v2"
	"github.com/pion/webrtc/v4"
	"github.com/pion/webrtc/v4/pkg/media"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	otelmetric "go.opentelemetry.io/otel/metric"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	semconv "go.opentelemetry.io/otel/semconv/v1.37.0"
)

const meterName = "livekit_publisher"

type Manager struct {
	enabled          bool
	provider         *sdkmetric.MeterProvider
	framesConsumed   otelmetric.Int64Counter
	frameSize        otelmetric.Int64Histogram
	framesPublished  otelmetric.Int64Counter
	publishErrors    otelmetric.Int64Counter
	consumeToPublish otelmetric.Float64Histogram
	publishDuration  otelmetric.Float64Histogram
	scheduleLag      otelmetric.Float64Histogram
	backlogEvents    otelmetric.Int64Counter

	mu            sync.RWMutex
	tracks        map[*TrackObserver]struct{}
	ice           atomic.Pointer[ICEState]
	registrations []otelmetric.Registration
}

type ICEState struct {
	ConnectionType      string
	Protocol            string
	LocalCandidateType  string
	RemoteCandidateType string
}

func EnabledFromEnvironment() bool {
	return os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT") != "" || os.Getenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT") != ""
}

func New(ctx context.Context) (*Manager, error) {
	m := &Manager{tracks: make(map[*TrackObserver]struct{})}
	if !EnabledFromEnvironment() {
		return m, nil
	}

	var exporter sdkmetric.Exporter
	var err error
	protocol := strings.ToLower(os.Getenv("OTEL_EXPORTER_OTLP_METRICS_PROTOCOL"))
	if protocol == "" {
		protocol = strings.ToLower(os.Getenv("OTEL_EXPORTER_OTLP_PROTOCOL"))
	}
	if strings.HasPrefix(protocol, "http/") {
		exporter, err = otlpmetrichttp.New(ctx)
	} else {
		exporter, err = otlpmetricgrpc.New(ctx)
	}
	if err != nil {
		return nil, err
	}

	version := "devel"
	if info, ok := debug.ReadBuildInfo(); ok && info.Main.Version != "" {
		version = info.Main.Version
	}
	res, err := resource.New(ctx,
		resource.WithFromEnv(),
		resource.WithTelemetrySDK(),
		resource.WithAttributes(
			semconv.ServiceName("livekit-publisher"),
			semconv.ServiceVersion(version),
		),
	)
	if err != nil {
		return nil, err
	}

	m.provider = sdkmetric.NewMeterProvider(sdkmetric.WithReader(sdkmetric.NewPeriodicReader(exporter)), sdkmetric.WithResource(res))
	otel.SetMeterProvider(m.provider)
	m.enabled = true
	if err := m.createInstruments(m.provider.Meter(meterName)); err != nil {
		_ = m.provider.Shutdown(ctx)
		return nil, err
	}
	return m, nil
}

func (m *Manager) createInstruments(meter otelmetric.Meter) error {
	var err error
	if m.framesConsumed, err = meter.Int64Counter("livekit_publisher.frames.consumed", otelmetric.WithUnit("{frame}")); err != nil {
		return err
	}
	if m.frameSize, err = meter.Int64Histogram("livekit_publisher.frame.size", otelmetric.WithUnit("By"), otelmetric.WithExplicitBucketBoundaries(1_000, 5_000, 10_000, 25_000, 50_000, 100_000, 250_000, 500_000, 1_000_000, 2_000_000)); err != nil {
		return err
	}
	if m.framesPublished, err = meter.Int64Counter("livekit_publisher.frames.published", otelmetric.WithUnit("{frame}")); err != nil {
		return err
	}
	if m.publishErrors, err = meter.Int64Counter("livekit_publisher.frames.publish_errors", otelmetric.WithUnit("{frame}")); err != nil {
		return err
	}
	latencyBuckets := otelmetric.WithExplicitBucketBoundaries(0.0001, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5)
	if m.consumeToPublish, err = meter.Float64Histogram("livekit_publisher.frame.consume_to_publish", otelmetric.WithUnit("s"), latencyBuckets); err != nil {
		return err
	}
	if m.publishDuration, err = meter.Float64Histogram("livekit_publisher.frame.publish_duration", otelmetric.WithUnit("s"), latencyBuckets); err != nil {
		return err
	}
	if m.scheduleLag, err = meter.Float64Histogram("livekit_publisher.backlog.schedule_lag", otelmetric.WithUnit("s"), latencyBuckets); err != nil {
		return err
	}
	if m.backlogEvents, err = meter.Int64Counter("livekit_publisher.backlog.events", otelmetric.WithUnit("{event}")); err != nil {
		return err
	}

	lagGauge, err := meter.Float64ObservableGauge("livekit_publisher.backlog.current_lag", otelmetric.WithUnit("s"))
	if err != nil {
		return err
	}
	iceGauge, err := meter.Int64ObservableGauge("livekit_publisher.ice.connection", otelmetric.WithUnit("{connection}"))
	if err != nil {
		return err
	}
	reg, err := meter.RegisterCallback(func(_ context.Context, observer otelmetric.Observer) error {
		m.mu.RLock()
		for track := range m.tracks {
			observer.ObserveFloat64(lagGauge, track.currentLag(), otelmetric.WithAttributes(track.attrs...))
		}
		m.mu.RUnlock()
		if state := m.ice.Load(); state != nil {
			observer.ObserveInt64(iceGauge, 1, otelmetric.WithAttributes(
				attribute.String("connection.type", state.ConnectionType),
				attribute.String("network.protocol", state.Protocol),
				attribute.String("local.candidate_type", state.LocalCandidateType),
				attribute.String("remote.candidate_type", state.RemoteCandidateType),
			))
		}
		return nil
	}, lagGauge, iceGauge)
	if err != nil {
		return err
	}
	m.registrations = append(m.registrations, reg)
	return nil
}

func (m *Manager) Enabled() bool { return m != nil && m.enabled }

func (m *Manager) Shutdown(ctx context.Context) error {
	if !m.Enabled() {
		return nil
	}
	for _, registration := range m.registrations {
		_ = registration.Unregister()
	}
	shutdownCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	return m.provider.Shutdown(shutdownCtx)
}

type TrackConfig struct {
	Name         string
	Codec        string
	Room         string
	Identity     string
	VideoQuality string
}

type TrackObserver struct {
	manager  *Manager
	attrs    []attribute.KeyValue
	lagNanos atomic.Int64
	closed   atomic.Bool
}

func (m *Manager) NewTrackObserver(config TrackConfig) lksdk.SampleObserver {
	if !m.Enabled() {
		return nil
	}
	attrs := []attribute.KeyValue{
		attribute.String("track.name", config.Name),
		attribute.String("codec", config.Codec),
		attribute.String("room", config.Room),
		attribute.String("participant.identity", config.Identity),
	}
	if config.VideoQuality != "" {
		attrs = append(attrs, attribute.String("video.quality", config.VideoQuality))
	}
	observer := &TrackObserver{manager: m, attrs: attrs}
	m.mu.Lock()
	m.tracks[observer] = struct{}{}
	m.mu.Unlock()
	return observer
}

func isFrame(sample media.Sample) bool { return len(sample.Data) > 0 && sample.Duration > 0 }

func (o *TrackObserver) OnSampleRead(sample media.Sample, _ time.Time) {
	if o.closed.Load() || !isFrame(sample) {
		return
	}
	ctx := context.Background()
	o.manager.framesConsumed.Add(ctx, 1, otelmetric.WithAttributes(o.attrs...))
	o.manager.frameSize.Record(ctx, int64(len(sample.Data)), otelmetric.WithAttributes(o.attrs...))
}

func (o *TrackObserver) OnSampleWriteComplete(sample media.Sample, result lksdk.SampleWriteResult) {
	if o.closed.Load() || !isFrame(sample) || result.Skipped {
		return
	}
	ctx := context.Background()
	if result.Err != nil {
		o.manager.publishErrors.Add(ctx, 1, otelmetric.WithAttributes(o.attrs...))
		return
	}
	o.manager.framesPublished.Add(ctx, 1, otelmetric.WithAttributes(o.attrs...))
	o.manager.consumeToPublish.Record(ctx, result.WriteCompletedAt.Sub(result.ReadCompletedAt).Seconds(), otelmetric.WithAttributes(o.attrs...))
	o.manager.publishDuration.Record(ctx, result.WriteCompletedAt.Sub(result.WriteStartedAt).Seconds(), otelmetric.WithAttributes(o.attrs...))
}

func (o *TrackObserver) OnSamplePacingLag(sample media.Sample, lag time.Duration) {
	if o.closed.Load() || !isFrame(sample) {
		return
	}
	if lag < 0 {
		lag = 0
	}
	o.lagNanos.Store(int64(lag))
	if lag > 0 {
		ctx := context.Background()
		o.manager.scheduleLag.Record(ctx, lag.Seconds(), otelmetric.WithAttributes(o.attrs...))
		o.manager.backlogEvents.Add(ctx, 1, otelmetric.WithAttributes(o.attrs...))
	}
}

func (o *TrackObserver) currentLag() float64 { return time.Duration(o.lagNanos.Load()).Seconds() }

func CloseTrackObserver(observer lksdk.SampleObserver) {
	o, ok := observer.(*TrackObserver)
	if !ok || !o.closed.CompareAndSwap(false, true) {
		return
	}
	o.lagNanos.Store(0)
	o.manager.mu.Lock()
	delete(o.manager.tracks, o)
	o.manager.mu.Unlock()
}

func ClassifyICEPair(pair *webrtc.ICECandidatePair) ICEState {
	state := ICEState{ConnectionType: "unknown", Protocol: "unknown", LocalCandidateType: "unknown", RemoteCandidateType: "unknown"}
	if pair == nil || pair.Local == nil || pair.Remote == nil {
		return state
	}
	state.Protocol = pair.Local.Protocol.String()
	state.LocalCandidateType = pair.Local.Typ.String()
	state.RemoteCandidateType = pair.Remote.Typ.String()
	if pair.Local.Typ == webrtc.ICECandidateTypeRelay || pair.Remote.Typ == webrtc.ICECandidateTypeRelay {
		state.ConnectionType = "turn"
	} else if pair.Local.Protocol == webrtc.ICEProtocolUDP && pair.Remote.Protocol == webrtc.ICEProtocolUDP {
		state.ConnectionType = "udp"
	}
	return state
}

func (m *Manager) SetICEPair(pair *webrtc.ICECandidatePair) {
	if !m.Enabled() {
		return
	}
	state := ClassifyICEPair(pair)
	m.ice.Store(&state)
}

func (m *Manager) ClearICE() {
	if m != nil {
		m.ice.Store(nil)
	}
}

func SelectedICEPair(pc *webrtc.PeerConnection) (*webrtc.ICECandidatePair, error) {
	if pc == nil || pc.SCTP() == nil || pc.SCTP().Transport() == nil || pc.SCTP().Transport().ICETransport() == nil {
		return nil, errors.New("publisher ICE transport is not ready")
	}
	return pc.SCTP().Transport().ICETransport().GetSelectedCandidatePair()
}
